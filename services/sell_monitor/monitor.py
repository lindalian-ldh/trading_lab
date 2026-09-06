"""核心卖出信号判定引擎。

check_exit_signals 是唯一对外接口：输入持仓 + 最新行情 + 配置，输出卖出指令。

判定优先级（高 → 低，先触发的先返回）：
    1. 硬止损触发        → sell_all （保命线，不可越过）
    2. 当前止损价被跌破  → sell_all （保本/网格上移后的止损被破）
    3. 移动回撤止损      → sell_all （趋势保护：从最高点回撤超阈值）
    4. 时间止损          → sell_all （超时未达预期）
    5. 网格分批止盈      → sell_partial / sell_all （阶梯抬升，支持跳层）
    6. 保本止损上移      → hold（仅更新 stop_loss_price，无卖出）
    其余：hold

网格跳层处理：若一根K线内浮盈一次性穿过多个网格阈值（如从 +4% 直接跳到 +15%），
则按"跳到最高可触发的层级"处理：
    - 若最高可触发的层级是最后一层 → 清仓（sell_all）
    - 否则按"累计剩余比例"计算卖出股数（sell_partial）

Position 实例会被原地修改：highest_price_since_entry / bars_held /
stop_loss_price / grid_level_index / sold_shares 等字段会更新。
调用方应在 check_exit_signals 返回后 save_position 持久化。
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from config import ExitConfig
from position import Position

logger = logging.getLogger(__name__)

# 浮点阈值比较容差：避免 (10.20-10.00)/10.00 = 0.01999... < 0.02 这类精度问题
EPSILON = 1e-9


# ====================================================================
# 公共接口
# ====================================================================

def check_exit_signals(position: Position, df: pd.DataFrame,
                       config: ExitConfig) -> dict:
    """输入当前持仓和最新行情，输出卖出指令。

    Args:
        position: 持仓实例（会被原地 modify）
        df: K线 DataFrame，需包含 'close' / 'high' / 'low' 列；
            若有 'date' 列（YYYY-MM-DD 字符串）则用于计算 bars_held；
            最后一行为最新K线
        config: ExitConfig 实例

    Returns:
        {
            'action': 'hold' | 'sell_partial' | 'sell_all',
            'reason': str,              # 触发原因（用于日志/CSV）
            'sell_price': float,        # 建议卖出价
            'sell_shares': int,         # 建议卖出股数
            'new_stop_price': float,    # 更新后的止损价
            'message': str,             # 给交易员看的提示（含 emoji）
        }
    """
    # ---- 边界情况 ----
    if position.is_closed:
        return _result('hold', '持仓已关闭', 0.0, 0,
                       position.stop_loss_price, '无操作（持仓已关闭）')

    if df is None or len(df) == 0:
        return _result('hold', '无行情数据', 0.0, 0,
                       position.stop_loss_price, '⚠️ 无行情数据，无法判定')

    # ---- 取最新K线 ----
    last_row = df.iloc[-1]
    cur_price = float(last_row['close'])
    cur_high = float(last_row['high'])
    cur_low = float(last_row['low'])

    # ---- 更新追踪数据 ----
    _update_bars_held(position, df)
    if cur_high > position.highest_price_since_entry:
        position.highest_price_since_entry = round(cur_high, 4)

    # ---- 计算浮盈比例 ----
    profit_pct = (cur_price - position.entry_price) / position.entry_price

    logger.info(
        "[%s] cur=%.2f high=%.2f low=%.2f profit=%+.2f%% bars=%d highest=%.2f stop=%.2f grid_idx=%d",
        position.symbol, cur_price, cur_high, cur_low,
        profit_pct * 100, position.bars_held,
        position.highest_price_since_entry, position.stop_loss_price,
        position.grid_level_index,
    )

    # ====================================================================
    # 优先级 1: 硬止损触发（保命线）
    # ====================================================================
    if cur_low <= position.hard_stop_price:
        sell_price = position.hard_stop_price
        msg = (f"⚠️ 硬止损触发：当日最低 {cur_low:.2f} ≤ 硬止损 "
               f"{position.hard_stop_price:.2f}，立即清仓保命")
        return _finalize_sell_all(position, sell_price, '硬止损触发', msg)

    # ====================================================================
    # 优先级 2: 当前止损价被跌破（保本/网格上移后的止损）
    # ====================================================================
    # 只有当 stop_loss_price 高于初始 hard_stop_price 时才检查
    # （否则与优先级 1 重复）
    if position.stop_loss_price > position.hard_stop_price + 1e-9:
        if cur_low <= position.stop_loss_price:
            sell_price = position.stop_loss_price
            msg = (f"⚠️ 移动止损被破：当日最低 {cur_low:.2f} ≤ 止损 "
                   f"{position.stop_loss_price:.2f}，清仓锁定利润/保本")
            return _finalize_sell_all(position, sell_price, '移动止损被破', msg)

    # ====================================================================
    # 优先级 3: 移动回撤止损（趋势保护）
    # ====================================================================
    if profit_pct >= config.TRAILING_ACTIVATE_PCT - EPSILON:
        regret = ((position.highest_price_since_entry - cur_price)
                  / position.highest_price_since_entry)
        if regret >= config.TRAILING_REGRET_PCT - EPSILON:
            # 卖出价 = 最高价 × (1 - 回撤阈值)，模拟挂在最高价回撤 X% 的止损单
            sell_price = round(
                position.highest_price_since_entry * (1 - config.TRAILING_REGRET_PCT), 4
            )
            msg = (f"📐 移动回撤止损：最高 {position.highest_price_since_entry:.2f} "
                   f"→ 现价 {cur_price:.2f}，回撤 {regret * 100:.1f}% ≥ "
                   f"{config.TRAILING_REGRET_PCT * 100:.0f}%，清仓保护利润")
            return _finalize_sell_all(position, sell_price, '移动回撤止损', msg)

    # ====================================================================
    # 优先级 4: 时间止损（效率线）
    # ====================================================================
    if position.bars_held >= config.MAX_HOLDING_BARS:
        if profit_pct < config.TIME_STOP_MIN_PROFIT_PCT - EPSILON:
            sell_price = cur_price
            msg = (f"⏰ 时间止损：已持仓 {position.bars_held} 根 ≥ "
                   f"{config.MAX_HOLDING_BARS}，浮盈 {profit_pct * 100:.2f}% < "
                   f"{config.TIME_STOP_MIN_PROFIT_PCT * 100:.0f}%，强制离场")
            return _finalize_sell_all(position, sell_price, '时间止损', msg)

    # ====================================================================
    # 优先级 5: 网格分批止盈（阶梯抬升）
    # ====================================================================
    grid_signal = _check_grid_exit(position, profit_pct, config)
    if grid_signal is not None:
        return grid_signal

    # ====================================================================
    # 优先级 6: 保本止损上移（仅更新止损，无卖出）
    # ====================================================================
    break_even_target_pct = config.HARD_STOP_PCT * config.BREAK_EVEN_TRIGGER
    if (profit_pct >= break_even_target_pct - EPSILON
            and position.stop_loss_price < position.break_even_price - EPSILON):
        position.stop_loss_price = position.break_even_price
        msg = (f"🛡️ 保本止损上移：浮盈 {profit_pct * 100:.1f}% ≥ "
               f"{break_even_target_pct * 100:.0f}%（止损距离的 "
               f"{config.BREAK_EVEN_TRIGGER:.1f} 倍），止损从 "
               f"{position.hard_stop_price:.2f} 上移至成本价 "
               f"{position.break_even_price:.2f}")
        return _result('hold', '保本止损上移', 0.0, 0,
                       position.stop_loss_price, msg)

    # ====================================================================
    # 默认：继续持有
    # ====================================================================
    regret_str = ''
    if profit_pct >= config.TRAILING_ACTIVATE_PCT - EPSILON:
        regret = ((position.highest_price_since_entry - cur_price)
                  / position.highest_price_since_entry)
        regret_str = (f" | 回撤 {regret * 100:.1f}% < "
                      f"{config.TRAILING_REGRET_PCT * 100:.0f}% (安全)")

    bars_left = max(0, config.MAX_HOLDING_BARS - position.bars_held)
    msg = (f"💡 继续持有：浮盈 {profit_pct * 100:+.2f}%，止损 "
           f"{position.stop_loss_price:.2f}，剩余 {position.remaining_shares} 股，"
           f"时间止损剩余 {bars_left} 根{regret_str}")
    return _result('hold', '继续持有', 0.0, 0,
                   position.stop_loss_price, msg)


# ====================================================================
# 网格卖出子模块
# ====================================================================

def _check_grid_exit(position: Position, profit_pct: float,
                     config: ExitConfig) -> Optional[dict]:
    """检查网格分批止盈。若触发则返回信号 dict，否则返回 None。

    核心逻辑：
        - 若 profit_pct ≥ 下一层阈值 → 至少触发一层
        - 向上扫描所有可触发的层级，取最高可触发的层级 highest_triggered
        - 若 highest_triggered 是最后一层 → 清仓（sell_all）
        - 否则按"累计剩余比例"计算中间层卖出股数（sell_partial）
        - 卖出后：grid_level_index 跳到 highest_triggered + 1
        - 若 GRID_TRAILING_STOP=True 且非清仓：止损上移到
          "最高触发价 × (1 - GRID_TRAILING_BUFFER_PCT)"，且不低于保本线
    """
    if not config.ENABLE_GRID_EXIT:
        return None
    if position.grid_level_index >= len(config.GRID_LEVELS):
        return None  # 所有层级已触发完

    # 下一层阈值
    next_level_pct, _ = config.GRID_LEVELS[position.grid_level_index]
    if profit_pct < next_level_pct - EPSILON:
        return None  # 未达下一层触发条件

    # 向上扫描所有可触发的层级（GRID_LEVELS 按 pct 升序）
    highest_triggered = position.grid_level_index
    for i in range(position.grid_level_index + 1, len(config.GRID_LEVELS)):
        level_pct, _ = config.GRID_LEVELS[i]
        if profit_pct >= level_pct - EPSILON:
            highest_triggered = i
        else:
            break

    # ---- 判断是否清仓（最后一层无论 ratio 多少都清仓） ----
    is_last_level = (highest_triggered == len(config.GRID_LEVELS) - 1)

    # 最高触发层对应的触发价
    trigger_level_pct = config.GRID_LEVELS[highest_triggered][0]
    trigger_price = round(position.entry_price * (1 + trigger_level_pct), 4)

    if is_last_level:
        # 最后一层：清仓剩余全部
        sell_shares = position.remaining_shares
        action = 'sell_all'
        layers_desc = (f"第{position.grid_level_index + 1}"
                       f"~{highest_triggered + 1}层" if highest_triggered > position.grid_level_index
                       else f"第{highest_triggered + 1}层")
        msg_tail = f"{layers_desc} (+{trigger_level_pct * 100:.0f}%) 触发，清仓离场"
        new_stop = position.stop_loss_price  # 清仓后止损价无意义
    else:
        # 中间层：累计多层的"剩余比例"计算卖出股数
        remaining_ratio = 1.0
        for i in range(position.grid_level_index, highest_triggered + 1):
            _, r = config.GRID_LEVELS[i]
            remaining_ratio *= (1.0 - r)
        sell_shares = int(position.remaining_shares * (1.0 - remaining_ratio))
        # 兜底：至少卖 1 股
        if sell_shares < 1:
            sell_shares = 1
        action = 'sell_partial'
        layers_desc = (f"第{position.grid_level_index + 1}"
                       f"~{highest_triggered + 1}层" if highest_triggered > position.grid_level_index
                       else f"第{highest_triggered + 1}层")
        msg_tail = (f"{layers_desc} (+{trigger_level_pct * 100:.0f}%) 触发，"
                    f"卖出 {sell_shares} 股")

        # 网格卖出后上移止损
        new_stop = position.stop_loss_price
        if config.GRID_TRAILING_STOP:
            candidate = trigger_price * (1 - config.GRID_TRAILING_BUFFER_PCT)
            # 只升不降，且不低于保本线
            new_stop = max(candidate, position.break_even_price, position.stop_loss_price)
            new_stop = round(new_stop, 4)

    # ---- 更新 Position 状态 ----
    position.sold_shares += sell_shares
    position.grid_level_index = highest_triggered + 1
    if not is_last_level:
        position.stop_loss_price = new_stop

    remaining = position.remaining_shares
    stop_info = (f"，止损上移至 {position.stop_loss_price:.2f}" if not is_last_level
                 else "")
    msg = (f"✅ 网格止盈：{msg_tail}；剩余 {remaining} 股{stop_info}")

    return _result(
        action, f'网格止盈(第{highest_triggered + 1}层)',
        trigger_price, sell_shares, position.stop_loss_price, msg,
    )


# ====================================================================
# 工具函数
# ====================================================================

def _update_bars_held(position: Position, df: pd.DataFrame) -> None:
    """根据 df 更新 position.bars_held（开仓日次日起的K线根数，不含开仓当日）。

    若 df 无 'date' 列，则用 max(bars_held, len(df)-1) 兜底。
    bars_held 只升不降（防止数据回填导致倒退）。
    """
    if 'date' not in df.columns:
        position.bars_held = max(position.bars_held, max(0, len(df) - 1))
        return

    entry = position.entry_date
    # date 列可能是字符串或 datetime，统一转字符串比较
    date_strs = df['date'].astype(str).str[:10]
    after_entry = (date_strs > entry).sum()
    position.bars_held = max(position.bars_held, int(after_entry))


def _result(action: str, reason: str, sell_price: float, sell_shares: int,
            new_stop_price: float, message: str) -> dict:
    """构造标准化返回 dict。"""
    return {
        'action': action,
        'reason': reason,
        'sell_price': float(sell_price),
        'sell_shares': int(sell_shares),
        'new_stop_price': float(new_stop_price),
        'message': message,
    }


def _finalize_sell_all(position: Position, sell_price: float,
                       reason: str, message: str) -> dict:
    """清仓并返回 sell_all 信号。

    会更新 position.sold_shares = position.shares（全部卖出），
    position.stop_loss_price = sell_price（无意义但保持一致性）。
    调用方负责把 position.status 改为 'closed' 并填 closed_* 字段。
    """
    position.sold_shares = position.shares
    position.stop_loss_price = round(sell_price, 4)
    return _result('sell_all', reason, sell_price, position.shares,
                   sell_price, message)
