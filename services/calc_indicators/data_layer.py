#!/usr/bin/env python3
"""回测/批量取数的数据层：本地缓存 + 限流 + 硬超时（**仅使用 baostock**）。

数据源策略（明确决策）：
    **只用 baostock，任何情况下都不调用东方财富系数据源（akshare / efinance / adata）。**
    原因（实测）：
      · 东财走 push2his.eastmoney.com，在本机 Python 请求栈上**间歇性可用**
        （同一命令时而成功、时而 RemoteDisconnected；curl 直连同一 URL 恒为 200）；
      · 其调用在半挂起时**无超时、会无限阻塞** —— 实测把 300 只的回测卡在 8/300 上 11 分钟；
      · 混用两个来源会让复权口径/行数不一致（实测 efinance 会忽略传入日期范围返回全量）。
    宁可让某只标的显式取数失败并记录下来，也不引入不可控的阻塞与来源不一致。

为什么必须单独做这一层：

    1. **baostock 限流敏感**。实测：串行 + 0.6s 间隔连拉 80 次零失败（2.6~3.5s/只，10 年区间）；
       而**无间隔快拉 30 次**会导致查询长时间阻塞。故间隔不可去掉。
    2. **回测要反复读同一批历史数据**，无缓存时每次运行都全量重拉，既慢又高概率触发限流。
    3. 缓存把"数据源抖动"与"策略逻辑"解耦：取数失败时回退旧缓存并显式告警，
       而不是让回测拿着半截数据跑出错误结论。

缓存布局（沿用 workspace 既有约定 `data/cache/`）：

    data/cache/calc_indicators/{kind}_{symbol}.csv    # 全量历史（date,open,high,low,close,volume）
    data/cache/calc_indicators/{kind}_{symbol}.json   # 元数据（覆盖区间、来源、更新时间）

命中规则：
    - 请求区间已被缓存覆盖 → 直接读，不触网（回测的常态路径）
    - 请求区间超出缓存 → 拉取 [min(请求起点, 缓存首日), end] 并合并去重
      （**必须取两端更早者**：只从缓存末尾前推的话，"请求更早起点"永远补不上 ——
       实测请求 2016 起却一直返回 3 年缓存）
    - 强制刷新（force=True）→ 忽略缓存重拉
    - 拉取失败但有旧缓存 → 用旧缓存 + 显式 warning（让调用方自己决定是否继续）

限流与超时（实测有效）：
    - 每次请求之间固定 sleep min_interval（默认 0.6s，可调）
    - 单次查询硬超时 source_timeout（默认 20s）—— 半挂起的 socket 必须从外层掐断
    - 失败重试 max_retries（默认 3）次，指数退避 2/4/8s
    - 只用一个 baostock 会话（复用 market_filter._ensure_bs_login）
"""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# 缓存根目录（相对 trading_lab 项目根）
CACHE_SUBDIR = "data/cache/calc_indicators"

# 请求间隔（秒）。0.6s ≈ 100 只/分钟，实测该速率下 baostock 稳定（连拉 80 次零失败）。
DEFAULT_MIN_INTERVAL = 0.6
DEFAULT_MAX_RETRIES = 3

# 单次查询硬超时（秒）。半挂起的 TCP 连接既不返回也不抛错，必须从外层掐断。
DEFAULT_SOURCE_TIMEOUT = 20

# 增量刷新时向前多拉几根：数据源可能事后修正最近几根的复权价
REFRESH_OVERLAP_BARS = 5

_LAST_REQUEST_TS: float = 0.0


# ==================== 路径与元数据 ====================

def _project_root() -> Path:
    """trading_lab 项目根：本文件在 services/calc_indicators/ 下。"""
    return Path(__file__).resolve().parents[2]


def cache_dir() -> Path:
    p = _project_root() / CACHE_SUBDIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def _paths(kind: str, symbol: str) -> tuple:
    base = cache_dir() / f"{kind}_{symbol}"
    return base.with_suffix(".csv"), base.with_suffix(".json")


def _read_meta(kind: str, symbol: str) -> dict:
    _, meta_path = _paths(kind, symbol)
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_meta(kind: str, symbol: str, meta: dict) -> None:
    _, meta_path = _paths(kind, symbol)
    try:
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # pragma: no cover
        logger.warning("[数据层] 元数据写入失败 %s: %s", meta_path, e)


# ==================== 限流与超时 ====================

def _throttle(min_interval: float) -> None:
    """保证两次真实请求之间至少间隔 min_interval 秒。"""
    global _LAST_REQUEST_TS
    if min_interval <= 0:
        return
    elapsed = time.time() - _LAST_REQUEST_TS
    wait = min_interval - elapsed
    if wait > 0:
        time.sleep(wait)
    _LAST_REQUEST_TS = time.time()


