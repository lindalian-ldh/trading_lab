"""AI 截图分析服务入口：调用 DeepSeek 视觉模型分析股票截图。

接收本地 PNG 图片路径 + 三个盘中模拟盘口数据（现价/量比/量比倍数），
调用 deepseek-v4-flash-vision-exp 模型，输出是否值得开仓的概率评估与风险提示。

用法
----
  # 纯文件名：默认从 data/reports/ 查找
  uv run services/ai_analysis/main.py \\
    --image report_002579_20260908.png \\
    --price-now 15.51 --volume-ratio 1.8 --vol-multiplier 1.5

  # 任意本地路径（绝对或相对含目录）
  uv run services/ai_analysis/main.py \\
    --image /tmp/chart.png \\
    --price-now 15.51 --volume-ratio 1.8 --vol-multiplier 1.5

  # 保存结果到与图片同目录
  uv run services/ai_analysis/main.py --image report_002579.png \\
    --price-now 15.51 --volume-ratio 1.8 --vol-multiplier 1.5 --save

输出：标准 JSON 打印到 stdout，含 conditions/probability/hold_conditions/main_reason/risk_alert。
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

# 注入项目根到 sys.path，使 config/core 可 import（uv run 时已自动，直接 python 运行时需要）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
# 注入本模块目录，使 client.py 内 `from prompt import build_prompt` 可 import
_MODULE_DIR = Path(__file__).resolve().parent
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

from config.settings import settings  # noqa: E402
from core.logger import get_logger  # noqa: E402
from client import DeepSeekVisionClient  # noqa: E402

log = get_logger("ai_analysis.main")


class TaskTimeout(Exception):
    """单次执行超时。"""


def _timeout_handler(signum, frame):
    raise TaskTimeout("AI 分析超时(>120s)")


def parse_args():
    p = argparse.ArgumentParser(
        description="DeepSeek 视觉模型分析股票截图，输出开仓概率评估与风险提示",
    )
    p.add_argument(
        "--image", required=True,
        help="PNG 图片路径（纯文件名默认从 data/reports/ 查找；含目录按原路径解析）",
    )
    p.add_argument(
        "--price-now", type=float, required=True,
        help="当前现价 (price_now)",
    )
    p.add_argument(
        "--volume-ratio", type=float, required=True,
        help="预估量比 (volume_ratio)",
    )
    p.add_argument(
        "--vol-multiplier", type=float, required=True,
        help="预估全天成交量/昨日成交量 (vol_multiplier)",
    )
    p.add_argument(
        "--timeout", type=int, default=120,
        help="单次执行超时秒数（默认 120）",
    )
    p.add_argument(
        "--save", action="store_true",
        help="将结果 JSON 保存到与图片同目录下的 ai_analysis_<原文件名>.json",
    )
    return p.parse_args()


def resolve_image_path(raw: str) -> Path:
    """解析图片路径。

    - 纯文件名（无目录分隔符）：默认从 settings.reports_dir 查找
    - 含目录分隔符（绝对/相对路径）：按原路径解析
    """
    # 同时兼容 / 和 os.sep
    if "/" in raw or "\\" in raw:
        return Path(raw).expanduser()
    # 纯文件名：fallback 到 data/reports
    return settings.reports_dir / raw


def main() -> int:
    args = parse_args()

    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(args.timeout)
    try:
        image_path = resolve_image_path(args.image)
        if not image_path.exists() or not image_path.is_file():
            log.error("图片不存在: %s", image_path)
            return 1

        log.info(
            "=" * 20 + " AI 截图分析 " + "=" * 20
        )
        log.info(
            "image=%s price_now=%s volume_ratio=%s vol_multiplier=%s",
            image_path, args.price_now, args.volume_ratio, args.vol_multiplier,
        )

        client = DeepSeekVisionClient()
        result = client.analyze_chart(
            image_path=image_path,
            price_now=args.price_now,
            volume_ratio=args.volume_ratio,
            vol_multiplier=args.vol_multiplier,
        )

        output = json.dumps(result, ensure_ascii=False, indent=2)
        print(output)

        if args.save:
            out_name = f"ai_analysis_{image_path.stem}.json"
            out_path = image_path.parent / out_name
            out_path.write_text(output, encoding="utf-8")
            log.info("结果已保存: %s", out_path)

        log.info("=" * 20 + " AI 分析完成 " + "=" * 20)
        return 0
    except TaskTimeout as e:
        log.error("超时退出: %s", e)
        return 1
    except Exception as e:
        log.exception("AI 分析失败: %s", e)
        return 1
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    sys.exit(main())
