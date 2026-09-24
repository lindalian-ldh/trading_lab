"""数据获取层：zzshare 涨停历史 + 龙虎榜 + 个股日线。

提供 5 个取数函数：
    fetch_stock_uplimit_history(code, cfg)   个股涨停历史（连板数/原因/封单）
    fetch_lhb_list(date_ymd, cfg)            指定日期龙虎榜（展平买卖席位）
    fetch_lhb_for_stock(code, dates, cfg)     个股跨日龙虎榜记录（限频迭代）
    fetch_daily_kline(symbol, days, cfg)      个股日线（efinance 主 + baostock 兜底）
    fetch_scan_candidates(date_ymd, cfg)      今日涨停股池（--scan 模式候选）

数据源策略（实测后）：
    - zzshare stock_uplimit_reason_history：个股涨停历史，返回 dict{'items':[...]}
    - zzshare lhb_list：龙虎榜，返回 list[dict]，含 buy_group_icons/sell_group_icons
    - zzshare lhb_stock_history/lhb_trader_history：实测返回 None，不可用，改用 lhb_list 跨日迭代
    - efinance：个股日线主源（稳定）
    - baostock：个股日线兜底

设计要点（复用 alarming_monitor/data_loader.py 模式）：
    1. _suppress_output 压制 zzshare 的 stdout/stderr 噪声
    2. _get_zzshare_api / _zz_call 统一 zzshare 调用入口
    3. 限频：_rate_limited_call 每次 sleep(history_request_interval_seconds)，防 429
    4. 缓存：涨停历史 + 日线按 data/cache/ CSV 缓存（TTL 20h）
    5. lhb_list 是日级快照不缓存（同 alarming_monitor fetch_uplimit_stocks 约定）
    6. 各源均失败返回 None，调用方决定降级
"""

from __future__ import annotations

import io
import logging
import os
import sys
import time as _time
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ====================================================================
# 工具：压制 zzshare/akshare 的 stdout/stderr 噪声
# 复用 alarming_monitor/data_loader.py 的实现
# ====================================================================

def _suppress_output(func):
    """压制一切 stdout/stderr（含 fd 级）执行 func，返回其结果。"""
    devnull_fd = os.open(os.devnull, os.O_RDWR)
    saved_out_fd, saved_err_fd = os.dup(1), os.dup(2)
    os.dup2(devnull_fd, 1)
    os.dup2(devnull_fd, 2)
    saved_out_obj, saved_err_obj = sys.stdout, sys.stderr
    saved_dunder_out, saved_dunder_err = sys.__stdout__, sys.__stderr__
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()
    sys.__stdout__ = io.StringIO()
    sys.__stderr__ = io.StringIO()
    try:
        return func()
    finally:
        sys.stdout = saved_out_obj
        sys.stderr = saved_err_obj
        sys.__stdout__ = saved_dunder_out
        sys.__stderr__ = saved_dunder_err
        os.dup2(saved_out_fd, 1)
        os.dup2(saved_err_fd, 2)
        os.close(saved_out_fd)
        os.close(saved_err_fd)
        os.close(devnull_fd)


# ====================================================================
# zzshare 封装（复用 alarming_monitor/data_loader.py 模式）
# ====================================================================

def _get_zzshare_api(cfg=None):
    """返回 (api_mode, handle)。

    api_mode: 'direct'  zzshare 顶层直接暴露 uplimit_stocks 等函数
              'obj'     通过 get_api() 返回的对象调用
              None      zzshare 未安装/不可用
    """
    try:
        import zzshare
    except Exception:
        logger.warning("zzshare 未安装，stock_character 不可用（pip install zzshare）")
        return None, None
    try:
        # 现代 zzshare 直接在顶层暴露
        needed = ['stock_uplimit_reason_history', 'lhb_list',
                  'uplimit_stocks', 'review_uplimit_reason']
        if all(hasattr(zzshare, n) for n in needed):
            return 'direct', zzshare
        # 老版本回退：get_api() 构造对象
        token = getattr(cfg, 'zzshare_token', None) if cfg else None
        if hasattr(zzshare, 'get_api'):
            return 'obj', zzshare.get_api(token)
        if hasattr(zzshare, 'DataApi'):
            return 'obj', zzshare.DataApi(token) if token else zzshare.DataApi()
    except Exception as e:
        logger.warning("zzshare 初始化失败: %s", e)
        return None, None
    logger.warning("zzshare 可用但未找到所需接口（版本过旧？）")
    return None, None


