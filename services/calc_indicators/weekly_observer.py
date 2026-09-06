#!/usr/bin/env python3
"""维度 D - 周线趋势背景观察哨（独立只读模块）。

模块性质：独立的信息面板（只读模式），不参与 A/B/C 维度的总分计算，
不触发自动止损或自动开仓。本模块只负责从周线数据接口取数 + 计算 + 显示。

数据层解耦：独立调用周线数据接口，与日线 DataFrame 完全隔离。
即便周线数据获取失败（如网络超时），也绝对不影响 A/B/C 的运行，
仅在该模块输出"周线数据获取失败，跳过检查"。

日志标识：所有日志打印均加 [D-周线观察] 前缀（emoji：🛰️），
与 A/B/C 的 ✅ ❌ 严格区分开，肉眼可一眼分辨。

5 项硬指标：
    1. 周线MA20趋势        向上 / 走平 / 向下
    2. 价格与周MA20关系    站上（多头区域） / 跌破（空头区域）
    3. 周线MA60（牛熊线）  价格距周MA60：+5.2%（上方） / -3.1%（下方）
    4. 周线RSI(14)         当前值：42.5（中性） / 超卖区（<30） / 超买区（>70）
    5. 周线MACD柱状体      绿柱连续缩短（第3根） / 红柱缩短（顶背离隐患）

综合评级（不带自动否决权，仅供人工参考）：
    A 多头趋势良好：MA20向上 + 价格站上MA20 + RSI 在 40-60 之间
    B 震荡/方向不明：MA20 走平，价格围绕 MA20 上下缠绕
    C 空头趋势压制：MA20 向下 且 价格在 MA20 之下
"""

from __future__ import annotations

import io
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from config import TradingConfig

logger = logging.getLogger(__name__)

# 进程内缓存：{(symbol, date_str): result_dict}
# 每天首次调用后复用，避免日内多次请求周线接口被禁 IP
_WEEKLY_CACHE: dict = {}


# ==================== 调试日志工具 ====================

def _debug(msg: str, enabled: bool = True) -> None:
    """打印调试日志到控制台（不受 Python logging 级别限制）。

    与 main.py（📋）/ market_filter.py（🌐）风格一致，
    使用 🛰️ 标记周线观察哨日志，便于在控制台中区分。
    """
    if enabled:
        print(f"  🛰️  {msg}")


def _suppress_output(func):
    """复用 main.py 的输出压制工具，避免 baostock 登录信息污染控制台。"""
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


# ==================== 周线数据获取 ====================

def _normalize_code(symbol: str) -> tuple:
    """将纯6位代码转换为 (ef_code, bs_code)。

    与 main.py 中同名函数保持一致，避免依赖 main 模块（解耦）。
    """
    code = symbol.strip()
    if len(code) != 6 or not code.isdigit():
        raise ValueError(f"股票代码格式错误: {code}，应为6位数字")
    if code.startswith(("6", "5", "9")):
        return code, f"sh.{code}"
    return code, f"sz.{code}"


def _fetch_weekly_efinance(ef_code: str, bars: int) -> Optional[pd.DataFrame]:
    """efinance 拉取周线数据（klt=102）。

    注意：efinance 周线日期标记为该周周一；东方财富后端对周线有返回上限
    （实测仅返回最近约 25 根），数据量常不足 MA60 所需，故仅作备选。
    """
    try:
        import efinance as ef
        df = None
        # 主用 klt=102（efinance 标准参数）；兼容个别旧版本可能用 ktype
        for kwargs in ({"klt": 102}, {"klt": "102"}, {"ktype": 102}, {"ktype": "W"}):
            try:
                df = ef.stock.get_quote_history(ef_code, **kwargs)
            except TypeError:
                continue
            except Exception:
                continue
            if df is not None and not df.empty:
                break
        if df is None or df.empty:
            return None
        col_map = {
            "日期": "date", "开盘": "open", "最高": "high",
            "最低": "low", "收盘": "close", "成交量": "volume",
        }
        df = df.rename(columns=col_map)
        df["date"] = pd.to_datetime(df["date"])
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"]).sort_values("date").tail(bars).reset_index(drop=True)
        if df.empty:
            return None
        return df[["date", "open", "high", "low", "close", "volume"]]
    except Exception as e:
        logger.debug("efinance 周线获取 %s 失败: %s", ef_code, e)
        return None


