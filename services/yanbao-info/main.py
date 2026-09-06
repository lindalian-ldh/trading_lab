#!/usr/bin/env python3
"""yanbao-info 主入口（券商研报摘要与短线信号提取，跑完即退）。

输入股票代码，自动获取该股票最近发布的券商研报，下载 PDF 并解析，
通过关键词+正则规则提取评级/目标价/盈利预测/催化剂/风险等结构化字段，
结合一致预期与当前股价产出"短线博弈信号"判断，输出 JSON + Markdown 摘要报告。

用法示例:
    # 默认：取最近 1 份研报，输出 JSON + Markdown
    uv run services/yanbao-info/main.py --symbol 600036

    # 取最近 3 份研报
    uv run services/yanbao-info/main.py --symbol 600036 --top-n 3

    # 仅输出 JSON
    uv run services/yanbao-info/main.py --symbol 600036 --format json

    # 跳过 PDF 解析（仅用研报列表元数据，最快）
    uv run services/yanbao-info/main.py --symbol 600036 --no-pdf

    # 自定义输出路径
    uv run services/yanbao-info/main.py --symbol 600036 --out data/yanbao/reports/cmb.json

    # 安静模式
    uv run services/yanbao-info/main.py --symbol 600036 --quiet

退出码:
    0 = 成功（含"未找到研报"场景，视为业务正常态）
    1 = 失败（未捕获异常/CLI 参数错误）

数据落盘约定:
    data/yanbao/pdfs/      PDF 原文缓存
    data/yanbao/cache/     研报列表原始响应缓存
    data/yanbao/history/   历史评级快照（用于评级跳升判断）
    data/yanbao/reports/   最终摘要报告（JSON + Markdown）
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

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

from config import YanbaoConfig  # noqa: E402
from data_loader import (  # noqa: E402
    download_pdf,
    fetch_consensus_forecast,
    fetch_current_price,
    fetch_report_list,
    load_history,
    save_history_snapshot,
)
from extractor import extract_all  # noqa: E402
from pdf_parser import extract_text_and_tables  # noqa: E402
from reporter import (  # noqa: E402
    build_no_report_found,
    build_report,
    print_console_summary,
    save_json,
    save_markdown,
)
from signal_judge import judge_signals  # noqa: E402

try:
    from core.logger import get_logger

    log = get_logger("yanbao_info")
except Exception:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log = logging.getLogger("yanbao_info")


# ====================================================================
# 参数解析
# ====================================================================


def _parse_args(argv):
    p = argparse.ArgumentParser(
        prog="yanbao-info",
        description="券商研报摘要与短线信号提取（akshare + pdfplumber，跑完即退）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  取最近 1 份研报:     %(prog)s --symbol 600036
  取最近 3 份研报:     %(prog)s --symbol 600036 --top-n 3
  跳过 PDF 解析:       %(prog)s --symbol 600036 --no-pdf
  仅输出 JSON:         %(prog)s --symbol 600036 --format json
""",
    )
    p.add_argument(
        "--symbol", required=True,
        help="股票代码（如 600036/000001/300750）",
    )
    p.add_argument("--top-n", type=int, default=1, help="取最近 N 份研报，默认 1")
    p.add_argument(
        "--format", default="both", choices=["json", "md", "both"],
        help="输出格式，默认 both",
    )
    p.add_argument(
        "--out", default=None,
        help="输出路径（默认 data/yanbao/reports/{symbol}_{date}.json/.md）",
    )
    p.add_argument(
        "--no-pdf", action="store_true",
        help="跳过 PDF 下载与解析，仅用研报列表元数据",
    )
    p.add_argument("--quiet", action="store_true", help="不打印控制台摘要")
    p.add_argument("--verbose", action="store_true", help="详细日志（INFO 级别）")
    return p.parse_args(argv)


# ====================================================================
# 输出路径计算
# ====================================================================


def _default_out_paths(cfg: YanbaoConfig, symbol: str, fmt: str):
    """返回默认输出路径（json/md/both 三种格式）。

    默认: data/yanbao/reports/{symbol}_{date}.json/.md
    """
    today = date.today().isoformat()
    out_dir = cfg.reports_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"{symbol}_{today}"
    paths = []
    if fmt in ("json", "both"):
        paths.append(out_dir / f"{base}.json")
    if fmt in ("md", "both"):
        paths.append(out_dir / f"{base}.md")
    return paths


def _resolve_out_paths(args, cfg: YanbaoConfig):
    """解析 --out 参数：如指定，根据 --format 派生 .json/.md 后缀。"""
    if not args.out:
        return _default_out_paths(cfg, args.symbol, args.format)

    # 用户指定路径，根据 format 补全后缀
    out = Path(args.out)
    paths = []
    if args.format == "json":
        if not out.suffix:
            out = out.with_suffix(".json")
        paths.append(out)
    elif args.format == "md":
        if not out.suffix:
            out = out.with_suffix(".md")
        paths.append(out)
    else:  # both
        base = out.with_suffix("")
        paths.append(base.with_suffix(".json"))
        paths.append(base.with_suffix(".md"))
    return paths


