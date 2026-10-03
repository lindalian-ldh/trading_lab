#!/usr/bin/env python3
"""半自动填充台账的 bought 列：用你的资金流水匹配"系统标记过、我到底买没买"。

为什么要半自动：
    `bought` 是整个观察的核心（唯一能把你和系统拆开的对照组），但手工填 30 行/天很烦。
    你的资金流水里已经有确切的买入记录，能匹配的直接填，剩下的才需要你人工判断。

匹配规则（保守，宁可漏配也不错配）：
    · 标的代码相同
    · 买入方向：业务名称含「买入」，或成交数量 > 0
    · 成交日期落在 [信号日, 信号日 + 窗口] 内（默认 5 个自然日 ≈ 覆盖 T+1/T+2 + 周末）

三层保护：
    ① 默认**只填空值**，不覆盖你手工填过的（用 --force 才覆盖）
    ② 写入 bought_src 列标明来源：`auto:<文件名>` / `manual` / `none`
    ③ 匹配不到的记为 0，但会单独统计 —— "流水里有买入、台账里没有对应行"
       就是**你自己加的、系统没标记的票**，那批本身是重要信息

用法：
    python scripts/autofill_ledger_bought.py --dry-run     # 先看会填什么
    python scripts/autofill_ledger_bought.py               # 正式填（只填空值）
    python scripts/autofill_ledger_bought.py --window 3
    python scripts/autofill_ledger_bought.py --force       # 覆盖已有值
"""
from __future__ import annotations

import argparse
import glob
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/fupan-report"))

LEDGER = ROOT / "data/observations/signal_ledger.csv"
FLOW_GLOB = "data/reports/历史资金流水_*.xls*"