def _with_deadline(fn, timeout: float, label: str):
    """用 SIGALRM 给单次数据源调用加硬超时（主线程可用）。

    实测教训：baostock 的 socket 在半挂起时既不返回也不抛错，会无限阻塞 ——
    曾把 300 只的回测卡在 8/300 上约 11 分钟。必须在最外层掐断。
    """
    import signal as _sig

    def _handler(signum, frame):
        raise TimeoutError(f"{label} 超时（>{timeout:.0f}s）")

    old = _sig.signal(_sig.SIGALRM, _handler)
    _sig.alarm(int(max(1, timeout)))
    try:
        return fn()
    finally:
        _sig.alarm(0)
        _sig.signal(_sig.SIGALRM, old)


# ==================== 代码归一 ====================

def normalize_symbol(symbol: str) -> tuple:
    """把 6 位代码转为 (ef_code, bs_code, kind)。

    kind: 'stock' / 'etf'。ETF 判定复用 etf.is_etf（基于代码段）。
    """
    code = (symbol or "").strip()
    if len(code) != 6 or not code.isdigit():
        raise ValueError(f"股票代码格式错误: {code}，应为 6 位数字")
    try:
        from etf import is_etf as _is_etf
        kind = "etf" if _is_etf(code) else "stock"
    except Exception:
        kind = "stock"
    if code.startswith(("6", "5", "9")):
        return code, f"sh.{code}", kind
    return code, f"sz.{code}", kind


# ==================== 底层数据源（仅 baostock）====================

