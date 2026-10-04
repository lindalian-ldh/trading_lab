#!/usr/bin/env python3
"""ETF 模式验证脚本。

两部分：
  A. 离线部分（无网络，快）：代码识别 / 标的指数映射 / 覆盖规则 / 边界
  B. 在线部分（需网络，慢）：用真实 ETF 日线对比"默认参数 vs ETF 参数"，
     验证"偏离阈值 0.5% 对 ETF 太严"这个判断，以及基准指数是否真的切换了。

用法:
    uv run python services/calc_indicators/verify_etf_mode.py
    uv run python services/calc_indicators/verify_etf_mode.py --offline   # 只跑离线部分
"""

from __future__ import annotations

import os
import sys
from copy import deepcopy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import etf
from config import TradingConfig

_OFFLINE_ONLY = "--offline" in sys.argv


def results_row(ok, label: str, detail: str) -> bool:
    val = bool(ok)
    print(f"    {'✅' if val else '❌'} {label:36s} {detail}")
    return val


# ==================== A. 离线部分 ====================

def check_mapping_table() -> list:
    print("【1】映射表合法性（代码/指数格式、字段真实存在）")
    out = []
    try:
        etf.assert_loaded()
        out.append(results_row(True, "assert_loaded()", "映射表结构合法"))
    except AssertionError as e:
        out.append(results_row(False, "assert_loaded()", f"断言失败: {e}"))

    # 所有映射到的指数代码必须能被 market_filter 归一化（否则取数必失败）
    from market_filter import _normalize_index_code
    bad = []
    for code, (idx, _name, _s) in etf._ETF_MAP.items():
        tx, bs = _normalize_index_code(idx)
        if not (tx[:2] in ("sh", "sz") and bs.count(".") == 1):
            bad.append(f"{code}->{idx}")
    out.append(results_row(not bad, "指数代码可归一化",
                           "全部通过" if not bad else f"异常: {bad[:3]}"))
    return out


def check_etf_detection() -> list:
    print("\n【2】ETF 代码识别")
    out = []
    cases = [
        ("510300", True), ("159915", True), ("588000", True), ("512880", True),
        ("518880", True), ("511990", True), ("513100", True),
        ("600519", False), ("000001", False), ("300750", False), ("601899", False),
        ("", False), ("12345", False), ("ABCDEF", False), ("15991", False),
    ]
    wrong = [f"{c}→{etf.is_etf(c)}(期望{v})" for c, v in cases if etf.is_etf(c) != v]
    out.append(results_row(not wrong, f"{len(cases)} 个代码识别",
                           "全部正确" if not wrong else f"错误: {wrong}"))
    return out


def check_index_resolution() -> list:
    print("\n【3】标的指数解析（重点：不再一律用上证综指）")
    out = []
    cases = [
        ("510300", "sh000300"), ("159915", "sz399006"), ("510500", "sh000905"),
        ("510050", "sh000016"), ("512100", "sh000852"), ("588000", "sh000688"),
    ]
    wrong = []
    for code, expect in cases:
        r = etf.resolve_underlying(code)
        if r["index"] != expect:
            wrong.append(f"{code}→{r['index']}(期望{expect})")
    out.append(results_row(not wrong, "已收录宽基ETF映射",
                           "全部正确" if not wrong else f"错误: {wrong}"))

    # 未收录 ETF：必须明确警告"按代码段兜底"，不能静默
    unk = etf.resolve_underlying("519999")
    out.append(results_row(
        (not unk["mapped"]) and any("不在内置映射表" in w for w in unk["warnings"]),
        "未收录ETF给出显式警告", f"index={unk['index']} warnings={len(unk['warnings'])} 条"))

    # 科创50：baostock 无该指数 → 必须提示维度E会取不到
    kc = etf.resolve_underlying("588000")
    out.append(results_row(
        any("baostock 中不存在" in w for w in kc["warnings"]),
        "科创50提示指数缺失", f"suitable={kc['suitable']}"))

    # 债券/货币/商品/跨境 → suitable=False
    for code in ("511990", "518880", "513100", "512880"):
        r = etf.resolve_underlying(code)
        out.append(results_row(not r["suitable"], f"{code} 标注不适合本工具",
                               f"warnings={len(r['warnings'])} 条"))
    return out


