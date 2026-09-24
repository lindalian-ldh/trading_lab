"""交易习惯约束器 MVP 数据库层。

约定：
    - DB 路径：与本文件同目录的 trade_habit.db
    - 连接：contextmanager 包装，自动 commit/close
    - 全部 SQL 用命名占位符
    - 时间戳统一 ISO 字符串（timespec='seconds'）
    - init_db() 幂等：建 6 张表 + 写默认 settings
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Optional


# ── 路径派生 ──
_SERVICE_DIR = Path(__file__).resolve().parent
_DB_PATH = _SERVICE_DIR / "trade_habit.db"


# ── 默认设置 ──
DEFAULT_SETTINGS: dict[str, str] = {
    "account_balance": "100000",
    "risk_per_trade_pct": "0.5",
    "daily_max_loss": "1000",
    "daily_max_trades": "3",
    "emotion_tags": "平静,焦虑,贪婪,恐惧,无聊,急躁",
}


# ====================================================================
# 连接管理
# ====================================================================

@contextmanager
def _connect():
    """连接上下文：自动 commit 与关闭。"""
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_db_path() -> Path:
    """暴露 DB 路径。"""
    return _DB_PATH


# ====================================================================
# 建库 + 默认设置
# ====================================================================

def init_db() -> None:
    """幂等建库：6 张表 + 默认 settings。"""
    with _connect() as conn:
        # settings：键值配置
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # stock_pool：股票池
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                name TEXT,
                logic TEXT,
                key_level TEXT,
                catalyst TEXT,
                risk TEXT,
                status TEXT NOT NULL DEFAULT '观察',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        # trade_plan：计划单
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_plan (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_code TEXT NOT NULL,
                stock_name TEXT,
                entry_trigger TEXT,
                entry_price_low REAL,
                entry_price_high REAL,
                stop_loss REAL,
                time_stop_date TEXT,
                target_price REAL,
                planned_shares INTEGER,
                max_loss_amount REAL,
                invalidation TEXT,
                status TEXT NOT NULL DEFAULT '草稿',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        # trade_log：交易日志
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER,
                stock_code TEXT NOT NULL,
                stock_name TEXT,
                entry_time TEXT,
                entry_price REAL,
                shares INTEGER,
                exit_time TEXT,
                exit_price REAL,
                pnl_amount REAL,
                pnl_r REAL,
                fees REAL,
                followed_plan INTEGER,
                deviation_reason TEXT,
                emotion_tag TEXT,
                emotion_intensity INTEGER,
                notes TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (plan_id) REFERENCES trade_plan(id)
            )
        """)
        # checklist_run：检查清单执行记录
        conn.execute("""
            CREATE TABLE IF NOT EXISTS checklist_run (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER,
                trade_log_id INTEGER,
                checked_items TEXT,
                all_passed INTEGER,
                override_reason TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (plan_id) REFERENCES trade_plan(id),
                FOREIGN KEY (trade_log_id) REFERENCES trade_log(id)
            )
        """)
        # red_flag_event：红灯事件
        conn.execute("""
            CREATE TABLE IF NOT EXISTS red_flag_event (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                detail TEXT,
                override_reason TEXT,
                created_at TEXT NOT NULL
            )
        """)
        # 索引
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_stock_pool_code ON stock_pool(code)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_stock_pool_status ON stock_pool(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trade_plan_status ON trade_plan(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trade_plan_stock ON trade_plan(stock_code)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trade_log_plan ON trade_log(plan_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trade_log_entry ON trade_log(entry_time)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trade_log_exit ON trade_log(exit_time)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_checklist_run_plan ON checklist_run(plan_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_red_flag_created ON red_flag_event(created_at)"
        )

        # 写默认 settings（INSERT OR IGNORE 幂等）
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (k, v),
            )


# ====================================================================
# settings 表 CRUD
# ====================================================================

def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """按 key 取单条设置。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
    return row["value"] if row else default


def get_settings() -> dict[str, str]:
    """一次取全部 settings。"""
    with _connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_setting(key: str, value: str) -> None:
    """单条设置保存（INSERT OR REPLACE）。"""
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
            (key, str(value)),
        )


def set_settings(data: dict[str, str]) -> None:
    """批量保存 settings。"""
    with _connect() as conn:
        for k, v in data.items():
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                (k, str(v)),
            )


# ====================================================================
# stock_pool 表 CRUD
# ====================================================================

def list_stock_pool(status_filter: Optional[str] = None) -> list[dict]:
    """列出股票池（按 id 升序）。可按 status 过滤。"""
    with _connect() as conn:
        if status_filter:
            rows = conn.execute(
                "SELECT * FROM stock_pool WHERE status=? ORDER BY id ASC",
                (status_filter,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM stock_pool ORDER BY id ASC"
            ).fetchall()
    return [dict(r) for r in rows]


def get_stock_pool(pool_id: int) -> Optional[dict]:
    """按 id 取股票池单条。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM stock_pool WHERE id=?", (pool_id,)
        ).fetchone()
    return dict(row) if row else None


def get_stock_pool_by_code(
    code: str, status: Optional[str] = None
) -> Optional[dict]:
    """按 code 取股票池单条。可限定 status。"""
    with _connect() as conn:
        if status:
            row = conn.execute(
                "SELECT * FROM stock_pool WHERE code=? AND status=? ORDER BY id DESC LIMIT 1",
                (code, status),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM stock_pool WHERE code=? ORDER BY id DESC LIMIT 1",
                (code,),
            ).fetchone()
    return dict(row) if row else None


def insert_stock_pool(data: dict) -> int:
    """插入股票池。返回新 id。"""
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO stock_pool
                (code, name, logic, key_level, catalyst, risk, status,
                 created_at, updated_at)
            VALUES
                (:code, :name, :logic, :key_level, :catalyst, :risk, :status,
                 :now, :now)
            """,
            {
                "code": data.get("code", ""),
                "name": data.get("name", ""),
                "logic": data.get("logic", ""),
                "key_level": data.get("key_level", ""),
                "catalyst": data.get("catalyst", ""),
                "risk": data.get("risk", ""),
                "status": data.get("status", "观察"),
                "now": now,
            },
        )
        return int(cur.lastrowid)


def update_stock_pool(pool_id: int, data: dict) -> None:
    """更新股票池。data 中的 status/字段会被覆盖。"""
    now = datetime.now().isoformat(timespec="seconds")
    fields = []
    params: dict = {"pid": pool_id, "now": now}
    for k in ("code", "name", "logic", "key_level",
              "catalyst", "risk", "status"):
        if k in data:
            fields.append(f"{k}=:{k}")
            params[k] = data[k]
    if not fields:
        return
    fields.append("updated_at=:now")
    sql = f"UPDATE stock_pool SET {', '.join(fields)} WHERE id=:pid"
    with _connect() as conn:
        conn.execute(sql, params)


def delete_stock_pool(pool_id: int) -> None:
    """删除股票池单条。"""
    with _connect() as conn:
        conn.execute("DELETE FROM stock_pool WHERE id=?", (pool_id,))


# ====================================================================
# trade_plan 表 CRUD
# ====================================================================

def list_trade_plans(status_filter: Optional[str] = None) -> list[dict]:
    """列出计划单（按 id 降序，最新的在上）。"""
    with _connect() as conn:
        if status_filter:
            rows = conn.execute(
                "SELECT * FROM trade_plan WHERE status=? ORDER BY id DESC",
                (status_filter,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trade_plan ORDER BY id DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def get_trade_plan(plan_id: int) -> Optional[dict]:
    """按 id 取计划单单条。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM trade_plan WHERE id=?", (plan_id,)
        ).fetchone()
    return dict(row) if row else None


