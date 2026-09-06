"""yanbao-info 报告生成层。

职责:
    - build_report           组装最终报告 dict（对齐用户 JSON 模板）
    - build_no_report_found 生成"未找到研报"占位报告
    - save_json              JSON 落盘
    - save_markdown          Markdown 落盘
    - print_console_summary  控制台轻量卡片摘要
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from config import YanbaoConfig
except ImportError:
    from .config import YanbaoConfig

logger = logging.getLogger(__name__)


# ====================================================================
# 报告组装
# ====================================================================


def build_report(
    symbol: str,
    report_meta: dict,
    parsed: dict,
    extracted: dict,
    signals: dict,
    current_price: Optional[float],
    consensus: dict,
    cfg: YanbaoConfig,
) -> dict:
    """组装最终报告 dict，对齐用户 JSON 模板结构。

    Args:
        symbol: 股票代码
        report_meta: 研报元数据（data_loader._normalize_report_list 中的 dict）
        parsed: pdf_parser.extract_text_and_tables 的返回
        extracted: extractor.extract_all 的返回
        signals: signal_judge.judge_signals 的返回
        current_price: 当前股价
        consensus: 一致预期
        cfg: 配置

    Returns:
        完整报告 dict
    """
    target_price = extracted.get("target_price")
    rating = extracted.get("rating", "")
    # 优先用 PDF 提取的评级，回退到研报列表元数据的"东财评级"
    if not rating:
        rating = report_meta.get("rating", "")

    # 计算隐含涨幅
    upside_str = "无法计算"
    if target_price is not None and current_price is not None and current_price > 0:
        upside = (target_price - current_price) / current_price
        upside_str = f"{upside * 100:.1f}%"

    # 解析状态
    fields_extracted = []
    if extracted.get("rating"):
        fields_extracted.append("评级")
    if extracted.get("target_price") is not None:
        fields_extracted.append("目标价")
    if extracted.get("earnings_forecast", {}).get("营收") or extracted.get("earnings_forecast", {}).get("归母净利润"):
        fields_extracted.append("盈利预测")
    if extracted.get("core_logic"):
        fields_extracted.append("核心逻辑")
    if extracted.get("catalysts"):
        fields_extracted.append("催化剂")
    if extracted.get("risks"):
        fields_extracted.append("风险")
    fields_extracted.append("首次覆盖")

    return {
        "股票代码": report_meta.get("stock_code", symbol),
        "股票简称": report_meta.get("stock_name", ""),
        "研报标题": report_meta.get("title", ""),
        "发布机构": report_meta.get("org_name", ""),
        "研报日期": report_meta.get("publish_date", ""),
        "发布日期": report_meta.get("publish_date", ""),
        "行业": report_meta.get("industry", ""),
        "公司基本情况": extracted.get("company_basics", {}),
        "文章标题列表": extracted.get("section_titles", []),
        "评级": rating,
        "目标价": target_price,
        "当前股价": current_price,
        "隐含涨幅": upside_str,
        "盈利预测": extracted.get("earnings_forecast", {}),
        "一致预期净利润": consensus,
        "核心逻辑摘要": extracted.get("core_logic", ""),
        "催化剂事件": extracted.get("catalysts", []),
        "风险提示": extracted.get("risks", []),
        "短线信号": signals,
        "PDF链接": report_meta.get("pdf_url", ""),
        "解析状态": {
            "pdf_downloaded": parsed.get("page_count", 0) > 0,
            "pdf_pages": parsed.get("page_count", 0),
            "pdf_parse_status": parsed.get("parse_status", "parse_failed"),
            "fields_extracted": fields_extracted,
        },
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def build_no_report_found(symbol: str, reason: str) -> dict:
    """生成"未找到研报"占位报告。

    对齐用户硬要求："找不到研报就直接返回，不过度尝试"。
    """
    return {
        "股票代码": symbol,
        "状态": "未找到研报",
        "原因": reason,
        "建议": "检查股票代码是否正确，或该股票近期无券商研报覆盖",
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ====================================================================
# 落盘函数
# ====================================================================


def save_json(report: dict, out_path: Path) -> Path:
    """JSON 落盘，ensure_ascii=False，indent=2。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    logger.info("JSON 报告已保存: %s", out_path)
    return out_path


