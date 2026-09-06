#!/usr/bin/env python3
"""卖出监控系统入口。

支持四种模式（互斥）：
    1. --init     登记新持仓（开仓即锁定全部卖出规则 + 生成退出路线图）
    2. (默认)     监控现有持仓，输出今日卖出信号（hold/sell_partial/sell_all）
    3. --list     列出所有未关闭持仓
    4. --close    强制关闭某持仓（手动平仓）

约束：
    - 纯信号系统：不直接下单，只输出 action 和 sell_price，由交易员手动确认
    - 网格卖出比例基于"当前剩余仓位"而非初始仓位（避免清仓过早）
    - 每次触发卖出信号都会记录到 CSV 便于复盘

用法示例：
    # 1. 开仓：登记 600550 在 2026-08-12 开仓 1000 股 @ 10.00
    uv run services/sell_monitor/main.py \\
        --symbol 600550 --date 2026-08-12 \\
        --init --entry-price 10.00 --shares 1000

    # 2. 监控：检查 600550 截至 2026-08-12 的卖出信号
    uv run services/sell_monitor/main.py --symbol 600550 --date 2026-08-12

    # 3. 列出所有未关闭持仓
    uv run services/sell_monitor/main.py --list

    # 4. 强制关闭
    uv run services/sell_monitor/main.py --symbol 600550 --close

    # 5. 使用趋势型配置开仓
    uv run services/sell_monitor/main.py \\
        --symbol 600550 --date 2026-08-12 \\
        --init --entry-price 10.00 --shares 1000 --profile trend
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from config import get_exit_config, list_profiles, ExitConfig
from data_loader import fetch_klines
from monitor import check_exit_signals
from position import Position
from reporter import print_position_brief, print_position_status
from storage import (
    init_db, load_all_open_positions, load_position,
    log_sell_signal, save_position,
)

logger = logging.getLogger(__name__)


# ====================================================================
# 参数解析
# ====================================================================

def _parse_args(argv):
    p = argparse.ArgumentParser(
        description="动态止盈止损与网格卖出监控系统（卖出执行模块）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  开仓:  %(prog)s --symbol 600550 --date 2026-08-12 --init --entry-price 10.00 --shares 1000
  监控:  %(prog)s --symbol 600550 --date 2026-08-12
  列表:  %(prog)s --list
  平仓:  %(prog)s --symbol 600550 --close
""",
    )

    p.add_argument(
        "--date", default=(date.today() - timedelta(days=1)).isoformat(),
        help="参考日期 YYYY-MM-DD，默认昨天。开仓时作为 entry_date，监控时作为 ref_date",
    )
    p.add_argument("--symbol", help="股票代码（6位数字）")
    p.add_argument(
        "--profile", default="default",
        help="卖出配置档: default / conservative / trend",
    )

    # 动作互斥组
    g = p.add_mutually_exclusive_group()
    g.add_argument("--init", action="store_true",
                   help="登记新持仓（开仓即锁定全部卖出规则 + 生成退出路线图）")
    g.add_argument("--list", action="store_true",
                   help="列出所有未关闭持仓")
    g.add_argument("--close", action="store_true",
                   help="强制关闭持仓（手动平仓）")

    # 开仓参数
    p.add_argument("--entry-price", type=float, help="开仓均价（--init 时必填）")
    p.add_argument("--shares", type=int, help="开仓股数（--init 时必填）")

    # 其他
    p.add_argument("--no-chart", action="store_true",
                   help="开仓时不生成退出路线图")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="详细日志（INFO 级别）")

    return p.parse_args(argv)


# ====================================================================
# 子命令实现
# ====================================================================

def _init_position(symbol: str, entry_price: float, shares: int,
                   entry_date: str, config: ExitConfig,
                   no_chart: bool) -> int:
    """登记新持仓。"""
    # ---- 输入验证 ----
    if entry_price <= 0:
        print(f"❌ 开仓价必须为正数，当前: {entry_price}")
        return 1
    if shares <= 0:
        print(f"❌ 开仓股数必须为正整数，当前: {shares}")
        return 1

    existing = load_position(symbol)
    if existing is not None:
        print(f"⚠️  {symbol} 已有未关闭持仓（开仓日 {existing.entry_date}），")
        print(f"   请先 --close 后再开新仓。")
        return 1

    pos = Position.create(symbol, entry_price, shares, entry_date, config)
    save_position(pos)

    print("=" * 60)
    print(f"✅ 已登记持仓: {symbol} 开仓价 {entry_price:.2f} 股数 {shares}")
    print("=" * 60)
    print(f"  配置档: {type(config).__name__}")
    print(f"  硬止损锁定: {pos.hard_stop_price:.2f} "
          f"(-{config.HARD_STOP_PCT * 100:.1f}%)")
    print(f"  保本线: {pos.break_even_price:.2f}")
    if config.ENABLE_GRID_EXIT:
        print(f"  网格层级: {len(config.GRID_LEVELS)} 层")
        for i, (pct, r) in enumerate(config.GRID_LEVELS):
            is_last = (i == len(config.GRID_LEVELS) - 1)
            label = "清仓" if is_last else f"卖 {r * 100:.0f}%"
            print(f"    第{i + 1}层 +{pct * 100:.0f}% → {label} "
                  f"@ {entry_price * (1 + pct):.2f}")
    print(f"  时间止损: {config.MAX_HOLDING_BARS} 根K线 "
          f"(浮盈 < {config.TIME_STOP_MIN_PROFIT_PCT * 100:.0f}% 时强制离场)")
    print(f"  移动止损: 浮盈≥{config.TRAILING_ACTIVATE_PCT * 100:.0f}% 激活，"
          f"回撤≥{config.TRAILING_REGRET_PCT * 100:.0f}% 卖出")
    print()

    if not no_chart:
        try:
            from chart import render_exit_route_map
            out_dir = (Path(__file__).resolve().parent.parent.parent
                       / "data" / "reports" / "exit-maps")
            out_path = render_exit_route_map(
                pos, config,
                out_dir / f"exit_map_{symbol}_{entry_date}.png"
            )
            print(f"📊 退出路线图已生成: {out_path}")
        except Exception as e:
            print(f"⚠️  退出路线图生成失败: {e}")
            if logger.isEnabledFor(logging.DEBUG):
                import traceback
                traceback.print_exc()
    return 0


