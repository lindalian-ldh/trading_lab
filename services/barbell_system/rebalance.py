# -*- coding: utf-8 -*-
"""再平衡与失效预警模块。

状态文件 barbell_state.json 记录上次再平衡时两端收盘价，
本次运行用价格漂移推算当前隐含权重，与目标权重对比。
"""

import json
import datetime as dt

import pandas as pd

import barbell_config as cfg
from config.settings import settings
from core.logger import get_logger

log = get_logger("barbell.rebalance")

# 状态文件路径：使用项目 data 目录
STATE_PATH = settings.data_dir / cfg.STATE_FILE


# ----------------------------------------------------------------------
# 状态文件读写
# ----------------------------------------------------------------------
def load_state():
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error("状态读取失败: %s", e)
    return None


def save_state(def_price, off_price, target_weights, today=None):
    if today is None:
        today = dt.datetime.now().strftime("%Y-%m-%d")
    state = {
        "last_rebalance_date": today,
        "def_price": float(def_price),
        "off_price": float(off_price),
        "target_weights_at_rebalance": target_weights,
    }
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        log.info("状态已写入 %s", STATE_PATH)
    except Exception as e:
        log.error("状态写入失败: %s", e)


# ----------------------------------------------------------------------
# 当前隐含权重推算
# ----------------------------------------------------------------------
def estimate_current_weights(state, def_close_now, off_close_now):
    """用价格漂移推算当前两端隐含权重。"""
    if state is None:
        return None
    try:
        d0 = state["def_price"]
        o0 = state["off_price"]
        tw = state.get("target_weights_at_rebalance", {})
        d_w = tw.get("defensive", cfg.BASE_WEIGHT)
        o_w = tw.get("offensive", cfg.BASE_WEIGHT)
        cash_w = tw.get("cash", 0.0)
        total_invested = d_w + o_w
        if total_invested == 0:
            return None
        d_share = d_w / total_invested
        o_share = o_w / total_invested
        d_val = d_share * (def_close_now / d0)
        o_val = o_share * (off_close_now / o0)
        s = d_val + o_val
        cur_def = d_val / s
        cur_off = o_val / s
        return {
            "defensive": float(cur_def * (1 - cash_w)),
            "offensive": float(cur_off * (1 - cash_w)),
            "cash": float(cash_w),
        }
    except Exception as e:
        log.error("权重推算异常: %s", e)
        return None


# ----------------------------------------------------------------------
# 再平衡检查
# ----------------------------------------------------------------------
def check_rebalance(current_weights, target_weights):
    """若两端实际权重偏离目标超阈值，返回再平衡建议。"""
    if current_weights is None:
        return {
            "triggered": False,
            "drifts": {},
            "suggestions": ["无状态文件，无法推算实际权重。建议本次按目标权重建仓并写入状态。"],
        }
    drifts = {}
    suggestions = []
    triggered = False
    for side in ["defensive", "offensive", "cash"]:
        cur = current_weights.get(side, 0)
        tgt = target_weights.get(side, 0)
        drift = cur - tgt
        drifts[side] = round(drift, 4)
        if abs(drift) > cfg.REBALANCE_THRESHOLD:
            triggered = True
            if drift > 0:
                suggestions.append(
                    f"{side}: 实际 {cur*100:.1f}% 高于目标 {tgt*100:.1f}%（超 {drift*100:.1f}pp），"
                    f"建议减仓 {drift*100:.1f}pp")
            else:
                suggestions.append(
                    f"{side}: 实际 {cur*100:.1f}% 低于目标 {tgt*100:.1f}%（差 {abs(drift)*100:.1f}pp），"
                    f"建议补仓 {abs(drift)*100:.1f}pp")
    return {"triggered": triggered, "drifts": drifts, "suggestions": suggestions}


# ----------------------------------------------------------------------
# 失效预警
# ----------------------------------------------------------------------
def check_failure_alert(corr_value, spread_value, def_df, off_df):
    """当以下任意两个条件同时成立时，发出降级预警：
    1. 两端 60 日相关性 > 0.5
    2. 股债利差 < 1.5%
    3. 两端同时连续 5 个交易日下跌
    """
    conditions = []

    c1 = (corr_value is not None) and (corr_value > cfg.FAILURE_CORR_THRESHOLD)
    conditions.append({"name": "60日相关性 > 0.5", "value": corr_value, "met": c1})

    c2 = (spread_value is not None) and (spread_value < cfg.FAILURE_SPREAD_THRESHOLD)
    conditions.append({"name": "股债利差 < 1.5%", "value": spread_value, "met": c2})

    c3 = _both_consecutive_down(def_df, off_df, cfg.FAILURE_DOWN_DAYS)
    conditions.append({"name": f"两端同时连续 {cfg.FAILURE_DOWN_DAYS} 日下跌",
                        "value": c3, "met": c3})

    met_count = sum(1 for c in conditions if c["met"])
    alert = met_count >= cfg.FAILURE_CONDITIONS_REQUIRED

    if alert:
        action = (f"降级预警触发（{met_count}/3 条件成立）。"
                  f"建议两端权重各削减 {cfg.FAILURE_REDUCE_PP*100:.0f} 个百分点转现金/货基，"
                  f"等待至少 {cfg.FAILURE_WAIT_DAYS} 个交易日后重新评估。")
    else:
        action = f"无失效预警（{met_count}/3 条件成立）。"

    return {"alert": alert, "conditions": conditions, "action": action}


def _both_consecutive_down(def_df, off_df, n):
    """两端同时连续 n 个交易日下跌。"""
    try:
        d = pd.merge(def_df[["date", "close"]], off_df[["date", "close"]],
                     on="date", suffixes=("_def", "_off"))
        d = d.sort_values("date").reset_index(drop=True).tail(n + 1)
        if len(d) < n + 1:
            return False
        d_ret_def = d["close_def"].pct_change().dropna()
        d_ret_off = d["close_off"].pct_change().dropna()
        if len(d_ret_def) < n:
            return False
        return bool((d_ret_def.tail(n) < 0).all() and (d_ret_off.tail(n) < 0).all())
    except Exception:
        return False