def check_theme_anchor() -> list:
    """【3b】主题锚：未收录的行业/主题 ETF 按**名称关键词**选锚。

    为什么必须测：这是 2026-10-01 新增的能力，它改的是"看哪张天气图"——
    改错了会让维度B/维度E 静默看错盘，且不报错。故逐条钉死。
    注意：本函数**不联网**，全部用假名称喂 match_theme_index（纯函数）。
    """
    print("\n【3b】主题锚指数（按 ETF 名称关键词，纯函数不联网）")
    out = []

    # ① 行业/主题关键词 → 创业板指（半导体在创业板权重高；科创50 baostock 无数据）
    cases = [
        ("华夏中证半导体材料设备主题ETF", "sz399006"),
        ("国泰CES芯片ETF", "sz399006"),
        ("易方达中证云计算与大数据主题ETF", "sz399006"),
        ("国泰中证消费电子主题ETF", "sz399006"),
        ("国泰恒生A股电网设备ETF", "sz399006"),
        ("华夏上证科创板半导体材料设备主题ETF", "sz399006"),
    ]
    wrong = []
    for nm, expect in cases:
        h = etf.match_theme_index(nm)
        got = h[0] if h else None
        if got != expect:
            wrong.append(f"{nm[:12]}→{got}(期望{expect})")
    out.append(results_row(not wrong, "半导体/芯片/电网/云计算 → 创业板指",
                           "全部正确" if not wrong else f"错误: {wrong}"))

    # ② 宽基不能被主题规则改掉（reason='ok' 的映射是已核对过的）
    for code, expect in (("510300", "sh000300"), ("510500", "sh000905")):
        r = etf.resolve_underlying(code, name="华夏中证半导体ETF")
        out.append(results_row(r["index"] == expect and not r["theme_matched"],
                               f"{code} 宽基不被主题规则覆盖",
                               f"index={r['index']} theme_matched={r['theme_matched']}"))

    # ③ 防御性公用事业 vs 成长性电力设备 必须分开
    z = etf.match_theme_index("国泰中证全指电力公用事业ETF")
    d = etf.match_theme_index("国泰恒生A股电网设备ETF")
    out.append(results_row(
        bool(z) and z[0] == "sh000001" and bool(d) and d[0] == "sz399006",
        "电力公用事业(防御)→上证 与 电网设备(成长)→创业板 区分",
        f"公用事业={z[0] if z else None} 电网={d[0] if d else None}"))

    # ④ 商品类必须带警示（黄金与A股低相关，锚意义有限）
    g = etf.resolve_underlying("517520", name="永赢中证沪深港黄金产业股票ETF")
    out.append(results_row(
        "商品类" in (g["index_name"] or "") and g["theme_matched"],
        "黄金/商品类锚带「意义有限」警示", f"index_name={g['index_name']}"))

    # ⑤ 名称缺失时不得乱匹配，且要告警
    noname = etf.resolve_underlying("561380")
    out.append(results_row(
        not noname["theme_matched"] and any("主题" in w or "不在内置映射表" in w
                                           for w in noname["warnings"]),
        "无名称时不误匹配且有告警",
        f"index={noname['index']} warnings={len(noname['warnings'])}"))

    # ⑥ ETF_INDEX_OVERRIDE 优先级最高（手工指定锚的逃生口）
    cfg = TradingConfig()
    cfg.ETF_INDEX_OVERRIDE = {"562590": ("sh000300", "沪深300")}
    r = etf.resolve_underlying("562590", name="华夏中证半导体材料设备主题ETF", config=cfg)
    out.append(results_row(
        r["index"] == "sh000300" and r["theme_matched"],
        "ETF_INDEX_OVERRIDE 覆盖关键词匹配", f"index={r['index']}"))

    # ⑦ 科创锚可切换：默认创业板指 / THEME_ANCHOR_KC=kc50 用科创50
    nm = "华夏中证半导体材料设备主题ETF"
    c_def = TradingConfig()
    c_kc = TradingConfig(); c_kc.THEME_ANCHOR_KC = "kc50"
    r_def = etf.resolve_underlying("562590", name=nm, config=c_def)
    r_kc = etf.resolve_underlying("562590", name=nm, config=c_kc)
    out.append(results_row(
        r_def["index"] == "sz399006" and r_kc["index"] == "sh000688",
        "科创锚可切换（默认创业板指 / kc50 科创50）",
        f"默认={r_def['index']} kc50={r_kc['index']}"))

    # ⑦b 非半导体主题不受 kc50 影响（云计算仍走创业板指）——防止 $KC 范围过大
    nm2 = "易方达中证云计算与大数据主题ETF"
    a2 = etf.resolve_underlying("516510", name=nm2, config=c_def)
    b2 = etf.resolve_underlying("516510", name=nm2, config=c_kc)
    out.append(results_row(
        a2["index"] == "sz399006" and b2["index"] == "sz399006",
        "kc50 不影响非半导体主题（云计算→创业板指）",
        f"默认={a2['index']} kc50={b2['index']}"))

    # ⑦c 科创50 的说明必须诚实（不能说"两段同为正"）
    kc50w = " ".join(r_kc["warnings"])
    out.append(results_row(
        "无法做双体制校验" in kc50w and "不显著" in kc50w,
        "科创50 警告如实说明验证局限",
        "含「无法做双体制校验」与「不显著」" if "不显著" in kc50w else "缺失"))

    # ⑦d 其它锚的说明仍是"两段同为正"（未被 ⑦c 改动搞坏）
    cybw = " ".join(r_def["warnings"])
    out.append(results_row("两段同为正" in cybw,
                           "创业板指警告仍为两段同为正", "ok" if "两段同为正" in cybw else "缺失"))

    # ⑧ 开关可关闭
    cfg2 = TradingConfig()
    cfg2.ETF_THEME_ANCHOR_ENABLED = False
    r2 = etf.resolve_underlying("562590", name="华夏中证半导体材料设备主题ETF", config=cfg2)
    out.append(results_row(not r2["theme_matched"],
                           "ETF_THEME_ANCHOR_ENABLED=False 可关闭",
                           f"index={r2['index']} theme_matched={r2['theme_matched']}"))
    return out