def _zz_call(mode_handle, func_name: str, *args, **kwargs):
    """按 mode_handle 统一调用 zzshare 函数。失败抛异常。"""
    mode, handle = mode_handle
    if mode in ('direct', 'obj'):
        fn = getattr(handle, func_name)
        return fn(*args, **kwargs)
    raise RuntimeError("zzshare 不可用")


# ====================================================================
# 限频：每次 zzshare 调用前 sleep(interval)，防 429
# ====================================================================

_call_timestamps: list = []


def _rate_limited_call(cfg, func, *args, **kwargs):
    """限频执行 func：确保距上次调用 ≥ history_request_interval_seconds 秒。"""
    interval = getattr(cfg, 'history_request_interval_seconds', 0.5) if cfg else 0.5
    now = _time.time()
    if _call_timestamps:
        elapsed = now - _call_timestamps[-1]
        if elapsed < interval:
            sleep_time = interval - elapsed
            logger.debug("限频等待 %.2fs（间隔 %.2fs）", sleep_time, interval)
            _time.sleep(sleep_time)
    _call_timestamps.append(_time.time())
    return func(*args, **kwargs)


# ====================================================================
# 缓存工具：data/cache/ 双层缓存（历史永久 + 当日 TTL）
# ====================================================================

def _cache_dir(cfg=None) -> str:
    """返回缓存目录路径。"""
    if cfg is not None and hasattr(cfg, 'cache_root'):
        d = cfg.cache_root()
        d.mkdir(parents=True, exist_ok=True)
        return str(d)
    # 默认 data/cache/
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))  # trading_lab/
    d = os.path.join(root, "data", "cache")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_path(cache_dir: str, key: str) -> str:
    return os.path.join(cache_dir, f"{key}.csv")


def _is_cache_fresh(path: str, ttl_hours: float = 20.0) -> bool:
    """检查缓存文件是否在 TTL 内且包含今日数据。"""
    if not os.path.exists(path):
        return False
    mtime = os.path.getmtime(path)
    age_hours = (_time.time() - mtime) / 3600
    if age_hours > ttl_hours:
        return False
    # 新鲜度检查：缓存最新日期是否为今日（仿 project_memory 约定）
    try:
        df = pd.read_csv(path, encoding='utf-8-sig')
        if 'date' in df.columns and len(df) > 0:
            latest_date = str(df['date'].iloc[-1])[:10]
            today_str = date.today().isoformat()
            if latest_date < today_str:
                # 缓存最新日期 < 今日，强制刷新（除非是周末/非交易日）
                # 简化处理：仅当 age > 4h 才强制刷新，避免周末反复刷新
                if age_hours > 4:
                    return False
        return True
    except Exception:
        return False


def _read_cache(path: str) -> Optional[pd.DataFrame]:
    try:
        df = pd.read_csv(path, encoding='utf-8-sig')
        return df if len(df) > 0 else None
    except Exception:
        return None


def _write_cache(df: pd.DataFrame, path: str) -> None:
    try:
        df.to_csv(path, index=False, encoding='utf-8-sig')
    except Exception as e:
        logger.debug("写缓存失败 %s: %s", path, e)


# ====================================================================
# 1. 个股涨停历史：stock_uplimit_reason_history(code, page, pageSize)
# ====================================================================

