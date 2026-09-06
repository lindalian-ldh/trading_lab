#!/usr/bin/env python3
"""开仓前三维度快速筛查工具。

单文件脚本：输入股票代码，输出结构/动量/赔率三维度通过状态。
所有参数通过 config.py 中的 TradingConfig 控制，零硬编码。
"""

import io
import logging
import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from config import get_config, list_profiles, TradingConfig, merge_profiles, apply_overrides
from market_filter import check_market_environment

# 维度D（周线观察哨）独立模块：即便模块加载失败也绝对不影响 A/B/C
try:
    from weekly_observer import check_weekly_background, print_weekly_result
    _WEEKLY_AVAILABLE = True
    _WEEKLY_IMPORT_ERROR: str | None = None
except Exception as _e:  # pragma: no cover - 容灾兜底
    _WEEKLY_AVAILABLE = False
    _WEEKLY_IMPORT_ERROR = str(_e)

    def check_weekly_background(*_args, **_kwargs):  # type: ignore
        return {"available": False, "error": f"模块加载失败: {_WEEKLY_IMPORT_ERROR}",
                "rating": None, "advice": "", "facts": {}}

    def print_weekly_result(*_args, **_kwargs) -> None:  # type: ignore
        pass

logger = logging.getLogger(__name__)


# ==================== 输出压制工具 ====================

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


# ==================== 数据获取层 ====================

def _normalize_code(symbol: str) -> tuple:
    """将纯6位代码转换为 (ef_code, bs_code)。"""
    code = symbol.strip()
    if len(code) != 6 or not code.isdigit():
        raise ValueError(f"股票代码格式错误: {code}，应为6位数字")
    if code.startswith(("6", "5", "9")):
        return code, f"sh.{code}"
    return code, f"sz.{code}"