def _fetch_weekly_baostock(bs_code: str, bars: int) -> Optional[pd.DataFrame]:
    """baostock 拉取周线数据（frequency='w'）。

    注意：baostock 周线日期标记为该周最后交易日（通常周五）；只返回已完成周，
    本周未结束不返回，故最新周线滞后到上周。
    """
    try:
        import baostock as bs
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=bars * 7 + 60)).strftime("%Y-%m-%d")
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,open,high,low,close,volume",
            start_date=start_date,
            end_date=end_date,
            frequency="w",
            adjustflag="2",
        )
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=rs.fields)
        df["date"] = pd.to_datetime(df["date"])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"]).sort_values("date").tail(bars).reset_index(drop=True)
        if df.empty:
            return None
        return df[["date", "open", "high", "low", "close", "volume"]]
    except Exception as e:
        logger.debug("baostock 周线获取 %s 失败: %s", bs_code, e)
        return None


def _fetch_daily_efinance(ef_code: str, days: int) -> Optional[pd.DataFrame]:
    """efinance 拉取日线数据（klt=101），供聚合生成周线用。"""
    try:
        import efinance as ef
        df = ef.stock.get_quote_history(ef_code, klt=101)
        if df is None or df.empty:
            return None
        col_map = {
            "日期": "date", "开盘": "open", "最高": "high",
            "最低": "low", "收盘": "close", "成交量": "volume",
        }
        df = df.rename(columns=col_map)
        df["date"] = pd.to_datetime(df["date"])
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"]).sort_values("date").tail(days).reset_index(drop=True)
        if df.empty:
            return None
        return df[["date", "open", "high", "low", "close", "volume"]]
    except Exception as e:
        logger.debug("efinance 日线获取 %s 失败: %s", ef_code, e)
        return None


def _fetch_daily_baostock(bs_code: str, days: int) -> Optional[pd.DataFrame]:
    """baostock 拉取日线数据（frequency='d'），供聚合生成周线用。"""
    try:
        import baostock as bs
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,open,high,low,close,volume",
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag="2",
        )
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=rs.fields)
        df["date"] = pd.to_datetime(df["date"])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"]).sort_values("date").tail(days).reset_index(drop=True)
        if df.empty:
            return None
        return df[["date", "open", "high", "low", "close", "volume"]]
    except Exception as e:
        logger.debug("baostock 日线获取 %s 失败: %s", bs_code, e)
        return None


def _daily_to_weekly(daily: pd.DataFrame) -> pd.DataFrame:
    """日线聚合成周线（以周五为周末标记，date 取该周最后实际交易日）。

    用 pandas Period('W-FRI') 分组：周一~周五为一个交易周。
    本周未结束时仍会生成一个"进行中"的桶，date 取该桶内最新交易日，
    故本周进行中的周线会包含截至最新交易日的日线（含今日若数据源已更新）。
    """
    df = daily.copy()
    df["week"] = df["date"].dt.to_period("W-FRI")
    weekly = df.groupby("week").agg(
        date=("date", "last"),    # 该周最后实际交易日（本周进行中则为最新已发生日）
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).reset_index(drop=True)
    return weekly[["date", "open", "high", "low", "close", "volume"]]


