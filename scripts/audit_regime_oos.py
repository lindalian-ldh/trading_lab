#!/usr/bin/env python3
"""恐慌择时信号的时序留出（temporal holdout）检验。

问题：之前的 +2.16pp / p=0.004 是**全样本内**的。阈值由人定的，而定阈值的人
看过整段历史 —— 所以必须问：这套阈值在它「没见过」的年份还成立吗？

设计：
    A 段（发展期）: 2016-01-01 ~ 2020-12-31
    B 段（留出期）: 2021-01-01 ~ 2026-09-29
    ① 默认阈值在 A / B 各自的表现（+ 片段匹配安慰剂 p）
    ② 在 A 段做阈值扫描，取 A 段最优配置 → 应用到 B 段
       · 若「A 最优」在 B 段失效、而默认在 B 段仍有效  → 说明 A 最优是过拟合
       · 若两者在 B 段都失效                      → 说明信号本身是时期特异
    ③ 朴素规则在两段的对照

诚实声明：这不是严格意义上的 OOS（阈值不是只在 A 段上拟合出来的），
它检验的是**时间稳定性** —— 但这正是"能不能真下注"需要的那一关。
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
sys.path.insert(0, str(ROOT / "scripts"))
from audit_regime_robustness import load_index, fwd_index_ret, episodes, effect, build_timeline  # noqa: E402

A = ("2016-01-01", "2020-12-31")
B = ("2021-01-01", "2026-09-29")
N_PLACEBO = 2000


def placebo_p(rets: pd.Series, mask: np.ndarray, n: int = N_PLACEBO, seed: int = 11) -> dict:
    """片段匹配安慰剂：随机挑同样段数、同样各段长度的日子（仅在窗口内）。"""
    m = np.asarray(mask, bool)
    lens = episodes(sorted(i for i, x in enumerate(m) if x))
    if not lens:
        return {"p": np.nan, "sd": np.nan, "p95": np.nan, "ep": 0, "n": 0}
    rng = np.random.default_rng(seed)
    L = len(rets)
    null = []
    for _ in range(n):
        mm = np.zeros(L, bool)
        for ell in lens:
            if ell >= L:
                continue
            for _try in range(60):
                s = int(rng.integers(0, L - ell))
                if not mm[s:s + ell].any():
                    mm[s:s + ell] = True
                    break
        _, _, dd, _ = effect(rets, mm)
        if np.isfinite(dd):
            null.append(dd)
    null = np.array(null)
    _, _, real, _ = effect(rets, m)
    return {"p": float((null >= real).mean()) if len(null) else np.nan,
            "sd": float(null.std()), "p95": float(np.percentile(null, 95)),
            "ep": len(lens), "n": int(m.sum())}


def measure(rets: pd.Series, tl: dict) -> tuple:
    dates = list(rets.index)
    m = np.array([tl.get(d, {}).get("regime") == "PANIC_DOWN" for d in dates])
    a, b, d, n = effect(rets, m)
    return m, a, b, d, n


def line(tag: str, rets: pd.Series, tl: dict) -> dict:
    m, a, b, d, n = measure(rets, tl)
    pl = placebo_p(rets, m)
    flag = "✅" if (np.isfinite(pl["p"]) and pl["p"] < 0.05) else "❌"
    print(f"  {tag:34s} {n:>4d}天/{pl['ep']:>2d}段  {a:>+6.2f}% vs {b:>+6.2f}%  "
          f"={d:>+6.2f}pp  零SD={pl['sd']:.2f} P95={pl['p95']:>+5.2f}  p={pl['p']:.3f} {flag}")
    return {"tag": tag, "n": n, "ep": pl["ep"], "effect": d, "p": pl["p"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="sh000001")
    ap.add_argument("--sweep", action="store_true", default=True)
    args = ap.parse_args()

    import config as C
    import market_filter as MF
    MF._fetch_index = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("禁止联网"))

    idx = load_index(args.index)
    rets_all = fwd_index_ret(idx)
    cfg0 = C.TradingConfig(); cfg0.REGIME_DEBUG = False

    def win(lo, hi):
        return rets_all.loc[[d for d in rets_all.index if lo <= d <= hi]]

    ra, rb = win(*A), win(*B)
    print(f"指数 {args.index}  {idx.date.iloc[0].date()} ~ {idx.date.iloc[-1].date()}")
    print(f"A 发展期 {ra.index[0]} ~ {ra.index[-1]}  ({len(ra)} 交易日)")
    print(f"B 留出期 {rb.index[0]} ~ {rb.index[-1]}  ({len(rb)} 交易日)\n")

    tl_full = build_timeline(idx, cfg0)
    print("【① 默认阈值在两段各自的表现】")
    ra_res = line("A 发展期 (2016-2020)", ra, tl_full)
    rb_res = line("B 留出期 (2021-2026)", rb, tl_full)

    # —— 分年（留出期内部）——
    print("\n【② 留出期逐年】")
    dfa = pd.DataFrame({"date": pd.to_datetime(list(rb.index)), "ret": rb.to_numpy(float),
                        "panic": [tl_full.get(d, {}).get("regime") == "PANIC_DOWN" for d in rb.index]})
    for y, g in dfa.groupby(dfa.date.dt.year):
        gp, gn = g[g.panic], g[~g.panic]
        if len(gp) == 0:
            print(f"    {y}: 无恐慌日"); continue
        print(f"    {y}: 恐慌{len(gp):>2d}天 {gp.ret.mean():>+6.2f}%  其它{gn.ret.mean():>+6.2f}%  "
              f"差{gp.ret.mean()-gn.ret.mean():>+6.2f}pp")

    # —— 阈值扫描：只在 A 段挑最优，再看它在 B 段如何 ——
    if args.sweep:
        print("\n【③ 在 A 段扫阈值 → 取 A 段最优 → 拿到 B 段检验（过拟合探针）】")
        idx_a = idx[idx["date"] <= pd.Timestamp(A[1])].reset_index(drop=True)
        sweeps = {
            "REGIME_PANIC_ATR_PCT": [0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95],
            "REGIME_PANIC_RET3_PCT": [-2.0, -2.5, -3.0, -3.5, -4.0, -5.0, -6.0],
            "REGIME_PANIC_RET3_ATR_MULT": [1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 6.0],
            "REGIME_PANIC_RET3_FLOOR_PCT": [-2.0, -2.5, -3.0, -3.5, -4.0, -5.0],
            "REGIME_MA_LONG": [30, 40, 50, 60, 80, 100, 120],
        }
        cands = []
        for pname, vals in sweeps.items():
            for v in vals:
                cfg = C.TradingConfig(); cfg.REGIME_DEBUG = False
                setattr(cfg, pname, v)
                tla = build_timeline(idx_a, cfg)
                _, _, _, da, na = measure(ra, tla)
                if np.isfinite(da):
                    cands.append((pname, v, da, na))
        cands.sort(key=lambda x: -x[2])
        print(f"  A 段扫描 {len(cands)} 个配置，A 段效应范围 "
              f"{min(c[2] for c in cands):+.2f} ~ {max(c[2] for c in cands):+.2f}pp")
        print(f"  {'A段最优配置(前5)':34s} {'A段效应':>8s} {'B段效应':>8s} {'B段p':>7s}")
        for pname, v, da, na in cands[:5]:
            cfg = C.TradingConfig(); cfg.REGIME_DEBUG = False
            setattr(cfg, pname, v)
            tl = build_timeline(idx, cfg)
            _, _, _, db, nb = measure(rb, tl)
            pl = placebo_p(rb, np.array([tl.get(d, {}).get("regime") == "PANIC_DOWN" for d in rb.index]))
            print(f"  {f'{pname}={v}':34s} {da:>+8.2f} {db:>+8.2f} {pl['p']:>7.3f} "
                  f"{'✅' if pl['p'] < 0.05 else '❌'}")
        # 相关：A 段效应能否预测 B 段效应
        xs, ys = [], []
        for pname, v, da, na in cands:
            cfg = C.TradingConfig(); cfg.REGIME_DEBUG = False
            setattr(cfg, pname, v)
            tl = build_timeline(idx, cfg)
            _, _, _, db, nb = measure(rb, tl)
            if np.isfinite(db):
                xs.append(da); ys.append(db)
        if len(xs) > 5:
            r = float(np.corrcoef(xs, ys)[0, 1])
            print(f"\n  A 段效应 vs B 段效应的相关: r = {r:+.3f}  "
                  f"({'A段挑最优对B段有预测力' if r > 0.3 else '❌ A段挑最优对B段无预测力 → 不能据此调参'})")

    # —— 朴素规则两段对照 ——
    print("\n【④ 朴素规则在两段的对照】")
    c3 = idx["close"].to_numpy(float)
    ret3 = np.full(len(idx), np.nan); ret3[3:] = (c3[3:] / c3[:-3] - 1) * 100
    sidx = idx["date"].dt.strftime("%Y-%m-%d")
    r3 = pd.Series(ret3, index=sidx)
    ma60 = pd.Series(idx["close"].rolling(60).mean().to_numpy(float), index=sidx)
    px = pd.Series(c3, index=sidx)
    for tag, lo, hi, rets_w in (("A 发展期", *A, ra), ("B 留出期", *B, rb)):
        print(f"  ── {tag} ──")
        for lab, m in (("PANIC_DOWN(检测器)", None),
                       ("跌幅≤-3%", (r3 <= -3.0).reindex(rets_w.index)),
                       ("跌幅≤-3% 且 破MA60", ((r3 <= -3.0) & (px < ma60)).reindex(rets_w.index))):
            if m is None:
                mm, a, b, dd, n = measure(rets_w, tl_full)
            else:
                mm = m.to_numpy(bool)
                a, b, dd, n = effect(rets_w, mm)
            pl = placebo_p(rets_w, mm)
            print(f"    {lab:22s} {n:>4d}天/{pl['ep']:>2d}段  {dd:>+6.2f}pp  "
                  f"p={pl['p']:.3f} {'✅' if pl['p'] < 0.05 else '❌'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
