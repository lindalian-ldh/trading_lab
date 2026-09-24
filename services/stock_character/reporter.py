"""输出层：ASCII 等宽报告 + CSV 行格式化 + Markdown 全景报告。

参考 alarming_monitor/reporter.py 的三格式输出模式：
    1. ASCII 等宽文本（控制台打印 + Markdown text 代码块）
    2. CSV 单行展平（storage 层按月分文件持久化）
    3. Markdown 全景报告（拼接功能一+功能二章节）
"""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd

# CSV 表头（与 storage 保持一致）
GENE_CSV_FIELDS = [
    'run_time', 'ref_date', 'code', 'name',
    'max_boards', 'limit_up_count',
    'consecutive_2plus_count', 'consecutive_3plus_count',
    'latest_limit_up_date', 'avg_amplitude', 'high_volatility',
    'has_gene', 'just_starting', 'gene_score', 'gene_level',
    'gene_reason', 'data_sufficient',
]

HOTMONEY_CSV_FIELDS = [
    'run_time', 'ref_date', 'code', 'name',
    'lhb_count', 'institutional_buy_count', 'hot_money_buy_count',
    'hot_money_names', 'next_day_premium_count', 'next_day_dump_count',
    'preference', 'preference_reason', 'hot_money_score', 'data_sufficient',
]


def format_gene_csv_row(result: dict) -> dict:
    """把 analyze_gene 结果展平为 CSV 行 dict（与 GENE_CSV_FIELDS 对齐）。"""
    return {k: result.get(k, '') for k in GENE_CSV_FIELDS}


def format_hotmoney_csv_row(result: dict) -> dict:
    """把 analyze_hot_money 结果展平为 CSV 行 dict（与 HOTMONEY_CSV_FIELDS 对齐）。"""
    row = {k: result.get(k, '') for k in HOTMONEY_CSV_FIELDS}
    # hot_money_names 是 list → 拼接为字符串
    names = result.get('hot_money_names', [])
    if isinstance(names, list):
        row['hot_money_names'] = '/'.join(names) if names else ''
    return row


def format_gene_scan_report(results: list, ref_date: str) -> str:
    """连板基因扫描结果 ASCII 等宽报告。

    Args:
        results: analyze_gene 结果列表（已过滤 has_gene AND just_starting）
        ref_date: 参考日

    Returns:
        ASCII 等宽文本。
    """
    lines = []
    lines.append(f"========== {ref_date} 连板基因扫描报告 ==========")
    lines.append("")

    if not results:
        lines.append("（无具备妖股基因且刚启动的标的）")
        lines.append("=" * 52)
        return "\n".join(lines)

    lines.append(f"【妖股基因标的】共 {len(results)} 只（按基因评分降序）")
    lines.append("")

    # 表头
    header = f"{'代码':<8} {'名称':<10} {'最高板':<6} {'2板+':<5} {'振幅%':<7} {'评分':<5} {'等级'}"
    lines.append(header)
    lines.append("-" * len(header) * 2)

    for r in results:
        code = str(r.get('code', ''))[:8]
        name = str(r.get('name', ''))[:10].ljust(10)
        max_b = str(r.get('max_boards', 0))[:6].ljust(6)
        cons2 = str(r.get('consecutive_2plus_count', 0))[:5].ljust(5)
        amp = f"{r.get('avg_amplitude', 0):.1f}".ljust(7)
        score = str(r.get('gene_score', 0))[:5].ljust(5)
        level = str(r.get('gene_level', ''))[:30]
        lines.append(f"{code:<8} {name} {max_b} {cons2} {amp} {score} {level}")

    lines.append("")
    lines.append("【基因原因】")
    for r in results[:5]:  # 前5只的原因详情
        code = str(r.get('code', ''))
        name = str(r.get('name', ''))
        reason = str(r.get('gene_reason', ''))
        lines.append(f"  {code} {name}: {reason}")
    if len(results) > 5:
        lines.append(f"  ...（其余 {len(results) - 5} 只省略）")

    lines.append("=" * 52)
    return "\n".join(lines)


def format_gene_single_report(result: dict, ref_date: str) -> str:
    """单股连板基因详情 ASCII 报告。"""
    lines = []
    lines.append(f"========== {ref_date} 连板基因分析 ==========")
    lines.append("")

    code = result.get('code', '')
    name = result.get('name', '')
    lines.append(f"【股票】{code} {name}")

    if not result.get('data_sufficient', False):
        lines.append(f"  数据不足: {result.get('gene_reason', '')}")
        lines.append("=" * 42)
        return "\n".join(lines)

    lines.append(f"  最高连板数: {result.get('max_boards', 0)}板")
    lines.append(f"  涨停总次数: {result.get('limit_up_count', 0)}次")
    lines.append(f"  2连板+次数: {result.get('consecutive_2plus_count', 0)}次")
    lines.append(f"  3连板+次数: {result.get('consecutive_3plus_count', 0)}次")
    lines.append(f"  最近涨停日: {result.get('latest_limit_up_date', '')}")
    lines.append(f"  日振幅均值: {result.get('avg_amplitude', 0):.2f}%")
    lines.append(f"  高波动属性: {'是' if result.get('high_volatility') else '否'}")
    lines.append("")
    lines.append(f"【基因判定】")
    lines.append(f"  具备妖股基因: {'✓' if result.get('has_gene') else '✗'}")
    lines.append(f"  当前刚启动: {'✓' if result.get('just_starting') else '✗'}")
    lines.append(f"  基因评分: {result.get('gene_score', 0)}分")
    lines.append(f"  基因等级: {result.get('gene_level', '')}")
    lines.append(f"  原因: {result.get('gene_reason', '')}")
    lines.append("=" * 42)
    return "\n".join(lines)


