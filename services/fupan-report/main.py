#!/usr/bin/env python3
"""招商证券资金流水多维度量化复盘入口。

五大维度（按序执行）：
    1. 交易成本分析（佣金率/规费/单笔最高）
    2. 标的绩效复盘（盈利榜/亏损榜）
    3. 买卖点效率（加权均价/价差率）
    4. 资金利用率与仓位（每日净流入/重仓/日均买入）
    5. 行为纪律（胜率/净盈亏/结论）

运行后控制台打印格式化报告，并落盘 data/reports/fupan_{date}.txt。

用法示例：
    # 基本用法（等价于需求中的 python review.py -f 历史资金流水.xls）
    python services/fupan-report/main.py -f 历史资金流水.xls

    # 通过 uv 运行
    uv run services/fupan-report/main.py -f 历史资金流水.xls
"""
from __future__ import annotations

import argparse
import sys

# 日志：优先用项目 core.logger，缺失时降级到标准 logging（兼容独立运行）
try:
    from core.logger import get_logger
    log = get_logger("fupan_report")
except Exception:
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("fupan_report")

from analyzers import (
    analyze_capital,
    analyze_cost,
    analyze_discipline,
    analyze_performance,
    analyze_timing,
)
from data_loader import load_flow
from reporter import render_report, save_report


def parse_args():
    p = argparse.ArgumentParser(
        description="招商证券资金流水多维度量化复盘",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "-f", "--file",
        required=True,
        help="资金流水 .xls/.xlsx 文件路径（招商证券导出格式）",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        log.info("=" * 30 + " 开始资金流水复盘 " + "=" * 30)
        log.info("输入文件: %s", args.file)

        # 1. 读取与清洗
        df, cleaned_count = load_flow(args.file)
        log.info(
            "读取完成：有效记录 %d 条，剔除现金理财 %d 条",
            len(df), cleaned_count,
        )

        # 日期范围
        date_min = df["成交日期"].min()
        date_max = df["成交日期"].max()
        date_range = (date_min.strftime("%Y-%m-%d"), date_max.strftime("%Y-%m-%d"))
        end_date_str = date_max.strftime("%Y%m%d")

        # 2. 五大维度分析
        cost = analyze_cost(df)
        perf = analyze_performance(df)
        timing = analyze_timing(df)
        capital = analyze_capital(df)
        discipline = analyze_discipline(df)

        # 3. 渲染报告
        report = render_report(
            cost, perf, timing, capital, discipline, date_range, cleaned_count
        )

        # 4. 打印 + 落盘
        print(report)
        out_path = save_report(report, end_date_str)
        log.info("报告已落盘 -> %s", out_path)
        log.info("=" * 30 + " 复盘完成 " + "=" * 30)
        return 0

    except (ValueError, FileNotFoundError) as e:
        # 输入校验类错误：清晰提示
        print(f"[错误] {e}", file=sys.stderr)
        return 1
    except Exception as e:
        log.exception("复盘失败: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
