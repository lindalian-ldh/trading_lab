#!/usr/bin/env python3
"""股票特性分析服务入口（连板基因 + 游资席位）。

整合两大核心功能：
    功能一：连板基因识别（股性分析）—— 遍历涨停历史，提取妖股基因
    功能二：游资席位偏好（龙虎榜分析）—— 解析买卖席位，判定锁仓/一日游

默认全流程：拉取数据 → 基因分析 → 游资分析 → 生成报告 → 落盘。

用法示例：
    # 默认：扫描今日涨停池，跑连板基因 + 游资席位
    uv run services/stock_character/main.py

    # 指定个股
    uv run services/stock_character/main.py --codes 000017,600550

    # 仅跑连板基因扫描
    uv run services/stock_character/main.py --gene-only --scan

    # 仅跑游资席位分析
    uv run services/stock_character/main.py --hotmoney-only --codes 000017

    # 指定参考日
    uv run services/stock_character/main.py --date 2026-08-28

    # 保守配置档
    uv run services/stock_character/main.py --profile conservative

    # 静默（仅写文件）
    uv run services/stock_character/main.py --quiet

    # 详细日志
    uv run services/stock_character/main.py -v
"""

from __future__ import annotations

import argparse
import logging
import sys
import time as _time
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from config import get_stock_char_config, list_profiles, StockCharConfig
from data_loader import (
    fetch_stock_uplimit_history, fetch_lhb_list, fetch_lhb_for_stock,
    fetch_daily_kline, fetch_scan_candidates, to_ymd, to_iso,
)
from gene import analyze_gene, filter_gene_candidates
from hot_money import analyze_hot_money
from reporter import (
    format_gene_scan_report, format_gene_single_report,
    format_hot_money_report, generate_markdown_report, print_summary,
)
from storage import log_gene_signal, log_hotmoney_signal, save_markdown_report

