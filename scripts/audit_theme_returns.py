#!/usr/bin/env python
"""P2.3 收益审计 —— L1 / L2 / RS / 组合 的主题级效应与显著性。

## 主判据（P0.5 冻结基准 v1 §⑥，H = 20）

1. `|t| ≥ 1.96`；2. `|D| ≥ MDE`（MDE = 2.80 × SE）；3. A/B 两段同号；
4. placebo `p < 0.05`；5. 联合频次 ≥ 3 次/年。

> ⚠️ 由于 `MDE = 2.80 × SE`，第 2 条实际上把门槛抬到 **|t| ≥ 2.8**，比第 1 条更严。
> 这是仓库既有口径（80% 功效），**不是笔误**。

## 窗口纪律

```
预热          < 2016-01-01                  ← 均线/摆动点需要历史，只作预热
A 段          2016-01-01 ~ 2020-12-31
B 段          2021-01-01 ~ 2023-12-31       ← P0.5 修订 #3：不含 2024 年之后
🔒 OOS        2024-01-01 ~ 2026-09-30       ← **本脚本一行都不算**
```

## 统计口径（P0.5 修订记录 #8）

`D = mean(fwd20|mask) − mean(fwd20|¬mask)`（pp）；SE = **块长 20 的移动块 bootstrap**
（重叠 H 日窗口下，朴素日级 t 会严重高估显著性）；`MDE = 2.80 × SE`；
placebo = 片段匹配重采样。详见 [`core/signal_stats.py`](../core/signal_stats.py)。

## 多重检验

**10 个主题实际只有 5 条不同价格序列**（`sz399363` 被 4 个主题共用），
所以本脚本**按去重后的指数**报告，并额外给出 BH 调整后的 p 作**参考**
（BH 不在冻结判据里 ⇒ 只标注，不参与通过/不通过）。

用法:
    .venv/bin/python scripts/audit_theme_returns.py
    .venv/bin/python scripts/audit_theme_returns.py --index sz399363 --holds 5,10,20,40
    .venv/bin/python scripts/audit_theme_returns.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.marketdata_tx import fetch_index_history  # noqa: E402
from core.signal_stats import (  # noqa: E402
    POWER_Z,
    block_bootstrap_diff,
    episode_spans,
    fwd_ret,
    placebo_p,
    summarize,
    two_sample_diff,
)
from core.theme_timing import EPISODE_GAP, layer_masks  # noqa: E402
from core.theme_universe import THEMES, WATCH_ONLY, all_observed  # noqa: E402

A_START, A_END = "2016-01-01", "2020-12-31"
B_START, B_END = "2021-01-01", "2023-12-31"
OOS_START, OOS_END = "2024-01-01", "2026-09-30"       # 🔒 锁定
PRIMARY_HOLD = 20
LAYERS = ("L1", "L2", "RS", "L1+L2", "L1+L2+RS")
T_CRIT = 1.96
MIN_JOINT_PER_YEAR = 3.0


def _seg_mask(dates: pd.Series, lo: str, hi: str) -> np.ndarray:
    dt = pd.to_datetime(dates)
    return ((dt >= lo) & (dt <= hi)).to_numpy()


def _stats_on(rets: np.ndarray, mask: np.ndarray, hold: int, *, n_boot: int,
              n_placebo: int) -> dict:
    out = summarize(rets, mask, block=hold, n_boot=n_boot, n_placebo=n_placebo)
    return out


def audit_series(index_code: str, anchor_code: str, holds: list, *,
                 n_boot: int, n_placebo: int, online: bool) -> dict:
    d = fetch_index_history(index_code, refresh=online, prefer_cache=not online)
    a = None if index_code == anchor_code else \
        fetch_index_history(anchor_code, refresh=online, prefer_cache=not online)
    rec = {"index": index_code, "anchor": anchor_code,
           "degenerate_rs": index_code == anchor_code, "holds": {}, "primary": {}}
    if d is None or d.empty:
        rec["error"] = "指数无数据"
        return rec

    masks = layer_masks(d, a)
    dates = masks["date"]
    in_a = _seg_mask(dates, A_START, A_END)
    in_b = _seg_mask(dates, B_START, B_END)
    in_main = in_a | in_b

    for hold in holds:
        r = fwd_ret(d, hold)
        r_main = np.where(in_main, r, np.nan)          # 主窗口外一律 NaN
        per_layer = {}
        for L in LAYERS:
            if rec["degenerate_rs"] and ("RS" in L):
                per_layer[L] = {"skipped": "index == anchor ⇒ RS 不可用"}
                continue
            m = masks[L].to_numpy(bool)
            full = _stats_on(r_main, m, hold, n_boot=n_boot, n_placebo=n_placebo)
            ra = np.where(in_a, r, np.nan)
            rb = np.where(in_b, r, np.nan)
            da = two_sample_diff(ra, m)
            db = two_sample_diff(rb, m)
            qa, pa = placebo_p(ra, m, np.random.default_rng(20261004), n=n_placebo,
                               gap=EPISODE_GAP)
            qb, pb = placebo_p(rb, m, np.random.default_rng(20261004), n=n_placebo,
                               gap=EPISODE_GAP)
            years_a = 5.0
            years_b = 3.0
            ep_a = len(episode_spans(m & in_a, gap=EPISODE_GAP))
            ep_b = len(episode_spans(m & in_b, gap=EPISODE_GAP))
            full.update({
                "D_a": da, "D_b": db, "p_a": pa, "p_b": pb, "q95_a": qa, "q95_b": qb,
                "same_sign": bool(np.isfinite(da) and np.isfinite(db) and da * db > 0),
                "ep_per_year_a": ep_a / years_a, "ep_per_year_b": ep_b / years_b,
            })
            per_layer[L] = full
        rec["holds"][hold] = per_layer
    rec["primary"] = rec["holds"].get(PRIMARY_HOLD, {})
    return rec


def _verdict(s: dict) -> tuple:
    """按 P0.5 §⑥ 五项判据给出结论（**只对 H=20 主判据**）。"""
    if s.get("skipped"):
        return "➖ N/A", s["skipped"]
    if not np.isfinite(s.get("t", np.nan)):
        return "➖ 不可判定", "有效样本不足（信号日或基准日 <3）"
    fails = []
    if abs(s["t"]) < T_CRIT:
        fails.append(f"|t|={abs(s['t']):.2f}<{T_CRIT}")
    if abs(s["D"]) < s["mde"]:
        fails.append(f"|D|={abs(s['D']):.2f}<MDE={s['mde']:.2f}")
    if not s["same_sign"]:
        fails.append("两段不同号")
    if not (np.isfinite(s["placebo_p"]) and s["placebo_p"] < 0.05):
        fails.append(f"placebo p={s['placebo_p']:.3f}")
    if fails:
        return "➖ 未通过", "；".join(fails)
    return "✅ 通过", "四项判据全过（频次见下表）"


def main() -> int:
    ap = argparse.ArgumentParser(description="P2.3 主题级收益审计")
    ap.add_argument("--index", action="append", help="只跑指定指数（可重复）")
    ap.add_argument("--holds", default=str(PRIMARY_HOLD),
                    help="持有期，逗号分隔（默认只用主判据 20）")
    ap.add_argument("--n-boot", type=int, default=2000)
    ap.add_argument("--n-placebo", type=int, default=200)
    ap.add_argument("--online", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--themes-only", action="store_true",
                    help="只看 THEMES（10 个含题材的主题），不含 WATCH_ONLY 观察项")
    args = ap.parse_args()

    holds = [int(x) for x in str(args.holds).split(",") if x.strip()]
    # 按**去重后的价格序列**跑（10 主题只有 5 条序列）
    series: dict = {}
    for t in (THEMES if args.themes_only else all_observed()):
        if not t.get("index"):
            continue
        if args.index and t["index"] not in args.index:
            continue
        series.setdefault(t["index"], {"anchor": t["anchor"], "themes": []})
        series[t["index"]]["themes"].append(t["theme"])

    out = []
    for idx, meta in series.items():
        rec = audit_series(idx, meta["anchor"], holds, n_boot=args.n_boot,
                           n_placebo=args.n_placebo, online=args.online)
        rec["themes"] = meta["themes"]
        out.append(rec)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0

    print("=" * 126)
    print(f"P2.3 收益审计 —— 主判据 H={PRIMARY_HOLD}    A 段 {A_START}~{A_END} / B 段 {B_START}~{B_END}")
    print(f"     🔒 OOS 锁定段 {OOS_START} ~ {OOS_END} **未计算**")
    print(f"     口径：次日开盘入场 → 持 {PRIMARY_HOLD} 根收盘出场；D = 掩码内均值 − 掩码外均值（pp）")
    print(f"     SE = 块长 {PRIMARY_HOLD} 的移动块 bootstrap（B={args.n_boot}）；MDE = {POWER_Z}×SE；"
          f"placebo = 片段匹配重采样（n={args.n_placebo}）")
    print("=" * 126)

    for rec in out:
        if rec.get("error"):
            print(f"\n❌ {rec['index']}: {rec['error']}")
            continue
        print(f"\n{'─' * 126}")
        print(f"指数 {rec['index']}   锚 {rec['anchor']}   主题: {', '.join(rec['themes'])}")
        if rec["degenerate_rs"]:
            print("  ⚠️ index == anchor ⇒ 比价恒为 1，**RS 层与其组合不可用（N/A）**")
        print(f"  {'层':<10}{'片段':>5}{'信号天':>7}{'D(pp)':>9}{'SE':>7}{'t':>8}{'MDE':>7}"
              f"{'p_boot':>8}{'plc_p':>7}{'A段D':>8}{'B段D':>8}{'同号':>5}  判定")
        print("  " + "-" * 122)
        for L in LAYERS:
            s = rec["primary"].get(L, {})
            if s.get("skipped"):
                print(f"  {L:<10}{'—':>5}{'—':>7}{'—':>9}{'—':>7}{'—':>8}{'—':>7}"
                      f"{'—':>8}{'—':>7}{'—':>8}{'—':>8}{'—':>5}  ➖ {s['skipped']}")
                continue
            mark, why = _verdict(s)
            print(f"  {L:<10}{s['n_episodes']:>5}{s['n_signal_days']:>7}{s['D']:>+9.2f}"
                  f"{s['se']:>7.2f}{s['t']:>+8.2f}{s['mde']:>7.2f}{s['p']:>8.3f}"
                  f"{s['placebo_p']:>7.3f}{s['D_a']:>+8.2f}{s['D_b']:>+8.2f}"
                  f"{('✅' if s['same_sign'] else '❌'):>5}  {mark}  {why}")
        print(f"     片/年：A 段 " + "  ".join(
            f"{L}={rec['primary'][L].get('ep_per_year_a', float('nan')):.2f}"
            for L in LAYERS) + "（B 段 " + "  ".join(
            f"{L}={rec['primary'][L].get('ep_per_year_b', float('nan')):.2f}"
            for L in LAYERS) + "）")

        if len(holds) > 1:
            print(f"\n  【持有期敏感性（描述性，不参与判定）】")
            print(f"    {'H':>4}" + "".join(f"{L:>22}" for L in ("L1", "L2")))
            for h in holds:
                cells = []
                for L in ("L1", "L2"):
                    s = rec["holds"].get(h, {}).get(L, {})
                    cells.append(f"{s.get('D', float('nan')):>+8.2f}pp t={s.get('t', float('nan')):>+6.2f}")
                print(f"    {h:>4}" + "".join(f"{c:>22}" for c in cells))

    print(f"\n{'=' * 126}")
    print("【结论】")
    for rec in out:
        if rec.get("error"):
            continue
        l1 = rec["primary"].get("L1", {})
        mark, why = _verdict(l1)
        joint = rec["primary"].get("L1+L2", {})
        print(f"  {rec['index']:<9} L1: {mark:<8} D={l1.get('D', float('nan')):>+6.2f}pp "
              f"t={l1.get('t', float('nan')):>+5.2f} 片段={l1.get('n_episodes', 0):>3}  | "
              f"L1+L2 片段={joint.get('n_episodes', 0):>2}（{'N/A' if joint.get('skipped') else 'kill 线 2/年'}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