def format_hot_money_report(result: dict, ref_date: str) -> str:
    """单股游资席位分析 ASCII 详情报告。"""
    lines = []
    lines.append(f"========== {ref_date} 游资席位偏好分析 ==========")
    lines.append("")

    code = result.get('code', '')
    name = result.get('name', '')
    lines.append(f"【股票】{code} {name}")

    if not result.get('data_sufficient', False):
        lines.append(f"  数据不足: {result.get('preference_reason', '')}")
        lines.append("=" * 48)
        return "\n".join(lines)

    lines.append(f"  龙虎榜上榜次数: {result.get('lhb_count', 0)}次")
    lines.append(f"  机构买入次数: {result.get('institutional_buy_count', 0)}次")
    lines.append(f"  游资买入次数: {result.get('hot_money_buy_count', 0)}次")
    hm_names = result.get('hot_money_names', [])
    if isinstance(hm_names, list) and hm_names:
        lines.append(f"  游资名称: {'/'.join(hm_names)}")
    lines.append(f"  次日溢价次数: {result.get('next_day_premium_count', 0)}次")
    lines.append(f"  次日核按钮次数: {result.get('next_day_dump_count', 0)}次")
    lines.append("")
    lines.append(f"【偏好判定】")
    lines.append(f"  偏好: {result.get('preference', '')}")
    lines.append(f"  原因: {result.get('preference_reason', '')}")
    lines.append(f"  评分: {result.get('hot_money_score', 0)}分")

    # 次日涨跌明细
    pcts = result.get('next_day_pcts', [])
    if isinstance(pcts, list) and pcts:
        lines.append("")
        lines.append(f"【次日涨跌明细】")
        for d, p in pcts:
            sign = '+' if p >= 0 else ''
            lines.append(f"  {d} 次日 {sign}{p:.2f}%")

    lines.append("=" * 48)
    return "\n".join(lines)


def generate_markdown_report(gene_results: list,
                             hotmoney_results: list,
                             ref_date: str,
                             scan_mode: bool = True) -> str:
    """生成 Markdown 全景报告。

    Args:
        gene_results: 连板基因分析结果列表
        hotmoney_results: 游资席位分析结果列表
        ref_date: 参考日
        scan_mode: True=扫描模式（基因报告用扫描格式），False=单股模式

    Returns:
        Markdown 文本。
    """
    sections = []
    sections.append(f"# 股票特性分析报告 — {ref_date}")
    sections.append("")

    # 功能一：连板基因
    if gene_results:
        sections.append("## 连板基因识别（股性分析）")
        sections.append("")
        if scan_mode:
            # 扫描模式：过滤 has_gene AND just_starting
            from gene import filter_gene_candidates
            candidates = filter_gene_candidates(gene_results)
            report = format_gene_scan_report(candidates, ref_date)
        else:
            # 单股模式：显示第一只详情
            report = format_gene_single_report(gene_results[0], ref_date)
        sections.append("```text")
        sections.append(report)
        sections.append("```")
        sections.append("")

    # 功能二：游资席位
    if hotmoney_results:
        sections.append("## 游资席位偏好（龙虎榜分析）")
        sections.append("")
        for r in hotmoney_results:
            report = format_hot_money_report(r, ref_date)
            sections.append("```text")
            sections.append(report)
            sections.append("```")
            sections.append("")

    if not gene_results and not hotmoney_results:
        sections.append("（本次运行无分析结果）")

    return "\n".join(sections)


def print_summary(gene_results: list, hotmoney_results: list, ref_date: str) -> None:
    """控制台打印简短摘要。"""
    print(f"\n{'=' * 52}")
    print(f"股票特性分析完成 — {ref_date}")
    if gene_results:
        from gene import filter_gene_candidates
        candidates = filter_gene_candidates(gene_results)
        print(f"  连板基因: 分析 {len(gene_results)} 只，命中 {len(candidates)} 只刚启动")
    if hotmoney_results:
        premiums = sum(1 for r in hotmoney_results if r.get('preference') == '偏爱锁仓')
        dumps = sum(1 for r in hotmoney_results if r.get('preference') == '一日游风险')
        print(f"  游资席位: 分析 {len(hotmoney_results)} 只，锁仓 {premiums} / 一日游 {dumps}")
    print(f"{'=' * 52}")
