#!/usr/bin/env python
"""P3.1 主题转折**观察哨** —— 一条命令看到 10 个主题的 L1/L2/RS 点亮情况。

## 契约（P3.3，**不可协商**）

- **只显示、不决策**：本脚本输出**永不**进入仓位/买卖/止损逻辑；
- 每个主题都带 `validated=False`（Phase 2 未通过 P0.5 预注册判据）；
- **数据不可用必须显式**：缺数据 / 过期一律打印 `❌ 数据不可用`，
  **绝不静默当成"安全"或"无信号"**。

## 与 PANIC_DOWN 的关系（P2.6 的最终形态）

PANIC_DOWN 能下指令，转折信号不能 ⇒ 结构上不可能给出矛盾指令。

## 用法

    .venv/bin/python scripts/theme_timing_report.py                  # 最新交易日
    .venv/bin/python scripts/theme_timing_report.py --date 2026-09-30  # 历史日期（可复现）
    .venv/bin/python scripts/theme_timing_report.py --save           # 并追加到主题级台账
    .venv/bin/python scripts/theme_timing_report.py --json
    .venv/bin/python scripts/theme_timing_report.py --gate           # 有主题不可用 ⇒ 退出码 1

产出（`--save`）：`data/observations/theme_signal_ledger.csv`（幂等：同 `(date, theme)` 不重复写）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.marketdata_tx import fetch_index_history  # noqa: E402
from core.theme_sentinel import (  # noqa: E402
    DISCIPLINE_NOTE,
    VALIDATION_NOTE,
    arbitration_note,
    build_sentinel,
    format_sentinel,
)
from core.theme_universe import THEMES  # noqa: E402

LEDGER = ROOT / "data" / "observations" / "theme_signal_ledger.csv"

#: 主题级台账的族标签（与个股级 signal_ledger.csv 的 'entry_gate' 区分）
SIGNAL_FAMILY = "theme_timing"

COLUMNS = [
    "date", "theme", "index", "anchor", "close", "bars", "last_bar", "staleness_days",
    "l1", "l2", "rs", "rs_available", "l1_state_days", "l1_span_start", "l1_span_end",
    "signal_name", "status", "signal_family", "validated", "available", "note", "logged_at",
]


def _loader(online: bool):
    def _load(code):
        if not code:
            return None
        return fetch_index_history(code, refresh=online, prefer_cache=not online)
    return _load


def _signal_name(r: dict) -> str:
    lit = [k.upper() if k in ("l1", "l2", "rs") else k for k in ("l1", "l2", "rs") if r.get(k)]
    return "+".join(lit) if lit else "-"


def _to_rows(rows: list) -> pd.DataFrame:
    out = []
    for r in rows:
        out.append({
            "date": r["as_of"], "theme": r["theme"], "index": r["index"],
            "anchor": r["anchor"], "close": r["close"], "bars": r["bars"],
            "last_bar": r["last_bar"], "staleness_days": r["staleness_days"],
            "l1": r["l1"], "l2": r["l2"], "rs": r["rs"],
            "rs_available": r["rs_available"], "l1_state_days": r["l1_state_days"],
            "l1_span_start": r["l1_span_start"], "l1_span_end": r["l1_span_end"],
            "signal_name": _signal_name(r), "status": r["status"],
            "signal_family": SIGNAL_FAMILY, "validated": False,
            "available": r["available"],
            "note": "；".join(r["warnings"]) if r["warnings"] else "",
            "logged_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
    return pd.DataFrame(out, columns=COLUMNS)


def save_ledger(rows: list, path: Path = LEDGER) -> tuple:
    """幂等追加：同 ``(date, theme)`` 只保留最新一版。返回 ``(新增数, 总行数)``。"""
    new = _to_rows([r for r in rows if r["available"]])
    if new.empty:
        return 0, 0
    path.parent.mkdir(parents=True, exist_ok=True)
    old = pd.read_csv(path, encoding="utf-8-sig") if path.exists() else pd.DataFrame(columns=COLUMNS)
    for c in COLUMNS:
        if c not in old.columns:
            old[c] = pd.NA
    merged = pd.concat([old[COLUMNS], new], ignore_index=True)
    before = len(merged)
    merged = merged.drop_duplicates(subset=["date", "theme"], keep="last")
    merged = merged.sort_values(["date", "theme"]).reset_index(drop=True)
    merged.to_csv(path, index=False, encoding="utf-8-sig")
    return len(new), len(merged)


def main() -> int:
    ap = argparse.ArgumentParser(description="主题转折观察哨（只显示、不决策）")
    ap.add_argument("--date", default=None, help="观测日 YYYY-MM-DD（默认最新交易日）")
    ap.add_argument("--online", action="store_true", help="联网刷新指数缓存")
    ap.add_argument("--save", action="store_true", help=f"追加到 {LEDGER.name}")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--gate", action="store_true", help="有主题数据不可用 ⇒ 退出码 1")
    args = ap.parse_args()

    rows = build_sentinel(THEMES, _loader(args.online), as_of=args.date)

    if args.json:
        print(json.dumps([{**r, "signal_family": SIGNAL_FAMILY, "discipline": DISCIPLINE_NOTE}
                          for r in rows], ensure_ascii=False, indent=2, default=str))
    else:
        print(format_sentinel(rows, as_of=args.date))

    if args.save:
        written, total = save_ledger(rows)
        print(f"\n台账: {LEDGER}")
        print(f"  本次处理 {written} 行（同 (date, theme) 幂等覆盖）   台账总行数 {total}")

    bad = [r for r in rows if not r["available"]]
    if args.gate and bad:
        print(f"\n❌ 观察哨数据门禁未通过：{len(bad)} 个主题不可用 → "
              f"{', '.join(r['theme'] for r in bad)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
