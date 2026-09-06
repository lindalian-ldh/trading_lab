"""全局配置：基于 Pydantic BaseSettings，从 .env 读取。

所有路径以项目根目录（pyproject.toml 所在层）为基准派生，
避免硬编码绝对路径，便于迁移。
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（config/ 的上一层）
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """应用配置，字段大小写不敏感，自动从 .env 注入。"""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- AI 配置 ----
    ai_enabled: bool = False
    ai_api_key: str = ""
    ai_api_url: str = "https://api.example.com/v1/analyze"

    # ---- 交易所 ----
    exchange: str = "binance"
    default_symbol: str = "BTC/USDT"

    # ---- 宏观数据 FRED ----
    # FRED API key（免费申请：https://fred.stlouisfed.org），pandas-datareader 获取美国宏观指标所需
    fred_api_key: str = ""

    # ---- 板块扫描 zzshare ----
    # zzshare API token（免费申请：https://quant.zizizaizai.com/me/profile），板块排名与成分股数据所需
    zzshare_token: str = ""

    # ---- 日志 ----
    log_level: str = "INFO"

    # ---- 派生路径 ----
    @property
    def data_dir(self) -> Path:
        return PROJECT_ROOT / "data"

    @property
    def klines_dir(self) -> Path:
        return self.data_dir / "raw" / "klines"

    @property
    def html_cache_dir(self) -> Path:
        return self.data_dir / "html_cache"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "trading.db"

    @property
    def logs_dir(self) -> Path:
        return PROJECT_ROOT / "logs"

    # ---- 财经新闻路径 ----
    @property
    def news_raw_dir(self) -> Path:
        return self.data_dir / "raw" / "news"

    @property
    def news_reports_dir(self) -> Path:
        return self.data_dir / "reports" / "news"

    @property
    def stock_mapping_path(self) -> Path:
        return self.data_dir / "stock_mapping.json"


# 全局单例
settings = Settings()
