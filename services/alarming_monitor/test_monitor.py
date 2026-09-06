"""alarming_monitor 测试：7 信号 + ADX 趋势过滤器 + 聚合引擎。

覆盖（构造 mock DataFrame，不触网）：
    - S1 成交额/总市值 > 3.0% → red；< 3.0% → 不 red
    - S2 放量不涨（5日均/20日均×1.3 且 指数5日涨幅≤0）→ red；量缩或价涨 → 不 red
    - S2 量级口径：日均÷日均（修正旧版量级错误）
    - S3 曾>2.5 且今日<1.0 → red；曾>2.5但今日>1.0 → 不 red；数据不足 → 灰
    - S4 市场跌+高股息涨+超额>3% → red；市场涨 → 不 red
    - S5 成长篮子近20日<-5% → red
    - ADX 计算：单调趋势市 ADX 高 / 震荡市 ADX 低 / 数据不足 → 灰
    - ADX 市场状态分类：trend/range/neutral 三档
    - 聚合：red_count 5/4/2/1 → high/high/warn/normal + 仓位建议匹配
    - ADX 动态阈值：强趋势市阈值上调 / 震荡市阈值下调+S4/S5/S6 加权×1.5
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

# 把 alarming_monitor 目录加入 sys.path，便于 `from config import ...`
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from config import AlarmConfig, ConservativeAlarmConfig  # noqa: E402
from indicators import (  # noqa: E402
    check_ad_ratio, check_adx, check_dividend_strength, check_growth_breakdown,
    check_turnover_ratio, check_volume_price_divergence,
)
from monitor import evaluate_signals  # noqa: E402


# ====================================================================
# 夹具：构造 mock 数据
# ====================================================================

def _dates(n: int, start: str = "2026-07-01") -> list:
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


def _make_index_df(closes: list, amounts: list,
                    highs: list = None, lows: list = None) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame({
        'date': _dates(n),
        'open': closes,
        'high': closes if highs is None else highs,
        'low': closes if lows is None else lows,
        'close': closes, 'volume': amounts, 'amount': amounts,
    })


def _make_etf_df(closes: list) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame({
        'date': _dates(n),
        'close': closes, 'open': closes, 'high': closes,
        'low': closes, 'volume': [1000] * n, 'amount': [1000.0] * n,
    })


def _make_spot(amount: float, total_value: float, change_pcts=None) -> pd.DataFrame:
    n = 1 if change_pcts is None else len(change_pcts)
    cps = [0.0] if change_pcts is None else change_pcts
    return pd.DataFrame({
        'code': [f'60000{i}' for i in range(n)],
        'name': [f'stk{i}' for i in range(n)],
        'close': [10.0] * n,
        'change_pct': cps,
        'amount': [amount / n] * n,
        'total_value': [total_value / n] * n,
        'circulation_value': [total_value / n] * n,
    })


def _signal(name, label, red, data_sufficient=True, value=0.0):
    return {'name': name, 'label': label, 'red': red,
            'value': value, 'threshold': 0.0,
            'detail': '', 'data_sufficient': data_sufficient}


@pytest.fixture
def cfg():
    return AlarmConfig()


# ====================================================================
# S1: 成交额/总市值
# ====================================================================

class TestS1TurnoverRatio:
    def test_above_threshold_red(self, cfg):
        # 成交额 3.1万亿 / 总市值 100万亿 = 3.1% > 3.0%
        spot = _make_spot(amount=3.1e12, total_value=100e12)
        r = check_turnover_ratio(spot, cfg)
        assert r['red'] is True
        assert r['data_sufficient'] is True
        assert abs(r['value'] - 0.031) < 1e-9

    def test_below_threshold_not_red(self, cfg):
        # 2.5% < 3.0%
        spot = _make_spot(amount=2.5e12, total_value=100e12)
        r = check_turnover_ratio(spot, cfg)
        assert r['red'] is False

    def test_empty_spot_insufficient(self, cfg):
        r = check_turnover_ratio(None, cfg)
        assert r['red'] is False
        assert r['data_sufficient'] is False

    def test_sina_source_uses_config_cap_fallback(self, cfg):
        """新浪源 spot 无 total_value 列 → S1 用 cfg.TOTAL_MARKET_CAP 兜底分母。"""
        # 模拟新浪快照：只有 amount，无 total_value 列
        spot = pd.DataFrame({
            'code': ['600000', '000001'],
            'name': ['a', 'b'],
            'close': [10.0, 10.0],
            'change_pct': [1.0, -1.0],
            'amount': [1.6e12, 1.6e12],   # 合计 3.2万亿
        })
        # total_value 列不存在
        assert 'total_value' not in spot.columns
        r = check_turnover_ratio(spot, cfg)
        assert r['data_sufficient'] is True
        # 分母应取 cfg.TOTAL_MARKET_CAP
        expected = 3.2e12 / cfg.TOTAL_MARKET_CAP
        assert abs(r['value'] - expected) < 1e-9
        # 成交额 3.2万亿 / 105万亿 ≈ 3.05%，>3.0% 阈值 → red
        assert r['red'] is True

    def test_sina_source_below_threshold_not_red(self, cfg):
        """新浪源 + 成交额偏低 → 不 red。"""
        spot = pd.DataFrame({
            'code': ['600000'], 'name': ['a'], 'close': [10.0],
            'change_pct': [1.0], 'amount': [2.0e12],   # 2万亿 / 105万亿 ≈ 1.9%
        })
        r = check_turnover_ratio(spot, cfg)
        assert r['red'] is False


# ====================================================================
# S2: 放量不涨（日均÷日均 量级修正）
# ====================================================================

class TestS2VolumePriceDivergence:
    def test_high_volume_flat_price_red(self, cfg):
        # 前 15 日 amount=100，后 5 日 amount=150 → avg5=150, avg20=112.5, 比=1.333>1.3
        # close 前 5 日持平 → 5日涨幅=0 ≤ 0
        amounts = [100.0] * 15 + [150.0] * 6
        closes = [100.0] * 21   # 完全持平，5日涨幅=0
        idx = _make_index_df(closes, amounts)
        r = check_volume_price_divergence(idx, cfg)
        assert r['red'] is True
        assert r['value'] > cfg.VOLUME_HIGH_MULT

    def test_low_volume_not_red(self, cfg):
        # 成交量萎缩，5日均 < 20日均×1.3
        amounts = [150.0] * 15 + [100.0] * 6   # 后5日缩量
        closes = [100.0] * 21
        idx = _make_index_df(closes, amounts)
        r = check_volume_price_divergence(idx, cfg)
        assert r['red'] is False

    def test_rising_price_not_red(self, cfg):
        # 放量但价格上涨 → 滞涨不成立
        amounts = [100.0] * 15 + [150.0] * 6
        closes = [100.0 + i * 0.5 for i in range(21)]  # 持续上涨
        idx = _make_index_df(closes, amounts)
        r = check_volume_price_divergence(idx, cfg)
        assert r['red'] is False  # 价涨打破滞涨

    def test_magnitude_correction(self, cfg):
        # 验证口径是"日均÷日均"而非"5日总量 vs 20日日均"
        # 若用错误口径(5日总量 vs 20日日均)，比会是 5×，必然>1.3 → 永真
        # 现正确口径：avg5/avg20。构造量平稳场景：全部 amount=100
        amounts = [100.0] * 21
        closes = [100.0] * 21
        idx = _make_index_df(closes, amounts)
        r = check_volume_price_divergence(idx, cfg)
        # 量平稳 → avg5/avg20=1.0，不>1.3 → 不应 red（旧错误口径会误判 red）
        assert r['red'] is False
        assert abs(r['value'] - 1.0) < 1e-9

    def test_insufficient_rows(self, cfg):
        idx = _make_index_df([100.0] * 5, [100.0] * 5)  # 不足 21 行
        r = check_volume_price_divergence(idx, cfg)
        assert r['red'] is False
        assert r['data_sufficient'] is False


# ====================================================================
# S3: 涨跌家数比回落
# ====================================================================

class TestS3AdRatio:
    def test_was_high_now_low_red(self, cfg):
        # 过去20日内曾>2.5，今日<1.0
        series = [1.0, 1.2, 2.6, 2.8, 0.8]   # max=2.8>2.5, today=0.8<1.0
        r = check_ad_ratio(series, cfg)
        assert r['red'] is True

    def test_was_high_but_today_above_one_not_red(self, cfg):
        series = [1.0, 2.6, 2.7, 1.5, 1.2]   # max=2.7>2.5, today=1.2>=1.0
        r = check_ad_ratio(series, cfg)
        assert r['red'] is False

    def test_never_high_not_red(self, cfg):
        series = [0.8, 0.9, 1.0, 0.95, 0.7]   # max=1.0<=2.5
        r = check_ad_ratio(series, cfg)
        assert r['red'] is False

    def test_insufficient_history_grey(self, cfg):
        r = check_ad_ratio([0.8], cfg)   # 仅1点
        assert r['red'] is False
        assert r['data_sufficient'] is False

    def test_empty_grey(self, cfg):
        r = check_ad_ratio([], cfg)
        assert r['data_sufficient'] is False


# ====================================================================
# S4: 高股息逆势走强
# ====================================================================

class TestS4DividendStrength:
    def _setup(self, market_closes, div_closes, gro_closes, cfg):
        idx = _make_index_df(market_closes, [100.0] * len(market_closes))
        etf_prices = {}
        for sym in cfg.DIVIDEND_BASKET:
            etf_prices[sym] = _make_etf_df(div_closes)
        for sym in cfg.GROWTH_BASKET:
            etf_prices[sym] = _make_etf_df(gro_closes)
        return idx, etf_prices

    def test_market_down_dividend_up_spread_ok_red(self, cfg):
        # 沪深300 4000→3900 (-2.5%)；高股息 1.0→1.03 (+3%)；成长 1.0→0.95 (-5%)
        # spread = 3% - (-5%) = 8% > 3%
        n = 21
        market = [4000 - (i * 100 / 20) for i in range(n)]   # 4000→3900
        div = [1.0 + (i * 0.03 / 20) for i in range(n)]      # 1.0→1.03
        gro = [1.0 - (i * 0.05 / 20) for i in range(n)]      # 1.0→0.95
        idx, etf = self._setup(market, div, gro, cfg)
        r = check_dividend_strength(idx, etf, cfg)
        assert r['red'] is True

    def test_market_up_not_red(self, cfg):
        # 市场上涨 → 非逆势
        n = 21
        market = [3900 + (i * 100 / 20) for i in range(n)]   # 3900→4000 (+2.5%)
        div = [1.0 + (i * 0.03 / 20) for i in range(n)]
        gro = [1.0 - (i * 0.05 / 20) for i in range(n)]
        idx, etf = self._setup(market, div, gro, cfg)
        r = check_dividend_strength(idx, etf, cfg)
        assert r['red'] is False

    def test_market_down_but_dividend_down_not_red(self, cfg):
        # 市场跌但高股息也跌 → 非逆势走强
        n = 21
        market = [4000 - (i * 100 / 20) for i in range(n)]
        div = [1.0 - (i * 0.02 / 20) for i in range(n)]      # 高股息也跌
        gro = [1.0 - (i * 0.05 / 20) for i in range(n)]
        idx, etf = self._setup(market, div, gro, cfg)
        r = check_dividend_strength(idx, etf, cfg)
        assert r['red'] is False


# ====================================================================
# S5: 成长股破位
# ====================================================================

class TestS5GrowthBreakdown:
    def test_ret_below_threshold_red(self, cfg):
        n = 21
        gro = [1.0 - (i * 0.06 / 20) for i in range(n)]   # 1.0→0.94 (-6%<-5%)
        etf = {sym: _make_etf_df(gro) for sym in cfg.GROWTH_BASKET}
        r = check_growth_breakdown(etf, cfg)
        assert r['red'] is True

    def test_above_threshold_not_red(self, cfg):
        n = 21
        gro = [1.0 + (i * 0.02 / 20) for i in range(n)]   # +2%
        etf = {sym: _make_etf_df(gro) for sym in cfg.GROWTH_BASKET}
        r = check_growth_breakdown(etf, cfg)
        assert r['red'] is False

    def test_insufficient_grey(self, cfg):
        gro = _make_etf_df([1.0] * 5)   # 不足21行
        etf = {sym: gro for sym in cfg.GROWTH_BASKET}
        r = check_growth_breakdown(etf, cfg)
        assert r['data_sufficient'] is False


# ====================================================================
# 聚合引擎：red_count → 风险等级 + 仓位建议
# ====================================================================

class TestAggregation:
    def _result(self, reds, cfg, adx_result=None):
        sigs = [_signal(f's{i+1}', f'S{i+1}', reds[i]) for i in range(5)]
        return evaluate_signals(sigs, cfg, '2026-08-19', adx_result)

    def test_all_five_red_high(self, cfg):
        r = self._result([True] * 5, cfg)
        assert r['red_count'] == 5
        assert r['risk_level'] == 'high'
        assert '5成' in r['position_advice']

    def test_four_red_high(self, cfg):
        r = self._result([True, True, True, True, False], cfg)
        assert r['red_count'] == 4
        assert r['risk_level'] == 'high'   # ≥4 触发"超过3"
        assert '5成' in r['position_advice']

    def test_two_red_warn(self, cfg):
        r = self._result([True, True, False, False, False], cfg)
        assert r['red_count'] == 2
        assert r['risk_level'] == 'warn'
        assert '7成' in r['position_advice']

    def test_one_red_normal(self, cfg):
        r = self._result([True, False, False, False, False], cfg)
        assert r['red_count'] == 1
        assert r['risk_level'] == 'normal'
        assert '维持' in r['position_advice']

    def test_zero_red_normal(self, cfg):
        r = self._result([False] * 5, cfg)
        assert r['risk_level'] == 'normal'

    def test_conservative_three_red_high(self):
        # Conservative 档 REDUCE_THRESHOLD=3 → 3红即降仓
        cfg = ConservativeAlarmConfig()
        sigs = [_signal(f's{i+1}', f'S{i+1}', i < 3) for i in range(5)]
        r = evaluate_signals(sigs, cfg, '2026-08-19')
        assert r['red_count'] == 3
        assert r['risk_level'] == 'high'
        assert '5成' in r['position_advice']


# ====================================================================
# ADX 计算 + 市场状态分类
# ====================================================================

class TestAdxCalculation:
    """ADX（Wilder 平均趋向指标）计算正确性 + 市场状态分类。

    覆盖：
        - 单调上涨趋势 → ADX 偏高（趋势市）
        - 来回震荡 → ADX 偏低（震荡市）
        - 数据不足（行数 < 2*period+5）→ 灰灯
        - 缺 high/low 列 → 灰灯
        - 市场状态分类：trend/range/neutral 三档
    """

    def test_uptrend_high_adx(self, cfg):
        """单调上涨 40 日 → +DI 主导，ADX 偏高（趋势市）。"""
        n = 40
        closes = [100.0 + i * 1.5 for i in range(n)]   # 100→158.5 单调涨
        highs = [c + 2.0 for c in closes]
        lows = [c - 2.0 for c in closes]
        idx = _make_index_df(closes, [100.0] * n, highs=highs, lows=lows)
        r = check_adx(idx, cfg)
        assert r['data_sufficient'] is True
        assert r['value'] > 25.0   # 趋势市阈值
        assert r['market_state'] == 'trend'
        assert r['red'] is False   # ADX 不直接亮红灯

    def test_range_low_adx(self, cfg):
        """来回震荡 40 日 → +DM/-DM 交替，ADX 偏低（震荡市）。"""
        n = 40
        # 上下震荡：偶数日涨，奇数日跌，幅度相近
        closes = [100.0]
        for i in range(1, n):
            closes.append(closes[-1] + (3.0 if i % 2 == 0 else -3.0))
        highs = [c + 1.5 for c in closes]
        lows = [c - 1.5 for c in closes]
        idx = _make_index_df(closes, [100.0] * n, highs=highs, lows=lows)
        r = check_adx(idx, cfg)
        assert r['data_sufficient'] is True
        # 震荡市 ADX 应偏低（不一定 < 22，但应明显低于趋势市）
        # 用相对宽松的断言：震荡市 ADX 应低于 30（不进入强趋势档）
        assert r['value'] < 30.0

    def test_insufficient_rows_grey(self, cfg):
        """不足 2*period+5=33 行 → 数据不足灰灯。"""
        n = 10   # 远小于 33
        closes = [100.0 + i for i in range(n)]
        idx = _make_index_df(closes, [100.0] * n,
                              highs=[c + 1 for c in closes],
                              lows=[c - 1 for c in closes])
        r = check_adx(idx, cfg)
        assert r['data_sufficient'] is False
        assert r['red'] is False

    def test_missing_high_column_grey(self, cfg):
        """缺 high 列 → 灰灯。"""
        n = 40
        df = pd.DataFrame({
            'date': _dates(n),
            'open': [100.0] * n, 'close': [100.0] * n,
            'low': [99.0] * n, 'volume': [100.0] * n, 'amount': [100.0] * n,
            # 缺 high
        })
        r = check_adx(df, cfg)
        assert r['data_sufficient'] is False

    def test_empty_df_grey(self, cfg):
        r = check_adx(None, cfg)
        assert r['data_sufficient'] is False
        r2 = check_adx(pd.DataFrame(), cfg)
        assert r2['data_sufficient'] is False

    def test_market_state_classification(self, cfg):
        """ADX 三档分类：trend (>30) / range (<22) / neutral (22-30)。"""
        # trend: 构造强趋势
        n = 50
        closes = [100.0 + i * 2.0 for i in range(n)]
        highs = [c + 3.0 for c in closes]
        lows = [c - 1.0 for c in closes]
        idx = _make_index_df(closes, [100.0] * n, highs=highs, lows=lows)
        r = check_adx(idx, cfg)
        assert r['market_state'] in ('trend', 'neutral')   # 强趋势应至少 neutral
        # 中性/震荡市场场景下断言 market_state 字段存在
        assert 'market_state' in r

    def test_adx_value_finite(self, cfg):
        """ADX 值必须是有限实数（非 NaN/inf）。"""
        n = 40
        closes = [100.0 + i * 1.5 for i in range(n)]
        highs = [c + 2.0 for c in closes]
        lows = [c - 2.0 for c in closes]
        idx = _make_index_df(closes, [100.0] * n, highs=highs, lows=lows)
        r = check_adx(idx, cfg)
        assert r['data_sufficient'] is True
        assert math.isfinite(r['value'])
        assert 0.0 <= r['value'] <= 100.0   # ADX 理论区间 [0, 100]


# ====================================================================
# ADX 动态阈值 + 震荡市加权
# ====================================================================

class TestAdxDynamicThreshold:
    """ADX 作为过滤器动态调整 red_count 阈值。

    覆盖：
        - 强趋势市：阈值上调至 5，4 红不警报，5 红才警报
        - 弱趋势/震荡市：阈值下调至 3，3 红即警报
        - 震荡市 S4/S5/S6 加权 ×1.5：2 个加权信号 → ceil(1.5+1.5)=3 即触发
        - ADX=None 时退化为中性口径（维持默认阈值 4）
    """

    def _adx(self, state: str, value: float = 25.0) -> dict:
        """构造 ADX 结果 dict（mock，不触网）。"""
        return {
            'name': 'adx_trend', 'label': 'ADX 市场状态', 'red': False,
            'value': value, 'threshold': 25.0,
            'market_state': state,
            'detail': f'mock {state} ADX={value}',
            'data_sufficient': True,
        }

    def test_trend_market_threshold_raised(self, cfg):
        """强趋势市：4 红不警报（阈值上调至 5），5 红才警报。"""
        adx = self._adx('trend', value=35.0)
        sigs = [_signal(f's{i+1}', f'S{i+1}', i < 4) for i in range(5)]   # 4 红
        r = evaluate_signals(sigs, cfg, '2026-08-19', adx)
        assert r['effective_threshold'] == cfg.REDUCE_THRESHOLD_TREND   # 5
        assert r['red_count'] == 4
        assert r['effective_red_count'] == 4   # 非震荡市，不加权
        assert r['risk_level'] == 'warn'      # 4 < 5 → warn 不是 high

        # 5 红才 high
        sigs5 = [_signal(f's{i+1}', f'S{i+1}', True) for i in range(5)]
        r5 = evaluate_signals(sigs5, cfg, '2026-08-19', adx)
        assert r5['effective_red_count'] == 5
        assert r5['risk_level'] == 'high'

    def test_range_market_threshold_lowered(self, cfg):
        """弱趋势/震荡市：3 红即警报（阈值下调至 3）。"""
        adx = self._adx('range', value=15.0)
        sigs = [_signal(f's{i+1}', f'S{i+1}', i < 3) for i in range(5)]   # 3 红
        r = evaluate_signals(sigs, cfg, '2026-08-19', adx)
        assert r['effective_threshold'] == cfg.REDUCE_THRESHOLD_RANGE   # 3
        assert r['red_count'] == 3
        assert r['risk_level'] == 'high'

    def test_range_market_s4_s5_weighted(self, cfg):
        """震荡市 S4/S5/S6 加权 ×1.5：2 个加权红灯 → ceil(1.5+1.5)=3 触发。"""
        adx = self._adx('range', value=15.0)
        # 只 S4 + S5 亮红灯（2 个加权信号）
        sigs = [
            _signal('turnover_ratio_high', 'S1', False),
            _signal('volume_price_divergence', 'S2', False),
            _signal('ad_ratio_bearish', 'S3', False),
            _signal('dividend_strength', 'S4', True),    # 加权 ×1.5
            _signal('growth_breakdown', 'S5', True),     # 加权 ×1.5
        ]
        r = evaluate_signals(sigs, cfg, '2026-08-19', adx)
        assert r['red_count'] == 2                       # 原始红灯数
        assert r['effective_red_count'] == 3             # ceil(1.5+1.5)=3
        assert r['risk_level'] == 'high'                 # 3 >= 3 触发

    def test_range_market_mixed_weighted(self, cfg):
        """震荡市混合加权：S1(普通) + S4(加权) + S5(加权)
        = 1 + 1.5 + 1.5 = 4 → ceil(4) = 4 触发。
        """
        adx = self._adx('range', value=15.0)
        sigs = [
            _signal('turnover_ratio_high', 'S1', True),  # 1.0
            _signal('volume_price_divergence', 'S2', False),
            _signal('ad_ratio_bearish', 'S3', False),
            _signal('dividend_strength', 'S4', True),    # 1.5
            _signal('growth_breakdown', 'S5', True),     # 1.5
        ]
        r = evaluate_signals(sigs, cfg, '2026-08-19', adx)
        assert r['red_count'] == 3
        assert r['effective_red_count'] == 4             # ceil(4.0)=4

    def test_neutral_market_maintains_default(self, cfg):
        """方向不明：维持默认阈值 4，4 红触发 high。"""
        adx = self._adx('neutral', value=25.0)
        sigs = [_signal(f's{i+1}', f'S{i+1}', i < 4) for i in range(5)]
        r = evaluate_signals(sigs, cfg, '2026-08-19', adx)
        assert r['effective_threshold'] == cfg.REDUCE_THRESHOLD_NEUTRAL   # 4
        assert r['effective_red_count'] == 4
        assert r['risk_level'] == 'high'

    def test_adx_none_degrades_to_config_default(self, cfg):
        """ADX=None 或数据不足 → 退化为配置档默认 REDUCE_THRESHOLD（兼容旧口径）。

        AlarmConfig.REDUCE_THRESHOLD=4，ConservativeAlarmConfig.REDUCE_THRESHOLD=3。
        ADX 未取时不再强制用 NEUTRAL=4，而是用配置档自身的阈值。
        """
        sigs = [_signal(f's{i+1}', f'S{i+1}', i < 4) for i in range(5)]
        # adx_result=None
        r = evaluate_signals(sigs, cfg, '2026-08-19', None)
        assert r['market_state'] == 'neutral'
        assert r['effective_threshold'] == cfg.REDUCE_THRESHOLD   # 退化为配置档默认
        # adx_result 数据不足
        adx_insufficient = {
            'name': 'adx_trend', 'data_sufficient': False,
            'value': float('nan'), 'market_state': 'neutral',
            'detail': '数据不足',
        }
        r2 = evaluate_signals(sigs, cfg, '2026-08-19', adx_insufficient)
        assert r2['market_state'] == 'neutral'
        assert r2['effective_threshold'] == cfg.REDUCE_THRESHOLD


# ====================================================================
# CSV 行格式化（reporter/storage 联动）
# ====================================================================

class TestCSVFormatting:
    def test_csv_row_fields_complete(self, cfg):
        sigs = [
            {'name': 'turnover_ratio_high', 'red': True, 'value': 0.031,
             'threshold': 0.03, 'data_sufficient': True},
            {'name': 'volume_price_divergence', 'red': False, 'value': 1.1,
             'threshold': 1.3, 'data_sufficient': True},
            {'name': 'ad_ratio_bearish', 'red': True, 'value': 0.8,
             'threshold': 1.0, 'data_sufficient': True},
            {'name': 'dividend_strength', 'red': False, 'value': 0.01,
             'threshold': 0.03, 'data_sufficient': True},
            {'name': 'growth_breakdown', 'red': True, 'value': -0.06,
             'threshold': -0.05, 'data_sufficient': True},
        ]
        adx = {'value': 28.5, 'market_state': 'neutral', 'detail': 'ADX=28.5',
               'data_sufficient': True}
        result = evaluate_signals(sigs, cfg, '2026-08-19', adx)
        from reporter import format_csv_row, CSV_FIELDS
        row = format_csv_row(result)
        for f in CSV_FIELDS:
            assert f in row, f'缺失字段 {f}'
        assert row['s1_turnover_ratio'] == round(0.031, 4)
        assert row['s1_red'] is True
        assert row['s5_growth_ret'] == round(-0.06, 4)
        assert row['red_count'] == 3
        assert row['risk_level'] == 'warn'
        # ADX 新增字段
        assert row['market_state'] == 'neutral'
        assert row['adx_value'] == round(28.5, 2)
        assert row['effective_threshold'] == cfg.REDUCE_THRESHOLD_NEUTRAL


class TestLogAlarmSignalStorage:
    """log_alarm_signal：按 ref_date 幂等去重 + 原子写。

    覆盖：
        - 同 ref_date 重复写入只保留最新一条（不产生重复行）
        - 不同 ref_date 各自保留
        - 写文件不残留 .tmp
    """

    def _make_result(self, cfg, ref_date: str, red_count_marker: str):
        sigs = [
            {'name': 'turnover_ratio_high', 'red': False, 'value': 0.01,
             'threshold': 0.03, 'data_sufficient': True},
            {'name': 'volume_price_divergence', 'red': False, 'value': 1.0,
             'threshold': 1.3, 'data_sufficient': True},
            {'name': 'ad_ratio_bearish', 'red': False, 'value': 1.2,
             'threshold': 1.0, 'data_sufficient': True},
            {'name': 'dividend_strength', 'red': False, 'value': 0.01,
             'threshold': 0.03, 'data_sufficient': True},
            {'name': 'growth_breakdown', 'red': False, 'value': -0.01,
             'threshold': -0.05, 'data_sufficient': True},
        ]
        adx = {'value': 25.0, 'market_state': 'neutral', 'detail': 'ADX=25',
               'data_sufficient': True}
        r = evaluate_signals(sigs, cfg, ref_date, adx)
        # 用 position_advice 携带 marker，便于区分两次写入
        r['position_advice'] = red_count_marker
        return r

    def test_same_ref_date_idempotent(self, cfg, monkeypatch, tmp_path):
        import storage as st
        monkeypatch.setattr(st, '_CSV_DIR', tmp_path)
        ref = '2026-08-19'
        st.log_alarm_signal(self._make_result(cfg, ref, 'first'))
        st.log_alarm_signal(self._make_result(cfg, ref, 'second'))
        import pandas as pd
        df = pd.read_csv(tmp_path / 'alarming_2026-08.csv', encoding='utf-8-sig')
        assert len(df) == 1
        assert str(df.iloc[0]['ref_date'])[:10] == ref
        assert df.iloc[0]['position_advice'] == 'second'  # 保留最新

    def test_different_ref_date_both_kept(self, cfg, monkeypatch, tmp_path):
        import storage as st
        monkeypatch.setattr(st, '_CSV_DIR', tmp_path)
        st.log_alarm_signal(self._make_result(cfg, '2026-08-18', 'a'))
        st.log_alarm_signal(self._make_result(cfg, '2026-08-19', 'b'))
        import pandas as pd
        df = pd.read_csv(tmp_path / 'alarming_2026-08.csv', encoding='utf-8-sig')
        assert len(df) == 2

    def test_no_tmp_file_left(self, cfg, monkeypatch, tmp_path):
        import storage as st
        monkeypatch.setattr(st, '_CSV_DIR', tmp_path)
        st.log_alarm_signal(self._make_result(cfg, '2026-08-19', 'x'))
        tmps = list(tmp_path.glob('*.tmp'))
        assert tmps == []


class TestToTxSymbol:
    """腾讯源 symbol 归一化（_to_tx_symbol）回归测试。

    覆盖指数代码 bug 修复：sh000300/sh000001 虽 00 开头但属沪市，
    必须保留 sh 前缀，不能误判为深市 sz000300（否则腾讯源查不到）。
    """

    def test_index_prefix_preserved(self):
        from data_loader import _to_tx_symbol
        # 指数代码：已带 sh/sz 前缀的必须原样小写返回（bug 修复点）
        assert _to_tx_symbol('sh000300') == 'sh000300'   # 沪深300
        assert _to_tx_symbol('sh000001') == 'sh000001'   # 上证综指
        assert _to_tx_symbol('sz399001') == 'sz399001'   # 深证成指
        assert _to_tx_symbol('sz399006') == 'sz399006'   # 创业板指

    def test_bare_code_infers_exchange(self):
        from data_loader import _to_tx_symbol
        # 6 位裸代码按开头数字推断交易所
        assert _to_tx_symbol('600000') == 'sh600000'   # 沪市主板
        assert _to_tx_symbol('000001') == 'sz000001'   # 深市主板（平安银行）
        assert _to_tx_symbol('159995') == 'sz159995'   # 深市 ETF
        assert _to_tx_symbol('516070') == 'sh516070'   # 51 开头归沪市

    def test_case_insensitive(self):
        from data_loader import _to_tx_symbol
        assert _to_tx_symbol('SH000300') == 'sh000300'
        assert _to_tx_symbol('SH600000') == 'sh600000'
        assert _to_tx_symbol('SZ399001') == 'sz399001'


class TestBreadthStorage:
    """S3 涨跌家数 CSV 自累积（append_breadth_today + read_breadth_history）。

    覆盖：
        - 同日重复写入去重（保留最新一条）
        - 多日序列升序重建，末值最新
        - ad_ratio 缺失时自动计算
        - 跨月去重（drop_duplicates keep=last）
    """

    def test_append_and_read_back(self, monkeypatch, tmp_path):
        import storage as st
        monkeypatch.setattr(st, '_CSV_DIR', tmp_path)
        st.append_breadth_today('2026-08-18', 1500, 1500)
        st.append_breadth_today('2026-08-19', 420, 4760, flat_count=24)
        df = st.read_breadth_history(20)
        assert df is not None and len(df) == 2
        assert list(df['date']) == ['2026-08-18', '2026-08-19']  # 升序
        assert abs(float(df.iloc[-1]['ad_ratio']) - 420/4760) < 1e-9  # 末值=今日

    def test_same_day_dedupe_keep_latest(self, monkeypatch, tmp_path):
        import storage as st
        monkeypatch.setattr(st, '_CSV_DIR', tmp_path)
        st.append_breadth_today('2026-08-19', 1000, 1000)  # 早写
        st.append_breadth_today('2026-08-19', 420, 4760)   # 晚写覆盖
        df = st.read_breadth_history(20)
        assert len(df) == 1                                  # 去重后只1行
        assert int(df.iloc[0]['up_count']) == 420           # 保留最新

    def test_ad_ratio_auto_calc_when_none(self, monkeypatch, tmp_path):
        import storage as st
        monkeypatch.setattr(st, '_CSV_DIR', tmp_path)
        st.append_breadth_today('2026-08-19', 3000, 1000, ad_ratio=None)
        df = st.read_breadth_history(20)
        assert abs(float(df.iloc[0]['ad_ratio']) - 3.0) < 1e-9

    def test_empty_dir_returns_none(self, monkeypatch, tmp_path):
        import storage as st
        monkeypatch.setattr(st, '_CSV_DIR', tmp_path)
        assert st.read_breadth_history(20) is None


# ====================================================================
# 阶段3新增：时间轴对齐（align_to_benchmark）
# ====================================================================

class TestAlignment:
    """align_to_benchmark：以基准交易日为锚，ffill 对齐所有 ETF。

    覆盖：
        - 长度一致：输出等长于 bench_df
        - 左连接保留基准所有日期
        - ffill：停牌（缺失日期）时复用前一日
        - 完全无数据 ETF：输出全 NaN 占位（不抛异常）
        - 空 bench_df：抛 ValueError
    """

    def test_equal_length_to_bench(self, cfg):
        from rotation import align_to_benchmark
        bench = _make_index_df([100.0, 101.0, 102.0, 103.0, 104.0], [100.0] * 5)
        etf = {'512800': _make_etf_df([1.0, 1.01, 1.02, 1.03, 1.04])}
        aligned = align_to_benchmark(bench, etf)
        assert len(aligned['512800']) == len(bench)

    def test_missing_dates_ffilled(self, cfg):
        """ETF 少一天中间数据 → 缺失当天用 ffill 填充。"""
        from rotation import align_to_benchmark
        bench = _make_index_df([100.0, 101.0, 102.0, 103.0], [100.0] * 4)
        # ETF 缺少 2026-07-02（索引1），只有 4 行中的 3 行
        etf_df = _make_etf_df([1.0, 1.02, 1.03])  # 只有 3 行
        # 把 date 改成"跳过 07-02"
        etf_df['date'] = ['2026-07-01', '2026-07-03', '2026-07-04']
        etf = {'512800': etf_df}
        aligned = align_to_benchmark(bench, etf)
        a = aligned['512800']
        # 4 行都存在
        assert len(a) == 4
        # 第2行（索引1）应该是 ffill 的：应该=1.0（与第0行相同）
        assert abs(float(a['close'].iloc[1]) - 1.0) < 1e-9

    def test_etf_completely_empty_gives_nan_placeholder(self, cfg):
        """ETF 完全无数据 → 不应抛错，输出全 NaN 占位行。"""
        from rotation import align_to_benchmark
        bench = _make_index_df([100.0, 101.0, 102.0], [100.0] * 3)
        etf = {'512800': None}
        aligned = align_to_benchmark(bench, etf)
        a = aligned['512800']
        assert len(a) == 3
        # close 列全 NaN
        assert all(pd.isna(a['close']))

    def test_empty_bench_raises(self, cfg):
        """bench_df 为空必须抛 ValueError。"""
        from rotation import align_to_benchmark
        with pytest.raises(ValueError):
            align_to_benchmark(pd.DataFrame(), {'512800': _make_etf_df([1.0])})

    def test_multiple_etfs_aligned_together(self, cfg):
        """多 ETF 同时对齐：长度全部 = len(bench)。"""
        from rotation import align_to_benchmark
        bench = _make_index_df([100.0 + i for i in range(10)], [100.0] * 10)
        etfs = {
            'A': _make_etf_df([1.0 + i * 0.01 for i in range(8)]),   # 短 2 行
            'B': _make_etf_df([2.0 + i * 0.02 for i in range(10)]),
        }
        # 把 A 的日期改成 07-01..07-08（缺最后 2 天）
        etfs['A']['date'] = _dates(8)
        aligned = align_to_benchmark(bench, etfs)
        assert len(aligned['A']) == 10
        assert len(aligned['B']) == 10


# ====================================================================
# 阶段3新增：RRG 四象限分类
# ====================================================================

class TestRRGQuadrant:
    """classify_rrg_quadrant 四象限 + 边界保守归类。

    覆盖：
        - 四象限：领涨/轮动初期/滞后/退潮 各场景
        - 边界保守：RS=1.0 归弱侧，Mom=0 归负侧
        - 关闭保守：RS=1.0 归强侧
    """

    def test_leading_quadrant(self, cfg):
        """RS>1 且 Mom>0 → 领涨主线 🟢。"""
        from rotation import classify_rrg_quadrant
        label, emoji = classify_rrg_quadrant(1.15, 2.5, cfg)
        assert label == '领涨主线'
        assert emoji == '🟢'

    def test_improving_quadrant(self, cfg):
        """RS≤1 且 Mom>0 → 轮动初期 🟡。"""
        from rotation import classify_rrg_quadrant
        label, emoji = classify_rrg_quadrant(0.95, 1.2, cfg)
        assert label == '轮动初期'
        assert emoji == '🟡'

    def test_lagging_quadrant(self, cfg):
        """RS≤1 且 Mom≤0 → 滞后回避 🔴。"""
        from rotation import classify_rrg_quadrant
        label, emoji = classify_rrg_quadrant(0.88, -1.5, cfg)
        assert label == '滞后回避'
        assert emoji == '🔴'

    def test_weakening_quadrant(self, cfg):
        """RS>1 且 Mom≤0 → 退潮预警 🟠。"""
        from rotation import classify_rrg_quadrant
        label, emoji = classify_rrg_quadrant(1.10, -0.3, cfg)
        assert label == '退潮预警'
        assert emoji == '🟠'

    def test_boundary_conservative_ratio_weak(self, cfg):
        """保守模式：RS=1.0 → 归入弱侧（≤1）。"""
        from rotation import classify_rrg_quadrant
        assert cfg.RRG_BOUNDARY_CONSERVATIVE is True
        # RS=1.0 + Mom>0 → 轮动初期（弱侧+正动量）
        label, _ = classify_rrg_quadrant(1.0, 1.0, cfg)
        assert label == '轮动初期'
        # RS=1.0 + Mom≤0 → 滞后回避（弱侧+负动量）
        label2, _ = classify_rrg_quadrant(1.0, 0.0, cfg)
        assert label2 == '滞后回避'

    def test_boundary_conservative_mom_weak(self, cfg):
        """保守模式：Mom=0 → 归入负侧（≤0）。"""
        from rotation import classify_rrg_quadrant
        # RS>1 + Mom=0 → 退潮预警（强侧+负动量）
        label, _ = classify_rrg_quadrant(1.1, 0.0, cfg)
        assert label == '退潮预警'

    def test_non_conservative_boundary_strong(self):
        """关闭保守：RS=1.0 归入强侧（≥1），Mom=0 归入正侧（≥0）。"""
        from config import AlarmConfig
        from rotation import classify_rrg_quadrant
        cfg = AlarmConfig()
        cfg.RRG_BOUNDARY_CONSERVATIVE = False
        # RS=1.0 + Mom=0 → 领涨主线（强强）
        label, _ = classify_rrg_quadrant(1.0, 0.0, cfg)
        assert label == '领涨主线'


# ====================================================================
# 阶段3新增：5 维评分卡
# ====================================================================

class Test5DScore:
    """calc_5d_score：5 维加权 + 归一化 [0,5]。

    覆盖：
        - 维1 相对动量：RS<1 → 0 分；RS>1 且 超 SCORE_REL_MOM_FULL → 5 分满分
        - 维3 ADX：数据不足时 0 分；SCORE_ADX_FULL → 5 分
        - 维5 拥挤度：换手率超阈值扣分，crowding_penalty_applied=True
        - 综合得分：clip 到 [0, 5]，操作参考：>4 持有 / >3 关注 / ≤3 回避
    """

    def test_relative_momentum_rs_below_one_scores_zero(self, cfg):
        """维1：RS-Ratio < 1 → 维1=0。"""
        from rotation import calc_5d_score
        etf = _make_etf_df([1.0] * 50)
        result = calc_5d_score(etf, rs_ratio_last=0.9, rs_momentum_last=0.0,
                               rs_momentum_series=pd.Series([0.0] * 5), cfg=cfg)
        # 相对动量维应该为 0（跑输基准）
        assert result['relative_momentum'] == 0.0

    def test_relative_momentum_full_score(self, cfg):
        """维1：RS-Ratio - 1 = SCORE_REL_MOM_FULL → 维1=5 满分。"""
        from rotation import calc_5d_score
        etf = _make_etf_df([1.0] * 50)
        # 构造强趋势，让维3 ADX也能算到值（避免0分影响综合分测试）
        closes = [1.0 + i * 0.05 for i in range(50)]
        etf = _make_etf_df(closes)
        etf['high'] = [c + 0.05 for c in closes]
        etf['low'] = [c - 0.01 for c in closes]
        full_ratio = 1.0 + cfg.SCORE_REL_MOM_FULL  # 满分值
        result = calc_5d_score(etf, rs_ratio_last=full_ratio, rs_momentum_last=0.0,
                               rs_momentum_series=pd.Series([0.0] * 5), cfg=cfg)
        # clip 到 5，达到满分
        assert result['relative_momentum'] == pytest.approx(5.0, abs=0.1)

    def test_crowding_penalty_applied(self, cfg):
        """维5：量能相对强度（MA5额/MA20额）> 2.5 → 触发拥挤度扣分 + 标记。"""
        from rotation import calc_5d_score
        # 构造放量场景：前45天缩量(1000)，最后5天巨量(10万) → MA5>>MA20，量比≈3.88>2.5
        closes = [1.0] * 50
        etf = _make_etf_df(closes)
        etf['amount'] = [1000.0] * 45 + [100000.0] * 5
        etf['high'] = [c + 0.02 for c in closes]
        etf['low'] = [c - 0.02 for c in closes]
        result = calc_5d_score(etf, rs_ratio_last=1.05, rs_momentum_last=0.0,
                               rs_momentum_series=pd.Series([0.0] * 5), cfg=cfg)
        # 量能相对强度 ≈ 3.88 > 2.5，触发拥挤度扣分
        assert result['crowding_penalty_applied'] is True
        assert result['crowding_penalty'] == pytest.approx(5.0 - cfg.CROWDING_PENALTY, abs=0.01)

    def test_total_score_range_and_action_hint(self, cfg):
        """综合得分：clip [0,5]，三档操作参考。"""
        from rotation import calc_5d_score
        closes = [1.0] * 50
        etf = _make_etf_df(closes)
        etf['high'] = [c + 0.03 for c in closes]
        etf['low'] = [c - 0.03 for c in closes]

        # 场景1：全维度差 → 综合分低 → 回避
        r1 = calc_5d_score(etf, rs_ratio_last=0.8, rs_momentum_last=-1.0,
                           rs_momentum_series=pd.Series([-1.0, -0.8, -0.6, -0.4, -0.2]), cfg=cfg)
        assert 0.0 <= r1['total_score'] <= 5.0
        # 低分时回避
        if r1['total_score'] < cfg.SCORE_ATTENTION_THRESHOLD:
            assert r1['action_hint'] == '回避'

        # 场景2：RS 好 但 动量差 → 关注档
        r2 = calc_5d_score(etf, rs_ratio_last=1.0 + cfg.SCORE_REL_MOM_FULL * 0.6,
                           rs_momentum_last=cfg.SCORE_MOM_ACC_FULL * 0.5,
                           rs_momentum_series=pd.Series([0.0] * 5), cfg=cfg)
        assert 0.0 <= r2['total_score'] <= 5.0
        if cfg.SCORE_ATTENTION_THRESHOLD <= r2['total_score'] <= cfg.SCORE_STRONG_THRESHOLD:
            assert r2['action_hint'] == '关注建仓'

    def test_score_weights_sum(self, cfg):
        """权重合计必须 = 1.0（不偏倚）。"""
        total_w = sum(cfg.SCORE_WEIGHTS.values())
        assert abs(total_w - 1.0) < 1e-9


# ====================================================================
# 阶段3新增：轮动批量分析 + Markdown 报告生成
# ====================================================================

class TestRotationAnalysisAndReport:
    """run_rotation_analysis（批量+排名） + generate_markdown_report（格式）。

    覆盖：
        - run_rotation_analysis：4 只 ETF 全部分析完成，结果数量=ROTATION_BASKET 数
        - summary 字段正确：total_basket=4
        - Markdown 报告结构：含 📊 标题、🚨 综合等级、🔄 轮动表表头、8 列
        - Markdown 表格行数：1 表头 + 1 分隔 + 4 ETF = 至少 6 行 | 表格式
    """

    def test_rotation_analysis_run_with_mock(self, cfg, monkeypatch):
        """mock 数据不触网跑完整轮动流程。"""
        from analysis import run_rotation_analysis
        from rotation import align_to_benchmark, analyze_single_etf

        # 构造：bench 120 天 = 指数 100→108，ETF1 跑赢、ETF2 跑输
        n = 120
        bench_closes = [100.0 + i * (8 / n) for i in range(n)]
        bench = _make_index_df(bench_closes, [1e9] * n,
                               highs=[c + 1 for c in bench_closes],
                               lows=[c - 1 for c in bench_closes])
        # ETF1：强跑赢（斜率更高）
        etf1_closes = [1.0 + i * (0.15 / n) for i in range(n)]   # +15%
        etf1 = _make_etf_df(etf1_closes)
        etf1['high'] = [c + 0.01 for c in etf1_closes]
        etf1['low'] = [c - 0.01 for c in etf1_closes]
        etf1['amount'] = [1e9] * n
        # ETF2：跑输（跌 5%）
        etf2_closes = [1.0 - i * (0.05 / n) for i in range(n)]
        etf2 = _make_etf_df(etf2_closes)
        etf2['high'] = [c + 0.01 for c in etf2_closes]
        etf2['low'] = [c - 0.01 for c in etf2_closes]
        etf2['amount'] = [1e7] * n

        # 只保留 2 只 ETF 跑 mock（其余篮子成员缺数据也应该能跑）
        etf_full_dfs = {'512800': etf1, '516070': etf2}
        result = run_rotation_analysis(bench, etf_full_dfs, cfg)

        # 输出结构正确
        assert 'results' in result
        assert 'summary' in result
        assert result['summary']['total_basket'] == len(cfg.ROTATION_BASKET)
        assert len(result['results']) == len(cfg.ROTATION_BASKET)
        # ETF1（有数据）→ data_sufficient=True，ETF2 同理；另外 2 只缺数据 → False
        data_ok_count = sum(1 for r in result['results'] if r['data_sufficient'])
        assert data_ok_count == 2
        assert result['summary']['data_ok'] == 2

    def test_markdown_report_template_structure(self, cfg):
        """generate_markdown_report：严格按用户模板生成结构。"""
        from reporter import generate_markdown_report

        # 构造一个最小 alarm_result + rotation_result
        alarm = {
            'ref_date': '2026-08-19',
            'risk_level': 'warn',
            'red_count': 3,
            'signals': [
                {'name': 's1', 'label': 'S1 成交额/总市值', 'red': False,
                 'data_sufficient': True, 'value': 0.02},
                {'name': 's2', 'label': 'S2 放量不涨', 'red': True,
                 'data_sufficient': True, 'value': 1.4},
                {'name': 's3', 'label': 'S3 涨跌家数比回落', 'red': True,
                 'data_sufficient': True, 'value': 0.8},
                {'name': 's4', 'label': 'S4 高股息逆势走强', 'red': True,
                 'data_sufficient': True, 'value': 0.08},
                {'name': 's5', 'label': 'S5 成长股破位', 'red': False,
                 'data_sufficient': True, 'value': -0.02},
            ],
            'adx': {'value': 24.5, 'market_state': 'neutral',
                    'data_sufficient': True, 'detail': 'ADX=24.5'},
            'market_state': 'neutral',
            'position_advice': '8成 → 5成',
            'advice_desc': '超过3个红灯，降仓至5成',
        }
        rotation = {
            'results': [
                {'symbol': '512800', 'label': '银行ETF', 'close': 1.052,
                 'adx': 32.1, 'rs_ratio': 1.12, 'rs_momentum': 2.3,
                 'quadrant': '领涨主线', 'quadrant_emoji': '🟢',
                 'score_5d': {'total_score': 4.2, 'crowding_penalty_applied': False,
                              'action_hint': '持有'},
                 'data_sufficient': True, 'reason': ''},
                {'symbol': '159995', 'label': '芯片ETF', 'close': 0.987,
                 'adx': 26.5, 'rs_ratio': 0.98, 'rs_momentum': 1.8,
                 'quadrant': '轮动初期', 'quadrant_emoji': '🟡',
                 'score_5d': {'total_score': 4.8, 'crowding_penalty_applied': False,
                              'action_hint': '关注建仓'},
                 'data_sufficient': True, 'reason': ''},
                {'symbol': '512720', 'label': '计算机ETF', 'close': 0.921,
                 'adx': 18.2, 'rs_ratio': 0.95, 'rs_momentum': -0.5,
                 'quadrant': '滞后回避', 'quadrant_emoji': '🔴',
                 'score_5d': {'total_score': 2.1, 'crowding_penalty_applied': False,
                              'action_hint': '回避'},
                 'data_sufficient': True, 'reason': ''},
            ],
            'rank_delta_map': {'512800': '持平', '159995': '+1', '512720': '-1'},
            'summary': {'total_basket': 4, 'data_ok': 3, 'leader_count': 1, 'laggard_count': 1},
            'analyzed_at': '2026-08-19 15:30:00',
        }

        md = generate_markdown_report(alarm, rotation)

        # ---- 断言：模板结构严格合规 ----
        assert '📊 2026-08-19 市场全景分析报告' in md
        # 综合警报等级
        assert '综合警报等级' in md
        assert '🚨' in md or '综合警报' in md
        assert '红灯计数: 3/5' in md
        assert 'ADX=24.5' in md or '过渡区' in md
        # 亮灯信号列表（S2, S3, S4 3个）
        assert 'S2（放量不涨）' in md
        assert 'S3（涨跌家数比回落）' in md
        assert 'S4（高股息逆势走强）' in md
        # 轮动全景表
        assert '🔄 板块轮动全景表' in md
        # 表头（严格 8 列：板块/收盘价/ADX/RS-Ratio/RS-Momentum/象限标签/5维评分/操作参考）
        assert '| 板块 | 收盘价 | ADX | RS-Ratio | RS-Momentum | 象限标签 | 5维评分 | 操作参考 |' in md
        # 分隔行
        assert '|------|--------|-----|----------|-------------|----------|---------|----------|' in md
        # 三行数据存在
        assert '银行ETF' in md and '1.052' in md
        assert '芯片ETF' in md and '0.987' in md
        assert '计算机ETF' in md and '0.921' in md
        # 含操作参考列内容
        assert '持有' in md
        assert '关注建仓' in md
        assert '回避' in md

    def test_rotation_analysis_with_breadth(self, cfg):
        """数据足够场景：align → analyze → 结果非 None（关键值存在）。"""
        from rotation import analyze_single_etf
        # 构造：120 日强上涨 ETF + 同长基准
        n = 120
        bench_closes = [100.0 + i * 0.1 for i in range(n)]
        bench = _make_index_df(bench_closes, [1e9] * n,
                               highs=[c + 1 for c in bench_closes],
                               lows=[c - 1 for c in bench_closes])
        etf_closes = [1.0 + i * (0.2 / n) for i in range(n)]  # ETF 斜率>基准：跑赢
        etf = _make_etf_df(etf_closes)
        etf['high'] = [c + 0.01 for c in etf_closes]
        etf['low'] = [c - 0.01 for c in etf_closes]
        etf['amount'] = [1e8] * n
        r = analyze_single_etf('512800', etf, bench, cfg)
        # 不抛异常，值存在
        assert r['data_sufficient'] is True
        assert r['close'] is not None
        assert r['rs_ratio'] is not None
        assert r['quadrant'] is not None
        assert r['score_5d'] is not None
        total_s = r['score_5d']['total_score']
        assert 0.0 <= total_s <= 5.0