def _normalize_history_items(items: list) -> pd.DataFrame:
    """把 stock_uplimit_reason_history 的 items 归一化为 DataFrame。

    统一列：date / code / name / continue_cnt / limit_up_reason / seal_money
    """
    if not items:
        return pd.DataFrame()
    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        # 日期：date1 字段格式 YYYYMMDD → YYYY-MM-DD
        d = str(it.get('date1', '')).strip()
        if len(d) == 8 and d.isdigit():
            d = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        rows.append({
            'date': d[:10],
            'code': str(it.get('stock_code', '') or '').strip().zfill(6),
            'name': str(it.get('stock_name', '')).strip(),
            'continue_cnt': int(it.get('up_limit_keep_times', 0) or 0),
            'limit_up_reason': str(it.get('reason', '') or '').strip(),
            'seal_money': float(it.get('fengdan_money', 0) or 0),
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df.sort_values('date').reset_index(drop=True)
    return df


def fetch_stock_uplimit_history(code: str, cfg=None) -> Optional[pd.DataFrame]:
    """获取个股涨停历史（归一化版）。

    调用 zzshare stock_uplimit_reason_history(code, page, pageSize)，分页拉满。
    返回 DataFrame: date / code / name / continue_cnt / limit_up_reason / seal_money。
    失败返回 None。

    缓存：data/cache/stock_{code}_history.csv，TTL 20h（日内不重复拉）。
    """
    code = str(code).strip()
    if not code:
        return None

    # 缓存检查
    cache_dir = _cache_dir(cfg)
    cache_key = f"stock_{code}_history"
    cache_path = _cache_path(cache_dir, cache_key)
    if _is_cache_fresh(cache_path, ttl_hours=20.0):
        df = _read_cache(cache_path)
        if df is not None and len(df) > 0:
            logger.debug("涨停历史 %s 命中缓存: %d 行", code, len(df))
            return df

    mode_h = _get_zzshare_api(cfg)
    if mode_h[0] is None:
        return None

    all_items: list = []
    page = 1
    page_size = 100
    max_pages = 20  # 安全上限：最多拉 20 页 = 2000 条
    while page <= max_pages:
        try:
            raw = _rate_limited_call(
                cfg,
                lambda p=page: _suppress_output(
                    lambda: _zz_call(mode_h, 'stock_uplimit_reason_history',
                                     str(code), page=p, pageSize=page_size)
                ),
            )
        except Exception as e:
            logger.debug("stock_uplimit_reason_history(%s, page=%d) 失败: %s",
                         code, page, e)
            break

        if raw is None:
            break
        # 返回结构：dict{'items':[...], 'total':N} 或 list[dict]
        items = None
        if isinstance(raw, dict):
            items = raw.get('items') or raw.get('data') or []
        elif isinstance(raw, list):
            items = raw
        if not items:
            break
        all_items.extend(items)
        # 不足一页 → 已拉满
        if len(items) < page_size:
            break
        page += 1

    if not all_items:
        logger.debug("涨停历史 %s 无数据", code)
        return None

    df = _normalize_history_items(all_items)
    if df is None or len(df) == 0:
        return None

    # 写缓存
    _write_cache(df, cache_path)
    logger.debug("涨停历史 %s 拉取成功: %d 行（缓存到 %s）", code, len(df), cache_path)
    return df


# ====================================================================
# 2. 龙虎榜：lhb_list(date1) → 展平买卖席位
# ====================================================================

def _normalize_lhb_list(raw, date_ymd: str) -> Optional[pd.DataFrame]:
    """把 lhb_list 返回值展平为席位级 DataFrame。

    每只股票的 buy_group_icons / sell_group_icons 展平为多行：
    date / stock_code / stock_name / seat_name / seat_type(buy/sell) /
    amount / youzi_icon / up_desc / up_reason
    """
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        return None

    # 日期格式化 YYYYMMDD → YYYY-MM-DD
    d = str(date_ymd).strip()
    if len(d) == 8 and d.isdigit():
        d = f"{d[:4]}-{d[4:6]}-{d[6:8]}"

    rows = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        stock_code = str(item.get('stock_code', '') or '').strip().zfill(6)
        stock_name = str(item.get('stock_name', '')).strip()
        up_desc = str(item.get('up_desc', '') or '').strip()
        up_reason = str(item.get('up_reason', '') or '').strip()

        # 买入席位
        for seat in item.get('buy_group_icons', []) or []:
            if not isinstance(seat, dict):
                continue
            rows.append({
                'date': d[:10],
                'stock_code': stock_code,
                'stock_name': stock_name,
                'seat_name': str(seat.get('name', '') or '').strip(),
                'seat_type': 'buy',
                'amount': float(seat.get('amount', 0) or 0),
                'youzi_icon': str(seat.get('youzi_icon', '') or '').strip(),
                'up_desc': up_desc,
                'up_reason': up_reason,
            })
        # 卖出席位
        for seat in item.get('sell_group_icons', []) or []:
            if not isinstance(seat, dict):
                continue
            rows.append({
                'date': d[:10],
                'stock_code': stock_code,
                'stock_name': stock_name,
                'seat_name': str(seat.get('name', '') or '').strip(),
                'seat_type': 'sell',
                'amount': float(seat.get('amount', 0) or 0),
                'youzi_icon': str(seat.get('youzi_icon', '') or '').strip(),
                'up_desc': up_desc,
                'up_reason': up_reason,
            })

    if not rows:
        return None
    return pd.DataFrame(rows)


def fetch_lhb_list(date_ymd: str, cfg=None) -> Optional[pd.DataFrame]:
    """获取指定日期龙虎榜（展平席位版）。

    返回 DataFrame: date / stock_code / stock_name / seat_name / seat_type /
    amount / youzi_icon / up_desc / up_reason。
    失败返回 None。不缓存（日级快照，同 alarming_monitor fetch_uplimit_stocks）。
    """
    if not date_ymd:
        return None
    mode_h = _get_zzshare_api(cfg)
    if mode_h[0] is None:
        return None
    try:
        raw = _rate_limited_call(
            cfg,
            lambda: _suppress_output(
                lambda: _zz_call(mode_h, 'lhb_list', date1=str(date_ymd))
            ),
        )
    except Exception as e:
        logger.debug("lhb_list(%s) 失败: %s", date_ymd, e)
        return None
    df = _normalize_lhb_list(raw, date_ymd)
    if df is None or len(df) == 0:
        logger.debug("lhb_list(%s) 无数据", date_ymd)
        return None
    logger.debug("lhb_list(%s) 拉取成功: %d 行", date_ymd, len(df))
    return df


def fetch_lhb_for_stock(code: str, dates: list, cfg=None) -> Optional[pd.DataFrame]:
    """获取个股跨日龙虎榜记录。

    对每个 date 调用 lhb_list，过滤出 stock_code == code 的行，合并。
    限频：每次调用 sleep(history_request_interval_seconds)。

    Args:
        code: 6位股票代码
        dates: 日期列表 ['YYYYMMDD', ...]（通常来自 stock_uplimit_history 的 date）
        cfg: StockCharConfig

    Returns:
        DataFrame 同 fetch_lhb_list 格式，仅含目标股票。
        None 表示无任何龙虎榜记录。
    """
    code = str(code).strip()
    if not code or not dates:
        return None
    mode_h = _get_zzshare_api(cfg)
    if mode_h[0] is None:
        return None

    all_dfs = []
    for date_ymd in dates:
        try:
            raw = _rate_limited_call(
                cfg,
                lambda d=date_ymd: _suppress_output(
                    lambda: _zz_call(mode_h, 'lhb_list', date1=str(d))
                ),
            )
        except Exception as e:
            logger.debug("lhb_list(%s) for stock %s 失败: %s", date_ymd, code, e)
            continue
        df = _normalize_lhb_list(raw, date_ymd)
        if df is None or len(df) == 0:
            continue
        # 过滤目标股票
        mask = df['stock_code'].astype(str).str.strip() == code
        sub = df[mask]
        if len(sub) > 0:
            all_dfs.append(sub)

    if not all_dfs:
        return None
    out = pd.concat(all_dfs, ignore_index=True)
    out = out.sort_values('date').reset_index(drop=True)
    logger.debug("个股龙虎榜 %s 拉取成功: %d 行（跨 %d 日）",
                 code, len(out), len(all_dfs))
    return out


# ====================================================================
# 3. 个股日线：efinance（主）+ baostock（兜底）
# 复用 sell_monitor/data_loader.py 的 _try_efinance / _try_baostock 模式
# ====================================================================

def _normalize_code(symbol: str):
    """6位代码 → (efinance_code, baostock_code)。"""
    code = str(symbol).strip().lstrip('shSHszSZ.')
    if not code:
        return symbol, symbol
    # 沪市: 6/5/9 开头
    if code.startswith(("6", "5", "9", "11", "13")):
        return code, f"sh.{code}"
    return code, f"sz.{code}"


def _try_efinance(ef_code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
    """efinance 数据源（主源）。"""
    try:
        import efinance as ef
        df = ef.stock.get_quote_history(
            ef_code,
            beg=start_date.replace("-", ""),
            end=end_date.replace("-", ""),
        )
        if df is None or df.empty:
            return None
        rename_map = {
            '日期': 'date', '开盘': 'open', '最高': 'high',
            '最低': 'low', '收盘': 'close', '成交量': 'volume',
        }
        df = df.rename(columns=rename_map)
        needed = ['date', 'open', 'high', 'low', 'close', 'volume']
        df = df[[c for c in needed if c in df.columns]]
        df['date'] = df['date'].astype(str).str[:10]
        for c in ['open', 'high', 'low', 'close', 'volume']:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce')
        df = df.dropna(subset=['close']).sort_values('date').reset_index(drop=True)
        return df
    except Exception as e:
        logger.debug("efinance 失败 %s: %s", ef_code, e)
        return None


def _try_baostock(bs_code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
    """baostock 数据源（兜底）。"""
    try:
        import baostock as bs
        bs.login()
        try:
            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,open,high,low,close,volume",
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="2",   # 前复权
            )
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
        finally:
            bs.logout()
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=rs.fields)
        df['date'] = df['date'].astype(str).str[:10]
        for c in ['open', 'high', 'low', 'close', 'volume']:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce')
        df = df.dropna(subset=['close']).sort_values('date').reset_index(drop=True)
        return df
    except Exception as e:
        logger.debug("baostock 失败 %s: %s", bs_code, e)
        return None


def fetch_daily_kline(symbol: str, days: int = 180, cfg=None) -> Optional[pd.DataFrame]:
    """获取个股近 N 个日历日的日线。

    Args:
        symbol: 6位股票代码
        days: 取近 N 个日历日（默认 180 ≈ 6个月）
        cfg: StockCharConfig（用于缓存路径）

    Returns:
        DataFrame: date/open/high/low/close/volume，按日期升序。
        失败返回 None。

    数据源：efinance（主）+ baostock（兜底）。
    缓存：data/cache/stock_{code}_kline.csv，TTL 20h。
    """
    symbol = str(symbol).strip()
    if not symbol:
        return None

    # 缓存检查
    cache_dir = _cache_dir(cfg)
    cache_key = f"stock_{symbol}_kline"
    cache_path = _cache_path(cache_dir, cache_key)
    if _is_cache_fresh(cache_path, ttl_hours=20.0):
        df = _read_cache(cache_path)
        if df is not None and len(df) > 0:
            # 裁剪到近 N 日
            df['date'] = df['date'].astype(str).str[:10]
            cutoff = (date.today() - timedelta(days=days)).isoformat()
            df = df[df['date'] >= cutoff].reset_index(drop=True)
            if len(df) > 0:
                logger.debug("日线 %s 命中缓存: %d 行", symbol, len(df))
                return df

    ef_code, bs_code = _normalize_code(symbol)
    end = date.today()
    start = end - timedelta(days=days + 15)  # 多拉15天余量
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    # efinance 主源
    df = _suppress_output(lambda: _try_efinance(ef_code, start_str, end_str))
    if df is not None and not df.empty:
        _write_cache(df, cache_path)
        logger.debug("efinance 获取 %s 成功: %d 行", symbol, len(df))
        return df

    # baostock 兜底
    df = _suppress_output(lambda: _try_baostock(bs_code, start_str, end_str))
    if df is not None and not df.empty:
        _write_cache(df, cache_path)
        logger.debug("baostock 获取 %s 成功: %d 行", symbol, len(df))
        return df

    logger.warning("日线获取失败 symbol=%s（efinance+baostock 均失败）", symbol)
    return None


# ====================================================================
# 4. 扫描候选池：今日涨停股（--scan 模式）
# 复用 alarming_monitor/data_loader.py 的 review_uplimit_reason 逻辑
# ====================================================================

def fetch_scan_candidates(date_ymd: str, cfg=None) -> list:
    """获取指定日期涨停股代码列表（用于 --scan 模式）。

    主力接口：review_uplimit_reason（涨停复盘，全量涨停股含首板+连板）。
    回退接口：uplimit_stocks（仅连板股，数据可能不全）。

    Args:
        date_ymd: 'YYYYMMDD' 字符串
        cfg: StockCharConfig

    Returns:
        list[code]：6位股票代码列表（去重）。空列表表示无数据。
    """
    mode_h = _get_zzshare_api(cfg)
    if mode_h[0] is None:
        return []

    codes: list = []

    # 主力：review_uplimit_reason（展平 stocks）
    try:
        raw = _rate_limited_call(
            cfg,
            lambda: _suppress_output(
                lambda: _zz_call(mode_h, 'review_uplimit_reason', date1=str(date_ymd))
            ),
        )
        if isinstance(raw, (list, tuple)):
            for plate_item in raw:
                if not isinstance(plate_item, dict):
                    continue
                stocks = plate_item.get('stocks')
                if isinstance(stocks, list):
                    for s in stocks:
                        if isinstance(s, dict):
                            c = str(s.get('stock_code', '') or s.get('code', '') or '').strip().zfill(6)
                            if c:
                                codes.append(c)
    except Exception as e:
        logger.debug("review_uplimit_reason(%s) 失败，尝试 fallback: %s", date_ymd, e)

    # 回退：uplimit_stocks
    if not codes:
        try:
            raw = _rate_limited_call(
                cfg,
                lambda: _suppress_output(
                    lambda: _zz_call(mode_h, 'uplimit_stocks', date1=str(date_ymd))
                ),
            )
            if isinstance(raw, (list, tuple)):
                for item in raw:
                    if isinstance(item, dict):
                        c = str(item.get('stock_code', '') or item.get('code', '') or '').strip().zfill(6)
                        if c:
                            codes.append(c)
            elif isinstance(raw, pd.DataFrame) and 'stock_code' in raw.columns:
                codes = raw['stock_code'].astype(str).str.strip().str.zfill(6).tolist()
        except Exception as e:
            logger.debug("uplimit_stocks(%s) fallback 失败: %s", date_ymd, e)

    # 去重
    seen = set()
    unique = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            unique.append(c)

    logger.debug("扫描候选池 %s: %d 只", date_ymd, len(unique))
    return unique


# ====================================================================
# 工具：日期格式转换
# ====================================================================

def to_ymd(d: str) -> str:
    """YYYY-MM-DD → YYYYMMDD。"""
    return str(d).replace("-", "")[:8]


def to_iso(d: str) -> str:
    """YYYYMMDD → YYYY-MM-DD。"""
    d = str(d).strip()
    if len(d) == 8 and d.isdigit():
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return d[:10]
