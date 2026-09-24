"""CSV 持久化：按月分文件追加写入。

参考 alarming_monitor/storage.py 的按月分文件 + 同日去重模式。

路径派生：
    stock_character/  →  services/  →  trading_lab/  →  data/stock_character/
    文件名: gene_YYYY-MM.csv / hotmoney_YYYY-MM.csv（按月分，追加模式，首行写表头）。
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from reporter import GENE_CSV_FIELDS, format_gene_csv_row, \
    HOTMONEY_CSV_FIELDS, format_hotmoney_csv_row

logger = logging.getLogger(__name__)

# ── 路径派生 ──
_SERVICE_DIR = Path(__file__).resolve().parent
_TRADING_LAB_DIR = _SERVICE_DIR.parent.parent


def _csv_dir(cfg=None) -> Path:
    """派生 CSV 目录：cfg.csv_dir 相对 trading_lab 根。"""
    if cfg is not None and hasattr(cfg, 'csv_root'):
        return cfg.csv_root()
    p = Path("data/stock_character")
    return p if p.is_absolute() else _TRADING_LAB_DIR / p


def _atomic_write_csv(df: pd.DataFrame, path: Path) -> None:
    """原子写 CSV：先写同目录 .tmp，再 os.replace 覆盖目标。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + '.tmp')
        df.to_csv(tmp_path, index=False, encoding='utf-8-sig')
        os.replace(tmp_path, path)
    except Exception as e:
        logger.warning("原子写 CSV 失败 %s: %s", path.name, e)
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass


def _month_key(ref_date: str) -> str:
    """从 ref_date 提取 YYYY-MM。"""
    try:
        return datetime.strptime(str(ref_date)[:7], "%Y-%m").strftime("%Y-%m")
    except Exception:
        return datetime.now().strftime("%Y-%m")


def _log_signal(result: dict, csv_fields: list, format_fn,
               file_prefix: str, cfg=None) -> Optional[Path]:
    """通用 CSV 落盘：读旧 → 去重当日 → 追加新行 → 原子写回。

    Args:
        result: 分析结果 dict
        csv_fields: CSV 表头列表
        format_fn: 结果 → CSV 行 dict 的格式化函数
        file_prefix: 文件名前缀（如 'gene' / 'hotmoney'）
        cfg: StockCharConfig

    Returns:
        写入的文件路径，None 表示失败。
    """
    if not result:
        return None
    ref = result.get('ref_date') or ''
    if not ref:
        logger.warning("%s 落盘缺少 ref_date，跳过", file_prefix)
        return None

    # 补齐 run_time
    import time as _time
    if 'run_time' not in result or not result.get('run_time'):
        result = dict(result)
        result['run_time'] = _time.strftime('%Y-%m-%d %H:%M:%S')

    # 目录 + 文件路径
    d = _csv_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    month = _month_key(ref)
    path = d / f"{file_prefix}_{month}.csv"

    # 格式化行
    row = format_fn(result)

    # 读旧 → 按 ref_date+code 去重 → 追加新行 → 写回
    if path.exists():
        try:
            old = pd.read_csv(path, encoding='utf-8-sig', dtype=str)
        except Exception:
            old = pd.DataFrame(columns=csv_fields)
    else:
        old = pd.DataFrame(columns=csv_fields)

    # 去重：同 ref_date + 同 code 的旧行移除
    if 'ref_date' in old.columns and 'code' in old.columns:
        ref_str = str(ref)
        code_str = str(result.get('code', ''))
        mask = (old['ref_date'].astype(str) == ref_str) & \
               (old['code'].astype(str) == code_str)
        old = old[~mask]

    new_row = pd.DataFrame([{k: row.get(k, '') for k in csv_fields}])
    out = pd.concat([old, new_row], ignore_index=True)
    if 'ref_date' in out.columns:
        out = out.sort_values('ref_date').reset_index(drop=True)
    _atomic_write_csv(out, path)
    logger.debug("%s 信号已记录: %s (%s %s)", file_prefix, path, ref, result.get('code', ''))
    return path


def log_gene_signal(result: dict, cfg=None) -> Optional[Path]:
    """连板基因分析结果落盘。文件名: gene_YYYY-MM.csv。"""
    return _log_signal(result, GENE_CSV_FIELDS, format_gene_csv_row, 'gene', cfg)


def log_hotmoney_signal(result: dict, cfg=None) -> Optional[Path]:
    """游资席位分析结果落盘。文件名: hotmoney_YYYY-MM.csv。"""
    return _log_signal(result, HOTMONEY_CSV_FIELDS, format_hotmoney_csv_row, 'hotmoney', cfg)


def read_gene_history(months: int = 3, cfg=None) -> Optional[pd.DataFrame]:
    """读取近 N 个月（含当月）连板基因 CSV。无文件返回 None。"""
    return _read_history('gene', months, cfg)


def read_hotmoney_history(months: int = 3, cfg=None) -> Optional[pd.DataFrame]:
    """读取近 N 个月（含当月）游资席位 CSV。无文件返回 None。"""
    return _read_history('hotmoney', months, cfg)


def _read_history(file_prefix: str, months: int, cfg=None) -> Optional[pd.DataFrame]:
    """读取近 N 个月 CSV 历史。"""
    base = _csv_dir(cfg)
    if not base.exists():
        return None
    now = datetime.now()
    targets = []
    for i in range(months):
        y, m = now.year, now.month - i
        while m <= 0:
            m += 12
            y -= 1
        targets.append(f"{y:04d}-{m:02d}")
    files = [base / f"{file_prefix}_{t}.csv" for t in targets]
    files = [f for f in files if f.exists()]
    if not files:
        return None
    dfs = []
    for f in files:
        try:
            d = pd.read_csv(f, encoding='utf-8-sig')
            dfs.append(d)
        except Exception:
            continue
    if not dfs:
        return None
    df = pd.concat(dfs, ignore_index=True)
    if 'ref_date' in df.columns:
        df = df.drop_duplicates(subset=['ref_date', 'code'], keep='last') \
            if 'code' in df.columns else df.drop_duplicates(subset='ref_date', keep='last')
        df = df.sort_values('ref_date').reset_index(drop=True)
    return df


def save_markdown_report(content: str, ref_date: str, cfg=None) -> Optional[Path]:
    """保存 Markdown 报告到文件。

    路径: data/reports/stock_character/YYYY-MM/YYYY-MM-DD_stock_character.md
    """
    if cfg is not None and hasattr(cfg, 'reports_root'):
        base = cfg.reports_root()
    else:
        base = _TRADING_LAB_DIR / "data" / "reports" / "stock_character"
    base.mkdir(parents=True, exist_ok=True)
    # 按月分子目录
    month = _month_key(ref_date)
    month_dir = base / month
    month_dir.mkdir(parents=True, exist_ok=True)
    path = month_dir / f"{ref_date}_stock_character.md"
    try:
        path.write_text(content, encoding='utf-8')
        logger.debug("Markdown 报告已保存: %s", path)
        return path
    except Exception as e:
        logger.warning("保存 Markdown 报告失败: %s", e)
        return None
