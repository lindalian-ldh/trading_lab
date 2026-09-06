"""存储统一接口：Parquet(K线) / SQLite(元数据) / JSON(爬虫缓存)。

保持函数式透明，无连接对象泄漏。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from config.settings import settings


def _safe_symbol(symbol: str) -> str:
    """将 BTC/USDT -> BTC_USDT，作为目录名安全形式。"""
    return symbol.replace("/", "_").replace(":", "_")


# ---------------- K线 (Parquet) ----------------

def save_klines(df: pd.DataFrame, symbol: str, date_str: str) -> Path:
    """将 DataFrame 保存至 data/raw/klines/{symbol}/{date}.parquet。"""
    out_dir = settings.klines_dir / _safe_symbol(symbol)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{date_str}.parquet"
    df.to_parquet(out_path, index=False)
    return out_path


def load_klines(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """读取并合并 {symbol} 目录下 [start_date, end_date] 区间的 Parquet 文件。

    文件名约定为 YYYY-MM-DD.parquet，按字符串区间比较即可。
    """
    sym_dir = settings.klines_dir / _safe_symbol(symbol)
    if not sym_dir.exists():
        return pd.DataFrame()

    frames: list[pd.DataFrame] = []
    for f in sorted(sym_dir.glob("*.parquet")):
        file_date = f.stem
        if start_date <= file_date <= end_date:
            frames.append(pd.read_parquet(f))

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ---------------- JSON (爬虫缓存) ----------------

def save_json(data: Any, filepath: str | Path) -> Path:
    """保存 JSON 数据。filepath 可为相对 data/html_cache/ 的路径或绝对路径。"""
    p = Path(filepath)
    if not p.is_absolute():
        p = settings.html_cache_dir / p
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    return p


def load_json(filepath: str | Path) -> Any:
    """读取 JSON 文件（相对路径基于 data/html_cache/）。"""
    p = Path(filepath)
    if not p.is_absolute():
        p = settings.html_cache_dir / p
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


# ---------------- SQLite (元数据) ----------------

@contextmanager
def _connect():
    """连接上下文：自动 commit 与关闭。"""
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def execute_sql(query: str, params: Iterable | tuple = ()) -> list[sqlite3.Row]:
    """连接 data/trading.db 执行 SQL（自动处理连接关闭）。

    返回 SELECT 的行列表（sqlite3.Row），非查询语句返回空列表。
    """
    with _connect() as conn:
        cur = conn.execute(query, tuple(params))
        try:
            return cur.fetchall()
        except sqlite3.ProgrammingError:
            return []


def init_db() -> None:
    """初始化 SQLite 表结构（幂等）。"""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trading_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT,
                price REAL,
                amount REAL,
                ts TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS run_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task TEXT NOT NULL,
                status TEXT,
                detail TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
            """
        )
