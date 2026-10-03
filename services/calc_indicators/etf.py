#!/usr/bin/env python3
"""ETF 规则模块：代码识别 + 标的指数映射 + ETF 专用参数覆盖。

为什么需要单独一层（而不是在 main.py 里加几个 if）：

    1. **ETF 没有"自身"** —— 它是指数的影子。宽基ETF 与对应指数 60 日相关性 **0.91~0.94**，
       而个股与上证相关性只有 **-0.23~0.50**。对 ETF 而言必须看**对标的那只指数**：
       给创业板ETF 看上证综指 = 看错盘（实测同日 上证→RANGE_MID、创业板指→SQUEEZE）。
       **这是本模块存在的主要理由。**
    2. **映射表必须可对账** —— 映射对不对要能一眼看到、能覆盖，不能藏在逻辑里。

⚠️ 一个被实测推翻的推测（留作教训）：
    曾以为"ETF 波动小，所以 0.5% 回踩阈值对 ETF 太严"，于是放宽到 1.5%。
    实测近 120 日"收盘距 MA5 偏离度"中位数，**ETF 反而小于个股**
    （沪深300ETF 0.81% / 上证50ETF 0.69% vs 茅台 1.00% / 紫金矿业 2.11%），
    0.5% 在宽基ETF 上能覆盖 30~37% 的交易日 —— 已经够用。
    放宽到 1.5% 会把"价格在 MA5 上方 1.5%"这种**偏离/追高**也当回踩买点
    （创业板ETF 有 29.2% 的交易日属于这种），与"回踩买入"定义冲突。
    **故 ETF 模式不碰回踩阈值。**

设计原则：
    - 本模块**只计算"应该用什么参数"**，不修改任何东西；应用与否由 main.py 决定
    - 纯函数，零网络依赖，便于单测（见 verify_etf_mode.py）
    - 所有覆盖都是"相对该档 profile 的增量"，且**只做单向放宽**，绝不收紧用户设定

关键前提：ETF 模式必须在**运行时开始时整体应用**（profile 叠加之后、取数之前），
这样维度B 大盘过滤 与 维度E 市场状态 会一起切到标的指数，保持同一视角。
"""

from __future__ import annotations

import json
import logging
from dataclasses import fields as dc_fields
from pathlib import Path
from typing import Optional

from config import TradingConfig

logger = logging.getLogger(__name__)


# ==================== ETF 代码集合 ====================

# 沪市 ETF/LOF 代码前缀（51x 股票ETF / 56x 新宽基 / 58x 科创板 / 513 跨境 / 518 商品 / 511 债券货币）
# 519xxx 是 LOF 段（场外开放式基金场内份额），交易特性与 ETF 接近，一并纳入识别。
SH_ETF_PREFIXES: tuple = ("510", "511", "512", "513", "515", "516", "517", "518", "519",
                          "520", "521", "522", "523", "524", "525", "526", "527", "528",
                          "530", "551", "560", "561", "562", "563", "588")
# 深市 ETF（159xxx 全部是 ETF/LOF）
SZ_ETF_PREFIXES: tuple = ("159",)


def is_etf(symbol: str) -> bool:
    """判断 6 位代码是否为 ETF/LOF。

    注意：这是**基于代码前缀的启发式判断**，不是权威分类（基金代码段会扩张）。
    因此 main.py 里 ETF 模式默认由 `--auto-etf` / `-o AUTO_ETF_MODE=true` 开启，
    默认 AUTO_ETF_MODE=False 时只有显式 `--etf` 才启用，避免误伤个股。
    """
    code = (symbol or "").strip()
    if len(code) != 6 or not code.isdigit():
        return False
    return code.startswith(SH_ETF_PREFIXES) or code.startswith(SZ_ETF_PREFIXES)


# ==================== 标的指数映射表 ====================