# ====================================================================
# Pipeline 单份研报处理
# ====================================================================


def _process_single_report(
    report_meta: dict,
    cfg: YanbaoConfig,
    skip_pdf: bool,
    current_price,
    consensus: dict,
    history: list,
) -> tuple:
    """处理单份研报：下载 PDF → 解析 → 提取 → 信号判断。

    返回 (parsed, extracted, signals)
    """
    symbol = report_meta.get("stock_code", "")
    publish_date = report_meta.get("publish_date", "")
    org = report_meta.get("org_name", "")
    pdf_url = report_meta.get("pdf_url", "")

    # 1. PDF 下载与解析
    if skip_pdf or not pdf_url:
        log.info("跳过 PDF 解析（--no-pdf 或无 PDF 链接）")
        parsed = {
            "full_text": "",
            "tables": [],
            "page_count": 0,
            "parse_status": "skipped",
            "char_count": 0,
        }
    else:
        pdf_path = download_pdf(pdf_url, symbol, publish_date, org, cfg)
        if pdf_path is None:
            parsed = {
                "full_text": "",
                "tables": [],
                "page_count": 0,
                "parse_status": "pdf_download_failed",
                "char_count": 0,
            }
        else:
            parsed = extract_text_and_tables(pdf_path, cfg)

    # 2. 信息提取
    extracted = extract_all(parsed, cfg, title=report_meta.get("title", ""))

    # 3. 信号判断
    signals = judge_signals(
        extracted=extracted,
        current_price=current_price,
        consensus=consensus,
        history=history,
        current_org=org,
        cfg=cfg,
    )

    return parsed, extracted, signals


# ====================================================================
# 主入口
# ====================================================================


def main(argv=None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    # 初始化日志
    level = logging.INFO if args.verbose else logging.WARNING
    logging.getLogger().setLevel(level)
    log.setLevel(logging.INFO)  # 本模块始终 INFO

    log.info("=" * 30 + " yanbao-info 开始 " + "=" * 30)
    log.info("symbol=%s top_n=%d format=%s no_pdf=%s",
             args.symbol, args.top_n, args.format, args.no_pdf)

    # 构造配置
    cfg = YanbaoConfig.from_env(top_n=args.top_n)

    # 1. 获取研报列表
    list_result = fetch_report_list(args.symbol, cfg)

    # ★ "未找到研报"早期退出（对齐用户硬要求）
    if list_result["status"] != "ok":
        reason = list_result["reason"]
        log.warning("未找到研报: %s", reason)
        report = build_no_report_found(args.symbol, reason)
        out_paths = _resolve_out_paths(args, cfg)
        # 占位报告同时落盘 JSON + Markdown（无论 --format，便于用户查看）
        json_path = next((p for p in out_paths if p.suffix == ".json"),
                         cfg.reports_dir() / f"{args.symbol}_{date.today().isoformat()}.json")
        md_path = json_path.with_suffix(".md")
        save_json(report, json_path)
        save_markdown(report, md_path)
        if not args.quiet:
            print_console_summary(report)
        log.info("=" * 30 + " yanbao-info 结束（未找到研报）" + "=" * 30)
        return 0  # 业务正常态

    reports = list_result["reports"]
    # 取 top_n 份
    reports = reports[:args.top_n]
    log.info("取前 %d 份研报处理", len(reports))

    # 2. 并行/串行处理每份研报
    # 当前股价与一致预期只需获取一次（对所有研报共用）
    current_price = fetch_current_price(args.symbol, cfg)
    consensus = fetch_consensus_forecast(args.symbol, cfg)
    history = load_history(cfg, args.symbol)

    # 处理第一份研报作为主报告（其余研报追加到历史快照）
    main_report_meta = reports[0]
    parsed, extracted, signals = _process_single_report(
        main_report_meta, cfg, args.no_pdf, current_price, consensus, history
    )

    # 组装主报告
    report = build_report(
        symbol=args.symbol,
        report_meta=main_report_meta,
        parsed=parsed,
        extracted=extracted,
        signals=signals,
        current_price=current_price,
        consensus=consensus,
        cfg=cfg,
    )

    # 更新历史快照（本次报告 + 其余报告追加）
    for r in reports:
        snapshot = {
            "publish_date": r.get("publish_date", ""),
            "org_name": r.get("org_name", ""),
            "rating": r.get("rating", "") or extracted.get("rating", ""),
            "target_price": r.get("target_price") if r == main_report_meta else None,
            "title": r.get("title", ""),
        }
        save_history_snapshot(cfg, args.symbol, snapshot)

    # 3. 落盘 + 控制台摘要
    out_paths = _resolve_out_paths(args, cfg)
    for path in out_paths:
        if path.suffix == ".json":
            save_json(report, path)
        elif path.suffix == ".md":
            save_markdown(report, path)

    if not args.quiet:
        print_console_summary(report)

    log.info("=" * 30 + " yanbao-info 完成 " + "=" * 30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
