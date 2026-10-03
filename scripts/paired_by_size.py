#!/usr/bin/env python3
"""按「规模」分层重跑配对检验：A/B/C 入场对不同市值/流动性档位是否都有效。

为什么要做：
    汇总口径的配对差（-0.22%~-0.38%）是**全样本平均**，它可能掩盖"在某一档里其实有效"。
    尤其是"小票"这一档 —— 如果入场逻辑对小票有效、只在大票上失效，那结论完全不同。

规模怎么度量（两个口径互为交叉验证）：
    ① 当日横截面「20日均成交额」排名（历史准确、逐日变化、300 只全覆盖）
       —— 成交额 = close × volume，从本地缓存算，无未来函数（只用到当日及之前）
       —— 分位在**每个交易日内**计算，因此是"横截面规模"，不混入"市场整体增长"
    ② 当前「流通市值」快照（bankuai 缓存，仅 171/300 只有数据）
       —— 只作交叉验证：它是单一时点，用它给 10 年回测分层会有"现在大≠当年大"的偏差

配对口径沿用 paired_stats.test_pair：同一交易日内，处理组均值 − 对照组均值。
先用可执行入场（次日开盘）的 CSV。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/calc_indicators"))
import paired_stats as ps  # noqa: E402

CACHE = ROOT / "data/cache/calc_indicators"
SRC = ROOT / "data/reports/paired/paired_300x2016_v2_nextopen.csv"
FLAGS = ROOT / "data/reports/paired/paired_300x2016_v2.csv"
N_BOOT, BLOCK, H = 400, 20, 10


def add_amount(df: pd.DataFrame) -> pd.DataFrame:
    """加一列 amt20 = 截至当日的 20 日均成交额（亿元）。"""
    amt = np.full(len(df), np.nan)
    sym = df["symbol"].to_numpy()
    dts = df["date"].astype(str).to_numpy()
    for s in pd.unique(sym):
        f = CACHE / f"stock_{s}.csv"
        if not f.exists():
            continue
        px = pd.read_csv(f, usecols=["date", "close", "volume"])
        a = (px["close"] * px["volume"]).rolling(20).mean().to_numpy()
        pos = {d: i for i, d in enumerate(px["date"].astype(str))}
        for r in np.flatnonzero(sym == s):
            p = pos.get(dts[r])
            if p is not None and np.isfinite(a[p]):
                amt[r] = a[p] / 1e8
    df = df.copy()
    df["amt20"] = amt
    return df


def quintile_within_day(df: pd.DataFrame, col: str, q: int = 5) -> pd.Series:
    """每个交易日内按 col 排名分 q 档（1=最小）。"""
    return df.groupby("date")[col].transform(
        lambda s: pd.qcut(s.rank(method="first"), q, labels=False) + 1
        if s.notna().sum() >= q else np.nan)


def run(df, treat, control, label):
    d = df.copy()
    d["ret20"] = d[f"ret{H}"]
    r = ps.test_pair(d, treat, control, 20, n_boot=N_BOOT, block=BLOCK)
    if "error" in r:
        print(f"  {label:28s} {r['error']}")
        return
    sig = "显著负" if r["ci_hi"] < 0 else ("★显著正" if r["ci_lo"] > 0 else "不显著")
    print(f"  {label:28s} n={r['treat_n_total']:>6d} diff={r['diff']:>7.3f} "
          f"CI=[{r['ci_lo']:>7.3f},{r['ci_hi']:>7.3f}] t={r['t_stat']:>6.2f} {sig}")


def main() -> int:
    base = pd.read_csv(FLAGS, usecols=["symbol", "date", "rec_type", "b_vol_surge",
                                       "b_rsi_oversold", "a_pass", "b_pass"], dtype={"symbol": str})
    nx = pd.read_csv(SRC, usecols=["ret5", "ret10", "ret20"], dtype={"symbol": str})
    assert len(base) == len(nx)
    df = pd.concat([base, nx], axis=1)
    df["date"] = pd.to_datetime(df["date"])
    df = add_amount(df)
    print(f"行数 {len(df)}  有成交额数据 {df.amt20.notna().sum()}  "
          f"(缺失多为近 20 日内退市/停牌)\n")

    df["size_q"] = quintile_within_day(df, "amt20", 5)
    sig = df.rec_type == "signal"
    nos = df.rec_type == "nosignal"

    print(f"=== 按当日横截面 20日均成交额 五档（口径：{H}日，次日开盘入场）===")
    print("  （档位1 = 当日成交额最小 = 偏小票）\n")
    run(df, sig, nos, "全样本（不分档）")
    for q in range(1, 6):
        m = df.size_q == q
        sub = df[m]
        lo, hi = sub.amt20.min(), sub.amt20.max()
        run(sub, sub.rec_type == "signal", sub.rec_type == "nosignal",
            f"档{q} ({lo:.2f}~{hi:.1f}亿)")
    print("\n  各档成交额中位数(亿): " +
          "  ".join(f"Q{q}={df[df.size_q==q].amt20.median():.2f}" for q in range(1, 6)))

    # ── 小票档里再拆触发源 ──
    print(f"\n=== 档1（最小成交额）内部按触发源拆 ===")
    q1 = df[df.size_q == 1]
    run(q1, q1.rec_type == "signal", q1.rec_type == "nosignal", "档1 全部 signal")
    run(q1, (q1.rec_type == "signal") & q1.b_vol_surge, q1.rec_type == "nosignal", "档1 放量触发")
    run(q1, (q1.rec_type == "signal") & ~q1.b_vol_surge, q1.rec_type == "nosignal", "档1 无放量")
    run(q1, (q1.rec_type == "signal") & q1.b_rsi_oversold, q1.rec_type == "nosignal", "档1 RSI超卖")
    run(q1, (q1.rec_type == "signal") & ~q1.b_vol_surge & ~q1.b_rsi_oversold,
        q1.rec_type == "nosignal", "档1 无放量且无RSI")

    # ── 交叉验证：用当前流通市值快照分层 ──
    import glob, json
    cap = {}
    for f in sorted(glob.glob(str(ROOT / "data/raw/bankuai/*/stocks_*.json"))):
        try:
            rows = json.load(open(f))
        except Exception:
            continue
        if isinstance(rows, list):
            for r in rows:
                c = str(r.get("stock_code", "")).zfill(6)
                if c and r.get("circulation_value"):
                    cap[c] = float(r["circulation_value"])
    df["cap"] = df.symbol.map(lambda s: cap.get(s, np.nan) / 1e8)
    have = df[df.cap.notna()]
    print(f"\n=== 交叉验证：按当前流通市值快照分层（{have.symbol.nunique()} 只有市值数据）===")
    print("  ⚠️ 单一时点市值给 10 年回测分层有偏差，只看方向是否一致\n")
    for lo, hi, lab in [(0, 50, "微盘 <50亿"), (50, 100, "小盘 50-100亿"),
                        (100, 300, "中小 100-300亿"), (300, 1e9, "中大 >300亿")]:
        sub = have[(have.cap >= lo) & (have.cap < hi)]
        if sub.empty:
            continue
        run(sub, sub.rec_type == "signal", sub.rec_type == "nosignal", lab)
    return 0


if __name__ == "__main__":
    sys.exit(main())