# 结构: etf_code -> (标的指数代码, 指数中文名, 是否适合本工具)
#   - 指数代码用本项目的统一格式 'sh000300' / 'sz399006'（market_filter._normalize_index_code 能解析）
#   - 跨境的纳指/标普/恒生/日经 **baostock 没有对应指数**，故指向A股宽基仅作近似，
#     并在 SUITABLE 里标注为"谨慎"，同时给出 warnings 提示
_ETF_MAP: dict = {
    # ── 宽基（标的指数明确，baostock 均有）──
    "510300": ("sh000300", "沪深300", "ok"),
    "159919": ("sh000300", "沪深300", "ok"),
    "510330": ("sh000300", "沪深300", "ok"),
    "510310": ("sh000300", "沪深300", "ok"),
    "510500": ("sh000905", "中证500", "ok"),
    "159922": ("sh000905", "中证500", "ok"),
    "512500": ("sh000905", "中证500", "ok"),
    "510050": ("sh000016", "上证50", "ok"),
    "512100": ("sh000852", "中证1000", "ok"),
    "159845": ("sh000852", "中证1000", "ok"),
    "512050": ("sh000852", "中证1000", "ok"),
    "510880": ("sh000015", "上证红利", "ok"),
    "515180": ("sh000922", "中证红利", "ok"),
    "159915": ("sz399006", "创业板指", "ok"),
    "159949": ("sz399673", "创业板50", "ok"),
    "588000": ("sh000688", "科创50", "no_index"),   # ⚠️ baostock 无科创50指数（000688 被识别为国城矿业）
    "588080": ("sh000688", "科创50", "no_index"),
    "588090": ("sh000688", "科创50", "no_index"),
    "159781": ("sh000688", "科创50", "no_index"),
    "510180": ("sh000010", "上证180", "ok"),
    "159901": ("sz399001", "深证成指", "ok"),

    # ── 行业/主题（标的指数多为中证/国证行业指数，baostock 覆盖不全 → 回退 A股宽基并提示）──
    "512880": ("sh000001", "证券(中证全指证券公司)", "sector"),
    "512000": ("sh000001", "券商(中证全指证券公司)", "sector"),
    "512800": ("sh000001", "银行", "sector"),
    "512170": ("sh000001", "医疗", "sector"),
    "512010": ("sh000001", "医药", "sector"),
    "512660": ("sh000001", "军工", "sector"),
    "512480": ("sh000001", "半导体", "sector"),
    "512760": ("sh000001", "芯片", "sector"),
    "515030": ("sh000001", "新能源车", "sector"),
    "515790": ("sh000001", "光伏", "sector"),
    "512690": ("sh000001", "酒", "sector"),
    "159928": ("sh000001", "消费", "sector"),
    "512980": ("sh000001", "传媒", "sector"),
    "159869": ("sh000001", "游戏", "sector"),
    "515000": ("sh000001", "科技龙头", "sector"),

    # ── 跨境（baostock 无对应海外指数）──
    "513100": ("sh000001", "纳指100（海外，无A股对标）", "overseas"),
    "513500": ("sh000001", "标普500（海外，无A股对标）", "overseas"),
    "513050": ("sh000001", "中概互联（海外，无A股对标）", "overseas"),
    "513180": ("sh000001", "恒生科技（海外，无A股对标）", "overseas"),
    "513030": ("sh000001", "德国DAX（海外，无A股对标）", "overseas"),

    # ── 商品 ──
    "518880": ("sh000001", "黄金（商品，无股票对标）", "commodity"),
    "518800": ("sh000001", "黄金（商品，无股票对标）", "commodity"),
    "159934": ("sh000001", "黄金（商品，无股票对标）", "commodity"),

    # ── 债券/货币：建议不要用本工具 ──
    "511990": ("sh000001", "货币基金", "bond"),
    "511880": ("sh000001", "货币基金", "bond"),
    "511010": ("sh000001", "国债", "bond"),
    "511260": ("sh000001", "十年国债", "bond"),
    "511380": ("sh000001", "可转债", "bond"),
    "159972": ("sh000001", "5年地方债", "bond"),
}

# 未收录 ETF 的按代码段兜底（比乱猜好，但仍会提示"未收录"）
# 顺序即优先级，最后一项是**任意 ETF 的总兜底** —— 保证 index 永远不为 None，
# 否则上游 BENCHMARK_INDEX 会被写成 None 并静默取数失败。
_PREFIX_FALLBACK: list = [
    (("588",), ("sh000688", "科创50", False)),
    (("159",), ("sz399006", "创业板指(深市ETF未收录兜底)", False)),
    (("51", "56", "52", "53", "55"), ("sh000001", "上证综指(沪市ETF未收录兜底)", False)),    ((), ("sh000001", "上证综指(未收录ETF总兜底)", False)),
]