def check_override_rules() -> list:
    print("\n【4】覆盖规则（只做单向放宽；且不得放宽回踩阈值）")
    out = []

    # ① 默认档：应切换基准 + 下调 ETF 放量门槛
    c = TradingConfig()
    ov = etf.build_config_overrides("510300", c)
    ok = (ov.get("BENCHMARK_INDEX") == "sh000300"
          and ov.get("VOLUME_SURGE_RATIO") == 1.25)
    out.append(results_row(ok, "默认档施加 ETF 参数", f"{sorted(ov.keys())}"))

    # ② 回归保护：**刻意不放开回踩偏离/缩量/RSI超卖**
    #    实测 ETF 的偏离度中位数反而小于个股，0.5% 已能覆盖 30~37% 的交易日；
    #    放宽到 1.5% 会把"价格在均线上方 1.5%"的追高日也当回踩买点。
    no_touch = [k for k in ("PULLBACK_MAX_DEVIATION", "PULLBACK_VOLUME_SHRINK_RATIO",
                            "RSI_OVERSOLD") if k in ov]
    out.append(results_row(not no_touch, "不碰回踩/缩量/RSI阈值（实测依据）",
                           f"未覆盖={no_touch if no_touch else '无（正确）'}"))

    # ③ 用户已设更宽：不得被收紧
    c2 = TradingConfig()
    c2.VOLUME_SURGE_RATIO = 1.05            # 比 ETF 推荐 1.25 更宽
    ov2 = etf.build_config_overrides("510300", c2)
    out.append(results_row("VOLUME_SURGE_RATIO" not in ov2, "更宽的设定不被收紧",
                           f"overrides={sorted(ov2.keys())}"))

    # ④ 非 ETF 必须零副作用
    c4 = TradingConfig()
    ov4 = etf.build_config_overrides("600519", c4)
    applied = etf.apply_etf_overrides(c4, "600519")
    out.append(results_row(ov4 == {} and applied == {} and c4.BENCHMARK_INDEX == "sh000001",
                           "非ETF零副作用", f"overrides={ov4} 变更={applied}"))

    # ⑤ apply 后 config 真的变了，且变更记录可用于对账
    c5 = TradingConfig()
    changed = etf.apply_etf_overrides(c5, "510300")
    ok5 = (c5.BENCHMARK_INDEX == "sh000300" and c5.VOLUME_SURGE_RATIO == 1.25
           and set(changed) == {"VOLUME_SURGE_RATIO", "BENCHMARK_INDEX"})
    out.append(results_row(ok5, "apply 生效且变更记录完整", f"{sorted(changed)}"))

    # ⑥ 幂等：重复 apply 不产生新变更
    again = etf.apply_etf_overrides(c5, "510300")
    out.append(results_row(again == {}, "幂等（重复 apply 无新变更）", f"第二次变更={again}"))
    return out


# ==================== B. 在线部分 ====================