def _fetch_weekly_data(symbol: str, config: TradingConfig) -> Optional[pd.DataFrame]:
    """获取周线数据：日线聚合为主 + 原生周线接口备选，带超时降级与数据量检查。

    设计理由：
        - efinance 原生周线（klt=102）东方财富后端仅返回最近约 25 根，不足 MA60 所需 60 根；
        - baostock 原生周线只返回已完成周，本周未结束不返回，最新滞后到上周；
        - 故改用日线聚合：日线历史完整（≥120 周），且本周进行中的周线会包含截至最新交易日的日线。
    与日线 fetch_data 完全隔离：独立代码路径、独立超时控制、独立异常捕获。
    失败时返回 None，绝不向 A/B/C 抛异常。
    """
    dbg = config.WEEKLY_DEBUG
    ef_code, bs_code = _normalize_code(symbol)
    bars = config.WEEKLY_FETCH_BARS
    timeout = config.WEEKLY_FETCH_TIMEOUT
    # 日线所需天数：bars 周 * 5 交易日/周 + 节假日缓冲 + 本周余量，再放大 1.5 倍确保充足
    need_days = int(bars * 5 * 1.5) + 30

    if dbg:
        _debug("══════ [D-周线观察] 周线数据获取排查 ══════", True)
        _debug(f"输入代码: {symbol}  →  efinance: {ef_code}  |  baostock: {bs_code}", True)
        _debug(f"目标周线根数: {bars}  |  需日线天数: ~{need_days}  |  单源超时: {timeout}s", True)

    def _with_timeout(fn, label):
        import signal as sig
        def _h(signum, frame):
            raise TimeoutError(f"{label} 超时")
        sig.signal(sig.SIGALRM, _h)
        sig.alarm(timeout)
        try:
            return fn()
        finally:
            sig.alarm(0)

    # —— 方案1（主）：efinance 日线 → 聚合周线 ——
    if dbg:
        _debug("─── 尝试 efinance 日线聚合周线 ───", True)
    try:
        daily = _suppress_output(lambda: _with_timeout(
            lambda: _fetch_daily_efinance(ef_code, need_days), "efinance 日线"))
        if daily is not None and not daily.empty:
            weekly = _daily_to_weekly(daily).tail(bars).reset_index(drop=True)
            if not weekly.empty:
                logger.info("[D-周线观察] 数据来源: efinance 日线聚合 (%s) %d 根周线", ef_code, len(weekly))
                if dbg:
                    _debug(f"✅ efinance 日线聚合成功: 日线 {len(daily)} 根 → 周线 {len(weekly)} 根", True)
                    _debug(f"   日期范围: {weekly['date'].iloc[0].date()} ~ {weekly['date'].iloc[-1].date()}", True)
                    _debug(f"   最新周收盘: {weekly['close'].iloc[-1]:.2f}（周线日期=该周最后交易日）", True)
                return weekly
        if dbg:
            _debug("❌ efinance 日线返回空", True)
    except Exception as e:
        if dbg:
            _debug(f"❌ efinance 日线失败: {type(e).__name__}: {e}", True)
        logger.info("[D-周线观察] efinance 日线获取失败: %s", e)

    # —— 方案2：baostock 日线 → 聚合周线 ——
    if dbg:
        _debug("─── 尝试 baostock 日线聚合周线 ───", True)
    try:
        def _bs_daily_fetch():
            import baostock as bs
            bs.login()
            try:
                return _fetch_daily_baostock(bs_code, need_days)
            finally:
                bs.logout()
        daily = _suppress_output(lambda: _with_timeout(_bs_daily_fetch, "baostock 日线"))
        if daily is not None and not daily.empty:
            weekly = _daily_to_weekly(daily).tail(bars).reset_index(drop=True)
            if not weekly.empty:
                logger.info("[D-周线观察] 数据来源: baostock 日线聚合 (%s) %d 根周线", bs_code, len(weekly))
                if dbg:
                    _debug(f"✅ baostock 日线聚合成功: 日线 {len(daily)} 根 → 周线 {len(weekly)} 根", True)
                    _debug(f"   日期范围: {weekly['date'].iloc[0].date()} ~ {weekly['date'].iloc[-1].date()}", True)
                    _debug(f"   最新周收盘: {weekly['close'].iloc[-1]:.2f}（周线日期=该周最后交易日）", True)
                return weekly
        if dbg:
            _debug("❌ baostock 日线返回空", True)
    except Exception as e:
        if dbg:
            _debug(f"❌ baostock 日线失败: {type(e).__name__}: {e}", True)
        logger.info("[D-周线观察] baostock 日线获取失败: %s", e)

    # —— 方案3（备）：efinance 原生周线 ——
    if dbg:
        _debug("─── 尝试 efinance 原生周线（备选） ───", True)
    try:
        df = _suppress_output(lambda: _with_timeout(
            lambda: _fetch_weekly_efinance(ef_code, bars), "efinance 周线"))
        if df is not None and not df.empty:
            logger.info("[D-周线观察] 数据来源: efinance 原生周线 (%s) %d 根", ef_code, len(df))
            if dbg:
                _debug(f"✅ efinance 原生周线成功: {len(df)} 根", True)
                _debug(f"   日期范围: {df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}", True)
                _debug(f"   ⚠️  efinance 周线常不足 60 根，MA60 计算可能数据不足", True)
            return df
        if dbg:
            _debug("❌ efinance 原生周线返回空", True)
    except Exception as e:
        if dbg:
            _debug(f"❌ efinance 原生周线失败: {type(e).__name__}: {e}", True)

    # —— 方案4（备）：baostock 原生周线 ——
    if dbg:
        _debug("─── 尝试 baostock 原生周线（备选） ───", True)
    try:
        def _bs_weekly_fetch():
            import baostock as bs
            bs.login()
            try:
                return _fetch_weekly_baostock(bs_code, bars)
            finally:
                bs.logout()
        df = _suppress_output(lambda: _with_timeout(_bs_weekly_fetch, "baostock 周线"))
        if df is not None and not df.empty:
            logger.info("[D-周线观察] 数据来源: baostock 原生周线 (%s) %d 根", bs_code, len(df))
            if dbg:
                _debug(f"✅ baostock 原生周线成功: {len(df)} 根", True)
                _debug(f"   日期范围: {df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}", True)
                _debug(f"   ⚠️  baostock 周线只含已完成周，最新滞后到上周", True)
            return df
        if dbg:
            _debug("❌ baostock 原生周线返回空", True)
    except Exception as e:
        if dbg:
            _debug(f"❌ baostock 原生周线失败: {type(e).__name__}: {e}", True)

    if dbg:
        _debug("❌ 周线所有数据源均失败，返回 None", True)
    return None