def insert_trade_plan(data: dict) -> int:
    """插入计划单。返回新 id。"""
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO trade_plan
                (stock_code, stock_name, entry_trigger,
                 entry_price_low, entry_price_high,
                 stop_loss, time_stop_date, target_price,
                 planned_shares, max_loss_amount, invalidation,
                 status, created_at, updated_at)
            VALUES
                (:stock_code, :stock_name, :entry_trigger,
                 :entry_price_low, :entry_price_high,
                 :stop_loss, :time_stop_date, :target_price,
                 :planned_shares, :max_loss_amount, :invalidation,
                 :status, :now, :now)
            """,
            {
                "stock_code": data.get("stock_code", ""),
                "stock_name": data.get("stock_name", ""),
                "entry_trigger": data.get("entry_trigger", ""),
                "entry_price_low": data.get("entry_price_low"),
                "entry_price_high": data.get("entry_price_high"),
                "stop_loss": data.get("stop_loss"),
                "time_stop_date": data.get("time_stop_date", ""),
                "target_price": data.get("target_price"),
                "planned_shares": data.get("planned_shares"),
                "max_loss_amount": data.get("max_loss_amount"),
                "invalidation": data.get("invalidation", ""),
                "status": data.get("status", "草稿"),
                "now": now,
            },
        )
        return int(cur.lastrowid)


def update_trade_plan(plan_id: int, data: dict) -> None:
    """更新计划单。"""
    now = datetime.now().isoformat(timespec="seconds")
    allowed = (
        "stock_code", "stock_name", "entry_trigger",
        "entry_price_low", "entry_price_high",
        "stop_loss", "time_stop_date", "target_price",
        "planned_shares", "max_loss_amount", "invalidation",
        "status",
    )
    fields = []
    params: dict = {"pid": plan_id, "now": now}
    for k in allowed:
        if k in data:
            fields.append(f"{k}=:{k}")
            params[k] = data[k]
    if not fields:
        return
    fields.append("updated_at=:now")
    sql = f"UPDATE trade_plan SET {', '.join(fields)} WHERE id=:pid"
    with _connect() as conn:
        conn.execute(sql, params)


def delete_trade_plan(plan_id: int) -> None:
    """删除计划单单条。仅允许在 trade_log 无关联记录时调用，
    否则外键约束会阻止删除（实际会因 trade_log.plan_id 引用而失败，
    调用方应在 UI 层先检查 status 是否在「草稿/待触发/取消」中）。
    """
    with _connect() as conn:
        conn.execute("DELETE FROM trade_plan WHERE id=?", (plan_id,))


# ====================================================================
# trade_log 表 CRUD
# ====================================================================

def list_trade_logs(date_str: Optional[str] = None) -> list[dict]:
    """列出交易日志（按 id 降序）。
    date_str 给定时仅返回 entry_time 当天匹配的记录。
    """
    with _connect() as conn:
        if date_str:
            like = f"{date_str}%"
            rows = conn.execute(
                "SELECT * FROM trade_log WHERE entry_time LIKE ? "
                "ORDER BY id DESC",
                (like,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trade_log ORDER BY id DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def get_trade_log(log_id: int) -> Optional[dict]:
    """按 id 取交易日志单条。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM trade_log WHERE id=?", (log_id,)
        ).fetchone()
    return dict(row) if row else None