def _normalize_ef(df: pd.DataFrame, config: TradingConfig) -> pd.DataFrame:
    """标准化 efinance 输出 → {date, open, high, low, close, volume}。"""
    col_map = {
        "日期": "date", "开盘": "open", "最高": "high",
        "最低": "low", "收盘": "close", "成交量": "volume",
    }
    df = df.rename(columns=col_map)
    df["date"] = pd.to_datetime(df["date"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values("date").tail(config.DATA_TRADING_DAYS).reset_index(drop=True)
    return df[["date", "open", "high", "low", "close", "volume"]]


def _normalize_bs(df: pd.DataFrame, config: TradingConfig) -> pd.DataFrame:
    """标准化 baostock 输出 → {date, open, high, low, close, volume}。"""
    df["date"] = pd.to_datetime(df["date"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values("date").tail(config.DATA_TRADING_DAYS).reset_index(drop=True)
    return df[["date", "open", "high", "low", "close", "volume"]]


def fetch_data(symbol: str, config: TradingConfig) -> pd.DataFrame:
    """获取近 DATA_TRADING_DAYS 个交易日的日线数据。

    尝试顺序：efinance → baostock。均失败则友好提示并退出。
    """
    ef_code, bs_code = _normalize_code(symbol)
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=240)).strftime("%Y-%m-%d")

    # —— 尝试 efinance ——
    def _try_efinance():
        try:
            import efinance as ef
            return ef.stock.get_quote_history(ef_code)
        except Exception:
            return None

    df = _suppress_output(_try_efinance)
    if df is not None and not df.empty:
        return _normalize_ef(df, config)

    # —— 尝试 baostock ——
    def _try_baostock():
        try:
            import baostock as bs
            bs.login()
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
            bs.logout()
            if rows:
                return pd.DataFrame(rows, columns=rs.fields)
        except Exception:
            pass
        return None

    df = _suppress_output(_try_baostock)
    if df is not None and not df.empty:
        return _normalize_bs(df, config)

    print(f"⚠️  无法获取 {symbol} 的行情数据，请检查代码或网络连接")
    sys.exit(1)


# ==================== 技术指标计算 ====================

def calc_ma(close: pd.Series, window: int) -> pd.Series:
    return close.rolling(window=window).mean()


def calc_ema(close: pd.Series, span: int) -> pd.Series:
    return close.ewm(span=span, adjust=False).mean()


def calc_rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(window=window).mean()
    loss = -delta.clip(upper=0).rolling(window=window).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = calc_ema(close, fast)
    ema_slow = calc_ema(close, slow)
    dif = ema_fast - ema_slow
    dea = calc_ema(dif, signal)
    hist = (dif - dea) * 2
    return dif, dea, hist


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """计算 Average True Range。"""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


# ==================== 维度判定 ====================

def _check_bullish_candle(open_p: float, high: float, low: float, close: float) -> bool:
    """判断 K 线是否为阳线或锤子线（止跌形态）。"""
    if close > open_p:
        return True
    body = abs(close - open_p)
    lower_shadow = min(open_p, close) - low
    if lower_shadow > body * 2:
        return True
    return False


def _debug(msg: str, enabled: bool = True) -> None:
    """打印调试日志到控制台（不受 Python logging 级别限制）。"""
    if enabled:
        print(f"  📋 {msg}")


def check_pullback_signal(df: pd.DataFrame, config: TradingConfig) -> dict:
    """检查是否符合短线回踩条件。

    依次检查：偏离度 → 均线方向 → 缩量 → K 线形态 → 确认窗口。
    任一均线周期通过全部检查即返回 True。

    Returns:
        dict: {
            'is_pullback': bool,
            'ma_name': str | None,
            'deviation': str,
            'details': str,
            'matched_conditions': list[str],
        }
    """
    dbg = config.PULLBACK_DEBUG
    close = df["close"]
    high = df["high"]
    low = df["low"]
    open_p = df["open"]
    volume = df["volume"]

    max_period = max(config.PULLBACK_MA_TARGETS)
    min_len = max_period + config.PULLBACK_CONFIRM_BARS + 10

    logger.info("[回踩排查] 开始 | 目标均线=%s 偏离阈值=%.2f%% 均线向上=%s 缩量=%s(阈值%s) K线形态=%s 确认窗口=%s根",
                config.PULLBACK_MA_TARGETS,
                config.PULLBACK_MAX_DEVIATION * 100,
                config.PULLBACK_REQUIRE_MA_UP,
                config.PULLBACK_REQUIRE_SHRINK_VOLUME,
                config.PULLBACK_VOLUME_SHRINK_RATIO,
                config.PULLBACK_REQUIRE_BULLISH_CANDLE,
                config.PULLBACK_CONFIRM_BARS)

    if dbg:
        _debug("══════ 回踩信号排查 ══════", True)
        _debug(f"目标均线: {config.PULLBACK_MA_TARGETS}", True)
        _debug(f"偏离阈值: {config.PULLBACK_MAX_DEVIATION * 100:.2f}%  |  均线向上: {config.PULLBACK_REQUIRE_MA_UP}", True)
        _debug(f"缩量要求: {config.PULLBACK_REQUIRE_SHRINK_VOLUME}  阈值: {config.PULLBACK_VOLUME_SHRINK_RATIO}", True)
        _debug(f"K线形态: {config.PULLBACK_REQUIRE_BULLISH_CANDLE}  |  确认窗口: {config.PULLBACK_CONFIRM_BARS} 根", True)
        _debug(f"K线数据: {len(df)} 根 (需 ≥ {min_len})", True)

    if len(df) < min_len:
        logger.info("[回踩排查] 数据不足: 实际=%d 需≥%d → 不通过", len(df), min_len)
        if dbg:
            _debug(f"❌ 数据不足: {len(df)} < {min_len}", True)
            _debug("══════ 排查结束（数据不足） ══════", True)
        return {
            "is_pullback": False,
            "ma_name": None,
            "deviation": "0.00%",
            "details": f"数据不足（需至少 {min_len} 根K线）",
            "matched_conditions": [],
        }

    cur_close = close.iloc[-1]
    cur_volume = volume.iloc[-1]
    vol_ma = calc_ma(volume, config.VOLUME_MA_PERIOD)
    avg_vol = vol_ma.iloc[-1]
    vol_ratio = cur_volume / avg_vol if avg_vol > 0 else float("inf")

    logger.info("[回踩排查] 行情快照: 最新价=%.2f 成交量=%s 5日均量=%s 量比=%.2f",
                cur_close, f"{cur_volume:,.0f}", f"{avg_vol:,.0f}", vol_ratio)

    if dbg:
        _debug(f"最新价: {cur_close:.2f}  |  成交量: {cur_volume:,.0f}  |  5日均量: {avg_vol:,.0f}  |  量比: {vol_ratio:.2f}", True)

    final_results: list[str] = []

    for period in config.PULLBACK_MA_TARGETS:
        ma_series = calc_ma(close, period)
        cur_ma = ma_series.iloc[-1]
        prev_ma = ma_series.iloc[-2]

        deviation = abs(cur_close - cur_ma) / cur_ma
        deviation_pct = deviation * 100
        ma_up = cur_ma > prev_ma
        bullish = _check_bullish_candle(open_p.iloc[-1], high.iloc[-1], low.iloc[-1], close.iloc[-1])

        logger.info("[回踩排查] MA%d 计算值: 当前MA=%.3f 前一日MA=%.3f 偏离=%.3f%% 均线向上=%s K线止跌=%s",
                    period, cur_ma, prev_ma, deviation_pct, ma_up, bullish)

        if dbg:
            _debug(f"─── MA{period} ───", True)
            _debug(f"  MA{period}值: {cur_ma:.3f}  |  前一日: {prev_ma:.3f}  |  偏离: {deviation_pct:.3f}%", True)

        # ① 偏离度检查
        dev_ok = deviation <= config.PULLBACK_MAX_DEVIATION
        logger.info("[回踩排查] MA%d ①偏离度: 偏离=%.3f%% 阈值=%.2f%% → %s",
                    period, deviation_pct, config.PULLBACK_MAX_DEVIATION * 100,
                    "通过" if dev_ok else "不通过(跳过)")
        if dbg:
            mark = "✅" if dev_ok else "❌"
            _debug(f"  {mark} 偏离度检查: {deviation_pct:.3f}% {'≤' if dev_ok else '>' } 阈值 {config.PULLBACK_MAX_DEVIATION * 100:.2f}%", True)
        if not dev_ok:
            final_results.append(f"MA{period}: 偏离 {deviation_pct:.3f}% > 阈值 {config.PULLBACK_MAX_DEVIATION * 100:.2f}%")
            continue

        # ② 均线方向检查
        if config.PULLBACK_REQUIRE_MA_UP:
            logger.info("[回踩排查] MA%d ②均线方向: 当前=%.3f 前一日=%.3f 向上=%s → %s",
                        period, cur_ma, prev_ma, ma_up,
                        "通过" if ma_up else "不通过(跳过)")
            if dbg:
                mark = "✅" if ma_up else "❌"
                _debug(f"  {mark} 均线方向: {'向上' if ma_up else '向下/走平'} (cur={cur_ma:.3f}, prev={prev_ma:.3f})", True)
            if not ma_up:
                final_results.append(f"MA{period}: 均线未向上 (cur={cur_ma:.3f}, prev={prev_ma:.3f})")
                continue
        else:
            logger.info("[回踩排查] MA%d ②均线方向: 已关闭检查(PULLBACK_REQUIRE_MA_UP=False) → 跳过", period)
            if dbg:
                _debug(f"  ➖ 均线方向: 不要求 (PULLBACK_REQUIRE_MA_UP=False)", True)

        # ③ 缩量检查
        if config.PULLBACK_REQUIRE_SHRINK_VOLUME:
            vol_ok = vol_ratio < config.PULLBACK_VOLUME_SHRINK_RATIO
            logger.info("[回踩排查] MA%d ③缩量: 量比=%.2f 阈值=%s → %s",
                        period, vol_ratio, config.PULLBACK_VOLUME_SHRINK_RATIO,
                        "通过" if vol_ok else "不通过(跳过)")
            if dbg:
                mark = "✅" if vol_ok else "❌"
                _debug(f"  {mark} 缩量检查: 量比 {vol_ratio:.2f} {'<' if vol_ok else '≥'} 阈值 {config.PULLBACK_VOLUME_SHRINK_RATIO}", True)
            if not vol_ok:
                final_results.append(f"MA{period}: 量比 {vol_ratio:.2f} ≥ 阈值 {config.PULLBACK_VOLUME_SHRINK_RATIO}")
                continue
        else:
            logger.info("[回踩排查] MA%d ③缩量: 已关闭检查(PULLBACK_REQUIRE_SHRINK_VOLUME=False) → 跳过", period)
            if dbg:
                _debug(f"  ➖ 缩量检查: 不要求 (PULLBACK_REQUIRE_SHRINK_VOLUME=False)", True)

        # ④ K 线形态检查
        if config.PULLBACK_REQUIRE_BULLISH_CANDLE:
            logger.info("[回踩排查] MA%d ④K线形态: open=%.2f close=%.2f high=%.2f low=%.2f 阳线/锤子线=%s → %s",
                        period, open_p.iloc[-1], close.iloc[-1], high.iloc[-1], low.iloc[-1], bullish,
                        "通过" if bullish else "不通过(跳过)")
            if dbg:
                mark = "✅" if bullish else "❌"
                _debug(f"  {mark} K线形态: {'阳线/锤子线 ✓' if bullish else '非阳线/锤子线 ✗'} (open={open_p.iloc[-1]:.2f}, close={close.iloc[-1]:.2f}, high={high.iloc[-1]:.2f}, low={low.iloc[-1]:.2f})", True)
                if not bullish:
                    body = abs(close.iloc[-1] - open_p.iloc[-1])
                    lower_shadow = min(open_p.iloc[-1], close.iloc[-1]) - low.iloc[-1]
                    _debug(f"     实体={body:.3f}, 下影线={lower_shadow:.3f}, 下影>2×实体={lower_shadow > body * 2}", True)
            if not bullish:
                final_results.append(f"MA{period}: K线非阳线/锤子线 (open={open_p.iloc[-1]:.2f}, close={close.iloc[-1]:.2f})")
                continue
        else:
            logger.info("[回踩排查] MA%d ④K线形态: 已关闭检查(PULLBACK_REQUIRE_BULLISH_CANDLE=False) → 跳过", period)
            if dbg:
                _debug(f"  ➖ K线形态: 不要求 (PULLBACK_REQUIRE_BULLISH_CANDLE=False)", True)

        # ⑤ 确认窗口检查
        window_ok = False
        window_detail: list[str] = []
        for i in range(1, config.PULLBACK_CONFIRM_BARS + 1):
            if len(df) < i + 1:
                break
            past_close = close.iloc[-(i + 1)]
            past_ma = ma_series.iloc[-(i + 1)]
            if past_ma > 0:
                past_dev = abs(past_close - past_ma) / past_ma * 100
                window_detail.append(f"    第-{i}根: close={past_close:.2f}, MA={past_ma:.3f}, 偏离={past_dev:.3f}%")
                if past_dev <= config.PULLBACK_MAX_DEVIATION * 100:
                    window_ok = True
                    logger.info("[回踩排查] MA%d ⑤确认窗口: 第-%d根 偏离=%.3f%% ≤ 阈值 → 通过",
                                period, i, past_dev)
                    if dbg:
                        _debug(f"  ✅ 确认窗口: 第-{i}根K线回踩确认 (偏离 {past_dev:.3f}%)", True)
                    break
                else:
                    logger.info("[回踩排查] MA%d ⑤确认窗口: 第-%d根 偏离=%.3f%% > 阈值 → 继续检查",
                                period, i, past_dev)
                    if dbg:
                        _debug(f"  ❌ 确认窗口: 第-{i}根 偏离 {past_dev:.3f}% > 阈值", True)
        if not window_ok:
            logger.info("[回踩排查] MA%d ⑤确认窗口: 最近%d根内无回踩动作 → 不通过(跳过)",
                        period, config.PULLBACK_CONFIRM_BARS)
            final_results.append(f"MA{period}: 最近{config.PULLBACK_CONFIRM_BARS}根内无回踩动作")
            continue

        # ── 全部条件通过 ──
        matched: list[str] = []
        matched.append(f"偏离度 {deviation_pct:.2f}% ≤ {config.PULLBACK_MAX_DEVIATION * 100:.1f}%")
        if config.PULLBACK_REQUIRE_MA_UP:
            matched.append("均线方向向上")
        if config.PULLBACK_REQUIRE_SHRINK_VOLUME:
            matched.append(f"缩量 (量比 {vol_ratio:.2f} < {config.PULLBACK_VOLUME_SHRINK_RATIO})")
        if config.PULLBACK_REQUIRE_BULLISH_CANDLE:
            matched.append("阳线或锤子线")
        matched.append(f"最近{config.PULLBACK_CONFIRM_BARS}根K线内确认")

        ma_name = f"MA{period}"
        logger.info("[回踩排查] MA%d ✅ 全部条件通过！触发回踩信号 偏离=%.2f%% 命中条件=[%s]",
                    period, deviation_pct, ", ".join(matched))
        if dbg:
            _debug(f"  ✅✅ 全部条件通过！触发 {ma_name} 回踩信号", True)
            _debug(f"     命中条件: {', '.join(matched)}", True)
            _debug("══════ 排查结束 ✅ 回踩信号触发 ══════", True)
        return {
            "is_pullback": True,
            "ma_name": ma_name,
            "deviation": f"{deviation_pct:.2f}%",
            "details": f"回踩{ma_name} (偏离 {deviation_pct:.2f}%)，条件: {', '.join(matched)}",
            "matched_conditions": matched,
        }

    logger.info("[回踩排查] ❌ 所有均线均未通过 共检查%d条 淘汰原因: %s",
                len(config.PULLBACK_MA_TARGETS), " | ".join(final_results))
    if dbg:
        _debug(f"❌ 所有均线均未通过，共检查 {len(config.PULLBACK_MA_TARGETS)} 条均线", True)
        for r in final_results:
            _debug(f"   淘汰原因: {r}", True)
        _debug("══════ 排查结束 ❌ 无回踩信号 ══════", True)

    return {
        "is_pullback": False,
        "ma_name": None,
        "deviation": "0.00%",
        "details": f"未触发任何均线回踩（检查了 {config.PULLBACK_MA_TARGETS}）",
        "matched_conditions": [],
    }


def check_dimension_a(df: pd.DataFrame, config: TradingConfig) -> tuple:
    """结构维度：突破前 N 日高点 或 回踩（短线增强 / 长线兼容）。"""
    close = df["close"]
    high = df["high"]

    for p in config.MA_PERIODS:
        calc_ma(close, p)

    prev_high = high.shift(1).rolling(config.BREAKOUT_WINDOW).max()
    cur_close = close.iloc[-1]
    cur_prev_high = prev_high.iloc[-1]

    breakout = cur_close > cur_prev_high

    if breakout:
        return True, f"突破前{config.BREAKOUT_WINDOW}日高点 {cur_prev_high:.2f}"

    # —— 回踩判定：增强模式 or 兼容模式 ——
    if config.USE_PULLBACK_ENHANCE:
        result = check_pullback_signal(df, config)
        if result["is_pullback"]:
            return True, result["details"]
        return False, f"无突破或回踩信号（短线模式：{result['details']}）"
    else:
        ma20 = calc_ma(close, config.MA_PERIODS[2])
        cur_ma20 = ma20.iloc[-1]
        pullback = abs(cur_close - cur_ma20) / cur_ma20 < config.PULLBACK_THRESHOLD
        if pullback:
            deviation = abs(cur_close - cur_ma20) / cur_ma20 * 100
            return True, f"回踩MA{config.MA_PERIODS[2]} (偏离 {deviation:.2f}%)"
        return False, "无突破或回踩信号"


def check_dimension_b(df: pd.DataFrame, config: TradingConfig) -> tuple:
    """动量维度：大盘环境前置判断 + MACD 金叉/柱线翻红 或 RSI 超卖 或 放量。

    大盘检查行为：
        - ENABLE_MARKET_FILTER = True：
          • 大盘不通过 → 仍然运行个股 MACD/RSI/放量检查，让用户看到完整动量状态。
          • 但 passed 最终按大盘结果打标：若 MACD/RSI/放量有信号，会在 reason 中明确标注
            "大盘不通过，个股虽有信号仍建议观望"。
        - ENABLE_MARKET_FILTER = False：
          • 完全跳过大盘判断，仅按个股 MACD/RSI/放量结果决定 passed。

    非侵入式：原有 MACD/RSI/放量逻辑完全保持不变，所有个股动量信号都被计算并输出。
    """
    dbg = config.MOMENTUM_DEBUG

    # —— 大盘环境判断（仅输出到 signals，不做硬拦截）——
    market_reason: str | None = None
    market_passed: bool = True
    if config.ENABLE_MARKET_FILTER:
        market = check_market_environment(config)
        market_passed = market["passed"]
        market_reason = market["reason"]
    # 过滤关闭时 market_passed 保持 True（不影响 passed）

    close = df["close"]
    volume = df["volume"]

    dif, dea, hist = calc_macd(close, config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL)
    rsi = calc_rsi(close, config.RSI_WINDOW)

    logger.info("[动量排查] 开始 | 大盘过滤=%s MACD=%d/%d/%d RSI窗口=%d 超卖=%.1f 超买=%.1f 放量倍数=%.2f",
                config.ENABLE_MARKET_FILTER,
                config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL,
                config.RSI_WINDOW, config.RSI_OVERSOLD, config.RSI_OVERBOUGHT,
                config.VOLUME_SURGE_RATIO)

    if dbg:
        _debug("══════ 动量排查 ══════", True)
        _debug(f"大盘过滤: {'开启' if config.ENABLE_MARKET_FILTER else '关闭'}  |  MACD: {config.MACD_FAST}/{config.MACD_SLOW}/{config.MACD_SIGNAL}", True)
        _debug(f"RSI窗口: {config.RSI_WINDOW}  超卖: {config.RSI_OVERSOLD}  超买: {config.RSI_OVERBOUGHT}", True)
        _debug(f"放量阈值: {config.VOLUME_SURGE_RATIO} 倍 (基于 {config.VOLUME_MA_PERIOD} 日均量)", True)
        _debug(f"K线数据: {len(df)} 根", True)

    signals: list[str] = []
    stock_passed = False

    # —— 大盘信息始终显示（开过滤时）——
    if config.ENABLE_MARKET_FILTER:
        if market_passed:
            signals.append(f"✅ 大盘通过：{market_reason}")
            logger.info("[动量排查] 大盘环境: 通过 (%s)", market_reason)
            if dbg:
                _debug(f"✅ 大盘环境: 通过 - {market_reason}", True)
        else:
            signals.append(f"⚠️  大盘环境警告：{market_reason}（个股动量继续检查中）")
            logger.info("[动量排查] 大盘环境: 不通过 (%s) - 个股动量继续检查", market_reason)
            if dbg:
                _debug(f"⚠️  大盘环境警告: {market_reason}（个股动量继续检查中）", True)
    else:
        logger.info("[动量排查] 大盘过滤已关闭 (ENABLE_MARKET_FILTER=False)，跳过大盘判断")
        if dbg:
            _debug("➖ 大盘过滤: 已关闭 (ENABLE_MARKET_FILTER=False)，跳过大盘判断", True)

    # —— MACD 金叉 / 柱线翻红 ——
    macd_cross = False
    macd_red = False
    if len(dif) >= 2:
        prev_diff = dif.iloc[-2] - dea.iloc[-2]
        curr_diff = dif.iloc[-1] - dea.iloc[-1]
        macd_cross = prev_diff <= 0 and curr_diff > 0
        logger.info("[动量排查] MACD金叉: 前日DIF-DEA=%.4f 今日DIF-DEA=%.4f (DIF=%.4f DEA=%.4f) → %s",
                    prev_diff, curr_diff, dif.iloc[-1], dea.iloc[-1],
                    "通过" if macd_cross else "不通过")
        if dbg:
            mark = "✅" if macd_cross else "❌"
            _debug(f"─── MACD金叉 ───", True)
            _debug(f"  {mark} 前日DIF-DEA={prev_diff:.4f}  今日DIF-DEA={curr_diff:.4f}", True)
            _debug(f"     DIF={dif.iloc[-1]:.4f}  DEA={dea.iloc[-1]:.4f}", True)
        if macd_cross:
            signals.append("MACD金叉")
            stock_passed = True

    # —— MACD 柱线翻红 ——
    if len(hist) >= 2:
        prev_hist = hist.iloc[-2]
        curr_hist = hist.iloc[-1]
        macd_red = prev_hist <= 0 and curr_hist > 0
        logger.info("[动量排查] MACD柱线翻红: 前日hist=%.4f 今日hist=%.4f → %s",
                    prev_hist, curr_hist,
                    "通过" if macd_red else "不通过")
        if dbg:
            mark = "✅" if macd_red else "❌"
            _debug(f"─── MACD柱线翻红 ───", True)
            _debug(f"  {mark} 前日hist={prev_hist:.4f}  今日hist={curr_hist:.4f}", True)
        if macd_red:
            signals.append("MACD柱线翻红")
            stock_passed = True

    # —— RSI ——
    cur_rsi = rsi.iloc[-1]
    rsi_tag = "超卖" if cur_rsi < config.RSI_OVERSOLD else ("超买" if cur_rsi >= config.RSI_OVERBOUGHT else "中性")
    logger.info("[动量排查] RSI: 当前=%.2f 超卖阈值=%.1f 超买阈值=%.1f → %s",
                cur_rsi, config.RSI_OVERSOLD, config.RSI_OVERBOUGHT, rsi_tag)
    if dbg:
        mark = "✅" if rsi_tag == "超卖" else ("⚠️" if rsi_tag == "超买" else "➖")
        _debug(f"─── RSI ───", True)
        _debug(f"  {mark} 当前RSI={cur_rsi:.2f}  超卖<{config.RSI_OVERSOLD}  超买≥{config.RSI_OVERBOUGHT}  → {rsi_tag}", True)
    if cur_rsi < config.RSI_OVERSOLD:
        signals.append(f"RSI超卖 ({cur_rsi:.1f})")
        stock_passed = True
    elif cur_rsi >= config.RSI_OVERBOUGHT:
        signals.append(f"RSI超买 ({cur_rsi:.1f})")
    else:
        signals.append(f"RSI中性: {cur_rsi:.1f}")

    # —— 成交量放大 ——
    vol_ma = calc_ma(volume, config.VOLUME_MA_PERIOD)
    cur_vol = volume.iloc[-1]
    avg_vol = vol_ma.iloc[-1]
    vol_surge = False
    vol_ratio = 0.0
    if avg_vol > 0:
        vol_ratio = cur_vol / avg_vol
        vol_surge = vol_ratio > config.VOLUME_SURGE_RATIO
        logger.info("[动量排查] 成交量放大: 当前量=%s 均量=%s 量比=%.2f 阈值=%.2f → %s",
                    f"{cur_vol:,.0f}", f"{avg_vol:,.0f}", vol_ratio,
                    config.VOLUME_SURGE_RATIO,
                    "通过" if vol_surge else "不通过")
        if dbg:
            mark = "✅" if vol_surge else "❌"
            _debug(f"─── 成交量放大 ───", True)
            _debug(f"  {mark} 当前量={cur_vol:,.0f}  均量={avg_vol:,.0f}  量比={vol_ratio:.2f}  阈值>{config.VOLUME_SURGE_RATIO}", True)
        if vol_surge:
            signals.append(f"成交量放大 ({vol_ratio:.1f}倍)")
            stock_passed = True
    else:
        logger.info("[动量排查] 成交量放大: 均量=0，无法计算量比 → 跳过")
        if dbg:
            _debug(f"─── 成交量放大 ───", True)
            _debug(f"  ➖ 均量为0，无法计算量比，跳过", True)

    # —— 最终 passed：大盘开启时必须 AND 大盘通过，否则只看个股动量 ——
    passed = stock_passed and market_passed

    logger.info("[动量排查] 结果: 个股动量=%s 大盘=%s 最终passed=%s (信号数=%d)",
                "通过" if stock_passed else "不通过",
                "通过" if market_passed else "不通过",
                "通过" if passed else "不通过",
                len(signals))
    if dbg:
        _debug(f"─── 汇总 ───", True)
        _debug(f"  个股动量: {'✅ 通过' if stock_passed else '❌ 不通过'}", True)
        if config.ENABLE_MARKET_FILTER:
            _debug(f"  大盘环境: {'✅ 通过' if market_passed else '❌ 不通过'}", True)
        _debug(f"  最终结果: {'✅ 通过' if passed else '❌ 不通过'}", True)
        _debug("══════ 排查结束 ══════", True)

    # 当大盘未通过但个股有信号时，给出友好提示（附加大盘警告信息到信号列表末尾，用户一眼能看出被"一票否决"）
    if config.ENABLE_MARKET_FILTER and (not market_passed) and stock_passed:
        signals.append("ℹ️  个股有动量信号，但大盘环境不佳，建议观望等待大盘转暖。")

    return passed, signals


def _calc_stop_price(df: pd.DataFrame, cur_price: float, config: TradingConfig) -> float:
    """根据 STOP_MODE 计算止损价。"""
    if config.STOP_MODE == "swing":
        low = df["low"]
        return low.tail(config.RR_WINDOW).min()
    elif config.STOP_MODE == "atr":
        atr = calc_atr(df, config.ATR_PERIOD)
        cur_atr = atr.iloc[-1]
        return cur_price - config.ATR_STOP_MULT * cur_atr
    elif config.STOP_MODE == "fixed":
        return cur_price * (1 - config.FIXED_STOP_PCT)
    else:
        raise ValueError(f"未知的 STOP_MODE: {config.STOP_MODE}")


def _calc_profit_price(df: pd.DataFrame, cur_price: float, stop_loss: float, config: TradingConfig) -> float:
    """根据 PROFIT_MODE 计算止盈价。"""
    if config.PROFIT_MODE == "swing":
        high = df["high"]
        return high.tail(config.RR_WINDOW).max()
    elif config.PROFIT_MODE == "swing_enhanced":
        # 增强结构派：取「20日最高价」与「入场价 + 2*ATR」的较大值
        # 兼顾结构压力位与波动率溢出，避免止盈目标被毛刺压得过低
        high_max = df["high"].tail(config.RR_WINDOW).max()
        atr = calc_atr(df, config.ATR_PERIOD)
        cur_atr = atr.iloc[-1]
        return max(high_max, cur_price + 2 * cur_atr)
    elif config.PROFIT_MODE == "swing_ultra":
        # 超短线：以 RR_WINDOW(默认10)日最高价为压力基准，上方加 N×ATR(默认2)
        high_max = df["high"].tail(config.RR_WINDOW).max()
        atr = calc_atr(df, config.ATR_PERIOD)
        cur_atr = atr.iloc[-1]
        return high_max + config.ATR_PROFIT_MULT * cur_atr
    elif config.PROFIT_MODE == "atr":
        atr = calc_atr(df, config.ATR_PERIOD)
        cur_atr = atr.iloc[-1]
        return cur_price + config.ATR_PROFIT_MULT * cur_atr
    elif config.PROFIT_MODE == "fixed":
        risk = cur_price - stop_loss
        return cur_price + risk * config.FIXED_PROFIT_MULT
    else:
        raise ValueError(f"未知的 PROFIT_MODE: {config.PROFIT_MODE}")


def _mode_label(mode: str) -> str:
    return {"swing": "结构派", "swing_enhanced": "增强结构派", "swing_ultra": "超短线压力派", "atr": "波动率派", "fixed": "固定派"}.get(mode, mode)


def check_dimension_c(df: pd.DataFrame, config: TradingConfig) -> tuple:
    """赔率维度：按配置模式计算止损止盈，判断盈亏比。"""
    close = df["close"]
    cur_price = close.iloc[-1]

    stop_loss = _calc_stop_price(df, cur_price, config)
    take_profit = _calc_profit_price(df, cur_price, stop_loss, config)

    loss_space = cur_price - stop_loss
    profit_space = take_profit - cur_price

    if loss_space <= 0:
        ratio = float("inf")
        passed = True
    else:
        ratio = profit_space / loss_space
        passed = ratio > config.MIN_RR_RATIO

    return passed, {
        "price": cur_price,
        "stop": stop_loss,
        "take": take_profit,
        "ratio": ratio,
        "loss_space": loss_space,
        "profit_space": profit_space,
        "stop_mode": config.STOP_MODE,
        "profit_mode": config.PROFIT_MODE,
    }


def calc_position(c_info: dict, config: TradingConfig) -> dict:
    """根据最大单笔风险计算建议开仓数量。"""
    cur_price = c_info["price"]
    stop_loss = c_info["stop"]
    loss_per_share = cur_price - stop_loss

    if loss_per_share <= 0:
        return {"shares": 0, "note": "当前价低于止损价，不适合计算仓位"}

    max_loss_amount = config.TOTAL_CAPITAL * config.MAX_RISK_PER_TRADE
    shares = int(max_loss_amount / loss_per_share)

    return {
        "shares": shares,
        "max_loss_amount": max_loss_amount,
        "loss_per_share": loss_per_share,
    }


# ==================== 对比模式 ====================

def _evaluate_stock(symbol: str, config: TradingConfig) -> dict:
    """对单只股票执行完整三维度判定，返回结果字典（不打印）。

    供对比模式复用：fetch_data → 三维度检查 → 仓位计算。
    """
    df = fetch_data(symbol, config)
    a_pass, a_msg = check_dimension_a(df, config)
    b_pass, b_signals = check_dimension_b(df, config)
    c_pass, c_info = check_dimension_c(df, config)
    pos = calc_position(c_info, config)
    return {
        "symbol": symbol,
        "df": df,
        "a_pass": a_pass, "a_msg": a_msg,
        "b_pass": b_pass, "b_signals": b_signals,
        "c_pass": c_pass, "c_info": c_info,
        "pos": pos,
        "latest_close": float(df["close"].iloc[-1]),
        "date_range": (
            f"{df['date'].iloc[0].strftime('%Y-%m-%d')} 至 "
            f"{df['date'].iloc[-1].strftime('%Y-%m-%d')}"
        ),
    }


def _print_stock_result(result: dict, config: TradingConfig, show_weekly: bool = True) -> None:
    """打印单只股票的三维度结果 + 仓位 + 总结（对比模式复用）。

    show_weekly=True 时在末尾追加维度D（周线观察哨）独立区块。
    维度D 完全独立：失败时仅打印"周线数据暂不可用"，绝不影响 A/B/C 已打印的结论。
    """
    c_info = result["c_info"]
    pos = result["pos"]
    a_pass, a_msg = result["a_pass"], result["a_msg"]
    b_pass, b_signals = result["b_pass"], result["b_signals"]
    c_pass = result["c_pass"]

    print(f"📊 数据范围: {result['date_range']}")
    print(f"📈 最新收盘价: {result['latest_close']:.2f}")
    print()

    # 维度 A
    print("【维度A - 结构】")
    if a_pass:
        print(f"  ✅ 维度A通过 - {a_msg}")
    else:
        print(f"  ❌ 维度A不通过 - {a_msg}")
    print()

    # 维度 B
    print("【维度B - 动量】")
    if b_pass:
        print(f"  ✅ 维度B通过 - 信号: {', '.join(b_signals)}")
    else:
        if b_signals:
            print("  ❌ 维度B不通过")
            for s in b_signals:
                print(f"    {s}")
        else:
            print("  ❌ 维度B不通过 - 无动量信号")
    print()

    # 维度 C
    print("【维度C - 赔率】")
    ratio_str = f"{c_info['ratio']:.2f}" if c_info["ratio"] != float("inf") else "∞"
    stop_label = _mode_label(c_info["stop_mode"])
    profit_label = _mode_label(c_info["profit_mode"])
    if c_pass:
        print(f"  ✅ 维度C通过（盈亏比 {ratio_str}:1 > {config.MIN_RR_RATIO}:1）")
    else:
        print(f"  ❌ 维度C不通过（盈亏比{ratio_str}:1 ≤ {config.MIN_RR_RATIO}:1）")
    print(f"  止损模式: {stop_label} | 止盈模式: {profit_label}")
    print(f"  当前价: {c_info['price']:.2f}")
    print(f"  支撑位(止损): {c_info['stop']:.2f}，亏损空间: {c_info['loss_space']:.2f}")
    print(f"  压力位(止盈): {c_info['take']:.2f}，盈利空间: {c_info['profit_space']:.2f}")
    print(f"  盈亏比: {ratio_str}:1")

    # 仓位建议
    print()
    print("【仓位建议】")
    if pos["shares"] > 0:
        print(f"  📐 总资金 {config.TOTAL_CAPITAL:,.0f} 元，单笔最大风险 {config.MAX_RISK_PER_TRADE*100:.1f}%")
        print(f"  每股亏损: {pos['loss_per_share']:.2f}，最大亏损额: {pos['max_loss_amount']:,.2f} 元")
        print(f"  ✅ 建议开仓: {pos['shares']:,} 股")
    else:
        print(f"  ⚠️  {pos['note']}")
    print()

    # 总结
    print("=" * 60)
    all_pass = a_pass and b_pass and c_pass
    if all_pass:
        print("✅ 三维度均通过，建议开仓。")
    else:
        failed = []
        if not a_pass:
            failed.append("A")
        if not b_pass:
            failed.append("B")
        if not c_pass:
            failed.append("C")
        print(f"❌ 维度 {', '.join(failed)} 未通过，暂不建议开仓。")
    print("=" * 60)

    # —— 维度D：周线趋势背景（独立观察哨，只读，不影响 A/B/C 总分）——
    if show_weekly and _WEEKLY_AVAILABLE:
        try:
            weekly_result = check_weekly_background(result["symbol"], config)
            print()
            print_weekly_result(weekly_result)
        except Exception as e:
            # 双保险：维度D 任何异常都不得影响 A/B/C 已打印的结论
            print()
            print("【维度D - 周线趋势背景（独立观察哨）】")
            print(f"  ⚠️  周线观察执行异常：{e}")
            print()


_MOMENTUM_SIGNAL_KEYWORDS = ["MACD金叉", "MACD柱线翻红", "RSI超卖", "成交量放大"]


def _count_momentum_signals(b_signals: list) -> int:
    """统计有效动量信号数（排除大盘警告/中性提示等非信号文本）。"""
    count = 0
    for s in b_signals:
        for kw in _MOMENTUM_SIGNAL_KEYWORDS:
            if kw in s:
                count += 1
                break
    return count


def _build_compare_verdict(r1: dict, r2: dict, config: TradingConfig) -> dict:
    """构建对比结论：维度通过数优先 → 盈亏比 → 动量信号数 → 结构信号。

    Returns:
        {"winner": result_dict, "loser": result_dict, "reasons": list[str]}
    """
    reasons = []

    # 1. 维度通过数
    pc1 = sum([r1["a_pass"], r1["b_pass"], r1["c_pass"]])
    pc2 = sum([r2["a_pass"], r2["b_pass"], r2["c_pass"]])
    reasons.append(
        f"  1. 维度通过数：{r1['symbol']} ({pc1}/3)  vs  {r2['symbol']} ({pc2}/3)"
    )
    if pc1 != pc2:
        winner, loser = (r1, r2) if pc1 > pc2 else (r2, r1)
        reasons.append(f"  → 通过数差异决定胜负")
        return {"winner": winner, "loser": loser, "reasons": reasons}

    # 2. 盈亏比（维度C）
    def _safe_ratio(r):
        return r if r != float("inf") else 999999.0

    ratio1 = r1["c_info"]["ratio"]
    ratio2 = r2["c_info"]["ratio"]
    rs1, rs2 = _safe_ratio(ratio1), _safe_ratio(ratio2)
    r1_str = f"{ratio1:.2f}" if ratio1 != float("inf") else "∞"
    r2_str = f"{ratio2:.2f}" if ratio2 != float("inf") else "∞"
    reasons.append(
        f"  2. 盈亏比：{r1['symbol']} ({r1_str}:1)  vs  {r2['symbol']} ({r2_str}:1)"
    )
    if rs1 != rs2:
        winner, loser = (r1, r2) if rs1 > rs2 else (r2, r1)
        reasons.append(f"  → 盈亏比差异决定胜负（通过数相同）")
        return {"winner": winner, "loser": loser, "reasons": reasons}

    # 3. 动量信号数
    ms1 = _count_momentum_signals(r1["b_signals"])
    ms2 = _count_momentum_signals(r2["b_signals"])
    reasons.append(
        f"  3. 动量信号数：{r1['symbol']} ({ms1}个)  vs  {r2['symbol']} ({ms2}个)"
    )
    if ms1 != ms2:
        winner, loser = (r1, r2) if ms1 > ms2 else (r2, r1)
        reasons.append(f"  → 动量信号数差异决定胜负（通过数+盈亏比相同）")
        return {"winner": winner, "loser": loser, "reasons": reasons}

    # 4. 结构信号（A 是否通过）
    reasons.append(
        f"  4. 结构信号：{r1['symbol']} ({'通过' if r1['a_pass'] else '不通过'})  vs  "
        f"{r2['symbol']} ({'通过' if r2['a_pass'] else '不通过'})"
    )
    if r1["a_pass"] != r2["a_pass"]:
        winner, loser = (r1, r2) if r1["a_pass"] else (r2, r1)
        reasons.append(f"  → 结构信号差异决定胜负")
        return {"winner": winner, "loser": loser, "reasons": reasons}

    # 5. 全部相同 → 平局，默认选第一只
    reasons.append(f"  → 各项指标均相同，视为平局，默认选择 {r1['symbol']}")
    return {"winner": r1, "loser": r2, "reasons": reasons}


def _run_compare_mode(
    symbol1: str,
    symbol2: str,
    config: TradingConfig,
    profile_names: list,
    overrides: list,
    report: bool,
    auto_open: bool,
    name1: str = "",
    name2: str = "",
    show_weekly: bool = True,
) -> int:
    """对比模式：两只股票三维度判定 + 对比图 + 结论。

    show_weekly=True 时，每只股票在 A/B/C 总结后追加维度D（周线观察哨）区块。
    """
    print("=" * 60)
    print(f"🔍 对比模式 - {symbol1} vs {symbol2}")
    print(f"   配置链: {' + '.join(profile_names)} ({type(config).__name__})")
    if overrides:
        print(f"   单字段覆盖: {len(overrides)} 项 ({'; '.join(overrides)})")
    print("=" * 60)
    print()

    # —— 评估第一只 ——
    header1 = f"━━━ {symbol1}" + (f"  {name1}" if name1 else "") + " ━━━"
    print(header1)
    print("=" * 60)
    try:
        r1 = _evaluate_stock(symbol1, config)
        r1["name"] = name1
        _print_stock_result(r1, config, show_weekly=show_weekly)
    except Exception as e:
        print(f"❌ {symbol1} 评估失败: {e}")
        return 1
    print()

    # —— 评估第二只 ——
    header2 = f"━━━ {symbol2}" + (f"  {name2}" if name2 else "") + " ━━━"
    print(header2)
    print("=" * 60)
    try:
        r2 = _evaluate_stock(symbol2, config)
        r2["name"] = name2
        _print_stock_result(r2, config, show_weekly=show_weekly)
    except Exception as e:
        print(f"❌ {symbol2} 评估失败: {e}")
        return 1
    print()

    # —— 对比结论 ——
    print("=" * 60)
    print("🏆 对比结论")
    print("=" * 60)
    verdict = _build_compare_verdict(r1, r2, config)
    winner = verdict["winner"]
    loser = verdict["loser"]
    w_label = winner["symbol"] + (f" {winner.get('name', '')}" if winner.get("name") else "")
    l_label = loser["symbol"] + (f" {loser.get('name', '')}" if loser.get("name") else "")
    print(f"  ✅ 推荐：{w_label}")
    print(f"  ❌ 次选：{l_label}")
    print()
    print("  对比依据：")
    for reason in verdict["reasons"]:
        print(reason)
    print("=" * 60)

    # —— 对比图 ——
    if report:
        try:
            from chart import generate_compare_chart
            print()
            print("📊 正在生成对比图...")
            out_path = generate_compare_chart(
                result1=r1, result2=r2,
                config=config, verdict=verdict,
                auto_open=auto_open,
            )
            print(f"  ✅ 对比图已保存: {out_path}")
            if auto_open:
                print(f"  🔗 已在默认浏览器打开")
            else:
                print(f"  💡 提示: 加 --open 可自动打开图片")
        except ImportError as e:
            print(f"  ⚠️  对比图生成失败: 缺少依赖 ({e})")
            print(f"     请运行: uv sync  (或 pip install matplotlib mplfinance)")
        except Exception as e:
            print(f"  ⚠️  对比图生成失败: {e}")
            import traceback
            traceback.print_exc()
        print("=" * 60)

    return 0


# ==================== 主入口 ====================

def _parse_args(argv: list) -> tuple:
    """解析命令行参数，返回 (symbol, profile_names, overrides, verbose, show_config, report)。

    - --profile/-p：允许重复传入，按顺序叠加（后面的配置覆盖前面的"显式字段"）。
      例如: -p aggressive_short -p long_term
    - --override/-o：单字段覆盖，语法 KEY=VALUE，大小写敏感。
      支持类型：bool (true/false/1/0/yes/no)、int、float、
                 List[int/float/str/bool] 用逗号分隔（支持 [a,b] 或 a,b）、
                 Tuple[...] 同上，str 原样（引号自动脱除）。
      例如: -o ENABLE_MARKET_FILTER=false -o PULLBACK_MAX_DEVIATION=0.01
    - --report/-r：生成可视化 PNG 报告（K线+成交量+MACD+RSI+止损止盈线+入场星+总结）。
      报告保存到 data/indicator-reports/report_{股票代码}_{日期}.png。
    """
    import argparse

    parser = argparse.ArgumentParser(description="开仓前三维度快速筛查工具")
    parser.add_argument("symbol", help="股票代码 (6位数字)")
    parser.add_argument(
        "--profile", "-p",
        action="append",
        default=None,
        dest="profiles",
        metavar="PROFILE_NAME",
        help=(f"配置档名称，可重复传入以叠加。例如 -p aggressive_short -p long_term。\n"
              f"  默认为 default。可用配置档:\n{list_profiles()}"),
    )
    parser.add_argument(
        "--override", "-o",
        action="append",
        dest="overrides",
        default=None,
        metavar="KEY=VALUE",
        help=("单字段覆盖（可重复）。支持 bool/int/float/List/Tuple/str。\n"
              "  例: -o ENABLE_MARKET_FILTER=false\n"
              "  例: -o PULLBACK_MAX_DEVIATION=0.01\n"
              "  例: -o PULLBACK_MA_TARGETS=[5,10]"),
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="启用详细日志（将 logger.info 的回踩/大盘排查过程打印到控制台）。\n"
             "  PULLBACK_DEBUG / MARKET_DEBUG 控制控制台 print，\n"
             "  -v 则额外输出 Python logging 级别的详细结构化日志。",
    )
    parser.add_argument(
        "--show-config",
        action="store_true",
        help="运行时先打印当前生效的 TradingConfig 全量字段（含字段来源），便于确认参数。",
    )
    parser.add_argument(
        "--report", "-r",
        action="store_true",
        help="生成可视化 PNG 报告（K线主图 + 成交量/MACD/RSI 副图 + 止损止盈线 + 入场星 + 总结）。\n"
             "  保存到 data/indicator-reports/report_{股票代码}_{日期}.png。\n"
             "  配合 --open 自动用浏览器打开。",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="配合 --report 使用，生成后自动用默认浏览器打开图片。",
    )
    parser.add_argument(
        "--name",
        default="",
        metavar="STOCK_NAME",
        help="股票名称（可选，用于图表标题显示）。例: --name 平安银行",
    )
    parser.add_argument(
        "--skip-all-fail",
        action="store_true",
        help="当三维度均不通过时跳过 PNG 生成（配合 --report 使用）。退出码 2 表示跳过。",
    )
    parser.add_argument(
        "--compare", "-c",
        default=None,
        metavar="SYMBOL",
        help=("对比模式：指定第二只股票代码，对两只股票进行三维度对比并生成对比图。\n"
              "  需配合 -r 生成对比图。--name 参数用逗号分隔两只股票名称。\n"
              "  例: python main.py 600550 -c 000001 -p aggressive_short -r\n"
              "  例: python main.py 600550 -c 000001 -r --name 平安银行,招商银行"),
    )
    parser.add_argument(
        "--no-weekly",
        action="store_true",
        help=("跳过维度D（周线趋势背景观察哨）的显示。\n"
              "  维度D 默认开启且只读，不影响 A/B/C 任何判定；本开关仅控制是否打印周线区块。"),
    )
    args = parser.parse_args(argv)
    if not args.profiles:
        args.profiles = ["default"]
    if not args.overrides:
        args.overrides = []
    return (args.symbol, args.profiles, args.overrides, args.verbose,
            args.show_config, args.report, args.open, args.name, args.skip_all_fail, args.compare,
            args.no_weekly)


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python main.py <股票代码> [-p PROFILE]... [-o KEY=VAL]... [-v] [--show-config] [-r] [--open] [--name 股票名称]")
        print("示例1 (单配置): python main.py 600118 -p high_vol")
        print("示例2 (叠加配置): python main.py 600550 -p aggressive_short -p long_term --show-config")
        print("示例3 (单字段覆盖): python main.py 600550 -o ENABLE_MARKET_FILTER=false -o MIN_RR_RATIO=1.5")
        print("示例4 (全组合): python main.py 600550 -p aggressive_short -p long_term -o PULLBACK_MAX_DEVIATION=0.008 -v --show-config")
        print("示例5 (可视化报告): python main.py 600550 -p aggressive_short -r --open")
        print("示例6 (带名称): python main.py 600550 -p aggressive_short -r --name 中国平安")
        print("示例7 (对比模式): python main.py 600550 -c 000001 -p aggressive_short -r")
        print("示例8 (对比+名称): python main.py 600550 -c 000001 -r --name 平安银行,招商银行")
        print(f"\n可用配置档:\n{list_profiles()}")
        return 1

    (symbol, profile_names, overrides, verbose, show_config, report,
     auto_open, stock_name, skip_all_fail, compare_symbol, no_weekly) = _parse_args(sys.argv[1:])

    # —— 初始化 logging：仅 -v 时启用 INFO 级别，否则用默认 WARNING（吞掉 logger.info）——
    if verbose:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s  %(levelname)-5s  %(name)s → %(message)s",
            datefmt="%H:%M:%S",
        )
    else:
        root = logging.getLogger()
        if not root.handlers:
            _h = logging.StreamHandler()
            _h.setLevel(logging.WARNING)
            root.addHandler(_h)
            root.setLevel(logging.WARNING)

    try:
        config, field_sources = merge_profiles(profile_names)
    except ValueError as e:
        print(f"❌ {e}")
        return 1

    if overrides:
        try:
            apply_overrides(config, overrides, field_sources)
        except ValueError as e:
            print(f"❌ --override 解析失败: {e}")
            return 1

    # —— 对比模式分支：--compare 指定第二只股票，走对比流程 ——
    if compare_symbol:
        name1, name2 = "", ""
        if stock_name:
            parts = stock_name.split(",", 1)
            name1 = parts[0].strip()
            if len(parts) > 1:
                name2 = parts[1].strip()
        return _run_compare_mode(
            symbol, compare_symbol, config,
            profile_names, overrides,
            report, auto_open, name1, name2,
            show_weekly=not no_weekly,
        )

    print("=" * 60)
    print(f"🔍 开仓检查清单 - 股票代码: {symbol}")
    print(f"   配置链: {' + '.join(profile_names)} ({type(config).__name__})")
    if len(profile_names) > 1:
        print(f"   叠加规则: 后者仅覆盖其「显式声明」的字段（继承字段不参与）")
    if overrides:
        print(f"   单字段覆盖: {len(overrides)} 项 ({'; '.join(overrides)})")
    if verbose:
        print(f"   详细日志: -v 已开启（logger.info 将输出）")
    print("=" * 60)
    print()

    # —— --show-config：打印当前生效的所有配置字段 + 来源 ——
    if show_config:
        print("【当前生效配置 (TradingConfig 全量字段 + 来源)】")
        from dataclasses import fields as _dc_fields
        fields_info = [(f.name, f) for f in _dc_fields(type(config))]
        pad_name = max(len(n) for n, _ in fields_info)
        pad_src = max(len(str(field_sources.get(n, "-"))) for n, _ in fields_info)
        for name, f in fields_info:
            val = getattr(config, name)
            if isinstance(val, float) and val > 1000:
                val_str = f"{val:,.2f}"
            else:
                val_str = repr(val)
            src = field_sources.get(name, "default")
            # 对多 profile 情况，若某字段来自合并链尾，高亮显示
            src_tag = f"[{src}]"
            print(f"  {name:<{pad_name}}  {src_tag:<{pad_src + 2}}  {val_str}")
        print()
        print("=" * 60)
        print()

    # —— 获取数据 ——
    df = fetch_data(symbol, config)

    date_range = (
        f"{df['date'].iloc[0].strftime('%Y-%m-%d')} 至 "
        f"{df['date'].iloc[-1].strftime('%Y-%m-%d')}"
    )
    latest_close = df["close"].iloc[-1]
    print(f"📊 数据范围: {date_range}")
    print(f"📈 最新收盘价: {latest_close:.2f}")
    print()

    # —— 维度 A ——
    print("【维度A - 结构】")
    a_pass, a_msg = check_dimension_a(df, config)
    if a_pass:
        print(f"  ✅ 维度A通过 - {a_msg}")
    else:
        print(f"  ❌ 维度A不通过 - {a_msg}")
    print()

    # —— 维度 B ——
    print("【维度B - 动量】")
    b_pass, b_signals = check_dimension_b(df, config)
    if b_pass:
        print(f"  ✅ 维度B通过 - 信号: {', '.join(b_signals)}")
    else:
        # 根据是否有信号内容显示不同标题（大盘警告也算有内容，非"无信号"）
        if b_signals:
            print("  ❌ 维度B不通过")
            for s in b_signals:
                print(f"    {s}")
        else:
            print("  ❌ 维度B不通过 - 无动量信号")
    print()

    # —— 维度 C ——
    print("【维度C - 赔率】")
    c_pass, c_info = check_dimension_c(df, config)
    ratio_str = f"{c_info['ratio']:.2f}" if c_info["ratio"] != float("inf") else "∞"
    stop_label = _mode_label(c_info["stop_mode"])
    profit_label = _mode_label(c_info["profit_mode"])
    if c_pass:
        print(f"  ✅ 维度C通过（盈亏比 {ratio_str}:1 > {config.MIN_RR_RATIO}:1）")
    else:
        print(f"  ❌ 维度C不通过（盈亏比{ratio_str}:1 ≤ {config.MIN_RR_RATIO}:1）")
    print(f"  止损模式: {stop_label} | 止盈模式: {profit_label}")
    print(f"  当前价: {c_info['price']:.2f}")
    print(f"  支撑位(止损): {c_info['stop']:.2f}，亏损空间: {c_info['loss_space']:.2f}")
    print(f"  压力位(止盈): {c_info['take']:.2f}，盈利空间: {c_info['profit_space']:.2f}")
    print(f"  盈亏比: {ratio_str}:1")

    # —— 仓位建议 ——
    pos = calc_position(c_info, config)
    print()
    print("【仓位建议】")
    if pos["shares"] > 0:
        print(f"  📐 总资金 {config.TOTAL_CAPITAL:,.0f} 元，单笔最大风险 {config.MAX_RISK_PER_TRADE*100:.1f}%")
        print(f"  每股亏损: {pos['loss_per_share']:.2f}，最大亏损额: {pos['max_loss_amount']:,.2f} 元")
        print(f"  ✅ 建议开仓: {pos['shares']:,} 股")
    else:
        print(f"  ⚠️  {pos['note']}")
    print()

    # —— 总结 ——
    print("=" * 60)
    all_pass = a_pass and b_pass and c_pass
    if all_pass:
        print("✅ 三维度均通过，建议开仓。")
    else:
        failed = []
        if not a_pass:
            failed.append("A")
        if not b_pass:
            failed.append("B")
        if not c_pass:
            failed.append("C")
        print(f"❌ 维度 {', '.join(failed)} 未通过，暂不建议开仓。")
    print("=" * 60)

    # —— 维度D：周线趋势背景（独立观察哨，只读，不影响 A/B/C 总分）——
    # 即便维度D 模块加载失败/周线接口超时/数据不足，也绝不影响 A/B/C 已得出的结论
    if not no_weekly and _WEEKLY_AVAILABLE:
        try:
            weekly_result = check_weekly_background(symbol, config)
            print()
            print_weekly_result(weekly_result)
        except Exception as e:
            # 双保险：维度D 任何异常都不得影响 A/B/C 已打印的结论
            print()
            print("【维度D - 周线趋势背景（独立观察哨）】")
            print(f"  ⚠️  周线观察执行异常：{e}")
            print()
    elif not _WEEKLY_AVAILABLE:
        print()
        print("【维度D - 周线趋势背景（独立观察哨）】")
        print(f"  ⚠️  weekly_observer 模块加载失败，已跳过：{_WEEKLY_IMPORT_ERROR}")
        print()

    # —— 可视化报告（--report/-r）——
    if report:
        # 若启用 --skip-all-fail 且三维度全不通过，跳过图片生成
        if skip_all_fail and not (a_pass or b_pass or c_pass):
            failed_str = ", ".join(
                [d for d, p in [("A", a_pass), ("B", b_pass), ("C", c_pass)] if not p]
            )
            print(f"\n⏭️  --skip-all-fail: 三维度均不通过（{failed_str}），跳过 PNG 生成")
            return 2

        try:
            from chart import generate_chart_report
            print()
            print("📊 正在生成可视化报告...")
            out_path = generate_chart_report(
                df=df,
                config=config,
                symbol=symbol,
                a_result=(a_pass, a_msg),
                b_result=(b_pass, b_signals),
                c_result=(c_pass, c_info),
                pos=pos,
                auto_open=auto_open,
                stock_name=stock_name,
            )
            print(f"  ✅ 报告已保存: {out_path}")
            if auto_open:
                print(f"  🔗 已在默认浏览器打开")
            else:
                print(f"  💡 提示: 加 --open 可自动打开图片")
        except ImportError as e:
            print(f"  ⚠️  可视化报告生成失败: 缺少依赖 ({e})")
            print(f"     请运行: uv sync  (或 pip install matplotlib mplfinance)")
        except Exception as e:
            print(f"  ⚠️  可视化报告生成失败: {e}")
            if verbose:
                import traceback
                traceback.print_exc()
        print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())