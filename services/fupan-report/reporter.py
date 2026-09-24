"""报告渲染与落盘。

将五大分析器的结构化结果按既定格式拼接为文本报告，
控制台打印与落盘 data/reports/fupan_{date}.txt。
业务计算（analyzers）与输出格式（reporter）解耦，便于二次开发扩展（如增加 HTML/图表）。
"""
from __future__ import annotations

from pathlib import Path


def format_money(x: float) -> str:
    """金额千分位 + 两位小数，如 123456.78 → '123,456.78'。"""
    return f"{x:,.2f}"


def _signed_money(x: float) -> str:
    """带正负号金额，如 +2,345.67 / -1,234.56。"""
    sign = "+" if x >= 0 else ""
    return f"{sign}{x:,.2f}"


def _reports_dir() -> Path:
    """获取报告落盘目录；core 不可用时降级到 项目根/data/reports。"""
    try:
        from config.settings import settings
        return settings.reports_dir
    except Exception:
        return Path(__file__).resolve().parents[2] / "data" / "reports"


def render_report(
    cost: dict,
    perf: dict,
    timing: dict,
    capital: dict,
    discipline: dict,
    date_range: tuple[str, str],
    cleaned_count: int,
) -> str:
    """按既定格式拼接完整复盘报告文本。"""
    start, end = date_range
    lines: list[str] = []

    lines.append("=" * 60)
    lines.append(f"     证券交易流水复盘报告 ({start} ~ {end})")
    lines.append("=" * 60)
    if cleaned_count:
        lines.append(f"(已自动剔除现金理财记录 {cleaned_count} 条)")
    lines.append("")

    # —— 1. 交易成本复盘 ——
    lines.append("【1. 交易成本复盘】")
    lines.append(f"总交易额(双边): {format_money(cost['total_turnover'])} 元")
    lines.append(
        f"总佣金: {format_money(cost['total_commission'])} 元 | "
        f"总印花税: {format_money(cost['total_stamp_tax'])} 元 | "
        f"总规费: {format_money(cost['total_fees'])} 元"
    )
    lines.append(
        f"实际佣金率: {cost['commission_rate_bps']:.2f} ‱ "
        f"(万分之{cost['commission_rate_bps']:.2f})"
    )
    if cost["warn"]:
        lines.append("⚠️ 警告：佣金过高，建议联系客户经理调整至万分之1.5！")
    if cost["top_commission_trades"]:
        lines.append("单笔佣金最高前5笔：")
        for i, t in enumerate(cost["top_commission_trades"], 1):
            lines.append(
                f"  {i}. {t['日期']} {t['证券名称']} [{t['方向']}] "
                f"佣金 {format_money(t['佣金'])} 元"
            )
    lines.append("")

    # —— 2. 标的绩效榜 ——
    lines.append(f"【2. 标的绩效榜 (Top {len(perf.get('profit_top', []))} 盈利)】")
    if perf.get("profit_top"):
        for i, r in enumerate(perf["profit_top"], 1):
            lines.append(
                f"{i}. {r['证券名称']}({r['证券代码']}): "
                f"{_signed_money(r['realized_pnl'])} 元 ({r['trade_count']}笔)"
            )
    else:
        lines.append("  (无)")
    lines.append("")
    loss_top = perf.get("loss_top", [])
    if loss_top:
        lines.append(f"-- 亏损拖累榜 (Top {len(loss_top)}) --")
        for i, r in enumerate(loss_top, 1):
            lines.append(
                f"{i}. {r['证券名称']}({r['证券代码']}): "
                f"{_signed_money(r['realized_pnl'])} 元 ({r['trade_count']}笔)"
            )
        lines.append("")

    # —— 3. 买卖点效率 ——
    lines.append("【3. 买卖点效率】")
    items = timing.get("items", [])
    if items:
        for it in items:
            cmp_sign = ">" if it["spread_rate"] > 0 else "<"
            lines.append(
                f"- {it['证券名称']}: 卖出均价 {it['avg_sell_price']:.4f} {cmp_sign} "
                f"买入均价 {it['avg_buy_price']:.4f}，{it['verdict']} "
                f"({it['spread_rate']:+.1f}%)"
            )
    else:
        lines.append("  (无同时含买卖双向操作的标的)")
    lines.append("")

    # —— 4. 资金与仓位 ——
    lines.append("【4. 资金与仓位】")
    holdings = capital.get("top_holdings", [])
    if holdings:
        hold_str = "、".join(
            f"{h['证券名称']}({int(h['剩余数量'])}股/估市值{format_money(h['估算市值'])}元)"
            for h in holdings
        )
        lines.append(f"截止{end}，前三大重仓：{hold_str}")
    else:
        lines.append(f"截止{end}，无持仓余额")
    lines.append(
        f"复盘期间日均买入金额: {format_money(capital['daily_avg_buy'])} 元 "
        f"(共{capital['n_days']}个交易日)"
    )
    # 每日净流入明细
    flows = capital.get("daily_net_flow", [])
    if flows:
        lines.append("每日资金净流入(正=净卖出回笼，负=净买入加仓)：")
        for f in flows:
            lines.append(f"  {f['日期']}: {_signed_money(f['net_flow'])} 元")
    lines.append("")

    # —— 5. 纪律与总结（按完整交易轮次聚合） ——
    lines.append("【5. 纪律与总结】")
    lines.append(
        f"总笔数: {discipline['buy_count'] + discipline['sell_count']}笔 "
        f"(买:{discipline['buy_count']}, 卖:{discipline['sell_count']}, "
        f"卖/买比={discipline['sell_buy_ratio']:.2f})"
    )
    lines.append(
        f"完整轮次: 已清仓 {discipline['closed_count']} 个 / "
        f"持仓中 {discipline['holding_count']} 个（持仓不计入盈亏比）"
    )
    # 真实盈亏比（按轮次）
    ratio_str = (
        f"{discipline['profit_loss_ratio']:.2f} : 1"
        if discipline["profit_loss_ratio"] != float("inf")
        else "∞（无亏损轮次）"
    )
    lines.append(
        f"真实盈亏比(按轮次): {ratio_str}  "
        f"(已实现盈利 {_signed_money(discipline['realized_profit_total'])} / "
        f"已实现亏损 {format_money(discipline['realized_loss_total'])})"
    )
    lines.append(
        f"轮次胜率: {discipline['win_rate']:.1f}% "
        f"(盈利轮 {discipline['win_rounds']} / 亏损轮 {discipline['loss_rounds']})"
    )
    # 盈亏分解：已实现 vs 浮动
    lines.append(
        f"盈亏分解: 已实现(扣费前) {_signed_money(sum(r['round_pnl'] for r in discipline['rounds_detail']))} 元 | "
        f"浮动 {_signed_money(discipline['floating_pnl'])} 元"
    )
    lines.append(
        f"扣费前总盈亏: {_signed_money(discipline['gross_pnl'])} 元 | "
        f"扣费后净盈亏: {_signed_money(discipline['net_pnl'])} 元"
    )
    lines.append(
        f"  其中佣金 {format_money(discipline['total_commission'])} + "
        f"印花税 {format_money(discipline['total_stamp_tax'])} + "
        f"规费 {format_money(discipline['total_fees'])}"
    )
    # 已清仓轮次明细（按净盈亏降序）
    detail = sorted(discipline["rounds_detail"], key=lambda x: x["round_pnl"], reverse=True)
    if detail:
        lines.append("已清仓轮次明细(按净盈亏降序)：")
        for r in detail:
            lines.append(
                f"  {r['证券名称']}({r['证券代码']}): "
                f"{_signed_money(r['round_pnl'])} 元 "
                f"(买{format_money(r['buy_cost'])}/卖{format_money(r['sell_income'])}, "
                f"{r['trade_count']}笔)"
            )
    lines.append(f">>> 结论：{discipline['conclusion']}")
    lines.append("=" * 60)

    return "\n".join(lines)


def save_report(text: str, date_str: str) -> Path:
    """落盘报告到 data/reports/fupan_{date_str}.txt，返回路径。"""
    from config import OUTPUT_FILENAME_PREFIX
    out_dir = _reports_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{OUTPUT_FILENAME_PREFIX}{date_str}.txt"
    out_path.write_text(text, encoding="utf-8")
    return out_path
