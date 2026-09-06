"""模块 C：主调度器。

串联模块 A（板块排名筛选）、模块 B（龙头梯队识别）、
模块 C2（涨停连板梯队），完成完整扫描流程，
并汇总为结构化报告。

处理流程：
    1. 调用 sector_ranker 获取 target_sectors（热门 + 冷门去重）
    2. 一次性拉取全市场涨停股池（uplimit_stocks 共享使用）
    3. 对每个目标板块调用 leader_selector 识别三级梯队
    4. C2 涨停连板梯队：uplimit_stocks 按 limit_times 分组输出
    5. 汇总结果 + 生成 summary

异常处理：
    - 单板块 / 新增模块失败：记录失败原因并继续，对应 meta.error 填字符串
    - 整体超时：返回已完成部分的结果并记录警告
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from config import ScanConfig
from data_loader import (
    fetch_uplimit_stocks,
    fetch_uplimit_hot,
)
from leader_selector import (
    build_uplimit_index,
    identify_leaders,
    _extract_code,
    _extract_name,
    _extract_continuous,
    _extract_seal_time,
    _extract_reason,
    _extract_change_pct,
)
from sector_ranker import rank_sectors

logger = logging.getLogger(__name__)


# ====================================================================
# 涨停连板梯队 — 纯函数处理
# ====================================================================

def _seal_time_short(seal_time: Optional[str]) -> str:
    """把完整封板时间压缩为 HH:MM（缺省返回 "--:--"）。"""
    if not seal_time:
        return "--:--"
    s = str(seal_time).strip()
    # 去掉日期前缀
    if " " in s:
        s = s.split(" ", 1)[1]
    s = s.split(".")[0]
    # 提取 HH:MM
    if ":" in s:
        parts = s.split(":")
        if len(parts) >= 2:
            try:
                return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
            except ValueError:
                pass
    # 纯数字 0935 → 09:35
    if s.isdigit() and len(s) >= 4:
        try:
            return f"{s[0:2]}:{s[2:4]}"
        except Exception:
            pass
    return "--:--"


def _ladder_label(limit_times: int) -> str:
    """连板数 → 展示 label：1 → 首板，其他 N → N板。"""
    if limit_times <= 1:
        return "首板"
    return f"{limit_times}板"


def _build_uplimit_ladder(raw_list: Optional[list], cfg: ScanConfig) -> dict:
    """从 uplimit_stocks 原始 list 按 limit_times 分组，生成连板梯队。

    Args:
        raw_list: fetch_uplimit_stocks 返回的原始 list[dict]（涨停股列表）
        cfg: ScanConfig，取 ladder_show_top_n / ladder_min_limit

    Returns:
        dict: {"ladders": [...按连板数倒序分组...],
               "meta": {"uplimit_stocks_count": N, "source": "zzshare_uplimit_stocks", "error": None}}
        raw_list 为空/None → ladders 为 []，uplimit_stocks_count=0
    """
    result: dict = {
        "ladders": [],
        "meta": {
            "uplimit_stocks_count": 0,
            "source": "zzshare_uplimit_stocks",
            "error": None,
        },
    }
    if not raw_list or not isinstance(raw_list, list):
        return result

    # 分组：limit_times(int) → list[个股dict]
    groups: dict[int, list[dict]] = {}
    seen_codes: set[str] = set()  # 按 stock_code 去重（同一股多板块记录只保留首条）
    valid = 0
    for rec in raw_list:
        if not isinstance(rec, dict):
            continue
        code = _extract_code(rec)
        if not code:
            continue
        if code in seen_codes:
            continue
        seen_codes.add(code)
        lt = int(_extract_continuous(rec))
        if lt < 1:
            lt = 1
        if lt < cfg.ladder_min_limit:
            continue
        stock = {
            "code": code,
            "name": _extract_name(rec),
            "change_pct": round(_extract_change_pct(rec), 2),
            "seal_time": _seal_time_short(_extract_seal_time(rec)),
            "reason": _extract_reason(rec),
            "limit_times": lt,
        }
        groups.setdefault(lt, []).append(stock)
        valid += 1

    result["meta"]["uplimit_stocks_count"] = valid
    if not groups:
        return result

    show_top = max(1, int(cfg.ladder_show_top_n))

    # 组内排序：按封板时间字符串升序（早封板在前，"--:--" 放末尾）
    def _seal_sort_key(s: dict) -> tuple:
        st = s.get("seal_time", "--:--")
        if st == "--:--":
            return (1, "99:99")
        return (0, st)

    # 按连板数倒序 → 高连板在前
    ordered_keys = sorted(groups.keys(), reverse=True)
    ladders = []
    for lt in ordered_keys:
        stocks = sorted(groups[lt], key=_seal_sort_key)
        shown = stocks[:show_top]
        has_more = len(stocks) > show_top
        ladders.append({
            "limit_times": lt,
            "label": _ladder_label(lt),
            "stocks": shown,
            "total_count": len(stocks),
            "has_more": has_more,
        })
    result["ladders"] = ladders
    return result


# ====================================================================
# 新增：uplimit_hot 热门板块处理（可选）
# ====================================================================

def _process_uplimit_hot(raw_list: Optional[list]) -> list:
    """把 uplimit_hot 原始 list 标准化为简洁结构（字段容错）。

    返回 list[dict]：{name, uplimit_count, continuous_head, change_pct, ...}
    空入返回 []
    """
    if not raw_list or not isinstance(raw_list, list):
        return []
    result = []
    for r in raw_list:
        if not isinstance(r, dict):
            continue
        name = (r.get("plate_name") or r.get("name") or r.get("板块") or
                r.get("sector_name") or "")
        try:
            count = int(r.get("uplimit_count") or r.get("涨停家数") or r.get("zt_count") or 0)
        except (TypeError, ValueError):
            count = 0
        head = (r.get("continuous_head") or r.get("最高连板") or
                r.get("龙头") or r.get("head_stock") or "")
        try:
            chg = float(r.get("rate") or r.get("涨跌幅") or r.get("change") or 0.0)
        except (TypeError, ValueError):
            chg = 0.0
        if not name:
            continue
        result.append({
            "name": str(name).strip(),
            "uplimit_count": count,
            "continuous_head": str(head).strip(),
            "change_pct": round(chg, 2),
        })
    return result


def scan(date1: str, cfg: ScanConfig) -> dict:
    """执行完整的板块扫描 + 龙头梯队识别。

    Args:
        date1: 查询日期 YYYY-MM-DD
        cfg:   扫描配置

    Returns:
        完整扫描报告 dict:
            scan_time:      str 扫描开始时间
            plate_type:     str 板块类型中文名
            hot_sectors:    list[dict] 热门板块
            cold_sectors:   list[dict] 冷门板块
            sector_leaders: dict {板块名: 梯队详情}
            summary:        dict 统计信息
                          (total_sectors/hot_count/cold_count/success_count/failed_sectors)
        即使板块排名失败也会返回最小报告（success_count=0），不抛异常
    """
    scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    plate_type_name = cfg.plate_type_name()
    logger.info("===== 板块扫描开始 %s [%s] =====", date1, plate_type_name)

    # ---- 模块 A：板块排名 ----
    rank_result = rank_sectors(date1, cfg)
    if rank_result is None:
        logger.error("板块排名获取失败，扫描终止")
        return {
            "scan_time": scan_time,
            "date1": date1,
            "plate_type": plate_type_name,
            "hot_sectors": [],
            "cold_sectors": [],
            "sector_leaders": {},
            "uplimit_ladder": {"ladders": [], "uplimit_hot": [],
                               "meta": {"uplimit_stocks_count": 0, "source": "",
                                        "error": "板块排名失败"}},
            "summary": {
                "total_sectors": 0,
                "hot_count": 0,
                "cold_count": 0,
                "success_count": 0,
                "failed_sectors": [],
                "error": "板块排名数据获取失败",
            },
        }

    hot_sectors = rank_result["hot_sectors"]
    cold_sectors = rank_result["cold_sectors"]
    targets = rank_result["target_sectors"]

    # ---- 一次性拉取全市场涨停股池（全板块共享，用于一级龙头封板时间判定 + 连板梯队）----
    # 失败时返回 None，identify_leaders 据此走降级评分（用相对强度替代封板时间）
    uplimit_index: dict = {}
    uplimit_raw: Optional[list] = None  # 预先声明，供连板梯队共享复用
    if cfg.enable_uplimit_pool or cfg.enable_uplimit_ladder:
        try:
            uplimit_raw = fetch_uplimit_stocks(date1, cfg)
            uplimit_index = build_uplimit_index(uplimit_raw)
            logger.info("涨停股池构建完成: %d 只涨停股（一级龙头封板判定%s）",
                        len(uplimit_index), "启用" if uplimit_index else "降级")
        except Exception as e:
            logger.warning("涨停股池获取失败，一级龙头封板判定降级为相对强度: %s", e)
            uplimit_index = {}
            uplimit_raw = None

    # ---- 模块 B：逐板块识别龙头梯队 ----
    sector_leaders: dict = {}
    failed_sectors: list[str] = []
    success_count = 0

    for t in targets:
        name = t["name"]
        code = t["code"]
        try:
            result = identify_leaders(code, name, date1, cfg,
                                      uplimit_index=uplimit_index or None)
            if result is None:
                failed_sectors.append(name)
                logger.warning("板块 %s(%s) 获取失败，跳过", name, code)
            else:
                sector_leaders[name] = result
                success_count += 1
        except Exception as e:
            failed_sectors.append(name)
            logger.exception("板块 %s(%s) 识别异常: %s", name, code, e)

    summary = {
        "total_sectors": len(targets),
        "hot_count": len(hot_sectors),
        "cold_count": len(cold_sectors),
        "success_count": success_count,
        "failed_sectors": failed_sectors,
        "uplimit_pool_size": len(uplimit_index),
        "uplimit_degraded": len(uplimit_index) == 0 and cfg.enable_uplimit_pool,
    }

    # ---- C2：涨停连板梯队（uplimit_stocks 共享 + 可选 uplimit_hot）----
    uplimit_ladder: dict = {"ladders": [],
                            "uplimit_hot": [],
                            "meta": {"uplimit_stocks_count": 0, "source": "", "error": "未启用"}}
    if cfg.enable_uplimit_ladder:
        try:
            # 连板梯队复用已获取的 uplimit_raw（pool 和 ladder 共享一次拉取）
            ladder_raw = uplimit_raw
            if ladder_raw is None:
                try:
                    ladder_raw = fetch_uplimit_stocks(date1, cfg)
                except Exception as e:
                    logger.warning("连板梯队拉取涨停股池失败: %s", e)
                    ladder_raw = None
            uplimit_ladder = _build_uplimit_ladder(ladder_raw, cfg)
            # 可选 uplimit_hot（需额外请求）
            if cfg.enable_uplimit_hot:
                try:
                    raw_hot = fetch_uplimit_hot(date1, cfg)
                    uplimit_ladder["uplimit_hot"] = _process_uplimit_hot(raw_hot)
                except Exception as e:
                    logger.warning("uplimit_hot 获取失败，跳过: %s", e)
                    uplimit_ladder["uplimit_hot"] = []
            logger.info("涨停连板梯队完成: 分组=%d 涨停股=%d",
                        len(uplimit_ladder["ladders"]),
                        uplimit_ladder["meta"].get("uplimit_stocks_count", 0))
        except Exception as e:
            logger.warning("涨停连板梯队构建失败，跳过: %s", e)
            uplimit_ladder["meta"]["error"] = str(e)

    logger.info(
        "===== 板块扫描结束: 目标=%d 成功=%d 失败=%d 涨停池=%d 连板组=%d =====",
        len(targets), success_count, len(failed_sectors), len(uplimit_index),
        len(uplimit_ladder.get("ladders", [])),
    )

    return {
        "scan_time": scan_time,
        "date1": date1,
        "plate_type": plate_type_name,
        "hot_sectors": hot_sectors,
        "cold_sectors": cold_sectors,
        "sector_leaders": sector_leaders,
        "uplimit_ladder": uplimit_ladder,
        "summary": summary,
    }
