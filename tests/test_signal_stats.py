"""P2.3 统计内核（core/signal_stats.py）单元测试。

重点验证三件事：
1. 前向收益口径**与 audit_regime_hold 一致**（次日开盘入场、持 H 根收盘出场）；
2. **块 bootstrap 比朴素日级 t 更保守** —— 这是本模块存在的理由（重叠 H 日窗口）；
3. 固定 seed 可复现，且退化输入返回 NaN 而不是编出一个显著结论。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import core.signal_stats as s


def _mk(closes, opens=None, start="2020-01-01"):
    c = np.asarray(closes, dtype=float)
    o = c if opens is None else np.asarray(opens, dtype=float)
    dates = pd.bdate_range(start, periods=len(c))
    return pd.DataFrame({"date": dates, "open": o, "high": c, "low": c, "close": c})


# ====================================================================
# 前向收益口径
# ====================================================================

def test_fwd_ret_hand_computation_hold1():
    """hold=1：入场 = open[i+1]，出场 = close[i+1]。"""
    df = _mk([10, 11, 12], opens=[9, 10, 11])
    r = s.fwd_ret(df, 1)
    assert r[0] == pytest.approx((11 / 10 - 1) * 100)
    assert r[1] == pytest.approx((12 / 11 - 1) * 100)
    assert np.isnan(r[2])


def test_fwd_ret_hand_computation_hold5():
    """hold=5：入场 = open[i+1]，出场 = close[i+5]（不是 close[i+1+5]）。"""
    closes = list(range(100, 120))
    df = _mk(closes, opens=[c - 1 for c in closes])
    r = s.fwd_ret(df, 5)
    assert r[0] == pytest.approx((closes[5] / (closes[1] - 1) - 1) * 100)
    assert r[3] == pytest.approx((closes[8] / (closes[4] - 1) - 1) * 100)
    assert np.all(np.isnan(r[-5:]))          # 尾部不足 H 根 ⇒ NaN


def test_fwd_ret_nan_when_open_invalid():
    df = _mk([10, 11, 12])
    df.loc[1, "open"] = np.nan
    r = s.fwd_ret(df, 1)
    assert np.isnan(r[0])              # 入场价缺失 ⇒ 不可估
    assert np.isfinite(r[1])           # 仅影响用到该 open 的那一天


# ====================================================================
# 点估计
# ====================================================================

def test_two_sample_diff_and_length_guard():
    r = np.array([1.0, 2.0, 10.0, 12.0])
    m = np.array([False, False, True, True])
    assert s.two_sample_diff(r, m) == pytest.approx(9.5)
    with pytest.raises(ValueError):
        s.two_sample_diff([1.0, 2.0], [True])


def test_two_sample_diff_nan_when_one_side_empty():
    assert np.isnan(s.two_sample_diff([1.0, 2.0], [True, True]))


# ====================================================================
# 块 bootstrap（主统计量）
# ====================================================================

def test_block_bootstrap_detects_clear_signal():
    rng = np.random.default_rng(1)
    n = 600
    r = rng.normal(0, 1, n)
    m = np.zeros(n, dtype=bool)
    m[100:200] = True
    r[m] += 5.0                                  # 强信号
    out = s.block_bootstrap_diff(r, m, block=20, n_boot=500, seed=42)
    assert out["D"] == pytest.approx(s.two_sample_diff(r, m))
    assert out["t"] > 3
    assert out["p"] < 0.01
    assert out["ci_lo"] > 0
    assert out["mde"] == pytest.approx(s.POWER_Z * out["se"])


def test_block_bootstrap_is_more_conservative_than_naive_t_on_overlapping_windows():
    """**本模块存在的理由**：收益强自相关 + 掩码成段 ⇒ 块 bootstrap 的 SE 必须大于朴素 SE。"""
    rng = np.random.default_rng(3)
    n = 800
    r = np.empty(n)
    r[0] = 0.0
    for i in range(1, n):                        # AR(1)：强自相关
        r[i] = 0.9 * r[i - 1] + rng.normal(0, 1)
    m = np.zeros(n, dtype=bool)
    m[200:600] = True                            # 一个很长的"状态"
    boot = s.block_bootstrap_diff(r, m, block=20, n_boot=500, seed=5)
    naive = s.naive_day_t(r, m)
    assert boot["se"] > naive["se"]
    assert abs(boot["t"]) < abs(naive["t"])


def test_block_bootstrap_degenerate_inputs_return_nan():
    out = s.block_bootstrap_diff(np.arange(50.0), np.zeros(50, dtype=bool), block=10)
    assert np.isnan(out["t"]) and np.isnan(out["mde"])
    out2 = s.block_bootstrap_diff(np.full(50, np.nan), np.ones(50, dtype=bool), block=10)
    assert np.isnan(out2["t"])


def test_block_bootstrap_is_deterministic_given_seed():
    rng = np.random.default_rng(9)
    r = rng.normal(size=400)
    m = rng.random(400) > 0.5
    a = s.block_bootstrap_diff(r, m, block=20, n_boot=300, seed=7)
    b = s.block_bootstrap_diff(r, m, block=20, n_boot=300, seed=7)
    assert a == b
    c = s.block_bootstrap_diff(r, m, block=20, n_boot=300, seed=8)
    assert c["se"] != a["se"]


# ====================================================================
# 片段与安慰剂
# ====================================================================

def test_episode_spans_merges_within_gap():
    m = np.zeros(20, dtype=bool)
    for i in (0, 2, 3, 10):
        m[i] = True
    assert s.episode_spans(m, gap=1) == [(0, 0, 1), (2, 3, 2), (10, 10, 1)]
    assert s.episode_spans(m, gap=3) == [(0, 3, 4), (10, 10, 1)]


def test_placebo_p_small_for_strong_signal_and_bounded():
    rng = np.random.default_rng(11)
    n = 800
    r = rng.normal(0, 1, n)
    m = np.zeros(n, dtype=bool)
    m[300:330] = True
    m[600:630] = True
    r[m] += 6.0
    _q, p = s.placebo_p(r, m, np.random.default_rng(2), n=200)
    assert 0.0 <= p <= 0.05

    r2 = rng.normal(0, 1, n)
    m2 = rng.random(n) > 0.9
    _q2, p2 = s.placebo_p(r2, m2, np.random.default_rng(2), n=200)
    assert 0.0 <= p2 <= 1.0


def test_placebo_p_nan_when_no_signal():
    q, p = s.placebo_p(np.arange(100.0), np.zeros(100, dtype=bool),
                       np.random.default_rng(0), n=50)
    assert np.isnan(q) and np.isnan(p)


# ====================================================================
# 片段级诊断与组装
# ====================================================================

def test_episode_level_stats_counts_episodes():
    r = np.array([0.0, 0.0, 5.0, 5.0, 0.0, 0.0, 0.0, 0.0, 0.0, 5.0])
    m = np.array([False, False, True, True, False, False, False, False, False, True])
    st = s.episode_level_stats(r, m)
    assert st["n_episodes"] == 2
    assert st["mean_sig"] == pytest.approx(5.0)


def test_summarize_returns_all_frozen_fields():
    rng = np.random.default_rng(4)
    n = 500
    r = rng.normal(size=n)
    m = np.zeros(n, dtype=bool)
    m[100:140] = True
    r[m] += 3
    out = s.summarize(r, m, block=20, n_boot=200, n_placebo=50)
    for k in ("D", "se", "t", "p", "ci_lo", "ci_hi", "mde", "n_signal_days",
              "n_base_days", "n_episodes", "placebo_q95", "placebo_p", "naive_day_t",
              "episode_n_episodes", "episode_t"):
        assert k in out, f"缺少字段 {k}"
    assert out["n_episodes"] == 1
    assert out["n_signal_days"] == 40
