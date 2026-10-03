#!/usr/bin/env python3
"""信号台账分析：把"感觉"换成可判定的数字。

配套 `log_signal_ledger.py`。核心设计有三条，都是为了**让结论可证伪**：

  ① 对照组必须存在
     · 默认用「同一交易日、同一台账内其它标的的中位数」当候选池对照
     · 也可 --control index 用指数当对照（更宽，但不受你选票范围影响）
     没有对照，就分不清"系统选对了"和"那天大盘涨了"。

  ② 前瞻收益机械计算，不用你的盈亏
     口径与全部回测一致：**次日开盘入场 → T+h 收盘**。
     你的实际盈亏混着仓位/情绪/择时，不能用来评系统。

  ③ 样本量不足时**拒绝给结论**
     每个分组都算 MDE（最小可检测效应）。若 |观测效应| < MDE，
     直接打印"不可判定"，而不是让你对着 20 个样本读方向。

用法：
    python scripts/analyze_signal_ledger.py                 # 分析台账
    python scripts/analyze_signal_ledger.py --horizon 20
    python scripts/analyze_signal_ledger.py --control index # 用指数当对照
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data/cache/calc_indicators"
LEDGER = ROOT / "data/observations/signal_ledger.csv"
HORIZONS = (5, 10, 20)


def load_ledger(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(f"❌ 台账不存在: {path}\n   先用 scripts/log_signal_ledger.py 记录")
    d = pd.read_csv(path, dtype={"symbol": str})
    d["symbol"] = d["symbol"].astype(str).str.zfill(6)
    d["date"] = pd.to_datetime(d["date"])
    for c in ("a_pass", "b_pass", "c_pass", "gate_passed"):
        if c in d.columns:
            d[c] = d[c].astype(str).str.lower().isin(("true", "1", "yes"))
    if "bought" not in d.columns:
        d["bought"] = ""
    return d


def attach_forward(d: pd.DataFrame) -> pd.DataFrame:
    """按『次日开盘入场 → T+h 收盘』算前瞻收益（与回测同口径）。"""
    for h in HORIZONS:
        d[f"fwd{h}"] = np.nan
    for sym, g in d.groupby("symbol"):
        f = CACHE / f"stock_{sym}.csv"
        if not f.exists():
            continue
        px = pd.read_csv(f, usecols=["date", "open", "close"])
        px["date"] = pd.to_datetime(px["date"])
        o = px["open"].to_numpy(float)
        c = px["close"].to_numpy(float)
        pos = {dt: i for i, dt in enumerate(px["date"])}
        for r in g.index:
            p = pos.get(d.loc[r, "date"])
            if p is None or p + 1 >= len(px):
                continue
            e = o[p + 1]
            if not np.isfinite(e) or e <= 0:
                continue
            for h in HORIZONS:
                t = p + h
                if t < len(px):
                    d.loc[r, f"fwd{h}"] = (c[t] / e - 1) * 100
    return d


def attach_index_control(d: pd.DataFrame, horizon: int, code: str = "sh000300") -> pd.Series:
    f = ROOT / f"data/cache/index_{code}_history.csv"
    if not f.exists():
        raise SystemExit(f"❌ 指数缓存不存在: {f}")
    px = pd.read_csv(f, encoding="utf-8-sig")
    px["date"] = pd.to_datetime(px["date"])
    o = px["open"].to_numpy(float)
    c = px["close"].to_numpy(float)
    ret = {}
    for i, dt in enumerate(px["date"]):
        if i + 1 < len(px):
            t = i + horizon
            if t < len(px) and o[i + 1] > 0:
                ret[dt] = (c[t] / o[i + 1] - 1) * 100
    return d["date"].map(ret)


def loo_control(d: pd.DataFrame, mask: pd.Series, horizon: int,
                min_others: int = 5) -> pd.DataFrame:
    """留一法对照：同日、台账内、**排除被检验组自身**之后的均值与样本数。

    为什么不能用"同日全体中位数"：
        被检验的组本身就在"全体"里。若某天记了 10 只、其中 8 只属于该组，
        中位数几乎就是该组的代表值 → 超额被机械地压向 0，永远"不可判定"。
        排除自身后，对照才是真正的"那天的其它票"。

    返回 [mean, n_ctrl]：**n_ctrl 必须参与误差计算** ——
        对照本身也是估计值。只按被检验组的方差算 MDE 会严重高估功效，
        典型症状是把 8 个样本劈成两组时，两组的超额互为镜像且都被判"可判定"。
    """
    mean = pd.Series(np.nan, index=d.index, dtype=float)
    cnt = pd.Series(0, index=d.index, dtype=int)
    for _, g in d.groupby("date"):
        gm = mask.reindex(g.index).fillna(False)
        others = g.loc[~gm, f"fwd{horizon}"].dropna()
        if len(others) >= min_others:
            mean.loc[g.index] = others.mean()
            cnt.loc[g.index] = len(others)
    return pd.DataFrame({"mean": mean, "n": cnt})


def stats(sub: pd.DataFrame, horizon: int, control: pd.DataFrame | pd.Series | None,
          min_days: int = 10) -> dict:
    """按**交易日聚类**的统计。

    为什么必须聚类：
        同一交易日的多行高度相关（当天大盘走势相同）。若把它当独立样本，
        16 行来自 3 天会被算成 n=16 —— 有效样本其实只有 3，误差被低估约 √5 倍，
        于是把噪声报成"可判定"。这与回测里"绝不对单条记录独立 bootstrap"是同一条纪律。

    做法：先算每个交易日的组内均值，再对"日级序列"求均值与标准误。
    """
    r = sub[f"fwd{horizon}"].dropna()
    if len(r) < 2:
        return {"n": 0, "n_days": 0}
    days = sub.loc[r.index, "date"]
    sd = r.std(ddof=1)
    out = {"n": len(r), "mean": float(r.mean()), "sd": sd,
           "win": float((r > 0).mean() * 100), "min_days": min_days}
    if control is None:
        dm = r.groupby(days).mean()
        se = float(dm.std(ddof=1) / np.sqrt(len(dm))) if len(dm) >= 2 else float("nan")
        out.update({"n_days": len(dm), "mde": 2.8 * se})
        return out
    if isinstance(control, pd.DataFrame):
        cc = control["mean"].reindex(r.index)
    else:
        cc = control.reindex(r.index)
    ok = cc.notna()
    if ok.sum() < 2:
        return {"n": 0, "n_days": 0}
    ex = r[ok] - cc[ok].astype(float)
    dm = ex.groupby(days[ok]).mean()
    n_days = len(dm)
    se = float(dm.std(ddof=1) / np.sqrt(n_days)) if n_days >= 2 else float("nan")
    out.update({
        "n": int(ok.sum()), "n_days": n_days,
        "n_dropped": int(len(r) - ok.sum()),
        "excess": float(dm.mean()),
        "excess_se": se,
        "excess_t": float(dm.mean() / se) if se and se > 0 else float("nan"),
        "excess_mde": 2.8 * se if se == se else float("nan"),
    })
    return out


def line(lab: str, s: dict, horizon: int) -> None:
    if not s.get("n"):
        print(f"  {lab:38s} 样本不足（对照不可用或该组无数据）")
        return
    nd = s.get("n_days", 0)
    if "excess" not in s:
        print(f"  {lab:38s} n={s['n']:>4d}/{nd:>3d}天 均值={s['mean']:>+7.3f}pp "
              f"MDE={s['mde']:.3f} 胜率={s['win']:>4.1f}%")
        return
    if nd < s.get("min_days", 10):
        verdict = f"⚠️ 交易日不足({nd}<{s.get('min_days',10)})—— 不下结论"
    elif abs(s["excess_t"]) >= 1.96:
        verdict = "✅ 显著"
        if abs(s["excess"]) < s["excess_mde"]:
            verdict += "，但效应 < MDE（脆弱）"
    else:
        verdict = "⚠️ 不显著"
    extra = f" 丢弃{s['n_dropped']}行" if s.get("n_dropped") else ""
    print(f"  {lab:38s} n={s['n']:>4d}/{nd:>3d}天 超额={s['excess']:>+7.3f}pp "
          f"t={s['excess_t']:>+5.2f} MDE={s['excess_mde']:.3f} 胜率={s['win']:>4.1f}%  {verdict}{extra}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default=str(LEDGER))
    ap.add_argument("--horizon", type=int, default=10, choices=HORIZONS)
    ap.add_argument("--control", default="index", choices=("pool", "index", "none"),
                    help=("index=对指数（默认，功效高，回答『比我买指数好吗』）；"
                          "pool=同日留一法（回答『比同期其它票好吗』，纯选股，但需每天记很多票）；"
                          "none=只看绝对收益"))
    ap.add_argument("--index", default="sh000300")
    args = ap.parse_args()

    h = args.horizon
    d = load_ledger(Path(args.ledger))
    d = attach_forward(d)
    print(f"台账: {args.ledger}")
    print(f"  共 {len(d)} 行，覆盖 {d.date.nunique()} 个交易日 / {d.symbol.nunique()} 只标的")
    print(f"  可算 T+{h} 前瞻收益的行: {int(d[f'fwd{h}'].notna().sum())}")
    print("\n  ⚠️ 两个对照回答的是**不同问题**，别混用：")
    print("     · index 对照 =『这笔比我直接买指数好吗』→ 含 beta 与择时，贴近你的实际决策")
    print("     · pool  对照 =『这笔比同期其它票好吗』→ 剥离大盘，纯选股能力")
    print("     某组若跑赢指数却输给同期同侪 → 那是 beta/择时，不是选股。")

    control = None
    if args.control == "index":
        control = attach_index_control(d, h, args.index)
        print(f"  对照组: 指数 {args.index}")
    elif args.control == "pool":
        med_n = d.groupby("date")[f"fwd{h}"].apply(lambda s: s.notna().sum())
        print(f"  对照组: **留一法** —— 同日台账内、排除被检验组自身后的均值")
        print(f"          各日可算前瞻收益的标的数: 中位 {med_n.median():.0f}，最少 {med_n.min():.0f}，最多 {med_n.max():.0f}")
        if med_n.median() < 10:
            print("  ⚠️  单日记录数偏少。留一法需要每天有足够的『其它票』当对照，")
            print("     建议每天至少记 15~20 只（含你根本没打算买的），否则多数分组会被丢弃")

    def ctl_for(mask: pd.Series):
        return loo_control(d, mask, h) if args.control == "pool" else control

    def show(lab: str, mask: pd.Series) -> None:
        line(lab, stats(d[mask], h, ctl_for(mask)), h)

    print(f"\n=== T+{h} 前瞻收益（超额 = 该组 − {args.control} 对照）===")
    print("【维度组合】")
    for lab, m in [("三者全不通过", d[["a_pass", "b_pass", "c_pass"]].eq(False).all(axis=1)),
                   ("仅 C 通过", (~d.a_pass) & (~d.b_pass) & d.c_pass),
                   ("仅 A 通过", d.a_pass & (~d.b_pass) & (~d.c_pass)),
                   ("仅 B 通过", (~d.a_pass) & d.b_pass & (~d.c_pass)),
                   ("A+C", d.a_pass & ~d.b_pass & d.c_pass),
                   ("B+C", ~d.a_pass & d.b_pass & d.c_pass),
                   ("A+B", d.a_pass & d.b_pass & ~d.c_pass),
                   ("★A+B+C", d.a_pass & d.b_pass & d.c_pass),
                   ("★A+B+C 且门控放行", d.a_pass & d.b_pass & d.c_pass & d.gate_passed)]:
        show(lab, m)

    print("\n【门控否决】")
    for lab, m in [("无否决", d.gate_passed), ("有否决", ~d.gate_passed),
                   ("被SURVIVABILITY拦下", d.gate_vetoes.fillna("").str.contains("SURVIVABILITY")),
                   ("被RR_RATIO拦下", d.gate_vetoes.fillna("").str.contains("RR_RATIO"))]:
        show(lab, m)

    print("\n【按 A 的失败原因归类 —— 用于回答『放宽哪个阈值有用』】")
    am = d.a_msg.fillna("")
    for lab, m in [("A 因偏离超阈值未过", am.str.contains("偏离")),
                   ("A 因方向(非从上方回落)", am.str.contains("方向|上方")),
                   ("A 因缩量不足", am.str.contains("缩量")),
                   ("A 因非阳线/锤子线", am.str.contains("阳线|锤子")),
                   ("A 因均线向下/走平", am.str.contains("均线方向|向下")),
                   ("A 因追高被拒", am.str.contains("追高")),
                   ("A 通过", d.a_pass)]:
        show(lab, m)

    print("\n【按 C 的止损距离分档 —— 检验『C 靠极紧止损过关』的假说】")
    sd_ = pd.to_numeric(d.c_stop_dist_pct, errors="coerce")
    for lab, m in [("止损距离 <1%", sd_ < 1), ("止损距离 1~3%", (sd_ >= 1) & (sd_ < 3)),
                   ("止损距离 3~6%", (sd_ >= 3) & (sd_ < 6)), ("止损距离 ≥6%", sd_ >= 6),
                   ("C 通过", d.c_pass),
                   ("C 通过 且 止损距离 ≥3%（可存活）", d.c_pass & (sd_ >= 3))]:
        show(lab, m)

    print("\n【双因子打分 vs A/B/C —— 谁更准（这是并行观察的闭环）】")
    sc = pd.to_numeric(d.get("score_double"), errors="coerce") if "score_double" in d.columns \
        else pd.Series(np.nan, index=d.index)
    if sc.notna().sum() < 5:
        print("  ⚠️ 台账里没有可用的 score_double（或不足 5 行）")
        print("     → 用新版 scripts/log_signal_ledger.py 记录后才有（它同期写入打分）")
    else:
        # 逐日横截面五等分：分数在全天内部排名，符合它被使用的方式
        q = pd.Series(np.nan, index=d.index)
        for _, idx in d.groupby("date").groups.items():
            s = sc.loc[idx]
            if s.notna().sum() >= 5:
                q.loc[idx] = pd.qcut(s.rank(method="first"), 5, labels=False) + 1
        for k in range(1, 6):
            m = q == k
            if m.sum():
                show(f"  分数 Q{k}{'（最低）' if k == 1 else '（最高）' if k == 5 else ''}", m)
        topn = max(3, int(round(0.2 * sc.notna().sum())))
        top_mask = sc.rank(ascending=False, method="first") <= topn
        show(f"  分数前 {topn} 名", top_mask & sc.notna())
        # 与 A/B/C 的直接对照
        show("  对照：A/B/C 建议开仓(三维度全通过)", d.get("suggest_open", pd.Series(False, index=d.index)))
        show("  对照：≥2 维通过", pd.to_numeric(d.get("dim_passed"), errors="coerce") >= 2)

    print("\n【买 vs 没买 —— 这一节才回答『我的判断加了多少分』】")
    cand = d[["a_pass", "b_pass", "c_pass"]].any(axis=1)
    b = d["bought"].astype(str).str.strip()
    known = b.isin(("0", "1", "0.0", "1.0"))
    show("全部已记录标的", pd.Series(True, index=d.index))
    if known.sum() == 0:
        print("  ⚠️  bought 列还是空的 —— 去台账里填 1/0，这一节才有意义")
    else:
        show("候选(任一维度通过)", cand)
        show("  ├ 买了 (bought=1)", known & b.isin(("1", "1.0")))
        show("  └ 没买 (bought=0)", known & b.isin(("0", "0.0")))
        bu = stats(d[known & b.isin(("1", "1.0"))], h, ctl_for(known & b.isin(("1", "1.0"))))
        nb = stats(d[known & b.isin(("0", "0.0"))], h, ctl_for(known & b.isin(("0", "0.0"))))
        if bu.get("n") and nb.get("n") and "excess" in bu and "excess" in nb:
            gap = bu["excess"] - nb["excess"]
            se = np.sqrt((bu["excess_mde"] / 2.8) ** 2 + (nb["excess_mde"] / 2.8) ** 2)
            print(f"\n  买了 − 没买 = {gap:+.3f}pp   (SE≈{se:.3f}, t≈{gap/se if se else float('nan'):+.2f})")
            need = int((2.8 * np.sqrt((bu['sd'] ** 2 + nb['sd'] ** 2) / 2) / max(abs(gap), 1e-9)) ** 2) if gap else 0
            print(f"  以当前效应量，要把这个差确认下来还差大约 {max(0, need - bu['n'] - nb['n'])} 笔样本")

    print("\n注：MDE = 最小可检测效应（80%功效/5%双尾）。|效应| < MDE 时结论『不可判定』，")
    print("    此时任何『感觉还可以』都不构成证据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
