"""持仓实例持久化：SQLite (data/trading.db) + CSV 卖出信号日志。

约定（与项目 memory 对齐）：
    - 时序数据 → Parquet（K线由 fetch_klines 服务负责）
    - 交易记录/元数据 → SQLite3
    - 卖出信号日志 → CSV（按天分文件，便于复盘）

positions 表主键：(symbol, entry_date) —— 同一只股票同一天只能开一个仓。
若需同一天多次开仓，可在 entry_date 后追加序号；当前实现不支持。
"""

from __future__ import annotations

import csv
import logging
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from position import Position

logger = logging.getLogger(__name__)


# ── 路径派生 ──
# sell_monitor/  →  services/  →  trading_lab/  →  data/
_SERVICE_DIR = Path(__file__).resolve().parent
_TRADING_LAB_DIR = _SERVICE_DIR.parent.parent
_DB_PATH = _TRADING_LAB_DIR / "data" / "trading.db"
_CSV_DIR = _TRADING_LAB_DIR / "data" / "sell_signals"


# ====================================================================
# SQLite 持久化
# ====================================================================

@contextmanager
def _connect():
    """连接上下文：自动 commit 与关闭。"""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """初始化 positions 表（幂等）。"""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                symbol TEXT NOT NULL,
                entry_price REAL NOT NULL,
                shares INTEGER NOT NULL,
                entry_date TEXT NOT NULL,
                hard_stop_price REAL NOT NULL,
                stop_loss_price REAL NOT NULL,
                break_even_price REAL NOT NULL,
                grid_level_index INTEGER NOT NULL,
                sold_shares INTEGER NOT NULL,
                highest_price_since_entry REAL NOT NULL,
                bars_held INTEGER NOT NULL,
                status TEXT NOT NULL,
                closed_reason TEXT,
                closed_price REAL,
                closed_date TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (symbol, entry_date)
            )
        """)
        # 已关闭持仓的归档索引（便于复盘查询）
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_positions_status
            ON positions(status)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_positions_closed_date
            ON positions(closed_date)
        """)


def save_position(position: Position) -> None:
    """保存（或更新）持仓到 SQLite。INSERT OR REPLACE 语义。

    主键 (symbol, entry_date) 冲突时整体替换，因此 Position 的所有字段
    都会被覆盖（包括 status、closed_* 等）。
    """
    with _connect() as conn:
        d = position.to_dict()
        conn.execute("""
            INSERT OR REPLACE INTO positions
            (symbol, entry_price, shares, entry_date, hard_stop_price, stop_loss_price,
             break_even_price, grid_level_index, sold_shares, highest_price_since_entry,
             bars_held, status, closed_reason, closed_price, closed_date, created_at)
            VALUES
            (:symbol, :entry_price, :shares, :entry_date, :hard_stop_price, :stop_loss_price,
             :break_even_price, :grid_level_index, :sold_shares, :highest_price_since_entry,
             :bars_held, :status, :closed_reason, :closed_price, :closed_date, :created_at)
        """, d)
    logger.debug("已保存持仓 %s entry_date=%s status=%s",
                 position.symbol, position.entry_date, position.status)


def load_position(symbol: str) -> Optional[Position]:
    """加载某只股票的当前未关闭持仓（status='open'）。

    若有多个未关闭持仓（理论上不应发生），取 entry_date 最近的。
    无则返回 None。
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE symbol=? AND status='open' "
            "ORDER BY entry_date DESC LIMIT 1",
            (symbol,)
        ).fetchone()
    if row is None:
        return None
    return Position.from_dict(dict(row))


def load_all_open_positions() -> list[Position]:
    """加载所有未关闭持仓，按开仓日期升序。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='open' ORDER BY entry_date ASC"
        ).fetchall()
    return [Position.from_dict(dict(r)) for r in rows]


def load_closed_positions(symbol: Optional[str] = None,
                          limit: int = 50) -> list[Position]:
    """加载已关闭持仓（用于复盘）。可按 symbol 过滤。"""
    with _connect() as conn:
        if symbol:
            rows = conn.execute(
                "SELECT * FROM positions WHERE status='closed' AND symbol=? "
                "ORDER BY closed_date DESC LIMIT ?",
                (symbol, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM positions WHERE status='closed' "
                "ORDER BY closed_date DESC LIMIT ?",
                (limit,)
            ).fetchall()
    return [Position.from_dict(dict(r)) for r in rows]


# ====================================================================
# CSV 卖出信号日志
# ====================================================================

_CSV_FIELDS = [
    'timestamp', 'symbol', 'action', 'reason', 'sell_price', 'sell_shares',
    'new_stop_price', 'cur_price', 'profit_pct', 'remaining_shares',
    'bars_held', 'highest_price', 'message',
]


def log_sell_signal(symbol: str, signal: dict, cur_price: float,
                    profit_pct: float, position: Position) -> Path:
    """将一次卖出信号追加写入 CSV 日志（按天分文件）。

    文件路径：data/sell_signals/sell_signals_YYYY-MM-DD.csv
    每天一个文件，首次写入时自动写表头。

    Args:
        symbol: 股票代码
        signal: check_exit_signals 返回的 dict
        cur_price: 当前现价（K线收盘）
        profit_pct: 当前浮盈比例（小数，如 0.051 表示 +5.1%）
        position: 持仓实例（用于取 remaining_shares / bars_held / highest）

    Returns:
        CSV 文件路径
    """
    _CSV_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = _CSV_DIR / f"sell_signals_{date.today().isoformat()}.csv"

    row = [
        datetime.now().isoformat(timespec='seconds'),
        symbol,
        signal['action'],
        signal['reason'],
        f"{signal['sell_price']:.4f}",
        signal['sell_shares'],
        f"{signal['new_stop_price']:.4f}",
        f"{cur_price:.4f}",
        f"{profit_pct * 100:.2f}",
        position.remaining_shares,
        position.bars_held,
        f"{position.highest_price_since_entry:.4f}",
        signal['message'],
    ]

    write_header = not csv_path.exists()
    with csv_path.open('a', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(_CSV_FIELDS)
        w.writerow(row)

    logger.info("卖出信号已记录 %s action=%s reason=%s",
                symbol, signal['action'], signal['reason'])
    return csv_path


def get_db_path() -> Path:
    """暴露 DB 路径，便于测试或外部查询。"""
    return _DB_PATH


def get_csv_dir() -> Path:
    """暴露 CSV 目录路径。"""
    return _CSV_DIR
