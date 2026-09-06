"""卖出监控系统 - 配置中心。

所有卖出/出场参数统一在此定义，main.py 中不再出现硬编码数值。
通过 get_exit_config("profile_name") 选择不同的配置档。

继承关系：ExitConfig(TradingConfig) —— 与 calc_indicators 的 TradingConfig
保持基底对齐（仅保留 TOTAL_CAPITAL 字段），避免跨服务耦合。
"""

from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass
class TradingConfig:
    """交易配置基类。

    仅保留与卖出/仓位相关的最小公共字段，作为 ExitConfig 的基底。
    不依赖 calc_indicators 的 TradingConfig，保持本服务独立可运行。
    """

    TOTAL_CAPITAL: float = 100_000


@dataclass
class ExitConfig(TradingConfig):
    """卖出/出场专项配置。

    所有参数在开仓时即"锁定"到 Position 实例中，后续 check_exit_signals
    只读这些字段做判定，不再受配置变更影响（除非显式重启策略）。
    """

    # ===== 1. 硬止损（初始） =====
    # 模式: 'fixed_pct' (固定百分比) / 'swing' (前低) / 'atr' (波动率)
    # 当前实现：'fixed_pct' 完整支持；'atr' 需外部传入 ATR 值；'swing' 需传入前低价
    HARD_STOP_MODE: str = 'fixed_pct'
    HARD_STOP_PCT: float = 0.02          # 初始硬止损 2%
    ATR_PERIOD: int = 14
    ATR_STOP_MULT: float = 1.5

    # ===== 2. 保本止损（第一道移动门槛） =====
    # 当浮盈达到初始止损距离的多少倍时，触发保本
    BREAK_EVEN_TRIGGER: float = 1.0
    # 例如：止损-2%，浮盈+2%（1.0 倍）时，止损上移至成本价

    # ===== 3. 分批止盈（网格卖出） =====
    # 是否启用分批止盈
    ENABLE_GRID_EXIT: bool = True

    # 网格层级定义: [ (触发浮盈百分比, 卖出比例), ... ]
    # 注意：卖出比例为"当前剩余仓位"的百分比；最后一层无论比例多少都清仓
    GRID_LEVELS: List[Tuple[float, float]] = field(default_factory=lambda: [
        (0.05, 0.30),   # 浮盈 5% 时，卖出当前仓位的 30%
        (0.10, 0.30),   # 浮盈 10% 时，再卖剩余仓位的 30%
        (0.15, 0.40)    # 浮盈 15% 时，卖出剩余全部（清仓）
    ])

    # 网格卖出后，剩余仓位的止损是否上移
    GRID_TRAILING_STOP: bool = True
    # 上移规则：触发后止损上移到 "触发价 × (1 - BUFFER)"，且不低于保本线
    GRID_TRAILING_BUFFER_PCT: float = 0.01   # 触发价回撤 1%

    # ===== 4. 时间止损 =====
    # 最大持仓时间（单位：根K线，日线即交易日，分钟线即分钟）
    MAX_HOLDING_BARS: int = 20           # 持股最多 20 个交易日

    # 时间止损的附加条件：持仓N天后，若浮盈小于此值，强制离场
    TIME_STOP_MIN_PROFIT_PCT: float = 0.01  # 20天后若盈利<1%，也走

    # ===== 5. 移动止损追踪（趋势保护） =====
    # 当浮盈超过此比例后，启动最高价回撤止损
    TRAILING_ACTIVATE_PCT: float = 0.08   # 浮盈超过8%后激活
    TRAILING_REGRET_PCT: float = 0.03     # 从最高点回撤3%即卖出


@dataclass
class ConservativeExitConfig(ExitConfig):
    """保守型卖出配置：更紧的止损、更小的网格层级、更短的时间止损。"""

    HARD_STOP_PCT: float = 0.015           # 1.5% 硬止损
    GRID_LEVELS: List[Tuple[float, float]] = field(default_factory=lambda: [
        (0.04, 0.40),
        (0.08, 0.30),
        (0.12, 0.30),
    ])
    MAX_HOLDING_BARS: int = 15
    TIME_STOP_MIN_PROFIT_PCT: float = 0.008
    TRAILING_ACTIVATE_PCT: float = 0.06
    TRAILING_REGRET_PCT: float = 0.025


@dataclass
class TrendExitConfig(ExitConfig):
    """趋势型卖出配置：更宽的移动止损、更长的持仓周期、更大的网格层级。"""

    HARD_STOP_PCT: float = 0.025
    GRID_LEVELS: List[Tuple[float, float]] = field(default_factory=lambda: [
        (0.08, 0.25),
        (0.15, 0.25),
        (0.25, 0.50),
    ])
    MAX_HOLDING_BARS: int = 40
    TRAILING_ACTIVATE_PCT: float = 0.10
    TRAILING_REGRET_PCT: float = 0.05


# ── 配置注册表 ──

_CONFIG_REGISTRY: dict = {
    "default": ExitConfig,
    "conservative": ConservativeExitConfig,
    "trend": TrendExitConfig,
}

_PROFILE_DESCRIPTIONS: dict = {
    "default": "标准配置 - 日常使用（硬止损2% / 3层网格5-10-15% / 20天 / 8%激活3%回撤）",
    "conservative": "保守配置 - 紧止损、快止盈、短持仓",
    "trend": "趋势配置 - 宽止损、大网格、长持仓",
}


def get_exit_config(profile: str = "default") -> ExitConfig:
    """按名称获取配置实例。"""
    cls = _CONFIG_REGISTRY.get(profile)
    if cls is None:
        available = ", ".join(_CONFIG_REGISTRY.keys())
        raise ValueError(f"未知配置档 '{profile}'，可用: {available}")
    return cls()


def list_profiles() -> str:
    """返回所有可用配置档的描述文本。"""
    lines = []
    for name, desc in _PROFILE_DESCRIPTIONS.items():
        cls = _CONFIG_REGISTRY[name]
        lines.append(f"  --profile {name:<14s} {desc}  ({cls.__name__})")
    return "\n".join(lines)
