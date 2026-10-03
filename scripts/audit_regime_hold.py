#!/usr/bin/env python
"""ETF 择时改进检验：① 持有期敏感性 ② 多指数稳健性 ③ 分段一致性。

背景（已知基线，见 logs/regime_oos.log）：
    恐慌日买 sh000001 持 20 日：
        2016-2020  +0.48pp  p=0.367  ❌
        2021-2026  +4.15pp  p=0.000  ✅
    即：**只在近段有效**。所以"能不能改进"必须回答三个问题：
        ① 20 日是不是巧合？持有期动一动还在不在？
        ② 只有 sh000001 有效吗？换个指数还在不在？
        ③ 两段都为正吗？（一致为正才敢说是真信号，而非单段拟合）

方法论与 audit_regime_robustness.py 保持一致：
    · 次日开盘入场，持 H 根收盘出场
    · 恐慌日均值 − 非恐慌日均值 = 差值
    · 分段时间线仍用全部历史预热（避免预热窗口差异污染）
    · 零假设用**片段匹配重采样**（保持片段长度分布）

用法:
    uv run python scripts/audit_regime_hold.py                    # 全跑
    uv run python scripts/audit_regime_hold.py --indices sh000001
    uv run python scripts/audit_regime_hold.py --no-cache         # 重建时间线
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "calc_indicators"))

WARMUP = 70
MEASURE_FROM = "2016-01-01"      # 与 audit_regime_robustness / regime_oos 的基线窗口对齐。
                                 # 缓存含 2014-2015 数据，只作预热；若纳入度量，
                                 # 恐慌日会从 71 变成 86，与基线不可比（2026-10-01 踩过）。
HOLDS = [1, 3, 5, 10, 20, 40]
DEFAULT_INDICES = ["sh000001", "sh000300", "sh000688"]
CACHE = ROOT / "data" / "cache" / "regime_timeline"


def load_index(code: str) -> pd.DataFrame | None:
    f = ROOT / f"data/cache/index_{code}_history.csv"
    if not f.exists():
        return None
    df = pd.read_csv(f, encoding="utf-8-sig")
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def build_timeline(index_df: pd.DataFrame, cfg, code: str, use_cache: bool) -> dict:
    """逐根算出历史 regime。**结果落盘缓存** —— 单指数约 2900 次判定，很慢。"""
    cf = CACHE / f"{code}.json"
    if use_cache and cf.exists():
        print(f"    （时间线命中缓存 {cf.name}）")
        return json.loads(cf.read_text(encoding="utf-8"))

    import regime_detector as RD
    original = RD._fetch_index_data
    tl: dict = {}
    try:
        for k in range(WARMUP, len(index_df)):
            sub = index_df.iloc[:k + 1].reset_index(drop=True)
            RD._fetch_index_data = lambda _cfg, _df=sub: _df
            RD._REGIME_CACHE.clear()
            try:
                # detect_regime 会打印整块「E-市场状态排查」，逐根调用会淹没结果
                with contextlib.redirect_stdout(io.StringIO()):
                    r = RD.detect_regime(cfg)
            except Exception:
                continue
            if r.get("available"):
                tl[index_df["date"].iloc[k].strftime("%Y-%m-%d")] = {
                    "regime": r["regime"], "label": r.get("label", "")}
    finally:
        RD._fetch_index_data = original
        RD._REGIME_CACHE.clear()
    CACHE.mkdir(parents=True, exist_ok=True)
    cf.write_text(json.dumps(tl, ensure_ascii=False), encoding="utf-8")
    return tl


def fwd_ret(index_df: pd.DataFrame, hold: int) -> pd.Series:
    """次日开盘入场 → 第 hold 根收盘出场，单位 %。"""
    o = index_df["open"].to_numpy(float)
    c = index_df["close"].to_numpy(float)
    n = len(index_df)
    out = np.full(n, np.nan)
    for i in range(n):
        j = i + 1
        e = o[j] if j < n else np.nan
        k = j + hold - 1
        if np.isfinite(e) and e > 0 and k < n:
            out[i] = (c[k] / e - 1) * 100
    return pd.Series(out, index=index_df["date"].dt.strftime("%Y-%m-%d"))


def episodes(pos: list[int]) -> list[int]:
    if not pos:
        return []
    lens, cur = [], 1
    for a, b in zip(pos, pos[1:]):
        if b - a <= 3:
            cur += 1
        else:
            lens.append(cur); cur = 1
    lens.append(cur)
    return lens


def effect(rets: pd.Series, mask: np.ndarray) -> tuple[float, float, float, int]:
    v = rets.to_numpy(float)
    ok = np.isfinite(v)
    m = mask & ok
    if m.sum() == 0 or (~mask & ok).sum() == 0:
        return np.nan, np.nan, np.nan, 0
    a, b = v[m].mean(), v[~mask & ok].mean()
    return a, b, a - b, int(m.sum())


def placebo_p(rets: pd.Series, mask: np.ndarray, rng: np.random.Generator,
              n: int) -> tuple[float, float]:
    """片段匹配重采样：打散片段位置但保持片段长度分布。返回 (零分布95分位, p)。"""
    v = rets.to_numpy(float)
    ok = np.isfinite(v)
    pos = np.where(mask & ok)[0]
    if len(pos) == 0:
        return np.nan, np.nan
    lens = episodes(list(pos))
    valid = np.where(ok)[0]
    obs = v[pos].mean()
    null = []
    for _ in range(n):
        take = []
        for L in lens:
            if len(valid) <= L:
                continue
            s = rng.integers(0, len(valid) - L)
            take.extend(valid[s:s + L])
        if len(take) >= 5:
            null.append(v[np.array(take)].mean())
    if len(null) < 20:
        return np.nan, np.nan
    null = np.array(null)
    return float(np.percentile(null, 95)), float((null >= obs).mean())


def main() -> int:
    ap = argparse.ArgumentParser(description="ETF 择时改进检验")
    ap.add_argument("--indices", default=",".join(DEFAULT_INDICES))
    ap.add_argument("--n-placebo", type=int, default=200)
    ap.add_argument("--seed", type=int, default=20261001)
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    import config as C
    cfg, _ = C.merge_profiles(["default"])
    rng = np.random.default_rng(args.seed)
    codes = [c.strip() for c in args.indices.split(",") if c.strip()]

    print("=" * 82)
    print("ETF 择时改进检验 —— ① 持有期敏感性  ② 多指数稳健性  ③ 分段一致性")
    print("=" * 82)

    summary = {}
    for code in codes:
        idx = load_index(code)
        if idx is None or len(idx) < WARMUP + 60:
            print(f"\n⚠️  {code}: 无缓存或数据不足，跳过")
            continue
        print(f"\n{'─' * 82}\n指数 {code}  {idx['date'].iloc[0]:%Y-%m-%d} ~ {idx['date'].iloc[-1]:%Y-%m-%d}"
              f"  ({len(idx)} 根)")
        tl = build_timeline(idx, cfg, code, not args.no_cache)
        if not tl:
            print("  ⚠️ 时间线为空，跳过")
            continue
        ds = idx["date"].dt.strftime("%Y-%m-%d")
        in_win = (ds >= MEASURE_FROM).to_numpy()
        mask_panic = (ds.isin([d for d, v in tl.items()
                               if v["regime"] == "PANIC_DOWN"]).to_numpy() & in_win)
        n_panic = int(mask_panic.sum())
        seg_a = (in_win & (ds <= "2020-12-31").to_numpy())
        seg_b = (in_win & (ds > "2020-12-31").to_numpy())
        print(f"  恐慌日 {n_panic} 天 / {len(episodes(list(np.where(mask_panic)[0])))} 段"
              f"   （A段 2016-2020 / B段 2021- ）")

        # ① 持有期敏感性
        print(f"\n  ① 持有期敏感性（全样本）")
        print(f"     {'持有':>5s} {'恐慌均值':>9s} {'其它均值':>9s} {'差值':>9s} {'零95分位':>9s} {'p':>7s}")
        hs = {}
        for h in HOLDS:
            r = fwd_ret(idx, h)
            a, b, d, n = effect(r, mask_panic)
            if not np.isfinite(d):
                continue
            q, p = placebo_p(r, mask_panic, rng, args.n_placebo)
            hs[h] = {"diff": d, "p": p, "n": n, "base": b}
            mark = "✅" if (p < 0.05 and d > 0) else ("❌" if d <= 0 else "➖")
            star = "  ← 当前默认" if h == 20 else ""
            print(f"     {h:>5d} {a:>+8.2f}% {b:>+8.2f}% {d:>+8.2f}pp {q:>+8.2f} {p:>7.3f} {mark}{star}")

        # ② 分段一致性（用各持有期）
        print(f"\n  ② 分段一致性（A 2016-2020 / B 2021- ，同持有期）")
        print(f"     {'持有':>5s} │ {'A段差值':>10s} {'A段p':>7s} │ {'B段差值':>10s} {'B段p':>7s} │ 两段同号?")
        seg_ok = {}
        for h in HOLDS:
            r = fwd_ret(idx, h)
            ra = r.copy(); ra[~seg_a] = np.nan
            rb = r.copy(); rb[~seg_b] = np.nan
            ma = mask_panic & seg_a
            mb = mask_panic & seg_b
            _, _, da, _ = effect(ra, ma)
            _, _, db, _ = effect(rb, mb)
            pa = placebo_p(ra, ma, rng, max(50, args.n_placebo // 2))[1]
            pb = placebo_p(rb, mb, rng, max(50, args.n_placebo // 2))[1]
            same = (np.isfinite(da) and np.isfinite(db) and da > 0 and db > 0)
            seg_ok[h] = {"a": da, "pa": pa, "b": db, "pb": pb, "same_positive": bool(same)}
            print(f"     {h:>5d} │ {da:>+9.2f}pp {pa:>7.3f} │ {db:>+9.2f}pp {pb:>7.3f} │ "
                  f"{'✅ 都为正' if same else '❌ 不一致'}")

        summary[code] = {"n_panic": n_panic, "holds": hs, "segments": seg_ok,
                         "seg_a_days": int(seg_a.sum()), "seg_b_days": int(seg_b.sum())}

    # —— 跨指数总表 ——
    print(f"\n{'=' * 82}\n【跨指数对照】h=20（当前默认）恐慌日效应\n{'=' * 82}")
    print(f"  {'指数':<10s} {'恐慌日':>6s} {'全样本差值':>11s} {'p':>7s} │ "
          f"{'A段':>9s} {'B段':>9s} │ 判定")
    for code, d in summary.items():
        h20 = d["holds"].get(20) or {}
        s20 = d["segments"].get(20) or {}
        verdict = ("✅ 两段同号" if s20.get("same_positive") else "❌ 不一致/负")
        print(f"  {code:<10s} {d['n_panic']:>6d} "
              f"{h20.get('diff', float('nan')):>+10.2f}pp {h20.get('p', float('nan')):>7.3f} │ "
              f"{s20.get('a', float('nan')):>+8.2f}pp {s20.get('b', float('nan')):>+8.2f}pp │ {verdict}")

    # —— 结论 ——
    print(f"\n{'=' * 82}\n【怎么读这张表】\n{'=' * 82}")
    print("  · ① 持有期：若只有 20 日为正、相邻档转负 → 说明是**阈值巧合**，不是信号")
    print("  · ② 多指数：若只有 sh000001 有效 → 说明拟到了**单一指数的特性**，不敢用")
    print("  · ③ 分段：两段都为正才算稳定；只在一段有效 = 体制依赖，需明说")
    print("  ⚠️ 33 段是硬约束：分段后每段只有 15~18 段，功效很低，")
    print("     B 段显著而 A 段不显著**不足以**证伪，二者都须看效应正负。")

    out = ROOT / "data" / "reports" / "regime_hold_result.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n📝 明细已存: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
