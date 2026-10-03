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
from market_filter import check_market_environment, _ensure_bs_login

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


# 维度E（市场状态雷达）独立只读模块：同样即便加载失败也绝对不影响 A/B/C/D
try:
    from regime_detector import detect_regime, print_regime_result
    _REGIME_AVAILABLE = True
    _REGIME_IMPORT_ERROR: str | None = None
except Exception as _e:  # pragma: no cover - 容灾兜底
    _REGIME_AVAILABLE = False
    _REGIME_IMPORT_ERROR = str(_e)

    def detect_regime(*_args, **_kwargs):  # type: ignore
        return {"available": False, "regime": "UNKNOWN",
                "error": f"模块加载失败: {_REGIME_IMPORT_ERROR}",
                "advice": "", "entry_hint": "", "facts": {}}

    def print_regime_result(*_args, **_kwargs) -> None:  # type: ignore
        pass


# ETF 规则模块（代码识别 + 标的指数映射 + 参数覆盖）：加载失败也不影响主流程
try:
    from etf import (is_etf, resolve_underlying, apply_etf_overrides,
                     describe_plan, print_etf_plan, fetch_etf_name,
                     match_theme_index, assert_loaded as _etf_assert_loaded)
    _ETF_AVAILABLE = True
    _ETF_IMPORT_ERROR: str | None = None
    _etf_assert_loaded()
except Exception as _e:  # pragma: no cover - 容灾兜底
    _ETF_AVAILABLE = False
    _ETF_IMPORT_ERROR = str(_e)

    def is_etf(*_a, **_k):  # type: ignore
        return False

    def resolve_underlying(symbol, *_a, **_k):  # type: ignore
        return {"etf": symbol, "is_etf": False, "index": None, "index_name": None,
                "mapped": False, "suitable": True, "warnings": []}

    def apply_etf_overrides(*_a, **_k):  # type: ignore
        return {}

    def describe_plan(*_a, **_k):  # type: ignore
        return {"is_etf": False, "resolved": resolve_underlying(""), "changed": {}, "warnings": []}

    def fetch_etf_name(*_a, **_k):  # type: ignore
        return ""

    def match_theme_index(*_a, **_k):  # type: ignore
        return None

    def print_etf_plan(*_a, **_k) -> None:  # type: ignore
        pass


# 基准指数自动选择（benchmark.py）：加载失败也不影响主流程
try:
    from benchmark import select_benchmark, print_benchmark_selection
    _BENCHMARK_AVAILABLE = True
