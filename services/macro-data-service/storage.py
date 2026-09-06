"""宏观数据本地存储：SQLite（默认）与 Parquet。

- SQLite：
  - macro_data：中国宏观指标，主键 (indicator, date, country) 天然去重。
  - macro_us_data：美国及商品指标，结构同 macro_data，独立分表便于隔离管理。
- Parquet：按 indicator/year 分区，中国指标存 data/macro/，美国/商品指标存 data/macro_us/。
- 写入策略：incremental=增量去重(INSERT OR IGNORE / 合并去重)，overwrite=全量覆盖。

复用项目 settings（数据库路径、数据目录），不引入 sqlalchemy，保持极简。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd

from config.settings import settings
from core.logger import get_logger

log = get_logger("macro.storage")

TABLE = "macro_data"
TABLE_US = "macro_us_data"
COLS = [
    "indicator", "name", "country", "frequency", "date", "value",
    "unit", "source", "source_url", "fetch_time", "extra",
]


# --------------------------- SQLite --------------------------- #

def _connect() -> sqlite3.Connection:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(settings.db_path)


def _init_table(conn: sqlite3.Connection, table: str) -> None:
    """建表（幂等）。主键即唯一索引，避免重复插入。"""
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {table} (
            indicator TEXT NOT NULL,
            name      TEXT,
            country   TEXT NOT NULL,
            frequency TEXT,
            date      TEXT NOT NULL,
            value     REAL,
            unit      TEXT,
            source    TEXT,
            source_url TEXT,
            fetch_time TEXT,
            extra     TEXT,
            PRIMARY KEY (indicator, date, country)
        )"""
    )


def init_macro_db() -> None:
    """建中国宏观数据表（幂等）。"""
    with _connect() as conn:
        _init_table(conn, TABLE)
        conn.commit()


def init_macro_us_db() -> None:
    """建美国/商品宏观数据表（幂等）。"""
    with _connect() as conn:
        _init_table(conn, TABLE_US)
        conn.commit()


def _row_tuple(r: dict) -> tuple:
    row = {k: r.get(k) for k in COLS}
    row["extra"] = json.dumps(r.get("extra") or {}, ensure_ascii=False)
    return tuple(row[k] for k in COLS)


def save_records_sqlite(
    records: list[dict], overwrite: bool = False, table: str = TABLE
) -> int:
    """写入 SQLite。

    - incremental：INSERT OR IGNORE 跳过主键重复。
    - overwrite：先删除本批次涉及的所有 (indicator, country) 旧行，再全量插入。
      （INSERT OR REPLACE 仅替换冲突行，无法删除新数据中不再存在的旧记录，会导致残留。）
    table 指定目标表（macro_data 或 macro_us_data），表不存在时自动创建。
    """
    if not records:
        return 0
    placeholders = ",".join("?" * len(COLS))
    cols_csv = ",".join(COLS)
    written = 0
    with _connect() as conn:
        _init_table(conn, table)
        if overwrite:
            # 全量覆盖：先清除本批次涉及 (indicator, country) 的旧数据，再插入新数据
            pairs = {(r.get("indicator"), r.get("country", "")) for r in records}
            for ind, ctry in pairs:
                conn.execute(
                    f"DELETE FROM {table} WHERE indicator=? AND country=?",
                    (ind, ctry),
                )
            action = "OR REPLACE"  # 兜底：同批次内主键重复
        else:
            action = "OR IGNORE"
        for r in records:
            cur = conn.execute(
                f"INSERT {action} INTO {table} ({cols_csv}) VALUES ({placeholders})",
                _row_tuple(r),
            )
            if cur.rowcount and cur.rowcount > 0:
                written += 1
        conn.commit()
    log.info("SQLite[%s] 写入 %d/%d 条 (overwrite=%s)", table, written, len(records), overwrite)
    return written


def load_records_sqlite(
    indicator: str | None = None,
    country: str | None = None,
    table: str = TABLE,
) -> pd.DataFrame:
    """便捷查询：按 indicator/country 过滤读取。"""
    sql = f"SELECT {','.join(COLS)} FROM {table} WHERE 1=1"
    params: list = []
    if indicator:
        sql += " AND indicator=?"
        params.append(indicator)
    if country:
        sql += " AND country=?"
        params.append(country)
    sql += " ORDER BY date"
    with _connect() as conn:
        _init_table(conn, table)
        return pd.read_sql_query(sql, conn, params=params)


# --------------------------- Parquet --------------------------- #

def reset_macro_db() -> int:
    """清空 SQLite 中 macro_data 表全部记录，返回删除条数。"""
    with _connect() as conn:
        _init_table(conn, TABLE)
        cur = conn.execute(f"DELETE FROM {TABLE}")
        conn.commit()
        deleted = cur.rowcount or 0
    log.info("SQLite 已清空 macro_data 表 %d 条", deleted)
    return deleted


def reset_macro_us_db() -> int:
    """清空 SQLite 中 macro_us_data 表全部记录，返回删除条数。"""
    with _connect() as conn:
        _init_table(conn, TABLE_US)
        cur = conn.execute(f"DELETE FROM {TABLE_US}")
        conn.commit()
        deleted = cur.rowcount or 0
    log.info("SQLite 已清空 macro_us_data 表 %d 条", deleted)
    return deleted


def _macro_parquet_root(sub_root: str = "macro") -> Path:
    root = settings.data_dir / sub_root
    root.mkdir(parents=True, exist_ok=True)
    return root


def reset_macro_parquet(sub_root: str = "macro") -> int:
    """删除指定 Parquet 宏观数据根目录（默认 data/macro），返回删除文件数。"""
    import shutil

    root = settings.data_dir / sub_root
    if not root.exists():
        return 0
    n = sum(1 for _ in root.rglob("*.parquet"))
    shutil.rmtree(root)
    log.info("Parquet 已清空 %s 目录 %d 个文件", sub_root, n)
    return n


def save_records_parquet(
    records: list[dict], overwrite: bool = False, sub_root: str = "macro"
) -> int:
    """按 indicator/year 分区写 Parquet。

    sub_root 指定分区根目录名（macro=中国，macro_us=美国/商品）。
    overwrite：直接覆盖该分区文件。
    incremental：与已有文件合并后按 (indicator,date,country) 去重(保留最新)。
    """
    if not records:
        return 0
    df = pd.DataFrame(records)
    if "extra" in df.columns:
        df["extra"] = df["extra"].apply(lambda x: json.dumps(x, ensure_ascii=False))
    df["year"] = df["date"].str[:4]
    keep_cols = [c for c in COLS if c in df.columns]

    root = _macro_parquet_root(sub_root)
    written = 0
    for (ind, country), group in df.groupby(["indicator", "country"]):
        for year, yg in group.groupby("year"):
            out_dir = root / str(ind) / str(year)
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{ind}_{country}_{year}.parquet"
            yg = yg[keep_cols]
            if overwrite or not out_path.exists():
                yg.to_parquet(out_path, index=False)
                written += len(yg)
            else:
                existing = pd.read_parquet(out_path)
                combined = pd.concat([existing, yg], ignore_index=True)
                combined = combined.drop_duplicates(
                    subset=["indicator", "date", "country"], keep="last"
                )
                combined[keep_cols].to_parquet(out_path, index=False)
                written += len(combined) - len(existing)
    log.info("Parquet[%s] 写入 %d 条 (overwrite=%s)", sub_root, written, overwrite)
    return written
