"""工具4：基于 Jinja2 生成每日复盘摘要（文本报告）。

读取已计算指标的 K线 + AI 评分（默认 Mock=0.5），渲染模板输出到 data/reports/。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from jinja2 import Template

from config.settings import settings
from core.ai_client import AIClient
from core.logger import get_logger
from core.storage import load_klines

log = get_logger("generate_report")

TEMPLATE = """\
===== 每日复盘报告 =====
日期: {{ date }}
标的: {{ symbol }}

【行情概览】
最新收盘: {{ close }}
MA5: {{ ma5 }}
MA20: {{ ma20 }}
RSI14: {{ rsi14 }}
MACD: {{ macd }} (signal={{ macd_signal }}, hist={{ macd_hist }})

【信号提示】
{% if rsi14 is not none and rsi14 > 70 %}RSI 超买，注意回调风险。
{% elif rsi14 is not none and rsi14 < 30 %}RSI 超卖，关注反弹机会。
{% else %}RSI 处于中性区间。
{% endif %}
【AI 评分】
{{ ai_score }}
========================
"""


def parse_args():
    p = argparse.ArgumentParser(description="生成复盘摘要")
    p.add_argument(
        "--date",
        default=(date.today() - timedelta(days=1)).isoformat(),
        help="目标日期 YYYY-MM-DD",
    )
    p.add_argument("--symbol", default=settings.default_symbol, help="交易对")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        log.info("=" * 30 + " 开始生成报告 " + "=" * 30)
        log.info("symbol=%s date=%s", args.symbol, args.date)

        start = (date.fromisoformat(args.date) - timedelta(days=30)).isoformat()
        df = load_klines(args.symbol, start, args.date)

        ctx = {
            "date": args.date,
            "symbol": args.symbol,
            "close": None,
            "ma5": None,
            "ma20": None,
            "rsi14": None,
            "macd": None,
            "macd_signal": None,
            "macd_hist": None,
            "ai_score": 0.5,
        }
        if not df.empty:
            row = df.iloc[-1]
            for k in ["close", "ma5", "ma20", "rsi14", "macd", "macd_signal", "macd_hist"]:
                if k in row and not pd_isna(row[k]):
                    ctx[k] = row[k]

        # AI 评分（默认 Mock=0.5）
        try:
            ctx["ai_score"] = AIClient().analyze(f"{args.symbol} {args.date} 复盘")
        except Exception as e:  # AI 不应阻断报告生成
            log.warning("AI 评分跳过: %s", e)

        report = Template(TEMPLATE).render(**ctx)
        out_dir = settings.reports_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"report_{args.date}.txt"
        out_path.write_text(report, encoding="utf-8")

        log.info("报告已生成 -> %s", out_path)
        print(report)
        log.info("=" * 30 + " 报告生成完成 " + "=" * 30)
        return 0
    except Exception as e:
        log.exception("报告生成失败: %s", e)
        return 1


def pd_isna(val) -> bool:
    """安全判空，兼容 numpy/pandas 缺失值。"""
    try:
        import pandas as pd

        return bool(pd.isna(val))
    except Exception:
        return val is None


if __name__ == "__main__":
    sys.exit(main())
