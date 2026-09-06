"""宏观数据获取服务入口（盘后批量，跑完即退）。

默认行为：每个指标只取【最近一条】记录，显示并保存。
如需取最近 N 条或全部历史，用 --recent（N>0 取最近 N 条，0 取全部）。

用法
----
  uv run services/macro-data-service/main.py                       # 每指标取最近1条
  uv run services/macro-data-service/main.py --reset               # 先清空旧数据再获取
  uv run services/macro-data-service/main.py --indicator cpi_cn    # 仅指定指标
  uv run services/macro-data-service/main.py --storage parquet     # 切换为 Parquet 存储
  uv run services/macro-data-service/main.py --recent 12           # 取最近 12 条
  uv run services/macro-data-service/main.py --mode overwrite      # 全量覆盖

CLI 参数覆盖 config.yaml 同名配置。单次执行超时 300 秒。
"""
from __future__ import annotations

import argparse
import signal
import sys
from datetime import date

from config_loader import is_indicator_enabled, load_config
from core.logger import get_logger
from fetchers import REGISTRY, US_INDICATORS
from storage import (
    init_macro_db,
    init_macro_us_db,
    reset_macro_db,
    reset_macro_us_db,
    reset_macro_parquet,
    save_records_parquet,
    save_records_sqlite,
    TABLE,
    TABLE_US,
)

log = get_logger("macro.main")


class TaskTimeout(Exception):
    """单次执行超时。"""


def _timeout_handler(signum, frame):
    raise TaskTimeout("宏观数据获取超时(>300s)")


def parse_args():
    p = argparse.ArgumentParser(description="宏观数据获取")
    p.add_argument("--date", default=date.today().isoformat(),
                   help="基准日期 YYYY-MM-DD（默认今天，仅作日志记录用）")
    p.add_argument("--indicator", default="all",
                   help="指标键名(如 cpi_cn；多个用逗号分隔如 sge_gold,sge_silver)或 all，默认 all")
    p.add_argument("--storage", default=None, choices=["sqlite", "parquet"],
                   help="覆盖配置中的存储方式")
    p.add_argument("--mode", default=None, choices=["incremental", "overwrite"],
                   help="写入模式：incremental=增量去重, overwrite=全量覆盖")
    p.add_argument("--recent", type=int, default=None,
                   help="取最近 N 条（默认 1；0=全部历史）")
    p.add_argument("--reset", action="store_true",
                   help="获取前先清空已存宏观数据（macro_data/macro_us_data 两表 + data/macro、data/macro_us 两目录）")
    return p.parse_args()


def _display(records: list[dict]) -> None:
    """显示获取到的记录摘要。"""
    if not records:
        return
    for r in records:
        log.info("  ▶ %-8s | %s | %s | 值=%s %s | 源=%s",
                 r["indicator"], r["date"], r["name"], r["value"], r["unit"], r["source"])


def main() -> int:
    args = parse_args()
    cfg = load_config()

    storage_cfg = cfg.get("storage", {})
    storage_type = args.storage or storage_cfg.get("type", "sqlite")
    mode = args.mode or ("overwrite" if storage_cfg.get("overwrite") else "incremental")
    overwrite = mode == "overwrite"

    fetch_cfg = cfg.get("fetch", {})
    # 默认只取最近一条：CLI > config > 1
    if args.recent is not None:
        recent = args.recent
    else:
        recent = int(fetch_cfg.get("recent_periods", 1))
    if recent == 0 and args.recent is None:
        recent = 1  # 兜底：未显式指定时绝不取全部，默认最新一条

    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(300)
    try:
        log.info("=" * 30 + " 开始获取宏观数据 " + "=" * 30)
        log.info("date=%s storage=%s mode=%s recent=%s reset=%s",
                 args.date, storage_type, mode, recent, args.reset)
        init_macro_db()
        init_macro_us_db()

        if args.reset:
            reset_macro_db()
            reset_macro_us_db()
            reset_macro_parquet()
            reset_macro_parquet(sub_root="macro_us")

        if args.indicator == "all":
            targets = list(REGISTRY.keys())
        else:
            targets = [k.strip() for k in args.indicator.split(",") if k.strip()]
        total = 0
        empty_keys: list[str] = []
        latest_summary: list[dict] = []
        for key in targets:
            fn = REGISTRY.get(key)
            if fn is None:
                log.warning("未知指标键名: %s（可用: %s）", key, ", ".join(REGISTRY))
                empty_keys.append(key)
                continue
            if not is_indicator_enabled(cfg, key):
                log.info("跳过(配置禁用): %s", key)
                continue

            log.info("----- 获取 %s -----", key)
            records = fn(recent_periods=recent)
            if not records:
                log.warning("%s 无数据或获取失败（详见 logs/macrodata-fetch.log）", key)
                empty_keys.append(key)
                continue

            # 美国及商品指标写入 macro_us_data 表 / data/macro_us 目录
            is_us = key in US_INDICATORS
            table = TABLE_US if is_us else TABLE
            sub_root = "macro_us" if is_us else "macro"
            if storage_type == "parquet":
                save_records_parquet(records, overwrite=overwrite, sub_root=sub_root)
            else:
                save_records_sqlite(records, overwrite=overwrite, table=table)
            total += len(records)
            latest_summary.extend(records)

        log.info("-" * 30 + " 最新宏观数据 " + "-" * 30)
        _display(latest_summary)
        log.info("=" * 30 + " 宏观数据获取完成 " + "=" * 30)
        log.info("共写入 %d 条；无数据/失败指标: %s", total, empty_keys or "无")
        return 0
    except TaskTimeout as e:
        log.error("超时退出: %s", e)
        return 1
    except Exception as e:
        log.exception("宏观数据获取失败: %s", e)
        return 1
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    sys.exit(main())