def get_trade_log_by_plan(plan_id: int) -> Optional[dict]:
    """按 plan_id 取最新一条 trade_log。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM trade_log WHERE plan_id=? "
            "ORDER BY id DESC LIMIT 1",
            (plan_id,),
        ).fetchone()
    return dict(row) if row else None


def insert_trade_log(data: dict) -> int:
    """插入交易日志。返回新 id。"""
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO trade_log
                (plan_id, stock_code, stock_name,
                 entry_time, entry_price, shares,
                 exit_time, exit_price, pnl_amount, pnl_r, fees,
                 followed_plan, deviation_reason,
                 emotion_tag, emotion_intensity, notes,
                 created_at)
            VALUES
                (:plan_id, :stock_code, :stock_name,
                 :entry_time, :entry_price, :shares,
                 :exit_time, :exit_price, :pnl_amount, :pnl_r, :fees,
                 :followed_plan, :deviation_reason,
                 :emotion_tag, :emotion_intensity, :notes,
                 :now)
            """,
            {
                "plan_id": data.get("plan_id"),
                "stock_code": data.get("stock_code", ""),
                "stock_name": data.get("stock_name", ""),
                "entry_time": data.get("entry_time"),
                "entry_price": data.get("entry_price"),
                "shares": data.get("shares"),
                "exit_time": data.get("exit_time"),
                "exit_price": data.get("exit_price"),
                "pnl_amount": data.get("pnl_amount"),
                "pnl_r": data.get("pnl_r"),
                "fees": data.get("fees"),
                "followed_plan": data.get("followed_plan"),
                "deviation_reason": data.get("deviation_reason", ""),
                "emotion_tag": data.get("emotion_tag", ""),
                "emotion_intensity": data.get("emotion_intensity"),
                "notes": data.get("notes", ""),
                "now": now,
            },
        )
        return int(cur.lastrowid)


def update_trade_log(log_id: int, data: dict) -> None:
    """更新交易日志。"""
    allowed = (
        "plan_id", "stock_code", "stock_name",
        "entry_time", "entry_price", "shares",
        "exit_time", "exit_price", "pnl_amount", "pnl_r", "fees",
        "followed_plan", "deviation_reason",
        "emotion_tag", "emotion_intensity", "notes",
    )
    fields = []
    params: dict = {"lid": log_id}
    for k in allowed:
        if k in data:
            fields.append(f"{k}=:{k}")
            params[k] = data[k]
    if not fields:
        return
    sql = f"UPDATE trade_log SET {', '.join(fields)} WHERE id=:lid"
    with _connect() as conn:
        conn.execute(sql, params)


