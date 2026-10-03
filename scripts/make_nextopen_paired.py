#!/usr/bin/env python3
"""把 paired CSV 的 ret5/10/20 从「信号日收盘入场」改写为「次日开盘入场」。

为什么：
    backtest.py 里 entry = close[i]，而 A/B/C 判定用的是含 close[i] 的数据
    （backtest.py:227）。收盘后才知道信号，却在同一根收盘成交 —— 这个价格
    不可执行。本脚本用本地缓存里的 open 重算，只改入场价，其余口径不变，
    以便用同一套 paired_stats.py 做对照。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data/cache/calc_indicators"
SRC = ROOT / "data/reports/paired/paired_300x2016_v2.csv"
DST = ROOT / "data/reports/paired/paired_300x2016_v2_nextopen.csv"
HORIZONS = (5, 10, 20)


def main() -> int:
    cols = ["symbol", "date", "close", "rec_type", "a_pass", "b_pass", "c_pass",
            *[f"ret{h}" for h in HORIZONS]]
    df = pd.read_csv(SRC, usecols=cols, dtype={"symbol": str})
    print(f"读入 {len(df)} 行，{df.symbol.nunique()} 只", flush=True)

    syms = sorted(df.symbol.unique())
    entry = np.full(len(df), np.nan)
    rets = {h: np.full(len(df), np.nan) for h in HORIZONS}
    miss_cache = 0

    for k, sym in enumerate(syms, 1):
        f = CACHE / f"stock_{sym}.csv"
        if not f.exists():
            miss_cache += 1
            continue
        px = pd.read_csv(f, usecols=["date", "open", "close"])
        pos = {d: i for i, d in enumerate(px["date"].astype(str))}
        o = px["open"].to_numpy(float)
        c = px["close"].to_numpy(float)
        n = len(px)
        idx = np.flatnonzero(df.symbol.to_numpy() == sym)
        for r in idx:
            p = pos.get(str(df.date.iat[r]))
            if p is None or p + 1 >= n:
                continue
            e = o[p + 1]
            if not np.isfinite(e) or e <= 0:
                continue
            entry[r] = e
            for h in HORIZONS:
                if p + h < n:
                    rets[h][r] = (c[p + h] / e - 1) * 100
        if k % 50 == 0:
            print(f"  {k}/{len(syms)} 已处理", flush=True)

    if miss_cache:
        print(f"⚠️  {miss_cache} 只无缓存（其行入场价为空，将被剔除）", flush=True)

    out = df.copy()
    out["entry_next_open"] = entry
    for h in HORIZONS:
        out[f"ret{h}"] = rets[h]
    before = len(out)
    out = out[np.isfinite(out["entry_next_open"])]
    print(f"可执行入场样本: {len(out)}/{before} "
          f"（剔除 {before - len(out)}，{(before-len(out))/before*100:.1f}%）", flush=True)

    # 诊断：信号日收盘 → 次日开盘 的跳空，看"用收盘价入场"偏乐观还是偏保守
    gap = (out["entry_next_open"] / out["close"] - 1) * 100
    for t in ("signal", "nosignal", "vetoed"):
        g = gap[out.rec_type == t]
        if len(g):
            print(f"  跳空 {t:9s} n={len(g):>7d}  均值={g.mean():+.3f}%  "
                  f"中位={g.median():+.3f}%", flush=True)

    out[cols + ["entry_next_open"]].to_csv(DST, index=False)
    print(f"写出 {DST}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
