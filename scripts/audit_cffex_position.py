#!/usr/bin/env python
"""检验『看中信期货的加多/加空来预判第二天行情』这个做法。

这是散户圈流传很广的说法。它有一个**统计上很有利**的特点：
    预测目标是**第二天**（1 日周期），**不存在重叠窗口问题** ——
    所以它的 t 值可以直接信（不像基差那种 20 日重叠、需 Newey-West 校正）。

但它在别处有四个硬伤，本脚本逐条检验：

    ① **会员同时持多空** —— 中金所公布的是"持买单量"与"持卖单量"两张榜，
       中信期货(代客) 常常**同时在两张榜的前列**。所以"中信加多"这种说法
       本身就不严谨，必须看**净头寸 = 持买 − 持卖**及其变化。
    ② **套保与投机无法区分** —— 空头里有大量是机构给现货做对冲，
       那是"锁仓"不是"看跌"。中金所数据不披露动机。
    ③ **数据是收盘后公布的** —— 大约 15:30~16:00 才出。你最早只能用于**次日**，
       而次日开盘时全市场都看到了 —— 存在"已被定价"的可能。
    ④ **无法证伪的记忆** —— 人只记得"中信加空第二天跌了"的那几次。

检验设计：
    · 净头寸 net = 持买单量 − 持卖单量（对 IF 各合约求和）
    · 信号 = net 的日变化 Δnet（"加多"为正、"加空"为负）
    · 目标 = 次日指数收益（**不重叠，t 值可直接信**）
    · 同时检验：前 20 席位合计净空的变化、以及"净头寸水平"（而非变化）
    · 分段一致性（2017-2020 / 2021-2026）

用法:
    uv run python scripts/audit_cffex_position.py                # 用缓存，缺则抓取
    uv run python scripts/audit_cffex_position.py --refresh      # 强制重抓
    uv run python scripts/audit_cffex_position.py --since 2021-01-01
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "cache"
POS_CACHE = CACHE / "cffex_position.json"

MEMBER = "中信期货"          # 关注会员（用前缀匹配，榜单里写作"中信期货(代客)"）
TOP_N = 20                   # 前 20 席位
INDEX_FOR = {"IF": "sh000300", "IC": "sh000905", "IH": "sh000016", "IM": "sh000852"}


# ====================================================================
# 取数
# ====================================================================
def fetch_positions(dates: list[str], variety: str = "IF",
                    refresh: bool = False) -> dict:
    """抓取每日持仓排名。缓存到 data/cache/cffex_position.json。"""
    cache: dict = {}
    if POS_CACHE.exists() and not refresh:
        cache = json.loads(POS_CACHE.read_text(encoding="utf-8"))
    todo = [d for d in dates if d not in cache]
    if not todo:
        print(f"  持仓数据全部命中缓存（{len(cache)} 天）")
        return cache
    import akshare as ak
    print(f"  需抓取 {len(todo)} 天（约 {len(todo) * 0.12 / 60:.1f} 分钟）…")
    t0 = time.time()
    for k, d in enumerate(todo, 1):
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                r = ak.get_cffex_rank_table(date=d.replace("-", ""), vars_list=[variety])
            rec = {}
            if isinstance(r, dict):
                for sym, df in r.items():
                    if not isinstance(df, pd.DataFrame) or df.empty:
                        continue
                    mem = {"long": 0, "short": 0, "long_chg": 0, "short_chg": 0}
                    for _, row in df.iterrows():
                        ln = str(row.get("long_party_name", ""))
                        sn = str(row.get("short_party_name", ""))
                        if MEMBER in ln:
                            mem["long"] = float(row.get("long_open_interest", 0) or 0)
                            mem["long_chg"] = float(row.get("long_open_interest_chg", 0) or 0)
                        if MEMBER in sn:
                            mem["short"] = float(row.get("short_open_interest", 0) or 0)
                            mem["short_chg"] = float(row.get("short_open_interest_chg", 0) or 0)
                    # 前 N 席位合计
                    top_l = pd.to_numeric(df["long_open_interest"], errors="coerce").head(TOP_N).sum()
                    top_s = pd.to_numeric(df["short_open_interest"], errors="coerce").head(TOP_N).sum()
                    rec[sym] = {"mem_long": mem["long"], "mem_short": mem["short"],
                                "mem_long_chg": mem["long_chg"], "mem_short_chg": mem["short_chg"],
                                "top_long": float(top_l), "top_short": float(top_s)}
            cache[d] = rec
        except Exception as e:
            cache[d] = {"_error": f"{type(e).__name__}: {str(e)[:60]}"}
        if k % 100 == 0:
            print(f"    {k}/{len(todo)}  {time.time() - t0:.0f}s")
            POS_CACHE.parent.mkdir(parents=True, exist_ok=True)
            POS_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    POS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    POS_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    print(f"  完成，用时 {time.time() - t0:.0f}s，缓存 {len(cache)} 天")
    return cache


def aggregate(cache: dict, dates: list[str]) -> pd.DataFrame:
    """把每日各合约的持仓汇总成一条序列（各合约求和）。"""
    rows = []
    for d in dates:
        rec = cache.get(d, {})
        if not rec or "_error" in rec:
            rows.append({"date": d})
            continue
        agg = {"date": d}
        for k in ("mem_long", "mem_short", "mem_long_chg", "mem_short_chg",
                  "top_long", "top_short"):
            agg[k] = sum(v.get(k, 0) or 0 for v in rec.values())
        rows.append(agg)
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    if "mem_long" not in df.columns:
        return df
    for c in ("mem_long", "mem_short", "top_long", "top_short",
              "mem_long_chg", "mem_short_chg"):
        df[c] = pd.to_numeric(df.get(c), errors="coerce")
    df["mem_net"] = df["mem_long"] - df["mem_short"]          # 中信净头寸
    df["top_net"] = df["top_long"] - df["top_short"]          # 前20席位净头寸
    df["mem_net_chg"] = df["mem_net"].diff()                  # 净头寸变化（"加多/加空"）
    df["top_net_chg"] = df["top_net"].diff()
    return df


# ====================================================================
# 检验
# ====================================================================
def _t(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """相关性与 t 值（1 日周期，无重叠 → 普通 t 即可信）。"""
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    if len(x) < 30:
        return np.nan, np.nan
    r = np.corrcoef(x, y)[0, 1]
    tt = r * np.sqrt((len(x) - 2) / max(1e-12, 1 - r ** 2))
    return r, tt


def report(df: pd.DataFrame, idx: pd.DataFrame, variety: str) -> None:
    m = df.merge(idx[["date", "open", "close"]], on="date", how="inner")
    m = m.sort_values("date").reset_index(drop=True)
    # 今日收益（用于『这个信号是不是只是跟着当天行情走』的对照）
    m["today"] = (m["close"] / m["open"] - 1) * 100
    # ⚠️ 次日收益必须用 shift(-1)。曾写成当天 close/open —— 那算的是**当天自身收益**，
    #    得到 t=+8.5 的假显著（实际只是"会员跟着当天行情加减仓"）。
    #    持仓数据盘后公布 → 只能用于次日，所以这才是正确的检验口径。
    m["fwd1"] = m["today"].shift(-1)
    m = m.dropna(subset=["fwd1"])
    print(f"\n{'─' * 78}")
    print(f"{variety} / {INDEX_FOR[variety]}   样本 {len(m)} 天  "
          f"{m.date.min():%Y-%m-%d} ~ {m.date.max():%Y-%m-%d}")
    if "mem_net" not in m.columns or m["mem_net"].isna().all():
        print("  ⚠️ 无有效持仓数据"); return
    # 会员识别率
    hit = (m["mem_long"].fillna(0) > 0) | (m["mem_short"].fillna(0) > 0)
    print(f"  中信识别率 {hit.mean() * 100:.0f}%   平均持买 {m.mem_long.mean():,.0f} "
          f"持卖 {m.mem_short.mean():,.0f}   平均净头寸 {m.mem_net.mean():+,.0f} 手")
    print(f"  中信在多头榜天数占比 {(m.mem_long.fillna(0) > 0).mean() * 100:.0f}%  "
          f"空头榜 {(m.mem_short.fillna(0) > 0).mean() * 100:.0f}%  "
          f"← 两者都高说明**同时持多空**")

    # ⚠️ 必须同时看两列：
    #    「vs 当天」= 该信号是不是只是**跟着当天行情走**（同步指标）
    #    「vs 次日」= 它到底有没有**预测力**（这才是那个说法的内容）
    # 2026-10-01 教训：只算当天那一列会得到 t=+8.5 的假显著。
    print(f"\n  【核心检验】信号 vs 当天收益（同步？） 与 vs 次日收益（预测？）")
    print(f"    {'信号':<28s}{'vs当天 r':>10s}{'t':>7s}{'vs次日 r':>11s}{'t':>7s}   判定")
    tests = [
        ("mem_net_chg", "中信净头寸日变化(加多/加空)"),
        ("mem_net", "中信净头寸水平"),
        ("top_net_chg", "前20席位净头寸日变化"),
        ("top_net", "前20席位净头寸水平"),
    ]
    for col, lab in tests:
        if col not in m.columns: continue
        r0, t0 = _t(m[col].to_numpy(float), m["today"].to_numpy(float))
        r1, t1 = _t(m[col].to_numpy(float), m["fwd1"].to_numpy(float))
        print(f"    {lab:<28s}{r0:>+10.4f}{t0:>+7.2f}{r1:>+11.4f}{t1:>+7.2f}   "
              f"{'✅ 有预测力' if abs(t1) > 1.96 else '❌ 无预测力'}")
    r, tt = _t(m["today"].to_numpy(float), m["fwd1"].to_numpy(float))
    print(f"    {'【参照】今天收益 vs 次日收益':<28s}{'':>10s}{'':>7s}{r:>+11.4f}{tt:>+7.2f}   "
          f"{'✅ 1日反转' if abs(tt) > 1.96 else '❌'}")

    # 分组看：加多日 vs 加空日的次日收益
    v = m.dropna(subset=["mem_net_chg"])
    if len(v) > 100:
        q = v["mem_net_chg"]
        lo, hi = v[q <= q.quantile(0.2)], v[q >= q.quantile(0.8)]
        print(f"\n  【分组】中信『加空最多』20% 日 vs 『加多最多』20% 日 的次日收益")
        print(f"    加空最多: n={len(lo):>4d}  次日 {lo.fwd1.mean():+.3f}%  "
              f"胜率 {(lo.fwd1 > 0).mean() * 100:.0f}%")
        print(f"    加多最多: n={len(hi):>4d}  次日 {hi.fwd1.mean():+.3f}%  "
              f"胜率 {(hi.fwd1 > 0).mean() * 100:.0f}%")
        diff = hi.fwd1.mean() - lo.fwd1.mean()
        se = np.sqrt(hi.fwd1.var(ddof=1) / len(hi) + lo.fwd1.var(ddof=1) / len(lo))
        print(f"    差 {diff:+.3f}pp  t={diff / se:+.2f}  "
              f"{'✅ 显著' if abs(diff / se) > 1.96 else '❌ 不显著'}")
        print(f"    全样本次日均值 {m.fwd1.mean():+.3f}%")

    # 分段
    print(f"\n  【分段一致性】")
    for lab, sub in (("2017-2020", m[m.date <= "2020-12-31"]),
                     ("2021-2026", m[m.date >= "2021-01-01"])):
        if len(sub) < 100: continue
        r, tt = _t(sub["mem_net_chg"].to_numpy(float), sub["fwd1"].to_numpy(float))
        print(f"    {lab}: n={len(sub):>4d}  中信净头寸变化 相关 {r:+.4f}  t={tt:+.2f}  "
              f"{'✅' if abs(tt) > 1.96 else '❌'}")


def main() -> int:
    ap = argparse.ArgumentParser(description="检验『看中信加多空预判次日』")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--since", default="2017-01-01")
    ap.add_argument("--variety", default="IF")
    args = ap.parse_args()

    idx = pd.read_csv(CACHE / f"index_{INDEX_FOR[args.variety]}_history.csv",
                      encoding="utf-8-sig")
    idx["date"] = pd.to_datetime(idx["date"])
    dates = [d.strftime("%Y-%m-%d") for d in idx["date"] if d >= pd.Timestamp(args.since)]
    print("=" * 78)
    print("检验：『看中信期货加多/加空预判第二天』")
    print("=" * 78)
    print(f"交易日 {len(dates)} 天（{dates[0]} ~ {dates[-1]}）")
    cache = fetch_positions(dates, variety=args.variety, refresh=args.refresh)
    df = aggregate(cache, dates)
    report(df, idx[idx.date >= pd.Timestamp(args.since)], args.variety)

    print(f"\n{'=' * 78}\n【四个硬伤（本脚本检验的就是它们）】\n{'=' * 78}")
    print("  ① 会员同时持多空 → 必须看净头寸，不能只看'加多/加空'")
    print("  ② 套保与投机无法区分 → 空头里大量是对冲，不是看跌")
    print("  ③ 数据收盘后才公布 → 最早只能用于次日，可能已被定价")
    print("  ④ 只记得对的次数 → 所以必须看 t 值和胜率，不能靠感觉")
    return 0


if __name__ == "__main__":
    sys.exit(main())
