#!/usr/bin/env python3
"""regime 判定「无未来函数」污染测试。

原理（不读代码也能定罪/洗清）：
    对任意切点 k，把指数序列 **k 之后** 的数据全部扭曲（放大 / 随机化），
    然后重建整条 regime 时间线。若第 k 天及之前任何一天的判定发生变化，
    就证明该判定读取了 k 之后的数据 —— 前视确认。

    反之，若全部 1..k 的判定逐日不变，则在**这段数据范围内**没有前视。

为什么可信：扭曲只作用在 k 之后，而"合法的"计算只应使用 <=k 的数据，
所以合法实现下前缀判定必须逐位不变。这是充要的实验判据。

同时做第二个方向的守卫：
    · 用 MF._fetch_index 抛异常，抓"未打补丁的真实联网取数"（最危险的前视路径）
    · 检查判定分布不是常数（重演并排除 _REGIME_CACHE 那个静默 bug）

用法：
    python scripts/audit_regime_lookahead.py                 # 默认 sh000300 缓存
    python scripts/audit_regime_lookahead.py --index sh000001  # 若已有该指数缓存
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/calc_indicators"))


def load_index(code: str) -> pd.DataFrame:
    f = ROOT / f"data/cache/index_{code}_history.csv"
    if not f.exists():
        raise SystemExit(f"❌ 无缓存: {f}")
    df = pd.read_csv(f, encoding="utf-8-sig")
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def poison(df: pd.DataFrame, after_k: int, mode: str, seed: int = 7) -> pd.DataFrame:
    """把 after_k 之后的行扭曲掉。0..after_k 行必须保持逐字节不变。"""
    out = df.copy()
    idx = out.index[after_k + 1:]
    if len(idx) == 0:
        return out
    rng = np.random.default_rng(seed)
    if mode == "scale":
        for c in ("open", "high", "low", "close"):
            out.loc[idx, c] = out.loc[idx, c] * 1.6
    elif mode == "flat":
        for c in ("open", "high", "low", "close"):
            out.loc[idx, c] = float(out["close"].iloc[after_k])
    elif mode == "random":
        base = float(out["close"].iloc[after_k])
        walk = base * np.exp(np.cumsum(rng.normal(0, 0.05, len(idx))))
        out.loc[idx, "close"] = walk
        out.loc[idx, "open"] = walk * 0.99
        out.loc[idx, "high"] = walk * 1.02
        out.loc[idx, "low"] = walk * 0.98
    else:
        raise ValueError(mode)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="sh000300")
    ap.add_argument("--modes", default="scale,flat,random")
    args = ap.parse_args()

    import backtest as BT
    import market_filter as MF
    import regime_detector as RD

    df = load_index(args.index)
    print(f"指数 {args.index}: {len(df)} 根  {df.date.iloc[0].date()} ~ {df.date.iloc[-1].date()}")

    # 守卫：任何"未打补丁的真实联网取数"都直接炸掉
    _orig_fetch = MF._fetch_index
    def _boom(*a, **k):
        raise RuntimeError("发生了真实联网取数 → 存在未打补丁的前视路径")
    MF._fetch_index = _boom
    try:
        base = BT.build_regime_timeline(df)
    finally:
        MF._fetch_index = _orig_fetch

    dist = Counter(v["regime"] for v in base.values())
    print(f"基线时间线: {len(base)} 天  分布={dict(dist)}")
    if len(dist) <= 1:
        print("❌ 判定退化成常数 —— 正是 _REGIME_CACHE 那个静默 bug 的症状，先修它")
        return 1
    print("✓ 判定分布非退化（排除进程内缓存键错误）\n")

    dates = list(base.keys())
    panics = [d for d, v in base.items() if v["regime"] == "PANIC_DOWN"]
    print(f"基线中的 PANIC_DOWN: {len(panics)} 天")
    if panics:
        # 统计独立片段（间隔 > 10 自然日算新片段）
        pdt = pd.to_datetime(panics)
        ep = (pdt.to_series().diff().dt.days.fillna(999) > 10).cumsum()
        print(f"独立恐慌片段: {ep.max()} 个\n")

    # 切点：每个恐慌片段前一天 + 若干等距点
    k_of_date = {d: i for i, d in enumerate(df["date"].dt.strftime("%Y-%m-%d"))}
    cuts = []
    if panics:
        pdt = pd.to_datetime(panics)
        firsts = pdt.to_series().groupby(
            (pdt.to_series().diff().dt.days.fillna(999) > 10).cumsum()).min()
        for d in firsts:
            k = k_of_date.get(d.strftime("%Y-%m-%d"))
            if k is not None and k > 120:
                cuts.append(k)
    n = len(df)
    cuts += [int(n * f) for f in (0.4, 0.55, 0.7, 0.85, 0.95)]
    cuts = sorted({c for c in cuts if 120 < c < n - 5})
    print(f"切点 {len(cuts)} 个: {cuts}\n")

    violations = 0
    print(f"{'切点k':>7s} {'日期':>12s} {'污染':>7s} {'prefix天数':>10s} {'判定变化':>9s}")
    for k in cuts:
        kdate = df["date"].iloc[k].strftime("%Y-%m-%d")
        for mode in args.modes.split(","):
            pdf = poison(df, k, mode.strip())
            try:
                MF._fetch_index = _boom
                tl = BT.build_regime_timeline(pdf)
            finally:
                MF._fetch_index = _orig_fetch
            changed = []
            for d in dates:
                if d > kdate:
                    break
                if tl.get(d, {}).get("regime") != base[d]["regime"]:
                    changed.append(d)
            violations += len(changed)
            flag = "✓ 无" if not changed else f"❌ {len(changed)}"
            print(f"{k:>7d} {kdate:>12s} {mode:>7s} {sum(1 for d in dates if d <= kdate):>10d} {flag:>9s}")
            if changed:
                for d in changed[:5]:
                    print(f"         ↳ {d}: {base[d]['regime']} → {tl.get(d,{}).get('regime')}")

    print("\n" + "=" * 70)
    if violations == 0:
        print("✅ 结论：在测试的切点与污染方式下，regime 判定对「未来数据」完全免疫。")
        print("   → 每段前缀的判定逐位不变，说明实现只使用了 <=k 的指数数据。")
    else:
        print(f"❌ 结论：发现 {violations} 处前视。regime 判定读取了未来数据，")
        print("   → 择时结论（恐慌日 +5.6pp）全部作废，必须先修。")
    return 0 if violations == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
