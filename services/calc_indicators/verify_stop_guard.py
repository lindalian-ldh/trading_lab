#!/usr/bin/env python3
"""止损保护 / 仓位约束 验证脚本 —— 纯合成数据，无网络依赖，直接跑。

背景（两个真实踩过的坑）：
  1. **退化止损**：`swing` 模式止损取「近 20 日最低」。标的刚创阶段新低就反弹时，
     该低点几乎等于现价 → 亏损空间趋近 0 → 盈亏比虚高。
     实测黄金ETF(518880)：止损距现价 0.06% → 盈亏比 **137:1**。
  2. **仓位超总资金**：只按"1% 风险 / 每股亏损"算股数，止损很小时会得出
     18,276 股 × 8.79 = **16 万元 > 总资金 10 万元**——隐形杠杆，无法执行。

本脚本验证：
  【1】止损保护只在"止损过近"时生效，正常情况零影响
  【2】保护后的止损距离 ≥ 下限（ATR 与百分比取更宽者）
  【3】ATR 不可用（数据全平/过短）时自动退回百分比下限
  【4】关闭开关后行为完全回到修复前
  【5】仓位绝不超总资金，且取整到整手；正常标的约束不生效
  【6】止损价 ≥ 现价（亏损失效）时不给出荒谬仓位

用法:
    uv run python services/calc_indicators/verify_stop_guard.py
"""

from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

import main as M
from config import TradingConfig

N = 160


def _to_ohlcv(close: np.ndarray, lows: np.ndarray, highs: np.ndarray) -> pd.DataFrame:
    """用显式的 close/low/high 组装 DataFrame。

    退化夹具必须**精确控制最低点的位置与数值**，不能让随机噪声决定
    （第一版用 close ± 噪声生成 low，结果噪声把最低点又压低 1%，夹具就失效了）。
    """
    op = np.r_[close[0], close[:-1]]
    return pd.DataFrame({
        "date": pd.bdate_range("2024-01-01", periods=len(close)),
        "open": op, "high": highs, "low": lows, "close": close,
        "volume": np.full(len(close), 1e6),
    })


def degenerate_df() -> pd.DataFrame:
    """构造"刚创阶段新低又立刻反弹"的退化形态。

    两个目标同时成立（这是夹具的难点）：
      ① 20 日窗口内最低点 ≈ 最后收盘价（差 ~0.2%）→ 原始止损距离趋近 0
         实测黄金ETF(518880) 当日：20日低 8.783 / 现价 8.788 = 0.06%
      ② ATR% 落在真实量级（1.1%~1.5%），否则"0.5×ATR 下限"小到不生效，
         测试会假通过。真实基准：黄金ETF 1.25 / 沪深300ETF 1.13 / 茅台 1.39。
    做法：收盘价走一个较陡的 V（日波动 ~1%，把 ATR 顶上去），
          同时让最后一根 K 线只比 V 底高 0.2%，且全窗口最低点就落在 V 底。
    """
    rng = np.random.default_rng(7)
    n_base, n_tail = N - 20, 20
    base = np.linspace(12.0, 10.6, n_base)
    # 尾部 V：先跌到 9.9685（窗口最低），最后反弹到 9.988（高 0.2%）
    tail = np.array([10.60, 10.55, 10.50, 10.44, 10.38, 10.33, 10.27, 10.22, 10.16, 10.10,
                     10.05, 10.01, 9.985, 9.9685, 9.976, 9.979, 9.981, 9.983, 9.986, 9.988])
    close = np.r_[base, tail]

    lows = close * (1 - abs(rng.normal(0, 0.009, len(close))))
    highs = close * (1 + abs(rng.normal(0, 0.009, len(close))))
    # 硬约束：最后 20 根的所有 low 都不得低于 9.955，且把下标 -5 定为窗口最低点（9.966）
    # 否则随机噪声会在窗口内造出更深的低点，退化条件就不成立了。
    lows[-20:] = np.maximum(lows[-20:], 9.955)
    v_bottom = len(close) - 5
    lows[v_bottom] = 9.966
    highs = np.maximum(highs, np.maximum(close, np.r_[close[0], close[:-1]]))
    lows = np.minimum(lows, np.minimum(close, np.r_[close[0], close[:-1]]))
    return _to_ohlcv(close, lows, highs)


