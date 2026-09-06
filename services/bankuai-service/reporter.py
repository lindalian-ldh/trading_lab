"""控制台报告输出：把扫描报告格式化为易读的卡片式文本。"""

from __future__ import annotations

from typing import Any


def _fmt_change(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "0.00"
    sign = "+" if f >= 0 else ""
    return f"{sign}{f:.2f}"


def _print_sector_line(s: dict) -> str:
    return (f"  #{s.get('rank', '-'):>3}  {s.get('name', ''):<10} "
            f"涨幅 {_fmt_change(s.get('change')):>7}%  "
            f"成交额 {s.get('turnover', 0):>8.1f}亿  "
            f"热度 {s.get('score', 0):.0f}")


def _print_stock_line(s: dict, idx: int, show_score: bool = False) -> str:
    base = (f"    {idx}. {s.get('code', '')} {s.get('name', ''):<10} "
            f"涨幅 {_fmt_change(s.get('change')):>7}%  "
            f"换手 {s.get('turnover_rate', 0):>6.2f}%  "
            f"量比 {s.get('vol_ratio', 0):>5.2f}  "
            f"rank {s.get('rank', '-')}")
    if show_score:
        base += f"  评分 {s.get('score', 0):.1f}"
    return base


def print_report(report: dict) -> None:
    """把完整扫描报告打印到控制台。"""
    sep = "=" * 64
    print(sep)
    print(f"📊 板块扫描报告  {report.get('scan_time', '')}  "
          f"[{report.get('plate_type', '')}]  日期 {report.get('date1', '')}")
    print(sep)

    summary = report.get("summary", {})
    print(f"  目标板块: {summary.get('total_sectors', 0)}  "
          f"成功: {summary.get('success_count', 0)}  "
          f"失败: {summary.get('failed_sectors', []) or '无'}")

    # ---- 热门板块 ----
    hot = report.get("hot_sectors", [])
    print("\n" + "-" * 64)
    print(f"🔥 热门板块 Top{len(hot)}")
    print("-" * 64)
    for s in hot:
        print(_print_sector_line(s))

    # ---- 冷门板块 ----
    cold = report.get("cold_sectors", [])
    print("\n" + "-" * 64)
    print(f"❄️  冷门板块 Bottom{len(cold)}")
    print("-" * 64)
    for s in cold:
        print(_print_sector_line(s))

    # ---- 龙头梯队详情 ----
    sector_leaders = report.get("sector_leaders", {})
    if sector_leaders:
        print("\n" + "=" * 64)
        print("🎯 各板块龙头梯队")
        print("=" * 64)
        for name, detail in sector_leaders.items():
            print(f"\n┌─ {name}  (板块均幅 {_fmt_change(detail.get('avg_change'))}%)")
            for tier_name, label in [("tier1", "一级·领涨龙"),
                                     ("tier2", "二级·中军"),
                                     ("tier3", "三级·补涨")]:
                tier = detail.get(tier_name, [])
                print(f"│ {label} ({len(tier)} 只):")
                if not tier:
                    print("│    （无）")
                for i, st in enumerate(tier, 1):
                    print(f"│  {_print_stock_line(st, i, show_score=(tier_name == 'tier1'))}")
            print("└" + "-" * 60)

    # ---- 涨停连板梯队 ----
    _print_uplimit_ladder(report.get("uplimit_ladder", {}))

    print("\n" + sep)
    print("✅ 扫描完成")
    print(sep)


# ====================================================================
# 涨停连板梯队输出
# ====================================================================

def _print_ladder_stock_line(s: dict, idx: int) -> str:
    return (f"      {idx}. {s.get('code', '')} {s.get('name', ''):<10} "
            f"封板 {s.get('seal_time', '--:--')}  "
            f"涨幅 {_fmt_change(s.get('change_pct')):>7}%  "
            f"{s.get('reason', '')}")


def _print_uplimit_ladder(uplimit_ladder: dict) -> None:
    sep64 = "-" * 64
    meta = uplimit_ladder.get("meta", {})
    err = meta.get("error")

    print("\n" + "=" * 64)
    print("🏆 涨停连板梯队")
    print("=" * 64)
    if err and "未启用" not in str(err) and "板块排名失败" not in str(err):
        print(f"  ⚠️  获取失败: {err}")
        return
    if err and ("未启用" in str(err) or "板块排名失败" in str(err)):
        print(f"  （{err}）")
        return

    ladders = uplimit_ladder.get("ladders", [])
    if not ladders:
        print("  （当日无涨停连板数据）")
    else:
        for grp in ladders:
            label = grp.get("label", "")
            total = grp.get("total_count", 0)
            shown = grp.get("stocks", [])
            has_more = grp.get("has_more", False)
            head_count = f"（共 {total} 只" + (f"，仅展示前 {len(shown)}" if has_more else "") + "）"
            print(f"\n{sep64}")
            print(f"  🪜 {label}  {head_count}")
            print(sep64)
            for i, s in enumerate(shown, 1):
                print(_print_ladder_stock_line(s, i))

    # uplimit_hot 热门板块（若启用）
    hot_list = uplimit_ladder.get("uplimit_hot", [])
    if hot_list:
        print(f"\n{sep64}")
        print("  🔥 涨停热门板块")
        print(sep64)
        for h in hot_list:
            print(f"    · {h.get('name', ''):<10}  "
                  f"涨停家数 {h.get('uplimit_count', 0):>3}  "
                  f"最高连板 {str(h.get('continuous_head', '')):<8}  "
                  f"涨幅 {_fmt_change(h.get('change_pct')):>7}%")
