#!/usr/bin/env python3
"""维度A 的通过率漏斗：到底卡在哪一步。

为什么需要：`regime_plan.md` 记录了"A 实测通过率约 5%，更像一个永远说不的过滤器"，
但没回答**是哪一步在卡**。调参必须先定位瓶颈，否则只是把 5 道 AND 条件挨个放松。

做法：在本地缓存上回放 `check_dimension_a`，用 PULLBACK_DEBUG 的输出定位
每个 (股票, 交易日) 的**首次失败步骤**。

两条路径：
    突破分支  cur_close > 前 N 日高点 → `_check_breakout_from_above`（幅度 + 新鲜度）
    回踩分支  5 道 AND：偏离度 → 方向(须从上方回落) → 均线向上 → 缩量 → K线形态 → 确认窗口

用法：
    python scripts/analyze_dimension_a_funnel.py                    # 默认 40 只 × 50 天
    python scripts/analyze_dimension_a_funnel.py --stocks 80 --days 80
    python scripts/analyze_dimension_a_funnel.py -p aggressive_short
"""
from __future__ import annotations

import argparse
import contextlib
import io
import re
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/calc_indicators"))

CACHE = ROOT / "data/cache/calc_indicators"
STEP_ORDER = ["偏离度检查", "方向检查", "均线方向", "缩量检查", "K线形态", "确认窗口"]
RE_STEP = re.compile(r"❌\s*(偏离度检查|方向检查|均线方向|缩量检查|K线形态|确认窗口)")


def classify(out: str, res: tuple) -> tuple[str, int, bool]:
    """返回 (归类, 最远到达的步骤索引, 突破分支是否触发)。"""
    ok, msg = res
    msg = str(msg)
    breakout_fired = ("突破" in msg) or ("追高" in msg)
    if ok:
        return ("✅通过-突破" if msg.startswith("突破") else "✅通过-回踩"), len(STEP_ORDER), breakout_fired
    if "数据不足" in out or "数据不足" in msg:
        return "数据不足", -1, breakout_fired
    blocks = re.split(r"───\s*MA\d+\s*───", out)[1:]
    furthest = -1
    for b in blocks:
        for line in b.splitlines():
            m = RE_STEP.search(line)
            if m:
                furthest = max(furthest, STEP_ORDER.index(m.group(1)))
                break
    if furthest < 0:
        return ("突破质量过滤" if breakout_fired else "未触达回踩"), -1, breakout_fired
    return STEP_ORDER[furthest], furthest, breakout_fired


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", type=int, default=40)
    ap.add_argument("--days", type=int, default=50, help="每只票采样多少个交易日")
    ap.add_argument("-p", "--profile", default="", help="配置档名（默认 TradingConfig）")
    args = ap.parse_args()

    import config as C
    import main as M

    cfg = C.TradingConfig() if not args.profile else getattr(C, args.profile)()
    cfg.PULLBACK_DEBUG = True
    cfg.MOMENTUM_DEBUG = False
    cfg.MARKET_DEBUG = False

    files = sorted(CACHE.glob("stock_*.csv"))[: args.stocks]
    print(f"配置档: {args.profile or 'default'}")
    print(f"标的: {len(files)} 只   每只采样 {args.days} 个交易日")
    print(f"回踩参数: 均线={cfg.PULLBACK_MA_TARGETS} 偏离≤{cfg.PULLBACK_MAX_DEVIATION*100:.2f}% "
          f"方向约束={cfg.PULLBACK_REQUIRE_TOUCH_FROM_ABOVE} 均线向上={cfg.PULLBACK_REQUIRE_MA_UP} "
          f"缩量={cfg.PULLBACK_REQUIRE_SHRINK_VOLUME}(<{cfg.PULLBACK_VOLUME_SHRINK_RATIO}) "
          f"阳线={cfg.PULLBACK_REQUIRE_BULLISH_CANDLE} 窗口={cfg.PULLBACK_CONFIRM_BARS}")
    print(f"突破参数: 窗口={cfg.BREAKOUT_WINDOW} 幅度上限={cfg.BREAKOUT_MAX_PCT}\n")

    rng = np.random.default_rng(20260930)
    tally = Counter()
    step_fail_all = Counter()
    n_eval = 0
    per_stock_pass = Counter()

    for f in files:
        df = pd.read_csv(f)
        df["date"] = pd.to_datetime(df["date"])
        warm = 250
        if len(df) <= warm + 5:
            continue
        idxs = rng.choice(np.arange(warm, len(df)), size=min(args.days, len(df) - warm), replace=False)
        for i in sorted(idxs):
            sub = df.iloc[: i + 1]
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                try:
                    res = M.check_dimension_a(sub, cfg)
                except Exception:
                    continue
            out = buf.getvalue()
            cat, furthest, bf = classify(out, res)
            tally[cat] += 1
            n_eval += 1
            for b in re.split(r"───\s*MA\d+\s*───", out)[1:]:
                for line in b.splitlines():
                    m = RE_STEP.search(line)
                    if m:
                        step_fail_all[m.group(1)] += 1
                        break
            if cat.startswith("✅"):
                per_stock_pass[f.stem.replace("stock_", "")] += 1

    print("=" * 74)
    print(f"共回放 {n_eval} 个 (股票, 交易日)")
    print("=" * 74)
    print(f"\n【A 的结果分布】")
    for k, v in tally.most_common():
        print(f"  {k:18s} {v:>6d}  {v/n_eval*100:>6.2f}%  {'█'*int(round(v/n_eval*40))}")
    passed = sum(v for k, v in tally.items() if k.startswith("✅"))
    print(f"\n  → 维度A 通过率 = {passed}/{n_eval} = {passed/n_eval*100:.2f}%")

    print(f"\n【未通过者的「最远到达步骤」分布（定位瓶颈）】")
    tail = [(k, v) for k, v in tally.items() if not k.startswith("✅")]
    for k, v in sorted(tail, key=lambda x: -x[1]):
        print(f"  {k:18s} {v:>6d}  {v/n_eval*100:>6.2f}%")

    print(f"\n【所有均线尝试的「首次失败步骤」频次（含被更早步骤挡掉后的 continue）】")
    tot = sum(step_fail_all.values()) or 1
    for s in STEP_ORDER:
        v = step_fail_all.get(s, 0)
        print(f"  {s:10s} {v:>6d}  {v/tot*100:>6.2f}%  {'█'*int(round(v/tot*40))}")

    if per_stock_pass:
        print(f"\n【通过 A 的标的】{len(per_stock_pass)}/{len(files)} 只至少通过一次")
        for c, n in per_stock_pass.most_common(15):
            print(f"    {c}  {n} 次")
    return 0


if __name__ == "__main__":
    sys.exit(main())
