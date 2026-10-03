#!/usr/bin/env python3
"""把「信号的前瞻收益」升级为「可执行的交易 P&L」——含止损与到期离场。

为什么必须做：
    backtest.py 落盘的 ret5/10/20 是 ret = close[i+h]/close[i]-1（:256），
    止损只有 stop{k} 这个布尔标记（:289-295），**没有任何一处把止损计入盈亏**。
    于是现有结论「信号是负的」严格说只否证了*信号*，没有否证*策略*。
    而系统 46% 的信号会被 2×ATR 打到（见 v2 日志），止损到底是在保护还是在
    「把浮亏实现掉」，只能靠模拟回答。

口径：
    入场 = 次日开盘（可执行，见 make_nextopen_paired.py）
    窗口 = 入场后 20 根
    止损 = entry - k×ATR（ATR 取信号日值，按 entry 等比缩放）
       · 若当日 open 已低于止损 → 按 open 成交（跳空穿透，不假设理想成交）
       · 否则按止损价成交
    到期 = 第 20 根收盘
    同时输出 no-stop 版本（对照片段），两者相减即「止损的净影响」
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data/cache/calc_indicators"
SRC = ROOT / "data/reports/paired/paired_300x2016_v2.csv"
DST = ROOT / "data/reports/paired/paired_300x2016_v2_tradepnl.csv"
STOP_KS = (2.0, 3.0, 4.0)
HOLD = 20


def main() -> int:
    cols = ["symbol", "date", "close", "rec_type", "a_pass", "b_pass", "atr_pct", "rr_ratio"]
    df = pd.read_csv(SRC, usecols=cols, dtype={"symbol": str})
    print(f"读入 {len(df)} 行", flush=True)

    n = len(df)
    out = {"entry": np.full(n, np.nan), "ret_nostop": np.full(n, np.nan)}
    for k in STOP_KS:
        out[f"ret_stop{k:g}"] = np.full(n, np.nan)
        out[f"stopped{k:g}"] = np.zeros(n, dtype=bool)
    out["bars"] = np.zeros(n, dtype=int)

    atr = df["atr_pct"].to_numpy(float)
    sym_arr = df["symbol"].to_numpy()
    date_arr = df["date"].astype(str).to_numpy()

    for si, sym in enumerate(sorted(df.symbol.unique()), 1):
        f = CACHE / f"stock_{sym}.csv"
        if not f.exists():
            continue
        px = pd.read_csv(f, usecols=["date", "open", "high", "low", "close"])
        pos = {d: i for i, d in enumerate(px["date"].astype(str))}
        o = px["open"].to_numpy(float); h = px["high"].to_numpy(float)
        lo = px["low"].to_numpy(float); c = px["close"].to_numpy(float)
        m = len(px)
        rows = np.flatnonzero(sym_arr == sym)
        for r in rows:
            p = pos.get(date_arr[r])
            if p is None or p + 1 >= m or not np.isfinite(atr[r]) or atr[r] <= 0:
                continue
            e = o[p + 1]
            if not np.isfinite(e) or e <= 0:
                continue
            end = min(p + 1 + HOLD, m - 1)
            if end <= p + 1:
                continue
            wl = lo[p + 1:end + 1]; wo = o[p + 1:end + 1]
            if len(wl) == 0:
                continue
            out["entry"][r] = e
            out["bars"][r] = len(wl)
            out["ret_nostop"][r] = (c[end] / e - 1) * 100
            for k in STOP_KS:
                stop = e - k * (atr[r] / 100) * e
                hit = np.flatnonzero(wl <= stop)
                if hit.size:
                    j = int(hit[0])
                    fill = wo[j] if wo[j] < stop else stop
                    out[f"ret_stop{k:g}"][r] = (fill / e - 1) * 100
                    out[f"stopped{k:g}"][r] = True
                else:
                    out[f"ret_stop{k:g}"][r] = (c[end] / e - 1) * 100
        if si % 50 == 0:
            print(f"  {si} 只已处理", flush=True)

    for k, v in out.items():
        df[k] = v
    df.to_csv(DST, index=False)
    print(f"写出 {DST}", flush=True)

    print("\n=== 各口径平均收益（%，含可执行入场）===")
    hdr = f"{'rec_type':10s} {'n':>7s} {'no-stop':>8s}"
    for k in STOP_KS:
        hdr += f" {f'stop{k:g}':>8s} {f'停损率':>7s}"
    print(hdr)
    for t in ("signal", "vetoed", "nosignal"):
        g = df[df.rec_type == t]
        if not len(g):
            continue
        line = f"{t:10s} {len(g):>7d} {g.ret_nostop.mean():>8.3f}"
        for k in STOP_KS:
            line += f" {g[f'ret_stop{k:g}'].mean():>8.3f} {g[f'stopped{k:g}'].mean()*100:>6.1f}%"
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