def match_theme_index(name: str, code: str = "", config=None) -> Optional[tuple]:
    """按 **ETF 名称关键词** 匹配主题锚指数。**纯函数，零网络。**

    为什么按名称而不是按代码：ETF 代码不含主题信息，且全市场 1000+ 只，
    逐只登记不可维护。而名称是法定披露的（如"华夏中证半导体材料设备主题ETF"），
    含主题关键词 —— 新上市的 ETF 自动匹配，**不需要登记**。

    Args:
        name:   ETF 名称（由 fetch_etf_name 取）
        code:   ETF 代码，用于查 ETF_INDEX_OVERRIDE（精确覆盖，优先级最高）
        config: 取 THEME_INDEX_RULES / ETF_INDEX_OVERRIDE 的配置实例；
                为 None 时读 config 模块的类默认值（即"改 config.py 即生效"）

    Returns:
        (指数代码, 指数名, 命中的关键词) 或 None
    """
    # config 为 None 时退回 **TradingConfig 基类**（不是 config 模块！）——
    # THEME_INDEX_RULES / ETF_INDEX_OVERRIDE 是 dataclass 的类属性，模块级取不到。
    # 曾因此全部返回 None，静默退化成旧行为（2026-10-01 踩过）。
    c = config if config is not None else TradingConfig
    if not bool(getattr(c, "ETF_THEME_ANCHOR_ENABLED", True)):
        return None

    # ① 精确代码覆盖（最高优先级）—— 用于关键词不准或想手工指定锚
    ov = getattr(c, "ETF_INDEX_OVERRIDE", None) or {}
    if code and code in ov:
        v = ov[code]
        if isinstance(v, (tuple, list)) and len(v) >= 2:
            return (str(v[0]), str(v[1]), "ETF_INDEX_OVERRIDE")
        if isinstance(v, str):
            return (v, v, "ETF_INDEX_OVERRIDE")

    # ② 名称关键词匹配（按 rules 顺序，先命中先用）
    if not name:
        return None
    nm = str(name)
    _rules = getattr(c, "THEME_INDEX_RULES", ()) or ()
    if not _rules:                                     # 配置丢了 → 明确告警，别静默
        logger.warning("[ETF] THEME_INDEX_RULES 为空，主题锚不可用（检查 config 是否加载正确）")
        return None
    for rule in _rules:
        try:
            kws, idx, idx_name = rule[0], rule[1], rule[2]
        except Exception:
            continue
        for kw in kws:
            if kw and kw in nm:
                # $KC 占位符 → 由 THEME_ANCHOR_KC 决定用哪个指数
                # （见 config 注释里的实测：h=3 偏科创50、h=20 偏创业板指）
                if str(idx) == "$KC":
                    mode = str(getattr(c, "THEME_ANCHOR_KC", "cyb") or "cyb").lower()
                    if mode in ("kc50", "kc", "star", "科创50"):
                        return ("sh000688", "科创50", str(kw))
                    return ("sz399006", "创业板指", str(kw))
                return (str(idx), str(idx_name), str(kw))
    return None


# ETF 名称本地缓存：baostock query_stock_basic 取一次即落盘，避免重复联网。
# 位置 data/cache/etf_names.json，键为 6 位代码。
_NAME_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "cache" / "etf_names.json"
_NAME_CACHE: dict = {}
_NAME_CACHE_LOADED = False


