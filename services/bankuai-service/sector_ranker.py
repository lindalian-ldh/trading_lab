"""模块 A：板块排名筛选。

调用 ``plates_rank`` 获取板块热度排名，输出 Top N（热门）与 Bottom N（冷门），
合并去重后得到待分析板块列表（target_sectors）。

zzshare ``plates_rank`` 返回按 score(热度) 降序的 list，无显式 rank 字段。
本模块以"在返回列表中的位置 +1"作为 rank（与官网展示顺序一致）。

字段映射（原始 → 输出）：
    plate_name → name
    plate_code → code
    rate       → change (涨跌幅%)
    trade_money→ turnover (成交额, 由元转亿元, /1e8)
    score      → score (热度)
    (位置+1)   → rank
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from config import ScanConfig
from data_loader import fetch_plates_rank

logger = logging.getLogger(__name__)


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _normalize_plate(raw: dict, rank: int) -> dict:
    """把原始板块 dict 标准化为输出结构。"""
    trade_money = _to_float(raw.get("trade_money"))  # 元
    return {
        "name": raw.get("plate_name", ""),
        "code": str(raw.get("plate_code", "")),
        "rank": rank,
        "change": _to_float(raw.get("rate")),
        "turnover": round(trade_money / 1e8, 2),   # 元 → 亿
        "score": _to_float(raw.get("score")),
        "time": raw.get("time", ""),
    }


def rank_sectors(date1: str, cfg: ScanConfig,
                 limit: Optional[int] = None) -> Optional[dict]:
    """获取板块排名并筛选热门/冷门板块。

    Args:
        date1: 查询日期 YYYY-MM-DD
        cfg: 扫描配置
        limit: 拉取条数，None 时取 max(50, top_n + bottom_n + 10) 确保尾部可取

    Returns:
        dict:
            hot_sectors:   list[dict] 热门板块（按 rank 升序，取 top_n）
            cold_sectors:  list[dict] 冷门板块（按 rank 降序，取 bottom_n）
            target_sectors: list[dict] 合并去重后的待分析板块（含 code/name，热门在前）
            total:         int 原始返回板块总数
        失败（接口与缓存均无数据）返回 None
    """
    # sectors_filter 模式下拉取全量板块，确保指定板块被覆盖
    if cfg.sectors_filter and not limit:
        fetch_limit = 500
    else:
        fetch_limit = limit or max(50, cfg.top_n + cfg.bottom_n + 10)
    raw_list = fetch_plates_rank(date1, cfg, limit=fetch_limit)
    if not raw_list:
        logger.error("板块排名数据为空 date1=%s", date1)
        return None

    # 标准化并赋予 rank（位置 +1）
    sectors = [_normalize_plate(r, idx + 1) for idx, r in enumerate(raw_list)]

    # ---- sectors_filter：指定板块名称过滤（支持子串模糊匹配）----
    filter_names: list[str] = []
    if cfg.sectors_filter:
        filter_names = [s.strip() for s in cfg.sectors_filter.split(",") if s.strip()]

    if filter_names:
        # 全量保留排名信息，hot/cold 仍按 top_n/bottom_n 截取
        hot = sectors[:cfg.top_n]
        cold = list(reversed(sectors))[:cfg.bottom_n]
        # target 仅保留名称匹配的板块（子串匹配，不区分大小写）
        def _match(name: str) -> bool:
            name_lower = name.lower()
            return any(fn.lower() in name_lower for fn in filter_names)
        matched = [s for s in sectors if _match(s["name"])]
        target = [{"name": s["name"], "code": s["code"]} for s in matched]
        # 检查未找到的板块名
        found_names = {s["name"] for s in matched}
        missing = [fn for fn in filter_names
                   if not any(fn.lower() in n.lower() for n in found_names)]
        if missing:
            logger.warning("以下板块未在排名中找到: %s", ", ".join(missing))
    else:
        # 热门：按 rank 升序（即 score 降序）取前 top_n
        hot = sectors[:cfg.top_n]
        # 冷门：按 rank 降序（即 score 升序）取前 bottom_n
        cold = list(reversed(sectors))[:cfg.bottom_n]
        # 合并去重（以 code 为 key，热门优先）
        seen: set[str] = set()
        target: list[dict] = []
        for s in hot + cold:
            code = s["code"]
            if code and code not in seen:
                seen.add(code)
                target.append({"name": s["name"], "code": code})

    logger.info(
        "板块排名筛选完成: 总数=%d, 热门=%d, 冷门=%d, 去重后待分析=%d",
        len(sectors), len(hot), len(cold), len(target),
    )

    return {
        "hot_sectors": hot,
        "cold_sectors": cold,
        "target_sectors": target,
        "total": len(sectors),
    }