# ==================== 周线指标计算（独立实现，不依赖 main.py） ====================

def _calc_ma(close: pd.Series, window: int) -> pd.Series:
    return close.rolling(window=window).mean()


def _calc_ema(close: pd.Series, span: int) -> pd.Series:
    return close.ewm(span=span, adjust=False).mean()


def _calc_rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(window=window).mean()
    loss = -delta.clip(upper=0).rolling(window=window).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _calc_macd(close: pd.Series, fast: int, slow: int, signal: int):
    ema_fast = _calc_ema(close, fast)
    ema_slow = _calc_ema(close, slow)
    dif = ema_fast - ema_slow
    dea = _calc_ema(dif, signal)
    hist = (dif - dea) * 2
    return dif, dea, hist


# ==================== 5 项硬指标 ====================

def _fact_ma20_trend(close: pd.Series, config: TradingConfig) -> dict:
    """① 周线 MA20 趋势：对比本周 MA20 vs 上周 MA20。

    判定规则：环比变化幅度 |Δ%| < WEEKLY_MA_TREND_THRESHOLD(默认 0.1%) 视为走平。
    """
    ma = _calc_ma(close, config.WEEKLY_MA20_PERIOD)
    if len(ma) < 2 or pd.isna(ma.iloc[-1]) or pd.isna(ma.iloc[-2]):
        return {"label": "数据不足", "trend": "unknown",
                "curr_ma": None, "prev_ma": None, "change_pct": None}
    curr, prev = float(ma.iloc[-1]), float(ma.iloc[-2])
    change_pct = (curr / prev - 1) if prev > 0 else 0.0
    if change_pct > config.WEEKLY_MA_TREND_THRESHOLD:
        trend, label = "up", "向上"
    elif change_pct < -config.WEEKLY_MA_TREND_THRESHOLD:
        trend, label = "down", "向下"
    else:
        trend, label = "flat", "走平"
    return {"label": label, "trend": trend,
            "curr_ma": curr, "prev_ma": prev, "change_pct": change_pct}


