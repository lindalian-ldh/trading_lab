"""加载 config.yaml 配置。

配置项：存储方式/写入策略、获取参数(重试/日期范围)、数据源开关与优先级、指标开关。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# 本文件所在目录即服务目录，config.yaml 与之同级
CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """读取 YAML 配置；文件缺失则返回空 dict（使用代码内默认值）。"""
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        return {}
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def is_indicator_enabled(cfg: dict[str, Any], key: str) -> bool:
    """指标是否启用（未配置默认为启用）。"""
    ind = cfg.get("indicators", {}) or {}
    return bool(ind.get(key, {}).get("enabled", True))