def save_markdown(report: dict, out_path: Path) -> Path:
    """Markdown 落盘，含表格 + 列表结构。

    支持"未找到研报"占位报告与完整报告两种结构。
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    md = _build_markdown(report)
    out_path.write_text(md, encoding="utf-8")
    logger.info("Markdown 报告已保存: %s", out_path)
    return out_path


def _build_markdown(report: dict) -> str:
    """根据报告 dict 生成 Markdown 文本。"""
    # "未找到研报"占位报告
    if report.get("状态") == "未找到研报":
        return (
            f"# 券商研报摘要 - {report.get('股票代码', '')}\n\n"
            f"## 状态\n\n"
            f"**未找到研报**\n\n"
            f"- 原因: {report.get('原因', '')}\n"
            f"- 建议: {report.get('建议', '')}\n"
            f"- 生成时间: {report.get('生成时间', '')}\n"
        )

    # 完整报告
    lines = []
    lines.append(f"# 券商研报摘要 - {report.get('股票简称', '')}({report.get('股票代码', '')})\n")
    lines.append(f"**研报标题**: {report.get('研报标题', '')}\n")
    lines.append(f"**发布机构**: {report.get('发布机构', '')}  ")
    lines.append(f"**研报日期**: {report.get('研报日期', '')}  ")
    lines.append(f"**行业**: {report.get('行业', '')}\n")

    # 公司基本情况
    basics = report.get("公司基本情况", {})
    if basics:
        lines.append("## 公司基本情况\n")
        lines.append("| 指标 | 数值 |")
        lines.append("|------|------|")
        for k, v in basics.items():
            if k != "主营业务":
                lines.append(f"| {k} | {v} |")
        if "主营业务" in basics:
            lines.append(f"\n**主营业务**: {basics['主营业务']}\n")
        else:
            lines.append("")

    # 文章标题列表
    titles = report.get("文章标题列表", [])
    if titles:
        lines.append("## 文章标题列表\n")
        for i, t in enumerate(titles, 1):
            lines.append(f"{i}. {t}")
        lines.append("")

    # 基本面概览
    lines.append("## 基本面概览\n")
    lines.append(f"- **评级**: {report.get('评级', '未提取到')}")
    lines.append(f"- **目标价**: {report.get('目标价', '未提取到')}")
    lines.append(f"- **当前股价**: {report.get('当前股价', '无法获取')}")
    lines.append(f"- **隐含涨幅**: {report.get('隐含涨幅', '无法计算')}\n")

    # 盈利预测
    earnings = report.get("盈利预测", {})
    if earnings:
        lines.append("### 盈利预测\n")
        for category, years in earnings.items():
            if years:
                lines.append(f"**{category}**:\n")
                lines.append("| 年度 | 数值 |")
                lines.append("|------|------|")
                for year, val in years.items():
                    lines.append(f"| {year} | {val} |")
                lines.append("")

    consensus = report.get("一致预期净利润", {})
    if consensus:
        lines.append("### 一致预期净利润\n")
        lines.append("| 年度 | 一致预期 |")
        lines.append("|------|----------|")
        for year, val in consensus.items():
            lines.append(f"| {year} | {val} |")
        lines.append("")

    # 核心逻辑
    core_logic = report.get("核心逻辑摘要", "")
    if core_logic:
        lines.append("## 核心投资逻辑\n")
        lines.append(f"{core_logic}\n")

    # 催化剂
    catalysts = report.get("催化剂事件", [])
    if catalysts:
        lines.append("## 催化剂事件\n")
        for c in catalysts:
            lines.append(f"- {c}")
        lines.append("")

    # 风险提示
    risks = report.get("风险提示", [])
    if risks:
        lines.append("## 风险提示\n")
        for r in risks:
            lines.append(f"- {r}")
        lines.append("")

    # 短线信号
    signals = report.get("短线信号", {})
    if signals:
        lines.append("## 短线博弈信号\n")
        lines.append("| 信号类型 | 判断 | 依据 |")
        lines.append("|----------|------|------|")

        fc = signals.get("首次覆盖", {})
        lines.append(f"| 首次覆盖 | {'是' if fc.get('is_first_coverage') else '否'} | {fc.get('reason', '')} |")

        fu = signals.get("盈利预测上调", {})
        lines.append(f"| 盈利预测上调 | {'是' if fu.get('is_revision_up') else '否'} | {fu.get('reason', '')} |")

        tp = signals.get("目标价空间", {})
        lines.append(f"| 目标价空间 | {tp.get('signal', '')} | {tp.get('reason', '')} |")

        rc = signals.get("评级变化", {})
        lines.append(f"| 评级变化 | {'跳升' if rc.get('is_jump') else '维持/无变化'} | {rc.get('reason', '')} |")

        cc = signals.get("近期催化剂", {})
        lines.append(f"| 近期催化剂 | {'有' if cc.get('has_recent_catalyst') else '无'} | {cc.get('reason', '')} |")

        lines.append("")

    # 解析状态
    parse_status = report.get("解析状态", {})
    lines.append("## 解析状态\n")
    lines.append(f"- PDF 下载: {'成功' if parse_status.get('pdf_downloaded') else '失败'}")
    lines.append(f"- PDF 页数: {parse_status.get('pdf_pages', 0)}")
    lines.append(f"- PDF 解析状态: {parse_status.get('pdf_parse_status', '')}")
    lines.append(f"- 已提取字段: {', '.join(parse_status.get('fields_extracted', []))}\n")

    lines.append(f"---\n")
    lines.append(f"**PDF 链接**: {report.get('PDF链接', '')}\n")
    lines.append(f"**生成时间**: {report.get('生成时间', '')}\n")

    return "\n".join(lines)


# ====================================================================
# 控制台摘要
# ====================================================================


def print_console_summary(report: dict) -> None:
    """控制台轻量卡片摘要（评级/目标价/隐含涨幅/短线信号一览）。"""
    # "未找到研报"占位报告
    if report.get("状态") == "未找到研报":
        print("\n" + "=" * 60)
        print(f"  未找到研报: {report.get('股票代码', '')}")
        print("=" * 60)
        print(f"  原因: {report.get('原因', '')}")
        print(f"  建议: {report.get('建议', '')}")
        print("=" * 60 + "\n")
        return

    # 完整报告摘要
    print("\n" + "=" * 60)
    print(f"  研报摘要 | {report.get('股票简称', '')}({report.get('股票代码', '')})")
    print("=" * 60)
    print(f"  标题: {report.get('研报标题', '')[:50]}")
    print(f"  机构: {report.get('发布机构', '')} | 研报日期: {report.get('研报日期', '')}")
    print("-" * 60)
    # 公司基本情况
    basics = report.get("公司基本情况", {})
    if basics:
        print(f"  公司基本情况:")
        for k, v in basics.items():
            print(f"    - {k}: {v}")
        print("-" * 60)
    # 文章标题数
    titles = report.get("文章标题列表", [])
    if titles:
        print(f"  文章标题列表 ({len(titles)} 条):")
        for i, t in enumerate(titles[:5], 1):
            print(f"    {i}. {t}")
        if len(titles) > 5:
            print(f"    ... 共 {len(titles)} 条")
        print("-" * 60)
    print(f"  评级: {report.get('评级', '未提取到')} | 目标价: {report.get('目标价', '未提取到')}")
    print(f"  当前股价: {report.get('当前股价', '无法获取')} | 隐含涨幅: {report.get('隐含涨幅', '无法计算')}")
    print("-" * 60)
    # 风险提示
    risks = report.get("风险提示", [])
    if risks:
        print(f"  风险提示 ({len(risks)} 条):")
        for i, r in enumerate(risks, 1):
            print(f"    {i}. {r}")
        print("-" * 60)

    signals = report.get("短线信号", {})
    if signals:
        print("  短线信号:")
        fc = signals.get("首次覆盖", {})
        fu = signals.get("盈利预测上调", {})
        tp = signals.get("目标价空间", {})
        rc = signals.get("评级变化", {})
        cc = signals.get("近期催化剂", {})
        print(f"    - 首次覆盖: {'是' if fc.get('is_first_coverage') else '否'}")
        print(f"    - 盈利预测上调: {'是' if fu.get('is_revision_up') else '否'} ({fu.get('reason', '')})")
        print(f"    - 目标价空间: {tp.get('signal', '')}")
        print(f"    - 评级变化: {'跳升' if rc.get('is_jump') else '维持/无变化'} ({rc.get('reason', '')})")
        print(f"    - 近期催化剂: {'有' if cc.get('has_recent_catalyst') else '无'}")

    parse_status = report.get("解析状态", {})
    print("-" * 60)
    print(f"  PDF 解析: {parse_status.get('pdf_parse_status', '')} | 页数: {parse_status.get('pdf_pages', 0)}")
    print("=" * 60 + "\n")