def _fact_price_vs_ma20(close: pd.Series, config: TradingConfig) -> dict:
    """② 价格与周 MA20 关系：当前最新收盘价是否站上 MA20。"""
    ma = _calc_ma(close, config.WEEKLY_MA20_PERIOD)
    if pd.isna(ma.iloc[-1]):
        return {"label": "数据不足", "above": None,
                "price": float(close.iloc[-1]), "ma": None}
    price = float(close.iloc[-1])
    ma_val = float(ma.iloc[-1])
    above = price >= ma_val
    label = "站上（多头区域）" if above else "跌破（空头区域）"
    return {"label": label, "above": above, "price": price, "ma": ma_val}


def _fact_price_vs_ma60(close: pd.Series, config: TradingConfig) -> dict:
    """③ 周线 MA60（牛熊线）：当前价格与 MA60 的距离百分比。"""
    ma = _calc_ma(close, config.WEEKLY_MA60_PERIOD)
    if pd.isna(ma.iloc[-1]):
        return {"label": "数据不足", "distance_pct": None,
                "price": float(close.iloc[-1]), "ma60": None}
    price = float(close.iloc[-1])
    ma60 = float(ma.iloc[-1])
    if ma60 <= 0:
        return {"label": "数据异常", "distance_pct": None, "price": price, "ma60": ma60}
    distance_pct = (price / ma60 - 1) * 100
    direction = "上方" if distance_pct >= 0 else "下方"
    sign = "+" if distance_pct >= 0 else ""
    label = f"价格距周MA60：{sign}{distance_pct:.1f}%（{direction}）"
    return {"label": label, "distance_pct": distance_pct, "price": price, "ma60": ma60}


def _fact_rsi(close: pd.Series, config: TradingConfig) -> dict:
    """④ 周线 RSI(14)：判断极端情绪。"""
    rsi = _calc_rsi(close, config.WEEKLY_RSI_PERIOD)
    if pd.isna(rsi.iloc[-1]):
        return {"label": "数据不足", "value": None, "zone": "unknown", "zone_cn": "数据不足"}
    val = float(rsi.iloc[-1])
    if val < config.WEEKLY_RSI_OVERSOLD:
        zone, zone_cn = "oversold", "超卖区"
    elif val > config.WEEKLY_RSI_OVERBOUGHT:
        zone, zone_cn = "overbought", "超买区"
    else:
        zone, zone_cn = "neutral", "中性"
    label = f"当前值：{val:.1f}（{zone_cn}）"
    return {"label": label, "value": val, "zone": zone, "zone_cn": zone_cn}


