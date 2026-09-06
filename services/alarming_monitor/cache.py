"""本地缓存层：双层缓存（历史永久 + 当日 TTL）。

策略：
    - 历史层（>history_split 天前）：永久缓存到 {key}_history.csv，
      避免每日重复请求相同历史数据。命中即用，不再触网。
    - 当日层（≤history_split 天）：每日刷新 {key}_latest.csv，
      TTL=ttl_hours（默认 20h，盘后运行一次足够覆盖次日盘前）。
    - 合并：调用方拿到的最终 DataFrame = history(完整) + latest(增量) 去重。

命中规则：
    1. 缓存未过期 → 直接读 CSV，不触网
    2. 缓存过期 → 触网拉取，成功覆盖；失败用旧缓存 + warning
    3. 无缓存 → 触网拉取，成功写缓存；失败返回 None（调用方降级为灰灯）

断网降级：
    网络失败时优先用最新缓存，仅缺最新日数据，signals 数据不足时按
    现有逻辑灰灯，不阻断整体流程。

用法：
    from cache import cached

    @cached(key_fn=lambda sym, **kw: f"etf_{sym}")
    def fetch_etf_daily(symbol: str, days: int = 30): ...
"""

from __future__ import annotations

import functools
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ====================================================================
# 缓存路径管理
# ====================================================================

def _project_root() -> Path:
    """返回 trading_lab 项目根目录（cache.py 所在 services/alarming_monitor 的上 2 级）。"""
    # cache.py 在 services/alarming_monitor/cache.py
    # parents[0] = alarming_monitor
    # parents[1] = services
    # parents[2] = trading_lab  ← 项目根
    return Path(__file__).resolve().parents[2]


def _cache_dir(cache_root: str = "data/cache") -> Path:
    """缓存根目录：{project_root}/data/cache/。"""
    p = _project_root() / cache_root
    p.mkdir(parents=True, exist_ok=True)
    return p


def _history_path(key: str, cache_root: str = "data/cache") -> Path:
    return _cache_dir(cache_root) / f"{key}_history.csv"


def _latest_path(key: str, cache_root: str = "data/cache") -> Path:
    return _cache_dir(cache_root) / f"{key}_latest.csv"


# ====================================================================
# 工具：判断缓存是否过期 / 读写 CSV
# ====================================================================

def _is_expired(path: Path, ttl_hours: int) -> bool:
    """检查文件 mtime 是否已过 TTL。"""
    if not path.exists():
        return True
    age_sec = time.time() - path.stat().st_mtime
    return age_sec > ttl_hours * 3600


def _read_csv(path: Path) -> Optional[pd.DataFrame]:
    """安全读 CSV。失败/空文件返回 None。"""
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, encoding='utf-8-sig')
        if df.empty:
            return None
        return df
    except Exception as e:
        logger.warning("读缓存失败 %s: %s", path.name, e)
        return None


