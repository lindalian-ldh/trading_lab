# -*- coding: utf-8 -*-
"""信号验证回测 MVP。

目标：验证哑铃三信号是否有效、再平衡决策是否产生超额收益。
不模拟 T+1 / 涨跌停 / 交易成本 — 仅信号有效性检验。

输出：
    1. 累计收益曲线 CSV（哑铃 vs 等权基准）
    2. 三信号 IC（信息系数：信号值与未来 N 日收益相关性）
    3. 再平衡触发次数与累计超额收益

运行方式：
    uv run python -c "from services.barbell_system.backtest import run_backtest; \\
    run_backtest(mode='style', start_date='2024-01-01')"
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

import numpy as np
import pandas as pd

import barbell_config as cfg
import data_fetcher as df_mod
import signals
import weights as weights_mod
import rebalance
from config.settings import settings
from core.logger import get_logger

log = get_logger("barbell.backtest")


def run_backtest(mode: str = "style",
                  start_date: str = "2024-01-01",
                  end_date: Optional[str] = None,
                  ic_horizon: int = 5) -> dict:
    """信号验证回测。

    Args:
        mode: "style" 或 "sector"（sector 模式资产池仍由 screener 提供，回测需先固定池）
        start_date: 起始日 YYYY-MM-DD
        end_date: 截止日，默认今天
        ic_horizon: IC 计算的未来 N 日收益窗口

    Returns:
        {
          "summary": {...},
          "equity_curve_path": str,
          "ic_table_path": str,
        }
    """
    end_date = end_date or dt.datetime.now().strftime("%Y-%m-%d")
    today = dt.datetime.now().strftime("%Y-%m-%d")

    # 模式覆盖（与 main.py 同逻辑）
    if mode == "sector":
        for pk, ck in {
            "base_weight": "BASE_WEIGHT", "spread_floor": "SPREAD_FLOOR",
            "correlation_alert": "CORRELATION_ALERT",
            "zscore_high": "ZSCORE_HIGH",
            "rebalance_threshold": "REBALANCE_THRESHOLD",
        }.items():
            if pk in cfg.SECTOR_PARAMS:
                setattr(cfg, ck, cfg.SECTOR_PARAMS[pk])
        log.info("sector 模式参数已覆盖（回测）")

    # 1. 拉两端历史数据（与 main.py 一致）
    bs_ok = df_mod.login_baostock()
    try:
        def_df = df_mod.fetch_etf_kline(cfg.DEFENSIVE)
        off_df = df_mod.fetch_etf_kline(cfg.OFFENSIVE)
    finally:
        if bs_ok:
            df_mod.logout_baostock()

    if def_df is None or off_df is None:
        log.error("两端数据获取失败，回测终止")
        return {"error": "data_fetch_failed"}

    # 2. 对齐日期 + 限定回测区间
    d = pd.merge(def_df[["date", "close"]], off_df[["date", "close"]],
                 on="date", suffixes=("_def", "_off"))
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date").reset_index(drop=True)
    d = d[(d["date"] >= start_date) & (d["date"] <= end_date)]
    if len(d) < 120:
        log.error("回测样本不足 120 日（实际 %d）", len(d))
        return {"error": "insufficient_sample"}

    # 3. 滚动计算三信号 + 目标权重 + 再平衡决策
    rows = []
    rebalance_count = 0
    cur_w = {"defensive": cfg.BASE_WEIGHT, "offensive": cfg.BASE_WEIGHT, "cash": 0.0}
    for i in range(max(cfg.CORR_WINDOW, cfg.ZSCORE_LOOKBACK + cfg.ZSCORE_WINDOW), len(d)):
        sub = d.iloc[:i + 1]
        def_sub = sub[["date", "close_def"]].rename(columns={"close_def": "close"})
        off_sub = sub[["date", "close_off"]].rename(columns={"close_off": "close"})

        # 简易信号：用历史回退计算
        # spread：用当前价格代理（无国债历史）
        spread_val, spread_score, _ = signals.signal_spread(
            cfg.DIVIDEND_YIELD_FALLBACK, None, None)
        # 价格代理：当前价格在过去 N 日分位
        if len(def_sub) > 60:
            cur_p = float(def_sub["close"].iloc[-1])
            pct = (def_sub["close"] <= cur_p).sum() / len(def_sub) * 100
            if pct < 40:
                spread_score = 1
            elif pct > 60:
                spread_score = -1
            else:
                spread_score = 0

        corr_val, corr_score, _ = signals.signal_correlation(def_sub, off_sub)
        z_val, z_score, _ = signals.signal_crowding(off_sub)
        total = signals.compute_total_score(spread_score, corr_score, z_score)
        tgt = weights_mod.determine_weights(total)

        # 再平衡检查：当前权重 vs 目标权重偏离
        reb = rebalance.check_rebalance(cur_w, tgt)
        if reb["triggered"]:
            rebalance_count += 1
            # 涨多减仓、跌多补仓：按目标权重重新平衡
            cur_w = dict(tgt)

        # 计算当日组合收益
        ret_def = float(def_sub["close"].pct_change().iloc[-1]) if len(def_sub) > 1 else 0
        ret_off = float(off_sub["close"].pct_change().iloc[-1]) if len(off_sub) > 1 else 0
        port_ret = cur_w["defensive"] * ret_def + cur_w["offensive"] * ret_off
        bench_ret = 0.5 * ret_def + 0.5 * ret_off

        rows.append({
            "date": sub["date"].iloc[-1].strftime("%Y-%m-%d"),
            "spread_score": spread_score, "corr_score": corr_score, "zscore_score": z_score,
            "total": total, "regime": tgt["regime"],
            "def_weight": cur_w["defensive"], "off_weight": cur_w["offensive"],
            "cash_weight": cur_w["cash"],
            "port_ret": port_ret, "bench_ret": bench_ret,
            "rebalance": int(reb["triggered"]),
        })

    bt = pd.DataFrame(rows)
    if bt.empty:
        return {"error": "no_records"}

    bt["port_equity"] = (1 + bt["port_ret"]).cumprod()
    bt["bench_equity"] = (1 + bt["bench_ret"]).cumprod()
    bt["excess"] = bt["port_equity"] - bt["bench_equity"]

    # 4. IC：信号值与未来 N 日组合收益相关性
    bt["fwd_ret"] = bt["port_ret"].rolling(ic_horizon).sum().shift(-ic_horizon)
    ic = {}
    for sig in ["spread_score", "corr_score", "zscore_score", "total"]:
        sub = bt[[sig, "fwd_ret"]].dropna()
        if len(sub) > 10:
            ic[sig] = round(float(sub[sig].corr(sub["fwd_ret"])), 4)
        else:
            ic[sig] = None

    # 5. 写 CSV
    eq_path = settings.data_dir / f"barbell_backtest_{mode}_{start_date}_{today}.csv"
    eq_path.parent.mkdir(parents=True, exist_ok=True)
    bt.to_csv(eq_path, index=False, encoding="utf-8-sig")

    ic_df = pd.DataFrame([ic])
    ic_path = settings.data_dir / f"barbell_backtest_ic_{mode}_{start_date}_{today}.csv"
    ic_df.to_csv(ic_path, index=False, encoding="utf-8-sig")

    summary = {
        "mode": mode,
        "period": f"{start_date} ~ {end_date}",
        "n_days": len(bt),
        "rebalance_count": rebalance_count,
        "port_total_ret_pct": round((bt["port_equity"].iloc[-1] - 1) * 100, 2),
        "bench_total_ret_pct": round((bt["bench_equity"].iloc[-1] - 1) * 100, 2),
        "excess_ret_pct": round((bt["excess"].iloc[-1]) * 100, 2),
        "ic_horizon_days": ic_horizon,
        "ic": ic,
    }

    log.info("回测完成: %s", summary)
    print(f"\n[回测完成] {mode} 模式 {start_date}~{end_date}")
    print(f"  组合累计: {summary['port_total_ret_pct']}% | 基准: {summary['bench_total_ret_pct']}% | "
          f"超额: {summary['excess_ret_pct']}%")
    print(f"  再平衡触发: {rebalance_count} 次")
    print(f"  IC: {ic}")
    print(f"  收益曲线: {eq_path}")
    print(f"  IC 表: {ic_path}")

    return {"summary": summary, "equity_curve_path": str(eq_path),
            "ic_table_path": str(ic_path)}


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "style"
    start = sys.argv[2] if len(sys.argv) > 2 else "2024-01-01"
    run_backtest(mode=mode, start_date=start)
