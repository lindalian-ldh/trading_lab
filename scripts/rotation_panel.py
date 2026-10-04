#!/usr/bin/env python
"""板块轮动资金确认板 —— 渲染 CLI（需求 #1 的面板）。

**定位**：一块"仪表盘"，不是"信号源"。**只做确认与证伪，不产生买入信号，不占仓位权重。**
口径已冻结（v1），见 `scripts/增强方案v2-板块轮动面板与ETF择时.md` 的「P0.5-A」。

用法:
    # ① 渲染最新交易日的面板
    .venv/bin/python scripts/rotation_panel.py

    # ② 指定日期（**支持历史日期**：LHB 已全量落盘，plates_rank 也支持历史）
    .venv/bin/python scripts/rotation_panel.py --date 2026-09-30

    # ③ 保存 Markdown 快照
    .venv/bin/python scripts/rotation_panel.py --save

    # ④ 导出逐日逐主题指标（供后续分析）
    .venv/bin/python scripts/rotation_panel.py --export

    # ⑤ 批量回填面板快照（历史可复现 ⇒ 不必"从今天开始累积"）
    .venv/bin/python scripts/rotation_panel.py --backfill 60

    # ⑥ 机器可读
    .venv/bin/python scripts/rotation_panel.py --json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

import core.lhb_store as L  # noqa: E402
from core.rotation_panel import (  # noqa: E402
    NA,
    PANEL_VERSION,
    add_time_series_metrics,
    attach_theme_rank,
    build_theme_daily,
    format_panel,
    new_entry_themes,
)
from core.theme_universe import THEMES, theme_constituent_counts  # noqa: E402

SNAPSHOT_DIR = ROOT / "data" / "reports" / "rotation"
EXPORT_PATH = ROOT / "data" / "lhb" / "theme_metrics.parquet"


# ====================================================================
# 组装
# ====================================================================

def _load_inputs(end: str, lookback_days: int):
    """读 LHB 累积 + 题材热度（只读本地，不联网）。"""
    concepts = L.load_concepts(end=end)
    stocks = L.load_stocks(end=end)
    rank = L.load_theme_rank(end=end)
    if concepts.empty:
        raise SystemExit("❌ LHB 题材明细为空 —— 先跑 scripts/lhb_update.py --backfill")
    dates = sorted(concepts["date"].unique())
    window = dates[-lookback_days:] if lookback_days > 0 else dates
    return concepts, stocks, rank, dates, window


def _compute(end: str, lookback_days: int = 200, fetch_rank: bool = True):
    concepts, stocks, rank, all_dates, window = _load_inputs(end, lookback_days)

    # 题材热度：若当日无缓存则联网取一次（1 次调用；实测该接口支持历史日期）
    if fetch_rank and (rank.empty or end not in set(rank["date"])):
        rank = L.fetch_theme_rank(end)

    counts = theme_constituent_counts()
    daily = build_theme_daily(concepts, stocks, window, THEMES, counts)
    daily = add_time_series_metrics(daily)
    daily = attach_theme_rank(daily, rank)

    state = L.load_state()
    hist_days = sorted(state.get("list", {}).keys())
    tier2_days = [d for d, n in state.get("detail", {}).items() if n]
    n_samples = max((daily["theme"] == t["theme"]).sum() for t in THEMES) if not daily.empty else 0
    latest = daily[daily["date"] == end] if not daily.empty else daily
    today_tier2 = int(latest["n_tier2"].fillna(0).sum()) if not latest.empty else 0

    suff = {
        "lhb_days": len(hist_days),
        "tier2_days": len([d for d in tier2_days if d <= end]),
        # 注意：必须显式 bool()，否则 numpy.bool_ 在 json.dumps 里会变成字符串 "True"
        "pct_rank_ready": bool(n_samples >= 60),
        "tier2_ready": bool(today_tier2 > 0),
        "window_days": len(window),
    }
    return daily, window, rank, suff


def _render(end: str, lookback_days: int = 200) -> tuple:
    daily, window, rank, suff = _compute(end, lookback_days)
    latest = daily[daily["date"] == end] if not daily.empty else daily
    # 状态标签需要"截至当日"的历史，直接复用 daily（已按日期升序）
    new_entries = new_entry_themes(daily, rank)
    text = format_panel(latest.sort_values("n_listed", ascending=False), daily,
                        new_entries=new_entries, data_sufficiency=suff, date=end)
    return text, latest, daily, suff


# ====================================================================
# main
# ====================================================================

def main() -> int:
    ap = argparse.ArgumentParser(description="板块轮动资金确认板（只显示，不决策）")
    ap.add_argument("--date", help="渲染日期 YYYY-MM-DD（默认：LHB 最新一天）")
    ap.add_argument("--save", action="store_true", help="保存 Markdown 快照到 data/reports/rotation/")
    ap.add_argument("--export", action="store_true", help="导出逐日逐主题指标 parquet")
    ap.add_argument("--backfill", type=int, metavar="N",
                    help="为最近 N 个交易日各存一份 Markdown 快照（历史可复现）")
    ap.add_argument("--lookback", type=int, default=200,
                    help="计算滚动/分位所用的回看交易日数（默认 200）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（当日各主题指标）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")

    state = L.load_state()
    hist = sorted(state.get("list", {}).keys())
    if not hist:
        print("❌ 龙虎榜累积为空 —— 先跑：.venv/bin/python scripts/lhb_update.py --backfill")
        return 1

    # —— 日期解析：请求日可能是**非交易日**（run_all 默认传"昨天"，周一即周日）
    #    或厂商尚未发布 ⇒ 一律回退到"<= 请求日的最近一个已抓交易日"，并显式说明。
    requested = args.date
    if requested:
        cand = [d for d in hist if d <= requested]
        if not cand:
            print(f"❌ 请求日 {requested} 之前没有任何已抓交易日（最早 {hist[0]}）")
            return 1
        end = cand[-1]
        if end != requested:
            print(f"ℹ️  请求日 {requested} 非交易日或尚无数据 → 回退到最近交易日 {end}")
    else:
        end = hist[-1]

    if args.backfill:
        daily, window, rank, suff = _compute(end, max(args.lookback, args.backfill + 5))
        targets = sorted(daily["date"].unique())[-args.backfill:]
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        for d in targets:
            txt, _, _, _ = _render(d, args.lookback)
            (SNAPSHOT_DIR / f"{d}_rotation_panel.md").write_text(txt, encoding="utf-8")
        print(f"✅ 已存 {len(targets)} 份快照 → {SNAPSHOT_DIR}")
        return 0

    text, latest, daily, suff = _render(end, args.lookback)

    if args.json:
        print(json.dumps({
            "date": end,
            "version": PANEL_VERSION,
            "data_sufficiency": suff,
            "themes": latest.to_dict(orient="records"),
        }, ensure_ascii=False, indent=2, default=str))
    else:
        print(text)

    if args.save:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        p = SNAPSHOT_DIR / f"{end}_rotation_panel.md"
        p.write_text(text, encoding="utf-8")
        print(f"\n✅ 快照已存: {p}")

    if args.export:
        EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        daily.to_parquet(EXPORT_PATH, index=False)
        print(f"✅ 指标已导出: {EXPORT_PATH}（{len(daily)} 行）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