def _write_csv(df: pd.DataFrame, path: Path) -> None:
    """原子写 CSV：先写同目录 .tmp 文件，再 os.replace 覆盖目标。

    并发场景下不会产生半截/损坏的 CSV；失败仅 warning，不阻断。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + '.tmp')
        df.to_csv(tmp_path, index=False, encoding='utf-8-sig')
        os.replace(tmp_path, path)
    except Exception as e:
        logger.warning("写缓存失败 %s: %s", path.name, e)
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass


def _merge_history_latest(history: Optional[pd.DataFrame],
                           latest: Optional[pd.DataFrame],
                           date_col: str = 'date') -> Optional[pd.DataFrame]:
    """合并 history + latest，按 date 去重保留 latest 的最新值。

    二者皆空 → None；其中一个为空 → 返回另一个。
    """
    if history is None and latest is None:
        return None
    if history is None:
        return latest
    if latest is None:
        return history
    if date_col not in history.columns or date_col not in latest.columns:
        return pd.concat([history, latest], ignore_index=True)
    out = pd.concat([history, latest], ignore_index=True)
    out = out.drop_duplicates(subset=date_col, keep='last')
    return out.sort_values(date_col).reset_index(drop=True)


# ====================================================================
# 装饰器：@cached
# ====================================================================

def cached(key_fn: Callable, ttl_hours: int = 20,
           history_split_days: int = 30,
           cache_root: str = "data/cache",
           freshness_check: bool = True,
           min_refetch_minutes: int = 10):
    """双层缓存装饰器。

    Args:
        key_fn: 从被装饰函数参数生成缓存 key 的函数
                 （如 lambda sym, **kw: f"etf_{sym}"）
        ttl_hours: 当日缓存 TTL（小时），默认 20h
        history_split_days: 历史层/当日层切分点（>30天前永久缓存）
        cache_root: 缓存目录（相对项目根）
        freshness_check: 数据新鲜度校验（默认开启）。
            若缓存最新日期 < 今日，即使 TTL 未过期也视为过期，强制重拉。
            解决"数据源更新前拉取到旧数据并写入缓存，TTL 内不再重拉"的问题。
            周末/节假日时缓存最新日自然为上一个交易日 < 今日，会触发重拉，
            但 min_refetch_minutes 会限制重拉频率，避免频繁触网。
        min_refetch_minutes: freshness_check 触发重拉的最小间隔（分钟）。
            刚拉取过（此时间内）即使数据不含今日也跳过，避免连续运行时频繁触网。

    被装饰函数应返回 pd.DataFrame 或 None。
    缓存命中 → 直接返回合并后的 DataFrame，不触网。
    缓存过期/不存在 → 调用原函数拉取，成功则写入缓存。
    拉取失败 → 用旧缓存降级，仍失败返回 None。

    env: 环境变量 NO_CACHE=1 时禁用缓存（强制重拉，调试用）。
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # 调试开关：NO_CACHE=1 禁用缓存
            if os.environ.get('NO_CACHE', '') == '1':
                return func(*args, **kwargs)

            key = key_fn(*args, **kwargs)
            hist_path = _history_path(key, cache_root)
            latest_path = _latest_path(key, cache_root)

            # 1. 历史层：永久缓存，未过期直接用
            history_df = _read_csv(hist_path)
            # 历史层无过期概念（>30天前永久缓存），只要存在即用

            # 2. 当日层：始终读出（既用于命中判断，也作为拉取失败时的降级兜底）
            # stale_latest_df 保留旧 latest 副本——即使判定过期需要重拉，
            # 若网络失败仍可返回 history + 旧 latest（仅缺最新日），不回写、不清缓存文件。
            latest_df = _read_csv(latest_path)
            latest_expired = _is_expired(latest_path, ttl_hours)
            stale_latest_df = latest_df.copy() if latest_df is not None else None
            # serve_latest：本次实际用于命中服务的 latest（过期或不新鲜时为 None → 触发重拉）
            serve_latest = None if latest_expired else latest_df

            # 2b. 数据新鲜度校验：缓存未过期但数据不含今日 → 视为过期
            if not latest_expired and latest_df is not None and freshness_check:
                today_str = datetime.now().strftime('%Y-%m-%d')
                if 'date' in latest_df.columns and len(latest_df) > 0:
                    cache_latest_date = str(latest_df['date'].iloc[-1])
                    if cache_latest_date < today_str:
                        # 缓存数据不含今日，可能是在数据源更新前拉取的
                        # 检查是否在 min_refetch_minutes 内刚拉取过
                        age_sec = time.time() - latest_path.stat().st_mtime
                        if age_sec < min_refetch_minutes * 60:
                            logger.debug(
                                "freshness: %s 缓存最新日=%s < 今日=%s，"
                                "但 %d分钟内刚拉取过，跳过",
                                key, cache_latest_date, today_str,
                                min_refetch_minutes)
                        else:
                            logger.info(
                                "freshness: %s 缓存最新日=%s < 今日=%s，"
                                "保留旧 latest 副本并尝试重拉",
                                key, cache_latest_date, today_str)
                            latest_expired = True
                            serve_latest = None  # 不直接服务，触发重拉；stale_latest_df 仍保留

            # 3. 命中：两层都有数据且未过期 → 直接合并返回，不触网
            if history_df is not None and serve_latest is not None:
                logger.debug("缓存命中 %s（history+%d 行 / latest+%d 行）",
                             key, len(history_df), len(serve_latest))
                return _merge_history_latest(history_df, serve_latest)

            # 4. 未命中：触网拉取
            try:
                fresh_df = func(*args, **kwargs)
            except Exception as e:
                logger.warning("拉取失败 %s: %s（尝试用旧缓存降级）", key, e)
                fresh_df = None

            if fresh_df is None or fresh_df.empty:
                # 拉取失败：优先用旧 latest 兜底（仅缺最新日），否则仅用历史
                # 不回写、不清缓存文件——保留旧数据以便下次再尝试
                if stale_latest_df is not None:
                    logger.warning("%s 拉取失败，用旧 latest 缓存降级（缺最新日）", key)
                    return _merge_history_latest(history_df, stale_latest_df)
                if history_df is not None:
                    logger.warning("%s 拉取失败，仅用历史缓存降级（缺近30日）", key)
                    return history_df
                return None

            # 5. 切分：>split 天前 → 历史层永久追加；其余 → 当日层覆盖
            fresh_df = fresh_df.copy()
            if 'date' in fresh_df.columns:
                # 确保 date 为字符串
                fresh_df['date'] = fresh_df['date'].astype(str)
                cutoff = (datetime.now() - timedelta(days=history_split_days)
                          ).strftime('%Y-%m-%d')
                hist_part = fresh_df[fresh_df['date'] < cutoff].copy()
                latest_part = fresh_df[fresh_df['date'] >= cutoff].copy()
            else:
                # 无 date 列：全部当 latest 处理
                hist_part = pd.DataFrame()
                latest_part = fresh_df

            # 写历史层：与旧历史合并去重，永久累积
            if not hist_part.empty:
                old_hist = history_df if history_df is not None else pd.DataFrame()
                merged_hist = pd.concat([old_hist, hist_part], ignore_index=True)
                if 'date' in merged_hist.columns:
                    merged_hist = merged_hist.drop_duplicates(
                        subset='date', keep='last').sort_values('date').reset_index(drop=True)
                _write_csv(merged_hist, hist_path)

            # 写当日层：覆盖（最新 30 日）
            if not latest_part.empty:
                _write_csv(latest_part, latest_path)
            elif history_df is None:
                # 极端情况：fresh_df 全空，不写
                pass

            # 6. 返回合并后的完整数据
            final_history = hist_part if not hist_part.empty else history_df
            return _merge_history_latest(final_history, latest_part)

        return wrapper
    return decorator


# ====================================================================
# 工具：清空缓存（调试用）
# ====================================================================

def clear_cache(cache_root: str = "data/cache", key: str = None) -> int:
    """清空缓存目录。

    Args:
        cache_root: 缓存根目录
        key: 若指定，只清此 key 的缓存；否则清全部

    Returns:
        删除的文件数
    """
    d = _cache_dir(cache_root)
    if not d.exists():
        return 0
    count = 0
    for f in d.iterdir():
        if not f.is_file() or not f.name.endswith('.csv'):
            continue
        if key and not f.name.startswith(key):
            continue
        try:
            f.unlink()
            count += 1
        except Exception:
            pass
    logger.info("清空缓存 %s（%d 个文件）", f"key={key}" if key else "全部", count)
    return count


# ====================================================================
# 兼容层：环境检测
# ====================================================================

def is_offline_mode() -> bool:
    """检测是否处于断网降级模式（环境变量 OFFLINE=1）。"""
    return os.environ.get('OFFLINE', '') == '1'