def _load_name_cache() -> dict:
    global _NAME_CACHE, _NAME_CACHE_LOADED
    if not _NAME_CACHE_LOADED:
        try:
            if _NAME_CACHE_PATH.exists():
                _NAME_CACHE = json.loads(_NAME_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            _NAME_CACHE = {}
        _NAME_CACHE_LOADED = True
    return _NAME_CACHE


def fetch_etf_name(symbol: str, use_cache: bool = True) -> str:
    """取 ETF 名称（baostock query_stock_basic + 本地 JSON 缓存）。

    ⚠️ 本函数**有网络依赖**，与 resolve_underlying 的纯函数契约分开：
       etf.py 的解析逻辑保持纯净，联网只发生在这里，且由调用方显式调用。
       任何失败都返回 ""（调用方退回代码段兜底，不会中断主流程）。

    名称用途：主题关键词匹配（match_theme_index）。名称也可用于展示与对账。
    """
    code = (symbol or "").strip()
    if not code:
        return ""
    cache = _load_name_cache()
    if use_cache and code in cache:
        return str(cache[code])

    bs_code = f"sh.{code}" if code.startswith(("6", "5", "9")) else f"sz.{code}"
    name = ""
    try:
        import baostock as bs
        import contextlib as _cl
        import io as _io
        _sink = _io.StringIO()          # baostock 的 login/logout 会 print 到 stdout
        with _cl.redirect_stdout(_sink):
            lg = bs.login()
            if getattr(lg, "error_code", "0") == "0":
                try:
                    rs = bs.query_stock_basic(code=bs_code)
                    while rs.next():
                        row = rs.get_row_data()
                        if row and len(row) > 1 and row[1]:
                            name = str(row[1])
                finally:
                    bs.logout()
    except Exception:
        name = ""
    if name:
        cache[code] = name
        try:
            _NAME_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _NAME_CACHE_PATH.write_text(
                json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
    return name


def resolve_underlying(symbol: str, name: str = "", config=None) -> dict:
    """解析 ETF 的标的指数。

    Args:
        symbol: ETF 代码
        name:   **可选**的 ETF 名称。传入后会按 config.THEME_INDEX_RULES 做主题匹配，
                用于修正 `_ETF_MAP` 里 reason='sector'/'no_index' 的占位锚。
                名称由调用方取（见 fetch_etf_name），以保持本模块**纯函数、零网络**。

    Returns:
        dict: {
            'etf': str,             # 输入代码
            'is_etf': bool,
            'index': str,           # 标的指数代码（未收录时给兜底值）
            'index_name': str,      # 指数中文名
            'mapped': bool,         # 是否命中映射表（False = 用了兜底，需人工确认）
            'suitable': bool,       # 是否适合用本工具做技术分析
            'reason': str,          # 原因码: ok/bond/commodity/overseas/sector/no_index/unknown
            'warnings': list[str],  # 需要人工注意的事项
            'theme_matched': bool,  # 是否由主题规则(名称关键词)定锚
        }
    """
    code = (symbol or "").strip()
    out = {
        "etf": code, "is_etf": is_etf(code), "index": None, "index_name": None,
        "mapped": False, "suitable": True, "reason": "ok", "warnings": [],
        "theme_matched": False,
    }
    if not out["is_etf"]:
        return out

    if code in _ETF_MAP:
        idx, name_, reason = _ETF_MAP[code]
        out.update(index=idx, index_name=name_, mapped=True,
                   suitable=(reason == "ok"), reason=reason)
    else:
        for prefixes, (idx, name_, suitable) in _PREFIX_FALLBACK:
            if not prefixes or code.startswith(prefixes):
                out.update(index=idx, index_name=name_, mapped=False,
                           suitable=suitable, reason="unknown")
                break
        if out["index"] is None:      # 理论上不可达（最后一项是总兜底），保底
            out.update(index="sh000001", index_name="上证综指(未收录ETF总兜底)",
                       mapped=False, reason="unknown")

    # —— 主题定锚：只改"已知不可用"的锚，不动 reason='ok' 的正确映射 ——
    # 'sector'   = 行业指数不在 baostock 覆盖内，原先一律兜底 sh000001（看错盘）
    # 'no_index' = 科创50 在 baostock 不存在，取数必然失败
    # 'unknown'  = 未收录，靠代码段兜底
    # reason='ok' 的（如 510300→沪深300）是已核对过的映射，**不覆盖**。
    if (out["index"] and code not in _ETF_MAP or out["reason"] in ("sector", "no_index")):
        if out["reason"] in ("sector", "no_index", "unknown") or code not in _ETF_MAP:
            hit = match_theme_index(name, code, config)
            if hit:
                idx, idx_name, kw = hit
                out.update(index=idx, index_name=f"{idx_name}(主题锚:{kw})",
                           suitable=True, theme_matched=True)
                # 三种来源都算"已按主题定锚"，都要给出解释性警告。
                # 曾漏掉 unknown（不在 _ETF_MAP 的 ETF，如 562590）——
                # 它们被主题定锚后**完全无提示**，用户不知道锚被换过。
                out["reason"] = {
                    "sector": "sector_anchored",
                    "no_index": "no_index_anchored",
                    "unknown": "theme_anchored",
                }.get(out["reason"], out["reason"])

    # —— 按原因给出**准确**提示（不要把行业ETF/科创50 都说成"债券商品跨境"）——
    reason = out["reason"]

    # 未收录 + 主题也没匹配上 → 必须显式告警。
    # （曾在一轮重构中丢掉这条，被 verify_etf_mode 的"未收录ETF给出显式警告"抓到）
    if not out["theme_matched"] and reason == "unknown":
        out["warnings"].append(
            f"ETF {code} 不在内置映射表中，且名称未命中主题规则，"
            f"已按代码段回退到 {out['index']}（{out['index_name']}）。"
            f"若标的指数不是它，请在 config.THEME_INDEX_RULES 加关键词，"
            f"或用 config.ETF_INDEX_OVERRIDE / `-o BENCHMARK_INDEX=...` 显式指定。")

    if reason == "bond":
        out["warnings"].append(
            f"{code} 是债券/货币类 ETF（价格近乎不动，实测 511990 日涨跌 +0.01%），"
            f"动量/超卖/突破信号都是噪声，建议不要用本工具。")
    elif reason == "commodity":
        out["warnings"].append(
            f"{code} 是商品类 ETF，与 A 股相关性低、无股票对标指数，"
            f"本工具的股票技术分析逻辑参考价值很低。")
    elif reason == "overseas":
        out["warnings"].append(
            f"{code} 是跨境 ETF（受海外市场驱动），baostock 无对应海外指数，"
            f"只能以 A 股指数近似参照，参考价值有限。")
    elif reason == "sector":
        out["warnings"].append(
            f"{code} 是行业/主题 ETF。其标的行业指数（如中证全指证券公司）不在 baostock "
            f"覆盖范围内，维度E/大盘过滤当前以 {out['index']} 近似，"
            f"**无法反映该行业自身的状态** —— 解读时请留意这一偏差。")
        if not name:
            out["warnings"].append(
                "未取得该 ETF 名称，故无法按主题选锚。传 name= 或跑 fetch_etf_name() 可修正；"
                "也可在 config.THEME_INDEX_RULES / ETF_INDEX_OVERRIDE 中指定。")
    elif reason == "no_index":
        out["warnings"].append(
            f"{code} 的标的指数（科创50，000688）在 baostock 中不存在"
            f"（该代码被识别为国城矿业），维度E/大盘过滤会取数失败并输出'暂不可用'；"
            f"建议改用创业板指锚（主题规则会自动处理）或加 --no-regime。")
    elif reason in ("sector_anchored", "no_index_anchored", "theme_anchored"):
        # 按**具体锚**给准确的实测结论 —— 不同锚的验证强度差别很大，
        # 不能统一写成"两段同为正"（科创50 根本没有 2016-2020 数据）。
        _ev = {
            "sz399006": "恐慌日买/持 20 日：2016-2020 +1.60pp、2021- +1.13pp，**两段同为正**",
            "sz399001": "恐慌日买/持 20 日：2016-2020 +0.77pp、2021- +2.66pp，**两段同为正**",
            "sh000001": "恐慌日买/持 20 日：2016-2020 +0.48pp、2021- +4.16pp，**两段同为正**",
            "sh000300": "恐慌日买/持 20 日：2016-2020 +0.44pp、2021- +1.58pp，**两段同为正**",
            "sh000688": ("⚠️ 科创50 基日 2019-12-31，**2020 才有数据，无法做双体制校验**；"
                         "实测 h=20 仅 +0.58pp(p=0.335，不显著)，但 h=3 为 +1.26pp(p=0.040，显著)"
                         "—— **适合短持有期，不适合默认 20 日**"),
        }.get(out["index"], "该锚的择时效应尚未单独实测")
        out["warnings"].append(
            f"{code} 原标的指数不在 baostock 覆盖内，已按**主题**改用 "
            f"{out['index_name']} 作为锚（见 config.THEME_INDEX_RULES）。"
            f"{_ev}。它**不是**该 ETF 的精确标的指数，解读时请知悉这一近似。")
    return out


# ==================== ETF 专用参数覆盖 ====================

def build_etf_overrides() -> dict:
    """ETF 模式相对 profile 的参数增量。

    ⚠️ 这里刻意保持**最小**。第一版曾放宽回踩偏离(0.5%→1.5%)、缩量比(0.8→0.9)、
    宽基RSI超卖(30→38)，基于"ETF 波动小所以 0.5% 太严"的推测；实测数据推翻了它：

        近 120 日 收盘距 MA5 的偏离度中位数 —— ETF 反而**小于**个股
          沪深300ETF 0.81% / 上证50ETF 0.69% / 中证500ETF 1.27% / 创业板ETF 1.81%
          贵州茅台   1.00% / 平安银行   0.92% / 紫金矿业   2.11%

        0.5% 阈值在 ETF 上的实际覆盖率（近120日）
          沪深300ETF 30% / 上证50ETF 37% / 中证500ETF 20% / 创业板ETF 16%
        → 已经能稳定触发，**不是"太严"**。

        若放宽到 1.5%：会把"价格在 MA5 上方 1.5%"这种**偏离/追高**也当作回踩买点
          （沪深300ETF 7.5% 的交易日、创业板ETF 29.2% 的交易日都属于这种"偏上方过远"）
        → 与"回踩买入"的定义直接冲突，是引入劣质信号。故不放宽。

    唯一有实测依据的是放量阈值：ETF 量比 >1.5 仅占 3~7% 的交易日（个股 4~8%），
    因为 ETF 以份额交易、量比中枢低，1.5 在 ETF 上近乎死信号；1.25 恢复到 ~12~24%。
    """
    return {
        # 维度B：ETF 放量门槛下调（实测 >1.5 仅 3~7% 交易日，近乎永不触发）
        "VOLUME_SURGE_RATIO": 1.25,
    }


# 说明：早期版本曾为"宽基ETF 很少极端超卖"而放宽 RSI 超卖线到 38，
# 但 38 已不属于"超卖"，等于重新定义信号而非修正参数；且这是个逆势信号，
# 放宽它只会增加劣质抄底。故不做覆盖 —— 需要的话用户自行 -o RSI_OVERSOLD=38。


def build_config_overrides(symbol: str, config: TradingConfig,
                           wide_base: bool = True, name: str = "") -> dict:
    """计算 ETF 模式下要施加到 config 上的字段字典。

    非 ETF 代码一律返回空字典 —— ETF 模式绝不能影响个股判定。

    Args:
        symbol: ETF 代码
        config: 当前（已叠加 profile 的）配置
        wide_base: 保留参数（当前不影响结果，历史版本曾用于区分宽基/行业）
        name: ETF 名称（可选）。传入后行业主题 ETF 会按 config.THEME_INDEX_RULES
              选到**主题锚指数**，而不是原先占位的 sh000001。
    """
    ov = dict(build_etf_overrides())

    # 基准指数：切到标的指数，使 维度B 大盘过滤 与 维度E 市场状态 都看对标指数
    resolved = resolve_underlying(symbol, name=name, config=config)
    if not resolved["is_etf"]:
        # 非 ETF：ETF 模式对个股不应产生任何影响（防止误用 --etf）
        return {}
    if resolved["index"]:
        ov["BENCHMARK_INDEX"] = resolved["index"]

    # 逐字段 guarding：只在"当前值比 ETF 推荐值更严"时才放宽，
    # 绝不把用户/其他 profile 已经调宽的设定又收紧（ETF 模式只做单向放宽）。
    if config.VOLUME_SURGE_RATIO < ov["VOLUME_SURGE_RATIO"]:
        ov.pop("VOLUME_SURGE_RATIO")

    return ov


def apply_etf_overrides(config: TradingConfig, symbol: str,
                        wide_base: bool = True, name: str = "") -> dict:
    """把 ETF 参数**原地施加**到 config，返回 {"field": (old, new)} 变更记录。

    返回变更记录是为了让输出能明确列出"ETF 模式改了什么" —— 映射与阈值必须可对账。
    """
    overrides = build_config_overrides(symbol, config, wide_base=wide_base, name=name)
    changed: dict = {}
    for key, new_val in overrides.items():
        if not hasattr(config, key):
            logger.info("[ETF] 跳过未知字段 %s", key)
            continue
        old_val = getattr(config, key)
        if old_val == new_val:
            continue
        setattr(config, key, new_val)
        changed[key] = (old_val, new_val)
    return changed


def describe_plan(symbol: str, config: TradingConfig,
                  wide_base: bool = True, name: str = "") -> dict:
    """生成给用户看的 ETF 模式说明（不做任何修改）。

    Returns:
        dict: {'is_etf', 'resolved', 'changed'(预览), 'warnings'}
    """
    resolved = resolve_underlying(symbol, name=name, config=config)
    preview = build_config_overrides(symbol, config, wide_base=wide_base, name=name)
    changed_preview = {}
    for key, new_val in preview.items():
        old_val = getattr(config, key, None)
        if old_val != new_val:
            changed_preview[key] = (old_val, new_val)
    return {
        "is_etf": resolved["is_etf"],
        "resolved": resolved,
        "changed": changed_preview,
        "warnings": list(resolved["warnings"]),
    }


def print_etf_plan(plan: dict, applied: Optional[dict] = None) -> None:
    """打印 ETF 模式说明区块。"""
    r = plan["resolved"]
    print("【ETF 模式】")
    print(f"  🧾 代码: {r['etf']}  |  标的指数: {r['index']}（{r['index_name']}）"
          f"{'' if r['mapped'] else '  ⚠️ 未收录，按代码段兜底'}")
    if applied:
        if applied:
            print(f"  🔧 参数调整（{len(applied)} 项）:")
            for k, (old, new) in applied.items():
                print(f"     {k}: {old!r} → {new!r}")
        else:
            print("  🔧 参数调整: 无（当前配置已不严于 ETF 推荐值）")
    else:
        if plan["changed"]:
            print(f"  🔧 预览调整（{len(plan['changed'])} 项）:")
            for k, (old, new) in plan["changed"].items():
                print(f"     {k}: {old!r} → {new!r}")
        else:
            print("  🔧 预览调整: 无")
    for w in plan["warnings"]:
        print(f"  ⚠️  {w}")
    # 只有"确实切换了"才宣告切换 —— 行业/商品/跨境类的兜底值可能恰好等于原基准，
    # 此时宣告"已切到标的指数"会与上面的警告自相矛盾。
    benchmark_changed = "BENCHMARK_INDEX" in (applied if applied is not None else plan["changed"])
    if benchmark_changed:
        print("  ℹ️  基准指数已切到标的指数：维度B 大盘过滤 与 维度E 市场状态 都会看它，")
        print("      不再用上证综指代替（给创业板ETF 看上证 = 看错盘）。")
    elif r["reason"] in ("sector", "overseas", "commodity"):
        print(f"  ℹ️  基准保持 {r['index']}（该项标的指数不在 baostock 覆盖内，无法切换）——")
        print("      维度E/大盘过滤看到的是大盘状态，**不代表该 ETF 自身标的的状态**。")
    print()


def assert_loaded() -> None:
    """轻量自检：映射表格式合法性（供 main.py 启动时兜底调用，失败不影响主流程）。"""
    valid_market = {"sh", "sz"}
    valid_reason = {"ok", "sector", "bond", "commodity", "overseas", "no_index"}
    for code, (idx, name, reason) in _ETF_MAP.items():
        assert len(code) == 6 and code.isdigit(), f"ETF 代码格式错误: {code}"
        assert idx[:2] in valid_market and idx[2:].isdigit(), f"指数代码格式错误: {code} -> {idx}"
        assert isinstance(name, str) and name, f"指数名称缺失: {code}"
        assert reason in valid_reason, f"原因码非法: {code} -> {reason!r}"
    # 覆盖 TradingConfig 的字段必须真实存在（防止改了字段名却静默失效）
    known = {f.name for f in dc_fields(TradingConfig)}
    for key in build_etf_overrides():
        assert key in known, f"ETF 覆盖了不存在的字段: {key}"
    assert "BENCHMARK_INDEX" in known
    assert "RSI_OVERSOLD" in known
