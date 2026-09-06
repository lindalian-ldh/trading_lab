"""市场预警数据获取层：腾讯源（指数/ETF）+ 新浪指数 spot（成交额）+ legu（涨跌家数）。

提供 4 个取数函数：
    fetch_index_daily()      指数日线（close + 成交额，用于 S2/S4）
    fetch_etf_daily()        ETF 日线（close，用于 S4/S5）
    fetch_breadth_today()    今日涨跌家数（legu，用于 S3 今日口径）
    fetch_market_amount()    两市总成交额（新浪指数 spot 成交额列，用于 S1）

数据源策略（实测后保留稳定源）：
    - 腾讯 stock_zh_index_daily_tx：稳定，指数历史日线 ✓（主源）
    - 腾讯 stock_zh_a_hist_tx：稳定，ETF 历史日线 ✓（主源）
    - 新浪 stock_zh_index_spot_sina：稳定，两市成交额 ✓（S1 主源）
    - legu stock_market_activity_legu：稳定，今日涨跌家数键值对 ✓
    - adata：指数/ETF 二级回退（可选依赖）
    - S3 历史序列：legu 仅今日，由 storage 层 CSV 自累积重建近 20 日

设计要点：
    1. 列标准化为英文小写：date/open/high/low/close/volume/amount，date 为 YYYY-MM-DD 字符串。
    2. _suppress_output 压制 akshare 的 stdout/stderr 噪声（复用 sell_monitor 模式）。
    3. adata 为可选依赖，import 失败时优雅降级（log warning，不阻断）。
    4. 各源均失败返回 None，调用方决定降级。
"""

from __future__ import annotations

import io
import logging
import os
import sys
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
import time as _time

logger = logging.getLogger(__name__)

# ====================================================================
# 全局 in-memory 缓存：东财 ETF 快照批量查询（单次全量拉取 + 20s TTL）
# fund_etf_spot_em 每次 15~20s（全市场几千行），19 只 ETF 若串行调用 × 19 次 ≈ 6min
# 改为一次拉取全局复用，≤ 20s 搞定
# ====================================================================
_ETF_SPOT_CACHE: dict = {"df": None, "expire_at": 0.0}


# ====================================================================
# 缓存：双层缓存装饰器（历史永久 + 当日 TTL）
# ====================================================================
# 通过环境变量 NO_CACHE=1 可禁用缓存（调试用）。
try:
    from cache import cached, _cache_dir
    _HAS_CACHE = True
except ImportError:
    _HAS_CACHE = False
    def cached(*args, **kwargs):  # type: ignore
        """无 cache 模块时的空装饰器（透传）。"""
        def decorator(func):
            return func
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]  # 直接当装饰器用
        return decorator
    def _cache_dir(*a, **kw):
        return None


# ====================================================================
# 工具：压制 akshare/adata 的 stdout/stderr 噪声
# ====================================================================

def _suppress_output(func):
    """压制一切 stdout/stderr（含 fd 级）执行 func，返回其结果。

    akshare 在 import / 调用阶段会向 stdout/stderr 打印进度信息，
    会污染 alarming_monitor 的控制台输出。
    """
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