def _monitor(symbol: str, ref_date: str, config: ExitConfig) -> int:
    """监控现有持仓并输出卖出信号。"""
    pos = load_position(symbol)
    if pos is None:
        print(f"❌ 未找到 {symbol} 的未关闭持仓")
        print(f"   请先用 --init --entry-price X --shares N 登记")
        return 1

    print("=" * 60)
    print(f"🔍 监控持仓: {symbol}  参考日: {ref_date}")
    print(f"   开仓日: {pos.entry_date}  开仓价: {pos.entry_price:.2f}  "
          f"初始股数: {pos.shares}")
    print("=" * 60)

    df = fetch_klines(symbol, pos.entry_date, ref_date)
    if df is None or len(df) == 0:
        print(f"❌ 无法获取 {symbol} 的K线数据 [{pos.entry_date}, {ref_date}]")
        return 1

    print(f"📈 K线数据: {len(df)} 根 "
          f"({df['date'].iloc[0]} ~ {df['date'].iloc[-1]})")

    # 调用核心判定引擎
    signal = check_exit_signals(pos, df, config)

    # 若清仓：更新 Position 状态
    if signal['action'] == 'sell_all':
        pos.status = 'closed'
        pos.closed_reason = signal['reason']
        pos.closed_price = signal['sell_price']
        pos.closed_date = ref_date

    # 持久化更新后的持仓
    save_position(pos)

    # 控制台状态卡片
    print_position_status(pos, df, config, signal)

    # 卖出信号写 CSV
    if signal['action'] in ('sell_partial', 'sell_all'):
        cur_price = float(df.iloc[-1]['close'])
        profit_pct = ((cur_price - pos.entry_price) / pos.entry_price
                      if pos.entry_price else 0.0)
        csv_path = log_sell_signal(symbol, signal, cur_price, profit_pct, pos)
        print(f"📝 卖出信号已记录: {csv_path}")

    return 0


def _list_positions() -> int:
    """列出所有未关闭持仓。"""
    positions = load_all_open_positions()
    if not positions:
        print("（暂无未关闭持仓）")
        return 0
    print(f"共 {len(positions)} 个未关闭持仓：")
    print("-" * 80)
    for p in positions:
        print_position_brief(p)
    return 0


def _close_position(symbol: str, ref_date: str) -> int:
    """强制关闭持仓（手动平仓）。"""
    pos = load_position(symbol)
    if pos is None:
        print(f"❌ 未找到 {symbol} 的未关闭持仓")
        return 1
    pos.status = 'closed'
    pos.closed_reason = 'manual_close'
    pos.closed_date = ref_date
    # closed_price 留空（手动平仓时由交易员记录实际成交价）
    save_position(pos)
    print(f"✅ 已强制关闭 {symbol} 持仓（开仓日 {pos.entry_date}）")
    return 0


# ====================================================================
# 主入口
# ====================================================================

def main() -> int:
    args = _parse_args(sys.argv[1:])

    # ---- 初始化日志 ----
    if args.verbose:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    else:
        root = logging.getLogger()
        if not root.handlers:
            _h = logging.StreamHandler()
            _h.setLevel(logging.WARNING)
            root.addHandler(_h)
            root.setLevel(logging.WARNING)

    # ---- 初始化 DB ----
    init_db()

    # ---- 加载配置 ----
    try:
        config = get_exit_config(args.profile)
    except ValueError as e:
        print(f"❌ {e}")
        print(f"可用配置档:\n{list_profiles()}")
        return 1

    # ---- 路由到子命令 ----
    try:
        if args.list:
            return _list_positions()

        if not args.symbol:
            print("用法: python main.py --symbol <代码> [--date YYYY-MM-DD] "
                  "[--init --entry-price X --shares N] [--list] [--close]")
            print(f"\n可用配置档:\n{list_profiles()}")
            return 1

        if args.init:
            if args.entry_price is None or args.shares is None:
                print("❌ --init 需要 --entry-price 和 --shares 参数")
                return 1
            return _init_position(args.symbol, args.entry_price, args.shares,
                                  args.date, config, args.no_chart)

        if args.close:
            return _close_position(args.symbol, args.date)

        # 默认：监控
        return _monitor(args.symbol, args.date, config)

    except ValueError as e:
        print(f"❌ 参数错误: {e}")
        return 1
    except Exception as e:
        logger.exception("执行失败: %s", e)
        print(f"❌ 执行失败: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