def normal_df() -> pd.DataFrame:
    """正常形态：20 日最低离现价约 5%，保护不该生效。"""
    rng = np.random.default_rng(11)
    base = np.linspace(10.0, 12.0, N - 20)
    tail = np.linspace(11.4, 12.0, 20)
    close = np.r_[base, tail]
    lows = close * (1 - abs(rng.normal(0, 0.009, len(close))))
    highs = close * (1 + abs(rng.normal(0, 0.009, len(close))))
    return _to_ohlcv(close, lows, highs)


def frame(close: np.ndarray) -> pd.DataFrame:
    """简易包装（high/low 各放宽 0.2%），用于全平/极短等边界序列。"""
    op = np.r_[close[0], close[:-1]]
    hi = np.maximum(op, close) * 1.002
    lo = np.minimum(op, close) * 0.998
    return _to_ohlcv(close, lo, hi)


def flat_df() -> pd.DataFrame:
    """全平序列：ATR = 0，用于验证退回百分比下限。"""
    return frame(np.full(N, 10.0))


def results_row(ok, label: str, detail: str) -> bool:
    # 显式转 Python bool：numpy/pandas 标量比较会返回 np.bool_，直接求和会污染汇总。
    val = bool(ok)
    print(f"    {'✅' if val else '❌'} {label:34s} {detail}")
    return val


# ==================== 各项验证 ====================

def check_guard_triggers_only_when_needed() -> list:
    print("【1】止损保护只在止损过近时生效（正常情况零影响）")
    # 显式锁定 swing 模式：本项测的是"最小距离保护"这条横切逻辑，
    # 与 STOP_MODE 默认值无关（默认已改为 near_low，不锁定会测错对象）。
    cfg = TradingConfig()
    cfg.STOP_MODE = "swing"
    out = []

    df = degenerate_df()
    price = float(df["close"].iloc[-1])
    raw = float(df["low"].tail(cfg.RR_WINDOW).min())
    stop, detail = M._calc_stop_price(df, price, cfg)
    dist = (price - stop) / price * 100
    raw_dist = (price - raw) / price * 100
    out.append(results_row(
        detail["applied"] and stop < raw,
        "退化形态：保护应生效",
        f"原始距离 {raw_dist:.3f}% → 最终 {dist:.3f}%（下移至 {stop:.3f}）"))

    df2 = normal_df()
    price2 = float(df2["close"].iloc[-1])
    raw2 = float(df2["low"].tail(cfg.RR_WINDOW).min())
    stop2, detail2 = M._calc_stop_price(df2, price2, cfg)
    out.append(results_row(
        (not detail2["applied"]) and abs(stop2 - raw2) < 1e-9,
        "正常形态：保护不该生效",
        f"原始 = 最终 = {stop2:.3f}（距离 {(price2 - stop2) / price2 * 100:.2f}%）"))
    return out


