"""stock_character 配置中心（配置驱动，所有可调参数集中于此）。

参考 yanbao-info/config.py 的 dataclass + from_env 模式，以及
alarming_monitor/config.py 的 Section 7（连板龙头）阈值表设计。

所有阈值/限频/路径/席位映射集中于此，main.py 不出现硬编码数值（用户硬偏好）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# 项目根（config.py 的上两级：services/stock_character/config.py
# → services/stock_character → services → trading_lab）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass
class StockCharConfig:
    """股票特性分析服务的全部可调参数。

    分三组：取数限频 / 妖股基因阈值 / 游资席位阈值。
    """

    # ===== 取数限频 =====
    zzshare_token: Optional[str] = None                      # None → 匿名(30次/分钟)
    history_request_interval_seconds: float = 0.5            # 历史接口调用间隔(秒)，防 429
    request_timeout: int = 30                                # 单次请求超时(秒)

    # ===== 历史窗口 =====
    gene_history_days: int = 365                             # 连板基因回溯窗口(近一年)
    gene_recent_days: int = 90                               # 妖股定义窗口(近三个月)
    kline_recent_days: int = 180                             # 日振幅计算窗口(近6个月)

    # ===== 妖股基因阈值 =====
    gene_min_max_boards: int = 2                             # 历史最高连板数下限
    gene_min_consecutive_2plus: int = 1                      # 近期2连板+次数下限
    gene_min_amplitude: float = 5.0                          # 日振幅均值下限(%)
    gene_recent_limitup_days: int = 30                       # "刚启动"窗口：近期有涨停但未过热
    gene_current_hot_threshold: int = 3                      # 当前连板≥此值视为"已过热"

    # 基因评分阈值表（按 key 降序，首个 value >= key 命中，仿 linkban 温度评分）
    gene_max_board_score: dict = field(default_factory=lambda: {
        7: 40, 5: 32, 4: 24, 3: 16, 2: 10, 1: 4, 0: 0,
    })
    gene_consecutive_score: dict = field(default_factory=lambda: {
        5: 30, 3: 22, 2: 16, 1: 8, 0: 0,
    })
    gene_amplitude_score: dict = field(default_factory=lambda: {
        8.0: 30, 6.0: 22, 5.0: 16, 3.0: 8, 0.0: 0,
    })

    # 基因等级映射（综合分 → 等级）
    gene_level_map: dict = field(default_factory=lambda: {
        70: "🔥 妖股基因强（历史高频连板+高波动）",
        50: "🌤️ 妖股基因中（具备一定连板特征）",
        0:  "❄️ 妖股基因弱（连板特征不明显）",
    })

    # ===== 游资席位映射表（可扩展）=====
    institution_seat_name: str = "机构专用"

    # 知名游资 → 常用营业部名称列表（seat_name 完全或包含匹配）
    # 用户可在 config 追加更多；zzshare 的 youzi_icon 字段作为补充信号
    hot_money_seats: dict = field(default_factory=lambda: {
        "炒股养家": [
            "华泰证券股份有限公司深圳益田路证券营业部",
            "方正证券股份有限公司绍兴营业部",
        ],
        "方新侠": [
            "兴业证券股份有限公司陕西分公司",
            "华泰证券股份有限公司上海牡丹江路证券营业部",
        ],
        "赵老哥": [
            "中国银河证券股份有限公司绍兴证券营业部",
            "华泰证券股份有限公司浙江分公司",
        ],
        "作手新一": [
            "国元证券股份有限公司上海中山北路证券营业部",
        ],
        "量化打板": [
            "中信证券股份有限公司上海溧阳路证券营业部",
        ],
    })

    # ===== 次日溢价/核按钮阈值 =====
    premium_threshold: float = 3.0                           # 次日涨幅≥3% → 锁仓信号
    dump_threshold: float = -5.0                            # 次日跌幅≥5% → 一日游风险
    hot_money_min_buy_count: int = 2                       # 游资买入≥N次才判定偏好

    # ===== 输出路径 =====
    csv_dir: str = "data/stock_character"                   # CSV 落盘目录（相对 trading_lab 根）
    reports_dir: str = "data/reports/stock_character"       # Markdown 报告目录

    # ===== 派生路径方法 =====
    def csv_root(self) -> Path:
        """返回 CSV 落盘根目录。"""
        p = Path(str(self.csv_dir))
        return p if p.is_absolute() else _PROJECT_ROOT / p

    def reports_root(self) -> Path:
        """返回 Markdown 报告根目录。"""
        p = Path(str(self.reports_dir))
        return p if p.is_absolute() else _PROJECT_ROOT / p

    def cache_root(self) -> Path:
        """返回 data/cache 根目录（与 alarming_monitor 共享）。"""
        return _PROJECT_ROOT / "data" / "cache"

    @classmethod
    def from_env(cls, **overrides) -> "StockCharConfig":
        """构造默认配置，并允许调用方覆盖任意字段。

        支持环境变量：
            ZZSHARE_TOKEN                          → zzshare_token
            STOCK_CHAR_HISTORY_INTERVAL_SECONDS    → history_request_interval_seconds
        """
        cfg = cls()
        # 环境变量注入
        token = os.environ.get("ZZSHARE_TOKEN") or os.environ.get("ZZSHARE_TOKEN ")
        if token:
            cfg.zzshare_token = token.strip()
        interval_env = os.environ.get("STOCK_CHAR_HISTORY_INTERVAL_SECONDS")
        if interval_env:
            try:
                cfg.history_request_interval_seconds = float(interval_env)
            except ValueError:
                pass
        # 调用方覆盖
        for k, v in overrides.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg


def list_profiles() -> list:
    """返回可用配置档名（目前仅 default，预留扩展）。"""
    return ["default", "conservative", "strict"]


def get_stock_char_config(profile: str = "default", **overrides) -> StockCharConfig:
    """按配置档构造配置。

    - default: 默认阈值
    - conservative: 更严阈值（max_boards≥3, amplitude≥6%）
    - strict: 最严阈值（max_boards≥4, amplitude≥8%）
    """
    if profile == "conservative":
        overrides.setdefault("gene_min_max_boards", 3)
        overrides.setdefault("gene_min_amplitude", 6.0)
        overrides.setdefault("hot_money_min_buy_count", 3)
    elif profile == "strict":
        overrides.setdefault("gene_min_max_boards", 4)
        overrides.setdefault("gene_min_amplitude", 8.0)
        overrides.setdefault("hot_money_min_buy_count", 3)
        overrides.setdefault("premium_threshold", 5.0)
    return StockCharConfig.from_env(**overrides)
