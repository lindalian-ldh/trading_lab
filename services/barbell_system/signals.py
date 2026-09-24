# -*- coding: utf-8 -*-
"""信号计算模块：三个信号 + 综合得分。

信号一：股债利差（spread）
信号二：两端滚动相关性（correlation）
信号三：进攻端拥挤度（zscore）
"""

import numpy as np
import pandas as pd

import barbell_config as cfg


# ----------------------------------------------------------------------
# 信号一：股债利差
# ----------------------------------------------------------------------
def signal_spread(dividend_yield, treasury_yield, price_percentile=None):
    """股债利差信号。

    Args:
        dividend_yield: 中证红利股息率（%）
        treasury_yield: 10 年期国债收益率（%）；None 时使用价格分位数代理
        price_percentile: 防御端价格分位数（0~100），国债不可用时代理

    Returns:
        (spread_value, score, note)
    """
    if treasury_yield is None:
        if price_percentile is None:
            return (None, 0, "国债与代理均不可用，利差信号得 0")
        if price_percentile < 40:
            return (None, 1, f"价格分位数 {price_percentile:.1f}% < 40，性价比高，+1")
        elif price_percentile > 60:
            return (None, -1, f"价格分位数 {price_percentile:.1f}% > 60，性价比低，-1")
        return (None, 0, f"价格分位数 {price_percentile:.1f}% 中性，0")

    spread = dividend_yield - treasury_yield
    if spread > cfg.SPREAD_HIGH:
        return (spread, 1, f"利差 {spread:.2f}% > {cfg.SPREAD_HIGH}，+1")
    if spread < cfg.SPREAD_FLOOR:
        return (spread, -1, f"利差 {spread:.2f}% < {cfg.SPREAD_FLOOR}，-1")
    return (spread, 0, f"利差 {spread:.2f}% 中性，0")


# ----------------------------------------------------------------------
# 信号二：两端滚动相关性
# ----------------------------------------------------------------------
def signal_correlation(def_df, off_df):
    """60 日滚动收益率相关系数。

    Returns:
        (corr_value, score, note)
    """
    d = pd.merge(def_df[["date", "close"]], off_df[["date", "close"]],
                 on="date", suffixes=("_def", "_off"))
    d = d.sort_values("date").reset_index(drop=True)
    if len(d) < cfg.CORR_WINDOW + 5:
        return (None, 0, f"数据不足 {cfg.CORR_WINDOW}+ 日，相关性得 0")

    ret_def = d["close_def"].pct_change()
    ret_off = d["close_off"].pct_change()
    corr = ret_def.rolling(cfg.CORR_WINDOW).corr(ret_off).dropna()
    if len(corr) == 0:
        return (None, 0, "相关性计算为空，得 0")
    cur = float(corr.iloc[-1])

    if cur < cfg.CORR_LOW:
        return (cur, 1, f"60日相关 {cur:.2f} < {cfg.CORR_LOW}，两端分化，+1")
    if cur > cfg.CORRELATION_ALERT:
        return (cur, -1, f"60日相关 {cur:.2f} > {cfg.CORRELATION_ALERT}，两端共振，-1")
    return (cur, 0, f"60日相关 {cur:.2f} 中性，0")


# ----------------------------------------------------------------------
# 信号三：进攻端拥挤度
# ----------------------------------------------------------------------
def signal_crowding(off_df):
    """进攻端 20 日收益率相对过去 120 交易日的 Z-score。

    Returns:
        (zscore, score, note)
    """
    closes = off_df["close"].sort_index()
    if len(closes) < cfg.ZSCORE_LOOKBACK + cfg.ZSCORE_WINDOW:
        return (None, 0, f"数据不足 {cfg.ZSCORE_LOOKBACK}+ 日，拥挤度得 0")

    ret = closes.pct_change(cfg.ZSCORE_WINDOW).dropna()
    if len(ret) < cfg.ZSCORE_LOOKBACK:
        return (None, 0, "20日收益率样本不足，拥挤度得 0")

    sample = ret.tail(cfg.ZSCORE_LOOKBACK).values
    cur = float(sample[-1])
    mu = float(np.mean(sample))
    sigma = float(np.std(sample))
    if sigma == 0:
        return (0.0, 0, "标准差为 0，拥挤度得 0")
    z = (cur - mu) / sigma

    if z < cfg.ZSCORE_LOW:
        return (z, 1, f"Z-score {z:.2f} < {cfg.ZSCORE_LOW}，不拥挤，+1")
    if z > cfg.ZSCORE_HIGH:
        return (z, -1, f"Z-score {z:.2f} > {cfg.ZSCORE_HIGH}，拥挤，-1")
    return (z, 0, f"Z-score {z:.2f} 中性，0")


# ----------------------------------------------------------------------
# 综合得分
# ----------------------------------------------------------------------
def compute_total_score(spread_score, corr_score, zscore_score):
    """三信号得分求和。范围 -3 ~ +3。"""
    return spread_score + corr_score + zscore_score
