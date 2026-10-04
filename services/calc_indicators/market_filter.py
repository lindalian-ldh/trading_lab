#!/usr/bin/env python3
"""大盘环境过滤器（维度 B 增强）。

判断当前大盘是否适合开仓个股，作为动量信号的前置拦截器。
职业交易员铁律："看大盘，做个股"——同一金叉信号在大盘暴涨时成功率极高，
在大盘暴跌时往往诱多。

判定逻辑（顺序执行）：
    1. 前置开关：ENABLE_MARKET_FILTER == False → 直接通过
    2. 数据获取：拉取基准指数近 N 日日线（**baostock / 腾讯双源；东财已禁用**）
    3. 数据不足保护：返回 passed=False
    4. 条件1 - 趋势判定：指数收盘价必须站上 MA{BENCHMARK_MA_PERIOD}
    5. 条件2 - 当日涨跌幅：必须 ≥ BENCHMARK_MIN_CHANGE_PCT
    6. 全部通过：返回 passed=True

错误降级：数据获取失败/超时/返回空 → passed=False（而非抛异常）。
"""

from __future__ import annotations

import atexit
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

# ==================== baostock 会话复用 ====================

_BS_LOGGED_IN: bool = False


def _ensure_bs_login() -> bool:
    """确保 baostock 已登录，并复用会话。返回是否可用。

    为什么必须复用：bs.login() 是一次到 baostock 登录服务器的往返，
    **新进程里实测要 ~15 秒**（同进程后续调用 0.2~3 秒）。
    原先每个取数函数都 login/logout 一遍，于是
      - 调用方超时必须设到 20s+ 才不至于被掐死（BENCHMARK_FETCH_TIMEOUT 默认才 5s，
        所以大盘过滤长期处于"取数失败 → 直接否决"的静默失效状态）；
      - 一次运行里 regime_detector 与 market_filter 各登录一遍，白等两次。
    这里改成进程内只登录一次，用 atexit 兜底登出。
    """
    global _BS_LOGGED_IN
    if _BS_LOGGED_IN:
        return True
    try:
        import baostock as bs
        if bs.login().error_code != "0":
            return False
        _BS_LOGGED_IN = True
        return True
    except Exception as e:  # pragma: no cover - 容灾兜底
        logger.debug("baostock 登录失败: %s", e)
        return False


def _release_bs_session() -> None:
    """进程退出时登出 baostock，避免留下 TCP 连接。

    注意：logout 会往 stdout 打 "logout success!"，而 atexit 时机已晚于
    各调用方的输出压制，所以这里必须自己压一次，否则会污染最后一行输出。
    """
    global _BS_LOGGED_IN
    if not _BS_LOGGED_IN:
        return

    def _logout():
        import baostock as bs
        bs.logout()

    try:
        _suppress_output(_logout)
    except Exception:  # pragma: no cover
        pass
    finally:
        _BS_LOGGED_IN = False