def load_buys(patterns: list[str]) -> tuple[pd.DataFrame, list[str]]:
    from data_loader import load_flow
    frames, used = [], []
    for pat in patterns:
        for f in sorted(glob.glob(str(ROOT / pat))):
            try:
                df, _ = load_flow(f)
            except Exception as e:
                print(f"  ⚠️ 跳过 {Path(f).name}: {type(e).__name__}: {str(e)[:60]}")
                continue
            df["src_file"] = Path(f).name
            frames.append(df)
            used.append(Path(f).name)
    if not frames:
        raise SystemExit(f"❌ 没找到可读的资金流水文件（模式: {patterns}）")
    allt = pd.concat(frames, ignore_index=True)
    allt["证券代码"] = allt["证券代码"].astype(str).str.strip().str.zfill(6)
    allt["成交日期"] = pd.to_datetime(allt["成交日期"], errors="coerce")
    name = allt.get("业务名称", pd.Series("", index=allt.index)).astype(str)
    qty = pd.to_numeric(allt.get("成交数量"), errors="coerce")
    is_buy = name.str.contains("买入") | (qty > 0)
    buys = allt[is_buy & allt["成交日期"].notna()].copy()
    key = ["证券代码", "成交日期", "成交数量", "发生金额"]
    buys = buys.drop_duplicates(subset=[c for c in key if c in buys.columns])
    return buys, used


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default=str(LEDGER))
    ap.add_argument("--flow", action="append", default=None,
                    help=f"资金流水 glob（可重复）。默认 {FLOW_GLOB}")
    ap.add_argument("--window", type=int, default=5,
                    help="信号日之后多少个自然日内买入算『买了』（默认 5）")
    ap.add_argument("--force", action="store_true", help="覆盖已有 bought 值")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    led_path = Path(args.ledger)
    if not led_path.exists():
        raise SystemExit(f"❌ 台账不存在: {led_path}\n   先跑 scripts/log_signal_ledger.py")
    led = pd.read_csv(led_path, dtype={"symbol": str})
    led["symbol"] = led["symbol"].astype(str).str.zfill(6)
    led["date"] = pd.to_datetime(led["date"])

    buys, used = load_buys(args.flow or [FLOW_GLOB])
    print(f"读取资金流水: {len(used)} 个文件  买入记录 {len(buys)} 笔")
    for u in used:
        print(f"    · {u}")
    if buys.empty:
        raise SystemExit("❌ 流水里没有买入记录，无法匹配")

    by_sym: dict[str, list] = defaultdict(list)
    for r in buys.itertuples(index=False):
        by_sym[r.证券代码].append((r.成交日期, float(getattr(r, "成交价格", float("nan"))),
                                   float(getattr(r, "成交数量", float("nan"))),
                                   getattr(r, "src_file", "")))
    for k in by_sym:
        by_sym[k].sort()

    for c in ("bought_src", "buy_date", "buy_price", "buy_qty"):
        if c not in led.columns:
            led[c] = ""
    if "bought" not in led.columns:
        led["bought"] = ""
    # 空的 bought 列会被 pandas 读成 float64(NaN)，之后写字符串 "1" 会报
    # TypeError: Invalid value '1' for dtype 'float64'。统一转 object。
    for c in ("bought", "bought_src", "buy_date", "buy_price", "buy_qty"):
        led[c] = led[c].astype(object)

    filled = skipped = matched = 0
    hits = []
    for i, row in led.iterrows():
        existing = str(row.get("bought", "")).strip()
        if existing in ("0", "1", "0.0", "1.0") and not args.force:
            skipped += 1
            continue
        sig = row["date"]
        cands = [b for b in by_sym.get(row["symbol"], [])
                 if sig <= b[0] <= sig + timedelta(days=args.window)]
        if cands:
            dt, px, qty, src = min(cands, key=lambda x: x[0])
            hits.append((row["symbol"], sig.strftime("%Y-%m-%d"), dt.strftime("%Y-%m-%d"),
                         f"{px:.3f}", f"{qty:.0f}"))
            led.at[i, "bought"] = "1"
            led.at[i, "buy_date"] = dt.strftime("%Y-%m-%d")
            led.at[i, "buy_price"] = px
            led.at[i, "buy_qty"] = qty
            led.at[i, "bought_src"] = f"auto:{src}"
            matched += 1
        else:
            led.at[i, "bought"] = "0"
            led.at[i, "bought_src"] = "auto:none"
        filled += 1

    print(f"\n台账 {len(led)} 行：本次填写 {filled} 行（其中匹配到买入 {matched} 行），"
          f"跳过已有值 {skipped} 行")
    if hits:
        print("\n匹配到买入的（前 20 条）：")
        print(f"  {'代码':8s} {'信号日':12s} {'买入日':12s} {'价格':>8s} {'数量':>8s}")
        for h in hits[:20]:
            print(f"  {h[0]:8s} {h[1]:12s} {h[2]:12s} {h[3]:>8s} {h[4]:>8s}")
        if len(hits) > 20:
            print(f"  … 共 {len(hits)} 条")

    # —— 反向统计：流水里有买入、但台账没有对应行 = 系统没标记、你自己加的 ——
    if not led.empty:
        lo, hi = led["date"].min(), led["date"].max() + timedelta(days=args.window)
        led_keys = set(zip(led["symbol"], led["date"]))
        unmatched = []
        for sym, dts in by_sym.items():
            for dt, px, qty, src in dts:
                if not (lo <= dt <= hi):
                    continue
                if not any((sym, dt - timedelta(days=k)) in led_keys
                           for k in range(0, args.window + 1)):
                    unmatched.append((sym, dt.strftime("%Y-%m-%d"), px, qty))
        print(f"\n【重要】流水里有买入、但台账无对应标记的：{len(unmatched)} 笔"
              f"（{len({(u[0], u[1]) for u in unmatched})} 个 代码-日期）")
        print("  这些是**系统没标记、你自己决定买的** —— 它们本身就是最有价值的信息：")
        print("  如果你的盈利主要来自这批，说明 edge 在你的判断，不在系统。")
        agg: dict[tuple, list] = {}
        for sym, ds, px, qty in unmatched:
            a = agg.setdefault((sym, ds), [0.0, 0, px])
            a[0] += abs(qty or 0)
            a[1] += 1
        for (sym, ds), (q, n, px) in sorted(agg.items(), key=lambda x: -x[1][0])[:15]:
            print(f"    {sym} {ds}  成交 {n:>2d} 笔  合计 {q:>8.0f} 股  均价约 {px:.3f}")
        if len(agg) > 15:
            print(f"    … 共 {len(agg)} 个 代码-日期")

    if args.dry_run:
        print("\n（--dry-run：未写盘）")
        print(f"  预览：bought=1 共 {int((led['bought'].astype(str)=='1').sum())} 行，"
              f"bought=0 共 {int((led['bought'].astype(str)=='0').sum())} 行")
        return 0
    led.to_csv(led_path, index=False, encoding="utf-8-sig")
    print(f"\n✅ 已写回 {led_path}")
    print("   bought_src 列标明来源：auto:<文件名> / auto:none / manual（你手填的）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
