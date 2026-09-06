#!/usr/bin/env python3
"""crawler_news 主入口（盘后批处理：跑完即退）。

子命令
------
    crawl    抓取 AkShare 财经资讯接口并落盘 JSON Lines
    report   基于已落盘数据生成新闻简报（Markdown / HTML，阶段六实现）
    all      一键串行 crawl → report（共用 --date 作为 crawl 目标日与 report 范围）

用法示例
--------
    # 抓取昨日全量（5 个接口）
    uv run services/crawler_news/main.py crawl

    # 抓取指定日期
    uv run services/crawler_news/main.py crawl --date 2026-08-17

    # 仅抓取单个数据源
    uv run services/crawler_news/main.py crawl --source cls

    # 抓取与个股相关的新闻（透传给 em_stock / juchao 等接口）
    uv run services/crawler_news/main.py crawl --symbol 600519

    # 生成 Markdown 简报
    uv run services/crawler_news/main.py report \
        --start-date 2026-08-01 --end-date 2026-08-17

    # 生成 HTML 简报（浏览器可直接打开）
    uv run services/crawler_news/main.py report \
        --start-date 2026-08-01 --end-date 2026-08-17 --format html

    # 仅生成与指定股票相关的报告
    uv run services/crawler_news/main.py report \
        --start-date 2026-08-01 --end-date 2026-08-17 --symbol 600519

    # 仅生成指定数据源的报告（cls / sina / juchao / em_global / em_stock）
    uv run services/crawler_news/main.py report \
        --start-date 2026-08-01 --end-date 2026-08-17 --source cls --format html

    # 一键 crawl + report（抓取当日数据并生成当日简报）
    uv run services/crawler_news/main.py all --date 2026-08-17 --format html

    # 一键单源 crawl + report
    uv run services/crawler_news/main.py all --date 2026-08-17 --source cls --format html

主流程
------
    crawl：限频调用 5 个接口（间隔 ≥ 2s）→ 标注（tagger.tag_news_batch）
           → 落盘（storage.save_jsonl 幂等）→ 汇总日志（成功/失败计数、耗时）
    report：加载（storage.load_jsonl，可按 source 过滤）→ symbol 过滤 → 统计
            → 渲染 → Markdown / HTML 落盘

约定
----
    - ``--date`` 默认昨天（与 bankuai-service / sell_monitor 一致）
    - 单次任务硬超时 300 秒（``signal.alarm``）；典型 5 接口 × 2s 限频 + 解析 < 60s
    - 单个接口失败不阻断整体流程，仅记录到失败清单
    - 全部接口失败时退出非零码

退出码
------
    0  全部接口成功，或部分失败但有数据落盘 / 报告生成成功
    1  全部接口失败 / 任务超时 / 报告异常 / 出现未捕获异常
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

# 项目根 + 服务目录加入 sys.path，便于直接 uv run 运行
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_SERVICE_DIR = Path(__file__).resolve().parent
if str(_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICE_DIR))

# 加载 .env（如有）
try:
    from dotenv import load_dotenv

    load_dotenv(_PROJECT_ROOT / ".env")
except Exception:
    pass

from news_config import ALL_SOURCES, SOURCE_MAP  # noqa: E402
from reporter import generate_report  # noqa: E402
from sources import fetch_source  # noqa: E402
from storage import save_jsonl  # noqa: E402
from tagger import tag_news_batch  # noqa: E402

try:
    from core.logger import get_logger

    log = get_logger("crawler_news")
except Exception:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log = logging.getLogger("crawler_news")


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 单次任务硬超时（项目约定：单任务 300s；典型 < 60s）
_TASK_TIMEOUT_SEC = 300

# 性能预算：典型全源采集应在此时间内完成
_PERF_BUDGET_SEC = 60

# 支持的 --source 选项（与 news_config.SOURCE_MAP 的键一致）
_SOURCE_CHOICES = ["cls", "sina", "juchao", "em_global", "em_stock"]


class TaskTimeout(Exception):
    """单次执行超时。"""


def _timeout_handler(signum, frame):  # pragma: no cover - 信号回调不便单测
    raise TaskTimeout(f"crawler_news 任务超时(>{_TASK_TIMEOUT_SEC}s)")


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """构造 argparse 解析器（支持 ``crawl`` / ``report`` / ``all`` 子命令）。

    ``report`` 子命令在阶段六实现，此处先占位以稳定 CLI 接口。
    ``all`` 子命令一键串行 crawl → report，共用 --date 作为 crawl 目标日
    与 report 的 start/end 日期。
    """
    p = argparse.ArgumentParser(
        prog="crawler_news",
        description="基于 AkShare 的财经新闻采集与报告生成（跑完即退）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True, help="子命令")

    # ---- crawl 子命令（阶段五）----
    crawl_p = sub.add_parser(
        "crawl",
        help="采集新闻数据",
        description="抓取 AkShare 财经资讯接口并落盘 JSON Lines",
    )
    crawl_p.add_argument(
        "--date",
        default=(date.today() - timedelta(days=1)).isoformat(),
        help="目标日期 YYYY-MM-DD（默认昨天）",
    )
    crawl_p.add_argument(
        "--symbol",
        default=None,
        help="股票代码（透传给 em_stock 等支持个股过滤的接口）",
    )
    crawl_p.add_argument(
        "--source",
        default=None,
        choices=_SOURCE_CHOICES,
        help="指定单一数据源（默认全量）",
    )

    # ---- report 子命令（阶段六实现）----
    report_p = sub.add_parser(
        "report",
        help="生成新闻简报（Markdown / HTML）",
        description="基于已落盘 JSON Lines 生成 Markdown/HTML 简报",
    )
    report_p.add_argument("--start-date", required=True, help="起始日期 YYYY-MM-DD")
    report_p.add_argument("--end-date", required=True, help="截止日期 YYYY-MM-DD")
    report_p.add_argument(
        "--format", default="md", choices=["md", "html"], help="输出格式，默认 md"
    )
    report_p.add_argument(
        "--symbol", default=None, help="仅生成与指定股票相关的报告"
    )
    report_p.add_argument(
        "--source",
        default=None,
        choices=_SOURCE_CHOICES,
        help="仅生成指定数据源的报告（cls / sina / juchao / em_global / em_stock）",
    )

    # ---- all 子命令（一键 crawl + report）----
    all_p = sub.add_parser(
        "all",
        help="一键执行 crawl + report",
        description="先抓取新闻数据落盘，再基于当日数据生成简报",
    )
    all_p.add_argument(
        "--date",
        default=(date.today() - timedelta(days=1)).isoformat(),
        help="目标日期 YYYY-MM-DD（默认昨天，crawl 与 report 共用）",
    )
    all_p.add_argument(
        "--format", default="md", choices=["md", "html"], help="报告格式，默认 md"
    )
    all_p.add_argument(
        "--symbol", default=None, help="股票代码（crawl 透传 + report 过滤）"
    )
    all_p.add_argument(
        "--source",
        default=None,
        choices=_SOURCE_CHOICES,
        help="指定单一数据源（crawl 单源 + report 同源过滤）",
    )

    return p


# ---------------------------------------------------------------------------
# crawl 主流程
# ---------------------------------------------------------------------------


def run_crawl(
    target_date: str,
    symbol: Optional[str],
    source: Optional[str],
) -> dict:
    """执行 crawl 子流程：抓取 → 标注 → 落盘。

    单个接口失败不阻断整体流程，仅记录到 ``failed_sources`` 清单。
    幂等策略由 ``storage.save_jsonl`` 保证：同日同源重复抓取不会增加行数。

    Args:
        target_date: 目标日期 YYYY-MM-DD
        symbol:     可选股票代码（透传给 em_stock / juchao）
        source:     可选单一数据源；None = 全量（ALL_SOURCES）

    Returns:
        汇总结果 dict，含以下字段：
            - target_date:     str
            - source:          str | None
            - symbol:          str | None
            - success_count:   int  成功接口数
            - total_sources:   int  参与接口总数
            - total_items:     int  抓取到的总条数
            - total_written:   int  实际写入条数（幂等去重后）
            - failed_sources:  list[str]  失败接口清单
            - results:         dict  每个接口的详细状态
            - duration_sec:    float  总耗时秒
    """
    start_time = time.time()

    # 1. 确定要抓取的数据源列表
    if source:
        sources_to_fetch = [SOURCE_MAP[source]]
    else:
        sources_to_fetch = list(ALL_SOURCES)

    log.info(
        "=" * 30 + " 开始采集 crawler_news " + "=" * 30
    )
    log.info(
        "date=%s source=%s symbol=%s",
        target_date,
        source or "all",
        symbol or "none",
    )
    log.info(
        "待抓取数据源: %s",
        [s.source_short for s in sources_to_fetch],
    )

    results: dict[str, dict] = {}  # source_short → 状态 dict
    total_items = 0
    total_written = 0
    success_count = 0
    failed_sources: list[str] = []

    # 2. 逐源抓取 + 标注 + 落盘
    for src in sources_to_fetch:
        src_short = src.source_short
        log.info("----- 数据源 %s -----" % src_short)
        try:
            # 抓取（fetch_source 内部已应用 _rate_limit 限频）
            items = fetch_source(src, target_date, symbol=symbol)
            log.info("%s 抓取 %d 条", src_short, len(items))

            # 标注（原地修改 items）
            tag_news_batch(items)

            # 落盘（幂等写入）
            written = save_jsonl(items, src_short, target_date)

            results[src_short] = {
                "fetched": len(items),
                "written": written,
                "status": "ok",
                "error": None,
            }
            total_items += len(items)
            total_written += written
            success_count += 1
            log.info(
                "%s 完成: 抓取 %d / 写入 %d",
                src_short,
                len(items),
                written,
            )
        except Exception as e:
            log.exception("%s 抓取失败: %s", src_short, e)
            results[src_short] = {
                "fetched": 0,
                "written": 0,
                "status": "failed",
                "error": str(e),
            }
            failed_sources.append(src_short)

    duration = time.time() - start_time

    # 3. 汇总日志
    log.info("-" * 30 + " 采集汇总 " + "-" * 30)
    log.info(
        "日期: %s | 数据源: %s | symbol: %s",
        target_date,
        source or "all",
        symbol or "none",
    )
    log.info(
        "成功: %d / %d | 抓取条数: %d | 写入条数: %d | 耗时: %.1fs",
        success_count,
        len(sources_to_fetch),
        total_items,
        total_written,
        duration,
    )
    if failed_sources:
        log.warning("失败数据源: %s", failed_sources)
    if duration > _PERF_BUDGET_SEC:
        log.warning(
            "采集耗时 %.1fs 超过 %ds 性能预算",
            duration,
            _PERF_BUDGET_SEC,
        )
    log.info("=" * 30 + " 采集完成 " + "=" * 30)

    return {
        "target_date": target_date,
        "source": source,
        "symbol": symbol,
        "success_count": success_count,
        "total_sources": len(sources_to_fetch),
        "total_items": total_items,
        "total_written": total_written,
        "failed_sources": failed_sources,
        "results": results,
        "duration_sec": duration,
    }


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def run_report(
    start_date: str,
    end_date: str,
    symbol: Optional[str],
    fmt: str,
    source: Optional[str] = None,
) -> dict:
    """执行 report 子流程：加载 → 过滤 → 渲染 → 落盘。

    委托 ``reporter.generate_report`` 完成实际工作，本层仅作日志与异常隔离。

    Args:
        start_date: 起始日期 YYYY-MM-DD
        end_date:   截止日期 YYYY-MM-DD
        symbol:     可选股票代码过滤
        fmt:        输出格式（``md`` 或 ``html``）；HTML 由 markdown 库渲染 + 基础样式模板
        source:     可选数据源过滤（``cls`` / ``sina`` / ``juchao`` / ...）

    Returns:
        ``generate_report`` 返回的结果 dict，含 path / total_items / source_stats 等
    """
    log.info(
        "=" * 30 + " 开始生成 crawler_news 报告 " + "=" * 30
    )
    log.info(
        "start=%s end=%s source=%s symbol=%s fmt=%s",
        start_date,
        end_date,
        source or "all",
        symbol or "all",
        fmt,
    )
    try:
        result = generate_report(start_date, end_date, symbol, fmt, source=source)
        log.info("报告生成成功: %s", result.get("path"))
        return result
    except Exception as e:
        log.exception("报告生成失败: %s", e)
        raise
    finally:
        log.info("=" * 30 + " 报告流程结束 " + "=" * 30)


def run_all_pipeline(
    target_date: str,
    symbol: Optional[str],
    fmt: str,
    source: Optional[str],
) -> int:
    """一键执行 crawl → report 串行流程。

    先抓取 ``target_date`` 当日新闻落盘，再以同日为 start/end 生成简报。
    crawl 失败仍尝试 report（可能已有历史数据），report 失败则返回 1。

    Args:
        target_date: 目标日期 YYYY-MM-DD（crawl 与 report 共用）
        symbol:     可选股票代码（crawl 透传 + report 过滤）
        fmt:        报告格式（``md`` 或 ``html``）
        source:     可选数据源（crawl 单源 + report 同源过滤）

    Returns:
        0 成功，1 失败
    """
    log.info("#" * 30 + " crawler_news all 流程开始 " + "#" * 30)
    log.info("date=%s source=%s symbol=%s fmt=%s", target_date, source or "all", symbol or "all", fmt)

    # 1. crawl
    try:
        crawl_result = run_crawl(target_date, symbol, source)
        if crawl_result["success_count"] == 0 and crawl_result["total_sources"] > 0:
            log.warning("crawl 全部源失败，仍尝试 report（可能已有历史数据）")
    except Exception as e:
        log.exception("crawl 阶段异常，仍尝试 report: %s", e)

    # 2. report（start = end = target_date）
    try:
        run_report(target_date, target_date, symbol, fmt, source=source)
        log.info("#" * 30 + " all 流程完成 " + "#" * 30)
        return 0
    except Exception as e:
        log.exception("report 阶段失败: %s", e)
        log.info("#" * 30 + " all 流程结束（report 失败）" + "#" * 30)
        return 1


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    """CLI 主入口。

    Args:
        argv: 命令行参数列表；None = ``sys.argv[1:]``

    Returns:
        0  成功
        1  全部接口失败 / 任务超时 / 未捕获异常
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # 设置硬超时（macOS/Linux 支持 signal.alrm）
    try:
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(_TASK_TIMEOUT_SEC)
        _has_alarm = True
    except (AttributeError, ValueError):  # pragma: no cover
        _has_alarm = False

    try:
        if args.command == "crawl":
            result = run_crawl(args.date, args.symbol, args.source)
            # 单源失败不阻断整体；只有全部失败时才返回非零码
            if (
                result["success_count"] == 0
                and result["total_sources"] > 0
            ):
                log.error("全部数据源抓取失败，退出非零码")
                return 1
            return 0
        elif args.command == "report":
            try:
                run_report(
                    args.start_date,
                    args.end_date,
                    args.symbol,
                    args.format,
                    source=args.source,
                )
                return 0
            except Exception:
                return 1
        elif args.command == "all":
            return run_all_pipeline(
                args.date, args.symbol, args.format, args.source
            )
        else:  # pragma: no cover - argparse 已保证有 command
            parser.print_help()
            return 1
    except TaskTimeout as e:
        log.error("超时退出: %s", e)
        return 1
    except Exception as e:
        log.exception("crawler_news 任务失败: %s", e)
        return 1
    finally:
        if _has_alarm:
            signal.alarm(0)


if __name__ == "__main__":
    sys.exit(main())
