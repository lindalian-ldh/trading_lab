#!/usr/bin/env python3
"""交易习惯约束器 MVP 启动入口。

运行方式：
    cd services/trade_habit
    python main.py

首次启动自动创建 trade_habit.db 与全部 6 张表，写入默认设置。
零第三方依赖，仅用 Python 3.11+ 标准库。
"""
from __future__ import annotations

import sys

from db import init_db
from ui import TradeHabitApp


def main() -> int:
    # 幂等：建库、建表、写默认 settings
    init_db()
    # 启动 GUI
    app = TradeHabitApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
