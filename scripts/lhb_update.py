#!/usr/bin/env python
"""龙虎榜累积层 CLI —— 每日增量 / 历史回填 / 覆盖体检。

**为什么需要它**：zzshare 龙虎榜历史只能回溯到 2025-01-02，
每拖一天就永久少一天历史。本脚本的第一用途是**每天收盘后落盘**。

用法:
    # ① 每日增量（推荐：收盘后跑一次，几秒钟）
    .venv/bin/python scripts/lhb_update.py

    # ② 首次全量回填（约 430 个交易日 × 1.5s ≈ 11 分钟，可断点续跑）
    .venv/bin/python scripts/lhb_update.py --backfill

    # ③ 分批回填（每次最多 100 天）
    .venv/bin/python scripts/lhb_update.py --backfill --max-days 100

    # ④ 补 Tier 2（毛额 buy_total/sell_total + 席位，用于"买卖比"）。**昂贵：1 次/股**
    .venv/bin/python scripts/lhb_update.py --tier 2 --start 2026-09-01

    # ⑤ 覆盖体检 / 还差哪些天
    .venv/bin/python scripts/lhb_update.py --status
    .venv/bin/python scripts/lhb_update.py --missing

    # ⑥ 刷新题材表（801xxx → 题材名）
    .venv/bin/python scripts/lhb_update.py --theme-table
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.lhb_store import (  # noqa: E402
    EARLIEST_DATE,
    fetch_theme_table,
    load_concepts,
    load_stocks,
    missing_days,
    status,
    update,
)


def _print_status() -> int:
    st = status()
    print("=" * 72)
    print("龙虎榜累积层 · 覆盖体检")
    print("=" * 72)
    print(f"  可用历史起点      : {st['earliest']}（zzshare 上限，实测）")
    print(f"  已记录交易日      : {st['days_recorded']} 天"
          f"（其中有上榜数据 {st['days_with_data']} 天）")
    print(f"  日期范围          : {st['first_date']} ~ {st['last_date']}")
    print(f"  个股日表行数      : {st['stock_rows']:,}（去重个股 {st['distinct_stocks']:,} 只）")
    print(f"  题材明细行数      : {st['concept_rows']:,}（涉及题材 {st['distinct_plates']:,} 个）")
    print(f"  席位明细行数      : {st['seat_rows']:,}")
    print(f"  Tier2(毛额)覆盖天 : {st['detail_days']} 天")

    miss = missing_days()
    print(f"  尚未抓取交易日    : {len(miss)} 天" + (f"（最早 {miss[0]}）" if miss else ""))
    if miss:
        print("  ⚠️  存在缺口 —— 跑 `--backfill` 补齐（可断点续跑）")

    # 空洞可见性：记成 0 的交易日几乎不可能是真的（全期日均 71.8 只、最少 46 只）
    zero = st.get("zero_days") or []
    if zero:
        print(f"  ⚠️  记成「0 只上榜」的交易日: {len(zero)} 天 —— 疑似静默空洞")
        print(f"       {', '.join(zero[:10])}" + (" …" if len(zero) > 10 else ""))
        print(f"       如确认厂商已发布而本地为空，用 --force 重取这些日子")
    else:
        print("  静默空洞检查      : ✅ 无（没有记成 0 只上榜的交易日）")

    # 与题材宇宙（plates_list(17)）的 join 命中率 = "板块归属能不能用"的判据
    if st["universe_hit_rate"] is not None:
        hit = st["universe_hit_rate"]
        print(f"  题材宇宙 join 率  : {hit * 100:.1f}%"
              f"（落在 plates_list(17) 内的行占比；命中的可查成分股 → 能算上榜率）")
        if hit < 1.0:
            print(f"                     未命中的 {(1 - hit) * 100:.1f}% 取不到成分股"
                  f"（如 信创/存储/数据要素/DeepSeek）→ 只能给上榜家数，不能给上榜率")
    else:
        print("  题材宇宙 join 率  : 未知（先跑 `--theme-table` 下载题材表）")
    if st["tier2_row_rate"] is not None:
        print(f"  Tier2 覆盖率      : {st['tier2_row_rate'] * 100:.1f}% 的个股日行有毛额"
              f"（买卖比需要它；未覆盖处应显示 n/a）")
    print("=" * 72)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="龙虎榜累积层：每日增量 / 历史回填 / 覆盖体检",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backfill", action="store_true",
                    help=f"回填全区间（{EARLIEST_DATE} ~ 今天），默认只做增量")
    ap.add_argument("--start", help="起始日期 YYYY-MM-DD（默认：增量=已抓末日之后；回填=可用起点）")
    ap.add_argument("--end", help="结束日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--tier", type=int, choices=(1, 2), default=1,
                    help="1=lhb_list(便宜,1次/天) 2=再加 lhb_detail(毛额+席位,1次/股)")
    ap.add_argument("--recent", type=int,
                    help="只对 Tier 2 生效：只在最近 N 个交易日内补 detail（必给，否则会去补历史 300+ 天）")
    ap.add_argument("--max-days", type=int, help="本次最多处理多少交易日（分批回填用）")
    ap.add_argument("--throttle", type=float, default=1.5, help="单次调用间隔秒数（默认 1.5）")
    ap.add_argument("--force", action="store_true", help="忽略已抓记录，强制重取")
    ap.add_argument("--status", action="store_true", help="只打印覆盖体检")
    ap.add_argument("--missing", action="store_true", help="列出尚未抓取的交易日")
    ap.add_argument("--theme-table", action="store_true", help="刷新题材表（801xxx→题材名）")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印调试日志")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")

    if args.status:
        return _print_status()

    if args.missing:
        miss = missing_days(args.start, args.end)
        print(f"尚未抓取的交易日：{len(miss)} 天")
        for d in miss[:50]:
            print("  ", d)
        if len(miss) > 50:
            print(f"   ... 另有 {len(miss) - 50} 天")
        return 0

    if args.theme_table:
        df = fetch_theme_table(force=True)
        print(f"✅ 题材表已刷新：{len(df)} 个题材")
        return 0

    update(start=args.start, end=args.end, tier=args.tier, force=args.force,
           throttle=args.throttle, max_days=args.max_days, recent=args.recent)
    print()
    _print_status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
