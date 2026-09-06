"""CCXT 交易所 REST 封装：仅暴露盘后所需的简单 K线抓取接口。

无 API Key 时仅读取公开行情（适用于历史回补）。
"""
from __future__ import annotations

from typing import Optional

import ccxt

from config.settings import settings
from core.logger import get_logger

log = get_logger(__name__)


def get_exchange(name: Optional[str] = None):
    """根据名称创建 CCXT 交易所实例（REST）。"""
    ex_name = (name or settings.exchange).lower()
    exchange_cls = getattr(ccxt, ex_name, None)
    if exchange_cls is None:
        raise ValueError(f"不支持的交易所: {ex_name}")
    return exchange_cls({"enableRateLimit": True})


def fetch_daily_klines(symbol: str, since_date: str, limit: int = 500) -> list[list]:
    """抓取指定 symbol 的日线 OHLCV。

    返回 CCXT 原始列表 [ts, open, high, low, close, volume]。
    """
    ex = get_exchange()
    log.info("抓取 %s 日线 (since=%s, limit=%d)", symbol, since_date, limit)
    since_ms = ex.parse8601(f"{since_date}T00:00:00Z")
    return ex.fetch_ohlcv(symbol, timeframe="1d", since=since_ms, limit=limit)
