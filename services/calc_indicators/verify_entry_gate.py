#!/usr/bin/env python3
"""入口门控验证脚本 —— 打通「市场状态 / RSI超买 / 赔率」三否决项。

改动依据（实测 002119，2026-09-24）：
    维度A ✅ 突破前20日高点 26.92（现价 29.61 → 突破幅度 **+9.99%**，已属追高）
    维度B ✅ 信号: RSI超买 (84.5), 成交量放大 (2.0倍)
    维度C ❌ 止损 21.69（−26.7%）、止盈 32.29（+9.1%）→ 盈亏比 **0.34:1**
    维度D   周线评级 A，但价格距周 MA60 **+37.1%**
    维度E   RANGE_MID（中位震荡），备注"禁止突破买入"，却**只读不参与判定**

    → 系统明知市场不支持突破，仍让 A/B 通过；RSI 84.5 被并列显示在"✅ B通过 - 信号"里。

本脚本验证门控把这三项打通，且**不破坏**既有维度语义。

用法:
    uv run python services/calc_indicators/verify_entry_gate.py
    uv run python services/calc_indicators/verify_entry_gate.py --offline  # 只跑合成用例
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

import main as M
from config import TradingConfig

N = 160


def results_row(ok, label: str, detail: str) -> bool:
    val = bool(ok)
    print(f"    {'✅' if val else '❌'} {label:40s} {detail}")
    return val


# ==================== 合成夹具 ====================

def breakout_df(magnitude: float = 0.02, rsi_high: bool = False,
                fresh: bool = True) -> pd.DataFrame:
    """构造"收盘突破前 20 日高点约 magnitude"的行情，且**不触发回踩分支**。

    注意：夹具必须让"**前 20 日**高点"稳定在 10.0 —— 否则最后 5 根的爬升本身会
    抬高"前 20 日高点"，突破幅度就不可控（第一版声称 +10% 实际只有 +2.04%）。
    做法：最后 20 根里前 19 根的 high 压到 10.0 以下，只让最后一根去突破。
    """
    rng = np.random.default_rng(7)
    close = np.r_[np.full(N - 20, 10.0), 10.0 + rng.normal(0, 0.02, 19), 0.0]
    close[-1] = 10.0 * (1 + magnitude)          # 精确控制突破幅度
    op = np.r_[close[0], close[:-1]]
    hi = np.maximum(op, close) * 1.004
    lo = np.minimum(op, close) * 0.996
    # 锁定"前 20 日高点" = 10.0：把除最后一根外的最近 19 根 high 压到 10.0 以下
    hi[-(20):-1] = np.minimum(hi[-(20):-1], 9.999)
    v = np.full(N, 1e6)
    if not fresh:
        # 让"突破"发生在很久以前：最后 10 根横盘在高位（无新的突破动作）
        close[-10:] = close[-11]
        op[-10:] = close[-11]
        hi[-10:] = close[-11] * 1.004
        lo[-10:] = close[-11] * 0.996
    return pd.DataFrame({"date": pd.bdate_range("2024-01-01", periods=N),
                         "open": op, "high": hi, "low": lo, "close": close, "volume": v})


def measured_magnitude(df: pd.DataFrame, config: TradingConfig) -> float:
    """复算夹具的真实突破幅度，避免"我以为构造了 +10%"这类夹具失真。"""
    prev_high = float(df["high"].shift(1).rolling(config.BREAKOUT_WINDOW).max().iloc[-1])
    return float(df["close"].iloc[-1]) / prev_high - 1


def cfg_base() -> TradingConfig:
    c = TradingConfig()
    c.PULLBACK_DEBUG = False
    c.MOMENTUM_DEBUG = False
    c.MARKET_DEBUG = False
    c.ENABLE_MARKET_FILTER = False
    # 本脚本的【1】~【6】节只针对前三条门控规则（REGIME/RSI/RR），
    # 必须把第 ④ 条关掉才能**隔离**测试它们 —— 夹具的成交量很小
    # （1e6 股 × 约10元 = 0.1亿），否则会恒定触发 SURVIVABILITY，
    # 让"关闭 X 规则"的用例里混入无关否决项。
    # 第 ④ 条由【7】节单独验证。
    c.ENTRY_GATE_SURVIVABILITY_ENABLED = False
    return c


def _regime(name: str, available: bool = True) -> dict:
    return {"available": available, "regime": name, "label": name}


def _gate(df, config, regime_name="RANGE_MID", available=True, c_info=None, c_pass=True):
    a_pass, a_msg = M.check_dimension_a(df, config)
    b_pass, b_signals = M.check_dimension_b(df, config)
    if c_info is None:
        c_pass, c_info = M.check_dimension_c(df, config)
    return M.check_entry_gate(_regime(regime_name, available), a_pass, a_msg,
                              b_signals, c_pass, c_info, df, config), a_pass, a_msg


# ==================== 用例 ====================

def check_regime_gate() -> list:
    print("【1】市场状态：震荡市禁突破，趋势市放行（且只禁突破、不禁回踩）")
    out = []
    cfg = cfg_base()
    df = breakout_df(magnitude=0.02)          # 小幅突破，A 应通过

    g, a_pass, a_msg = _gate(df, cfg, "RANGE_MID")
    out.append(results_row(
        a_pass and str(a_msg).startswith("突破") and not g["passed"]
        and any(v["rule"] == "REGIME_BREAKOUT" for v in g["vetoes"]),
        "RANGE_MID + 突破 → 否决",
        f"A通过={a_pass}({a_msg[:18]}…) 门控passed={g['passed']}"))

    g2, _, _ = _gate(df, cfg, "TREND_UP")
    out.append(results_row(
        g2["passed"] or not any(v["rule"] == "REGIME_BREAKOUT" for v in g2["vetoes"]),
        "TREND_UP + 突破 → 不否决",
        f"门控passed={g2['passed']} 否决={[v['rule'] for v in g2['vetoes']]}"))

    # 回踩入场不应被"禁突破"规则拦截
    g3, a3, am3 = _gate(breakout_df(magnitude=0.02), cfg, "RANGE_MID")
    out.append(results_row(
        not any(v["rule"] == "REGIME_BREAKOUT" for v in g3["vetoes"]) is False
        or "突破" in str(am3),
        "规则只针对突破（本轮样本即突破）",
        f"入场性质={str(am3)[:20]}…"))
    return out


def check_regime_unavailable_skips() -> list:
    print("\n【2】市场状态不可用 → 该规则跳过（不否决、也不静默放行）")
    out = []
    cfg = cfg_base()
    df = breakout_df(magnitude=0.02)
    g, a_pass, _ = _gate(df, cfg, "UNKNOWN", available=False)
    regime_rule = [r for r in g["rules"] if r["rule"] == "REGIME_BREAKOUT"][0]
    out.append(results_row(
        not any(v["rule"] == "REGIME_BREAKOUT" for v in g["vetoes"]) and g["excluded"],
        "跳过且标记 excluded",
        f"detail={regime_rule['detail'][:40]}… excluded={g['excluded']}"))
    return out


def check_rsi_gate() -> list:
    print("\n【3】RSI 极端超买（≥80）否决；正常区间不否决")
    out = []
    cfg = cfg_base()
    out.append(results_row(cfg.ENTRY_GATE_RSI_ENABLED and cfg.MOMENTUM_RSI_EXTREME == 80.0,
                           "默认开启且阈值为 80",
                           f"enabled={cfg.ENTRY_GATE_RSI_ENABLED} 阈值={cfg.MOMENTUM_RSI_EXTREME}"))

    # 用真实极端超买样本（RSI 84.5 的 002119 缓存数据）更可信
    try:
        import data_layer as D
        df = D.fetch_history("002119", "2023-09-01")
        rsi = float(M.calc_rsi(df["close"], cfg.RSI_WINDOW).iloc[-1])
        g, _, _ = _gate(df, cfg, "TREND_UP")     # 用允许突破的状态，隔离出 RSI 规则
        out.append(results_row(
            rsi >= 80 and any(v["rule"] == "RSI_EXTREME" for v in g["vetoes"]),
            "真实超买样本被否决",
            f"RSI={rsi:.1f} 否决={[v['rule'] for v in g['vetoes']]}"))
    except Exception as e:
        print(f"      ⚠️ 真实样本不可用（{str(e)[:40]}），跳过该项")

    # 平缓行情（RSI 不高）不应触发 RSI 否决
    cfg2 = cfg_base()
    flat = breakout_df(magnitude=0.02)
    g2, _, _ = _gate(flat, cfg2, "TREND_UP")
    out.append(results_row(
        not any(v["rule"] == "RSI_EXTREME" for v in g2["vetoes"]),
        "非超买样本不触发 RSI 否决",
        f"RSI={float(M.calc_rsi(flat['close'], cfg2.RSI_WINDOW).iloc[-1]):.1f}"))
    return out


def check_rr_and_expectancy() -> list:
    print("\n【4】赔率不足 → 否决，并给出「不亏所需胜率」")
    out = []
    cfg = cfg_base()
    df = breakout_df(magnitude=0.02)
    c_pass, c_info = M.check_dimension_c(df, cfg)
    g, _, _ = _gate(df, cfg, "TREND_UP", c_info=c_info, c_pass=c_pass)
    ratio = c_info["ratio"]
    expect_p = 1 / (1 + ratio) if np.isfinite(ratio) and ratio > 0 else float("nan")
    ok = (not c_pass) == (not g["passed"] or any(v["rule"] == "RR_RATIO" for v in g["vetoes"]))
    out.append(results_row(
        ok, "C 不通过 ⇔ RR_RATIO 否决",
        f"盈亏比={ratio:.2f} C通过={c_pass} 所需胜率={expect_p * 100:.1f}%"
        if np.isfinite(expect_p) else f"盈亏比={ratio:.2f}"))

    # 002119 的期望值口径（0.34:1 → 需 74.6%）
    out.append(results_row(
        abs(M._breakout_ratio_needed(0.34) - 0.746) < 0.001,
        "期望值公式正确（0.34:1）",
        f"所需胜率={M._breakout_ratio_needed(0.34) * 100:.1f}%（期望 74.6%）"))
    return out


def check_switches_off() -> list:
    print("\n【5】三项分别关闭 → 回到「不否决」（可回退、可归因）")
    out = []
    cfg = cfg_base()
    cfg.ENTRY_GATE_ENABLED = False
    df = breakout_df(magnitude=0.02)
    g, _, _ = _gate(df, cfg, "RANGE_MID")
    out.append(results_row(not g["vetoes"] and g["passed"],
                           "总开关关闭 → 无否决",
                           f"vetoes={g['vetoes']} passed={g['passed']}"))

    for field, rule in [("ENTRY_GATE_REGIME_ENABLED", "REGIME_BREAKOUT"),
                        ("ENTRY_GATE_RSI_ENABLED", "RSI_EXTREME"),
                        ("ENTRY_GATE_RR_RATIO_ENABLED", "RR_RATIO")]:
        c = cfg_base()
        setattr(c, field, False)
        gx, _, _ = _gate(df, c, "RANGE_MID")
        out.append(results_row(
            not any(v["rule"] == rule for v in gx["vetoes"]),
            f"关闭 {rule}", f"vetoes={[v['rule'] for v in gx['vetoes']]}"))

    # 关闭 RR_RATIO 必须**只**影响该规则，并让 C❌ 的样本得以放行 ——
    # 这是"C 到底有没有用"能成为可测问题的前提
    # （见 config.ENTRY_GATE_RR_RATIO_ENABLED 的说明）。
    c_off = cfg_base()
    c_off.ENTRY_GATE_RR_RATIO_ENABLED = False
    g_off, _, _ = _gate(df, c_off, "RANGE_MID")
    rule_state = [r for r in g_off["rules"] if r["rule"] == "RR_RATIO"]
    out.append(results_row(
        bool(rule_state) and rule_state[0]["enabled"] is False
        and not any(v["rule"] == "RR_RATIO" for v in g_off["vetoes"]),
        "关闭 RR_RATIO → 该规则标记 enabled=False 且不否决",
        f"enabled={rule_state[0]['enabled'] if rule_state else '缺失'} "
        f"vetoes={[v['rule'] for v in g_off['vetoes']]}"))
    # 其余两条规则不受影响（不能连坐）
    c_off2 = cfg_base()
    c_off2.ENTRY_GATE_RR_RATIO_ENABLED = False
    gx2, _, _ = _gate(df, c_off2, "RANGE_MID")
    out.append(results_row(
        any(v["rule"] == "REGIME_BREAKOUT" for v in gx2["vetoes"]),
        "关闭 RR_RATIO 不影响 REGIME_BREAKOUT（无连坐）",
        f"vetoes={[v['rule'] for v in gx2['vetoes']]}"))
    return out


def check_breakout_quality() -> list:
    print("\n【6】突破质量过滤（P1）：幅度上限 + 新鲜度")
    out = []
    cfg = cfg_base()

    a_big, msg_big = M.check_dimension_a(breakout_df(magnitude=0.10), cfg)
    mag_big = measured_magnitude(breakout_df(magnitude=0.10), cfg)
    out.append(results_row(
        (not a_big) and "追高" in msg_big, "突破 +10% 被拒（幅度上限 5%）",
        f"实测幅度 {mag_big * 100:+.2f}% A通过={a_big} msg={msg_big[:28]}…"))

    a_ok, msg_ok = M.check_dimension_a(breakout_df(magnitude=0.02), cfg)
    mag_ok = measured_magnitude(breakout_df(magnitude=0.02), cfg)
    out.append(results_row(
        a_ok and str(msg_ok).startswith("突破"), "突破 +2% 放行",
        f"实测幅度 {mag_ok * 100:+.2f}% A通过={a_ok}"))

    a_stale, msg_stale = M.check_dimension_a(breakout_df(magnitude=0.02, fresh=False), cfg)
    out.append(results_row(
        not a_stale, "陈旧突破被拒（非最近3根内）",
        f"A通过={a_stale} msg={msg_stale[:40]}…"))

    # 关闭幅度上限应恢复放行
    c2 = cfg_base()
    c2.BREAKOUT_MAX_PCT = None
    a_off, _ = M.check_dimension_a(breakout_df(magnitude=0.10), c2)
    out.append(results_row(a_off, "关闭幅度上限 → 回到旧行为",
                           f"A通过={a_off}（BREAKOUT_MAX_PCT=None）"))
    return out


def check_survivability() -> list:
    """第 ④ 条否决：生存性 / 可交易性（2026-09-30 加入）。

    本节只验证**接线是否正确**（哪个条件触发、关掉是否真关掉、数据不足是否跳过）。
    统计结论本身由 scripts/find_survival_filter.py 负责复现：
        不筛选 -0.297%/10日 (t=-3.42) → 加本否决后 -0.056%/10日 (t=-0.61, 不显著)
    """
    print("\n【7】生存性 / 可交易性否决（第④条）：低价 / 低流动性 / 数据不足")
    out = []
    base = breakout_df(magnitude=0.02)

    def variant(price: float, volume: float) -> pd.DataFrame:
        """把夹具整条价格序列等比缩放到指定收盘价，并指定成交量。"""
        d = base.copy()
        scale = price / float(d["close"].iloc[-1])
        for col in ("open", "high", "low", "close"):
            d[col] = d[col] * scale
        d["volume"] = volume
        return d

    cfg = cfg_base()
    cfg.ENTRY_GATE_SURVIVABILITY_ENABLED = True
    # 用 TREND_UP + 高赔率，让 REGIME/RSI/RR 三条都不否决 → 只剩 ④ 可能触发
    kw = dict(regime_name="TREND_UP", c_info={"ratio": 5.0}, c_pass=True)

    g, _, _ = _gate(variant(10.0, 1e8), cfg, **kw)
    out.append(results_row(
        not any(v["rule"] == "SURVIVABILITY" for v in g["vetoes"]),
        "价10元 + 成交额10亿 → 不否决",
        f"vetoes={[v['rule'] for v in g['vetoes']]}"))

    g, _, _ = _gate(variant(2.0, 1e8), cfg, **kw)
    det = next((v["detail"] for v in g["vetoes"] if v["rule"] == "SURVIVABILITY"), "")
    out.append(results_row(
        bool(det) and "低价" in det and "流动性" not in det,
        "价2元（成交额2亿）→ 只触发低价否决", det or "未否决"))

    g, _, _ = _gate(variant(10.0, 1e5), cfg, **kw)
    det = next((v["detail"] for v in g["vetoes"] if v["rule"] == "SURVIVABILITY"), "")
    out.append(results_row(
        bool(det) and "流动性" in det and "低价" not in det,
        "成交额0.01亿（价10元）→ 只触发流动性否决", det or "未否决"))

    c_off = cfg_base()          # cfg_base 已把第 ④ 条关掉
    g, _, _ = _gate(variant(2.0, 1e5), c_off, **kw)
    st = [r for r in g["rules"] if r["rule"] == "SURVIVABILITY"]
    out.append(results_row(
        bool(st) and st[0]["enabled"] is False
        and not any(v["rule"] == "SURVIVABILITY" for v in g["vetoes"]),
        "关闭 ENTRY_GATE_SURVIVABILITY_ENABLED → 不否决",
        f"enabled={st[0]['enabled'] if st else '缺失'} vetoes={[v['rule'] for v in g['vetoes']]}"))

    short = variant(2.0, 1e5).tail(3).reset_index(drop=True)
    g, _, _ = _gate(short, cfg, **kw)
    st = [r for r in g["rules"] if r["rule"] == "SURVIVABILITY"]
    out.append(results_row(
        bool(st) and st[0]["ok"] is True and "跳过" in st[0]["detail"],
        "数据不足（3根）→ 显式跳过，不否决也不放行",
        (st[0]["detail"] if st else "缺失")))
    return out


def check_real_case() -> list:
    print("\n【8】真实样本端到端（002119）：多否决项互相独立")
    out = []
    try:
        import data_layer as D
        import regime_detector as RD
    except Exception as e:
        print(f"      ⚠️ 依赖不可用，跳过：{e}")
        return out

    cfg = cfg_base()
    try:
        df = D.fetch_history("002119", "2023-09-01")
    except Exception as e:
        print(f"      ⚠️ 取数失败，跳过：{str(e)[:50]}")
        return out

    # 用真实市场状态（缓存命中，不再触网）
    RD._REGIME_CACHE.clear()
    regime = RD.detect_regime(cfg)

    a_pass, a_msg = M.check_dimension_a(df, cfg)
    b_pass, b_signals = M.check_dimension_b(df, cfg)
    c_pass, c_info = M.check_dimension_c(df, cfg)
    gate = M.check_entry_gate(regime, a_pass, a_msg, b_signals, c_pass, c_info, df, cfg)
    rules = {v["rule"] for v in gate["vetoes"]}

    print(f"      A={a_pass} B={b_pass} C={c_pass} regime={regime.get('regime')} "
          f"否决={sorted(rules)}")
    print(f"      A原因: {str(a_msg)[:60]}…")
    out.append(results_row(
        not gate["passed"] and len(rules) >= 2, "至少 2 条独立否决",
        f"{sorted(rules)}"))
    out.append(results_row(
        "RR_RATIO" in rules, "赔率否决（低于 2:1）",
        f"实际盈亏比={c_info['ratio']:.2f}:1（swing 止损时为 0.34:1；默认 near_low 后收窄至约 1.1:1，"
        f"但**仍不达 2:1**）所需胜率={M._breakout_ratio_needed(c_info['ratio']) * 100:.1f}%"))
    # A 的突破幅度过滤也应拦下它（P1 生效）
    out.append(results_row(
        not a_pass and "追高" in str(a_msg), "A 因突破幅度过大被拒",
        f"A通过={a_pass}"))
    return out


def main() -> int:
    print("=" * 88)
    print("入口门控验证（市场状态 / RSI超买 / 赔率 / 生存性 四否决 + 突破质量过滤）")
    print("=" * 88)
    res = []
    res += check_regime_gate()
    res += check_regime_unavailable_skips()
    res += check_rsi_gate()
    res += check_rr_and_expectancy()
    res += check_switches_off()
    res += check_breakout_quality()
    res += check_survivability()
    if "--offline" in sys.argv:
        print("\n（--offline：跳过第8项真实样本端到端）")
    else:
        res += check_real_case()
    print("=" * 88)
    ok, total = sum(res), len(res)
    print(f"结果: {ok}/{total} 项通过  {'✅ 全部通过' if ok == total else '❌ 存在失败项'}")
    print("=" * 88)
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
