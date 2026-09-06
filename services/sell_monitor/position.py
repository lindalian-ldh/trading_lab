"""持仓对象模型。

进场即锁定全部卖出规则：开仓成交的那一刻，硬止损 / 保本线 / 网格层级全部
固化到 Position 实例中。后续 check_exit_signals 只读取这些字段，不再受
配置文件变更影响（实现"进场即锁死"的理念）。

Position 既负责运行期状态，也负责序列化（to_dict / from_dict）用于 SQLite 持久化。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from config import ExitConfig


@dataclass
class Position:
    """持仓实例：开仓时创建，全部卖出后归档。

    所有"卖出规则锁定"字段在 Position.create() 时根据 config 计算；
    所有"运行期追踪"字段在 check_exit_signals() 中持续更新。
    """

    # ---- 基础信息 ----
    symbol: str
    entry_price: float                # 开仓均价
    shares: int                       # 初始总股数
    entry_date: str                   # YYYY-MM-DD（与 --date 对齐）

    # ---- 卖出规则锁定（进场即固化） ----
    hard_stop_price: float            # 初始硬止损价（永不下降）
    stop_loss_price: float            # 当前有效止损价（会随保本/网格上移，只升不降）
    break_even_price: float           # 保本线（= entry_price）

    # ---- 网格卖出状态 ----
    grid_level_index: int = 0         # 下一个待触发的网格层级索引
    sold_shares: int = 0              # 累计已卖出股数

    # ---- 追踪数据 ----
    highest_price_since_entry: float = 0.0   # 持仓以来最高价
    bars_held: int = 0                       # 已持有K线根数（不含开仓当日）

    # ---- 状态 ----
    status: str = "open"              # 'open' / 'closed'
    closed_reason: Optional[str] = None
    closed_price: Optional[float] = None
    closed_date: Optional[str] = None

    # ---- 元数据 ----
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec='seconds'))

    # ------------------------------------------------------------------
    # 构造与状态查询
    # ------------------------------------------------------------------

    @classmethod
    def create(cls, symbol: str, entry_price: float, shares: int,
               entry_date: str, config: ExitConfig) -> "Position":
        """开仓时创建持仓实例，自动锁定全部卖出规则。

        根据 config.HARD_STOP_MODE 计算初始硬止损价：
        - 'fixed_pct': entry_price * (1 - HARD_STOP_PCT)
        - 'atr'      : entry_price - ATR_STOP_MULT * atr（需调用方先算好 atr 传入，
                       通过 create_with_atr 类方法）
        - 'swing'    : 取开仓前的最近前低（需调用方传入，通过 create_with_swing 类方法）

        Args:
            symbol: 股票代码
            entry_price: 开仓均价
            shares: 总股数
            entry_date: 开仓日期 YYYY-MM-DD
            config: ExitConfig 实例

        Returns:
            Position 实例（已固化全部卖出规则）
        """
        if config.HARD_STOP_MODE != 'fixed_pct':
            raise ValueError(
                f"HARD_STOP_MODE='{config.HARD_STOP_MODE}' 需使用 create_with_atr / create_with_swing，"
                f"当前仅支持 'fixed_pct'"
            )
        hard_stop = entry_price * (1 - config.HARD_STOP_PCT)
        return cls(
            symbol=symbol,
            entry_price=entry_price,
            shares=shares,
            entry_date=entry_date,
            hard_stop_price=round(hard_stop, 4),
            stop_loss_price=round(hard_stop, 4),
            break_even_price=round(entry_price, 4),
            highest_price_since_entry=round(entry_price, 4),
        )

    @classmethod
    def create_with_atr(cls, symbol: str, entry_price: float, shares: int,
                        entry_date: str, config: ExitConfig, atr: float) -> "Position":
        """使用 ATR 波动率计算硬止损的工厂方法。"""
        hard_stop = entry_price - config.ATR_STOP_MULT * atr
        pos = cls(
            symbol=symbol,
            entry_price=entry_price,
            shares=shares,
            entry_date=entry_date,
            hard_stop_price=round(hard_stop, 4),
            stop_loss_price=round(hard_stop, 4),
            break_even_price=round(entry_price, 4),
            highest_price_since_entry=round(entry_price, 4),
        )
        return pos

    @classmethod
    def create_with_swing(cls, symbol: str, entry_price: float, shares: int,
                          entry_date: str, config: ExitConfig, swing_low: float) -> "Position":
        """使用结构前低作为硬止损的工厂方法。"""
        # 前低不得高于开仓价（否则没意义），且至少留 HARD_STOP_PCT 的空间
        floor = entry_price * (1 - config.HARD_STOP_PCT)
        hard_stop = min(swing_low, floor)
        pos = cls(
            symbol=symbol,
            entry_price=entry_price,
            shares=shares,
            entry_date=entry_date,
            hard_stop_price=round(hard_stop, 4),
            stop_loss_price=round(hard_stop, 4),
            break_even_price=round(entry_price, 4),
            highest_price_since_entry=round(entry_price, 4),
        )
        return pos

    @property
    def remaining_shares(self) -> int:
        """当前剩余持仓股数。"""
        return max(0, self.shares - self.sold_shares)

    @property
    def is_closed(self) -> bool:
        """持仓是否已关闭（status='closed' 或剩余股数为 0）。"""
        return self.status == "closed" or self.remaining_shares == 0

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            'symbol': self.symbol,
            'entry_price': self.entry_price,
            'shares': self.shares,
            'entry_date': self.entry_date,
            'hard_stop_price': self.hard_stop_price,
            'stop_loss_price': self.stop_loss_price,
            'break_even_price': self.break_even_price,
            'grid_level_index': self.grid_level_index,
            'sold_shares': self.sold_shares,
            'highest_price_since_entry': self.highest_price_since_entry,
            'bars_held': self.bars_held,
            'status': self.status,
            'closed_reason': self.closed_reason,
            'closed_price': self.closed_price,
            'closed_date': self.closed_date,
            'created_at': self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        """从字典（SQLite Row 或 JSON）重建 Position。"""
        return cls(
            symbol=d['symbol'],
            entry_price=float(d['entry_price']),
            shares=int(d['shares']),
            entry_date=d['entry_date'],
            hard_stop_price=float(d['hard_stop_price']),
            stop_loss_price=float(d['stop_loss_price']),
            break_even_price=float(d['break_even_price']),
            grid_level_index=int(d['grid_level_index']),
            sold_shares=int(d['sold_shares']),
            highest_price_since_entry=float(d['highest_price_since_entry']),
            bars_held=int(d['bars_held']),
            status=d['status'],
            closed_reason=d.get('closed_reason'),
            closed_price=float(d['closed_price']) if d.get('closed_price') is not None else None,
            closed_date=d.get('closed_date'),
            created_at=d.get('created_at') or datetime.now().isoformat(timespec='seconds'),
        )