def delete_trade_log(log_id: int) -> None:
    """删除交易日志单条。"""
    with _connect() as conn:
        conn.execute("DELETE FROM trade_log WHERE id=?", (log_id,))


# ====================================================================
# checklist_run 表 CRUD
# ====================================================================

def list_checklist_runs(date_str: Optional[str] = None) -> list[dict]:
    """列出检查清单执行记录（按 id 降序）。"""
    with _connect() as conn:
        if date_str:
            like = f"{date_str}%"
            rows = conn.execute(
                "SELECT * FROM checklist_run WHERE created_at LIKE ? "
                "ORDER BY id DESC",
                (like,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM checklist_run ORDER BY id DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def insert_checklist_run(data: dict) -> int:
    """插入检查清单记录。返回新 id。"""
    now = datetime.now().isoformat(timespec="seconds")
    items = data.get("checked_items")
    if isinstance(items, (list, dict)):
        items = json.dumps(items, ensure_ascii=False)
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO checklist_run
                (plan_id, trade_log_id, checked_items, all_passed,
                 override_reason, created_at)
            VALUES
                (:plan_id, :trade_log_id, :checked_items, :all_passed,
                 :override_reason, :now)
            """,
            {
                "plan_id": data.get("plan_id"),
                "trade_log_id": data.get("trade_log_id"),
                "checked_items": items,
                "all_passed": 1 if data.get("all_passed") else 0,
                "override_reason": data.get("override_reason", ""),
                "now": now,
            },
        )
        return int(cur.lastrowid)


def update_checklist_run_trade_log_id(run_id: int,
                                       trade_log_id: int) -> None:
    """回填 checklist_run 的 trade_log_id 关联。"""
    with _connect() as conn:
        conn.execute(
            "UPDATE checklist_run SET trade_log_id=? WHERE id=?",
            (trade_log_id, run_id),
        )


# ====================================================================
# red_flag_event 表 CRUD
# ====================================================================

def list_red_flags(date_str: Optional[str] = None) -> list[dict]:
    """列出红灯事件（按 id 降序）。"""
    with _connect() as conn:
        if date_str:
            like = f"{date_str}%"
            rows = conn.execute(
                "SELECT * FROM red_flag_event WHERE created_at LIKE ? "
                "ORDER BY id DESC",
                (like,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM red_flag_event ORDER BY id DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def insert_red_flag(
    event_type: str,
    detail: str = "",
    override_reason: Optional[str] = None,
) -> int:
    """插入红灯事件。返回新 id。"""
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO red_flag_event
                (event_type, detail, override_reason, created_at)
            VALUES
                (:event_type, :detail, :override_reason, :now)
            """,
            {
                "event_type": event_type,
                "detail": detail,
                "override_reason": override_reason or "",
                "now": now,
            },
        )
        return int(cur.lastrowid)


# ====================================================================
# 今日统计聚合
# ====================================================================

def count_today_entries() -> int:
    """今日入场次数：trade_log.entry_time 当天匹配的记录数。"""
    today = date.today().isoformat()
    like = f"{today}%"
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM trade_log WHERE entry_time LIKE ?",
            (like,),
        ).fetchone()
    return int(row["c"])


def sum_today_pnl() -> float:
    """今日已实现盈亏：今日 exit_time 的 trade_log.pnl_amount 之和。
    无出场记录则返回 0.0。
    """
    today = date.today().isoformat()
    like = f"{today}%"
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl_amount), 0.0) AS s FROM trade_log "
            "WHERE exit_time LIKE ?",
            (like,),
        ).fetchone()
    return float(row["s"] or 0.0)


def count_today_checklist_all_passed() -> int:
    """今日 checklist_run all_passed=1 数。"""
    today = date.today().isoformat()
    like = f"{today}%"
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM checklist_run "
            "WHERE created_at LIKE ? AND all_passed=1",
            (like,),
        ).fetchone()
    return int(row["c"])


def count_today_checklist_total() -> int:
    """今日 checklist_run 总数。"""
    today = date.today().isoformat()
    like = f"{today}%"
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM checklist_run WHERE created_at LIKE ?",
            (like,),
        ).fetchone()
    return int(row["c"])


def count_today_red_flag_overrides() -> int:
    """今日 red_flag_event 数。"""
    today = date.today().isoformat()
    like = f"{today}%"
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM red_flag_event WHERE created_at LIKE ?",
            (like,),
        ).fetchone()
    return int(row["c"])