logger = logging.getLogger(__name__)


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _parse_args(argv: list = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="股票特性分析（连板基因 + 游资席位）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # 输入
    parser.add_argument('--codes', type=str, default=None,
                        help='指定股票代码列表（逗号分隔），如 000017,600550')
    parser.add_argument('--scan', action='store_true', default=False,
                        help='扫描今日涨停股池作为候选（--codes 优先）')
    # 功能开关
    parser.add_argument('--gene-only', action='store_true', default=False,
                        help='仅跑连板基因分析')
    parser.add_argument('--hotmoney-only', action='store_true', default=False,
                        help='仅跑游资席位分析')
    # 日期
    parser.add_argument('--date', type=str, default=None,
                        help='参考日 YYYY-MM-DD（默认昨日）')
    # 配置
    parser.add_argument('--profile', type=str, default='default',
                        choices=list_profiles(),
                        help='配置档（default/conservative/strict）')
    parser.add_argument('--interval', type=float, default=None,
                        help='限频间隔秒（覆盖 config.history_request_interval_seconds）')
    # 输出
    parser.add_argument('--no-csv', action='store_true', default=False,
                        help='不写 CSV 文件')
    parser.add_argument('--no-report', action='store_true', default=False,
                        help='不写 Markdown 报告文件')
    parser.add_argument('--quiet', action='store_true', default=False,
                        help='静默模式（仅写文件，不打印控制台）')
    parser.add_argument('-v', '--verbose', action='store_true', default=False,
                        help='详细日志')
    return parser.parse_args(argv)


def _determine_ref_date(args_date: str) -> str:
    """确定参考日。默认昨日（与 alarming_monitor 一致）。"""
    if args_date:
        return to_iso(args_date)[:10]
    # 默认昨日
    return (date.today() - timedelta(days=1)).isoformat()


def _determine_candidates(args, ref_date: str, cfg: StockCharConfig) -> list:
    """确定候选股票池。--codes 优先，否则 --scan。"""
    if args.codes:
        codes = [c.strip() for c in args.codes.split(',') if c.strip()]
        logger.info("候选股票池（--codes）: %s", codes)
        return codes

    # --scan 模式：拉取今日涨停股
    ref_ymd = to_ymd(ref_date)
    codes = fetch_scan_candidates(ref_ymd, cfg)
    logger.info("候选股票池（--scan %s）: %d 只", ref_ymd, len(codes))
    if not codes:
        logger.warning("扫描候选池为空，尝试今日")
        today_ymd = date.today().strftime("%Y%m%d")
        codes = fetch_scan_candidates(today_ymd, cfg)
        logger.info("今日候选池: %d 只", len(codes))
    return codes


def _run_gene_analysis(codes: list, ref_date: str, cfg: StockCharConfig) -> list:
    """对候选池逐只跑连板基因分析。返回结果列表。"""
    results = []
    total = len(codes)
    for i, code in enumerate(codes, 1):
        logger.info("[%d/%d] 连板基因分析: %s", i, total, code)
        try:
            # 拉取涨停历史
            history_df = fetch_stock_uplimit_history(code, cfg)
            # 拉取日线
            kline_df = fetch_daily_kline(code, days=cfg.kline_recent_days, cfg=cfg)
            # 分析
            result = analyze_gene(history_df, kline_df, ref_date, cfg)
            results.append(result)
            logger.debug("  %s: max_boards=%d, has_gene=%s, just_starting=%s, score=%d",
                         code, result.get('max_boards', 0),
                         result.get('has_gene'), result.get('just_starting'),
                         result.get('gene_score', 0))
        except Exception as e:
            logger.warning("  %s 分析失败: %s", code, e)
            results.append({
                'ref_date': ref_date, 'code': code, 'name': '',
                'data_sufficient': False, 'has_gene': False, 'just_starting': False,
                'gene_score': 0, 'gene_reason': f'分析异常: {e}',
            })
    return results


def _run_hotmoney_analysis(codes: list, ref_date: str, cfg: StockCharConfig) -> list:
    """对候选池逐只跑游资席位分析。返回结果列表。"""
    results = []
    total = len(codes)
    for i, code in enumerate(codes, 1):
        logger.info("[%d/%d] 游资席位分析: %s", i, total, code)
        try:
            # 先拉涨停历史，取上榜日期（涨停日通常上龙虎榜）
            history_df = fetch_stock_uplimit_history(code, cfg)
            if history_df is None or len(history_df) == 0:
                logger.debug("  %s 无涨停历史，跳过龙虎榜分析", code)
                results.append({
                    'ref_date': ref_date, 'code': code, 'name': '',
                    'data_sufficient': False, 'preference': '中性',
                    'preference_reason': '无涨停历史（无龙虎榜线索）',
                    'hot_money_score': 0,
                })
                continue

            # 取近 90 日涨停日期作为龙虎榜查询日
            from gene import _filter_by_window
            recent_df = _filter_by_window(history_df, ref_date, cfg.gene_recent_days)
            if len(recent_df) == 0:
                logger.debug("  %s 近%d日无涨停记录", code, cfg.gene_recent_days)
                results.append({
                    'ref_date': ref_date, 'code': code, 'name': '',
                    'data_sufficient': False, 'preference': '中性',
                    'preference_reason': f'近{cfg.gene_recent_days}日无涨停记录',
                    'hot_money_score': 0,
                })
                continue

            lhb_dates = [to_ymd(d) for d in recent_df['date'].astype(str).tolist()]
            # 拉取龙虎榜记录
            lhb_df = fetch_lhb_for_stock(code, lhb_dates, cfg)
            # 拉取日线
            kline_df = fetch_daily_kline(code, days=cfg.kline_recent_days + 30, cfg=cfg)
            # 分析
            result = analyze_hot_money(lhb_df, kline_df, ref_date, cfg)
            results.append(result)
            logger.debug("  %s: lhb_count=%d, preference=%s, score=%d",
                         code, result.get('lhb_count', 0),
                         result.get('preference'), result.get('hot_money_score', 0))
        except Exception as e:
            logger.warning("  %s 游资分析失败: %s", code, e)
            results.append({
                'ref_date': ref_date, 'code': code, 'name': '',
                'data_sufficient': False, 'preference': '中性',
                'preference_reason': f'分析异常: {e}',
                'hot_money_score': 0,
            })
    return results


def _run_once(args) -> int:
    """主流程编排。返回退出码。"""
    cfg = get_stock_char_config(
        profile=args.profile,
        **({'history_request_interval_seconds': args.interval}
           if args.interval is not None else {})
    )

    ref_date = _determine_ref_date(args.date)
    logger.info("参考日: %s | 配置档: %s | 限频: %.2fs",
                ref_date, args.profile, cfg.history_request_interval_seconds)

    # 确定候选池
    codes = _determine_candidates(args, ref_date, cfg)
    if not codes:
        if not args.quiet:
            print(f"无候选股票（--codes 未指定且 --scan 候选池为空）")
        return 0

    run_gene = not args.hotmoney_only
    run_hotmoney = not args.gene_only

    gene_results = []
    hotmoney_results = []

    t0 = _time.time()
    if run_gene:
        gene_results = _run_gene_analysis(codes, ref_date, cfg)
    if run_hotmoney:
        hotmoney_results = _run_hotmoney_analysis(codes, ref_date, cfg)
    elapsed = _time.time() - t0
    logger.info("分析完成，耗时 %.2fs", elapsed)

    # 输出
    scan_mode = not args.codes  # --codes 是单股模式，--scan 是扫描模式
    if not args.quiet:
        # 控制台报告
        if gene_results:
            if scan_mode:
                candidates = filter_gene_candidates(gene_results)
                print(format_gene_scan_report(candidates, ref_date))
            else:
                for r in gene_results:
                    print(format_gene_single_report(r, ref_date))
        if hotmoney_results:
            for r in hotmoney_results:
                print(format_hot_money_report(r, ref_date))
        print_summary(gene_results, hotmoney_results, ref_date)
        print(f"耗时: {elapsed:.2f}s")

    # CSV 落盘
    if not args.no_csv:
        for r in gene_results:
            if r.get('data_sufficient'):
                log_gene_signal(r, cfg)
        for r in hotmoney_results:
            if r.get('data_sufficient'):
                log_hotmoney_signal(r, cfg)

    # Markdown 报告
    if not args.no_report:
        md = generate_markdown_report(gene_results, hotmoney_results, ref_date, scan_mode)
        save_markdown_report(md, ref_date, cfg)

    return 0


def main(argv: list = None) -> int:
    args = _parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return _run_once(args)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except Exception as e:
        logger.error("运行失败: %s", e, exc_info=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
