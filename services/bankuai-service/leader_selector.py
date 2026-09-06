"""模块 B：龙头梯队识别。

对单个板块，调用 ``market_plate_stocks`` 获取成分股人气排行，按规则分为
一/二/三级龙头梯队。

三级梯队规则：
    一级龙头（领涨龙）：综合评分排序取前 tier_size
        - 硬过滤：change > 板块平均涨幅；不足 tier_min 时放宽为 change > 0
        - 综合评分（加权求和，各分项 min-max 归一化到 0-100）：
            1) 涨幅相对强度（change - avg_change）          权重 w_change
            2) 量比异动（vol_ratio）                        权重 w_vol_ratio
            3) 换手异动（turnover_ratio）                   权重 w_turnover
            4) 封板强度（涨停时间越早越高；无涨停数据降级用相对强度） 权重 w_seal
            5) 板块内首个涨停：额外加分 first_limit_bonus
    二级龙头（中军）：  流动性 + 稳定性，三重过滤后按 est_turnover 降序取前 tier_size
        - 过滤1：涨幅在 0 ~ 板块均幅之间（0 < change <= avg_change）—— 跟涨但不领涨
        - 过滤2：流通市值中等以上（circ_value >= 全板块中位数）—— 流动性支撑
        - 过滤3：换手率适中（Q1 <= turnover_rate <= Q3）—— 排除极端高/低换手
        - 降级：不足 tier_min 时逐级放宽 → 去换手率限制 → 去市值限制 → 放宽涨幅
    三级龙头（补涨）：  涨幅落后 + 量能启动，三重过滤后按 vol_ratio 降序取前 tier_size
        - 过滤1：涨幅 < 板块均幅（change < avg_change）—— 涨幅落后，有补涨空间
        - 过滤2：换手率 > 板块平均换手（turnover_rate > avg_turnover）—— 资金开始关注
        - 过滤3：量比 > 1（vol_ratio > 1）—— 量能启动，成交放大
        - 降级：不足 tier_min 时逐级放宽 → 去量比限制 → 去换手率限制 → 放宽涨幅
    不足补足：         任一梯队 < tier_min 时，从"未被选入的剩余池"按对应排序补足

字段映射（原始 → 输出）：
    stock_code      → code
    stock_name      → name
    rank            → rank
    px_change_rate  → change (涨跌幅%)
    turnover_ratio  → turnover_rate (换手率%)
    vol_ratio       → vol_ratio (量比)
    circulation_value → circ_value (流通市值,元)
    est_turnover    → 估算成交额(元) = circ_value * turnover_rate / 100（用于二级排序）

封板时间降级：
    涨停股池（uplimit_stocks）由 scanner 层一次性拉取并构建为 {code: info} 索引传入。
    若索引为空（接口失败/未启用），封板强度分自动降级为相对强度归一化，
    保证一级龙头判定在任何数据可用性下都能产出结果。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from config import ScanConfig
from data_loader import fetch_plate_stocks

logger = logging.getLogger(__name__)


# ====================================================================
# 工具函数
# ====================================================================

def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _normalize_stock(raw: dict) -> dict:
    """把原始成分股 dict 标准化为输出结构，并计算 est_turnover。"""
    circ = _to_float(raw.get("circulation_value"))
    turnover_rate = _to_float(raw.get("turnover_ratio"))
    est_turnover = circ * turnover_rate / 100.0  # 估算成交额(元)
    return {
        "code": str(raw.get("stock_code", "")),
        "name": raw.get("stock_name", ""),
        "rank": int(_to_float(raw.get("rank"), default=0)),
        "change": _to_float(raw.get("px_change_rate")),
        "turnover_rate": turnover_rate,
        "vol_ratio": _to_float(raw.get("vol_ratio")),
        "circ_value": circ,
        "est_turnover": est_turnover,
    }


def _pick(stocks: list[dict], key: str, reverse: bool,
          size: int, predicate) -> list[dict]:
    """从 stocks 中按 key 排序后取满足 predicate 的前 size 个（不修改原列表）。"""
    ordered = sorted(stocks, key=lambda s: s.get(key, 0), reverse=reverse)
    picked: list[dict] = []
    for s in ordered:
        if len(picked) >= size:
            break
        if predicate(s):
            picked.append(s)
    return picked


def _min_max(values: list[float]) -> list[float]:
    """min-max 归一化到 0-100；全相等或空时返回全 0。"""
    if not values:
        return []
    lo = min(values)
    hi = max(values)
    if hi <= lo:
        return [0.0] * len(values)
    return [(v - lo) / (hi - lo) * 100.0 for v in values]


def _percentile(values: list[float], p: float) -> float:
    """线性插值法计算百分位数。p in [0, 100]。

    用于二级龙头的"流通市值中等以上"(p=50) 和"换手率适中"(p=25/75)。
    空列表返回 0.0；单元素返回该元素。
    """
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


# ====================================================================
# 涨停股池索引构建 + 封板时间评分
# ====================================================================

# 涨停股池字段名容错（不同接口版本字段名可能不同）
_UPLIMIT_CODE_KEYS = ("stock_code", "code", "ts_code", "symbol", "stock_id")
_UPLIMIT_NAME_KEYS = ("stock_name", "name", "stk_name", "简称", "名称")
_UPLIMIT_SEAL_KEYS = (
    "seal_time", "first_limit_time", "limit_up_time", "zt_time",
    "fb_time", "first_seal_time", "seal", "time",
    "up_limit_time",  # zzshare 字段
)
# 连板数：limit_times 是用户需求指定字段，优先级放第一
_UPLIMIT_CONT_KEYS = (
    "limit_times", "continuous", "limit_count", "board_count", "lian_ban",
    "continuous_days", "continue_count", "ct", "连板天数", "连板",
    "up_limit_keep_times",  # zzshare 字段（字符串如 "2"）
)
_UPLIMIT_REASON_KEYS = (
    "reason", "limit_reason", "zt_reason", "reason_for", "cause",
    "涨停原因", "概念", "题材", "logic",
    "up_limit_desc",  # zzshare 字段（如 "2连板"）
)
_UPLIMIT_CHG_KEYS = (
    "change_pct", "quote_rate", "pct_chg", "px_change_rate", "chg",
    "涨跌幅", "涨幅", "change",
)


def _extract_code(rec: dict) -> str:
    for k in _UPLIMIT_CODE_KEYS:
        v = rec.get(k)
        if v:
            return str(v).split(".")[0]  # 兼容 000001.SZ 形式
    return ""


def _extract_seal_time(rec: dict) -> Optional[str]:
    for k in _UPLIMIT_SEAL_KEYS:
        v = rec.get(k)
        if v:
            return str(v)
    return None


def _extract_continuous(rec: dict) -> int:
    for k in _UPLIMIT_CONT_KEYS:
        v = rec.get(k)
        if v:
            return int(_to_float(v, default=1))
    return 1


def _extract_name(rec: dict) -> str:
    """从涨停记录提取股票名称，缺省返回 ""。"""
    for k in _UPLIMIT_NAME_KEYS:
        v = rec.get(k)
        if v:
            return str(v).strip()
    return ""


def _extract_reason(rec: dict) -> str:
    """从涨停记录提取涨停原因，缺省返回 ""。"""
    for k in _UPLIMIT_REASON_KEYS:
        v = rec.get(k)
        if v:
            return str(v).strip()
    return ""


def _extract_change_pct(rec: dict) -> float:
    """从涨停记录提取涨跌幅%，缺省返回 10.0（涨停默认）。"""
    for k in _UPLIMIT_CHG_KEYS:
        v = rec.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 10.0


def build_uplimit_index(uplimit_list: Optional[list]) -> dict:
    """把涨停股池 list 规整为 {code: {seal_time, continuous}} 索引。

    Args:
        uplimit_list: fetch_uplimit_stocks 返回的原始 list[dict]

    Returns:
        dict {code: {"seal_time": str|None, "continuous": int}}
        入参为空或解析失败时返回 {}（调用方据此走降级评分）
    """
    if not uplimit_list or not isinstance(uplimit_list, list):
        return {}
    index: dict = {}
    for rec in uplimit_list:
        if not isinstance(rec, dict):
            continue
        code = _extract_code(rec)
        if not code:
            continue
        index[code] = {
            "seal_time": _extract_seal_time(rec),
            "continuous": _extract_continuous(rec),
        }
    return index


def _seal_time_to_minutes(seal_time: Optional[str]) -> Optional[float]:
    """把涨停时间字符串转为距开盘(9:30)的分钟数。

    支持格式：'09:35:00' / '09:35' / '2026-08-12 09:35:00' / '093500'
    解析失败返回 None。
    """
    if not seal_time:
        return None
    s = str(seal_time).strip()
    # 取时间部分（去掉日期前缀）
    if " " in s:
        s = s.split(" ", 1)[1]
    # 去 . 后的毫秒
    s = s.split(".")[0]
    # 纯数字 093500
    if s.isdigit() and len(s) >= 4:
        if len(s) == 6:
            hh, mm, _ = int(s[0:2]), int(s[2:4]), int(s[4:6])
        elif len(s) == 4:
            hh, mm = int(s[0:2]), int(s[2:4])
        else:
            return None
    elif ":" in s:
        parts = s.split(":")
        try:
            hh = int(parts[0])
            mm = int(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            return None
    else:
        return None
    # 距 9:30 的分钟数（开盘=0，收盘 15:00=330）
    return (hh * 60 + mm) - (9 * 60 + 30)


def _seal_score(seal_minutes: Optional[float]) -> float:
    """涨停时间 → 0-100 分（9:30=100, 15:00=0；未涨停/解析失败=0）。"""
    if seal_minutes is None:
        return 0.0
    # 交易时段 9:30-11:30(120min) + 13:00-15:00(120min) = 240min
    # 午休时段(11:30-13:00) 视为 120min 处理（封板在午休前）
    if seal_minutes < 0:
        return 100.0  # 早于 9:30（集合竞价封板）
    if seal_minutes > 330:
        return 0.0
    # 线性映射 330→0, 0→100
    return max(0.0, min(100.0, (330.0 - seal_minutes) / 330.0 * 100.0))


# ====================================================================
# 一级龙头综合评分
# ====================================================================

def compute_tier1_scores(
    stocks: list[dict],
    avg_change: float,
    uplimit_index: Optional[dict],
    cfg: ScanConfig,
) -> list[float]:
    """为成分股计算一级龙头综合评分（不修改入参）。

    Args:
        stocks: 已标准化的成分股列表
        avg_change: 板块平均涨跌幅
        uplimit_index: build_uplimit_index 的结果；None/{} 触发降级
        cfg: 扫描配置（取权重）

    Returns:
        list[float] 与 stocks 等长的得分列表（用于排序）
    """
    if not stocks:
        return []

    use_uplimit = bool(uplimit_index)

    # 1) 涨幅相对强度
    rel_changes = [s["change"] - avg_change for s in stocks]
    change_scores = _min_max(rel_changes)

    # 2) 量比异动
    vol_ratios = [s["vol_ratio"] for s in stocks]
    vol_scores = _min_max(vol_ratios)

    # 3) 换手异动
    turnovers = [s["turnover_rate"] for s in stocks]
    turnover_scores = _min_max(turnovers)

    # 4) 封板强度
    if use_uplimit:
        seal_scores = []
        for s in stocks:
            info = uplimit_index.get(s["code"])
            if info:
                mins = _seal_time_to_minutes(info.get("seal_time"))
                seal_scores.append(_seal_score(mins))
            else:
                seal_scores.append(0.0)  # 未涨停
    else:
        # 降级：用相对强度归一化作为封板时间代理
        seal_scores = _min_max(rel_changes)

    # 5) 板块内首个涨停加分
    first_limit_codes: set = set()
    if use_uplimit:
        # 找出板块内涨停的股票，按封板时间升序，取首个
        sealed = []
        for s in stocks:
            info = uplimit_index.get(s["code"])
            if info and info.get("seal_time"):
                mins = _seal_time_to_minutes(info.get("seal_time"))
                if mins is not None:
                    sealed.append((s["code"], mins))
        if sealed:
            sealed.sort(key=lambda x: x[1])
            first_limit_codes.add(sealed[0][0])

    scores = []
    for i, s in enumerate(stocks):
        total = (
            cfg.w_change * change_scores[i]
            + cfg.w_vol_ratio * vol_scores[i]
            + cfg.w_turnover * turnover_scores[i]
            + cfg.w_seal * seal_scores[i]
        )
        if s["code"] in first_limit_codes:
            total += cfg.first_limit_bonus
        scores.append(round(total, 4))
    return scores


# ====================================================================
# 三级梯队核心逻辑（纯函数，便于单测）
# ====================================================================

def select_tiers(
    stocks: list[dict],
    cfg: ScanConfig,
    uplimit_index: Optional[dict] = None,
) -> dict:
    """对已标准化的成分股列表执行三级梯队筛选（纯函数，不访问网络）。

    Args:
        stocks: list[dict] 已标准化的成分股（含 change/turnover_rate/est_turnover/rank/vol_ratio）
        cfg:    扫描配置（用 tier_size / tier_min / 各权重）
        uplimit_index: 涨停股池索引；None/{} 时封板强度降级为相对强度

    Returns:
        dict: {sector_name?, avg_change, tier1, tier2, tier3, unselected}
              各 tier 为 list[dict]，每个 dict 含 code/name/change/turnover_rate/rank/est_turnover
              unselected 为未入选任何梯队的标的 list[dict]（含 code/name/change/turnover_rate/vol_ratio）
    """
    size = cfg.tier_size
    mn = cfg.tier_min

    if not stocks:
        return {"avg_change": 0.0, "tier1": [], "tier2": [], "tier3": [], "unselected": []}

    avg_change = sum(s["change"] for s in stocks) / len(stocks)

    # ---- 一级：硬过滤（change>avg；不足放宽 change>0）+ 综合评分排序 ----
    candidates = [s for s in stocks if s["change"] > avg_change]
    if len(candidates) < mn:
        candidates = [s for s in stocks if s["change"] > 0]
    if len(candidates) < mn:
        # 极端情况：全板块几乎都跌，放宽到全部
        candidates = list(stocks)

    scores = compute_tier1_scores(candidates, avg_change, uplimit_index, cfg)
    # 按综合评分降序取前 size；评分并列时按 rank 升序（rank 越小越靠前）
    ranked = sorted(
        zip(candidates, scores),
        key=lambda x: (-x[1], x[0].get("rank", 0)),
    )
    tier1 = [s for s, _ in ranked[:size]]
    # 把评分挂到结果上便于展示
    tier1 = [{**s, "score": sc} for s, sc in ranked[:size]]

    t1_codes = {s["code"] for s in tier1}
    remaining_after_t1 = [s for s in stocks if s["code"] not in t1_codes]

    # ---- 二级（中军）：流动性 + 稳定性 ----
    # 三重过滤：
    #   1) 涨幅在 0 ~ 板块均幅之间（0 < change <= avg_change）—— 跟涨但不领涨
    #   2) 流通市值中等以上（circ_value >= 全板块中位数）—— 流动性支撑
    #   3) 换手率适中（Q1 <= turnover_rate <= Q3）—— 排除极端高/低换手
    # 排序：est_turnover 降序（流动性优先）
    # 降级：不足 tier_min 时逐级放宽 → 去换手率限制 → 去市值限制 → 放宽涨幅
    median_circ = _percentile([s["circ_value"] for s in stocks], 50)
    q1_turnover = _percentile([s["turnover_rate"] for s in stocks], 25)
    q3_turnover = _percentile([s["turnover_rate"] for s in stocks], 75)

    def _tier2_filter(require_change_band: bool = True,
                      require_circ: bool = True,
                      require_turnover_band: bool = True):
        out = []
        for s in remaining_after_t1:
            if require_change_band and not (0 < s["change"] <= avg_change):
                continue
            if require_circ and s["circ_value"] < median_circ:
                continue
            if require_turnover_band and not (q1_turnover <= s["turnover_rate"] <= q3_turnover):
                continue
            out.append(s)
        return out

    # 三重过滤
    t2_cands = _tier2_filter(True, True, True)
    # 降级1：放宽换手率适中（去 Q1~Q3 限制）
    if len(t2_cands) < mn:
        t2_cands = _tier2_filter(True, True, False)
    # 降级2：放宽流通市值（去中位数限制）
    if len(t2_cands) < mn:
        t2_cands = _tier2_filter(True, False, False)
    # 降级3：放宽涨幅上限（只要 change > 0）
    if len(t2_cands) < mn:
        t2_cands = [s for s in remaining_after_t1 if s["change"] > 0]
    # 降级4：全部
    if len(t2_cands) < mn:
        t2_cands = list(remaining_after_t1)

    tier2 = sorted(t2_cands, key=lambda s: s.get("est_turnover", 0), reverse=True)[:size]
    t2_codes = {s["code"] for s in tier2}

    remaining_after_t2 = [s for s in remaining_after_t1 if s["code"] not in t2_codes]

    # ---- 三级（补涨）：涨幅落后 + 量能启动 ----
    # 三重过滤：
    #   1) 涨幅 < 板块均幅（change < avg_change）—— 涨幅落后，有补涨空间
    #   2) 换手率 > 板块平均换手（turnover_rate > avg_turnover）—— 资金开始关注
    #   3) 量比 > 1（vol_ratio > 1）—— 量能启动，成交放大
    # 排序：vol_ratio 降序（量能启动强度优先），并列时按 turnover_rate 降序
    # 降级：不足 tier_min 时逐级放宽 → 去量比限制 → 去换手率限制 → 放宽涨幅
    avg_turnover = sum(s["turnover_rate"] for s in stocks) / len(stocks) if stocks else 0.0

    def _tier3_filter(require_change_lt_avg: bool = True,
                      require_turnover_gt_avg: bool = True,
                      require_vol_gt_1: bool = True):
        out = []
        for s in remaining_after_t2:
            if require_change_lt_avg and not (s["change"] < avg_change):
                continue
            if require_turnover_gt_avg and not (s["turnover_rate"] > avg_turnover):
                continue
            if require_vol_gt_1 and not (s["vol_ratio"] > 1.0):
                continue
            out.append(s)
        return out

    # 三重过滤
    t3_cands = _tier3_filter(True, True, True)
    # 降级1：去量比 > 1 限制
    if len(t3_cands) < mn:
        t3_cands = _tier3_filter(True, True, False)
    # 降级2：去换手率 > 均值限制
    if len(t3_cands) < mn:
        t3_cands = _tier3_filter(True, False, False)
    # 降级3：放宽涨幅限制（全部剩余）
    if len(t3_cands) < mn:
        t3_cands = list(remaining_after_t2)

    tier3 = sorted(t3_cands,
                   key=lambda s: (s.get("vol_ratio", 0), s.get("turnover_rate", 0)),
                   reverse=True)[:size]

    # ---- 不足补足：从"未被选入的剩余池"按对应排序补足到 tier_min ----
    selected_codes = t1_codes | t2_codes | {s["code"] for s in tier3}
    pool = [s for s in stocks if s["code"] not in selected_codes]

    def _backfill(tier: list[dict], key: str, reverse: bool) -> None:
        if len(tier) >= mn or not pool:
            return
        ordered = sorted(pool, key=lambda s: s.get(key, 0), reverse=reverse)
        for s in ordered:
            if len(tier) >= mn:
                break
            tier.append(s)
            pool.remove(s)

    _backfill(tier1, "rank", reverse=False)
    _backfill(tier2, "est_turnover", reverse=True)
    _backfill(tier3, "vol_ratio", reverse=True)

    # ---- 未入选标的（不在任何梯队中）----
    all_selected = (
        {s["code"] for s in tier1}
        | {s["code"] for s in tier2}
        | {s["code"] for s in tier3}
    )
    unselected = [
        {"code": s["code"], "name": s["name"], "change": s["change"],
         "turnover_rate": s["turnover_rate"], "vol_ratio": s["vol_ratio"]}
        for s in stocks if s["code"] not in all_selected
    ]

    return {
        "avg_change": round(avg_change, 4),
        "tier1": tier1,
        "tier2": tier2,
        "tier3": tier3,
        "unselected": unselected,
    }


# ====================================================================
# 对外接口（含数据获取）
# ====================================================================

def identify_leaders(plate_code: str, plate_name: str, date1: str,
                     cfg: ScanConfig,
                     uplimit_index: Optional[dict] = None) -> Optional[dict]:
    """获取板块成分股并识别三级龙头梯队。

    Args:
        plate_code: 板块代码
        plate_name: 板块名称（用于展示）
        date1:      查询日期 YYYY-MM-DD
        cfg:        扫描配置
        uplimit_index: 涨停股池索引（由 scanner 层一次性构建后传入）；
                       None 时封板强度降级为相对强度

    Returns:
        dict:
            sector_name: str
            avg_change:  float 板块平均涨跌幅
            tier1/tier2/tier3: list[dict]
        接口与缓存均无数据时返回 None
    """
    raw_list = fetch_plate_stocks(plate_code, date1, cfg)
    if not raw_list:
        logger.warning("板块成分股为空，跳过: %s(%s)", plate_name, plate_code)
        return None

    stocks = [_normalize_stock(r) for r in raw_list]

    # 成分股少于 9 只：仍按规则分配，尽量保证每队至少 1 只
    if len(stocks) < 9:
        logger.info("板块 %s 成分股仅 %d 只(<9)，按实际数量分配",
                    plate_name, len(stocks))

    result = select_tiers(stocks, cfg, uplimit_index=uplimit_index)
    result["sector_name"] = plate_name
    result["plate_code"] = plate_code

    logger.info(
        "梯队识别完成 %s(%s): 成分=%d, 均幅=%.2f%%, T1=%d T2=%d T3=%d, 涨停池=%s",
        plate_name, plate_code, len(stocks), result["avg_change"],
        len(result["tier1"]), len(result["tier2"]), len(result["tier3"]),
        "有" if uplimit_index else "无(降级)",
    )
    return result