def check_real_etf_effect() -> list:
    print("\n【5】真实 ETF 数据：基准指数切换 + 偏离阈值不放开的事实依据")
    out = []
    import main as M
    from config import TradingConfig as TC

    # ── 5a：基准指数确实按标的切换，且能取到数据 ──
    expect_map = {
        "510300": "sh000300", "510500": "sh000905", "159915": "sz399006",
        "510050": "sh000016", "512100": "sh000852",
    }
    print("    5a. 基准指数切换（ETF 模式的核心修复）")
    wrong = []
    for code, expect in expect_map.items():
        cfg = TC()
        cfg.PULLBACK_DEBUG = False
        etf.apply_etf_overrides(cfg, code)
        if cfg.BENCHMARK_INDEX != expect:
            wrong.append(f"{code}→{cfg.BENCHMARK_INDEX}(期望{expect})")
    out.append(results_row(not wrong, "5 只宽基ETF 基准切换正确",
                           "全部正确" if not wrong else f"错误: {wrong}"))

    # ── 5b：实测环境下的通过情况（记录事实，不做断言）──
    print("    5b. 当日维度A 实测（用于人工查看，不做通过率断言）")
    print(f"    {'代码':8s}{'名称':13s}{'默认A':>8s}{'ETF模式A':>10s}{'基准(默认→ETF)':>26s}")
    for code, name in [("510300", "沪深300ETF"), ("510500", "中证500ETF"),
                       ("159915", "创业板ETF"), ("510050", "上证50ETF"),
                       ("512100", "中证1000ETF")]:
        base = TC()
        base.PULLBACK_DEBUG = False
        etf_cfg = deepcopy(base)
        etf.apply_etf_overrides(etf_cfg, code)
        try:
            df = M.fetch_data(code, base)
        except SystemExit:
            print(f"    {code:8s}{name:13s}  ❌ 取数失败（跳过）")
            continue
        a_base, _ = M.check_dimension_a(df, base)
        a_etf, _ = M.check_dimension_a(df, etf_cfg)
        fmt = lambda ok: "✅通过" if ok else "❌不通过"
        print(f"    {code:8s}{name:13s}{fmt(a_base):>9s}{fmt(a_etf):>11s}"
              f"   {base.BENCHMARK_INDEX} → {etf_cfg.BENCHMARK_INDEX}")

    # ── 5c：用事实守住"不放开偏离阈值"这个决定 ──
    #     若哪天有人想把 ETF 的 PULLBACK_MAX_DEVIATION 放宽到 1.5%，
    #     这段会显示：那等于接受"价格在 MA5 上方 1.5%"的追高日。
    print("    5c. 为什么 ETF 不放宽回踩偏离阈值（近 120 日实测）")
    print(f"    {'代码':8s}{'名称':13s}{'偏离中位数':>11s}{'≤0.5%占比':>11s}{'>1.5%占比(追高日)':>19s}")
    base = TC()
    base.PULLBACK_DEBUG = False
    coverage_ok = True      # 0.5% 必须能稳定覆盖（否则才叫"太严"）
    shows_risk = False      # 至少要有一个样本证明放宽到 1.5% 会纳入大量追高日
    for code, name in [("510300", "沪深300ETF"), ("510500", "中证500ETF"),
                       ("159915", "创业板ETF"), ("510050", "上证50ETF")]:
        try:
            df = M.fetch_data(code, base)
        except SystemExit:
            continue
        c = df["close"]
        dev = ((c - c.rolling(5).mean()) / c.rolling(5).mean() * 100).dropna()
        med = dev.abs().median()
        p05 = (dev.abs() <= 0.5).mean() * 100
        p15 = (dev > 1.5).mean() * 100
        print(f"    {code:8s}{name:13s}{med:10.2f}%{p05:10.0f}%{p15:18.1f}%")
        if p05 < 10:
            coverage_ok = False
        if p15 >= 10:
            shows_risk = True
    # 注意：不拿"中位数"当判据 —— 创业板ETF 中位数 1.81% 属正常分布（波动本就更大），
    # 真正要证的是两件事：① 0.5% 覆盖率够（不是"太严"）；② 放宽确实会纳入追高日。
    out.append(results_row(
        coverage_ok, "偏离阈值 0.5% 对 ETF 已足够",
        "各宽基ETF 覆盖率均 >10%（能稳定触发，不是'太严'）"))
    out.append(results_row(
        shows_risk, "放宽到 1.5% 会纳入追高日",
        "存在 '价格在MA5上方>1.5%' 占比≥10% 的样本 → 放宽会引入劣质信号"))
    return out


def main() -> int:
    print("=" * 76)
    print("ETF 模式验证")
    print("=" * 76)
    res = []
    res += check_mapping_table()
    res += check_etf_detection()
    res += check_index_resolution()
    res += check_theme_anchor()
    res += check_override_rules()
    if _OFFLINE_ONLY:
        print("\n（--offline：跳过需要联网的真实数据对比）")
    else:
        res += check_real_etf_effect()
    print("=" * 76)
    ok, total = sum(res), len(res)
    print(f"结果: {ok}/{total} 项通过  {'✅ 全部通过' if ok == total else '❌ 存在失败项'}")
    print("=" * 76)
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