def _fact_macd_hist(close: pd.Series, config: TradingConfig) -> dict:
    """⑤ 周线 MACD 柱状体：判断绿柱是否在缩短（止跌）或红柱是否在缩短（滞涨）。

    判定逻辑：
        - 当前 hist < 0（绿柱）：从最后一根向前数，统计连续 |hist| 递减的根数
        - 当前 hist > 0（红柱）：同理
        - 输出示例："绿柱连续缩短（第3根）" 表示当前为第 3 根连续缩短的绿柱
    """
    _, _, hist = _calc_macd(close, config.WEEKLY_MACD_FAST,
                            config.WEEKLY_MACD_SLOW, config.WEEKLY_MACD_SIGNAL)
    min_bars = config.WEEKLY_HIST_SHRINK_BARS
    if len(hist) < min_bars + 1 or pd.isna(hist.iloc[-1]):
        return {"label": "数据不足", "shrink": "none",
                "consecutive_bars": 0, "curr_hist": None}

    curr = float(hist.iloc[-1])
    shrink_bars = 0  # 不含最后一根的"前方连续缩短根数"

    if curr < 0:
        direction = "green"
        prev_abs = abs(curr)
        i = len(hist) - 2
        while i >= 0 and not pd.isna(hist.iloc[i]) and float(hist.iloc[i]) < 0:
            cur_abs = abs(float(hist.iloc[i]))
            if cur_abs > prev_abs:  # |hist| 在缩小 → 柱状体在缩短
                shrink_bars += 1
                prev_abs = cur_abs
                i -= 1
            else:
                break
        if shrink_bars >= 1:
            label = f"绿柱连续缩短（第{shrink_bars + 1}根）"
        else:
            label = f"绿柱未缩短（{curr:.4f}）"
    elif curr > 0:
        direction = "red"
        prev_abs = abs(curr)
        i = len(hist) - 2
        while i >= 0 and not pd.isna(hist.iloc[i]) and float(hist.iloc[i]) > 0:
            cur_abs = abs(float(hist.iloc[i]))
            if cur_abs > prev_abs:
                shrink_bars += 1
                prev_abs = cur_abs
                i -= 1
            else:
                break
        if shrink_bars >= 1:
            label = f"红柱缩短（第{shrink_bars + 1}根，顶背离隐患）"
        else:
            label = f"红柱未缩短（{curr:.4f}）"
    else:
        direction = "zero"
        label = f"MACD 柱状体接近零轴（{curr:.4f}）"

    return {
        "label": label,
        "shrink": direction,
        # "第N根" 含义：含当前根在内的连续缩短根数
        "consecutive_bars": shrink_bars + 1 if shrink_bars >= 1 else 0,
        "curr_hist": curr,
    }


# ==================== 周线综合评级 ====================

def _build_rating(facts: dict, config: TradingConfig) -> tuple:
    """基于 5 项事实给出综合评级（A/B/C），不带自动否决权。

    评级优先级（依次判定，先命中即返回）：
        C 空头趋势压制：MA20 向下 且 价格在 MA20 之下
        A 多头趋势良好：MA20 向上 + 价格站上 MA20 + RSI ∈ [40, 60]
        B 震荡/方向不明：其他情况（MA20 走平、价格缠绕、极端 RSI、反抽中等）

    Returns:
        (rating: str, advice: str)
        rating ∈ {"A", "B", "C", "unknown"}
    """
    ma20_trend = facts["ma20_trend"]["trend"]
    price_above = facts["price_vs_ma20"]["above"]
    rsi_val = facts["rsi"]["value"]

    # 数据不足保护
    if ma20_trend == "unknown" or price_above is None or rsi_val is None:
        return "unknown", "周线关键指标数据不足，无法给出评级"

    # 评级 C：空头趋势压制
    if ma20_trend == "down" and price_above is False:
        return "C", "周线逆风，当前日线超卖仅为反弹，强烈建议放弃入场，或仅用极小仓位试盘。"

    # 评级 A：多头趋势良好
    if (ma20_trend == "up" and price_above is True
            and config.WEEKLY_RSI_NEUTRAL_LOW <= rsi_val <= config.WEEKLY_RSI_NEUTRAL_HIGH):
        return "A", "周线顺风，日线买点可信度高，可考虑正常仓位介入。"

    # 评级 B：震荡/方向不明
    return "B", "周线无明确方向，建议降低仓位，严格按日线止损执行。"


# ==================== 主入口 ====================

