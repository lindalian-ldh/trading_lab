"""yanbao-info 配置中心（配置驱动，所有可调参数集中于此）。

模块命名为 ``config``，但因为本服务作为子目录被独立运行（``uv run services/yanbao-info/main.py``），
sys.path 优先注入服务目录，再注入项目根，因此 ``from config import YanbaoConfig`` 解析到本模块，
``from config.settings import settings`` 解析到项目根的 ``config`` 包，二者通过路径注入顺序隔离。

参考 bankuai-service/config.py 与 crawler_news/news_config.py 的设计约定。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# 项目根（config.py 的上两级：services/yanbao-info/config.py → services/yanbao-info → services → trading_lab）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass
class YanbaoConfig:
    """券商研报摘要服务的全部可调参数。

    所有阈值/URL/UA/超时/路径集中于此，main.py 不出现硬编码数值（用户硬偏好）。
    """

    # ===== 取数 =====
    top_n: int = 1                    # 取最近 N 份研报（默认 1）
    request_interval: float = 2.0     # 限频间隔（秒），相邻请求间隔 ≥ 此值
    request_timeout: int = 30         # PDF 下载与单次接口请求超时（秒，PDF 较大）
    retry_times: int = 2              # 失败重试次数（不含首次；仅网络异常重试）

    # ===== PDF 解析 =====
    pdf_max_pages: int = 50           # 最多解析页数（防超长报告耗时）
    pdf_text_min_chars: int = 100     # 文本字符数 < 此值视为扫描版 PDF

    # ===== PDF 下载请求头（防东方财富防盗链）=====
    pdf_user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    pdf_referer: str = "https://data.eastmoney.com/"

    # ===== 短线信号阈值（对齐计划"短线博弈信号"定义）=====
    target_price_upside_strong: float = 0.30    # 目标价隐含涨幅 ≥30% 视为强信号
    target_price_upside_moderate: float = 0.15  # 15%-30% 视为中信号，<15% 视为弱信号
    catalyst_recent_days: int = 30               # 催化剂事件窗口期（天）
    forecast_revision_threshold: float = 0.05   # 研报预测值高于一致预期 5% 视为上调
    history_keep_count: int = 20                 # 历史快照保留份数（超出自动淘汰最旧）

    # ===== 评级词汇（用于提取与跳升判断；可由用户追加）=====
    # 顺序即优先级：若文本同时出现"买入"和"增持"，取靠前的列表
    rating_buy: list = field(default_factory=lambda: [
        "买入", "强推", "强烈推荐", "Strong Buy", "Buy",
    ])
    rating_outperform: list = field(default_factory=lambda: [
        "增持", "推荐", "优于大市", "Outperform", "Add",
    ])
    rating_neutral: list = field(default_factory=lambda: [
        "中性", "持有", "同步大市", "Neutral", "Hold",
    ])
    rating_underperform: list = field(default_factory=lambda: [
        "减持", "Underperform", "Reduce",
    ])
    rating_sell: list = field(default_factory=lambda: [
        "卖出", "回避", "Sell",
    ])

    # ===== 一致预期接口参数（ak.stock_profit_forecast_ths）=====
    consensus_indicator: str = "预测年报净利润"

    # ===== 数据源 URL（便于迁移）=====
    tencent_quote_url: str = "https://qt.gtimg.cn/q={symbol}"

    # ===== 派生路径（不序列化）=====
    cache_dir: Optional[Path] = None   # None 时由 data_loader 决定（默认 data/yanbao/）

    @classmethod
    def from_env(cls, **overrides) -> "YanbaoConfig":
        """构造默认配置，并允许调用方覆盖任意字段。

        目前无环境变量配置项（所有参数都有合理默认值）；保留 ``from_env`` 接口
        以便后续扩展（如 ``YANBAO_TOP_N`` / ``YANBAO_PDF_MAX_PAGES`` 等）。
        """
        cfg = cls()
        for k, v in overrides.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg

    # ===== 路径派生方法（参考 bankuai-service data_loader._cache_root 模式）=====
    def yanbao_root(self) -> Path:
        """返回 data/yanbao/ 根目录（不存在由调用方 mkdir）。"""
        if self.cache_dir is not None:
            return Path(self.cache_dir)
        return _PROJECT_ROOT / "data" / "yanbao"

    def pdfs_dir(self) -> Path:
        return self.yanbao_root() / "pdfs"

    def cache_dir_path(self) -> Path:
        return self.yanbao_root() / "cache"

    def history_dir(self) -> Path:
        return self.yanbao_root() / "history"

    def reports_dir(self) -> Path:
        return self.yanbao_root() / "reports"


# 便捷单例（默认配置）
default_config = YanbaoConfig()
