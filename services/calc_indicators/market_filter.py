#!/usr/bin/env python3
"""大盘环境过滤器（维度 B 增强）。

判断当前大盘是否适合开仓个股，作为动量信号的前置拦截器。
职业交易员铁律："看大盘，做个股"——同一金叉信号在大盘暴涨时成功率极高，
在大盘暴跌时往往诱多。

判定逻辑（顺序执行）：
    1. 前置开关：ENABLE_MARKET_FILTER == False → 直接通过
    2. 数据获取：拉取基准指数近 N 日日线（efinance 优先，baostock 兜底）
    3. 数据不足保护：返回 passed=False
    4. 条件1 - 趋势判定：指数收盘价必须站上 MA{BENCHMARK_MA_PERIOD}
    5. 条件2 - 当日涨跌幅：必须 ≥ BENCHMARK_MIN_CHANGE_PCT
    6. 全部通过：返回 passed=True

错误降级：数据获取失败/超时/返回空 → passed=False（而非抛异常）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from config import TradingConfig

logger = logging.getLogger(__name__)


# ==================== 调试日志工具 ====================

def _debug(msg: str, enabled: bool = True) -> None:
    """打印调试日志到控制台（不受 Python logging 级别限制）。

    与 main.py 中的 _debug 风格一致，使用 🌐 标记大盘相关日志，
    便于在控制台中与回踩排查日志（📋）区分。
    """
    if enabled:
        print(f"  🌐 {msg}")


# ==================== 指数数据获取 ====================

def _suppress_output(func):
    """复用 main.py 的输出压制工具，避免 baostock 登录信息污染控制台。"""
    import io
    import os
    import sys
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


def _normalize_index_code(index_code: str) -> tuple:
    """将统一格式 'sh000001' 转换为 (ef_code, bs_code)。

    efinance 指数代码格式: '000001' (需通过 dataapi 调用) 或 'sh000001'
    baostock 指数代码格式: 'sh.000001'
    """
    code = index_code.strip().lower()
    # 输入 'sh000001' / 'sz399001' / 'sh.000001' 统一拆分
    if "." in code:
        market, num = code.split(".")
        market = market.replace("sh", "sh").replace("sz", "sz")
    elif code.startswith(("sh", "sz")):
        market, num = code[:2], code[2:]
    else:
        # 纯 6 位代码，按规则推断市场
        num = code
        market = "sh" if num.startswith(("000", "9")) else "sz"
    return f"{market}{num}", f"{market}.{num}"


def _fetch_index_efinance(ef_code: str, days: int) -> Optional[pd.DataFrame]:
    """使用 efinance 获取指数数据。"""
    try:
        import efinance as ef
        # efinance 的 get_quote_history 同时支持个股与指数代码
        df = ef.stock.get_quote_history(ef_code)
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
        logger.debug("efinance 获取指数 %s 失败: %s", ef_code, e)
        return None


def _fetch_index_baostock(bs_code: str, days: int) -> Optional[pd.DataFrame]:
    """使用 baostock 获取指数数据。"""
    try:
        import baostock as bs
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=days * 2 + 30)).strftime("%Y-%m-%d")
        # baostock 指数需用 query_history_k_data_plus + 频率 d
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,open,high,low,close,volume",
            start_date=start_date,
            end_date=end_date,
            frequency="d",
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
        logger.debug("baostock 获取指数 %s 失败: %s", bs_code, e)
        return None


def _fetch_index(index_code: str, days: int, timeout: int, dbg: bool = False) -> Optional[pd.DataFrame]:
    """获取指数日线数据：efinance 优先，baostock 兜底。带超时降级。

    Args:
        index_code: 指数代码，如 'sh000001'
        days: 拉取的天数
        timeout: 单数据源超时秒数
        dbg: 是否打印详细排查日志
    """
    ef_code, bs_code = _normalize_index_code(index_code)
    if dbg:
        _debug("══════ 大盘数据获取排查 ══════", True)
        _debug(f"输入指数代码: {index_code}  →  efinance: {ef_code}  |  baostock: {bs_code}", True)
        _debug(f"拉取天数: {days}  |  单源超时: {timeout}s", True)

    # —— efinance ——
    if dbg:
        _debug("─── 尝试 efinance ───", True)
    try:
        import signal as sig
        def _handler(signum, frame):
            raise TimeoutError("efinance 超时")
        sig.signal(sig.SIGALRM, _handler)
        sig.alarm(timeout)
        try:
            df = _suppress_output(lambda: _fetch_index_efinance(ef_code, days))
        finally:
            sig.alarm(0)
        if df is not None and not df.empty:
            logger.info("大盘指数数据来源: efinance (%s)", ef_code)
            if dbg:
                _debug(f"✅ efinance 成功: {len(df)} 根 K 线", True)
                _debug(f"   日期范围: {df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}", True)
                _debug(f"   最新收盘: {df['close'].iloc[-1]:.2f}", True)
            return df
        else:
            if dbg:
                _debug("❌ efinance 返回空数据", True)
    except Exception as e:
        if dbg:
            _debug(f"❌ efinance 失败: {type(e).__name__}: {e}", True)
        logger.info("efinance 获取指数失败: %s", e)

    # —— baostock 兜底 ——
    if dbg:
        _debug("─── 尝试 baostock 兜底 ───", True)
    try:
        import signal as sig
        def _handler2(signum, frame):
            raise TimeoutError("baostock 超时")
        sig.signal(sig.SIGALRM, _handler2)
        sig.alarm(timeout)

        def _bs_fetch():
            import baostock as bs
            bs.login()
            try:
                return _fetch_index_baostock(bs_code, days)
            finally:
                bs.logout()
        try:
            df = _suppress_output(_bs_fetch)
        finally:
            sig.alarm(0)
        if df is not None and not df.empty:
            logger.info("大盘指数数据来源: baostock (%s)", bs_code)
            if dbg:
                _debug(f"✅ baostock 成功: {len(df)} 根 K 线", True)
                _debug(f"   日期范围: {df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}", True)
                _debug(f"   最新收盘: {df['close'].iloc[-1]:.2f}", True)
            return df
        else:
            if dbg:
                _debug("❌ baostock 返回空数据", True)
    except Exception as e:
        if dbg:
            _debug(f"❌ baostock 失败: {type(e).__name__}: {e}", True)
        logger.info("baostock 获取指数失败: %s", e)

    if dbg:
        _debug("❌ 所有数据源均失败，返回 None", True)
    return None


# ==================== 大盘环境判定 ====================

def check_market_environment(config: TradingConfig) -> dict:
    """获取大盘指数数据，判断当前是否适合交易。

    Args:
        config: 配置对象，读取以下字段：
            - ENABLE_MARKET_FILTER: 是否启用大盘过滤
            - BENCHMARK_INDEX: 基准指数代码（如 'sh000001'）
            - BENCHMARK_MA_PERIOD: 趋势判定均线周期
            - BENCHMARK_MIN_CHANGE_PCT: 当日涨跌幅下限
            - BENCHMARK_FETCH_DAYS: 拉取指数的天数
            - BENCHMARK_FETCH_TIMEOUT: 获取超时秒数

    Returns:
        dict: {
            'passed': bool,          # True=环境安全，False=环境恶劣
            'reason': str,           # 详细说明
            'index_price': float,    # 指数最新价（无数据时为 0.0）
            'ma_value': float,       # 均线值（无数据时为 0.0）
            'change_pct': float      # 指数今日涨跌幅（无数据时为 0.0）
        }
    """
    dbg = config.MARKET_DEBUG

    # —— 前置开关 ——
    if not config.ENABLE_MARKET_FILTER:
        if dbg:
            _debug("══════ 大盘环境过滤排查 ══════", True)
            _debug("ENABLE_MARKET_FILTER = False，跳过大盘过滤，直接通过", True)
            _debug("══════ 排查结束 ✅ 过滤已关闭 ══════", True)
        logger.info("大盘过滤已关闭 (ENABLE_MARKET_FILTER=False)，直接通过")
        return {
            "passed": True,
            "reason": "大盘过滤已关闭",
            "index_price": 0.0,
            "ma_value": 0.0,
            "change_pct": 0.0,
        }

    if dbg:
        _debug("══════ 大盘环境过滤排查 ══════", True)
        _debug(f"基准指数: {config.BENCHMARK_INDEX}", True)
        _debug(f"趋势均线: MA{config.BENCHMARK_MA_PERIOD}  |  涨跌幅下限: {config.BENCHMARK_MIN_CHANGE_PCT:.2f}%", True)
        _debug(f"拉取天数: {config.BENCHMARK_FETCH_DAYS}  |  超时: {config.BENCHMARK_FETCH_TIMEOUT}s", True)

    # —— 数据获取 ——
    df = _fetch_index(
        config.BENCHMARK_INDEX,
        config.BENCHMARK_FETCH_DAYS,
        config.BENCHMARK_FETCH_TIMEOUT,
        dbg=dbg,
    )

    if df is None or df.empty:
        if dbg:
            _debug(f"❌ 数据获取失败，降级处理：返回 passed=False", True)
            _debug("══════ 排查结束 ❌ 数据获取失败 ══════", True)
        logger.info("大盘指数数据获取失败，降级处理：返回不通过")
        return {
            "passed": False,
            "reason": f"大盘指数 {config.BENCHMARK_INDEX} 数据获取失败，无法判断环境",
            "index_price": 0.0,
            "ma_value": 0.0,
            "change_pct": 0.0,
        }

    # —— 数据不足保护 ——
    min_len = config.BENCHMARK_MA_PERIOD + 5
    if dbg:
        _debug("─── 数据充足性检查 ───", True)
        _debug(f"  实际 K 线数: {len(df)}  |  最低需求: {min_len} (MA{config.BENCHMARK_MA_PERIOD} + 5)", True)
    if len(df) < min_len:
        if dbg:
            _debug(f"  ❌ 数据不足: {len(df)} < {min_len}", True)
            _debug("══════ 排查结束 ❌ 数据不足 ══════", True)
        logger.info("大盘指数数据不足: %d < %d", len(df), min_len)
        return {
            "passed": False,
            "reason": f"指数数据不足（仅 {len(df)} 根，需 ≥ {min_len}），无法判断环境",
            "index_price": float(df["close"].iloc[-1]) if len(df) > 0 else 0.0,
            "ma_value": 0.0,
            "change_pct": 0.0,
        }
    if dbg:
        _debug(f"  ✅ 数据充足: {len(df)} ≥ {min_len}", True)

    close = df["close"]
    cur_price = float(close.iloc[-1])
    prev_close = float(close.iloc[-2])
    cur_date = df["date"].iloc[-1].date()

    if dbg:
        _debug(f"  最新日期: {cur_date}", True)
        _debug(f"  最新收盘: {cur_price:.2f}  |  昨日收盘: {prev_close:.2f}", True)

    # —— 条件 1：趋势判定（站上 N 日均线）——
    if dbg:
        _debug("─── 条件 1：趋势判定 ───", True)
    ma_series = close.rolling(window=config.BENCHMARK_MA_PERIOD).mean()
    ma_value = float(ma_series.iloc[-1])
    trend_ok = cur_price >= ma_value

    if dbg:
        mark = "✅" if trend_ok else "❌"
        _debug(f"  {mark} 趋势检查: 指数 {cur_price:.2f} {'≥' if trend_ok else '<'} MA{config.BENCHMARK_MA_PERIOD} {ma_value:.2f}", True)

    if not trend_ok:
        reason = f"大盘跌破 MA{config.BENCHMARK_MA_PERIOD}（指数 {cur_price:.2f} < 均线 {ma_value:.2f}），环境偏空"
        if dbg:
            _debug(f"  ❌ 条件 1 不通过，环境偏空", True)
            _debug("══════ 排查结束 ❌ 趋势不通过 ══════", True)
        logger.info(reason)
        return {
            "passed": False,
            "reason": reason,
            "index_price": cur_price,
            "ma_value": ma_value,
            "change_pct": 0.0,
        }

    # —— 条件 2：当日涨跌幅 ——
    if dbg:
        _debug("─── 条件 2：当日涨跌幅 ───", True)
    if prev_close > 0:
        change_pct = (cur_price / prev_close - 1) * 100
    else:
        change_pct = 0.0
        if dbg:
            _debug(f"  ⚠️ 昨日收盘为 0，涨跌幅按 0% 处理", True)

    change_ok = change_pct >= config.BENCHMARK_MIN_CHANGE_PCT
    if dbg:
        mark = "✅" if change_ok else "❌"
        _debug(f"  {mark} 涨跌幅检查: 今日 {change_pct:+.2f}% {'≥' if change_ok else '<'} 阈值 {config.BENCHMARK_MIN_CHANGE_PCT:.2f}%", True)
        _debug(f"     计算: ({cur_price:.2f} / {prev_close:.2f} - 1) × 100 = {change_pct:+.2f}%", True)

    if not change_ok:
        reason = (f"大盘今日大跌 {change_pct:.2f}%（阈值 {config.BENCHMARK_MIN_CHANGE_PCT:.2f}%），"
                  f"逆势风险大")
        if dbg:
            _debug(f"  ❌ 条件 2 不通过，逆势风险大", True)
            _debug("══════ 排查结束 ❌ 涨跌幅不通过 ══════", True)
        logger.info(reason)
        return {
            "passed": False,
            "reason": reason,
            "index_price": cur_price,
            "ma_value": ma_value,
            "change_pct": change_pct,
        }

    # —— 全部通过 ——
    reason = (f"大盘环境安全（指数 {cur_price:.2f} ≥ MA{config.BENCHMARK_MA_PERIOD} "
              f"{ma_value:.2f}，今日 {change_pct:+.2f}% ≥ {config.BENCHMARK_MIN_CHANGE_PCT:.2f}%）")
    if dbg:
        _debug("  ✅✅ 全部条件通过！大盘环境安全", True)
        _debug(f"     指数: {cur_price:.2f}  |  MA{config.BENCHMARK_MA_PERIOD}: {ma_value:.2f}  |  今日: {change_pct:+.2f}%", True)
        _debug("══════ 排查结束 ✅ 大盘环境安全 ══════", True)
    logger.info(reason)
    return {
        "passed": True,
        "reason": reason,
        "index_price": cur_price,
        "ma_value": ma_value,
        "change_pct": change_pct,
    }