def check_near_low_mode() -> list:
    """近端结构止损（STOP_MODE="near_low"）：默认档，短线突破交易不该用 20 日低点那么远。

    实测依据（002119）：swing 止损距现价 −26.7% → 盈亏比 0.34:1；
    near_low 降到 −8.0% → 盈亏比 1.13:1。
    规则：止损 = max(近 NEAR_LOW_WINDOW 日最低, 现价 − NEAR_LOW_MAX_PCT × 现价)。
    """
    print("\n【2b】近端结构止损 near_low（默认档）")
    out = []

    cfg = TradingConfig()          # 默认即 near_low
    out.append(results_row(cfg.STOP_MODE == "near_low", "默认 STOP_MODE",
                           f"= {cfg.STOP_MODE!r}（期望 near_low）"))

    df = normal_df()
    price = float(df["close"].iloc[-1])
    near = float(df["low"].tail(cfg.NEAR_LOW_WINDOW).min())
    cap = price - cfg.NEAR_LOW_MAX_PCT * price
    stop, _ = M._calc_stop_price(df, price, TradingConfig())
    expect = max(near, cap)
    out.append(results_row(
        abs(stop - expect) < 1e-9, "取更近者（近端低 vs 距离上限）",
        f"止损 {stop:.3f} = max(近{cfg.NEAR_LOW_WINDOW}日低 {near:.3f}, "
        f"上限 {cap:.3f})，距现价 {(price - stop) / price * 100:.2f}%"))

    # 上限必须真正生效：构造"近端低点极远"的情形，止损应被上限托住而不会过远
    c = TradingConfig()
    c.NEAR_LOW_WINDOW = 60
    stop_far, _ = M._calc_stop_price(df, price, c)
    dist_far = (price - stop_far) / price * 100
    out.append(results_row(
        dist_far <= c.NEAR_LOW_MAX_PCT * 100 + 1e-6,
        "距离上限生效（不会过远）",
        f"近60日低点场景下止损距离 {dist_far:.2f}% ≤ 上限 {c.NEAR_LOW_MAX_PCT * 100:.1f}%"))
    return out


def check_floor_is_widest() -> list:
    print("\n【2】保护后止损距离 ≥ 下限（ATR 与百分比取更宽者）")
    cfg = TradingConfig()
    cfg.STOP_MODE = "swing"        # 锁定模式：本项测的是距离下限这条横切逻辑
    out = []
    for mult, pct, tag in [(0.5, 0.003, "默认 0.5×ATR / 0.3%"),
                           (2.0, 0.003, "放宽 2.0×ATR / 0.3%"),
                           (0.01, 0.02, "极小ATR倍数 / 2% 百分比")]:
        c = TradingConfig()
        c.STOP_MODE = "swing"
        c.STOP_MIN_DIST_ATR_MULT, c.STOP_MIN_DIST_PCT = mult, pct
        df = degenerate_df()
        price = float(df["close"].iloc[-1])
        atr = float(M.calc_atr(df, c.ATR_PERIOD).iloc[-1])
        stop, detail = M._calc_stop_price(df, price, c)
        dist = price - stop
        expected_floor = max(mult * atr, pct * price)
        ok = abs(dist - expected_floor) < 1e-6 and dist >= expected_floor - 1e-9
        out.append(results_row(ok, tag, f"距离 {dist:.4f} = 期望下限 {expected_floor:.4f}"
                                        f"（ATR={atr:.3f}）"))
    return out


def check_atr_unavailable_fallback() -> list:
    print("\n【3】ATR 不可用（全平序列）时退回百分比下限")
    cfg = TradingConfig()
    out = []
    df = flat_df()
    price = float(df["close"].iloc[-1])
    stop, detail = M._calc_stop_price(df, price, cfg)
    # 全平：20 日低 == 现价 → 原始距离 0，必须被百分比下限托住
    expect = price - cfg.STOP_MIN_DIST_PCT * price
    ok = abs(stop - expect) < 1e-6
    out.append(results_row(ok, "全平序列", f"止损 {stop:.4f} = 期望 {expect:.4f}"
                                          f"（原始 {detail['raw_stop']:.4f}）"))

    short = frame(np.array([10.0, 10.1]))          # 数据过短，ATR 全 NaN
    price_s = float(short["close"].iloc[-1])
    try:
        stop_s, detail_s = M._calc_stop_price(short, price_s, cfg)
        ok_s = stop_s < price_s and math.isfinite(stop_s)
        out.append(results_row(ok_s, "数据过短", f"止损 {stop_s:.4f}（未崩溃，已托底）"))
    except Exception as e:  # noqa: BLE001
        out.append(results_row(False, "数据过短", f"抛异常 {type(e).__name__}: {e}"))
    return out


