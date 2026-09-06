"""zzshare 数据获取层：板块排名 + 板块成分股排行 + 涨停热门。

职责：
    1. 封装 zzshare SDK 的 plates_rank / market_plate_stocks / uplimit_stocks / uplimit_hot
    2. 限频保护：每次调用后 time.sleep(request_interval)，避免触发 429（免费版 30次/分钟）
    3. 失败重试：网络异常/空数据时按 retry_times 重试
    4. 降级缓存：接口失败时读取本地 JSON 缓存（最近一个有数据的交易日），保证流程不中断
    5. 原始数据落盘：成功响应写入 data/raw/bankuai/{date1}/ 便于复盘

zzshare 实际返回字段（已通过实测确认）：
    plates_rank → plate_code / plate_name / rate(涨跌幅%) / trade_money / score(热度) / time
    market_plate_stocks → stock_code / stock_name / rank / px_change_rate / turnover_ratio /
                           circulation_value / vol_ratio / attention
    uplimit_hot / uplimit_stocks → 详见 client.py SHORTCUTS（含 limit_times / continuous / seal_time 等）

注意：原需求文档假设成分股返回 turnover(成交额) 字段，实际 zzshare 接口未提供。
      leader_selector 中以 circulation_value * turnover_ratio / 100
      作为成交额估算值（est_turnover），用于二级龙头排序。
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from config import ScanConfig

logger = logging.getLogger(__name__)


# ====================================================================
# zzshare 客户端工厂
# ====================================================================

def get_api(cfg: ScanConfig):
    """创建 zzshare DataApi 实例。

    token 优先级：cfg.token → 环境变量 ZZSHARE_TOKEN → 'anonymous'。
    """
    import zzshare

    token = cfg.token or ""
    # DataApi 内部也会读 ZZSHARE_TOKEN，显式传入更可控
    return zzshare.pro_api(token=token, timeout=cfg.timeout)


# ====================================================================
# 缓存路径
# ====================================================================

def _cache_root(cfg: ScanConfig) -> Path:
    """返回 bankuai 原始缓存根目录。"""
    if cfg.cache_dir:
        return Path(cfg.cache_dir)
    # 默认：trading_lab/data/raw/bankuai/
    here = Path(__file__).resolve().parent
    return here.parent.parent / "data" / "raw" / "bankuai"


def _cache_dir_for(cfg: ScanConfig, date1: str) -> Path:
    return _cache_root(cfg) / date1


def _save_cache(cfg: ScanConfig, date1: str, filename: str, data: Any) -> Path:
    """把原始响应写入 JSON 缓存。"""
    d = _cache_dir_for(cfg, date1)
    d.mkdir(parents=True, exist_ok=True)
    path = d / filename
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("写入缓存失败 %s: %s", path, e)
    return path


def _load_cache(cfg: ScanConfig, date1: str, filename: str) -> Optional[Any]:
    """读取指定日期的缓存，不存在返回 None。"""
    path = _cache_dir_for(cfg, date1) / filename
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("读取缓存失败 %s: %s", path, e)
        return None


def _load_latest_cache(cfg: ScanConfig, date1: str, filename: str,
                       look_back_days: int = 30) -> Optional[Any]:
    """读取最近一个有数据的交易日的缓存（降级使用）。

    从 date1 开始向前回溯 look_back_days 天，找到第一个含该 filename 的目录。
    """
    try:
        dt = date.fromisoformat(date1)
    except ValueError:
        return None
    for i in range(1, look_back_days + 1):
        prev = (dt - timedelta(days=i)).isoformat()
        data = _load_cache(cfg, prev, filename)
        if data is not None:
            logger.info("降级使用缓存: %s/%s", prev, filename)
            return data
    return None


# ====================================================================
# 限频 + 重试装饰
# ====================================================================

def _call_with_retry(func, cfg: ScanConfig, label: str) -> Optional[Any]:
    """带重试的调用包装。func 应返回原始数据（list/dict）或 None。"""
    last_err: Optional[Exception] = None
    for attempt in range(1 + cfg.retry_times):
        try:
            data = func()
            if data is not None:
                return data
            logger.warning("[%s] 第 %d 次返回空数据", label, attempt + 1)
        except Exception as e:
            last_err = e
            logger.warning("[%s] 第 %d 次异常: %s", label, attempt + 1, e)
        if attempt < cfg.retry_times:
            # 重试前额外等待（比 request_interval 略长）
            time.sleep(cfg.request_interval + 1)
    if last_err:
        logger.error("[%s] 重试 %d 次后仍失败: %s", label, cfg.retry_times, last_err)
    return None


def _throttle(cfg: ScanConfig):
    """请求后限频等待。"""
    if cfg.request_interval > 0:
        time.sleep(cfg.request_interval)


# ====================================================================
# 对外接口
# ====================================================================

def fetch_plates_rank(date1: str, cfg: ScanConfig, limit: int = 50) -> Optional[list]:
    """获取板块热度排名。

    Args:
        date1: 查询日期 YYYY-MM-DD
        cfg: 扫描配置
        limit: 返回条数（至少 50，确保能取到尾部冷门板块）

    Returns:
        list[dict] 原始板块排名数据，按热度(score)降序；失败降级返回缓存，全失败返回 None
    """
    api = get_api(cfg)
    label = f"plates_rank[{cfg.plate_type_name()}]"

    def _do():
        return api.plates_rank(plate_type=cfg.plate_type, date1=date1, limit=limit)

    data = _call_with_retry(_do, cfg, label)
    _throttle(cfg)

    if data:
        _save_cache(cfg, date1, f"plates_rank_{cfg.plate_type}.json", data)
        return data

    # 降级：当日缓存 → 历史缓存
    cache = _load_cache(cfg, date1, f"plates_rank_{cfg.plate_type}.json")
    if cache is None:
        cache = _load_latest_cache(cfg, date1, f"plates_rank_{cfg.plate_type}.json")
    if cache is not None:
        logger.info("[%s] 接口失败，使用缓存数据(%d 条)", label, len(cache) if isinstance(cache, list) else 0)
    return cache


def fetch_plate_stocks(plate_code: str, date1: str, cfg: ScanConfig,
                       limit: Optional[int] = None) -> Optional[list]:
    """获取板块成分股人气排行。

    Args:
        plate_code: 板块代码（如 885852）
        date1: 查询日期 YYYY-MM-DD
        cfg: 扫描配置
        limit: 返回条数，None 时用 cfg.stock_limit

    Returns:
        list[dict] 成分股排行；失败降级返回缓存，全失败返回 None
    """
    api = get_api(cfg)
    use_limit = limit if limit is not None else cfg.stock_limit
    label = f"plate_stocks[{plate_code}]"

    def _do():
        # market_plate_stocks 路径含 {plate_type}，默认 17；按 cfg.plate_type 传入
        return api.market_plate_stocks(
            plate_code=plate_code, date1=date1,
            is_real=1, limit=use_limit, plate_type=cfg.plate_type,
        )

    data = _call_with_retry(_do, cfg, label)
    _throttle(cfg)

    if data:
        _save_cache(cfg, date1, f"stocks_{cfg.plate_type}_{plate_code}.json", data)
        return data

    cache = _load_cache(cfg, date1, f"stocks_{cfg.plate_type}_{plate_code}.json")
    if cache is None:
        cache = _load_latest_cache(cfg, date1, f"stocks_{cfg.plate_type}_{plate_code}.json")
    if cache is not None:
        logger.info("[%s] 接口失败，使用缓存数据(%d 条)", label, len(cache) if isinstance(cache, list) else 0)
    return cache


def fetch_uplimit_stocks(date1: str, cfg: ScanConfig) -> Optional[list]:
    """获取当日全市场涨停股池（用于一级龙头的封板时间/板块首个涨停判定）。

    调用 zzshare ``uplimit_stocks(date1)`` 接口。该接口返回全市场当日所有涨停股，
    一个板块扫描周期内只需调用一次（全市场共享），由 scanner 层缓存后分发给各板块。

    Args:
        date1: 查询日期 YYYY-MM-DD
        cfg: 扫描配置

    Returns:
        list[dict] 涨停股列表（原始字段，由调用方按多种字段名容错提取）；
        接口失败时降级返回缓存；全失败返回 None（调用方据此走降级评分）
    """
    if not cfg.enable_uplimit_pool:
        return None

    api = get_api(cfg)
    label = "uplimit_stocks"

    def _do():
        # uplimit_stocks 路径: open/review/uplimit/stocks/{date1}
        # 返回结构可能是 list[dict] 或 dict 包装的 list，统一在 _call_with_retry 后规整
        return api.uplimit_stocks(date1=date1)

    data = _call_with_retry(_do, cfg, label)
    _throttle(cfg)

    # 规整：接口可能返回 {"list": [...]} 或直接 [...]
    if isinstance(data, dict):
        inner = data.get("list") or data.get("data") or data.get("items")
        if isinstance(inner, list):
            data = inner
        else:
            data = [data]  # 退化为单条
    if isinstance(data, dict):
        data = None

    if data:
        _save_cache(cfg, date1, "uplimit_stocks.json", data)
        return data

    cache = _load_cache(cfg, date1, "uplimit_stocks.json")
    if cache is None:
        cache = _load_latest_cache(cfg, date1, "uplimit_stocks.json")
    if cache is not None:
        logger.info("[%s] 接口失败，使用缓存数据(%d 条)",
                    label, len(cache) if isinstance(cache, list) else 0)
    return cache


# ====================================================================
# 涨停热门板块 Fetch 接口
# ====================================================================

def fetch_uplimit_hot(date1: str, cfg: ScanConfig) -> Optional[list]:
    """获取当日涨停热门板块（zzshare uplimit_hot，可选模块）。

    仅当 cfg.enable_uplimit_hot=True 时由 scanner 调用。用于补充"涨停热门板块
    涨停数 & 最高连板龙头"信息。复用现有 _call_with_retry + _throttle + 缓存降级。
    """
    if not cfg.enable_uplimit_hot:
        return None

    api = get_api(cfg)
    label = f"uplimit_hot[{cfg.plate_type_name()}]"

    def _do():
        # uplimit_hot: open/review/uplimit/hot，参数 date1 + board（板块类型整数编码）
        # board 默认传概念；可能返回 list[dict] 或 {"data": [...]}，统一后规整
        return api.uplimit_hot(date1=date1, board=cfg.plate_type)

    data = _call_with_retry(_do, cfg, label)
    _throttle(cfg)

    if isinstance(data, dict):
        inner = data.get("list") or data.get("data") or data.get("items")
        if isinstance(inner, list):
            data = inner
        elif isinstance(data, dict):
            data = [data]
    if isinstance(data, dict):
        data = None

    if data:
        _save_cache(cfg, date1, f"uplimit_hot_{cfg.plate_type}.json", data)
        return data

    cache = _load_cache(cfg, date1, f"uplimit_hot_{cfg.plate_type}.json")
    if cache is None:
        cache = _load_latest_cache(cfg, date1, f"uplimit_hot_{cfg.plate_type}.json")
    if cache is not None:
        logger.info("[%s] 接口失败，使用缓存数据(%d 条)",
                    label, len(cache) if isinstance(cache, list) else 0)
    return cache
