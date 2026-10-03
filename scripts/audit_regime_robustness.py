#!/usr/bin/env python3
"""恐慌择时信号的 三个检验：阈值敏感性 / 安慰剂 / 朴素规则对照。

全部离线：只用 data/cache/index_sh000300_history.csv。

为什么需要这三个（第 1 项的审计只清了「前视」，没清「过拟合」）：
    无前视 ≠ 未过拟合。阈值是在同一段数据上定的，所以必须问：
      ① 敏感性：阈值小幅扰动，效应还在吗？（塌掉 → 过拟合）
      ② 安慰剂：随机挑同样多的日子，能挑出多大效应？（给出零分布与 p 值）
      ③ 朴素对照：不用检测器、只写「跌够多就买」，效果差多少？（检测器是否白加）

度量口径：
    主口径 = 指数自身的 20 根前瞻收益（次日开盘入场 → 第20根收盘）。
    用它而不是个股，是为了避开退市/生存者/票池构成这些混淆 —— 这里只问
    「恐慌状态本身能不能预测指数上涨」，这正是择时问题。
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

WARMUP, HOLD = 70, 20


def load_index(code: str) -> pd.DataFrame:
    f = ROOT / f"data/cache/index_{code}_history.csv"
    df = pd.read_csv(f, encoding="utf-8-sig")
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def build_timeline(index_df: pd.DataFrame, cfg) -> dict:
    """与 backtest.build_regime_timeline 同逻辑，但允许传入自定义 cfg。"""
    import regime_detector as RD
    original = RD._fetch_index_data
    tl: dict = {}
    try:
        for k in range(WARMUP, len(index_df)):
            sub = index_df.iloc[:k + 1].reset_index(drop=True)
            RD._fetch_index_data = lambda _cfg, _df=sub: _df
            RD._REGIME_CACHE.clear()
            try:
                r = RD.detect_regime(cfg)
            except Exception:
                continue
            if r.get("available"):
                tl[index_df["date"].iloc[k].strftime("%Y-%m-%d")] = {
                    "regime": r["regime"], "label": r["label"]}
    finally:
        RD._fetch_index_data = original
        RD._REGIME_CACHE.clear()
    return tl


def fwd_index_ret(index_df: pd.DataFrame) -> pd.Series:
    """指数 20 根前瞻收益（次日开盘入场 → 第 HOLD 根收盘），单位 %。"""
    o = index_df["open"].to_numpy(float)
    c = index_df["close"].to_numpy(float)
    n = len(index_df)
    out = np.full(n, np.nan)
    for i in range(n):
        j = i + 1
        e = o[j] if j < n else np.nan
        k = j + HOLD - 1
        if np.isfinite(e) and e > 0 and k < n:
            out[i] = (c[k] / e - 1) * 100
    return pd.Series(out, index=index_df["date"].dt.strftime("%Y-%m-%d"))


def episodes(days_pos: list[int]) -> list[int]:
    """把有序位置列表切成连续片段的长度列表。"""
    if not days_pos:
        return []
    lens, cur = [], 1
    for a, b in zip(days_pos, days_pos[1:]):
        if b - a <= 3:          # 允许周末/短假间隔
            cur += 1
        else:
            lens.append(cur); cur = 1
    lens.append(cur)
    return lens


def effect(rets: pd.Series, mask: np.ndarray) -> tuple[float, float, float, int]:
    """返回 (恐慌日均值, 非恐慌日均值, 差值, 恐慌日数)。"""
    v = rets.to_numpy(float)
    ok = np.isfinite(v)
    m = mask & ok
    if m.sum() == 0 or (~mask & ok).sum() == 0:
        return np.nan, np.nan, np.nan, int(m.sum())
    a, b = v[m].mean(), v[~mask & ok].mean()
    return a, b, a - b, int(m.sum())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="sh000300")
    ap.add_argument("--n-placebo", type=int, default=200)
    ap.add_argument("--seed", type=int, default=20260930)
    ap.add_argument("--since", default="", help="只在该日期之后度量效应（timeline 仍用全部历史预热）")
    args = ap.parse_args()

    import config as C
    import backtest as BT
    import market_filter as MF

    idx = load_index(args.index)
    rets = fwd_index_ret(idx)
    if args.since:
        keep = [d for d in rets.index if d >= args.since]
        rets = rets.loc[keep]
        print(f"度量窗口限制为 >= {args.since}（timeline 仍用全部历史预热）")
    print(f"指数 {args.index}: {len(idx)} 根  {idx.date.iloc[0].date()} ~ {idx.date.iloc[-1].date()}")
    print(f"可算 20 根前瞻收益的交易日: {int(np.isfinite(rets.to_numpy(float)).sum())}")
    print(f"⚠️  本检验仅覆盖该缓存窗口，非原回测的 2016-2026 全段\n")

    # 守卫：不允许真实联网取数
    _orig = MF._fetch_index
    MF._fetch_index = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("发生真实联网取数"))

    base_cfg = C.TradingConfig()
    base_cfg.REGIME_DEBUG = False

    # —— 自建 builder 必须与官方 builder 完全一致，否则结论不可信 ——
    MF._fetch_index = _orig
    official = BT.build_regime_timeline(idx)
    MF._fetch_index = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("联网"))
    mine = build_timeline(idx, base_cfg)
    MF._fetch_index = _orig
    assert mine == official, "自建 timeline 与官方不一致，中止"
    print("✓ 自建 timeline 与 backtest.build_regime_timeline 逐位一致\n")

    dates = list(rets.index)
    pos_of = {d: i for i, d in enumerate(dates)}
    tl_keys = set(base_cfg and mine.keys())

    def mask_from(tl: dict) -> np.ndarray:
        return np.array([tl.get(d, {}).get("regime") == "PANIC_DOWN" for d in dates])

    base_mask = mask_from(mine)
    a0, b0, d0, n0 = effect(rets, base_mask)
    print(f"【基线】PANIC_DOWN {n0} 天；恐慌日 {a0:.2f}% vs 其它 {b0:.2f}%  →  差 {d0:+.2f}pp")
    print(f"       独立片段 {len(episodes(sorted(i for i,m in enumerate(base_mask) if m)))} 个\n")

    # ================= ① 阈值敏感性 =================
    print("=" * 74)
    print("① 阈值敏感性（每次只动一个参数，重建 timeline）")
    print(f"{'参数':26s} {'取值':>7s} {'恐慌日':>6s} {'恐慌日%':>8s} {'其它%':>7s} {'差pp':>7s}")
    sweeps = {
        "REGIME_PANIC_ATR_PCT":     [0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95],
        "REGIME_PANIC_RET3_PCT":    [-2.0, -2.5, -3.0, -3.5, -4.0, -5.0, -6.0],
        "REGIME_PANIC_RET3_ATR_MULT": [1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 6.0],
        "REGIME_PANIC_RET3_FLOOR_PCT": [-2.0, -2.5, -3.0, -3.5, -4.0, -5.0],
        "REGIME_MA_LONG":           [30, 40, 50, 60, 80, 100, 120],
    }
    rows = []
    for pname, vals in sweeps.items():
        for v in vals:
            cfg = C.TradingConfig(); cfg.REGIME_DEBUG = False
            setattr(cfg, pname, v)
            tl = build_timeline(idx, cfg)
            m = mask_from(tl)
            a, b, dd, nn = effect(rets, m)
            mark = " ←默认" if getattr(base_cfg, pname) == v else ""
            print(f"{pname:26s} {v:>7} {nn:>6d} {a:>8.2f} {b:>7.2f} {dd:>7.2f}{mark}")
            rows.append((pname, v, nn, dd))
        print()

    arr = np.array([r[3] for r in rows], float)
    print(f"  敏感性汇总: 效应范围 {np.nanmin(arr):+.2f} ~ {np.nanmax(arr):+.2f} pp，"
          f"中位 {np.nanmedian(arr):+.2f} pp，全部为正? {bool((arr > 0).all())}")
    print(f"  恐慌日数范围: {min(r[2] for r in rows)} ~ {max(r[2] for r in rows)}\n")

    # ================= ② 安慰剂 =================
    print("=" * 74)
    print("② 安慰剂检验：随机挑「同样片段数 + 同样各段长度」的日子")
    ppos = sorted(i for i, m in enumerate(base_mask) if m)
    lens = episodes(ppos)
    print(f"   真实片段长度: {lens}  (共 {sum(lens)} 天)")
    rng = np.random.default_rng(args.seed)
    valid = np.isfinite(rets.to_numpy(float))
    null = []
    for _ in range(args.n_placebo):
        m = np.zeros(len(dates), bool)
        for L in lens:
            for _try in range(50):
                s = rng.integers(0, len(dates) - L)
                if not m[s:s + L].any():
                    m[s:s + L] = True
                    break
        aa, bb, ddd, _ = effect(rets, m)
        if np.isfinite(ddd):
            null.append(ddd)
    null = np.array(null)
    p = float((null >= d0).mean())
    print(f"   零分布: 均值 {null.mean():+.2f}pp  标准差 {null.std():+.2f}pp  "
          f"P95 {np.percentile(null,95):+.2f}pp  最大 {null.max():+.2f}pp")
    print(f"   真实效应 {d0:+.2f}pp  →  安慰剂 p = {p:.3f}"
          f"  ({'显著' if p < 0.05 else '❌ 不显著（落在随机挑选的范围内）'})\n")

    # ================= ③ 朴素规则对照 =================
    print("=" * 74)
    print("③ 朴素规则对照：不用检测器，只写「指数近3日跌够多就买」")
    c3 = idx["close"].to_numpy(float)
    ret3 = np.full(len(idx), np.nan)
    ret3[3:] = (c3[3:] / c3[:-3] - 1) * 100
    ret3s = pd.Series(ret3, index=idx["date"].dt.strftime("%Y-%m-%d")).loc[dates]
    print(f"{'规则':30s} {'信号日':>7s} {'信号日%':>9s} {'其它%':>7s} {'差pp':>7s}")
    print(f"{'PANIC_DOWN（检测器）':30s} {n0:>7d} {a0:>9.2f} {b0:>7.2f} {d0:>7.2f}")
    for thr in (-2.0, -3.0, -4.0, -5.0):
        m = (ret3s.to_numpy(float) <= thr)
        a, b, dd, nn = effect(rets, m)
        print(f"{'近3日跌幅 ≤ %.1f%%' % thr:30s} {nn:>7d} {a:>9.2f} {b:>7.2f} {dd:>7.2f}")
    ma60 = pd.Series(idx["close"].rolling(60).mean().to_numpy(float),
                     index=idx["date"].dt.strftime("%Y-%m-%d")).loc[dates]
    below60 = (pd.Series(c3, index=idx["date"].dt.strftime("%Y-%m-%d")).loc[dates]
               < ma60).to_numpy(bool)
    m = (ret3s.to_numpy(float) <= -3.0) & below60
    a, b, dd, nn = effect(rets, m)
    print(f"{'跌幅≤-3% 且 跌破MA60':30s} {nn:>7d} {a:>9.2f} {b:>7.2f} {dd:>7.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
