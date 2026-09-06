"""控制台状态输出：每日收盘后打印持仓快照。

参考 5.1 控制台状态输出格式，但把 emoji 替换为 [√]/[x] 风格的纯文字标记，
避免在终端遇到 "Glyph missing" 警告（与 chart.py 一致）。
"""

from __future__ import annotations

import pandas as pd

from config import ExitConfig
from position import Position


def print_position_status(position: Position, df: pd.DataFrame,
                          config: ExitConfig,
                          signal: dict | None = None) -> None:
    """打印持仓状态卡片（参考 5.1 控制台格式）。

    Args:
        position: 当前持仓实例（已被 check_exit_signals 更新过）
        df: K线 DataFrame，最后一行为最新K线
        config: ExitConfig 实例
        signal: check_exit_signals 返回的 dict（可选，用于打印"建议"行）
    """
    if df is not None and len(df) > 0:
        cur_price = float(df.iloc[-1]['close'])
    else:
        cur_price = 0.0

    profit_pct = ((cur_price - position.entry_price) / position.entry_price
                  if position.entry_price else 0.0)

    bars_total = config.MAX_HOLDING_BARS
    bars_left = max(0, bars_total - position.bars_held)

    print()
    print(f"📦 持仓状态 [{position.symbol}] "
          f"第 {position.bars_held}/{bars_total} 个交易日")
    print("━" * 56)
    print(f"  开仓价: {position.entry_price:.2f} | 现价: {cur_price:.2f} | "
          f"浮盈: {profit_pct * 100:+.1f}%")

    hard_triggered = "已触发" if cur_price <= position.hard_stop_price else "未触发"
    stop_moved = ("已上移" if position.stop_loss_price > position.hard_stop_price + 1e-9
                  else "初始")
    print(f"  硬止损: {position.hard_stop_price:.2f} ({hard_triggered}) | "
          f"当前止损: {position.stop_loss_price:.2f} ({stop_moved})")
    print("━" * 56)

    # ── 网格卖出记录 ──
    if config.ENABLE_GRID_EXIT:
        print("📌 网格卖出记录:")
        for i, (lvl_pct, sell_ratio) in enumerate(config.GRID_LEVELS):
            trigger_price = position.entry_price * (1 + lvl_pct)
            if i < position.grid_level_index:
                # 已触发
                print(f"  [√] 第{i + 1}层 (+{lvl_pct * 100:.0f}%): "
                      f"已触发 @ {trigger_price:.2f}")
            else:
                print(f"  [ ] 第{i + 1}层 (+{lvl_pct * 100:.0f}%): "
                      f"待触发 @ {trigger_price:.2f} (卖 {sell_ratio * 100:.0f}%)")
        sold_pct = (position.sold_shares / position.shares * 100
                    if position.shares else 0.0)
        print(f"  累计卖出: {position.sold_shares}/{position.shares} 股 "
              f"({sold_pct:.1f}%)，剩余 {position.remaining_shares} 股")
        print("━" * 56)

    # ── 时间止损 ──
    time_status = "⚠️ 已超时" if bars_left == 0 else f"剩余 {bars_left} 个交易日"
    print(f"⏰ 时间止损: {time_status} (上限 {bars_total} 根)")

    # ── 移动回撤 ──
    if (profit_pct >= config.TRAILING_ACTIVATE_PCT
            and position.highest_price_since_entry > 0):
        regret = ((position.highest_price_since_entry - cur_price)
                  / position.highest_price_since_entry)
        safe = "安全" if regret < config.TRAILING_REGRET_PCT else "触发"
        print(f"📐 移动回撤: 最高 {position.highest_price_since_entry:.2f}，"
              f"回撤 {regret * 100:.1f}% < {config.TRAILING_REGRET_PCT * 100:.0f}% "
              f"({safe})")
    else:
        print(f"📐 移动回撤: 未激活 (需浮盈 ≥ {config.TRAILING_ACTIVATE_PCT * 100:.0f}%)")

    # ── 当前信号 ──
    if signal is not None:
        print("━" * 56)
        print(f"💡 建议: {signal['message']}")
    print()


def print_position_brief(position: Position) -> None:
    """打印持仓简要信息（用于 --list 模式）。"""
    cur_stop = position.stop_loss_price
    print(f"  {position.symbol} | 开仓 {position.entry_date} "
          f"@ {position.entry_price:.2f} | "
          f"持有 {position.remaining_shares}/{position.shares} 股 | "
          f"已持有 {position.bars_held} 根 | 止损 {cur_stop:.2f} | "
          f"最高 {position.highest_price_since_entry:.2f}")


def print_closed_brief(position: Position) -> None:
    """打印已关闭持仓简要信息（用于复盘）。"""
    closed_px = position.closed_price if position.closed_price else 0.0
    print(f"  {position.symbol} | 开仓 {position.entry_date} "
          f"@ {position.entry_price:.2f} → 平仓 {position.closed_date} "
          f"@ {closed_px:.2f} | "
          f"原因: {position.closed_reason or '-'}")
