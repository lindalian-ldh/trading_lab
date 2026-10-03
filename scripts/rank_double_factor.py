#!/usr/bin/env python3
"""并行跑：同一批候选，左边 A/B/C 门控判定，右边双因子打分排名。

为什么并行而不是替换：
    双因子打分是**样本内原型、权重未拟合**（见 score_double.py 的说明）。
    正确做法不是"换掉 A/B/C"，而是让两套口径**同时记录**，几个月后用台账数据
    判断哪个更准。这样你既不用现在就押注，也不会丢掉新方向。

产出：
    · 控制台按分数降序的对照表
    · --out 落盘 CSV（含两套口径的全部字段，可直接喂给台账分析）

用法：
    python scripts/rank_double_factor.py --file data/observations/watchlist.txt
    python scripts/rank_double_factor.py --symbols 600519,000001,601899 --top 10
    python scripts/rank_double_factor.py --file wl.txt --out data/observations/rank_20260930.csv
"""
from __future__ import annotations

import argparse
import contextlib
import io
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/calc_indicators"))


def resolve_symbols(args) -> list[str]:
    syms: list[str] = []
    if args.symbols:
        syms += [s.strip() for s in args.symbols.split(",") if s.strip()]
    if args.file:
        for line in Path(args.file).read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if line:
                syms += [s.strip() for s in line.replace(",", " ").split() if s.strip()]
    seen, out = set(), []
    for s in syms:
        s = s.zfill(6)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="")
    ap.add_argument("--file", default="")
    ap.add_argument("-p", "--profile", action="append", default=None,
                    help="配置档名（可重复传入以叠加，与 main.py 一致）")
    ap.add_argument("--top", type=int, default=0, help="只显示前 N 名（0=全部）")
    ap.add_argument("--out", default="", help="可选：落盘 CSV 路径")
    args = ap.parse_args()

    syms = resolve_symbols(args)
    if not syms:
        print("❌ 用 --symbols 或 --file 提供标的")
        return 1

    import config as C
    import main as M
    import score_double as S

    # 用官方解析器：CLI 传的是别名（ultra_short），不是类名（UltraShortConfig）。
    # 之前写成 getattr(C, args.profile)() —— 传 -p ultra_short 会直接 AttributeError。
    cfg, _srcs = C.merge_profiles(list(args.profile or ["default"]))
    cfg.PULLBACK_DEBUG = False
    cfg.MOMENTUM_DEBUG = False
    cfg.MARKET_DEBUG = False

    print(f"配置档: {'+'.join(args.profile) if args.profile else 'default'}")
    print(f"{S.describe()}\n")

    rows, failed = [], 0
    for i, s in enumerate(syms, 1):
        try:
            df = M.fetch_data(s, cfg)
        except SystemExit:
            df = None
        except Exception:
            df = None
        if df is None or len(df) < 70:
            failed += 1
            continue
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        d0 = df["date"].iloc[-1].strftime("%Y-%m-%d")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                a_pass, a_msg = M.check_dimension_a(df, cfg)
                b_pass, b_signals = M.check_dimension_b(df, cfg)
                c_pass, c_info = M.check_dimension_c(df, cfg)
                gate = M.check_entry_gate({}, a_pass, a_msg, b_signals, c_pass, c_info, df, cfg)
            except Exception:
                failed += 1
                continue
        f = S.features_from_df(df, cfg)
        close = f["close"]
        stop = float(c_info.get("stop", float("nan")))
        stop_dist = (close - stop) / close * 100 if close and np.isfinite(stop) else None
        vet = S.hard_vetoes(f, cfg, stop_dist, symbol=s)
        rows.append({
            "date": d0, "symbol": s, **f,
            "a_pass": bool(a_pass), "b_pass": bool(b_pass), "c_pass": bool(c_pass),
            "dim_passed": int(bool(a_pass)) + int(bool(b_pass)) + int(bool(c_pass)),
            "suggest_open": bool(a_pass and b_pass and c_pass and gate.get("passed")),
            "gate_passed": bool(gate.get("passed")),
            "gate_vetoes": ",".join(v["rule"] for v in gate.get("vetoes", [])),
            "c_stop_dist_pct": round(stop_dist, 3) if stop_dist is not None else np.nan,
            "hard_veto": ";".join(vet),
        })
        if i % 10 == 0:
            print(f"  … {i}/{len(syms)}")

    if not rows:
        print("❌ 无可用数据")
        return 1

    out = pd.DataFrame(rows)
    # ETF 排除出排名：本卡的分位/边界都在 300 只**个股**上测得（样本内 0 只 ETF），
    # 把 ETF 与个股放进同一横截面算百分位是类别错误（净值量级与分布都不同）。
    from etf import is_etf as _is_etf
    out["is_etf"] = out["symbol"].map(_is_etf)
    out = S.rank_and_score(out, exclude=out["is_etf"])
    out = out.sort_values("score_double", ascending=False, na_position="last").reset_index(drop=True)
    out.insert(0, "score_rank", range(1, len(out) + 1))

    show = out if args.top <= 0 else out.head(args.top)
    print(f"\n{'='*118}")
    print(f"并行对照（{out.date.iloc[0]}，共 {len(out)} 只，失败 {failed} 只）")
    print(f"{'='*118}")
    print(f"{'#':>3s} {'代码':>7s} {'收盘':>8s} {'ATR%':>6s} {'距MA20%':>8s} {'量比':>5s} "
          f"{'分数':>6s} {'A':>2s}{'B':>2s}{'C':>2s} {'建议开仓':>8s}  硬否决")
    print("-" * 118)
    for _, r in show.iterrows():
        abc = "".join("✅" if r[k] else "·" for k in ("a_pass", "b_pass", "c_pass"))
        if not np.isfinite(r["score_double"]):
            sc = " n/a(ETF)" if r.get("is_etf") else "   n/a"
        else:
            sc = f"{r['score_double']:>6.1f}"
        rk = f"{int(r['score_rank']):>3d}" if np.isfinite(r["score_rank"]) else "  -"
        print(f"{rk:>3s} {r['symbol']:>7s} {r['close']:>8.2f} "
              f"{r['atr_pct']:>6.2f} {r['dev_ma20']:>8.2f} {r['vol_ratio']:>5.2f} "
              f"{sc} {abc:>6s} {'★是' if r['suggest_open'] else ' 否':>8s}  {r['hard_veto'] or '—'}")

    # —— 两套口径的重合度：这是并行观察的核心指标 ——
    ok_veto = out["hard_veto"] == ""
    n_cand = int(ok_veto.sum())
    print(f"\n【两套口径对照】")
    n_etf = int(out["is_etf"].sum())
    print(f"  ETF 已排除出排名: {n_etf} 只（判读卡在个股上测得，ETF 不适用）")
    print(f"  双因子打分可排名: {int(out['score_double'].notna().sum())} 只"
          + ("  ⚠️ 少于 3 只无法构成横截面，分数为 n/a" if out['score_double'].notna().sum() < 3 else ""))
    print(f"  硬否决后剩余:     {n_cand} 只（被否决 {len(out)-n_cand} 只）")
    print(f"  A/B/C 建议开仓:   {int(out['suggest_open'].sum())} 只")
    print(f"  门控放行(任一维度通过): {int(out['gate_passed'].sum())} 只")
    if n_cand >= 3:
        top5 = set(out.head(5)["symbol"])
        abc_set = set(out[out["suggest_open"]]["symbol"])
        abd = set(out[out["dim_passed"] >= 2]["symbol"])
        print(f"\n  打分前5: {sorted(top5)}")
        if abc_set:
            print(f"  A/B/C 建议开仓: {sorted(abc_set)}   与打分前5重合 {len(top5 & abc_set)} 只")
        else:
            print(f"  A/B/C 建议开仓: 无（这正是『三维度 AND 出不了信号』的现场）")
        if abd:
            print(f"  ≥2维通过: {sorted(abd)}   与打分前5重合 {len(top5 & abd)} 只")

    print(f"\n⚠️  底部提示：双因子分数是**样本内、权重未拟合**的原型，"
          f"且单调性**只在 signal 池内成立**（见 score_double.py 的适用范围说明）。"
          f"它的用途是并行记录、日后用台账判断谁更准 —— 不要据此下单。")
    print(f"    分数 = 50%×ATR%百分位 + 50%×(100−距MA20百分位)，逐日横截面计算。")
    print(f"    ⚠️ 重要：本表排名覆盖**整个自选**，但实测单调性只在『A或B通过 且 C通过 且 门控放行』"
          f"的子集内成立。")
    print(f"       全自选排名仅供观察；若要按已验证的口径用，请只看那 0~1 只落在 signal 池里的。")

    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(p, index=False, encoding="utf-8-sig")
        print(f"\n✅ 落盘 {p}（{len(out)} 行，含两套口径全部字段）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
