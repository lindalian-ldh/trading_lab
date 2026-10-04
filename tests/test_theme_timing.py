"""Phase 2 三个择时层（core/theme_timing.py）单元测试。

**全部离线**：只用手造的合成序列，不取数、不联网。
重点是三条纪律：
1. **无未来函数** —— 截断数据不得改变历史掩码（`test_*_no_lookahead`）；
2. **方向不许反** —— L1 是**看涨**反转（`test_l1_*direction*`）；
3. **冻结参数不许漂** —— 常量必须等于 P0.5 冻结基准 v1（`test_frozen_parameters`）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import core.theme_timing as t


# ====================================================================
# 夹具
# ====================================================================

def _mk(closes, start="2020-01-01", spread=0.1) -> pd.DataFrame:
    """由收盘价构造最小 OHLC（high=close+spread，low=close-spread）。"""
    c = np.asarray(closes, dtype=float)
    dates = pd.bdate_range(start, periods=len(c))
    return pd.DataFrame({"date": dates, "open": c, "high": c + spread,
                         "low": c - spread, "close": c})


def _breakout_frame(k: int = 5):
    """**L1 看涨突破**场景（k=5）：

    - 0~15：100 起、每根 +2 ⇒ 130（**摆动高点** = 130，在 15+5=20 日被确认）
    - 16~35：每根 −2 ⇒ 90（一路创新低）
    - 36~60：每根 +2 ⇒ 92…140（在 56 日首次收上 130，57 日仍站上 ⇒ 触发）
    """
    a = [100 + 2 * i for i in range(16)]              # idx 0..15 → 100..130
    b = [130 - 2 * (i - 15) for i in range(16, 36)]   # idx 16..35 → 128..90
    c = [92 + 2 * (i - 36) for i in range(36, 61)]    # idx 36..60 → 92..140
    return _mk(a + b + c)


def _cross_frame(rebound: float = 1.2):
    """先跌后涨。

    ``rebound=1.2``（默认）**: 上穿发生在 MA30 仍在下行时 ⇒ 被斜率条件拒绝**（用于反向用例）；
    ``rebound=2.0`` **: 上穿时 MA30 已转平/上行 ⇒ 通过**（用于正向用例）。
    """
    down = [40 - 0.5 * i for i in range(40)]
    up = [down[-1] + rebound * i for i in range(1, 41)]
    return _mk(down + up)


def _cross_frame_ok():
    """上穿且 MA30 斜率 ≥ 0 的夹具（实测在 idx 47 点亮）。"""
    return _cross_frame(rebound=2.0)


# ====================================================================
# 输入校验
# ====================================================================

def test_prepare_rejects_missing_columns():
    with pytest.raises(t.ThemeTimingError):
        t.l1_structure_break(pd.DataFrame({"date": pd.bdate_range("2020-01-01", periods=5),
                                           "close": [1, 2, 3, 4, 5]}))


def test_prepare_rejects_empty():
    with pytest.raises(t.ThemeTimingError):
        t.l2_ma_turn(pd.DataFrame(columns=["date", "open", "high", "low", "close"]))


# ====================================================================
# 摆动点
# ====================================================================

def test_swing_points_marks_endpoint_with_delay_not_the_middle():
    """摆动低点标记在**端点 i**，而不是确认日 i+k。"""
    closes = [10, 9, 8, 7, 6, 5, 4, 3, 4, 5, 6, 7, 8, 9, 10]
    sp = t.swing_points(_mk(closes), k=2)
    assert bool(sp["swing_low"].iloc[7]) is True
    assert int(sp["swing_low"].sum()) == 1


def test_swing_points_strict_extremum_ignores_flat_top():
    """平顶（两根等高）**不构成**摆动高点 —— 判据是严格极值。"""
    sp = t.swing_points(_mk([1, 2, 3, 4, 5, 5, 4, 3, 2, 1]), k=2)
    assert int(sp["swing_high"].sum()) == 0


def test_swing_high_asof_is_invisible_before_confirmation():
    """摆动高点价格在 i+k 之前必须不可见（否则 L1 会用到未确认的摆点）。"""
    df = _breakout_frame()
    asof = t.swing_high_asof(df, k=5)
    assert np.isnan(asof.iloc[19])          # 15+5-1：仍不可见
    assert asof.iloc[20] == pytest.approx(130.1)   # 摆动高点的 high = close+0.1
    assert asof.iloc[30] == pytest.approx(130.1)


# ====================================================================
# L1 · 结构破坏（看涨反转）
# ====================================================================

def test_l1_fires_on_confirmed_upside_breakout():
    """56 日首次收上 130 → 57 日确认 ⇒ 掩码在 **57** 日点亮（不是 56）。"""
    mask = t.l1_structure_break(_breakout_frame())
    assert bool(mask.iloc[56]) is False      # 突破日不点亮（还要确认）
    assert bool(mask.iloc[57]) is True


def test_l1_requires_confirmation_bar():
    """突破次日收盘跌回 H_last 之下 ⇒ 该次突破作废。"""
    a = [100 + 2 * i for i in range(16)]
    b = [130 - 2 * (i - 15) for i in range(16, 36)]
    c = [92 + 2 * (i - 36) for i in range(36, 58)]     # 36..57 → 92..134
    c[21] = 120                                        # idx 57 收盘跌回 130 以下
    mask = t.l1_structure_break(_mk(a + b + c))
    assert bool(mask.iloc[57]) is False
    assert int(mask.sum()) == 0                        # 之后不再上行，全程无信号


def test_l1_direction_is_bullish_not_bearish():
    """**方向守卫**：把同一个突破序列上下翻转（涨→跌）后不得产生任何 L1。

    防止把「结构破坏」实现成看跌信号（P0.5 修订记录 #1）。
    """
    up = _breakout_frame()
    down = up.copy()
    for col in ("open", "high", "low", "close"):
        down[col] = 260.0 - up[col]                    # 镜像翻转（价格恒正）
    down["high"], down["low"] = down["high"] + 0.2, down["low"] - 0.2
    assert int(t.l1_structure_break(_mk(down["close"].tolist())).sum()) == 0


def test_l1_no_lookahead_truncation_invariance():
    """**无未来函数**：用前 n 根算出的掩码，必须与用全序列算出的前 n 根完全一致。"""
    df = _breakout_frame()
    full = t.l1_structure_break(df)
    for n in (30, 45, 58, 60):
        part = t.l1_structure_break(df.iloc[:n].reset_index(drop=True))
        assert part.tolist() == full.iloc[:n].tolist(), f"n={n} 出现前视偏差"


def test_l1_is_all_false_when_monotone_down():
    mask = t.l1_structure_break(_mk([100 - i for i in range(80)]))
    assert int(mask.sum()) == 0


# ====================================================================
# L2 · 均线转向
# ====================================================================

def test_ols_slope_matches_numpy_polyfit():
    rng = np.random.default_rng(7)
    for w in (2, 3, 5, 8):
        y = rng.normal(size=w).cumsum()
        ref = float(np.polyfit(np.arange(w), y, 1)[0])
        assert t._ols_slope(y) == pytest.approx(ref)


def test_l2_fires_on_cross_with_nonneg_slope():
    df = _cross_frame_ok()
    mask = t.l2_ma_turn(df)
    ma_f = df["close"].rolling(10, min_periods=10).mean()
    ma_s = df["close"].rolling(30, min_periods=30).mean()
    cross = (ma_f.shift(1) <= ma_s.shift(1)) & (ma_f > ma_s)
    assert int(mask.sum()) >= 1
    # 每个点亮日都必须是"上穿日"，且当日 MA30 斜率 ≥ 0
    for i in np.where(mask.to_numpy())[0]:
        assert bool(cross.iloc[i]) is True
        assert t._ols_slope(ma_s.iloc[i - 4:i + 1].to_numpy(float)) >= 0


def test_l2_slope_filter_rejects_cross_while_ma30_still_falling():
    """MA30 仍在下行时的上穿必须被斜率条件挡掉。"""
    df = _cross_frame()
    mask = t.l2_ma_turn(df)
    ma_f = df["close"].rolling(10, min_periods=10).mean()
    ma_s = df["close"].rolling(30, min_periods=30).mean()
    cross = (ma_f.shift(1) <= ma_s.shift(1)) & (ma_f > ma_s)
    rejected = [i for i in np.where(cross.fillna(False).to_numpy())[0]
                if t._ols_slope(ma_s.iloc[i - 4:i + 1].to_numpy(float)) < 0]
    assert rejected, "夹具应包含至少一个被斜率条件拒绝的上穿"
    assert not mask.iloc[rejected].any()


def test_l2_is_event_like_not_persistent():
    """上穿是**事件**：掩码为 True 的连续段长度应为 1。"""
    mask = t.l2_ma_turn(_cross_frame_ok())
    assert mask.sum() <= 3
    for s, e, _n in t.episode_spans(mask, gap=0):
        assert s == e


def test_l2_no_lookahead_truncation_invariance():
    df = _cross_frame_ok()
    full = t.l2_ma_turn(df)
    for n in (45, 60, 79, 80):
        part = t.l2_ma_turn(df.iloc[:n].reset_index(drop=True))
        assert part.tolist() == full.iloc[:n].tolist(), f"n={n} 出现前视偏差"


# ====================================================================
# RS · 相对强度
# ====================================================================

def _anchor_flat(n, value=100.0):
    return _mk([value] * n)


def test_rs_requires_absolute_price_condition_to_avoid_false_positive():
    """**坑 3 守卫**：主题与锚同跌、主题跌得少 ⇒ 比价走强，但价格仍在下行 ⇒ 必须为 False。"""
    n = 60
    theme = _mk([100 - 0.5 * i for i in range(n)])      # 跌得慢
    anchor = _mk([100 - 1.0 * i for i in range(n)])     # 跌得快 ⇒ 比价上升
    mask = t.rs_strength(theme, anchor, window=20, ma_window=20)
    assert int(mask.sum()) == 0


def test_rs_fires_when_both_ratio_and_price_rise():
    n = 60
    theme = _mk([100 + 1.0 * i for i in range(n)])
    anchor = _anchor_flat(n)
    mask = t.rs_strength(theme, anchor, window=20, ma_window=20)
    assert int(mask.sum()) > 0
    assert bool(mask.iloc[-1]) is True


def test_rs_missing_anchor_dates_are_not_forward_filled():
    """锚缺某天 ⇒ 该天 RS 必须是 False（**不许** ffill 出假的"走强"）。"""
    n = 40
    theme = _mk([100 + 1.0 * i for i in range(n)])
    anchor = _anchor_flat(n)
    drop = anchor.index[30]
    anchor = anchor.drop(index=drop).reset_index(drop=True)
    mask = t.rs_strength(theme, anchor, window=5, ma_window=5)
    assert len(mask) == n
    assert bool(mask.iloc[30]) is False


def test_rs_raises_on_no_common_trading_dates():
    theme = _mk([100, 101, 102], start="2020-01-01")
    anchor = _mk([100, 101, 102], start="2021-06-01")
    with pytest.raises(t.ThemeTimingError):
        t.rs_strength(theme, anchor)


# ====================================================================
# 组合 / 频次
# ====================================================================

def test_combine_layers_is_same_day_and():
    l1 = pd.Series([False, True, True, False, False])
    l2 = pd.Series([False, False, True, True, False])
    rs = pd.Series([True, True, False, True, True])
    out = t.combine_layers(l1, l2, rs)
    assert out["L1"].tolist() == [False, True, True, False, False]
    assert out["L1+L2"].tolist() == [False, False, True, False, False]
    assert out["L1+L2+RS"].tolist() == [False, False, False, False, False]


def test_combine_layers_length_mismatch_raises():
    with pytest.raises(t.ThemeTimingError):
        t.combine_layers(pd.Series([True]), pd.Series([True, False]))


def test_episode_spans_merges_gap_and_counts_segments():
    mask = pd.Series([False] * 12)
    for i in (0, 2, 3, 10):
        mask.iloc[i] = True
    assert t.episode_spans(mask, gap=1) == [(0, 0, 1), (2, 3, 2), (10, 10, 1)]
    assert t.episode_spans(mask, gap=3) == [(0, 3, 4), (10, 10, 1)]
    assert len(t.episode_spans(mask, gap=9)) == 1


def test_episodes_per_year():
    mask = pd.Series([False] * 500)
    mask.iloc[0] = mask.iloc[250] = True
    dates = pd.Series(pd.bdate_range("2020-01-01", periods=500))
    assert t.episodes_per_year(mask, dates) == pytest.approx(2 / (dates.iloc[-1] - dates.iloc[0]).days * 365.25)


def test_episodes_per_year_zero_when_no_signal():
    dates = pd.Series(pd.bdate_range("2020-01-01", periods=100))
    assert t.episodes_per_year(pd.Series([False] * 100), dates) == 0.0


# ====================================================================
# 一次性取三层 / 冻结参数守卫
# ====================================================================

def test_layer_masks_columns_and_rs_fallback_without_anchor():
    out = t.layer_masks(_cross_frame(), anchor_df=None)
    assert list(out.columns) == ["date", "close", "L1", "L2", "RS", "L1+L2", "L1+L2+RS"]
    assert not out["RS"].any()
    assert not (out["L1+L2+RS"]).any()


def test_layer_masks_with_anchor_produces_rs():
    df = _cross_frame()
    out = t.layer_masks(df, anchor_df=_anchor_flat(len(df)))
    assert out["RS"].dtype == bool
    assert len(out) == len(df)


def test_frozen_parameters_match_p05_v1():
    """**冻结守卫**：这些数字改了就必须在方案文档记修订记录。"""
    assert (t.K_SWING, t.L1_LOW_LOOKBACK, t.L1_CONFIRM_BARS) == (5, 20, 1)
    assert (t.MA_FAST, t.MA_SLOW, t.MA_SLOPE_WINDOW, t.MA_SLOPE_MIN) == (10, 30, 5, 0.0)
    assert (t.RS_WINDOW, t.RS_MA_WINDOW) == (20, 20)
    assert t.EPISODE_GAP == 3
    assert t.LAYER_SPEC["L1"] == {"k": 5, "low_lookback": 20, "confirm_bars": 1}