def check_switch_off_is_legacy() -> list:
    print("\n【4】关闭开关后行为 = 修复前")
    cfg = TradingConfig()
    cfg.STOP_MIN_DIST_ENABLED = False
    out = []
    df = degenerate_df()
    price = float(df["close"].iloc[-1])
    raw = float(df["low"].tail(cfg.RR_WINDOW).min())
    stop, detail = M._calc_stop_price(df, price, cfg)
    out.append(results_row(
        abs(stop - raw) < 1e-9 and not detail["applied"],
        "STOP_MIN_DIST_ENABLED=False",
        f"止损 {stop:.3f} = 原始 {raw:.3f}（未施加保护）"))
    return out


def check_position_capital_cap() -> list:
    print("\n【5】仓位受总资金约束（且正常标的约束不生效）")
    cfg = TradingConfig()
    out = []

    # ① 退化止损 → 必须被资金约束截断
    df = degenerate_df()
    price = float(df["close"].iloc[-1])
    _, c_info = M.check_dimension_c(df, cfg)
    pos = M.calc_position(c_info, cfg)
    value = pos["shares"] * price
    ok = value <= cfg.TOTAL_CAPITAL and pos["shares"] % 100 == 0 and pos["shares"] > 0
    out.append(results_row(
        ok, "退化止损：不得超总资金",
        f"{pos['shares']:,} 股 × {price:.2f} = {value:,.0f} 元 ≤ {cfg.TOTAL_CAPITAL:,.0f}，"
        f"limited_by={pos['limited_by']}"))

    # ② 正常止损 → 风险约束应主导，不被资金截断
    df2 = normal_df()
    price2 = float(df2["close"].iloc[-1])
    _, c_info2 = M.check_dimension_c(df2, cfg)
    pos2 = M.calc_position(c_info2, cfg)
    ok2 = (pos2["limited_by"] is None
           and pos2["shares"] <= pos2["shares_by_risk"] + 0.5
           and pos2["shares"] * price2 <= cfg.TOTAL_CAPITAL)
    out.append(results_row(
        ok2, "正常止损：风险约束主导",
        f"{pos2['shares']:,} 股（风险仓 {pos2['shares_by_risk']:,} / "
        f"资金上限 {pos2['max_shares_by_capital']:,}）"))

    # ③ 边界：总资金买不起 1 手 → 明确给出 0 与原因
    c3 = TradingConfig()
    c3.TOTAL_CAPITAL = 500
    _, c_info3 = M.check_dimension_c(df2, c3)
    pos3 = M.calc_position(c_info3, c3)
    ok3 = pos3["shares"] == 0 and pos3.get("note")
    out.append(results_row(
        ok3, "总资金买不起 1 手", f"shares=0, note={pos3.get('note', '')[:34]}…"))
    return out


def check_invalid_stop() -> list:
    print("\n【6】止损 ≥ 现价（亏损失效）不得给出荒谬仓位")
    cfg = TradingConfig()
    out = []
    bad = {"price": 10.0, "stop": 10.5}
    pos = M.calc_position(bad, cfg)
    out.append(results_row(pos["shares"] == 0 and bool(pos.get("note")),
                           "止损 > 现价", f"shares=0, note={pos.get('note')}"))
    bad2 = {"price": 0.0, "stop": -1.0}
    pos2 = M.calc_position(bad2, cfg)
    out.append(results_row(pos2["shares"] == 0 and bool(pos2.get("note")),
                           "现价 = 0", f"shares=0, note={pos2.get('note')}"))
    return out


def main() -> int:
    print("=" * 72)
    print("止损保护 / 仓位约束 验证（合成数据，无网络依赖）")
    print("=" * 72)
    res = []
    res += check_guard_triggers_only_when_needed()
    res += check_near_low_mode()
    res += check_floor_is_widest()
    res += check_atr_unavailable_fallback()
    res += check_switch_off_is_legacy()
    res += check_position_capital_cap()
    res += check_invalid_stop()
    print("=" * 72)
    ok, total = sum(res), len(res)
    print(f"结果: {ok}/{total} 项通过  {'✅ 全部通过' if ok == total else '❌ 存在失败项'}")
    print("=" * 72)
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
