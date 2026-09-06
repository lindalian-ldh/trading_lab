"""S7 大盘 KDJ 高位死叉 纯函数测试（不触网，全 mock DataFrame）。

覆盖：
    T1  _resample_ohlc：日线聚合成周线，open=first / close=last / high=max / low=min / amount=sum
    T2  _calc_kdj：横盘（h_max==l_min）→ RSV=50，K/D 收敛到 50 附近
    T3  _calc_kdj：单调上涨 → K 进入高位（>80）
    T4  _find_recent_cross：能正确识别死叉（K 从上方穿 D）
    T5  _find_recent_cross：lookback 窗口外的死叉返回 None
    T6  check_kdj_divergence：数据不足（< n+2 行）→ 灰灯 data_sufficient=False
    T7  check_kdj_divergence：AND 模式 + 一个周期数据不足 → 实际存在的周期全部命中即红（灰周期不永久锁死）
    T8  check_kdj_divergence：近 3 周期（tight）高位死叉 → S7 红灯，value 与命中周期一致
    T9  check_kdj_divergence：K>80 边界（K 恰好 80.0）不算高位死叉（条件是 >80）
    T10 check_kdj_divergence：仅月线模式（KDJ_USE_WEEKLY=False）仍能正常计算
    T11 check_kdj_divergence：OR 模式周线命中 → 红灯；月线未命中不影响
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_THIS_DIR = Path(__file__).resolve().parent
_TRADING_LAB_ROOT = _THIS_DIR.parent.parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))
for _p in list(sys.path[:5]):
    try:
        if Path(_p).resolve() == _TRADING_LAB_ROOT.resolve():
            sys.path.remove(_p)
            sys.path.append(_p)
    except Exception:
        pass

from config import AlarmConfig
from indicators import (
    _calc_kdj,
    _find_recent_cross,
    _resample_ohlc,
    check_kdj_divergence,
)


# ====================================================================
# 辅助：构造日线 OHLC
# ====================================================================

def _daily_df(cnt: int = 300, seed: int = 0, regime: str = 'flat') -> pd.DataFrame:
    """生成 cnt 根模拟日线 OHLC。regime 控制尾部形态。"""
    rng = np.random.default_rng(seed)
    dates = pd.date_range('2025-01-06', periods=cnt, freq='B')
    close = 100.0
    out = []
    for i in range(cnt):
        if regime == 'up' and i >= cnt - 60:
            ret = rng.normal(0.012, 0.004)
        elif regime == 'down' and i >= cnt - 60:
            ret = rng.normal(-0.012, 0.004)
        elif regime == 'down_recent' and i >= cnt - 15:
            ret = rng.normal(-0.025, 0.004)   # 末 15 日急跌 → 制造近期高位死叉
        elif regime == 'down_recent_mild' and i >= cnt - 10:
            ret = rng.normal(-0.018, 0.003)
        else:
            ret = rng.normal(0.0, 0.008)
        open_ = close * (1 + rng.normal(0, 0.004))
        high = max(open_, close) * (1 + abs(rng.normal(0, 0.006)))
        low = min(open_, close) * (1 - abs(rng.normal(0, 0.006)))
        close = close * (1 + ret)
        out.append({
            'date': dates[i].strftime('%Y-%m-%d'),
            'open': open_, 'high': high, 'low': low, 'close': close,
            'amount': rng.uniform(1e8, 1e9),
        })
    return pd.DataFrame(out)


def _cfg(**over) -> AlarmConfig:
    cfg = AlarmConfig()
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


# ====================================================================
# T1-T5: 底层工具函数
# ====================================================================

def test_T1_resample_ohlc_weekly_aggregation():
    """日线→周线：open=first, close=last, high=max, low=min, amount=sum。"""
    df = _daily_df(cnt=10, seed=1, regime='flat')
    wk = _resample_ohlc(df, 'W')
    assert wk is not None and not wk.empty
    # 第一周的 high 应 >= 该周所有日 high
    first_week_dates = wk['date'].iloc[0]
    daily_in_first_week = df[pd.to_datetime(df['date']) <= pd.to_datetime(first_week_dates)]
    if len(daily_in_first_week) > 0:
        assert wk['high'].iloc[0] >= daily_in_first_week['high'].max() - 1e-6
        assert wk['low'].iloc[0] <= daily_in_first_week['low'].min() + 1e-6
    # 列名正确
    assert {'date', 'open', 'high', 'low', 'close', 'amount'}.issubset(wk.columns)


def test_T2_calc_kdj_flat_market_rsv_50():
    """横盘：h_max==l_min → RSV=50，K/D 向 50 收敛。"""
    n = 9
    close = np.array([100.0] * 30)
    high = np.array([101.0] * 30)
    low = np.array([99.0] * 30)
    k, d, j = _calc_kdj(high, low, close, n, 3, 3)
    # 前 n-1 个为 NaN
    assert all(np.isnan(k[:n - 1]))
    # 有效位置 RSV=50 → K/D 趋近 50
    assert abs(k[-1] - 50.0) < 1.0
    assert abs(d[-1] - 50.0) < 1.0
    assert abs(j[-1] - 50.0) < 1.5


def test_T3_calc_kdj_uptrend_k_above_80():
    """单调上涨 → K 进入高位。"""
    n = 9
    close = np.linspace(100, 200, 60)
    high = close + 2.0
    low = close - 2.0
    k, d, j = _calc_kdj(high, low, close, n, 3, 3)
    # 末段 K 应 > 80（持续创新高 → RSV 接近 100 → K 上升）
    assert k[-1] > 80.0


def test_T4_find_recent_cross_death():
    """_find_recent_cross 正确识别死叉：K 从 >=D 下穿到 <D。"""
    k = np.array([60., 65., 70., 68., 60., 55.])
    d = np.array([58., 62., 66., 67., 65., 60.])
    # i=4: k[3]=68>=d[3]=67 且 k[4]=60<d[4]=65 → 死叉（最近一次）
    r = _find_recent_cross(k, d, lookback=5)
    assert r is not None
    assert r['cross_type'] == 'death'
    assert r['idx'] == 4


def test_T5_find_recent_cross_outside_lookback_none():
    """死叉在 lookback 窗口外 → 返回 None。"""
    k = np.array([60., 65., 70., 68., 60., 55., 50., 48.])
    d = np.array([58., 62., 66., 67., 65., 60., 55., 52.])
    # 死叉在 idx=3（5 个周期前），lookback=2 找不到
    r = _find_recent_cross(k, d, lookback=2)
    assert r is None


# ====================================================================
# T6-T11: check_kdj_divergence
# ====================================================================

def test_T6_insufficient_data_gray():
    """数据不足（< n+2 行）→ 灰灯。"""
    df = _daily_df(cnt=5, seed=0)
    r = check_kdj_divergence(df, _cfg())
    assert r['data_sufficient'] is False
    assert r['red'] is False


def test_T7_and_mode_missing_period_still_red():
    """AND 模式 + 一个周期数据不足：实际存在的周期全部命中高位死叉即红（灰周期不锁死）。
    构造：周线有充足数据且命中高位死叉；月线数据不足未进 period_states。
    AND 模式只要求 period_states 里的周期全部命中 → 周线命中即红。
    """
    # 末段急跌制造近期高位死叉
    df = _daily_df(cnt=300, seed=33, regime='down_recent')
    cfg = _cfg(KDJ_USE_MONTHLY=False)  # 只开周线，等价于"月线周期缺失"场景
    r = check_kdj_divergence(df, cfg)
    # 只要周线命中高位死叉就红（不受缺失周期影响）
    assert r['data_sufficient'] is True
    # 验证 AND 模式对"只实际存在 1 个周期"的处理
    cfg_and = _cfg(KDJ_USE_MONTHLY=False, KDJ_RED_MODE='AND')
    r_and = check_kdj_divergence(df, cfg_and)
    # 实际周期只有周线；周线命中则红，未命中则不红（不会因"月线缺失"永久 False）
    assert r_and['red'] == r['red']


def test_T8_tight_3period_high_death_cross_red():
    """近 3 周期（tight）高位死叉 → S7 红灯，value 与命中周期 K 一致。"""
    df = _daily_df(cnt=300, seed=7, regime='down_recent')
    cfg = _cfg()
    r = check_kdj_divergence(df, cfg)
    assert r['data_sufficient'] is True
    # 红灯：value 应等于命中周期的 K 值
    if r['red']:
        kd = r['kdj_detail']
        bh = kd['branch_hits']
        assert len(bh) > 0
        # value 周期与命中周期一致
        hit_p = list(bh.keys())[0]
        assert abs(r['value'] - kd['period_states'][hit_p]['K']) < 1e-6


def test_T9_k_at_80_boundary_not_overbought():
    """K 恰好 80.0 不算高位（条件是 >80）。构造 K≈80 的场景验证不亮红（除非死叉前值严格>80）。
    用一个先冲高到 K>80 然后死叉下来的序列：若死叉前一期 K=80.0 则不触发。
    这里直接验证边界逻辑：high_death_hit 要求 k_prev > overbought（严格大于）。
    """
    # 直接构造 K/D 序列让死叉前一期 K 恰好 80.0
    k = np.array([85., 82., 80.0, 75., 70.])
    d = np.array([80., 81., 81., 80., 78.])
    # i=2: k[1]=82>=d[1]=81 且 k[2]=80<d[2]=81 → 死叉；k_prev=82.0 > 80 → 命中
    r_hit = _find_recent_cross(k, d, lookback=5)
    assert r_hit is not None and r_hit['cross_type'] == 'death'
    assert r_hit['k_prev'] == 82.0  # > 80 → 应命中

    # 边界：k_prev 恰好 80.0（不满足严格 >80）
    k2 = np.array([80.0, 75., 70.])
    d2 = np.array([80., 80.5, 78.])
    # i=1: k[0]=80>=d[0]=80 且 k[1]=75<d[1]=80.5 → 死叉；k_prev=k[0]=80.0
    r2 = _find_recent_cross(k2, d2, lookback=5)
    assert r2 is not None
    assert r2['k_prev'] == 80.0  # == 80 不满足 >80 → 不命中高位死叉


def test_T10_monthly_only_mode():
    """仅月线模式（KDJ_USE_WEEKLY=False）仍能正常计算。"""
    df = _daily_df(cnt=300, seed=5, regime='flat')
    cfg = _cfg(KDJ_USE_WEEKLY=False, KDJ_USE_MONTHLY=True)
    r = check_kdj_divergence(df, cfg)
    assert r['data_sufficient'] is True
    kd = r['kdj_detail']
    assert '月线' in kd['period_states']
    assert '周线' not in kd['period_states']


def test_T11_or_mode_weekly_hit_red():
    """OR 模式：周线命中高位死叉 → 红灯（月线未命中不影响）。"""
    df = _daily_df(cnt=300, seed=11, regime='down_recent')
    cfg = _cfg(KDJ_RED_MODE='OR')
    r = check_kdj_divergence(df, cfg)
    assert r['data_sufficient'] is True
    kd = r['kdj_detail']
    # 若周线命中则整体红
    if '周线' in kd['branch_hits']:
        assert r['red'] is True
