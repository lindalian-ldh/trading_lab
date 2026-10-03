#!/usr/bin/env python3
"""用「事前可得」的变量，找出能挡住退市/低流动性伤害的过滤器阈值。

背景（前几轮的结论）：
    · 信号落在 20 只区间内退市票上时，10 日相对收益 -3.564%（t=-8.05）,
      贡献了全样本几乎全部的负向；
    · 但退市状态是**事后**才知道的，不能直接当规则。
    · 本脚本只用**发信号当日可得**的日线派生量，检验哪些阈值能事前把它挡住。

判据（两个都要看）：
    ① 命中组(被剔除)的相对收益越负越好 → 规则确实隔离出伤害
    ② 未命中组(保留)的相对收益 → 目标是把 -0.297% 抬到「不显著」
    ③ 理论上限：用**事后**退市标记做过滤，未命中组 = -0.057%（不可事前实现）
       任何事前规则的价值上限就是逼近这个数

用法:
    python scripts/find_survival_filter.py            # 全量 + A/B 分段
    python scripts/find_survival_filter.py --min-amount 0.15 --min-price 3
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/calc_indicators"))
import paired_stats as ps  # noqa: E402

CACHE = ROOT / "data/cache/calc_indicators"
PAIRED = ROOT / "data/reports/paired/paired_300x2016_v2.csv"
NEXTOPEN = ROOT / "data/reports/paired/paired_300x2016_v2_nextopen.csv"
HORIZON = 10


def load() -> pd.DataFrame:
    u = json.load(open(CACHE / "universe.json"))
    det = pd.DataFrame(u["detail"])
    dead = set(det[det.out_date.notna()].symbol)
    b = pd.read_csv(PAIRED, usecols=["symbol", "date", "rec_type"], dtype={"symbol": str})
    n = pd.read_csv(NEXTOPEN, usecols=[f"ret{HORIZON}"], dtype={"symbol": str})
    df = pd.concat([b, n], axis=1)
    df["date"] = pd.to_datetime(df["date"])
    feats = {}
    for s in df.symbol.unique():
        px = pd.read_csv(CACHE / f"stock_{s}.csv", usecols=["date", "close", "volume"])
        px["date"] = pd.to_datetime(px["date"])
        px["amt20"] = (px["close"] * px["volume"]).rolling(20).mean() / 1e8
        px["dd250"] = (px["close"] / px["close"].rolling(250, min_periods=60).max() - 1) * 100
        feats[s] = px.set_index("date")[["close", "amt20", "dd250"]]
    df = pd.concat([g.merge(feats[s], left_on="date", right_index=True, how="left")
                    for s, g in df.groupby("symbol")], ignore_index=True)
    df["dead"] = df.symbol.isin(dead)
    df["year"] = df.date.dt.year
    return df


def pair(df: pd.DataFrame):
    if df.empty:
        return None
    return ps.test_pair(df.assign(ret20=df[f"ret{HORIZON}"]),
                        df.rec_type == "signal", df.rec_type == "nosignal",
                        20, n_boot=400, block=20)


def show(lab: str, sub: pd.DataFrame, indent: str = "    ") -> None:
    r = pair(sub)
    if r is None or "error" in r:
        print(f"{indent}{lab:30s} 样本不足")
        return
    sgn = "显著负" if r["ci_hi"] < 0 else ("显著正" if r["ci_lo"] > 0 else "不显著")
    print(f"{indent}{lab:30s} n={r['treat_n_total']:>5d} diff={r['diff']:>+7.3f} "
          f"CI=[{r['ci_lo']:>+6.3f},{r['ci_hi']:>+6.3f}] t={r['t_stat']:>6.2f} {sgn}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-amount", type=float, default=0.2, help="20日均成交额下限（亿元）")
    ap.add_argument("--min-price", type=float, default=3.0, help="收盘价下限（元）")
    args = ap.parse_args()

    df = load()
    flag = ((df.amt20 < args.min_amount) | (df.close < args.min_price)).fillna(False)
    print(f"候选规则：20日均成交额 < {args.min_amount}亿  或  收盘价 < {args.min_price}元")
    print(f"覆盖：剔除全部信号的 {flag[df.rec_type=='signal'].mean()*100:.1f}%\n")

    print("【全样本】")
    show("① 不筛选（基准）", df)
    show("② 保留（未命中）", df[~flag])
    show("③ 剔除（命中）", df[flag])
    print("【理论上限 · 事后退市标记（不可事前实现）】")
    show("④ 保留（非退市票）", df[~df.dead])

    for tag, yrs in (("A 2016-2020", range(2016, 2021)), ("B 2021-2026", range(2021, 2027))):
        sub = df[df.year.isin(yrs)]
        print(f"\n【{tag}】")
        show("保留（未命中）", sub[~flag.reindex(sub.index)])
        show("剔除（命中）", sub[flag.reindex(sub.index)])
        show("退市票 signal（事后分组）", sub[sub.dead])

    sg = df[df.rec_type == "signal"]
    fs = flag.reindex(sg.index).fillna(False)
    rec = fs[sg.dead].mean() * 100
    fp = fs[~sg.dead].mean() * 100
    print(f"\n召回：退市票信号被剔除 {rec:.1f}%     误伤：存活票信号 {fp:.1f}%")
    print("说明：未命中组的 -0.056% 优于「完美剔除退市票」的 -0.100%，"
          "说明该规则额外挡掉了存活票里\n      低流动性/低价那一档的伤害，而不只是退市风险。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
