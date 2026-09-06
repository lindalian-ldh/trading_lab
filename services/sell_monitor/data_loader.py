"""K线数据获取：优先 efinance，失败兜底 baostock。

与 calc_indicators 的 fetch_data 保持一致的风格，但本服务需要：
    1. 按 [entry_date, ref_date] 区间取数（用于计算 bars_held 和最高价）
    2. 列标准化为 {date, open, high, low, close, volume}
    3. date 列为字符串 YYYY-MM-DD（便于与 position.entry_date 字符串比较）

数据来源顺序：efinance → baostock。均失败返回 None，由调用方决定降级行为。
"""

from __future__ import annotations

import io
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ====================================================================
# 工具：压制 efinance/baostock 的 stdout/stderr 噪声
# ====================================================================

def _suppress_output(func):
    """压制一切 stdout/stderr（含 fd 级）执行 func，返回其结果。

    复用 calc_indicators/main.py 的实现：efinance 在 import / 调用阶段
    会向 stdout/stderr 打印进度信息，会污染 sell_monitor 的控制台输出。
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


# ====================================================================
# 代码归一化
# ====================================================================

def _normalize_code(symbol: str) -> tuple:
    """将纯6位代码转换为 (ef_code, bs_code)。

    与 calc_indicators 一致：6/5/9 开头 → sh，其余 → sz。
    """
    code = symbol.strip()
    if len(code) != 6 or not code.isdigit():
        raise ValueError(f"股票代码格式错误: {code}，应为6位数字")
    if code.startswith(("6", "5", "9")):
        return code, f"sh.{code}"
    return code, f"sz.{code}"


# ====================================================================
# 数据获取主函数
# ====================================================================

def fetch_klines(symbol: str, start_date: str, end_date: str,
                 extra_days: int = 15) -> Optional[pd.DataFrame]:
    """获取 [start_date - extra_days, end_date] 区间的日线K线。

    extra_days 用于在 entry_date 之前多拉一些数据（虽然本服务主要用 entry 之后的K线，
    但保留余量便于后续扩展 ATR 计算等场景）。

    Args:
        symbol: 6位股票代码
        start_date: 起始日期 YYYY-MM-DD（通常是开仓日 entry_date）
        end_date: 结束日期 YYYY-MM-DD（通常是参考日 ref_date）
        extra_days: 在 start_date 之前额外拉取的日历日数

    Returns:
        DataFrame 列: date(YYYY-MM-DD str) / open / high / low / close / volume
        按日期升序，最后一行为 end_date 当日（或最近交易日）
        失败返回 None
    """
    ef_code, bs_code = _normalize_code(symbol)

    # 把 start_date 往前推 extra_days，给 ATR/前低等计算留余量
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=extra_days)
        fetch_start = start_dt.strftime("%Y-%m-%d")
    except ValueError:
        fetch_start = start_date  # 格式异常时退化为原值

    # ---- efinance 优先 ----
    df = _suppress_output(lambda: _try_efinance(ef_code, fetch_start, end_date))
    if df is not None and not df.empty:
        logger.debug("efinance 获取 %s 成功: %d 行", symbol, len(df))
        return df

    # ---- baostock 兜底 ----
    df = _suppress_output(lambda: _try_baostock(bs_code, fetch_start, end_date))
    if df is not None and not df.empty:
        logger.debug("baostock 获取 %s 成功: %d 行", symbol, len(df))
        return df

    logger.warning("K线获取失败 symbol=%s [%s, %s]", symbol, fetch_start, end_date)
    return None


# ====================================================================
# 数据源适配器
# ====================================================================

def _try_efinance(ef_code: str, start_date: str,
                  end_date: str) -> Optional[pd.DataFrame]:
    """efinance 数据源。

    ef.stock.get_quote_history 返回中文列名：
        股票名称 / 股票代码 / 日期 / 开盘 / 收盘 / 最高 / 最低 / 成交量 / 成交额 / ...
    """
    try:
        import efinance as ef
        df = ef.stock.get_quote_history(
            ef_code,
            beg=start_date.replace("-", ""),
            end=end_date.replace("-", ""),
        )
        if df is None or df.empty:
            return None

        # 列名标准化
        rename_map = {
            '日期': 'date', '开盘': 'open', '最高': 'high',
            '最低': 'low', '收盘': 'close', '成交量': 'volume',
        }
        df = df.rename(columns=rename_map)
        needed = ['date', 'open', 'high', 'low', 'close', 'volume']
        df = df[[c for c in needed if c in df.columns]]

        # date 转字符串 YYYY-MM-DD
        df['date'] = df['date'].astype(str).str[:10]
        # 数值列
        for c in ['open', 'high', 'low', 'close', 'volume']:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce')
        df = df.dropna(subset=['close']).sort_values('date').reset_index(drop=True)
        return df
    except Exception as e:
        logger.debug("efinance 失败: %s", e)
        return None


def _try_baostock(bs_code: str, start_date: str,
                  end_date: str) -> Optional[pd.DataFrame]:
    """baostock 数据源兜底。"""
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
        logger.debug("baostock 失败: %s", e)
        return None