except Exception as _e:  # pragma: no cover - 容灾兜底
    _BENCHMARK_AVAILABLE = False
    _BENCHMARK_IMPORT_ERROR = str(_e)

    def select_benchmark(*_a, **_k):  # type: ignore
        return {"index": "sh000001", "name": "上证综指", "corr": None,
                "ranking": [], "weak": True, "fallback": True}

    def print_benchmark_selection(*_a, **_k) -> None:  # type: ignore
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

    尝试顺序：baostock 优先，efinance 兜底。均失败则友好提示并退出。

    注意 start_date 的换算：DATA_TRADING_DAYS 是**交易日**数，而接口按**自然日**过滤，
    故需 ×1.6 再留缓冲（一年约 243 交易日 / 365 自然日 ≈ 0.67）。
    原实现写死 `timedelta(days=240)`，实际只换来约 161 个交易日 ——
    会让依赖数据长度的判定（如 `--index auto` 的相关性样本数）与配置不符。
    """
    ef_code, bs_code = _normalize_code(symbol)
    end_date = datetime.now().strftime("%Y-%m-%d")
    calendar_days = int(config.DATA_TRADING_DAYS * 1.6) + 30
    start_date = (datetime.now() - timedelta(days=calendar_days)).strftime("%Y-%m-%d")

    # —— 数据源 1: baostock（首选：直连 TCP，稳定无代理依赖）——
    # 会话复用：bs.login() 是新进程里 ~15s 的登录往返，原先本函数每次登录/登出，
    # 加上 regime_detector 与 weekly_observer 各自的登录，一次运行要白等 3 次。
    # 统一走 market_filter._ensure_bs_login()（进程内只登录一次，atexit 兜底登出）。
    # 改为首选源：efinance 调 eastmoney 接口在 macOS OpenSSL 3.6+ 环境下
    # 会被 Microsoft-IIS/10.0 断开（TLS 兼容性问题），永久不可用；
    # baostock 直连其 TCP 服务器，不受系统 HTTP 代理影响。
    def _try_baostock():
        try:
            if not _ensure_bs_login():
                return None
            import baostock as bs
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
            if rows:
                return pd.DataFrame(rows, columns=rs.fields)
        except Exception:
            return None
        return None

    df = _suppress_output(_try_baostock)
    if df is not None and not df.empty:
        return _normalize_bs(df, config)

    # —— 数据源 2: efinance（兜底）——
    def _try_efinance():
        try:
            import efinance as ef
            return ef.stock.get_quote_history(ef_code)
        except Exception:
            return None

    df = _suppress_output(_try_efinance)
    if df is not None and not df.empty:
        return _normalize_ef(df, config)

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

    依次检查：偏离度 → **方向（须从上方回落触及均线）** → 均线方向 → 缩量 → K 线形态 → 确认窗口。
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
    logger.info("[回踩排查] 方向约束: 需从上方回落=%s (回看%d根, 允许下方%.2f%%)",
                config.PULLBACK_REQUIRE_TOUCH_FROM_ABOVE,
                config.PULLBACK_TOUCH_LOOKBACK,
                config.PULLBACK_ALLOW_BELOW_RATIO * config.PULLBACK_MAX_DEVIATION * 100)

    if dbg:
        _debug("══════ 回踩信号排查 ══════", True)
        _debug(f"目标均线: {config.PULLBACK_MA_TARGETS}", True)
        _debug(f"偏离阈值: {config.PULLBACK_MAX_DEVIATION * 100:.2f}%  |  均线向上: {config.PULLBACK_REQUIRE_MA_UP}", True)
        _debug(f"缩量要求: {config.PULLBACK_REQUIRE_SHRINK_VOLUME}  阈值: {config.PULLBACK_VOLUME_SHRINK_RATIO}", True)
        _debug(f"K线形态: {config.PULLBACK_REQUIRE_BULLISH_CANDLE}  |  确认窗口: {config.PULLBACK_CONFIRM_BARS} 根", True)
        _debug(f"方向约束: 需从上方回落={config.PULLBACK_REQUIRE_TOUCH_FROM_ABOVE} "
               f"(回看{config.PULLBACK_TOUCH_LOOKBACK}根, 允许下方{config.PULLBACK_ALLOW_BELOW_RATIO * config.PULLBACK_MAX_DEVIATION * 100:.2f}%)", True)
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

        # ① 偏离度检查（绝对距离，仅作为"够不够近"的粗筛）
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

        # ①b 方向检查：必须是「从上方回落触及均线」，不是「在均线上方偏离」也不是「破位」
        #    有符号偏离：>0 = 收盘在均线上方，<0 = 下方
        signed_dev = (cur_close - cur_ma) / cur_ma
        if config.PULLBACK_REQUIRE_TOUCH_FROM_ABOVE:
            dir_ok, dir_detail = _check_pullback_from_above(
                close, ma_series, signed_dev, ma_up, config)
            logger.info("[回踩排查] MA%d ①b方向: 有符号偏离=%+.3f%% 均线向上=%s → %s",
                        period, signed_dev * 100, ma_up, "通过" if dir_ok else "不通过(跳过)")
            if dbg:
                mark = "✅" if dir_ok else "❌"
                _debug(f"  {mark} 方向检查: {dir_detail}", True)
            if not dir_ok:
                final_results.append(f"MA{period}: {dir_detail}")
                continue
            dir_label = "从上方回落触及均线"
        else:
            logger.info("[回踩排查] MA%d ①b方向: 已关闭方向约束(PULLBACK_REQUIRE_TOUCH_FROM_ABOVE=False) → 跳过", period)
            if dbg:
                _debug("  ➖ 方向检查: 不要求 (PULLBACK_REQUIRE_TOUCH_FROM_ABOVE=False)", True)
            dir_label = None

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
        if dir_label:
            matched.append(dir_label)
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


def _check_pullback_from_above(close: pd.Series, ma_series: pd.Series,
                               signed_dev: float, ma_up: bool,
                               config: TradingConfig) -> tuple:
    """方向检查：判定这是「从上方回落触及均线」的回踩，而不是「偏离」或「破位」。

    为什么需要这一步：
        `check_pullback_signal` 的偏离度用 `abs(收盘 − MA) / MA`，把均线**上方** 0.5%
        与**下方** 0.5% 视作等价。但对"回踩买入"而言两者含义完全相反：
            · 均线上方 1.5%  = 偏离/追高（实测创业板ETF 有 30% 的交易日处于这种状态）
            · 均线下方较远   = 破位，不是回踩
        只用绝对距离，这两种劣质信号都会被放进来。

    判定规则（两步）：
        ① **必须存在回落动作**：最近 PULLBACK_TOUCH_LOOKBACK 根内有收盘价位于均线**上方**，
           证明价格是从上方掉下来的（排除"一直贴着均线横盘"与"从下方反弹上来"）。
        ② **下方穿越受限**：当前收盘若在均线下方，跌幅不得超过
           PULLBACK_ALLOW_BELOW_RATIO × PULLBACK_MAX_DEVIATION，
           且均线必须向上（均线向下时的失守是破位，不是回踩）。

    Returns:
        (是否通过, 说明文本)
    """
    lookback = max(1, int(config.PULLBACK_TOUCH_LOOKBACK))
    had_above = False
    above_days = 0
    for i in range(1, lookback + 1):
        if len(close) < i + 1:
            break
        past_close = float(close.iloc[-(i + 1)])
        past_ma = float(ma_series.iloc[-(i + 1)])
        if past_ma > 0 and past_close > past_ma:
            had_above = True
            above_days = i
            break

    if not had_above:
        return False, (f"最近{lookback}根内无'收盘在均线上方'的回落动作"
                       f"（当前有符号偏离 {signed_dev * 100:+.3f}%）")

    below_limit = -config.PULLBACK_ALLOW_BELOW_RATIO * config.PULLBACK_MAX_DEVIATION
    if signed_dev < 0:
        if signed_dev < below_limit:
            return False, (f"收盘已跌破均线 {abs(signed_dev) * 100:.3f}%，"
                           f"超过允许的 {abs(below_limit) * 100:.2f}%（属破位而非回踩）")
        if not ma_up:
            return False, (f"收盘在均线下方（{signed_dev * 100:+.3f}%）且均线未向上，"
                           f"属破位而非回踩")

    where = "均线上方" if signed_dev >= 0 else "均线下方"
    return True, (f"第-{above_days}根曾在均线上方，当前位于{where}"
                  f"（有符号偏离 {signed_dev * 100:+.3f}%）")


def _check_breakout_from_above(close: pd.Series, prev_high: pd.Series,
                               config: TradingConfig) -> tuple:
    """突破质量检查：区分「刚突破」与「已追高」以及「盘中假突破」。

    为什么需要这一步（实测 002119）：现价 29.61、前 20 日高点 26.92，突破幅度 **+9.99%**。
    `check_dimension_a` 原本只判 `close > prev_high`，于是把这种"已经涨了 10% 的追高"
    与"刚站上前高 1%"当作同一个信号放行。两者风险完全不同。

    判定规则（两步，均可配置关闭）：
        ① **新鲜度**（BREAKOUT_REQUIRE_FRESH）：突破必须发生在最近
           BREAKOUT_CONFIRM_BARS 根内 —— 用收盘价序列判定，天然排除"盘中触及"；
           若突破发生在很久以前，现在只是在高位徘徊，不属于"买点"。
        ② **幅度上限**（BREAKOUT_MAX_PCT）：突破幅度 =（现价 − 前高）/ 前高，
           超过上限即视为追高（默认 5%）。

    Returns:
        (是否通过, 说明文本)
    """
    cur_close = float(close.iloc[-1])
    cur_prev_high = float(prev_high.iloc[-1])
    if not np.isfinite(cur_prev_high) or cur_prev_high <= 0:
        return False, "前高数据不足"

    magnitude = cur_close / cur_prev_high - 1

    # ① 新鲜度：从最近一根往前找"发生突破"的那一根（收盘 > 当时的前高）
    if config.BREAKOUT_REQUIRE_FRESH:
        n = max(1, int(config.BREAKOUT_CONFIRM_BARS))
        broke_at = None
        for k in range(1, n + 1):
            if len(close) < k + 1 or len(prev_high) < k + 1:
                break
            c_k = close.iloc[-k]
            h_k = prev_high.iloc[-k]
            if np.isfinite(h_k) and c_k > h_k:
                broke_at = k
                break
        if broke_at is None:
            return False, (f"突破不新鲜：最近{n}根内无收盘价站上前高"
                           f"（突破幅度 {magnitude * 100:+.2f}%），属高位徘徊而非买点")

    # ② 幅度上限
    max_pct = getattr(config, "BREAKOUT_MAX_PCT", None)
    if max_pct is not None and 0 < max_pct < 1:
        if magnitude > max_pct:
            return False, (f"突破幅度 {magnitude * 100:+.2f}% 超过上限 {max_pct * 100:.1f}%"
                           f"（前高 {cur_prev_high:.2f} → 现价 {cur_close:.2f}），属追高")

    fresh_note = f"，最近{config.BREAKOUT_CONFIRM_BARS}根内确认" if config.BREAKOUT_REQUIRE_FRESH else ""
    return True, (f"突破前{config.BREAKOUT_WINDOW}日高点 {cur_prev_high:.2f}"
                  f"（幅度 {magnitude * 100:+.2f}%{fresh_note}）")


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
        # 突破质量过滤：区分"刚突破"与"已追高"。
        # 未通过时**不直接判死** —— 可能仍满足回踩条件（如突破后回落至均线），
        # 故继续往下走回踩分支，并在最终说明里保留追高原因。
        bq_ok, bq_msg = _check_breakout_from_above(close, prev_high, config)
        if bq_ok:
            return True, bq_msg
        breakout_reject_msg = bq_msg
    else:
        breakout_reject_msg = None

    # —— 回踩判定：增强模式 or 兼容模式 ——
    if config.USE_PULLBACK_ENHANCE:
        result = check_pullback_signal(df, config)
        if result["is_pullback"]:
            return True, result["details"]
        if breakout_reject_msg:
            # 突破被质量过滤拦下、回踩也不满足 → 把两条原因都说明，便于定位
            return False, f"{breakout_reject_msg}；回踩也不满足（{result['details']}）"
        return False, f"无突破或回踩信号（短线模式：{result['details']}）"
    else:
        ma20 = calc_ma(close, config.MA_PERIODS[2])
        cur_ma20 = ma20.iloc[-1]
        pullback = abs(cur_close - cur_ma20) / cur_ma20 < config.PULLBACK_THRESHOLD
        if pullback:
            deviation = abs(cur_close - cur_ma20) / cur_ma20 * 100
            return True, f"回踩MA{config.MA_PERIODS[2]} (偏离 {deviation:.2f}%)"
        if breakout_reject_msg:
            return False, f"{breakout_reject_msg}；也未回踩MA{config.MA_PERIODS[2]}"
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
    trigger_hits: list[str] = []       # 触发信号（决定 B 是否通过）
    confirm_hits: list[str] = []       # 确认信号（不单独放行，仅供人工参考）
    trigger_hits_suppressed: list[str] = []   # 命中但因白名单被拦下的触发源（归因用）
    vetoed = False                     # 被闸门否决

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

    # —— 触发源白名单（MOMENTUM_TRIGGERS）——
    # 只影响"能否把 B 放行"，不影响 signals 记录（否则会破坏诊断列语义）。
    # 动机见 config.MOMENTUM_TRIGGERS：配对回测显示 成交量放大 与 RSI超卖 是负增量的全部来源。
    _allowed = {str(t).strip().upper() for t in
                (config.MOMENTUM_TRIGGERS or []) if str(t).strip()}
    _allow_macd = "MACD" in _allowed
    _allow_rsi = "RSI_OVERSOLD" in _allowed
    _allow_vol = "VOLUME_SURGE" in _allowed

    # —— MACD 金叉 / 柱线翻红 ——
    # 注意：hist = (DIF − DEA) × 2，所以"柱线翻红"与"DIF 上穿 DEA（金叉）"在数学上**恒等**，
    # 两者永远是同一根 K 线触发（实测样本数完全相同：802 vs 802）。
    # 它们是同一个信号，不应算作两个独立信号（此前"四信号 OR"实为三个）。
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
            # trigger_hits 始终记录（诊断列口径不变）；stock_passed 受白名单约束
            trigger_hits.append("MACD金叉")
            if not _allow_macd:
                trigger_hits_suppressed.append("MACD金叉")
            else:
                stock_passed = True

    # —— MACD 柱线翻红（与金叉恒等，保留输出以便与历史习惯一致）——
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
            _debug(f"     （与'金叉'数学恒等：hist=(DIF−DEA)×2，恒为同一根触发，不重复计数）", True)
        if macd_red:
            # 与金叉是同一信号 → 不重复 append 进 trigger_hits
            if "MACD金叉" not in trigger_hits:
                trigger_hits.append("MACD柱线翻红")
                if not _allow_macd:
                    trigger_hits_suppressed.append("MACD柱线翻红")
            signals.append("MACD柱线翻红")
            if _allow_macd:
                stock_passed = True

    # —— RSI：从"独立触发"降级为"确认信号 + 可选否决闸门" ——
    cur_rsi = rsi.iloc[-1]
    rsi_tag = "超卖" if cur_rsi < config.RSI_OVERSOLD else ("超买" if cur_rsi >= config.RSI_OVERBOUGHT else "中性")
    logger.info("[动量排查] RSI: 当前=%.2f 超卖阈值=%.1f 超买阈值=%.1f → %s",
                cur_rsi, config.RSI_OVERSOLD, config.RSI_OVERBOUGHT, rsi_tag)
    if dbg:
        mark = "✅" if rsi_tag == "超卖" else ("⚠️" if rsi_tag == "超买" else "➖")
        _debug(f"─── RSI ───", True)
        _debug(f"  {mark} 当前RSI={cur_rsi:.2f}  超卖<{config.RSI_OVERSOLD}  超买≥{config.RSI_OVERBOUGHT}  → {rsi_tag}", True)
    if cur_rsi < config.RSI_OVERSOLD:
        if config.MOMENTUM_RSI_REQUIRE_CONFIRM:
            # 需 MACD/放量确认：先记为待确认，等放量判定后再决定
            confirm_hits.append(f"RSI超卖 ({cur_rsi:.1f})")
            signals.append(f"RSI超卖 ({cur_rsi:.1f})")
            if dbg:
                _debug("  ⏳ RSI超卖需 MACD/放量 确认才计入通过"
                       "（MOMENTUM_RSI_REQUIRE_CONFIRM=True）", True)
        else:
            signals.append(f"RSI超卖 ({cur_rsi:.1f})")
            trigger_hits.append(f"RSI超卖 ({cur_rsi:.1f})")
            if not _allow_rsi:
                trigger_hits_suppressed.append(f"RSI超卖 ({cur_rsi:.1f})")
            else:
                stock_passed = True
    elif cur_rsi >= config.RSI_OVERBOUGHT:
        signals.append(f"RSI超买 ({cur_rsi:.1f})")
        if config.MOMENTUM_RSI_VETO_OVERBOUGHT:
            vetoed = True
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
            # trigger_hits 始终记录（诊断列口径不变）；stock_passed 受白名单约束。
            # 注意：即便被白名单拦下，成交量放大**仍可作为 RSI超卖的确认信号**，
            # 因为下方 rsi_confirmed 只看 trigger_hits 是否非空。
            trigger_hits.append(f"成交量放大 ({vol_ratio:.1f}倍)")
            if not _allow_vol:
                trigger_hits_suppressed.append(f"成交量放大 ({vol_ratio:.1f}倍)")
            else:
                stock_passed = True
    else:
        logger.info("[动量排查] 成交量放大: 均量=0，无法计算量比 → 跳过")
        if dbg:
            _debug(f"─── 成交量放大 ───", True)
            _debug(f"  ➖ 均量为0，无法计算量比，跳过", True)

    # —— RSI 确认落地：触发信号成立后，RSI超卖才算"确认"（不再单独放行）——
    rsi_confirmed = False
    if confirm_hits and trigger_hits:
        rsi_confirmed = True
        confirm_hits = []      # 已被确认，不再是"未确认"
        for idx, s in enumerate(signals):
            if s.startswith("RSI超卖"):
                signals[idx] = s + " + 已被 MACD/放量 确认"
                break
    if dbg and confirm_hits:
        _debug("  ⚠️ RSI超卖未被 MACD/放量 确认 → 不计入维度B通过（避免急跌接刀）", True)

    # —— 最终 passed ——
    # 触发条件：有 MACD/放量 触发（若关了 RSI 确认要求，RSI超卖也能触发）
    # 闸门：RSI超买且开启否决时，一票否决
    passed = stock_passed and market_passed and not vetoed
    if vetoed and stock_passed:
        signals.append("🚫 虽有触发信号，但 RSI 超买（追高风险），维度B 被否决")
    # 归因：命中却被白名单拦下的触发源必须显式说明，否则"B 为何没过"无法解释
    if trigger_hits_suppressed and not stock_passed:
        signals.append(
            f"⛔ 触发源被 MOMENTUM_TRIGGERS 白名单拦下：{', '.join(trigger_hits_suppressed)}"
            f"（当前白名单={list(config.MOMENTUM_TRIGGERS)})")
        logger.info("[动量排查] 白名单拦下触发源: %s (白名单=%s)",
                    trigger_hits_suppressed, config.MOMENTUM_TRIGGERS)

    logger.info("[动量排查] 结果: 触发信号=%s 确认信号=%s 大纲=%s 最终passed=%s (信号数=%d)",
                trigger_hits or "无", confirm_hits or "无",
                "通过" if market_passed else "不通过",
                "通过" if passed else "不通过", len(signals))
    if dbg:
        _debug(f"─── 汇总 ───", True)
        _debug(f"  触发信号: {', '.join(trigger_hits) if trigger_hits else '无'}", True)
        if config.MOMENTUM_RSI_REQUIRE_CONFIRM:
            _debug(f"  确认信号: {'RSI超卖已被确认' if rsi_confirmed else '无'}", True)
        if config.ENABLE_MARKET_FILTER:
            _debug(f"  大盘环境: {'✅ 通过' if market_passed else '❌ 不通过'}", True)
        _debug(f"  最终结果: {'✅ 通过' if passed else '❌ 不通过'}", True)
        _debug("══════ 排查结束 ══════", True)

    # 当大盘未通过但个股有信号时，给出友好提示（附加大盘警告信息到信号列表末尾，用户一眼能看出被"一票否决"）
    if config.ENABLE_MARKET_FILTER and (not market_passed) and stock_passed:
        signals.append("ℹ️  个股有动量信号，但大盘环境不佳，建议观望等待大盘转暖。")

    return passed, signals


def _calc_stop_price(df: pd.DataFrame, cur_price: float, config: TradingConfig) -> tuple:
    """根据 STOP_MODE 计算止损价，并施加"最小止损距离"保护。

    返回 (止损价, 保护详情 dict)。详情含 raw_stop / floor / atr 等，供输出与图表展示。

    退化止损问题：`swing` 模式取「近 RR_WINDOW 日最低」，当标的刚创阶段新低就反弹时，
    该低点几乎等于现价 → 亏损空间趋近 0 → 盈亏比虚高（实测黄金ETF 137:1）、
    仓位公式算出满仓。这里给止损加一个下限距离，保证再近也有 0.5×ATR / 0.3% 的缓冲。
    """
    if config.STOP_MODE == "swing":
        low = df["low"]
        raw_stop = float(low.tail(config.RR_WINDOW).min())
    elif config.STOP_MODE == "near_low":
        # 近端结构止损：取"近 NEAR_LOW_WINDOW 日最低"与"现价 − 上限比例"中**离现价更近**者。
        # 用途：短线突破/回踩交易不该用 20 日低点那么远的结构支撑
        # （实测 002119：20日低距现价 −26.7%，而止盈空间只有 +9.1% → 盈亏比 0.34:1）。
        low = df["low"]
        near = float(low.tail(max(1, int(config.NEAR_LOW_WINDOW))).min())
        cap = cur_price - config.NEAR_LOW_MAX_PCT * cur_price
        raw_stop = max(near, cap)
    elif config.STOP_MODE == "swing_atr":
        # 波段(几天~两周)自适应止损：**按波动率定宽度，而不是找最近的结构位**。
        #
        # 关键教训（300 只 × 10 年回测，34741 条信号）：
        #   · 止损距离 2% ≈ 0.6×ATR → 被扫率 **82%**；5% ≈ 1.4×ATR → 58%
        #   · 10% ≈ 2.8×ATR → 30%；12% ≈ 3.3×ATR → 23%
        #   · 而「近10日最低」这类近端结构位平均只在现价下方 1~2%
        #     ⇒ 用 min(结构位, ATR止损) 会取到那个 1%，等于把止损设在噪声里
        #
        # 故以 **ATR 宽度为基准**，结构位只有在"明显更宽"时才采用它：
        #   结构位比 ATR 止损更远 ≥ STRUCT_ATR_SLACK × ATR → 用结构位（尊重真实支撑）
        #   否则（结构位贴着现价，多半是噪声）→ 用 ATR 止损
        # 注意不能用 min/max 直接取：max() 会在"结构位略宽"时取到它，仍偏紧；
        # min() 会在"结构位很宽"时丢弃真实支撑。故用带松紧带的条件判断。
        low = df["low"]
        near = float(low.tail(max(1, int(config.SWING_ATR_STRUCT_WINDOW))).min())
        atr = calc_atr(df, config.ATR_PERIOD)
        cur_atr = float(atr.iloc[-1]) if len(atr) and not pd.isna(atr.iloc[-1]) else None
        if cur_atr is None or cur_atr <= 0:
            # ATR 不可用（数据过短/全平）→ 退回固定百分比
            raw_stop = cur_price * (1 - config.FIXED_STOP_PCT)
        else:
            atr_stop = cur_price - config.SWING_ATR_STOP_MULT * cur_atr
            slack = config.SWING_ATR_STRUCT_SLACK_ATR * cur_atr
            # 结构位需"比 ATR 止损还远出 slack"才被认为是有效支撑
            raw_stop = near if near <= atr_stop - slack else atr_stop
            # 上限：止损不得比 MAX_PCT 更远（防结构位过远导致盈亏比崩掉）
            floor_cap = cur_price * (1 - config.SWING_ATR_MAX_PCT)
            raw_stop = max(raw_stop, floor_cap)
    elif config.STOP_MODE == "atr":
        atr = calc_atr(df, config.ATR_PERIOD)
        cur_atr = float(atr.iloc[-1])
        raw_stop = cur_price - config.ATR_STOP_MULT * cur_atr
    elif config.STOP_MODE == "fixed":
        raw_stop = cur_price * (1 - config.FIXED_STOP_PCT)
    else:
        raise ValueError(f"未知的 STOP_MODE: {config.STOP_MODE}")

    detail = {
        "raw_stop": raw_stop,
        "floor": None,
        "floor_components": None,
        "atr": None,
        "applied": False,
    }

    if not getattr(config, "STOP_MIN_DIST_ENABLED", False):
        return raw_stop, detail

    atr = calc_atr(df, config.ATR_PERIOD)
    cur_atr = float(atr.iloc[-1]) if len(atr) and not pd.isna(atr.iloc[-1]) else None
    if cur_atr is None or cur_atr <= 0:
        # ATR 不可用（数据太短/全平）→ 退回百分比下限
        cur_atr = None

    candidates = {}
    if cur_atr is not None:
        candidates["ATR"] = config.STOP_MIN_DIST_ATR_MULT * cur_atr
    if config.STOP_MIN_DIST_PCT > 0:
        candidates["PCT"] = config.STOP_MIN_DIST_PCT * cur_price

    detail["atr"] = cur_atr
    detail["floor_components"] = candidates
    if not candidates:
        return raw_stop, detail

    # 取更宽的候选（更保守）；用 max 比较距离大小，不比较价格
    floor_dist = max(candidates.values())
    floor_stop = cur_price - floor_dist
    detail["floor"] = floor_stop

    if raw_stop > floor_stop:
        # 原始止损离现价太近 → 下移到保护线
        detail["applied"] = True
        return floor_stop, detail
    return raw_stop, detail


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
    return {"swing": "结构派", "near_low": "近端结构派", "swing_atr": "波段自适应(ATR)",
            "swing_enhanced": "增强结构派", "swing_ultra": "超短线压力派",
            "atr": "波动率派", "fixed": "固定派"}.get(mode, mode)


def _print_stop_guard(c_info: dict) -> None:
    """当日志/输出中需要说明"止损保护已生效"时，打印一行提示。

    退化止损（止损位几乎等于现价 → 亏损空间趋近 0 → 盈亏比虚高）是隐形陷阱：
    用户看到漂亮盈亏比却不知道它来自一个 0.06% 的止损。这里显式说明下限来源。
    """
    d = c_info.get("stop_detail") or {}
    if not d.get("stop_min_dist_applied"):
        return
    raw = d.get("raw_stop")
    floor = d.get("stop_floor")
    comp = d.get("stop_floor_components") or {}
    parts = []
    if "ATR" in comp:
        parts.append(f"{comp['ATR']:.3f}(ATR下限)")
    if "PCT" in comp:
        parts.append(f"{comp['PCT']:.3f}(百分比下限)")
    src = " / ".join(parts) if parts else "-"
    raw_str = f"{raw:.2f}" if raw is not None else "-"
    floor_str = f"{floor:.2f}" if floor is not None else "-"
    print(f"  🛡️  止损保护生效: 原始止损 {raw_str} 距现价仅 "
          f"{((c_info['price'] - raw) / c_info['price'] * 100) if raw else 0:.2f}%"
          f"（过近，盈亏比会虚高）→ 已下移至 {floor_str}"
          f"（下限 {src}），最终止损距离 {d.get('stop_min_dist_pct', 0):.2f}%")


def check_dimension_c(df: pd.DataFrame, config: TradingConfig) -> tuple:
    """赔率维度：按配置模式计算止损止盈，判断盈亏比。"""
    close = df["close"]
    cur_price = close.iloc[-1]

    stop_loss, stop_detail = _calc_stop_price(df, cur_price, config)
    take_profit = _calc_profit_price(df, cur_price, stop_loss, config)

    loss_space = cur_price - stop_loss
    profit_space = take_profit - cur_price

    if loss_space <= 0:
        ratio = float("inf")
        passed = True
    else:
        ratio = profit_space / loss_space
        passed = ratio > config.MIN_RR_RATIO

    # 止损保护是否生效（用于输出提示；不影响 passed 判定口径）
    detail = {
        "raw_stop": stop_detail.get("raw_stop"),
        "stop_floor": stop_detail.get("floor"),
        "stop_floor_components": stop_detail.get("floor_components"),
        "stop_min_dist_applied": bool(stop_detail.get("applied")),
        "stop_min_dist_pct": ((cur_price - stop_loss) / cur_price * 100) if cur_price > 0 else 0.0,
    }

    return passed, {
        "price": cur_price,
        "stop": stop_loss,
        "take": take_profit,
        "ratio": ratio,
        "loss_space": loss_space,
        "profit_space": profit_space,
        "stop_mode": config.STOP_MODE,
        "profit_mode": config.PROFIT_MODE,
        "stop_detail": detail,
    }


def _breakout_ratio_needed(ratio: float) -> float:
    """给定盈亏比，算出"不亏所需的最低胜率"（期望值 = p×盈利 − (1−p)×亏损 = 0）。"""
    if not np.isfinite(ratio) or ratio <= 0:
        return float("nan")
    return 1.0 / (1.0 + ratio)


def _is_fresh_breakout(df: pd.DataFrame, config: TradingConfig) -> bool:
    """当前是否处于"刚突破前高"的结构状态（**只看事实，不评价质量**）。

    与 `_check_breakout_from_above` 的区别（两者都必要）：
        · 本函数回答"是不是一笔突破入场" → 供门控判断该不该套用"禁突破"规则
        · `_check_breakout_from_above` 回答"这笔突破质量好不好"（新鲜度 + 幅度上限）
    不能用一个代替另一个：实测 002119 突破幅度 +9.99% 被**质量**检查拒绝，
    但它确实是一笔突破入场 —— 若据此认为"非突破"，市场状态规则会自我豁免，少一道防线。
    """
    try:
        close = df["close"]
        high = df["high"]
        prev_high = high.shift(1).rolling(config.BREAKOUT_WINDOW).max()
        cur_close = float(close.iloc[-1])
        cur_prev_high = float(prev_high.iloc[-1])
        if not np.isfinite(cur_prev_high) or cur_prev_high <= 0:
            return False
        if cur_close <= cur_prev_high:
            return False
        if not config.BREAKOUT_REQUIRE_FRESH:
            return True
        n = max(1, int(config.BREAKOUT_CONFIRM_BARS))
        for k in range(1, n + 1):
            if len(close) < k + 1 or len(prev_high) < k + 1:
                break
            h_k = prev_high.iloc[-k]
            if np.isfinite(h_k) and close.iloc[-k] > h_k:
                return True
        return False
    except Exception:
        return False


def check_entry_gate(regime: dict, a_pass: bool, a_msg: str,
                     b_signals: list, c_pass: bool, c_info: dict,
                     df: pd.DataFrame, config: TradingConfig,
                     symbol: str = "") -> dict:
    """入口门控：把「市场状态 / RSI超买 / 赔率 / 生存性」四个否决项打通。

    为什么单开一层（而不是塞进 A/B/C）：
        `check_dimension_a/b/c` 的返回契约（`(bool, str)` / `(bool, dict)`）被
        backtest 与 6 个验证脚本共 20 余处依赖。改它们的通过语义会连坐整条测试链，
        而且会把"维度自身的判定"与"风控否决"混为一谈。
        这里把两者分开：**维度回答"信号成不成立"，门控回答"这笔能不能做"**。

    四条规则（各自可独立关闭，见 config）：
        ① REGIME_BREAKOUT：市场状态不允许突破时，否决"由突破放行"的 A
        ② RSI_EXTREME：RSI ≥ MOMENTUM_RSI_EXTREME（默认 80）→ 否决
        ③ RR_RATIO：C 未通过（盈亏比不足）→ 否决
        ④ SURVIVABILITY：低价 / 低流动性 → 否决（事前可得，见下方实现处的实测依据）

    Returns:
        dict: {
            'passed': bool,                 # 是否允许进场（无否决项）
            'vetoes': list[dict],           # [{'rule','detail'}]
            'rules': list[dict],            # 全部规则的评估结果（含通过的，便于对账）
            'p_required': float|nan,        # 该赔率下不亏所需胜率（提示用）
            'excluded': bool,               # 是否因市场状态不可用而跳过了 regime 规则
        }
    """
    rules: list = []
    vetoes: list = []

    # —— ① 市场状态：震荡市禁突破买入 ——
    regime_excluded = False
    if not config.ENTRY_GATE_REGIME_ENABLED:
        rules.append({"rule": "REGIME_BREAKOUT", "enabled": False,
                      "ok": True, "detail": "已关闭（ENTRY_GATE_REGIME_ENABLED=False）"})
    else:
        regime_name = (regime or {}).get("regime", "UNKNOWN")
        regime_ok = (regime or {}).get("available", False)
        # 入场性质：优先用 A 的说明（它知道自己是从哪个分支放行的）；
        # A 未通过时退回独立复算，避免"突破被幅度过滤拦下"导致规则自我豁免。
        is_breakout = (bool(a_pass) and str(a_msg).startswith("突破")) or _is_fresh_breakout(df, config)
        allowed = list(getattr(config, "ENTRY_GATE_BREAKOUT_ALLOWED_REGIMES", []))
        if not regime_ok:
            # 取数失败/数据不足时**跳过**该规则（不否决），并显式标注 ——
            # 绝不因取数失败而放行（也不因失败而误拦），让用户看到"这条没生效"。
            regime_excluded = True
            rules.append({"rule": "REGIME_BREAKOUT", "enabled": True, "ok": True,
                          "detail": f"市场状态不可用（{regime_name}），本规则已跳过"})
        elif not is_breakout:
            rules.append({"rule": "REGIME_BREAKOUT", "enabled": True, "ok": True,
                          "detail": f"非突破入场（{regime_name}），本规则不适用"})
        elif regime_name in allowed:
            rules.append({"rule": "REGIME_BREAKOUT", "enabled": True, "ok": True,
                          "detail": f"{regime_name} 允许突破买入"})
        else:
            detail = (f"市场状态 {regime_name}（{(regime or {}).get('label', '')}）"
                      f"不允许突破买入，而当前处于刚突破前高的状态")
            rules.append({"rule": "REGIME_BREAKOUT", "enabled": True, "ok": False, "detail": detail})
            vetoes.append({"rule": "REGIME_BREAKOUT", "detail": detail})

    # —— ② RSI 极端超买 ——
    if not config.ENTRY_GATE_RSI_ENABLED:
        rules.append({"rule": "RSI_EXTREME", "enabled": False,
                      "ok": True, "detail": "已关闭（ENTRY_GATE_RSI_ENABLED=False）"})
    else:
        cur_rsi = float(calc_rsi(df["close"], config.RSI_WINDOW).iloc[-1])
        if not np.isfinite(cur_rsi):
            rules.append({"rule": "RSI_EXTREME", "enabled": True, "ok": True,
                          "detail": "RSI 数据不足，本规则已跳过"})
        elif cur_rsi >= config.MOMENTUM_RSI_EXTREME:
            detail = (f"日线 RSI {cur_rsi:.1f} ≥ 极端超买线 {config.MOMENTUM_RSI_EXTREME:.0f}"
                      f"（突破追高中的情绪顶风险）")
            rules.append({"rule": "RSI_EXTREME", "enabled": True, "ok": False, "detail": detail})
            vetoes.append({"rule": "RSI_EXTREME", "detail": detail})
        else:
            rules.append({"rule": "RSI_EXTREME", "enabled": True, "ok": True,
                          "detail": f"RSI {cur_rsi:.1f} < {config.MOMENTUM_RSI_EXTREME:.0f}"})

    # —— ③ 赔率（复用 C 的判定，仅做归因，不重复计算）——
    ratio = c_info.get("ratio", float("nan"))
    ratio_str = "∞" if ratio == float("inf") else f"{ratio:.2f}"
    p_required = _breakout_ratio_needed(ratio)
    if not getattr(config, "ENTRY_GATE_RR_RATIO_ENABLED", True):
        # 关闭后 C❌ 不再被否决 → 保留 "C❌ 但门控放行" 的样本，
        # 使"C 有没有用"成为可测问题（对照 = signal 中的 C✅）。
        rules.append({"rule": "RR_RATIO", "enabled": False, "ok": True,
                      "detail": f"已关闭（ENTRY_GATE_RR_RATIO_ENABLED=False）；"
                                f"当前盈亏比 {ratio_str}:1，C通过={bool(c_pass)}"})
    elif c_pass:
        rules.append({"rule": "RR_RATIO", "enabled": True, "ok": True,
                      "detail": f"盈亏比 {ratio_str}:1 ≥ {config.MIN_RR_RATIO}:1"})
    else:
        detail = (f"盈亏比 {ratio_str}:1 < {config.MIN_RR_RATIO}:1"
                  + (f"（该赔率需胜率 > {p_required * 100:.1f}% 才不亏）"
                     if np.isfinite(p_required) else ""))
        rules.append({"rule": "RR_RATIO", "enabled": True, "ok": False, "detail": detail})
        vetoes.append({"rule": "RR_RATIO", "detail": detail})

    # —— ④ 生存性 / 可交易性（2026-09-30 加入）——
    # 依据（300只 × 2016-2026，10日、次日开盘入场，逐日配对）：
    #   不筛选         : -0.297%/10日 (t=-3.42, 显著负)
    #   加本否决后     : -0.056%/10日 (t=-0.61, 不显著)
    #   命中(被否决)组 : -0.746%/10日 (t=-4.27, 显著负)
    #   完美剔除退市票 : -0.100%/10日（事后才知道，不可实现）
    # 只用**当日可得**的日线派生量：收盘价、20日均成交额。复现：
    #   python scripts/find_survival_filter.py --min-amount 0.2 --min-price 3
    # 注意：本规则不产生 alpha，只降低尾部伤害；不要拿它当收益来源。
    if not getattr(config, "ENTRY_GATE_SURVIVABILITY_ENABLED", True):
        rules.append({"rule": "SURVIVABILITY", "enabled": False, "ok": True,
                      "detail": "已关闭（ENTRY_GATE_SURVIVABILITY_ENABLED=False）"})
    else:
        min_price = float(getattr(config, "MIN_ENTRY_PRICE", 3.0))
        min_amt = float(getattr(config, "MIN_ENTRY_AMOUNT", 2e7))
        cur_close = float(df["close"].iloc[-1]) if len(df) else float("nan")
        # ⚠️ 价格否决**不适用于 ETF**：ETF 单份净值常在 0.5~2 元区间，
        #    但它没有"面值退市"这回事（0.5 元的 ETF 不会因为价格低而退市）。
        #    实测踩过：自选里 8 只 ETF（0.63~1.56 元）被全部误杀。
        #    流动性否决对 ETF 仍有意义，故只跳过价格这一条。
        _skip_price = bool(symbol) and is_etf(symbol)
        # 均成交额需要**成交额**列；缓存日线没有 amount 列时退化为 close×volume。
        if "amount" in df.columns:
            amt_series = pd.to_numeric(df["amount"], errors="coerce")
        else:
            amt_series = (pd.to_numeric(df["close"], errors="coerce")
                          * pd.to_numeric(df["volume"], errors="coerce"))
        amt20 = float(amt_series.tail(20).mean()) if len(df) >= 5 else float("nan")
        if not np.isfinite(amt20) or amt20 <= 0:
            # 与 REGIME_BREAKOUT 同样处理：数据不足时**跳过**并显式标注，
            # 既不静默放行、也不误拦。
            rules.append({"rule": "SURVIVABILITY", "enabled": True, "ok": True,
                          "detail": "成交额数据不足，本规则已跳过（不否决）"})
        else:
            # ETF 跳过价格否决（无面值退市）；个股照常
            low_price = bool(not _skip_price and np.isfinite(cur_close)
                             and cur_close < min_price)
            low_amt = amt20 < min_amt
            if low_price or low_amt:
                bits = []
                if low_price:
                    bits.append(f"股价 {cur_close:.2f} < {min_price:g}元（低价/面值退市风险）")
                if low_amt:
                    bits.append(f"20日均成交额 {amt20 / 1e8:.2f}亿 < {min_amt / 1e8:.1f}亿（流动性不足）")
                detail = "；".join(bits)
                rules.append({"rule": "SURVIVABILITY", "enabled": True, "ok": False, "detail": detail})
                vetoes.append({"rule": "SURVIVABILITY", "detail": detail})
            else:
                tag = "ETF 免价格检查" if _skip_price else f"股价 {cur_close:.2f} ≥ {min_price:g}元"
                rules.append({"rule": "SURVIVABILITY", "enabled": True, "ok": True,
                              "detail": f"{tag} 且 "
                                        f"20日均成交额 {amt20 / 1e8:.2f}亿 ≥ {min_amt / 1e8:.1f}亿"})

    enabled_any = bool(config.ENTRY_GATE_ENABLED)
    passed = (not vetoes) if enabled_any else True
    return {
        "passed": passed,
        "vetoes": vetoes if enabled_any else [],
        "rules": rules,
        "p_required": p_required,
        "excluded": regime_excluded,
        "enabled": enabled_any,
    }


def _print_gate_result(gate: dict, config: TradingConfig) -> None:
    """打印门控结果（缺失时静默）。"""
    if not gate:
        return
    print("【入口门控 - 市场状态 / RSI超买 / 赔率 / 生存性】")
    if not gate.get("enabled"):
        print("  ⏭️  门控已关闭（ENTRY_GATE_ENABLED=False），仅按 A/B/C 判定")
        print()
        return
    for r in gate.get("rules", []):
        if not r.get("enabled"):
            print(f"  ➖ {r['rule']}: {r['detail']}")
        else:
            print(f"  {'✅' if r['ok'] else '🚫'} {r['rule']}: {r['detail']}")
    if gate.get("vetoes"):
        print(f"  ⇒ 被 {len(gate['vetoes'])} 条风控否决")
    else:
        print("  ⇒ 无风控否决")
    print()


def calc_position(c_info: dict, config: TradingConfig) -> dict:
    """根据最大单笔风险计算建议开仓数量。

    两个独立约束，取更小者：
        1. 风险约束：单笔最大亏损 = 总资金 × MAX_RISK_PER_TRADE
           → 股数 = 最大亏损额 / 每股亏损
        2. 资金约束：买不起就不成立 → 股数 ≤ 总资金 / 现价
    只做约束①时，若止损空间很小（0.05 元），会算出 20000 股 ≈ 17.6 万元，
    **超过总资金**——这种"建议"根本无法执行，属于隐形风险。
    """
    cur_price = c_info["price"]
    stop_loss = c_info["stop"]
    loss_per_share = cur_price - stop_loss

    if loss_per_share <= 0:
        return {"shares": 0, "note": "当前价低于止损价，不适合计算仓位"}
    if cur_price <= 0:
        return {"shares": 0, "note": "现价异常（≤0），无法计算仓位"}

    max_loss_amount = config.TOTAL_CAPITAL * config.MAX_RISK_PER_TRADE
    shares_by_risk = int(max_loss_amount / loss_per_share)

    # 资金约束：单手 100 股（A股/ETF 交易单位），向下取整到整手
    max_shares_by_capital = int(config.TOTAL_CAPITAL / cur_price)
    shares_raw = min(shares_by_risk, max_shares_by_capital)
    shares = (shares_raw // 100) * 100

    limited_by = None
    if shares_raw == max_shares_by_capital < shares_by_risk:
        limited_by = "capital"

    out = {
        "shares": shares,
        "shares_by_risk": shares_by_risk,
        "max_shares_by_capital": max_shares_by_capital,
        "position_value": shares * cur_price,
        "max_loss_amount": max_loss_amount,
        "loss_per_share": loss_per_share,
        "limited_by": limited_by,
    }
    if shares == 0:
        out["note"] = (f"按资金约束上限 {max_shares_by_capital} 股不足 1 手（100股），"
                       f"或止损空间过大导致风险仓为 0")
    return out


# ==================== 对比模式 ====================

def _evaluate_stock(symbol: str, config: TradingConfig, regime: dict | None = None) -> dict:
    """对单只股票执行完整判定（三维度 + 入口门控），返回结果字典（不打印）。

    供普通模式与对比模式共用：fetch_data → A/B/C → 门控 → 仓位。
    regime 由调用方注入（市场状态是全局的，只算一次）；未注入时门控会跳过 regime 规则。
    """
    df = fetch_data(symbol, config)
    a_pass, a_msg = check_dimension_a(df, config)
    b_pass, b_signals = check_dimension_b(df, config)
    c_pass, c_info = check_dimension_c(df, config)
    gate = check_entry_gate(regime or {}, a_pass, a_msg, b_signals, c_pass, c_info, df, config,
                            symbol=symbol)
    pos = calc_position(c_info, config)
    return {
        "symbol": symbol,
        "df": df,
        "a_pass": a_pass, "a_msg": a_msg,
        "b_pass": b_pass, "b_signals": b_signals,
        "c_pass": c_pass, "c_info": c_info,
        "gate": gate,
        "pos": pos,
        "latest_close": float(df["close"].iloc[-1]),
        "date_range": (
            f"{df['date'].iloc[0].strftime('%Y-%m-%d')} 至 "
            f"{df['date'].iloc[-1].strftime('%Y-%m-%d')}"
        ),
    }


def _overall_verdict(result: dict) -> tuple:
    """综合结论：三维度 + 入口门控。

    Returns:
        (all_pass: bool, failed_labels: list[str], veto_rules: list[str])
    """
    a_pass, b_pass = result["a_pass"], result["b_pass"]
    c_pass = result["c_pass"]
    gate = result.get("gate") or {}
    failed = [d for d, p in (("A", a_pass), ("B", b_pass), ("C", c_pass)) if not p]
    vetoes = gate.get("vetoes") or []
    all_pass = (not failed) and (not vetoes)
    return all_pass, failed, [v["rule"] for v in vetoes]


def _print_position(pos: dict, config: TradingConfig) -> None:
    """打印仓位建议（普通模式与对比模式共用）。"""
    print(f"  📐 总资金 {config.TOTAL_CAPITAL:,.0f} 元，单笔最大风险 {config.MAX_RISK_PER_TRADE*100:.1f}%")
    print(f"  每股亏损: {pos['loss_per_share']:.2f}，最大亏损额: {pos['max_loss_amount']:,.2f} 元")
    print(f"  ✅ 建议开仓: {pos['shares']:,} 股"
          + (f"（占用资金 {pos.get('position_value', 0):,.0f} 元）" if pos.get("position_value") else ""))
    if pos.get("limited_by") == "capital":
        print(f"  ⚠️  已按【资金约束】截断: 纯风险仓位需 {pos['shares_by_risk']:,} 股，"
              f"但总资金只买得起 {pos['max_shares_by_capital']:,} 股（已取整到整手 100 股）。")
        print(f"     含义：该标的止损空间太小（{pos['loss_per_share']:.2f} 元），"
              f"按 1% 风险算出的仓位会超过总资金 —— 属于隐形杠杆，务必按截断后的股数下单。")


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
    _print_stop_guard(c_info)

    # —— 入口门控（市场状态 / RSI超买 / 赔率）——
    gate = result.get("gate") or {}
    print()
    _print_gate_result(gate, config)

    all_pass, failed, veto_rules = _overall_verdict(result)

    # —— 仓位建议：只有综合通过才给出股数，否则明确"不适用" ——
    # 动因（实测 002119）：C 未通过时仍打印"✅ 建议开仓: 100 股"，
    # 虽然结论行写了"暂不建议开仓"，但股数出现在结论之前，极易被误读。
    print("【仓位建议】")
    if all_pass and pos["shares"] > 0:
        _print_position(pos, config)
        _print_weekly_position_hint(result, config)
    else:
        reasons = []
        if failed:
            reasons.append(f"维度 {'/'.join(failed)} 未通过")
        if veto_rules:
            reasons.append(f"风控否决 {'/'.join(veto_rules)}")
        print(f"  🚫 不适用 —— {'；'.join(reasons) if reasons else '未通过综合条件'}")
        print(f"     （C 口径下的参考仓位为 {pos.get('shares', 0):,} 股，仅供参考，不建议按此下单）"
              if pos.get("shares", 0) > 0 else "     （当前无可执行仓位）")
    print()

    # —— 总结 ——
    print("=" * 60)
    if all_pass:
        print("✅ 三维度均通过、无风控否决，建议开仓。")
    else:
        head = []
        if failed:
            head.append(f"维度 {'/'.join(failed)} 未通过")
        if veto_rules:
            head.append(f"风控否决 {len(veto_rules)} 项")
        print(f"❌ 暂不建议开仓 —— {'；'.join(head)}")
        for v in gate.get("vetoes", []):
            print(f"     · {v['detail']}")
    print("=" * 60)

    # —— 维度D：周线趋势背景（独立观察哨，只读，不影响 A/B/C 总分）——
    if show_weekly and _WEEKLY_AVAILABLE:
        try:
            weekly_result = check_weekly_background(result["symbol"], config)
            result["weekly"] = weekly_result
            print()
            print_weekly_result(weekly_result)
            _print_weekly_divergence_hint(weekly_result, result, config)
        except Exception as e:
            # 双保险：维度D 任何异常都不得影响 A/B/C 已打印的结论
            print()
            print("【维度D - 周线趋势背景（独立观察哨）】")
            print(f"  ⚠️  周线观察执行异常：{e}")
            print()


_MOMENTUM_SIGNAL_KEYWORDS = ["MACD金叉", "MACD柱线翻红", "RSI超卖", "成交量放大"]


def _weekly_ma60_distance(weekly_result: dict) -> float | None:
    """从周线结果里取「价格距周 MA60」的百分比（不可用时返回 None）。"""
    try:
        facts = (weekly_result or {}).get("facts") or {}
        return facts.get("price_vs_ma60", {}).get("distance_pct")
    except Exception:
        return None


def _print_weekly_position_hint(result: dict, config: TradingConfig) -> None:
    """周线顺风但价格已远离 MA60 → 给出仓位折扣建议（提示式，不改算出的股数）。"""
    dist = _weekly_ma60_distance(result.get("weekly") or {})
    if dist is None:
        return
    if abs(dist) >= config.WEEKLY_FAR_FROM_MA60_PCT:
        disc = config.WEEKLY_FAR_POSITION_DISCOUNT
        print(f"  ⚠️  周线顺风但价格距周MA60 {dist:+.1f}%（≥{config.WEEKLY_FAR_FROM_MA60_PCT:.0f}%），"
              f"均值回归风险偏高 → 建议仓位按 {disc:.0%} 折执行"
              f"（约 {int(result['pos'].get('shares', 0) * disc):,} 股）")


def _print_weekly_divergence_hint(weekly_result: dict, result: dict,
                                  config: TradingConfig) -> None:
    """周线顺风 + 日线极端超买 → 明确提示日周背离，防止"周线A 就敢追高"。"""
    if not weekly_result.get("available"):
        return
    if weekly_result.get("rating") != "A":
        return
    try:
        cur_rsi = float(calc_rsi(result["df"]["close"], config.RSI_WINDOW).iloc[-1])
    except Exception:
        return
    dist = _weekly_ma60_distance(weekly_result)
    overbought = np.isfinite(cur_rsi) and cur_rsi >= config.MOMENTUM_RSI_EXTREME
    far = dist is not None and abs(dist) >= config.WEEKLY_FAR_FROM_MA60_PCT
    if overbought or far:
        bits = []
        if overbought:
            bits.append(f"日线 RSI {cur_rsi:.1f} 极端超买")
        if far:
            bits.append(f"价格距周MA60 {dist:+.1f}%（远离）")
        print(f"  ⚠️  日周背离提示: 周线评级A（顺风），但 {'、'.join(bits)}。")
        print(f"      '周线顺风'不等于'日线可以追高' —— 顺风只提高胜率，不改变入场位置的风险。")


def _print_regime_block(config: TradingConfig, show_regime: bool = True,
                        regime_result: dict | None = None,
                        gate: dict | None = None) -> dict | None:
    """打印维度E（市场状态雷达）区块。

    容灾：任何异常都不得影响 A/B/C/D 已打印的结论。
    市场状态是全局的（指数级），与具体个股无关，故在对比模式下只打印一次。

    regime_result 由调用方注入（主流程在维度判定**之前**算好一次，供门控与展示共用）；
    未注入时自行计算（兼容既有调用）。返回实际使用的 regime 结果，便于调用方复用。
    """
    if not show_regime or not _REGIME_AVAILABLE:
        if not _REGIME_AVAILABLE:
            print()
            print("【维度E - 市场状态雷达（只读，不参与 A/B/C 判定）】")
            print(f"  ⚠️  regime_detector 模块加载失败，已跳过：{_REGIME_IMPORT_ERROR}")
            print()
        return regime_result
    try:
        if regime_result is None:
            regime_result = detect_regime(config)
        print()
        print_regime_result(regime_result)
        return regime_result
    except Exception as e:
        # 双保险：维度E 任何异常都不得影响其他维度已打印的结论
        print()
        print("【维度E - 市场状态雷达（只读，不参与 A/B/C 判定）】")
        print(f"  ⚠️  市场状态检测执行异常：{e}")
        print()
        return regime_result

def _print_factor_inputs(df: pd.DataFrame, config: TradingConfig,
                         a_pass: bool = False, b_pass: bool = False,
                         c_pass: bool = False, gate_passed: bool = False,
                         symbol: str = "") -> None:
    """打印双因子原料与判读卡格位（**只显示**，不参与任何判定）。

    为什么只给原料、不给分数：
        分数是同日**横截面百分位**，单只标的没有横截面可排
        （见 score_double.rank_and_score 的 <3 只不给分）。要看分数请用批量命令
        scripts/rank_double_factor.py。

    零风险约定（改这里请一并遵守）：
        · 纯打印：不修改任何状态、不影响退出码、不参与 A/B/C/D/E 任何判定
        · 整个函数体包在 try/except 里，任何异常都静默返回
        · 输出**不得包含「建议开仓」四字** —— scan_leaders_chart.sh 用
          `grep -E "建议开仓" | tail -1` 取总结行做分档归档。
          实测：单股模式全输出恰好 1 行含该词（总结行）；对比模式 2 行
          （= 两只票各自的结论行，**既有行为、与本函数无关**；scan 脚本只用
          单股模式，故不受影响）。本函数贡献 0 行。
    """
    try:
        import score_double as _S
        f = _S.features_from_df(df, config)
        r = _S.interpret(f["atr_pct"], f["dev_ma20"])
        in_pool = bool(a_pass and b_pass and c_pass and gate_passed)

        print()
        print(f"【双因子原料（只显示，不参与任何判定）{(' — ' + symbol) if symbol else ''}】")
        is_fund = bool(symbol) and is_etf(symbol)
        print(f"  个股 ATR%:  {f['atr_pct']:>6.2f}%   [{r['atr_band']}]")
        print(f"  距MA20%:   {f['dev_ma20']:>+7.2f}%   [{r['dev_band']}]")
        if is_fund:
            # 判读卡的样本是 300 只**个股**（universe 只含 6 个股票代码段，0 只 ETF）。
            # 对 ETF 套用个股的波动/偏离分位没有依据，故整卡不适用。
            print("  判读卡格位: ⛔ **本标的是 ETF，判读卡不适用**")
            print("     · 判读卡在 300 只**个股**上测得（样本内不含任何 ETF）")
            print("     · ETF 的净值、波动、偏离分布与个股不同，套用分位没有依据")
            print("     · 上表两个数值仅作事实展示，不构成任何判读")
        else:
            print(f"  判读卡格位: {r['cell']}  →  实测 {r['excess']:+.3f}pp/10日 "
                  f"(t={r['t']:+.2f}, n={r['n']})  {r['verdict']}")
            # 边界脆弱性：硬分档必然有"差一点就翻格"的毛病，必须显式告知，
            # 否则会把一个由 0.1% 股价波动决定的数字当成确定结论。
            for _n in r.get("near_boundary", []):
                print(f"  ⚠️ 格位不稳：{_n['axis']} {_n['value']:.2f} 距分界 {_n['bound']:g} "
                      f"仅 {_n['dist']:.2f}pp（档宽 {_n['pct_of_band']:.0f}%）")
                print(f"     邻格「{_n['neighbor_cell']}」= {_n['neighbor_excess']:+.3f}pp "
                      f"(t={_n['neighbor_t']:+.2f}) —— 结论会反转 {_n['swing']:.2f}pp")
                print(f"     价格再动约 {abs(_n['bound'] - _n['value']):.2f}% 就会翻格；"
                      f"**不要把本行当作确定判读**")
            print("  判读顺序: 先看 ATR —— 低波那一整行实测全为负"
                  "（-1.350 / -0.341 / -0.237，|t| 均 >2.9），ATR 低时无论多超跌都不该买")
            if in_pool:
                print("  ✅ 当前处于「系统已触发」状态（A或B通过 且 C通过 且 门控放行），判读卡适用")
            else:
                print("  ⚠️ 判读卡仅在「系统已触发」时适用；当前未触发 → 上表仅供参考")
        print("  ⚠️ 样本内实测、幅度微小（最好格 +0.917pp vs 单笔噪声 8.72pp），"
              "不可单独作为决策依据")
    except Exception:
        return          # 显示失败绝不影响主流程


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
    """构建对比结论：风控否决数 → 维度通过数 → 盈亏比 → 动量信号数 → 结构信号。

    Returns:
        {"winner": result_dict, "loser": result_dict, "reasons": list[str]}
    """
    reasons = []

    # 0. 风控否决数（放在最前：被否决的标的即便维度通过数更多，也不该胜出 ——
    #    否则会出现"推荐一只市场状态明确禁止入场的票"）
    v1 = len((r1.get("gate") or {}).get("vetoes") or [])
    v2 = len((r2.get("gate") or {}).get("vetoes") or [])
    reasons.append(
        f"  0. 风控否决数：{r1['symbol']} ({v1} 项)  vs  {r2['symbol']} ({v2} 项)"
    )
    if v1 != v2:
        winner, loser = (r1, r2) if v1 < v2 else (r2, r1)
        reasons.append("  → 风控否决更少者胜出")
        return {"winner": winner, "loser": loser, "reasons": reasons}

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
    show_regime: bool = True,
    index_arg: str | None = None,
    field_sources: dict | None = None,
) -> int:
    """对比模式：两只股票三维度判定 + 对比图 + 结论。

    show_weekly=True 时，每只股票在 A/B/C 总结后追加维度D（周线观察哨）区块。
    show_regime=True 时，在对比结论前追加维度E（市场状态雷达）区块——
    市场状态是全局的（指数级），与个股无关，故只打印一次。

    基准指数说明：`--index auto` 会按**第一只**标的的相关性选定基准，
    该基准对两只标的通用（指数是全局的），并在需要时提示两者最优基准不同。
    """
    print("=" * 60)
    print(f"🔍 对比模式 - {symbol1} vs {symbol2}")
    print(f"   配置链: {' + '.join(profile_names)} ({type(config).__name__})")
    if overrides:
        print(f"   单字段覆盖: {len(overrides)} 项 ({'; '.join(overrides)})")
    print("=" * 60)
    print()

    # —— 评估第一只（基准选择需要它的行情，故在第一只取数后立即应用）——
    # 先跑三维度（不带 regime），待基准确定后再补算门控，保证门控与展示用的是同一基准。
    header1 = f"━━━ {symbol1}" + (f"  {name1}" if name1 else "") + " ━━━"
    print(header1)
    print("=" * 60)
    try:
        r1 = _evaluate_stock(symbol1, config)
        r1["name"] = name1
        # 基准选择需要标的行情 → 在第一只取数后立即应用（指数是全局的，两只共用）
        _apply_benchmark(symbol1, r1["df"], config, index_arg,
                         field_sources if field_sources is not None else {},
                         auto_etf=False, force_etf=False)
    except Exception as e:
        print(f"❌ {symbol1} 评估失败: {e}")
        return 1

    # —— 市场状态：基准确定后算一次，两只共用（进程内缓存保证指数只取一次）——
    regime_result: dict = {}
    if show_regime and _REGIME_AVAILABLE and config.ENABLE_REGIME_DETECTOR:
        try:
            regime_result = detect_regime(config)
        except Exception as e:
            print(f"  ⚠️  市场状态检测异常（门控将跳过该规则）：{e}")
            regime_result = {"available": False, "regime": "UNKNOWN", "error": str(e)}

    # —— 用市场状态补算两两的门控（regime 一致，确保可比）——
    for r in (r1,):
        r["gate"] = check_entry_gate(regime_result, r["a_pass"], r["a_msg"],
                                     r["b_signals"], r["c_pass"], r["c_info"],
                                     r["df"], config, symbol=symbol1)
    _print_stock_result(r1, config, show_weekly=show_weekly)
    print()

    # —— 评估第二只 ——
    header2 = f"━━━ {symbol2}" + (f"  {name2}" if name2 else "") + " ━━━"
    print(header2)
    print("=" * 60)
    try:
        r2 = _evaluate_stock(symbol2, config)
        r2["name"] = name2
        r2["gate"] = check_entry_gate(regime_result, r2["a_pass"], r2["a_msg"],
                                      r2["b_signals"], r2["c_pass"], r2["c_info"],
                                      r2["df"], config, symbol=symbol2)
        _print_stock_result(r2, config, show_weekly=show_weekly)
        # 提示：两只标的最优基准不同时，说明当前基准对第二只未必合适
        if index_arg == "auto" and _BENCHMARK_AVAILABLE:
            returns2 = r2["df"].set_index("date")["close"].pct_change()
            sel2 = select_benchmark(returns2, lookback=config.BENCHMARK_AUTO_LOOKBACK)
            if (not sel2.get("fallback")) and sel2["index"] != config.BENCHMARK_INDEX:
                print(f"  ℹ️  {symbol2} 的最优基准其实是 {sel2['index']}（{sel2['name']}，"
                      f"相关性 {sel2['corr']:+.2f}），与本次所用 {config.BENCHMARK_INDEX} 不同；")
                print(f"      基准是全局的，如需以第二只口径评估请单独运行它。")
                print()
    except Exception as e:
        print(f"❌ {symbol2} 评估失败: {e}")
        return 1
    print()

    # —— 维度E：市场状态雷达（全局信息，只打印一次；与门控同源）——
    _print_regime_block(config, show_regime=show_regime, regime_result=regime_result)
    # —— 双因子原料（只显示；与 A/B/C 并行观察）—— 两只都显示，便于对照
    for _rr, _sym in ((r1, symbol1), (r2, symbol2)):
        _print_factor_inputs(_rr["df"], config, _rr["a_pass"], _rr["b_pass"],
                             _rr["c_pass"], bool((_rr.get("gate") or {}).get("passed")),
                             symbol=_sym)

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
    parser.add_argument(
        "--no-regime",
        action="store_true",
        help=("跳过维度E（市场状态雷达）的显示。\n"
              "  维度E 默认开启且只读，不影响 A/B/C/D 任何判定；本开关仅控制是否打印市场状态区块。\n"
              "  用途：对账期人工核对'机器判定的市场状态'与'你眼里的行情'是否一致。"),
    )
    parser.add_argument(
        "--index",
        default=None,
        metavar="CODE|auto",
        help=("基准指数。默认 sh000001（上证综指）。\n"
              "  · 填指数代码（如 sz399006）→ 直接使用该指数；\n"
              "  · 填 auto → 按**过去 N 日收益率相关性**为该标的自动挑最相关的指数。\n"
              " ⚠️ 实测：个股与任何指数相关性仅 0.15~0.38（ETF 是 0.80~0.94），\n"
              "    且'按板块换基准'会选错一半，故 auto 是按相关性选、不是按板块套。\n"
              "  ETF 模式下会被标的指数映射覆盖。"),
    )
    parser.add_argument(
        "--etf",
        action="store_true",
        help=("ETF 模式：为 ETF 切换到标的指数并放宽偏严阈值。\n"
              "  ① 基准指数切到该 ETF 的**主题锚指数**（见 config.THEME_INDEX_RULES）：\n"
              "     维度B 大盘过滤 与 维度E 市场状态 都改看它 —— 给创业板ETF 看上证综指是看错盘。\n"
              "     行业/主题 ETF 的精确标的指数多不在 baostock 覆盖内，故按 **ETF 名称关键词**\n"
              "     选同主题的宽基指数做锚（如 512760 芯片ETF → 创业板指），输出里会标明近似性。\n"
              "     想改锚：编辑 config.THEME_INDEX_RULES / ETF_INDEX_OVERRIDE，或用 -o 覆盖。\n"
              "  ② 只放宽放量倍数 1.5 → 1.25（实测 >1.5 仅覆盖 3~7%% 的交易日，近乎永不触发）。\n"
              "     ⚠️ 早期版本还放宽过回踩偏离/缩量比/RSI超卖 —— 均已被实测推翻并移除。\n"
              "  可在任意配置档上叠加使用，也支持 -o 再覆盖单个字段。"),
    )
    parser.add_argument(
        "--kc50", action="store_true",
        help=("科创/半导体类主题的锚改用**科创50**（默认用创业板指）。\n"
              "  仅 ETF 模式生效；等价于 -o THEME_ANCHOR_KC=kc50。\n"
              "  为什么默认不是科创50：实测恐慌日效应 h=20 时创业板指 +0.97pp 优于\n"
              "  科创50 +0.58pp（且后者不显著、基日 2019-12-31 无法做双体制校验）；\n"
              "  但 h=3 时科创50 +1.26pp(p=.040) 优于创业板指 −0.16pp ——\n"
              "  所以**短持有期用 --kc50，默认持有期用创业板指**。"),
    )
    parser.add_argument(
        "--auto-etf",
        action="store_true",
        help=("按代码前缀自动识别 ETF 并进入 ETF 模式（等价于 AUTO_ETF_MODE=true）。\n"
              "  识别规则：51x/56x/58x/159 等基金代码段。属启发式判断，\n"
              "  输出会打印实际采用的标的指数，可再用 -o BENCHMARK_INDEX=... 纠正。"),
    )
    args = parser.parse_args(argv)
    if not args.profiles:
        args.profiles = ["default"]
    if not args.overrides:
        args.overrides = []
    # --kc50 复用既有的 -o override 机制（避免改动 15 元组签名）。
    # 时序安全：apply_overrides 在 _print_etf_kickoff（ETF 模式入口）之前执行。
    if getattr(args, "kc50", False):
        args.overrides.append("THEME_ANCHOR_KC=kc50")
    return (args.symbol, args.profiles, args.overrides, args.verbose,
            args.show_config, args.report, args.open, args.name, args.skip_all_fail, args.compare,
            args.no_weekly, args.no_regime, args.etf, args.auto_etf, args.index)


def _print_etf_kickoff(symbol: str, config: TradingConfig, compare_symbol: str | None,
                       force_etf: bool, auto_etf: bool, field_sources: dict) -> bool:
    """ETF 模式入口：决定是否启用、施加参数、打印可对账说明。

    必须在 profile 叠加 + `-o` 覆盖之后、取数之前调用，这样：
      - 维度B 大盘过滤 与 维度E 市场状态 会一起切到标的指数（同一视角）
      - `--show-config` 能显示出被 ETF 模式改过的字段及其来源

    对比模式下两只 ETF 的标的指数可能不同，而基准指数只有一个全局值：
    此时采用**第一只**的标的指数，并显式提示。

    Returns:
        bool: 是否启用了 ETF 模式
    """
    if not _ETF_AVAILABLE:
        if force_etf or auto_etf:
            print(f"⚠️  ETF 模块加载失败（{_ETF_IMPORT_ERROR}），ETF 模式已跳过")
        return False

    first = resolve_underlying(symbol)
    enabled = bool(force_etf or auto_etf or config.AUTO_ETF_MODE)
    if compare_symbol:
        second = resolve_underlying(compare_symbol)
    else:
        second = None

    # --auto-etf / AUTO_ETF_MODE：按前缀自动进入（仅当确实识别为 ETF）
    if not force_etf and (auto_etf or config.AUTO_ETF_MODE):
        if first["is_etf"] or (second and second["is_etf"]):
            enabled = True
        elif auto_etf:
            print(f"ℹ️  --auto-etf：{symbol} 未识别为 ETF 代码段，按普通标的处理。")
            return False

    if not enabled:
        return False

    if not first["is_etf"]:
        # 显式 --etf 但第一只不是 ETF：退到第二只（对比模式下有意义）
        if second and second["is_etf"]:
            first, second = second, first
        else:
            print(f"ℹ️  --etf：{symbol} 未识别为 ETF 代码段，本 ETF 模式无副作用（仅提示）。")

    # —— 主题定锚：取 ETF 名称（联网，带本地缓存）后重新解析 ——
    # 为什么放在这里：resolve_underlying 本身是纯函数，取名称有网络成本，
    # 所以**只在 ETF 模式确实启用后**才取，避免给个股白跑一次网络。
    # 名称用于按 config.THEME_INDEX_RULES 匹配主题锚，修正 reason='sector' 的占位锚。
    name1 = ""
    if first["is_etf"]:
        name1 = fetch_etf_name(first["etf"])
        if name1:
            first = resolve_underlying(first["etf"], name=name1, config=config)
            print(f"  ℹ️  {first['etf']} 名称：{name1}")
        else:
            print(f"  ℹ️  {first['etf']} 名称获取失败（不影响主流程，主题锚不可用）")
    if second and second["is_etf"]:
        _n2 = fetch_etf_name(second["etf"])
        if _n2:
            second = resolve_underlying(second["etf"], name=_n2, config=config)

    plan = describe_plan(first["etf"], config, name=name1)
    applied = apply_etf_overrides(config, first["etf"], name=name1)
    config.ETF_MODE_ENABLED = True
    for key in applied:
        field_sources[key] = "etf-mode"

    print_etf_plan(plan, applied=applied)

    # 对比模式提示：两只 ETF 标的指数不同
    if second and second["is_etf"] and second["index"] and second["index"] != first["index"]:
        print(f"  ⚠️  对比模式下两只 ETF 标的指数不同：{first['etf']}→{first['index']}"
              f"（{first['index_name']}） vs {second['etf']}→{second['index']}"
              f"（{second['index_name']}）。")
        print(f"      基准指数只能有一个全局值，本次采用第一只的 {first['index']}；")
        print(f"      如需以第二只口径评估，请单独运行第二只。")
        print()
    return True


def _print_effective_config(config: TradingConfig, field_sources: dict) -> None:
    """打印当前生效的全量配置 + 字段来源（--show-config）。"""
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
        src_tag = f"[{src}]"
        print(f"  {name:<{pad_name}}  {src_tag:<{pad_src + 2}}  {val_str}")
    print()
    print("=" * 60)
    print()


def _apply_benchmark(symbol: str, df: pd.DataFrame, config: TradingConfig,
                     index_arg: str | None, field_sources: dict,
                     auto_etf: bool, force_etf: bool) -> None:
    """按需设置基准指数（必须在拿到标的行情之后 —— 相关性选择依赖它）。

    三种来源，优先级从低到高：
      1. config 默认（sh000001）
      2. `--index CODE` 显式指定
      3. `--index auto` 按相关性自动选择（依赖 df 的收益率）
    ETF 模式（在 _print_etf_kickoff 里已切到标的指数）优先级最高 ——
    因为 ETF 的标的指数是事实，不该被相关性猜测覆盖。
    """
    ef = bool(config.ETF_MODE_ENABLED)
    if ef or not _BENCHMARK_AVAILABLE:
        return

    if index_arg == "auto" or (index_arg is None and config.BENCHMARK_AUTO_ENABLED):
        returns = df.set_index("date")["close"].pct_change()
        sel = select_benchmark(returns, lookback=config.BENCHMARK_AUTO_LOOKBACK)
        if not sel.get("fallback"):
            config.BENCHMARK_INDEX = sel["index"]
            field_sources["BENCHMARK_INDEX"] = "auto-index"
            print_benchmark_selection(sel)
    elif index_arg:
        config.BENCHMARK_INDEX = index_arg.strip()
        field_sources["BENCHMARK_INDEX"] = f"cli-index:{index_arg}"
        from regime_detector import _benchmark_cn
        _cn = _benchmark_cn(config.BENCHMARK_INDEX)
        print(f"  🎯 指定基准: {config.BENCHMARK_INDEX}"
              f"{('（' + _cn + '）') if _cn else ''}")


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
     auto_open, stock_name, skip_all_fail, compare_symbol, no_weekly, no_regime,
     force_etf, auto_etf, index_arg) = _parse_args(sys.argv[1:])

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

    # —— ETF 模式（在 profile 叠加 + -o 覆盖之后、取数之前统一施加）——
    _print_etf_kickoff(symbol, config, compare_symbol, force_etf, auto_etf, field_sources)

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
            show_regime=not no_regime,
            index_arg=index_arg,
            field_sources=field_sources,
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


    # —— 获取数据 ——
    df = fetch_data(symbol, config)

    # —— 基准指数（--index / auto 需要行情数据，故在取数之后）——
    _apply_benchmark(symbol, df, config, index_arg, field_sources, auto_etf, force_etf)

    # —— --show-config：放在基准选择之后，保证显示的 BENCHMARK_INDEX 与实际使用一致 ——
    if show_config:
        _print_effective_config(config, field_sources)

    date_range = (
        f"{df['date'].iloc[0].strftime('%Y-%m-%d')} 至 "
        f"{df['date'].iloc[-1].strftime('%Y-%m-%d')}"
    )
    latest_close = df["close"].iloc[-1]
    print(f"📊 数据范围: {date_range}")
    print(f"📈 最新收盘价: {latest_close:.2f}")
    print()

    # —— 维度E（市场状态）：在维度判定**之前**算一次 ——
    # 它现在有两个消费方：入口门控（决定是否否决突破买入）与展示区块。
    # 提前算 + regime_detector 内的进程内缓存 → 指数只取一次
    # （否则冷启动会取两遍，各 ~20s）。只读契约不变：它不修改 A/B/C 的任何判定。
    regime_result: dict = {}
    if _REGIME_AVAILABLE and config.ENABLE_REGIME_DETECTOR:
        try:
            regime_result = detect_regime(config)
        except Exception as e:
            print(f"  ⚠️  市场状态检测异常（门控将跳过该规则）：{e}")
            regime_result = {"available": False, "regime": "UNKNOWN", "error": str(e)}

    # —— 维度D：周线趋势背景（独立观察哨，只读，不影响 A/B/C 总分）——
    # 提前到门控之前计算：门控与仓位提示都要用它的"评级 / 距MA60"做参考，
    # 但它的结论**不参与** A/B/C 判定（保持只读契约）。
    # 即便维度D 模块加载失败/周线接口超时/数据不足，也绝不影响 A/B/C 已得出的结论
    weekly_result: dict = {}
    if not no_weekly and _WEEKLY_AVAILABLE:
        try:
            weekly_result = check_weekly_background(symbol, config)
        except Exception as e:
            weekly_result = {"available": False, "error": f"周线观察执行异常：{e}"}

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
    _print_stop_guard(c_info)

    # —— 入口门控（市场状态 / RSI超买 / 赔率）——
    gate = check_entry_gate(regime_result, a_pass, a_msg, b_signals, c_pass, c_info, df, config,
                            symbol=symbol)
    print()
    _print_gate_result(gate, config)

    all_pass = a_pass and b_pass and c_pass and (not gate.get("vetoes"))

    # —— 仓位建议：只有综合通过才给出股数 ——
    pos = calc_position(c_info, config)
    print("【仓位建议】")
    if all_pass and pos["shares"] > 0:
        _print_position(pos, config)
        _print_weekly_position_hint({"pos": pos, "weekly": weekly_result}, config)
    else:
        reasons = []
        failed = [d for d, p in (("A", a_pass), ("B", b_pass), ("C", c_pass)) if not p]
        veto_rules = [v["rule"] for v in gate.get("vetoes", [])]
        if failed:
            reasons.append(f"维度 {'/'.join(failed)} 未通过")
        if veto_rules:
            reasons.append(f"风控否决 {'/'.join(veto_rules)}")
        print(f"  🚫 不适用 —— {'；'.join(reasons) if reasons else '未通过综合条件'}")
        if pos.get("shares", 0) > 0:
            print(f"     （C 口径下的参考仓位为 {pos['shares']:,} 股，仅供参考，不建议按此下单）")
    print()

    # —— 总结（三维度 + 风控门控）——
    print("=" * 60)
    if all_pass:
        print("✅ 三维度均通过、无风控否决，建议开仓。")
    else:
        failed = [d for d, p in (("A", a_pass), ("B", b_pass), ("C", c_pass)) if not p]
        head = []
        if failed:
            head.append(f"维度 {'/'.join(failed)} 未通过")
        if gate.get("vetoes"):
            head.append(f"风控否决 {len(gate['vetoes'])} 项")
        print(f"❌ 暂不建议开仓 —— {'；'.join(head)}")
        for v in gate.get("vetoes", []):
            print(f"     · {v['detail']}")
    print("=" * 60)

    # —— 维度E：市场状态（注入已算好的结果，展示与门控同源）——
    _print_regime_block(config, show_regime=not no_regime, regime_result=regime_result)
    # —— 双因子原料（只显示；与 A/B/C 并行观察）——
    _print_factor_inputs(df, config, a_pass, b_pass, c_pass,
                         bool(gate.get("passed")), symbol=symbol)

    # —— 维度D 展示（计算已在维度A 之前完成，此处只打印）——
    if not no_weekly and _WEEKLY_AVAILABLE:
        if weekly_result.get("available") or weekly_result.get("error"):
            print()
            print_weekly_result(weekly_result)
            if weekly_result.get("available"):
                _print_weekly_divergence_hint(weekly_result,
                                             {"df": df, "pos": pos}, config)
        else:
            print()
            print("【维度D - 周线趋势背景（独立观察哨）】")
            print(f"  ⚠️  weekly_observer 模块加载失败，已跳过：{_WEEKLY_IMPORT_ERROR}")
            print()
    elif not _WEEKLY_AVAILABLE:
        print()
        print("【维度D - 周线趋势背景（独立观察哨）】")
        print(f"  ⚠️  weekly_observer 模块加载失败，已跳过：{_WEEKLY_IMPORT_ERROR}")
        print()

    # —— 维度D 与 维度E 已在上方打印（D 计算提前到门控之前，E 与门控同源）——

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