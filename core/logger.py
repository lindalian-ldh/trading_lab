"""日志模块：按天分割，同时输出到终端和 logs/{date}.log。

适用于“跑完即退”的批处理：每次进程启动时按当天日期生成日志文件。
"""
from __future__ import annotations

import logging
import sys
from datetime import date

from config.settings import settings

_CONFIGURED = False
_CURRENT_LOG_FILE: str | None = None


def _configure_root() -> str:
    """配置根 Logger（仅首次调用生效）。返回当前日志文件路径。"""
    global _CONFIGURED, _CURRENT_LOG_FILE

    logs_dir = settings.logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / f"{date.today().isoformat()}.log"
    _CURRENT_LOG_FILE = str(log_file)

    if not _CONFIGURED:
        root = logging.getLogger()
        root.setLevel(settings.log_level)
        root.handlers.clear()

        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)

        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)

        _CONFIGURED = True

    return _CURRENT_LOG_FILE


def get_logger(name: str) -> logging.Logger:
    """返回 Logger 实例，同时输出到终端和 logs/{date}.log 文件。"""
    _configure_root()
    return logging.getLogger(name)


def current_log_file() -> str | None:
    """返回当前进程使用的日志文件路径（未配置则返回 None）。"""
    return _CURRENT_LOG_FILE