atexit.register(_release_bs_session)


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
    """将统一格式 'sh000001' 转换为 (tx_code, bs_code)。

    腾讯（akshare stock_zh_index_daily_tx）: 'sh000001'
    baostock:                              'sh.000001'

    ⚠️ 2026-10-03：东财（efinance）已从本模块移除（实测反复 ConnectionError，
    且对 000688 有静默取错标的的隐患）。返回的第一项原先叫 ef_code，
    现在语义是**腾讯源代码**（两者格式相同，故签名不变，下游无需改动）。
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


def _fetch_index_tencent(tx_code: str, days: int) -> Optional[pd.DataFrame]:
    """用 akshare 的**腾讯源**取指数日线（保留 sh/sz 前缀，无代码歧义）。

    为什么需要这个源（2026-10-01 加）：

      · **baostock 没有科创50** —— `sh.000688` 查询返回空。
      · **腾讯用显式 sh/sz 前缀，无代码歧义** —— 历史上 efinance 只认纯 6 位数字，
        `000688` 会被解析成**深市个股「国城矿业」**（科创50 应是 1000+ 点、个股是几元），
        可能**静默返回个股数据**，把市场状态建立在一只小盘股上。
        这是比取数失败更糟的失效模式，也是**东财于 2026-10-03 被整体移除**的原因之一。
      · **腾讯是非东财供应商** —— 东财通道（efinance / akshare 的 `*_em`）在本机
        反复 `ConnectionError: RemoteDisconnected`，换供应商可规避。

    腾讯源用显式 `sh000688` / `sz399006` 形式，不存在上述歧义。
    任何失败返回 None（调用方继续降级），绝不向 A/B/C 抛异常。
    """
    try:
        import akshare as ak
        df = _suppress_output(lambda: ak.stock_zh_index_daily_tx(symbol=tx_code))
        if df is None or df.empty:
            return None
        col_map = {"日期": "date", "开盘": "open", "最高": "high",
                   "最低": "low", "收盘": "close", "成交量": "volume"}
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        if "date" not in df.columns:
            return None
        df["date"] = pd.to_datetime(df["date"])
        for col in ("open", "high", "low", "close", "volume"):
            if col not in df.columns:
                df[col] = 0.0
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"]).sort_values("date")
        df = df.tail(days).reset_index(drop=True)
        return df[["date", "open", "high", "low", "close", "volume"]] if not df.empty else None
    except Exception as e:
        logger.debug("腾讯源获取指数 %s 失败: %s", tx_code, e)
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


def _fetch_index(index_code: str, days: int, timeout: int, dbg: bool = False,
                 prefer: str = "baostock") -> Optional[pd.DataFrame]:
    """获取指数日线数据：baostock / 腾讯 双源，带超时降级。**东财已移除。**

    Args:
        index_code: 指数代码，如 'sh000001'
        days: 拉取的天数
        timeout: 单数据源超时秒数
        dbg: 是否打印详细排查日志
        prefer: 首选数据源。"baostock" → 先 baostock 再腾讯；其它值 → 先腾讯再 baostock。
            ⚠️ 2026-10-03：efinance（东财）已从源顺序中**彻底移除**
            （实测反复 ConnectionError；且对 000688 有静默取错标的的隐患）。
            传 "efinance" 不再有任何特殊含义，等价于"腾讯优先"。
    """
    tx_code, bs_code = _normalize_index_code(index_code)
    if dbg:
        _debug("══════ 大盘数据获取排查 ══════", True)
        _debug(f"输入指数代码: {index_code}  →  腾讯: {tx_code}  |  baostock: {bs_code}", True)
        _debug(f"拉取天数: {days}  |  单源超时: {timeout}s  |  首选: {prefer}", True)
        _debug("数据源: baostock + 腾讯（东财已禁用）", True)

    def _try_baostock_source():
        if dbg:
            _debug("─── 尝试 baostock ───", True)
        try:
            import signal as sig
            def _handler2(signum, frame):
                raise TimeoutError("baostock 超时")
            sig.signal(sig.SIGALRM, _handler2)
            sig.alarm(timeout)

            def _bs_fetch():
                if not _ensure_bs_login():
                    return None
                return _fetch_index_baostock(bs_code, days)
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
            if dbg:
                _debug("❌ baostock 返回空数据", True)
        except Exception as e:
            if dbg:
                _debug(f"❌ baostock 失败: {type(e).__name__}: {e}", True)
            logger.info("baostock 获取指数失败: %s", e)
        return None

    def _try_tencent_source():
        if dbg:
            _debug("─── 尝试 腾讯(akshare) ───", True)
        try:
            import signal as sig
            def _handler3(signum, frame):
                raise TimeoutError("腾讯源超时")
            sig.signal(sig.SIGALRM, _handler3)
            sig.alarm(timeout)
            try:
                df = _fetch_index_tencent(index_code, days)
            finally:
                sig.alarm(0)
            if df is not None and not df.empty:
                logger.info("大盘指数数据来源: 腾讯 (%s)", index_code)
                if dbg:
                    _debug(f"✅ 腾讯成功: {len(df)} 根 K 线", True)
                    _debug(f"   日期范围: {df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}", True)
                    _debug(f"   最新收盘: {df['close'].iloc[-1]:.2f}", True)
                return df
            if dbg:
                _debug("❌ 腾讯返回空数据", True)
        except Exception as e:
            if dbg:
                _debug(f"❌ 腾讯失败: {type(e).__name__}: {e}", True)
            logger.info("腾讯源获取指数失败: %s", e)
        return None

    # —— 按 prefer 决定尝试顺序（只有两个源：baostock 与腾讯）——
    # 东财已于 2026-10-03 从源顺序中彻底移除（实测反复 ConnectionError，
    # 且对 000688 存在静默取错标的的隐患）。
    # 腾讯用显式 sh/sz 前缀（无 000688 歧义），是非东财供应商，作为唯一备源。
    order = [_try_baostock_source, _try_tencent_source]
    if prefer != "baostock":
        order = [_try_tencent_source, _try_baostock_source]
    for fetch in order:
        df = fetch()
        if df is not None and not df.empty:
            return df

    if dbg:
        _debug("❌ 所有数据源均失败（baostock + 腾讯），返回 None", True)
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
        prefer=getattr(config, "BENCHMARK_PREFER_SOURCE", "baostock"),
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
