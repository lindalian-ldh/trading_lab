# -*- coding: utf-8 -*-
"""sector 模式失效预警增强模块。

7 条件预警，任意 cfg.FAILURE_CONDITIONS_REQUIRED（默认 2）条同时成立触发降级。
阈值全部从 cfg.FAILURE_* 读取，sector 模式由 main.py 在启动时覆盖。

条件列表（与原 rebalance.check_failure_alert 的 3 条件合并去重）：
    1. 两端 60 日相关性 > FAILURE_CORR_THRESHOLD（sector=0.65）
    2. 股债利差 < FAILURE_SPREAD_THRESHOLD（sector=2.0%）
    3. 两端同时连续 FAILURE_DOWN_DAYS 日下跌（sector=3）
    4. 两端同时跌破 20 日均线
    5. 政策事件标志（policy_flag 非空）
    6. 60 日相关性从 <0.3 骤升至 >0.6（corr_history 提供）
    7. 任意端单日跌幅 > 5%（重大单日波动）

返回结构与 rebalance.check_failure_alert 一致，便于 main.py 透明替换。
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

import barbell_config as cfg
from core.logger import get_logger

log = get_logger("barbell.alert")

# 复用 rebalance._both_consecutive_down，不重复实现
import rebalance as _reb


def check_failure_alert_sector(corr_value: Optional[float],
                                spread_value: Optional[float],
                                def_df: pd.DataFrame,
                                off_df: pd.DataFrame,
                                policy_flag: list | None = None,
                                corr_history: list | None = None) -> dict:
    """sector 模式 8 条件失效预警。

    Args:
        corr_value: 当前 60 日两端相关系数
        spread_value: 当前股债利差（%）
        def_df / off_df: 两端日线 DataFrame（含 date, close）
        policy_flag: 政策事件列表，非空视为触发
        corr_history: 60 日相关系数历史序列，用于检测骤升

    Returns:
        {"alert": bool, "conditions": [{"name","value","met"}], "action": str}
    """
    conditions = []

    # ---- 原条件 1: 60 日相关性 ----
    c1 = (corr_value is not None) and (corr_value > cfg.FAILURE_CORR_THRESHOLD)
    conditions.append({
        "name": f"60日相关性 > {cfg.FAILURE_CORR_THRESHOLD}",
        "value": corr_value, "met": c1,
    })

    # ---- 原条件 2: 股债利差 ----
    c2 = (spread_value is not None) and (spread_value < cfg.FAILURE_SPREAD_THRESHOLD)
    conditions.append({
        "name": f"股债利差 < {cfg.FAILURE_SPREAD_THRESHOLD}%",
        "value": spread_value, "met": c2,
    })

    # ---- 原条件 3: 两端同时连续 N 日下跌 ----
    c3 = _reb._both_consecutive_down(def_df, off_df, cfg.FAILURE_DOWN_DAYS)
    conditions.append({
        "name": f"两端同时连续 {cfg.FAILURE_DOWN_DAYS} 日下跌",
        "value": c3, "met": c3,
    })

    # ---- 新条件 4: 两端同时跌破 20 日均线 ----
    c4 = _both_below_ma(def_df, off_df, 20)
    conditions.append({
        "name": "两端同时跌破 20 日均线",
        "value": c4, "met": c4,
    })

    # ---- 新条件 5: 政策事件 ----
    flag_list = policy_flag or []
    c5 = len(flag_list) > 0
    conditions.append({
        "name": f"政策事件标志（{len(flag_list)} 条）",
        "value": flag_list, "met": c5,
    })

    # ---- 新条件 6: 相关性骤升（<0.3 → >0.6）----
    c6 = _corr_surge(corr_history)
    conditions.append({
        "name": "60日相关性从 <0.3 骤升至 >0.6",
        "value": c6, "met": c6,
    })

    # ---- 新条件 7: 任意端单日跌幅 > 5% ----
    c7 = _any_side_large_drop(def_df, off_df, threshold_pct=5.0)
    conditions.append({
        "name": "任意端单日跌幅 > 5%",
        "value": c7, "met": c7,
    })

    # ---- 计数与降级判定 ----
    met_count = sum(1 for c in conditions if c["met"])
    alert = met_count >= cfg.FAILURE_CONDITIONS_REQUIRED

    if alert:
        action = (f"sector 模式降级预警触发（{met_count}/{len(conditions)} 条件成立）。"
                  f"两端权重各削减 {cfg.FAILURE_REDUCE_PP*100:.0f} 个百分点转现金/货基，"
                  f"等待至少 {cfg.FAILURE_WAIT_DAYS} 个交易日后重新评估。"
                  f"触发条件: {[c['name'] for c in conditions if c['met']]}")
    else:
        action = f"sector 模式无失效预警（{met_count}/{len(conditions)} 条件成立）。"

    return {"alert": alert, "conditions": conditions, "action": action}


# ====================================================================
# 辅助函数
# ====================================================================
def _both_below_ma(def_df: pd.DataFrame, off_df: pd.DataFrame, n: int) -> bool:
    """两端当前价同时低于 N 日均线。"""
    try:
        d_close = def_df["close"].tail(n + 1)
        o_close = off_df["close"].tail(n + 1)
        if len(d_close) < n + 1 or len(o_close) < n + 1:
            return False
        d_ma = d_close.iloc[:-1].mean()
        o_ma = o_close.iloc[:-1].mean()
        d_cur = float(d_close.iloc[-1])
        o_cur = float(o_close.iloc[-1])
        return bool(d_cur < d_ma and o_cur < o_ma)
    except Exception:
        return False


def _corr_surge(corr_history: list | None,
                low: float = 0.3, high: float = 0.6,
                lookback: int = 30) -> bool:
    """60 日相关性从 < low 骤升至 > high。

    Args:
        corr_history: 历史相关系数序列（最早 → 最新）
        low: 早期低阈值
        high: 当前高阈值
        lookback: 检查 lookback 天内是否有过 < low
    """
    if not corr_history or len(corr_history) < 5:
        return False
    try:
        recent = list(corr_history[-lookback:]) if len(corr_history) >= lookback else list(corr_history)
        cur = float(recent[-1])
        if cur <= high:
            return False
        # 检查早期是否曾经 < low
        early = recent[:-3] if len(recent) > 3 else recent[:-1]
        if not early:
            return False
        had_low = any(float(v) < low for v in early if v is not None)
        return bool(had_low)
    except Exception:
        return False


def _any_side_large_drop(def_df: pd.DataFrame, off_df: pd.DataFrame,
                          threshold_pct: float = 5.0) -> bool:
    """任意端最近单日跌幅 > 阈值（%）。"""
    try:
        for df in (def_df, off_df):
            if df is None or len(df) < 2:
                continue
            ret = float(df["close"].pct_change().iloc[-1] * 100)
            if ret < -threshold_pct:
                return True
        return False
    except Exception:
        return False
