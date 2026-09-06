#!/usr/bin/env python3
"""板块扫描与龙头梯队识别系统入口（bankuai-service）。

基于 zzshare 免费接口，扫描 A 股板块热度排名，并对热门/冷门板块识别三级龙头梯队。
跑完即退，无常驻循环。

用法示例：
    # 1. 扫描昨日（概念板块，Top10/Bottom10 + 连板梯队）
    uv run services/bankuai-service/main.py

    # 2. 指定日期与板块类型（题材）
    uv run services/bankuai-service/main.py --date 2026-08-12 --plate-type 题材

    # 3. 自定义热门/冷门数量与梯队大小
    uv run services/bankuai-service/main.py --top-n 5 --bottom-n 5 --tier-size 5

    # 4. 仅输出 JSON 报告（不打印控制台卡片）
    uv run services/bankuai-service/main.py --quiet

    # 5. 关闭连板梯队 / 仅展示 2板以上
    uv run services/bankuai-service/main.py --no-uplimit-ladder --ladder-min-limit 2

环境变量：
    ZZSHARE_TOKEN  zzshare API token（在 .env 中配置）
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

# 项目根 on sys.path：使 core.logger / config.settings 可用
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 加载 .env 到 os.environ（zzshare SDK 读 ZZSHARE_TOKEN 环境变量）
try:
    from dotenv import load_dotenv
    load_dotenv(_PROJECT_ROOT / ".env")
except Exception:
    pass

from config import ScanConfig, resolve_plate_type  # noqa: E402
from reporter import print_report  # noqa: E402
from scanner import scan  # noqa: E402

logger = logging.getLogger(__name__)


# ====================================================================
# 参数解析
# ====================================================================

def _parse_args(argv):
    p = argparse.ArgumentParser(
        description="板块扫描与龙头梯队识别 + 涨停连板梯队（基于 zzshare 免费接口）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  扫描昨日概念板块:  %(prog)s
  指定日期+题材:     %(prog)s --date 2026-08-12 --plate-type 题材
  自定义数量:        %(prog)s --top-n 5 --bottom-n 5 --tier-size 5
  关闭连板梯队:      %(prog)s --no-uplimit-ladder
  指定板块扫描:      %(prog)s --sectors 半导体,AI算力
""",
    )
    p.add_argument(
        "--date", default=(date.today() - timedelta(days=1)).isoformat(),
        help="查询日期 YYYY-MM-DD，默认昨天",
    )
    p.add_argument(
        "--plate-type", default="概念",
        help="板块类型: 概念 / 题材 / 行业，默认概念",
    )
    p.add_argument("--top-n", type=int, default=10, help="热门板块数量，默认 10")
    p.add_argument(
        "--sectors", default="",
        help="指定板块名称（逗号分隔），非空时仅扫描匹配板块（子串匹配），如: 半导体,AI算力",
    )
    p.add_argument("--bottom-n", type=int, default=10, help="冷门板块数量，默认 10")
    p.add_argument("--tier-size", type=int, default=6, help="每个梯队最大数量，默认 6")
    p.add_argument(
        "--out", default=None,
        help="JSON 报告输出路径，默认 data/reports/bankuai/bankuai_{date}_{plate_type}.json",
    )
    p.add_argument("--quiet", action="store_true", help="不打印控制台卡片")
    p.add_argument("--verbose", "-v", action="store_true", help="详细日志（INFO 级别）")
    # ===== 涨停连板梯队开关 =====
    p.add_argument("--no-uplimit-ladder", action="store_true",
                   help="关闭涨停连板梯队（不分组展示连板股）")
    p.add_argument("--enable-uplimit-hot", action="store_true",
                   help="额外拉取 uplimit_hot 热门板块视图（需多一次 zzshare 请求）")
    p.add_argument("--ladder-show-top-n", type=int, default=5,
                   help="每个连板组最多展示个股数，默认 5")
    p.add_argument("--ladder-min-limit", type=int, default=1,
                   help="最低展示连板数（1=含首板，2=仅2板及以上），默认 1")
    return p.parse_args(argv)


# ====================================================================
# 报告输出
# ====================================================================

def _default_out_path(date1: str, plate_type_name: str) -> Path:
    out_dir = _PROJECT_ROOT / "data" / "reports" / "bankuai"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"bankuai_{date1}_{plate_type_name}.json"


def _save_report(report: dict, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


# ====================================================================
# 主入口
# ====================================================================

def main() -> int:
    args = _parse_args(sys.argv[1:])

    # ---- 初始化日志 ----
    try:
        from core.logger import get_logger
        get_logger("bankuai")
    except Exception:
        # 独立运行时退化为基础日志配置
        level = logging.INFO if args.verbose else logging.WARNING
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )

    # ---- 构造配置 ----
    try:
        plate_type = resolve_plate_type(args.plate_type)
    except ValueError as e:
        print(f"❌ {e}")
        return 1

    cfg = ScanConfig.from_env(
        plate_type=plate_type,
        top_n=args.top_n,
        bottom_n=args.bottom_n,
        tier_size=args.tier_size,
        sectors_filter=args.sectors,
        # ===== 涨停连板梯队 =====
        enable_uplimit_ladder=not args.no_uplimit_ladder,
        enable_uplimit_hot=args.enable_uplimit_hot,
        ladder_show_top_n=args.ladder_show_top_n,
        ladder_min_limit=args.ladder_min_limit,
    )

    if not cfg.token:
        print("⚠️  未配置 ZZSHARE_TOKEN，将以 anonymous 身份请求（可能受限）。")
        print("   请在 trading_lab/.env 中设置 ZZSHARE_TOKEN")

    # ---- 执行扫描 ----
    try:
        report = scan(args.date, cfg)
    except Exception as e:
        logger.exception("扫描失败: %s", e)
        print(f"❌ 扫描失败: {e}")
        return 1

    # ---- 保存 JSON 报告 ----
    out_path = Path(args.out) if args.out else _default_out_path(args.date, cfg.plate_type_name())
    try:
        _save_report(report, out_path)
        print(f"📝 报告已保存: {out_path}")
    except Exception as e:
        print(f"⚠️  报告保存失败: {e}")

    # ---- 控制台卡片 ----
    if not args.quiet:
        print_report(report)

    # ---- 退出码：成功率 > 0 视为成功 ----
    summary = report.get("summary", {})
    if summary.get("success_count", 0) == 0 and summary.get("total_sectors", 0) > 0:
        print("❌ 所有板块均获取失败，请检查 token / 网络 / 日期")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
