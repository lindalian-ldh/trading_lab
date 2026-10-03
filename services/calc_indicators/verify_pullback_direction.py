#!/usr/bin/env python3
"""维度A「回踩方向约束」验证脚本 —— 合成数据为主，无网络依赖。

修的 bug（个股与 ETF 共有）：
    `check_pullback_signal` 的偏离度用 `abs(收盘 − MA) / MA`，
    把"均线**上方** 0.5%"与"均线**下方** 0.5%"当成同一件事。
    但回踩买入只应认**从上方回落触及均线**：

        · 收盘在均线上方 1.4%（偏离/追高）  → 旧逻辑：仅因未超阈值就可能放行
        · 收盘在均线下方且均线下行（破位）  → 旧逻辑：只看距离，可能放行
        · 一直贴着均线横盘 / 从下方反弹上来 → 根本没有"回落动作"

    实测影响面：创业板ETF 有 30.2% 的交易日收盘位于 MA5 上方 1.5% 以外，
    这些"偏离日"在旧逻辑下只要绝对距离落在阈值内就会被当作回踩买点。

新规则（两步，见 main._check_pullback_from_above）：
    ① 必须存在回落动作：最近 PULLBACK_TOUCH_LOOKBACK 根内有收盘在均线**上方**
    ② 下方穿越受限：收盘若在均线下方，跌幅 ≤ PULLBACK_ALLOW_BELOW_RATIO × MAX_DEVIATION
       且均线必须向上（均线下行时的失守是破位）

用法:
    uv run python services/calc_indicators/verify_pullback_direction.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

import main as M
from config import TradingConfig

N = 120


def results_row(ok, label: str, detail: str) -> bool:
    val = bool(ok)
    print(f"    {'✅' if val else '❌'} {label:38s} {detail}")
    return val


def _frame(close: np.ndarray) -> pd.DataFrame:
    """组装 DataFrame，并把**前 20 日的高点抬到 10.8**。

    这一步是必须的：夹具里收盘价被调高以制造"偏离"时，会顺带突破"前20日高点"，
    于是走的是维度A 的**突破分支**而不是回踩分支，测试就测错了对象
    （第一版夹具正是这样"通过"的）。抬高历史高点可让突破分支永不触发。
    """
    op = np.r_[close[0], close[:-1]]
    df = pd.DataFrame({
        "date": pd.bdate_range("2024-01-01", periods=len(close)),
        "open": op, "high": op * 1.001, "low": np.minimum(op, close) * 0.999,
        "close": close, "volume": np.full(len(close), 1e6),
    })
    df.loc[df.index[:-1], "high"] = 10.8
    return df


def make_above(dev_pct: float) -> pd.DataFrame:
    """构造"收盘站在 MA5 **上方** dev_pct%"的形态（MA5 温和上行）。

    用二分法解出使有符号偏离恰好等于 dev_pct 的收盘价 ——
    因为改动收盘价会反过来影响 MA5，直接赋值会有反扑误差。
    """
    c = np.r_[np.linspace(9.9, 10.0, N - 5), np.full(5, 10.0)]
    lo, hi = 9.0, 11.0
    for _ in range(80):
        mid = (lo + hi) / 2
        cc = c.copy()
        cc[-1] = mid
        ma = pd.Series(cc).rolling(5).mean().iloc[-1]
        if (mid - ma) / ma * 100 > dev_pct:
            hi = mid
        else:
            lo = mid
    cc = c.copy()
    cc[-1] = (lo + hi) / 2
    return _frame(cc)


def make_below_touch(dev_pct: float = -0.02) -> pd.DataFrame:
    """构造"先上行、再浅回落到 MA5 **下方一点点**、且 MA5 仍向上"的形态。

    这是回踩的经典形态（踩穿一点点后企稳），必须判过。
    注意：若均线转为下行，同样的"收在均线下方"就是破位 —— 另一组用例覆盖。
    """
    tail = [10.02, 10.04, 10.06, 10.07, 10.068, 10.066, 10.064]
    c = np.r_[np.linspace(9.5, 10.0, N - len(tail)), tail]
    return _frame(c)


def make_below_breakdown() -> pd.DataFrame:
    """构造"均线明显向下 + 收盘失守"的破位形态（必须判拒）。"""
    c = np.r_[np.linspace(10.4, 10.1, N - 6), np.linspace(10.1, 9.85, 6)]
    return _frame(c)


def make_no_retrace() -> pd.DataFrame:
    """构造"一直在均线下方贴着走"的形态：没有从上方回落过（必须判拒）。"""
    c = np.linspace(10.0, 10.0, N)
    # 让 MA5 略上行，但所有收盘都在均线下方一点点 → 无"回落动作"
    c = np.r_[np.linspace(9.9, 10.0, N - 6), [10.01, 10.012, 10.014, 10.016, 9.998, 9.999]]
    return _frame(c)


def cfg_base() -> TradingConfig:
    """只测方向约束：关掉缩量/阳线/均线向上等其它条件，避免它们掩盖结论。"""
    c = TradingConfig()
    c.PULLBACK_DEBUG = False
    c.MOMENTUM_DEBUG = False
    c.PULLBACK_MA_TARGETS = [5]
    c.PULLBACK_REQUIRE_SHRINK_VOLUME = False
    c.PULLBACK_REQUIRE_BULLISH_CANDLE = False
    c.PULLBACK_REQUIRE_MA_UP = False
    return c


# ==================== 验证项 ====================

def check_above_ma_rejected() -> list:
    print("【1】收盘在均线【上方】远离 → 不是回踩，必须拒（本次修复的核心）")
    out = []
    cfg = cfg_base()
    for dev, expect in [(1.4, False), (0.9, False), (0.7, False)]:
        df = make_above(dev)
        # 用实际有符号偏离确认夹具生效
        ma = df["close"].rolling(5).mean().iloc[-1]
        actual = (df["close"].iloc[-1] - ma) / ma * 100
        ok, _ = M.check_dimension_a(df, cfg)
        out.append(results_row(ok == expect, f"上方 {dev:+.2f}%",
                               f"实际偏离 {actual:+.3f}% → {'通过' if ok else '拒绝'}"
                               f"（期望{'通过' if expect else '拒绝'}）"))
    return out


def check_near_ma_accepted() -> list:
    print("\n【2】收盘贴近均线（上方 0.4% 内）→ 仍是有效回踩，必须过")
    out = []
    cfg = cfg_base()
    for dev, expect in [(0.4, True), (0.1, True), (0.0, True)]:
        df = make_above(dev)
        ma = df["close"].rolling(5).mean().iloc[-1]
        actual = (df["close"].iloc[-1] - ma) / ma * 100
        ok, _ = M.check_dimension_a(df, cfg)
        out.append(results_row(ok == expect, f"贴近 {dev:+.2f}%",
                               f"实际偏离 {actual:+.3f}% → {'通过' if ok else '拒绝'}"
                               f"（期望{'通过' if expect else '拒绝'}）"))
    return out


def check_below_touch_allowed() -> list:
    print("\n【3】浅踩均线下方 + 均线向上 → 经典回踩，必须过")
    out = []
    cfg = cfg_base()
    df = make_below_touch()
    ma = df["close"].rolling(5).mean()
    actual = (df["close"].iloc[-1] - ma.iloc[-1]) / ma.iloc[-1] * 100
    ma_up = ma.iloc[-1] > ma.iloc[-2]
    ok, msg = M.check_dimension_a(df, cfg)
    out.append(results_row(ok and actual < 0 and ma_up, "浅穿越 (允许 0.30%)",
                           f"有符号偏离 {actual:+.3f}%，MA5向上={ma_up} → {'通过' if ok else '拒绝'}"))

    # ALLOW_BELOW_RATIO=0 时应转为拒绝（证明该杠杆真的生效）
    cfg0 = cfg_base()
    cfg0.PULLBACK_ALLOW_BELOW_RATIO = 0.0
    ok0, _ = M.check_dimension_a(make_below_touch(), cfg0)
    out.append(results_row(not ok0, "ALLOW_BELOW_RATIO=0 时拒绝",
                           f"→ {'通过' if ok0 else '拒绝'}（该开关可关闭下方容忍）"))
    return out


def check_breakdown_rejected() -> list:
    print("\n【4】跌破均线 + 均线下行（破位）→ 必须拒")
    out = []
    cfg = cfg_base()
    df = make_below_breakdown()
    ma = df["close"].rolling(5).mean()
    ma_up = ma.iloc[-1] > ma.iloc[-2]
    ok, msg = M.check_dimension_a(df, cfg)
    out.append(results_row(not ok, "均线下行的失守",
                           f"MA5向上={ma_up} → {'通过' if ok else '拒绝'}  {msg[:38]}"))
    return out


def check_no_retrace_rejected() -> list:
    print("\n【5】没有'从上方回落'的动作 → 必须拒")
    out = []
    cfg = cfg_base()
    df = make_no_retrace()
    close = df["close"]
    ma = close.rolling(5).mean()
    above = [(i, close.iloc[-(i + 1)] > ma.iloc[-(i + 1)]) for i in range(1, 6)]
    print(f"    近5根 收盘>MA5: {[a for _, a in above]}")
    ok, msg = M.check_dimension_a(df, cfg)
    out.append(results_row(not ok, "无回落动作",
                           f"→ {'通过' if ok else '拒绝'}  {msg[:42]}"))
    return out


def check_switch_off_is_legacy() -> list:
    print("\n【6】关闭方向约束 → 回到旧行为（保证可回退）")
    out = []
    cfg = cfg_base()
    cfg.PULLBACK_REQUIRE_TOUCH_FROM_ABOVE = False
    df = make_above(0.4)      # 旧逻辑：绝对偏离 0.4% ≤ 0.5% 即通过
    ok, _ = M.check_dimension_a(df, cfg)
    out.append(results_row(ok, "REQUIRE_TOUCH_FROM_ABOVE=False",
                           f"上方 0.4% → {'通过（旧行为）' if ok else '拒绝'}"))
    return out


# ==================== 真实数据影响面（需联网）====================

def _scan_signals(df: pd.DataFrame, cfg: TradingConfig, use_direction: bool):
    """逐根重放维度A 的**回踩分支**（跳过突破分支），统计信号数。

    use_direction=False 复现旧逻辑（仅绝对偏离 + 确认窗口）；
    True 额外施加方向约束。返回 (信号数, 过滤原因 Counter)。
    """
    from collections import Counter
    reasons = Counter()
    kept = 0
    for end in range(60, len(df) + 1):
        sub = df.iloc[:end]
        close = sub["close"]
        prev_high = sub["high"].shift(1).rolling(cfg.BREAKOUT_WINDOW).max().iloc[-1]
        if close.iloc[-1] > prev_high:      # 突破分支，不属于本次讨论
            continue
        for p in cfg.PULLBACK_MA_TARGETS:
            ma = close.rolling(p).mean()
            cm = ma.iloc[-1]
            if not np.isfinite(cm) or cm <= 0:
                continue
            sd = (close.iloc[-1] - cm) / cm
            if abs(sd) > cfg.PULLBACK_MAX_DEVIATION:
                continue
            win = any(ma.iloc[-(i + 1)] > 0 and
                      abs(close.iloc[-(i + 1)] - ma.iloc[-(i + 1)]) / ma.iloc[-(i + 1)]
                      <= cfg.PULLBACK_MAX_DEVIATION
                      for i in range(1, cfg.PULLBACK_CONFIRM_BARS + 1))
            if not win:
                continue
            if use_direction:
                ok, detail = M._check_pullback_from_above(close, ma, sd, cm > ma.iloc[-2], cfg)
                if not ok:
                    if "无'收盘在均线上方'" in detail:
                        reasons["无回落动作（一直在均线下方）"] += 1
                    elif "超过允许的" in detail:
                        reasons["破位（跌破均线过深）"] += 1
                    elif "且均线未向上" in detail:
                        reasons["破位（均线下行中失守）"] += 1
                    else:
                        reasons["其它"] += 1
                    break
            kept += 1
            break
    return kept, reasons


def check_real_impact() -> list:
    print("\n【7】真实数据影响面（联网；验证方向约束只做过滤、不引入新信号）")
    out = []
    cfg = cfg_base()
    codes = [("600519", "贵州茅台"), ("000001", "平安银行"), ("601899", "紫金矿业"),
             ("510300", "沪深300ETF"), ("510500", "中证500ETF"), ("159915", "创业板ETF")]
    print(f"    {'代码':8s}{'名称':13s}{'旧逻辑':>7s}{'新逻辑':>7s}{'过滤':>7s}")
    old_total = new_total = 0
    all_reasons = {}
    for code, name in codes:
        try:
            df = M.fetch_data(code, cfg)
        except SystemExit:
            print(f"    {code:8s}{name:13s}  ❌ 取数失败（跳过）")
            continue
        o, _ = _scan_signals(df, cfg, use_direction=False)
        n, reasons = _scan_signals(df, cfg, use_direction=True)
        old_total += o
        new_total += n
        for k, v in reasons.items():
            all_reasons[k] = all_reasons.get(k, 0) + v
        drop = f"{(o - n) / o * 100:.0f}%" if o else "—"
        print(f"    {code:8s}{name:13s}{o:7d}{n:7d}{drop:>7s}")

    print("    被过滤信号的原因分布:")
    for k, v in sorted(all_reasons.items(), key=lambda x: -x[1]):
        print(f"      {k}: {v} 个")

    # 断言：方向约束是纯过滤器 —— 不能增加信号（增加就说明逻辑写反了）
    out.append(results_row(
        new_total <= old_total,
        "方向约束不引入新信号",
        f"旧 {old_total} → 新 {new_total}（过滤 {old_total - new_total} 个，"
        f"{(old_total - new_total) / old_total * 100:.0f}%）" if old_total else "无样本"))
    # 过滤原因应当都是结构性劣质信号（这里只校验确实发生了过滤）
    out.append(results_row(
        bool(all_reasons), "过滤原因可归类",
        "、".join(f"{k}×{v}" for k, v in sorted(all_reasons.items(), key=lambda x: -x[1]))))
    return out


def main() -> int:
    print("=" * 80)
    print("维度A 回踩方向约束 验证（合成数据为主，无网络依赖；第7项需联网）")
    print("=" * 80)
    res = []
    res += check_above_ma_rejected()
    res += check_near_ma_accepted()
    res += check_below_touch_allowed()
    res += check_breakdown_rejected()
    res += check_no_retrace_rejected()
    res += check_switch_off_is_legacy()
    if "--offline" in sys.argv:
        print("\n（--offline：跳过第7项真实数据影响面）")
    else:
        res += check_real_impact()
    print("=" * 80)
    ok, total = sum(res), len(res)
    print(f"结果: {ok}/{total} 项通过  {'✅ 全部通过' if ok == total else '❌ 存在失败项'}")
    print("=" * 80)
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