def _fetch_baostock(bs_code: str, start: str, end: str,
                    adjustflag: str = "2") -> Optional[pd.DataFrame]:
    """baostock 单次取数（不负责登录/限流/超时，由调用方管）。

    返回 [date, open, high, low, close, volume]，按日期升序；失败返回 None。
    """
    import baostock as bs
    try:
        rs = bs.query_history_k_data_plus(
            bs_code, "date,open,high,low,close,volume",
            start_date=start, end_date=end, frequency="d", adjustflag=adjustflag,
        )
        if rs.error_code != "0":
            logger.info("[数据层] baostock 返回错误 %s: %s", rs.error_code, rs.error_msg)
            return None
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=rs.fields)
    except Exception as e:
        logger.info("[数据层] baostock 取数异常 %s: %s", bs_code, e)
        return None

    df["date"] = pd.to_datetime(df["date"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    return df[["date", "open", "high", "low", "close", "volume"]] if not df.empty else None


def _fetch_with_retry(symbol: str, start: str, end: str, adjustflag: str,
                      min_interval: float, max_retries: int,
                      source_timeout: float = DEFAULT_SOURCE_TIMEOUT) -> Optional[pd.DataFrame]:
    """带限流 + 硬超时 + 指数退避重试的取数（仅 baostock）。

    不做多源兜底：单只失败返回 None，由 fetch_history 决定"回退旧缓存"还是"抛错"，
    并在元数据里记下来源，保证不会静默混入异源数据。
    """
    from market_filter import _ensure_bs_login

    _, bs_code, _ = normalize_symbol(symbol)
    for attempt in range(1, max_retries + 1):
        _throttle(min_interval)
        if not _ensure_bs_login():
            logger.warning("[数据层] baostock 登录失败（第 %d 次）", attempt)
        else:
            try:
                df = _with_deadline(
                    lambda: _fetch_baostock(bs_code, start, end, adjustflag),
                    source_timeout, f"baostock {symbol}")
            except TimeoutError as e:
                df = None
                logger.warning("[数据层] %s", e)
            if df is not None and not df.empty:
                return df
        if attempt < max_retries:
            backoff = (2 ** attempt) + random.uniform(0, 1)
            logger.info("[数据层] %s 第 %d 次取数失败，退避 %.1fs 后重试", symbol, attempt, backoff)
            time.sleep(backoff)
    return None


# ==================== 缓存读写 ====================

def _load_cache(kind: str, symbol: str) -> tuple:
    csv_path, _ = _paths(kind, symbol)
    if not csv_path.exists():
        return None, {}
    try:
        df = pd.read_csv(csv_path, parse_dates=["date"])
        if df.empty:
            return None, {}
        return df.sort_values("date").reset_index(drop=True), _read_meta(kind, symbol)
    except Exception as e:
        logger.warning("[数据层] 缓存读取失败 %s: %s", csv_path, e)
        return None, {}


def _save_cache(kind: str, symbol: str, df: pd.DataFrame, source: str,
                requested: tuple) -> None:
    csv_path, _ = _paths(kind, symbol)
    try:
        df.to_csv(csv_path, index=False)
        _write_meta(kind, symbol, {
            "symbol": symbol,
            "kind": kind,
            "source": source,
            "rows": int(len(df)),
            "covered_start": df["date"].iloc[0].strftime("%Y-%m-%d"),
            "covered_end": df["date"].iloc[-1].strftime("%Y-%m-%d"),
            "requested": {"start": requested[0], "end": requested[1]},
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        })
    except Exception as e:  # pragma: no cover
        logger.warning("[数据层] 缓存写入失败 %s: %s", csv_path, e)


def _covers(df: pd.DataFrame, start: str, end: str) -> bool:
    """缓存是否覆盖请求区间。

    两端各留容差（跨越周末/节假日）：
      - 起点：缓存首日 ≤ start+3天 即认为"起点够早"
      - 终点：缓存末日 ≥ end−5天 即认为"终点够新"
        （数据源常滞后半天到一天，回测里没必要为最新一两根反复触网）
    """
    if df is None or df.empty:
        return False
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    return (df["date"].iloc[0] <= start_ts + pd.Timedelta(days=3) and
            df["date"].iloc[-1] >= end_ts - pd.Timedelta(days=5))


# ==================== 对外主接口 ====================

def fetch_history(symbol: str, start: str, end: str = "",
                  min_interval: float = DEFAULT_MIN_INTERVAL,
                  max_retries: float = DEFAULT_MAX_RETRIES,
                  force: bool = False,
                  adjustflag: str = "2",
                  allow_stale: bool = True,
                  source_timeout: float = DEFAULT_SOURCE_TIMEOUT) -> pd.DataFrame:
    """取单只标的的历史日线（优先缓存，数据源仅 baostock）。

    Args:
        symbol: 6 位代码
        start / end: 'YYYY-MM-DD'，end 留空表示今天
        min_interval: 请求间隔秒数（防限流；批量拉取建议 ≥0.6）
        max_retries: 单只最大重试次数
        force: True=忽略缓存强制重拉
        adjustflag: baostock 复权标志（'2'=前复权，'1'=后复权，'3'=不复权）
        allow_stale: 取数失败时是否允许用旧缓存兜底（会打 warning）
        source_timeout: 单次查询硬超时秒数

    Returns:
        DataFrame[date, open, high, low, close, volume]（按日期升序）

    Raises:
        RuntimeError: 无缓存且取数失败，或 allow_stale=False 且取数失败
    """
    end = end or datetime.now().strftime("%Y-%m-%d")
    _, _, kind = normalize_symbol(symbol)

    cached, meta = _load_cache(kind, symbol)

    # —— 命中缓存 ——
    if not force and cached is not None and _covers(cached, start, end):
        logger.debug("[数据层] %s 缓存命中 %d 根（%s~%s）", symbol, len(cached),
                     meta.get("covered_start"), meta.get("covered_end"))
        return cached

    # —— 需要向两端扩展或全量拉取 ——
    # 起点取「请求起点」与「缓存首日」的更早者，同时覆盖两种扩展：
    #   · 请求起点更早（向历史回填，如从 3 年扩到 2016 起）
    #   · 缓存更早（向最新增量）
    if cached is not None and not force:
        frm = min(start, cached["date"].iloc[0].strftime("%Y-%m-%d"))
    else:
        frm = start

    df_new = _fetch_with_retry(symbol, frm, end, adjustflag, min_interval,
                               int(max_retries), source_timeout)

    if df_new is None or df_new.empty:
        if cached is not None and allow_stale:
            logger.warning("[数据层] %s 取数失败，回退到旧缓存（%s~%s，可能缺最新数据）",
                           symbol, meta.get("covered_start"), meta.get("covered_end"))
            return cached
        raise RuntimeError(f"{symbol} 取数失败且无可用缓存（区间 {frm}~{end}）")

    if cached is not None and not force:
        merged = (pd.concat([cached, df_new], ignore_index=True)
                  .drop_duplicates(subset=["date"], keep="last")
                  .sort_values("date").reset_index(drop=True))
        source = "baostock+merge"
    else:
        merged = df_new
        source = "baostock"

    merged = merged[merged["date"] >= pd.Timestamp(start) - pd.Timedelta(days=3)]
    merged = merged.reset_index(drop=True)
    _save_cache(kind, symbol, merged, source, (start, end))
    return merged


def cache_stats() -> dict:
    """缓存概览（用于回测前确认"数据已就位、不用再联网"）。"""
    d = cache_dir()
    csvs = sorted(d.glob("*.csv"))
    total_rows = 0
    per_kind: dict = {}
    oldest = newest = None
    for c in csvs:
        try:
            df = pd.read_csv(c, usecols=["date"])
        except Exception:
            continue
        n = len(df)
        total_rows += n
        kind = c.stem.split("_")[0]
        per_kind[kind] = per_kind.get(kind, 0) + 1
        if n:
            s, e = df["date"].iloc[0], df["date"].iloc[-1]
            oldest = s if oldest is None or s < oldest else oldest
            newest = e if newest is None or e > newest else newest
    return {
        "dir": str(d),
        "files": len(csvs),
        "total_rows": total_rows,
        "per_kind": per_kind,
        "date_span": (oldest, newest),
    }


def print_cache_stats() -> None:
    st = cache_stats()
    print(f"  缓存目录: {st['dir']}")
    print(f"  文件数: {st['files']}  总行数: {st['total_rows']:,}  分类: {st['per_kind']}")
    if st["date_span"][0]:
        print(f"  覆盖区间: {st['date_span'][0]} ~ {st['date_span'][1]}")