def _date_str(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _to_tx_symbol(code: str) -> str:
    """6 位代码 → 腾讯系交易所前缀（sh/sz）。

    沪市: 60/68/51/50/56/58/11/13 开头 → 'sh'
    深市: 00/30/15/16/12/14 开头 → 'sz'
    用于 ak.stock_zh_a_hist_tx / stock_zh_index_daily_tx 的 symbol 参数。

    注意：指数代码（如 sh000300 沪深300、sh000001 上证综指）虽 00 开头但属沪市，
    必须保留原 sh/sz 前缀，不能按 6 位代码推断。故优先判断是否已带前缀。
    """
    s = str(code).strip()
    # 已带 sh/sz 前缀的（含指数代码如 sh000300）原样小写返回
    if s.lower().startswith(('sh', 'sz')):
        return s.lower()
    # 6 位裸代码：按开头数字推断交易所
    c = s.lstrip('shSHzsSZ')  # 兜底去残留前缀
    if not c:
        return s
    if c[:2] in ('60', '68', '51', '50', '56', '58', '11', '13'):
        return 'sh' + c
    if c[:2] in ('00', '30', '15', '16', '12', '14'):
        return 'sz' + c
    return 'sz' + c


# ====================================================================
# 1. 两市总成交额（S1 分子）+ 今日涨跌家数（S3 今日口径）
# ====================================================================

def fetch_market_amount(sh_symbol: str = "sh000001",
                        sz_symbol: str = "sz399001") -> Optional[float]:
    """两市总成交额（元）：新浪指数实时快照的"成交额"列求和。

    数据源：ak.stock_zh_index_spot_sina()，返回全市场指数实时行情，
    含"成交额"列（单位：元，实测 sh000001=1.218万亿、sz399001=1.293万亿，
    两市合计 2.51万亿，与新华/AASTOCKS 报道一致）。

    注意：腾讯源 stock_zh_index_daily_tx 的 amount 是"成交量(手)"非成交额，
    不能用作 S1 分子；本函数用新浪指数 spot 的成交额列。
    失败返回 None。
    """
    df = _suppress_output(_try_index_spot_sina)
    if df is None or df.empty:
        logger.warning("新浪指数 spot 获取失败，两市总成交额不可取")
        return None
    # 按"代码"列过滤目标指数
    if '代码' not in df.columns or '成交额' not in df.columns:
        logger.warning("新浪指数 spot 列结构异常: %s", list(df.columns))
        return None
    hit = df[df['代码'].astype(str).isin([sh_symbol, sz_symbol])]
    if hit.empty:
        logger.warning("新浪指数 spot 未匹配 %s / %s", sh_symbol, sz_symbol)
        return None
    amt = pd.to_numeric(hit['成交额'], errors='coerce').dropna()
    if amt.empty:
        return None
    total = float(amt.sum())
    logger.debug("两市总成交额: %.0f 亿 (%.3f 万亿)", total / 1e8, total / 1e12)
    return total


def _try_index_spot_sina() -> Optional[pd.DataFrame]:
    """新浪指数实时快照（ak.stock_zh_index_spot_sina）。

    返回全市场指数 spot，含"成交额"(元) 列，用于 S1 两市总成交额。
    实测稳定（连续多次成功）。
    """
    try:
        import akshare as ak
        df = ak.stock_zh_index_spot_sina()
        if df is None or df.empty:
            return None
        return df
    except Exception as e:
        logger.debug("akshare stock_zh_index_spot_sina 失败: %s", e)
        return None


def fetch_breadth_today() -> Optional[pd.DataFrame]:
    """今日涨跌家数（legu stock_market_activity_legu 键值对解析）。

    legu 返回 [item, value] 键值对，含"上涨"/"下跌"/"平盘"/"统计日期"。
    解析为单行 DataFrame：date / up_count / down_count / flat_count / ad_ratio。

    legu 仅返回今日快照（非历史序列），S3 历史 20 日序列由 storage 层
    CSV 自累积重建（read_breadth_history + append_breadth_today）。
    失败返回 None。
    """
    df = _suppress_output(_try_breadth_legu)
    if df is not None and not df.empty:
        logger.debug("legu 今日涨跌家数获取成功: up=%s down=%s",
                     df.iloc[0].get('up_count'), df.iloc[0].get('down_count'))
        return df
    logger.warning("今日涨跌家数获取失败（legu 不可取），S3 将降级")
    return None


def _try_breadth_legu() -> Optional[pd.DataFrame]:
    """legu 键值对解析为单行涨跌家数 df。"""
    try:
        import akshare as ak
        df = ak.stock_market_activity_legu()
        if df is None or df.empty:
            return None
        # legu 返回 [item, value] 两列键值对
        kv = {}
        for _, row in df.iterrows():
            item = str(row.iloc[0]).strip()
            val = row.iloc[1]
            kv[item] = val
        up = _to_int(kv.get('上涨'))
        down = _to_int(kv.get('下跌'))
        flat = _to_int(kv.get('平盘'))
        date_str = str(kv.get('统计日期', ''))[:10]
        if not date_str:
            from datetime import date as _d
            date_str = _d.today().strftime('%Y-%m-%d')
        if up is None or down is None:
            logger.debug("legu 解析失败，kv=%s", kv)
            return None
        import numpy as np
        ad_ratio = float(up / down) if down > 0 else float('inf')
        return pd.DataFrame([{
            'date': date_str,
            'up_count': up,
            'down_count': down,
            'flat_count': flat if flat is not None else 0,
            'ad_ratio': ad_ratio,
        }])
    except Exception as e:
        logger.debug("legu stock_market_activity_legu 解析失败: %s", e)
        return None


def _to_int(v) -> Optional[int]:
    """legu value 列容错转 int（支持 '420.0' / 420 / '420'）。"""
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


# ====================================================================
# 2. 指数日线（S2 放量不涨：成交额 + 收盘价）
# ====================================================================

def fetch_index_daily(symbol: str, days: int = 30) -> Optional[pd.DataFrame]:
    """指数日线，取最近 days 个交易日。

    Args:
        symbol: akshare 指数代码，如 "sh000300" / "sh000001"
        days: 取近 N 个交易日（多取余量）

    返回：date/open/high/low/close/volume/amount，升序，最后一行最新。
    失败返回 None。

    数据源：腾讯源 stock_zh_index_daily_tx（实测稳定），
    adata 为二级回退。东财源已删（代理拦截，稳定失败）。

    缓存：history 层永久（>30天前）+ latest 层 20h TTL（≤30天）。
    委托给 _fetch_index_full（带 @cached）拉完整数据，本地 tail 裁剪。
    """
    full = _fetch_index_full(symbol)
    if full is None or full.empty:
        logger.warning("指数日线获取失败 symbol=%s（腾讯+adata 均失败或缓存空）", symbol)
        return None
    out = full.tail(days).reset_index(drop=True)
    logger.debug("指数 %s 裁剪到 %d 行（来自缓存/拉取）", symbol, len(out))
    return out


@cached(key_fn=lambda symbol, **kw: f"index_{symbol}")
def _fetch_index_full(symbol: str) -> Optional[pd.DataFrame]:
    """拉取完整指数日线（不 tail），带 @cached 双层缓存。

    缓存命中时秒级返回；未命中时触网拉取近 5 年全历史，
    成功后写入 history 层（>30天前永久）+ latest 层（≤30天，20h TTL）。
    """
    end = date.today()
    # 拉近 5 年全历史（足够覆盖 ADX 14 + WMA 20 + 20 日均值）
    start = end - timedelta(days=365 * 5 + 30)
    start_str = _date_str(start)
    end_str = _date_str(end)

    # 腾讯源（实测稳定，主源）
    df = _suppress_output(lambda: _try_index_daily_tx(symbol, start_str, end_str))
    if df is not None and not df.empty:
        logger.debug("腾讯源 指数 %s 获取成功: %d 行", symbol, len(df))
        return df

    # adata 二级回退
    df = _suppress_output(lambda: _try_index_daily_adata(symbol, start_str, end_str))
    if df is not None and not df.empty:
        logger.debug("adata 指数 %s 获取成功: %d 行", symbol, len(df))
        return df

    return None


def _try_index_daily_tx(symbol: str, start: str, end: str) -> Optional[pd.DataFrame]:
    """腾讯源指数日线（ak.stock_zh_index_daily_tx）。

    实测稳定，作为主源。接口返回全历史（无 start/end 参数），
    本地按 [start,end] 过滤。symbol 需带 sh/sz 前缀（如 sh000300），
    由 _to_tx_symbol 归一。
    """
    try:
        import akshare as ak
        tx_symbol = _to_tx_symbol(symbol)
        df = ak.stock_zh_index_daily_tx(symbol=tx_symbol)
        if df is None or df.empty:
            return None
        df = _normalize_ohlc(df)
        if df is None:
            return None
        df = _filter_by_date(df, start, end)
        if 'amount' not in df.columns and 'volume' in df.columns:
            df['amount'] = df['volume']
        return df
    except Exception as e:
        logger.debug("akshare stock_zh_index_daily_tx 失败 %s: %s", symbol, e)
        return None


def _try_index_daily_adata(symbol: str, start: str, end: str) -> Optional[pd.DataFrame]:
    try:
        import adata
        # adata 指数代码格式与 akshare 不同（如 '000300'），尽力尝试
        code = symbol.replace('sh', '').replace('sz', '').replace('sh', '')
        df = adata.stock.market.get_market(stock_code=code, k_type=1,
                                           start_date=start, end_date=end)
        if df is None or df.empty:
            return None
        df = _normalize_ohlc(df)
        return df
    except ImportError:
        return None
    except Exception as e:
        logger.debug("adata 指数失败: %s", e)
        return None


# ====================================================================
# 3. ETF 日线（S4/S5 板块代表涨幅）
# ====================================================================

def fetch_etf_daily(symbol: str, days: int = 30) -> Optional[pd.DataFrame]:
    """ETF 日线（前复权），取最近 days 个交易日。

    Args:
        symbol: 6 位 ETF 代码，如 "512800"
        days: 取近 N 个交易日

    返回：date/open/high/low/close/volume/amount，升序。
    失败返回 None。

    数据源：腾讯源 stock_zh_a_hist_tx（实测稳定，主源），
    adata 为二级回退。东财源已删（代理拦截，稳定失败）。

    缓存：history 层永久（>30天前）+ latest 层 20h TTL（≤30天）。
    委托给 _fetch_etf_full（带 @cached）拉完整数据，本地 tail 裁剪。
    """
    full = _fetch_etf_full(symbol)
    if full is None or full.empty:
        logger.warning("ETF 日线获取失败 symbol=%s（腾讯+adata 均失败或缓存空）", symbol)
        return None
    out = full.tail(days).reset_index(drop=True)
    logger.debug("ETF %s 裁剪到 %d 行（来自缓存/拉取）", symbol, len(out))
    return out


@cached(key_fn=lambda symbol, **kw: f"etf_{symbol}")
def _fetch_etf_full(symbol: str) -> Optional[pd.DataFrame]:
    """拉取完整 ETF 日线（不 tail），带 @cached 双层缓存。

    缓存命中时秒级返回；未命中时触网拉取近 5 年全历史，
    成功后写入 history 层（>30天前永久）+ latest 层（≤30天，20h TTL）。

    数据源：腾讯历史日线（主源）→ adata 回退 → 东财实时快照补今日。
    """
    end = date.today()
    start = end - timedelta(days=365 * 5 + 30)
    start_str = _date_str(start)
    end_str = _date_str(end)

    # 腾讯源（实测稳定，主源）
    df = _suppress_output(lambda: _try_etf_tx(symbol, start_str, end_str))
    if df is not None and not df.empty:
        # 检查是否含今日数据，不含则用东财实时快照补充
        today_str = end.isoformat()
        if str(df['date'].iloc[-1]) < today_str:
            spot = _suppress_output(lambda: _try_etf_spot_em(symbol))
            if spot is not None and not spot.empty:
                df = pd.concat([df, spot], ignore_index=True)
                logger.debug("ETF %s 东财快照补充今日数据: close=%s",
                             symbol, spot.iloc[0]['close'])
        logger.debug("腾讯源 ETF %s 获取成功: %d 行", symbol, len(df))
        return df

    # adata 二级回退
    df = _suppress_output(lambda: _try_etf_adata(symbol, start_str, end_str))
    if df is not None and not df.empty:
        # 同样用东财快照补今日
        today_str = end.isoformat()
        if str(df['date'].iloc[-1]) < today_str:
            spot = _suppress_output(lambda: _try_etf_spot_em(symbol))
            if spot is not None and not spot.empty:
                df = pd.concat([df, spot], ignore_index=True)
                logger.debug("ETF %s (adata) 东财快照补充今日数据", symbol)
        logger.debug("adata ETF %s 获取成功: %d 行", symbol, len(df))
        return df

    # 三级兜底：仅东财快照（至少能拿到今日 OHLCV，历史靠缓存）
    spot = _suppress_output(lambda: _try_etf_spot_em(symbol))
    if spot is not None and not spot.empty:
        logger.debug("ETF %s 仅东财快照（历史靠缓存）: %d 行", symbol, len(spot))
        return spot

    return None


def _try_etf_tx(symbol: str, start: str, end: str) -> Optional[pd.DataFrame]:
    """腾讯源 ETF 日线（ak.stock_zh_a_hist_tx）。

    ETF 在腾讯源按个股接口取（前复权），symbol 需带 sh/sz 前缀。
    接口返回全历史，本地按 [start,end] 过滤。
    """
    try:
        import akshare as ak
        tx_symbol = _to_tx_symbol(symbol)
        df = ak.stock_zh_a_hist_tx(symbol=tx_symbol, adjust="qfq")
        if df is None or df.empty:
            return None
        df = _normalize_ohlc(df)
        if df is None:
            return None
        df = _filter_by_date(df, start, end)
        return df
    except Exception as e:
        logger.debug("akshare stock_zh_a_hist_tx (ETF) 失败 %s: %s", symbol, e)
        return None


def _try_etf_adata(symbol: str, start: str, end: str) -> Optional[pd.DataFrame]:
    try:
        import adata
        df = adata.stock.market.get_market(stock_code=symbol, k_type=1,
                                           start_date=start, end_date=end)
        if df is None or df.empty:
            return None
        df = _normalize_ohlc(df)
        return df
    except ImportError:
        return None
    except Exception as e:
        logger.debug("adata ETF 失败 %s: %s", symbol, e)
        return None


def _get_etf_spot_df_batch(ttl_seconds: int = 30) -> Optional[pd.DataFrame]:
    """批量获取东财 ETF 全市场快照（带进程内全局缓存 + TTL）。

    fund_etf_spot_em 单次请求耗时 15~20s（返回全市场 ~5000 行 ETF），
    必须一次性拉取后多只 ETF 复用，不能逐只调用。

    Args:
        ttl_seconds: 缓存 TTL（秒），默认 30s。一次 main.py 运行 <60s，
                      同一进程中复用 1 次即可。
    """
    now = _time.time()
    if (_ETF_SPOT_CACHE["df"] is not None
            and now < _ETF_SPOT_CACHE["expire_at"]):
        return _ETF_SPOT_CACHE["df"]
    try:
        import akshare as ak
        df = ak.fund_etf_spot_em()
        if df is None or df.empty:
            return None
        # 字符串化代码列，避免 float 代码（如 159995 → 159995.0）
        df = df.copy()
        df['代码'] = df['代码'].astype(str).str.strip()
        _ETF_SPOT_CACHE["df"] = df
        _ETF_SPOT_CACHE["expire_at"] = now + ttl_seconds
        return df
    except Exception as e:
        logger.debug("东财 ETF 批量快照 获取失败: %s", e)
        return None


def _try_etf_spot_em(symbol: str) -> Optional[pd.DataFrame]:
    """东财 ETF 实时快照（ak.fund_etf_spot_em），**批量缓存版**。

    首次调用会拉取全市场 ETF 快照（~15-20s）并缓存 30s；
    后续调用直接从缓存查表，< 0.001s/只。

    Returns:
        单行 DataFrame（date/open/high/low/close/volume/amount），失败返回 None。
    """
    df = _get_etf_spot_df_batch(ttl_seconds=30)
    if df is None:
        return None
    try:
        row = df[df['代码'] == str(symbol).strip()]
        if row.empty:
            return None
        r = row.iloc[0]
        today = date.today().isoformat()
        # 注意：东财 fund_etf_spot_em 的「成交量」是「手」单位（1手=100股）
        # 而腾讯源 stock_zh_a_hist_tx 的 volume 是「股」单位，
        # 需 ×100 对齐，否则今日 vs 历史量级差 100x 会导致 MA5/MA20 量比严重失真。
        vol_em = float(r.get('成交量', 0) or 0) * 100
        return pd.DataFrame([{
            'date': today,
            'open': float(r.get('开盘价', 0) or 0),
            'high': float(r.get('最高价', 0) or 0),
            'low': float(r.get('最低价', 0) or 0),
            'close': float(r.get('最新价', 0) or 0),
            'volume': vol_em,
            'amount': float(r.get('成交额', 0) or 0),
        }])
    except Exception as e:
        logger.debug("东财 ETF spot %s 解析失败: %s", symbol, e)
        return None


# ====================================================================
# 4. 涨跌家数历史（S3：曾>2.5 且今日<1.0）
# ====================================================================
# 注：fetch_breadth_today() 在 Section 1 提供（legu 今日口径）。
# legu 仅返回今日快照，S3 历史 20 日序列由 storage 层 CSV 自累积重建：
#   - storage.append_breadth_today(date, up, down, ratio)  每次运行追加
#   - storage.read_breadth_history(days)                   读回近 N 日序列
# main._gather_data 负责拼接"历史 CSV + 今日 legu"传给 S3。


# ====================================================================
# 共用：OHLC 列标准化 + 日期过滤
# ====================================================================

def _normalize_ohlc(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """把 akshare/adata 中文 OHLC 列标准化为英文小写。"""
    rename = {
        '日期': 'date', '开盘': 'open', '收盘': 'close',
        '最高': 'high', '最低': 'low', '成交量': 'volume',
        '成交额': 'amount',
        # adata 英文兼容
        'date': 'date', 'open': 'open', 'close': 'close',
        'high': 'high', 'low': 'low', 'volume': 'volume', 'amount': 'amount',
        'trade_date': 'date',
    }
    df = df.rename(columns=rename)
    needed = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount']
    df = df[[c for c in needed if c in df.columns]]
    if 'date' not in df.columns or 'close' not in df.columns:
        return None
    df['date'] = df['date'].astype(str).str[:10]
    for c in ['open', 'high', 'low', 'close', 'volume', 'amount']:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
    df = df.dropna(subset=['close']).sort_values('date').reset_index(drop=True)
    return df


def _filter_by_date(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """按 [start, end] 过滤日期（闭区间）。"""
    mask = (df['date'] >= start) & (df['date'] <= end)
    return df[mask].reset_index(drop=True)


# ====================================================================
# Section 5: 连板龙头 & 大盘温度（zzshare 接口）
# ====================================================================
# 字段别名归一化：不同版本 zzshare 返回字段差异较大，
# 统一成：code / name / continue_cnt / limit_up_time / seal_money /
#         limit_up_reason / pct_chg / is_st
# ====================================================================

_LINK_UPLIMIT_COL_ALIASES: dict = {
    "code":            ["ts_code", "ticker", "code", "股票代码", "证券代码", "Code",
                        "stock_code", "symbol"],
    "name":            ["name", "股票名称", "证券简称", "名称", "Name",
                        "stock_name", "sec_name", "short_name"],
    "continue_cnt":    ["连续涨停天数", "continue_day_cnt", "continue_days",
                        "连板天数", "连续板数", "lian_ban_cnt", "连板数",
                        "up_limit_keep_times", "keep_times", "continuous_board"],
    "limit_up_time":   ["limit_up_time", "首次涨停时间", "涨停时间", "first_limit_up_time",
                        "up_limit_time", "first_seal_time"],
    "seal_money":      ["seal_money", "封单金额", "封单资金", "封单(元)", "封板资金",
                        "seal_amount", "block_money", "fengdan_money"],
    # reason 优先于 up_limit_desc：review_uplimit_reason 里 reason 是完整涨停原因文本，
    # up_limit_desc 只是"2连板"/"首板"这样的连板描述，不是真正的涨停原因
    "limit_up_reason": ["limit_up_reason", "涨停原因", "概念", "题材", "涨停概念",
                        "reason", "up_limit_desc", "concept"],
    "pct_chg":         ["pct_chg", "涨跌幅", "涨幅", "change_pct", "pct_change", "change"],
    "is_st":           ["is_st", "ST标记", "是否ST", "st_flag", "st"],
}


def _normalize_uplimit_columns(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """把 zzshare uplimit_stocks 的列名归一化为统一小写英文字段。

    对每一个目标列，从别名表里找 df.columns 命中的第一个列；
    未命中目标列时：数值列填 NaN，字符串列填 ''，is_st 填 False。

    输入 df 为 None/空 → None。
    """
    if df is None or len(df) == 0:
        return None
    # 列名 str 化 & strip & 大小写不敏感：先建立 {lower_stripped_actual_col: original_col} 映射
    actual_cols = {str(c).strip(): c for c in df.columns}
    lower_map = {str(c).strip().lower(): c for c in df.columns}

    out_cols = {}
    for target, aliases in _LINK_UPLIMIT_COL_ALIASES.items():
        src = None
        for alias in aliases:
            a = str(alias).strip().lower()
            if a in lower_map:
                src = lower_map[a]
                break
        if src is not None:
            out_cols[target] = df[src].values
        else:
            # 未命中：根据类型填默认
            if target == "is_st":
                out_cols[target] = [False] * len(df)
            elif target in ("continue_cnt",):
                out_cols[target] = [0] * len(df)
            elif target in ("seal_money", "pct_chg"):
                import numpy as _np
                out_cols[target] = [_np.nan] * len(df)
            else:
                out_cols[target] = [""] * len(df)

    out = pd.DataFrame(out_cols)
    # 数值转型：continue_cnt int，seal_money float，pct_chg float
    out["continue_cnt"] = pd.to_numeric(out["continue_cnt"], errors="coerce").fillna(0).astype(int)
    out["seal_money"] = pd.to_numeric(out["seal_money"], errors="coerce")
    out["pct_chg"] = pd.to_numeric(out["pct_chg"], errors="coerce")
    # is_st bool：如果原始是字符串 '1'/'0'/'ST'/*ST* 则判断
    def _to_bool(x):
        if isinstance(x, bool):
            return x
        s = str(x).strip()
        if s.lower() in ("1", "true", "yes", "y", "st"):
            return True
        if "ST" in s:
            return True
        return False
    out["is_st"] = out["is_st"].map(_to_bool)
    # 兜底：name 字段带 ST 也顺手标记（老源 is_st 缺失场景）
    for i in range(len(out)):
        if not out.loc[i, "is_st"]:
            nm = str(out.loc[i, "name"])
            if nm.startswith("ST") or nm.startswith("*ST") or nm.startswith("NST"):
                out.loc[i, "is_st"] = True
    return out.reset_index(drop=True)


def _get_zzshare_api():
    """返回 (api_mode,  handle):

    api_mode: 'direct'  表示 zzshare 顶层直接暴露 uplimit_stocks 等函数
              'obj'     表示通过 get_api() 返回的对象调用
              None      表示 zzshare 未安装 / 不可用
    handle:   api_mode='direct' → zzshare 模块本身；'obj' → api 对象；None → None
    """
    # 延迟 import：没装 zzshare 时其他模块依然工作
    try:
        import zzshare
    except Exception:
        logger.warning("zzshare 未安装，连板模块不可用（pip install zzshare）")
        return None, None
    try:
        # 现代 zzshare 直接在顶层暴露 uplimit_stocks/uplimit_hot/stock_uplimit_reason
        if (hasattr(zzshare, 'uplimit_stocks')
                and hasattr(zzshare, 'uplimit_hot')
                and hasattr(zzshare, 'stock_uplimit_reason')):
            return 'direct', zzshare
        # 老版本回退：get_api() 构造对象
        import os as _os
        token = _os.environ.get("ZZSHARE_TOKEN") or None
        if hasattr(zzshare, 'get_api'):
            return 'obj', zzshare.get_api(token)
        # 兜底 DataApi
        if hasattr(zzshare, 'DataApi'):
            return 'obj', zzshare.DataApi(token) if token else zzshare.DataApi()
    except Exception as e:
        logger.warning("zzshare 初始化失败: %s", e)
        return None, None
    logger.warning("zzshare 可用但未找到 uplimit_* 接口（版本过旧？）")
    return None, None


def _zz_call(mode_handle, func_name: str, *args, **kwargs):
    """按 mode_handle 统一调用 zzshare 函数。失败抛异常。"""
    mode, handle = mode_handle
    if mode == 'direct':
        fn = getattr(handle, func_name)
        return fn(*args, **kwargs)
    elif mode == 'obj':
        fn = getattr(handle, func_name)
        return fn(*args, **kwargs)
    else:
        raise RuntimeError("zzshare 不可用")


def _any_to_df(raw) -> Optional[pd.DataFrame]:
    """uplimit_stocks 返回值规范化：list[dict] / DataFrame / 其他 → DataFrame 或 None。"""
    if raw is None:
        return None
    if isinstance(raw, pd.DataFrame):
        return raw if len(raw) > 0 else None
    if isinstance(raw, (list, tuple)):
        if len(raw) == 0:
            return None
        try:
            df = pd.DataFrame(list(raw))
            return df if len(df) > 0 else None
        except Exception:
            return None
    # 其他类型（dict 等）：直接包成 list
    try:
        df = pd.DataFrame([raw])
        return df
    except Exception:
        return None


def _flatten_review_stocks(raw) -> Optional[pd.DataFrame]:
    """展平 review_uplimit_reason 的返回结构。

    review_uplimit_reason 返回 list[dict]，每个 dict 含 plate_code / plate_name /
    plate_score / stocks(list[dict])。本函数把所有板块下的 stocks 展平成一张表。
    """
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        return None
    all_stocks: list = []
    for plate_item in raw:
        if not isinstance(plate_item, dict):
            continue
        stocks = plate_item.get('stocks')
        if isinstance(stocks, list):
            for s in stocks:
                if isinstance(s, dict):
                    all_stocks.append(s)
    if not all_stocks:
        return None
    try:
        df = pd.DataFrame(all_stocks)
        return df if len(df) > 0 else None
    except Exception:
        return None


def _fetch_uplimit_core(date_ymd: str) -> Optional[pd.DataFrame]:
    """涨停股数据获取核心逻辑（不含缓存）。

    主力接口：review_uplimit_reason —— 返回全量涨停股（含首板+连板），
    每只股票携带 up_limit_keep_times（连板数）、fengdan_money（封单金额）、
    up_limit_time（首板时间）、reason（涨停原因）等完整字段。

    回退接口：uplimit_stocks —— 仅返回连板股，字段较少且数据可能不全。
    """
    mode_h = _get_zzshare_api()
    if mode_h[0] is None:
        return None

    # ---- 主力：review_uplimit_reason（板块复盘 → 展平 stocks） ----
    try:
        raw = _suppress_output(lambda: _zz_call(mode_h, 'review_uplimit_reason', date1=str(date_ymd)))
        df = _flatten_review_stocks(raw)
        if df is not None and len(df) > 0:
            normalized = _normalize_uplimit_columns(df)
            if normalized is not None and len(normalized) > 0:
                normalized = _dedup_uplimit_by_code(normalized)
                if len(normalized) > 0:
                    normalized["date"] = f"{date_ymd[:4]}-{date_ymd[4:6]}-{date_ymd[6:8]}"
                    return normalized.reset_index(drop=True)
    except Exception as e:
        logger.debug("review_uplimit_reason(%s) 失败，尝试 fallback: %s", date_ymd, e)

    # ---- 回退：uplimit_stocks（仅连板股，字段较少） ----
    try:
        raw = _suppress_output(lambda: _zz_call(mode_h, 'uplimit_stocks', date1=str(date_ymd)))
        df = _any_to_df(raw)
        if df is None or len(df) == 0:
            return None
        normalized = _normalize_uplimit_columns(df)
        if normalized is None or len(normalized) == 0:
            return None
        normalized = _dedup_uplimit_by_code(normalized)
        if len(normalized) == 0:
            return None
        normalized["date"] = f"{date_ymd[:4]}-{date_ymd[4:6]}-{date_ymd[6:8]}"
        return normalized.reset_index(drop=True)
    except Exception as e:
        logger.debug("uplimit_stocks(%s) fallback 失败: %s", date_ymd, e)
        return None


# 注意：涨停数据是日级快照（同日多行，每行一只股票），不适合 @cached 的
# 时间序列缓存逻辑（按 date 去重会把同日 82 行压成 1 行）。
# 每次运行最多调用 2 次（今日+昨日），直接走无缓存即可。
def fetch_uplimit_stocks(date_ymd: str) -> Optional[pd.DataFrame]:
    """获取指定日期涨停股列表（归一化列版）。

    主力数据源为 review_uplimit_reason（涨停复盘），返回全量涨停股（含首板+连板）。
    回退到 uplimit_stocks（仅连板股）。

    Args:
        date_ymd: 'YYYYMMDD' 字符串

    Returns:
        DataFrame with columns:
          code, name, continue_cnt(int), limit_up_time,
          seal_money(float,元), limit_up_reason, pct_chg, is_st(bool), date
        None on failure / empty.
    """
    return _fetch_uplimit_core(date_ymd)


def fetch_uplimit_hot(date_ymd: str) -> Optional[pd.DataFrame]:
    """获取指定日期涨停热门板块（辅助）。失败/空 None。"""
    mode_h = _get_zzshare_api()
    if mode_h[0] is None:
        return None
    try:
        df = _suppress_output(lambda: _zz_call(mode_h, 'uplimit_hot', date1=str(date_ymd)))
    except Exception as e:
        logger.debug("zzshare uplimit_hot(%s) 失败: %s", date_ymd, e)
        return None
    if df is None or (hasattr(df, '__len__') and len(df) == 0):
        return None
    if isinstance(df, pd.DataFrame):
        return df.reset_index(drop=True)
    return df


def _dedup_uplimit_by_code(normalized: pd.DataFrame) -> pd.DataFrame:
    """zzshare 涨停表按 plate_code 拆行时会导致同 code 多行，按 code 去重。

    排序优先级（保留每组第一行）：continue_cnt DESC, seal_money DESC,
    非空 limit_up_reason 优先, limit_up_time ASC。
    """
    if normalized is None or len(normalized) == 0:
        return normalized
    if 'code' not in normalized.columns:
        return normalized
    work = normalized.copy()
    # 构造辅助排序列
    work['_seal'] = pd.to_numeric(work['seal_money'], errors='coerce').fillna(0) if 'seal_money' in work.columns else 0
    work['_reason_nonempty'] = work['limit_up_reason'].map(
        lambda v: 0 if (isinstance(v, str) and v.strip() and v.lower() not in ('nan', 'none')) else 1
    ) if 'limit_up_reason' in work.columns else 1
    work['_time'] = work['limit_up_time'].astype(str).map(
        lambda s: '99:99:99' if (not s or s.strip() == '' or str(s).lower() == 'nan') else s
    ) if 'limit_up_time' in work.columns else '99:99:99'
    sort_by = [c for c in ['continue_cnt', '_seal', '_reason_nonempty', '_time'] if c in work.columns]
    ascending = [
        (c == '_time' or c == '_reason_nonempty')
        for c in sort_by
    ]
    # continue_cnt 和 _seal 是 DESC → 对应 ascending=False
    asc_list = []
    for c in sort_by:
        if c in ('continue_cnt', '_seal'):
            asc_list.append(False)
        else:
            asc_list.append(True)
    work = work.sort_values(by=sort_by, ascending=asc_list, kind='mergesort', na_position='last')
    dedup = work.drop_duplicates(subset=['code'], keep='first').reset_index(drop=True)
    for col in ['_seal', '_reason_nonempty', '_time']:
        if col in dedup.columns:
            dedup = dedup.drop(columns=[col])
    return dedup


def fetch_uplimit_reason(code: str, date_ymd: str) -> Optional[str]:
    """获取个股指定日期的涨停原因字符串。None 表示不可得。"""
    if not code or not date_ymd:
        return None
    mode_h = _get_zzshare_api()
    if mode_h[0] is None:
        return None
    try:
        reason = _suppress_output(lambda: _zz_call(mode_h, 'stock_uplimit_reason', str(code), str(date_ymd)))
    except Exception as e:
        logger.debug("zzshare stock_uplimit_reason(%s,%s) 失败: %s", code, date_ymd, e)
        return None
    if reason is None:
        return None
    if isinstance(reason, (str, int, float)):
        s = str(reason).strip()
        return s or None
    # 如果返回是 DataFrame，挑第一个可能的原因列
    try:
        import pandas as _pd
        if isinstance(reason, _pd.DataFrame) and len(reason) > 0:
            for col in ["limit_up_reason", "涨停原因", "概念", "题材", "reason"]:
                if col in reason.columns:
                    s = str(reason[col].iloc[0]).strip()
                    if s and s.lower() not in ("nan", "none"):
                        return s
    except Exception:
        pass
    return None


# ====================================================================
# S6: 两融汇总（SSE + SZSE 合并，单位统一为元）
# ====================================================================

def _margin_date_fmt(d: str) -> str:
    """接受 YYYY-MM-DD / YYYYMMDD，返回 YYYYMMDD（akshare sse/szse 要求）。"""
    s = str(d or '').strip()
    if not s:
        return ''
    return s.replace('-', '').replace('/', '')[:8]


def _margin_rows_to_yuan(s) -> pd.Series:
    """把 SZSE 快照单位转成元。

    实测：
      - SSE stock_margin_sse 返回「融资余额」=1.34e12（元，原始 int）
      - SZSE stock_margin_szse 返回「融资余额」=12790.04（亿，浮点数）
    启发式：如果最大值 < 1e6（亿级或更小），则 × 1e8；否则按原值。
    """
    import numpy as _np
    x = pd.to_numeric(s, errors='coerce')
    if x.dropna().empty:
        return x
    maxabs = float(x.abs().max())
    if maxabs > 0 and maxabs < 1e6:
        return (x * 1e8).fillna(_np.nan)
    return x


def _try_margin_sse(start_ymd: str, end_ymd: str) -> Optional[pd.DataFrame]:
    """上交所两融区间汇总 → 归一化 DataFrame（单位统一为元，升序 date）。"""
    try:
        import akshare as ak
        df = ak.stock_margin_sse(start_date=str(start_ymd), end_date=str(end_ymd))
    except Exception as e:
        logger.debug("stock_margin_sse(%s~%s) 失败: %s", start_ymd, end_ymd, e)
        return None
    if df is None or df.empty:
        return None
    try:
        rename = {
            '信用交易日期': 'date',
            '融资余额':      'rzye',
            '融资买入额':    'rzmre',
            '融券余量金额':  'rqye',
            '融券卖出量':    'rqmcl',
            '融资融券余额':  'rzrqye',
            '融券余量':      'rqyl',
        }
        df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
        if 'date' not in df.columns:
            logger.debug("sse margin 缺 date 列: %s", list(df.columns))
            return None
        df['date'] = df['date'].astype(str).map(
            lambda s: f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s[:10])
        for c in ['rzye', 'rzmre', 'rqye', 'rqmcl', 'rzrqye', 'rqyl']:
            if c in df.columns:
                df[c] = _margin_rows_to_yuan(df[c])
        # 按日期升序
        df = df.sort_values('date').reset_index(drop=True)
        return df
    except Exception as e:
        logger.debug("sse margin 解析失败: %s", e)
        return None


def _try_margin_szse_snapshot(ymd: str) -> Optional[pd.DataFrame]:
    """深交所两融日快照汇总 → 单行 DataFrame。

    stock_margin_szse(YYYYMMDD) 有时候会报 length mismatch（周末/节假日/数据未出），
    此时 fallback 到 stock_margin_detail_szse 明细汇总。
    """
    try:
        import akshare as ak
        df = ak.stock_margin_szse(date=str(ymd))
    except Exception as e:
        logger.debug("stock_margin_szse(%s) 汇总快照失败，fallback 明细: %s", ymd, e)
        df = None
    if df is not None and not df.empty:
        # 汇总表列：融资买入额、融资余额、融券卖出量、融券余量、融券余额、融资融券余额
        rename = {
            '融资买入额': 'rzmre',
            '融资余额':   'rzye',
            '融券卖出量': 'rqmcl',
            '融券余量':   'rqyl',
            '融券余额':   'rqye',
            '融资融券余额': 'rzrqye',
        }
        try:
            df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
            if 'date' not in df.columns:
                df = df.copy()
                s_ = str(ymd)
                df['date'] = f"{s_[:4]}-{s_[4:6]}-{s_[6:8]}" if len(s_) == 8 else s_[:10]
            for c in ['rzye', 'rzmre', 'rqye', 'rqmcl', 'rzrqye', 'rqyl']:
                if c in df.columns:
                    df[c] = _margin_rows_to_yuan(df[c])
            return df.reset_index(drop=True).tail(1).reset_index(drop=True)
        except Exception as e:
            logger.debug("szse 汇总解析失败，fallback 明细: %s", e)

    # --- fallback：明细 → 汇总 ---
    try:
        import akshare as ak
        dfd = ak.stock_margin_detail_szse(date=str(ymd))
    except Exception as e:
        logger.debug("stock_margin_detail_szse(%s) 明细也失败: %s", ymd, e)
        return None
    if dfd is None or len(dfd) == 0:
        logger.debug("szse margin detail(%s) 空，可能为非交易日", ymd)
        return None
    try:
        rename = {
            '融资买入额': 'rzmre',
            '融资余额':   'rzye',
            '融券卖出量': 'rqmcl',
            '融券余量':   'rqyl',
            '融券余额':   'rqye',
            '融资融券余额': 'rzrqye',
        }
        dfd = dfd.rename(columns={k: v for k, v in rename.items() if k in dfd.columns})
        row = {'date': f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"}
        for c in ['rzye', 'rzmre', 'rqye', 'rqmcl', 'rzrqye', 'rqyl']:
            if c in dfd.columns:
                v = pd.to_numeric(dfd[c], errors='coerce').sum()
                # SZSE 明细单位也可能是亿；统一走元归一化
                v = float(v)
                if 0 < v < 1e6:
                    v = v * 1e8
                row[c] = v
            else:
                import numpy as _np
                row[c] = _np.nan
        return pd.DataFrame([row])
    except Exception as e:
        logger.debug("szse margin 明细汇总失败: %s", e)
        return None


def fetch_margin_summary(ref_date: str,
                         lookback_days: int = 25,
                         prefer_csv_df: Optional[pd.DataFrame] = None
                         ) -> Optional[pd.DataFrame]:
    """全市场两融汇总序列（SSE + SZSE 按 date 相加，单位统一为元）。

    Args:
        ref_date:       参考日 YYYY-MM-DD / YYYYMMDD
        lookback_days:  SSE 区间拉取回溯天数（默认 25，覆盖 20 日增速）
        prefer_csv_df:  storage 层 CSV 自累积的历史 DataFrame，优先拼接。
                        列要求包含: date,rzye,rzmre,rqye,rqmcl,rzrqye,rqyl
                        若 prefer_csv_df 已覆盖 [T-lookback, T]，则只补拉 SZSE 今日。

    Returns:
        DataFrame（升序 date），列:
          date(str YYYY-MM-DD)、rzye(融资余额元)、rzmre(融资买入额元)、
          rqye(融券余额元)、rzrqye(融资融券余额元)、rqmcl(融券卖出量)、rqyl(融券余量)
        data_source: 'sse_szse_merged'
        失败返回 None。
    """
    ref_ymd = _margin_date_fmt(ref_date)
    if not ref_ymd:
        return None
    # 构造 SSE start_date：回退 lookback_days 个自然日（非交易日自然缺失即可）
    try:
        ref_dt = datetime.strptime(ref_ymd, "%Y%m%d")
    except Exception:
        ref_dt = datetime.now()
    start_dt = ref_dt - timedelta(days=int(lookback_days) + 5)
    start_ymd = start_dt.strftime("%Y%m%d")
    end_ymd = ref_dt.strftime("%Y%m%d")

    # ---- 优先 CSV：如果 CSV 覆盖到 最新日期就免拉 SSE ----
    sse: Optional[pd.DataFrame] = None
    if prefer_csv_df is not None and len(prefer_csv_df) > 0:
        need_cols = {'date', 'rzye', 'rzmre', 'rqye', 'rzrqye'}
        if need_cols.issubset(set(prefer_csv_df.columns)):
            # 用 CSV 作为 sse
            sse = prefer_csv_df.copy()
            sse = sse.sort_values('date').reset_index(drop=True)
            # 缺失列兜底 NaN
            for c in ['rzye', 'rzmre', 'rqye', 'rqmcl', 'rzrqye', 'rqyl']:
                if c not in sse.columns:
                    import numpy as _np
                    sse[c] = _np.nan
            # 缺 end_ymd 或 CSV 最后一行早于 end_ymd → 再拉 SSE 区间做拼接
            last_dt_s = str(sse['date'].iloc[-1])[:10]
            last_dt_s = last_dt_s.replace('-', '')
            sse_ok = (last_dt_s >= end_ymd) and (len(sse) >= lookback_days)
            if not sse_ok:
                logger.debug("margin CSV 不覆盖最新日或不足 %s 行，补拉 SSE 区间", lookback_days)
                sse_raw = _suppress_output(lambda: _try_margin_sse(start_ymd, end_ymd))
                if sse_raw is not None and len(sse_raw) > 0:
                    sse = pd.concat([sse, sse_raw], ignore_index=True)
                    sse = sse.drop_duplicates(subset=['date'], keep='last')
                    sse = sse.sort_values('date').reset_index(drop=True)
            else:
                logger.debug("margin CSV 直接复用（含 %s 行，最新 %s）", len(sse), last_dt_s)
        else:
            sse = _suppress_output(lambda: _try_margin_sse(start_ymd, end_ymd))
    else:
        sse = _suppress_output(lambda: _try_margin_sse(start_ymd, end_ymd))

    if sse is None or len(sse) == 0:
        logger.warning("两融 SSE 历史不可取，S6 降级")
        return None

    # ---- SZSE 最新日快照（今日；失败回退昨日）----
    szse: Optional[pd.DataFrame] = None
    for delta in (0, 1, 2):
        dt_i = ref_dt - timedelta(days=delta)
        ymd_i = dt_i.strftime("%Y%m%d")
        row = _suppress_output(lambda d=ymd_i: _try_margin_szse_snapshot(d))
        if row is not None and len(row) > 0:
            szse = row
            if delta > 0:
                logger.debug("SZSE 两融用 %s（今日数据未出）", ymd_i)
            break
    if szse is None:
        logger.warning("两融 SZSE 近 3 日均不可取，S6 仅用 SSE")
        merged = sse.copy()
    else:
        # 合并：在 SSE 上找到 SZSE 相同 date，数值列相加；未找到追加
        merged = sse.copy()
        sz_date = str(szse.iloc[0]['date'])[:10]
        numeric_cols = [c for c in ['rzye', 'rzmre', 'rqye', 'rqmcl', 'rzrqye', 'rqyl'] if c in szse.columns]
        if sz_date in set(merged['date'].astype(str).tolist()):
            idx = merged.index[merged['date'].astype(str) == sz_date][-1]
            for c in numeric_cols:
                if c in merged.columns and c in szse.columns:
                    merged.loc[idx, c] = (float(pd.to_numeric(merged.loc[idx, c], errors='coerce') or 0)
                                          + float(pd.to_numeric(szse.iloc[0][c], errors='coerce') or 0))
        else:
            # SZSE 日期未在 SSE 出现 → 构造带累加的新行：数字列 = SZSE 当日值（SSE 空按 0 算）
            new_row = {'date': sz_date}
            for c in numeric_cols:
                new_row[c] = float(pd.to_numeric(szse.iloc[0][c], errors='coerce') or 0)
            import numpy as _np
            for c in numeric_cols:
                if c not in new_row:
                    new_row[c] = _np.nan
            merged = pd.concat([merged, pd.DataFrame([new_row])], ignore_index=True)
        merged = merged.sort_values('date').reset_index(drop=True)

    if len(merged) == 0:
        return None
    logger.debug("两融汇总序列: %s 行, %s ~ %s，融资余额=%.3f万亿",
                 len(merged), merged['date'].iloc[0], merged['date'].iloc[-1],
                 float(pd.to_numeric(merged['rzye'], errors='coerce').iloc[-1] or 0) / 1e12)
    return merged

