"""宏观数据统一数据模型。

每条记录对应一份「指标-国家-日期」的观测值，字段与需求文档 JSON 结构一一对应。
唯一键：indicator + date + country。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class MacroRecord:
    """单条宏观数据记录。"""

    indicator: str            # 指标英文简称，如 CPI / LPR1Y / PMI
    name: str                 # 指标中文全称
    country: str              # 国家/地区代码 (ISO 3166-1 alpha-2)，如 CN / US
    frequency: str            # 数据频率：D/W/M/Q/Y
    date: str                 # 数据日期 YYYY-MM-DD
    value: float              # 数值
    unit: str                 # 单位，如 % / 指数 / 亿元 / 万亿元
    source: str               # 数据来源名称
    source_url: str           # 可追溯 URL/函数名
    fetch_time: str           # 获取时间 YYYY-MM-DD HH:MM:SS
    extra: dict[str, Any] = field(default_factory=dict)  # 预测值/前值/分类等扩展字段

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def record_key(record: dict[str, Any]) -> tuple[str, str, str]:
    """返回唯一键 (indicator, date, country)。"""
    return (record["indicator"], record["date"], record["country"])