def check_weekly_background(symbol: str, config: TradingConfig) -> dict:
    """维度D - 周线趋势背景观察哨主入口。

    独立拉取最近 120 根周K线，计算 5 项硬指标 + 综合评级。
    完全不影响 A/B/C 维度的任何变量与流程。

    Args:
        symbol: 6 位股票代码
        config: TradingConfig 实例

    Returns:
        dict: {
            'available': bool,    # True=数据获取成功并完成计算
            'symbol': str,
            'rating': str|None,   # 'A' / 'B' / 'C' / 'unknown' / None
            'advice': str,        # 人工参考建议
            'facts': dict,        # 5 项硬指标详情
            'bars_used': int,
            'error': str|None,
            'from_cache': bool,
        }
    """
    dbg = config.WEEKLY_DEBUG

    # —— 前置开关 ——
    if not config.ENABLE_WEEKLY_OBSERVER:
        return {
            "available": False, "symbol": symbol, "rating": None,
            "advice": "维度D已关闭 (ENABLE_WEEKLY_OBSERVER=False)",
            "facts": {}, "bars_used": 0, "error": "disabled",
            "from_cache": False,
        }

    # —— 每日 1 次缓存（首次调用后当天复用）——
    today_key = datetime.now().strftime("%Y-%m-%d")
    cache_key = (symbol, today_key)
    if config.WEEKLY_CACHE_ENABLED and cache_key in _WEEKLY_CACHE:
        if dbg:
            _debug(f"══════ [D-周线观察] 命中每日缓存 ({today_key})，跳过周线接口调用 ══════", True)
        cached = dict(_WEEKLY_CACHE[cache_key])
        cached["from_cache"] = True
        return cached

    if dbg:
        _debug("══════ [D-周线观察] 周线趋势背景排查 ══════", True)
        _debug(f"目标股票: {symbol}  |  周K线根数: {config.WEEKLY_FETCH_BARS}", True)
        _debug(f"MA20: {config.WEEKLY_MA20_PERIOD}  |  MA60(牛熊线): {config.WEEKLY_MA60_PERIOD}  |  RSI: {config.WEEKLY_RSI_PERIOD}", True)
        _debug(f"MACD: {config.WEEKLY_MACD_FAST}/{config.WEEKLY_MACD_SLOW}/{config.WEEKLY_MACD_SIGNAL}  |  缓存: {config.WEEKLY_CACHE_ENABLED}", True)

    # —— 数据获取（与日线完全隔离）——
    df = _fetch_weekly_data(symbol, config)

    # —— 失败：仅输出不可用，绝不影响 A/B/C ——
    if df is None or df.empty:
        msg = f"周线数据获取失败，跳过检查（股票 {symbol}）"
        if dbg:
            _debug(f"❌ {msg}", True)
            _debug("══════ [D-周线观察] 排查结束 ❌ 数据暂不可用 ══════", True)
        logger.info("[D-周线观察] %s", msg)
        # 失败结果不写入缓存，下次调用仍会重试
        return {
            "available": False, "symbol": symbol, "rating": None,
            "advice": "数据暂不可用", "facts": {}, "bars_used": 0,
            "error": msg, "from_cache": False,
        }

    # —— 数据充足性 ——
    min_len = max(config.WEEKLY_MA60_PERIOD,
                  config.WEEKLY_MACD_SLOW + config.WEEKLY_MACD_SIGNAL) + 5
    if len(df) < min_len:
        msg = f"周线数据不足：仅 {len(df)} 根，需 ≥ {min_len}"
        if dbg:
            _debug(f"❌ {msg}", True)
            _debug("══════ [D-周线观察] 排查结束 ❌ 数据不足 ══════", True)
        logger.info("[D-周线观察] %s", msg)
        return {
            "available": False, "symbol": symbol, "rating": None,
            "advice": "数据暂不可用", "facts": {}, "bars_used": len(df),
            "error": msg, "from_cache": False,
        }

    close = df["close"]

    # —— 5 项硬指标 ——
    if dbg:
        _debug("─── 5 项硬指标计算 ───", True)

    facts = {
        "ma20_trend": _fact_ma20_trend(close, config),
        "price_vs_ma20": _fact_price_vs_ma20(close, config),
        "price_vs_ma60": _fact_price_vs_ma60(close, config),
        "rsi": _fact_rsi(close, config),
        "macd_hist": _fact_macd_hist(close, config),
    }

    if dbg:
        f1 = facts["ma20_trend"]
        _debug(f"  ① 周线MA20趋势: {f1['label']}"
               + (f" (cur={f1.get('curr_ma'):.3f}, prev={f1.get('prev_ma'):.3f})"
                  if f1.get('curr_ma') is not None else ""), True)
        _debug(f"  ② 价格与MA20关系: {facts['price_vs_ma20']['label']}", True)
        _debug(f"  ③ {facts['price_vs_ma60']['label']}", True)
        _debug(f"  ④ 周线RSI: {facts['rsi']['label']}", True)
        _debug(f"  ⑤ 周线MACD柱状体: {facts['macd_hist']['label']}", True)

    # —— 综合评级 ——
    rating, advice = _build_rating(facts, config)
    if dbg:
        _debug("─── 周线综合评级 ───", True)
        _debug(f"  评级: {rating}", True)
        _debug(f"  建议: {advice}", True)
        _debug("══════ [D-周线观察] 排查结束 ══════", True)

    result = {
        "available": True, "symbol": symbol, "rating": rating,
        "advice": advice, "facts": facts, "bars_used": len(df),
        "error": None, "from_cache": False,
    }

    # —— 写入每日缓存（仅成功结果）——
    if config.WEEKLY_CACHE_ENABLED:
        _WEEKLY_CACHE[cache_key] = dict(result)
        # 清理过期缓存避免内存膨胀（保留当前 key）
        if len(_WEEKLY_CACHE) > 50:
            _WEEKLY_CACHE.clear()
            _WEEKLY_CACHE[cache_key] = dict(result)

    logger.info("[D-周线观察] %s 评级=%s bars=%d", symbol, rating, len(df))
    return result


