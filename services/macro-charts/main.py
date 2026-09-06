"""宏观数据图表模块入口（跑完即退，与采集模块解耦）。

用法
----
  # 返回 JSON（默认输出到终端）
  uv run services/macro-charts/main.py --indicator CPI
  uv run services/macro-charts/main.py --indicator CPI --start-date 2020-01-01 --limit 60
  uv run services/macro-charts/main.py --indicator CPI,PPI            # 多指标对比(双Y轴)

  # 生成 Plotly HTML 文件
  uv run services/macro-charts/main.py --indicator CPI --output html
  uv run services/macro-charts/main.py --indicator M2 --output html --out data/reports/charts/M2.html

  # 多组合合并到同一个 HTML（; 分隔组，每组一张图纵向堆叠）
  uv run services/macro-charts/main.py --indicator "CPI,PPI;GOLD,SILVER;DGS10" --output html

  # 美国宏观指标（macro_us_data 表，country 默认 US）
  uv run services/macro-charts/main.py --table macro_us_data --indicator UNRATE
  uv run services/macro-charts/main.py --table macro_us_data --indicator CPIAUCSL,CPILFESL --output html

参数优先级：日期范围 > limit。start_date/end_date 任一提供即视为日期范围模式。
--table 指定数据表（macro_data 中国 / macro_us_data 美国及商品），country 默认随 table。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from config.settings import settings
from core.logger import get_logger
from query import query_chart
from render import render_html, render_html_multi

log = get_logger("macro.charts.main")


def parse_args():
    p = argparse.ArgumentParser(description="宏观数据图表生成")
    p.add_argument("--indicator", required=True,
                   help="指标组合：单个(CPI)、逗号分隔同图对比(CPI,PPI,最多3)、"
                        "分号分隔多图合并(CPI,PPI;GOLD,SILVER)")
    p.add_argument("--country", default=None,
                   help="国家代码；默认随 table（macro_data→CN, macro_us_data→US）")
    p.add_argument("--table", default="macro_data", choices=["macro_data", "macro_us_data"],
                   help="数据表：macro_data(中国,默认) / macro_us_data(美国及商品)")
    p.add_argument("--start-date", default=None, help="起始日期 YYYY-MM-DD")
    p.add_argument("--end-date", default=None, help="结束日期 YYYY-MM-DD，默认最新")
    p.add_argument("--limit", type=int, default=None, help="返回最近 N 期（优先级低于日期范围）")
    p.add_argument("--chart-type", default="line", help="图表类型，默认 line（暂只支持折线）")
    p.add_argument("--output", default="json", choices=["json", "html"],
                   help="输出格式：json 返回数据，html 返回渲染后的图表 HTML")
    p.add_argument("--out", default=None,
                   help="输出文件路径；html 默认 data/reports/charts/{indicator}.html"
                        "（多组合用 __ 连接，如 CPI_PPI__GOLD_SILVER.html），json 默认终端")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        country = args.country or ("US" if args.table == "macro_us_data" else "CN")
        # 按 ; 拆分多组合：每组内部仍用逗号分隔多指标同图对比
        groups = [g.strip() for g in args.indicator.split(";") if g.strip()]
        if not groups:
            log.error("未提供有效指标：%s", args.indicator)
            return 1
        multi = len(groups) > 1

        results = [
            query_chart(
                indicator=g,
                country=country,
                start_date=args.start_date,
                end_date=args.end_date,
                limit=args.limit,
                chart_type=args.chart_type,
                table=args.table,
            )
            for g in groups
        ]
        ok = all(r.get("ok") for r in results)

        if args.output == "json":
            # 单组合：保持原 dict 结构（向后兼容）；多组合：包裹为 {"groups": [...]}
            payload_obj = results[0] if not multi else {"groups": results}
            payload = json.dumps(payload_obj, ensure_ascii=False, indent=2, default=str)
            if args.out:
                outp = _resolve_out(args.out)
                outp.parent.mkdir(parents=True, exist_ok=True)
                outp.write_text(payload, encoding="utf-8")
                log.info("JSON 已写入 %s", outp)
            else:
                print(payload)
            return 0 if ok else 1

        # html：单组合走单图，多组合走纵向堆叠合并
        html = render_html_multi(results) if multi else render_html(results[0])
        if args.out:
            outp = _resolve_out(args.out)
        else:
            outp = settings.reports_dir / "charts" / _default_html_name(groups)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(html, encoding="utf-8")
        log.info("HTML 图表已生成：%s", outp)
        if not ok:
            log.warning("部分组合查询未成功，详见 HTML 内提示块：%s",
                        "; ".join(r.get("message", "") for r in results if not r.get("ok")))
            return 1
        return 0
    except Exception as e:
        log.exception("图表生成失败: %s", e)
        return 1


def _resolve_out(path: str):
    from pathlib import Path
    p = Path(path)
    return p if p.is_absolute() else (settings.data_dir / p)


def _default_html_name(groups: list[str]) -> str:
    """根据指标组合生成默认 HTML 文件名。

    单组合：CPI_PPI.html；多组合用 __ 连接：CPI_PPI__GOLD_SILVER.html。
    """
    parts = [g.replace(",", "_").replace("/", "_").strip() for g in groups]
    return "__".join(parts) + ".html"


if __name__ == "__main__":
    sys.exit(main())
