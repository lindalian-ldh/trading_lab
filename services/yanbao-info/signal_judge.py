"""yanbao-info 短线信号判断逻辑层。

对齐用户"短线信号判断"表，5 个独立判断函数 + 一个 judge_signals 聚合入口:

    - judge_target_price_upside      目标价隐含涨幅（强/中/弱/无法判断）
    - judge_rating_jump              评级跳升（同机构历史对比）
    - judge_forecast_revision        盈利预测上调（与一致预期对比）
    - judge_catalyst_timeliness      催化剂时效性（是否近期落地）
    - judge_first_coverage_signal    首次覆盖（机构关注度提升信号）

所有判断函数返回 dict，含 reason 字段说明判断依据，便于排查。
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Optional

try:
    from config import YanbaoConfig
except ImportError:
    from .config import YanbaoConfig

logger = logging.getLogger(__name__)


# ====================================================================
# 1. 目标价隐含涨幅
# ====================================================================


def judge_target_price_upside(
    target_price: Optional[float],
    current_price: Optional[float],
    cfg: YanbaoConfig,
) -> dict:
    """(目标价 - 当前股价) / 当前股价，按阈值分级信号强度。

    Args:
        target_price: 提取到的目标价（None 表示未提取到）
        current_price: 当前股价（None 表示获取失败）

    Returns:
        {"upside_pct": float|None, "signal": str, "reason": str}
        signal ∈ {"强", "中", "弱", "无法判断"}
    """
    if target_price is None or current_price is None or current_price <= 0:
        return {
            "upside_pct": None,
            "signal": "无法判断",
            "reason": "目标价或当前股价缺失" if (target_price is None or current_price is None) else "当前股价非正数",
        }

    upside = (target_price - current_price) / current_price
    if upside >= cfg.target_price_upside_strong:
        signal = "强"
    elif upside >= cfg.target_price_upside_moderate:
        signal = "中"
    else:
        signal = "弱"

    return {
        "upside_pct": round(upside, 4),
        "signal": signal,
        "reason": f"目标价 {target_price} vs 现价 {current_price}，隐含涨幅 {upside*100:.1f}%",
    }


# ====================================================================
# 2. 评级跳升
# ====================================================================


def judge_rating_jump(
    current_rating: str,
    history: list,
    current_org: str,
    cfg: YanbaoConfig,
) -> dict:
    """若当前评级在 rating_buy 且同机构历史最近一次为中性/持有，视为评级跳升。

    Args:
        current_rating: 当前报告评级（"买入"/"增持"/"中性"/"减持"/"卖出"，空字符串表示未提取）
        history: 历史快照列表（dict: {org_name, rating, publish_date, ...}）
        current_org: 当前报告机构名
        cfg: 配置（评级词表）

    Returns:
        {"is_jump": bool, "from": str, "to": str, "reason": str}
    """
    if not current_rating:
        return {
            "is_jump": False,
            "from": "",
            "to": "",
            "reason": "当前报告评级未提取到",
        }

    if not history:
        return {
            "is_jump": False,
            "from": "",
            "to": current_rating,
            "reason": "无历史对比数据",
        }

    # 查找同机构历史最近一次（按 publish_date 倒序，跳过当日记录）
    same_org_history = [
        h for h in history
        if h.get("org_name") == current_org
        and h.get("rating")  # 必须有评级
    ]
    if not same_org_history:
        return {
            "is_jump": False,
            "from": "",
            "to": current_rating,
            "reason": f"无机构 {current_org} 的历史评级",
        }

    # 取最近一次（history 已倒序，但保险起见再排一次）
    same_org_history.sort(key=lambda x: x.get("publish_date", ""), reverse=True)
    last_rating = same_org_history[0].get("rating", "")

    # 评级强度顺序：买入 > 增持 > 中性 > 减持 > 卖出
    rank_map = {
        "买入": 5, "增持": 4, "中性": 3, "减持": 2, "卖出": 1,
    }
    current_rank = rank_map.get(current_rating, 0)
    last_rank = rank_map.get(last_rating, 0)

    if current_rank > last_rank:
        return {
            "is_jump": True,
            "from": last_rating,
            "to": current_rating,
            "reason": f"机构 {current_org} 评级由 {last_rating} 升至 {current_rating}",
        }
    elif current_rank < last_rank:
        return {
            "is_jump": False,
            "from": last_rating,
            "to": current_rating,
            "reason": f"机构 {current_org} 评级由 {last_rating} 降至 {current_rating}",
        }
    else:
        return {
            "is_jump": False,
            "from": last_rating,
            "to": current_rating,
            "reason": f"机构 {current_org} 维持 {current_rating}",
        }


# ====================================================================
# 3. 盈利预测上调
# ====================================================================


def _parse_money_value(val) -> Optional[float]:
    """解析含单位的金额字符串为纯数字（亿元）。

    支持格式:
        - "1480亿" → 1480.0
        - "16.5万亿" → 165000.0
        - "1480" → 1480.0
        - "16.5%" → None（百分比不是金额）
    """
    if val is None:
        return None
    s = str(val).strip()
    if not s or "%" in s:
        return None
    # 提取数字 + 单位
    m = re.match(r"([\d.]+)\s*([万亿]?)", s)
    if not m:
        return None
    try:
        num = float(m.group(1))
        unit = m.group(2)
        if unit == "万":
            num *= 10000
        elif unit == "亿":
            num *= 1
        elif unit == "万亿":
            num *= 10000
        return num
    except (ValueError, IndexError):
        return None


def judge_forecast_revision(
    latest_forecast: dict,
    consensus: dict,
    cfg: YanbaoConfig,
) -> dict:
    """将最新研报的预测值与一致预期对比，超出阈值视为上调。

    Args:
        latest_forecast: 最新研报的盈利预测 {"营收": {...}, "归母净利润": {...}}
        consensus: 一致预期 {"year": "value_str", ...}（ak.stock_profit_forecast_ths 返回）

    Returns:
        {"is_revision_up": bool, "latest": float|None, "consensus": float|None,
         "year": str, "reason": str}
    """
    if not consensus:
        return {
            "is_revision_up": False,
            "latest": None,
            "consensus": None,
            "year": "",
            "reason": "无法获取一致预期",
        }

    # 取一致预期中最近一年的预测值
    # consensus 形如 {"2024": "1480亿", "2025": "1650亿"}
    sorted_years = sorted(consensus.keys(), reverse=True)
    if not sorted_years:
        return {
            "is_revision_up": False,
            "latest": None,
            "consensus": None,
            "year": "",
            "reason": "一致预期无可用年份",
        }

    consensus_year = sorted_years[0]
    consensus_val = _parse_money_value(consensus.get(consensus_year))
    if consensus_val is None:
        return {
            "is_revision_up": False,
            "latest": None,
            "consensus": None,
            "year": consensus_year,
            "reason": f"一致预期 {consensus_year} 值无法解析: {consensus.get(consensus_year)}",
        }

    # 在最新研报的归母净利润预测中查找对应年份
    # latest_forecast["归母净利润"] 形如 {"2024E": "1480亿", "2025E": "1650亿"}
    net_profit_forecast = latest_forecast.get("归母净利润", {}) if latest_forecast else {}

    # 尝试匹配 "2024E" 或 "2024"
    candidates = [f"{consensus_year}E", consensus_year]
    latest_val = None
    matched_year = ""
    for cand in candidates:
        if cand in net_profit_forecast:
            latest_val = _parse_money_value(net_profit_forecast[cand])
            matched_year = cand
            if latest_val is not None:
                break

    if latest_val is None:
        return {
            "is_revision_up": False,
            "latest": None,
            "consensus": consensus_val,
            "year": consensus_year,
            "reason": f"最新研报无 {consensus_year} 年归母净利润预测",
        }

    if consensus_val <= 0:
        return {
            "is_revision_up": False,
            "latest": latest_val,
            "consensus": consensus_val,
            "year": consensus_year,
            "reason": "一致预期值为 0 或负数，无法计算比率",
        }

    diff_ratio = (latest_val - consensus_val) / consensus_val
    if diff_ratio > cfg.forecast_revision_threshold:
        return {
            "is_revision_up": True,
            "latest": latest_val,
            "consensus": consensus_val,
            "year": matched_year,
            "reason": f"最新研报预测 {latest_val} 高于一致预期 {consensus_val}，超出 {(diff_ratio*100):.1f}%",
        }
    else:
        return {
            "is_revision_up": False,
            "latest": latest_val,
            "consensus": consensus_val,
            "year": matched_year,
            "reason": f"最新研报预测 {latest_val} 与一致预期 {consensus_val} 差异 {(diff_ratio*100):.1f}%（未达上调阈值 {cfg.forecast_revision_threshold*100:.0f}%）",
        }


# ====================================================================
# 4. 催化剂时效性
# ====================================================================


def judge_catalyst_timeliness(catalysts: list, cfg: YanbaoConfig) -> dict:
    """检查催化剂是否含近期时间词或具体日期在窗口期内。

    Args:
        catalysts: 催化剂事件列表

    Returns:
        {"has_recent_catalyst": bool, "items": list, "reason": str}
    """
    if not catalysts:
        return {
            "has_recent_catalyst": False,
            "items": [],
            "reason": "未提取到催化剂事件",
        }

    # 近期时间词
    recent_words = ["即将", "预计", "将于", "近期", "短期内", "马上", "下周", "下月", "本月"]
    # 具体日期模式：9月、三季度、2026年Q3 等
    date_patterns = [
        r"\d{1,2}月",
        r"[一二三四]季度",
        r"Q[1-4]",
        r"\d{4}年",
    ]

    recent_items = []
    for item in catalysts:
        item_str = str(item)
        is_recent = any(w in item_str for w in recent_words)
        if not is_recent:
            for p in date_patterns:
                if re.search(p, item_str):
                    is_recent = True
                    break
        if is_recent:
            recent_items.append(item_str)

    has_recent = len(recent_items) > 0
    reason = (
        f"提取到 {len(recent_items)} 条近期催化剂"
        if has_recent
        else f"共 {len(catalysts)} 条催化剂，但无近期时间标记"
    )

    return {
        "has_recent_catalyst": has_recent,
        "items": recent_items,
        "reason": reason,
    }


# ====================================================================
# 5. 首次覆盖信号
# ====================================================================


def judge_first_coverage_signal(first_coverage: bool) -> dict:
    """首次覆盖视为机构关注度提升的信号。

    Args:
        first_coverage: extractor.detect_first_coverage 的返回值

    Returns:
        {"is_first_coverage": bool, "reason": str}
    """
    if first_coverage:
        return {
            "is_first_coverage": True,
            "reason": "标题或正文含'首次覆盖'字眼，机构关注度提升",
        }
    return {
        "is_first_coverage": False,
        "reason": "非首次覆盖",
    }


# ====================================================================
# 聚合入口
# ====================================================================


def judge_signals(
    extracted: dict,
    current_price: Optional[float],
    consensus: dict,
    history: list,
    current_org: str,
    cfg: YanbaoConfig,
) -> dict:
    """聚合所有信号判断，返回短线信号 dict（对齐用户 JSON 模板的"短线信号"字段）。

    Args:
        extracted: extractor.extract_all 的返回（含 rating/target_price/...）
        current_price: 当前股价
        consensus: 一致预期 {year: value, ...}
        history: 历史快照列表
        current_org: 当前报告机构名
        cfg: 配置

    Returns:
        {
            "首次覆盖": {...},
            "盈利预测上调": {...},
            "目标价空间": {...},
            "评级变化": {...},
            "近期催化剂": {...},
        }
    """
    return {
        "首次覆盖": judge_first_coverage_signal(extracted.get("first_coverage", False)),
        "盈利预测上调": judge_forecast_revision(
            extracted.get("earnings_forecast", {}), consensus, cfg
        ),
        "目标价空间": judge_target_price_upside(
            extracted.get("target_price"), current_price, cfg
        ),
        "评级变化": judge_rating_jump(
            extracted.get("rating", ""), history, current_org, cfg
        ),
        "近期催化剂": judge_catalyst_timeliness(
            extracted.get("catalysts", []), cfg
        ),
    }