def print_weekly_result(result: dict) -> None:
    """打印维度D结果到控制台（独立区块，与 A/B/C 输出严格分隔）。"""
    print("【维度D - 周线趋势背景（独立观察哨）】")
    if not result.get("available"):
        err = result.get("error") or "数据暂不可用"
        if err == "disabled":
            print("  ⏭️  维度D已关闭，跳过周线检查")
        else:
            print(f"  ⚠️  周线数据暂不可用：{err}")
        print()
        return

    facts = result.get("facts", {})
    if result.get("from_cache"):
        print("  🛰️  来源: 每日缓存（日内首次已计算，未重复请求接口）")
    print(f"  📊 周K线根数: {result.get('bars_used', 0)}")
    print(f"  ① 周线MA20趋势: {facts.get('ma20_trend', {}).get('label', '-')}")
    print(f"  ② 价格与周MA20关系: {facts.get('price_vs_ma20', {}).get('label', '-')}")
    print(f"  ③ {facts.get('price_vs_ma60', {}).get('label', '-')}")
    print(f"  ④ 周线RSI: {facts.get('rsi', {}).get('label', '-')}")
    print(f"  ⑤ 周线MACD柱状体: {facts.get('macd_hist', {}).get('label', '-')}")
    print()
    rating = result.get("rating", "unknown")
    advice = result.get("advice", "")
    rating_label = {
        "A": "多头趋势良好", "B": "震荡/方向不明",
        "C": "空头趋势压制", "unknown": "数据不足",
    }.get(rating, rating)
    print(f"  🎯 周线综合评级: {rating}（{rating_label}）")
    print(f"  💡 给人工的建议: {advice}")
    print()
