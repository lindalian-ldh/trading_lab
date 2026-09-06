"""S6 两融杠杆异常 纯函数 + CSV 自累积测试（不触网，全 mock DataFrame）。

覆盖：
    T1  空 margin_df → 数据不足，red=False
    T2  缺 rzye 列 → 数据不足
    T3  行数 < 6 → 数据不足
    T4  5 日融资增速 3.5% > 阈值 3% → 过热分支亮 → S6 red=True（默认 OR）
    T5  20 日融资增速 10% > 8%；其他全绿 → 过热亮 → red=True
    T6  融资买入占比 13% > 12% → 过热亮 → red=True
    T7  净多头杠杆 2.0% > 1.8% → 过热亮 → red=True
    T8  5 日回撤 -3% < -2% → 去杠杆分支亮 → red=True
    T9  10 日回撤 -4% < -3.5%；其他分支绿 → 去杠杆亮 → red=True
    T10 无分支命中 → red=False
    T11 MARGIN_RED_MODE=BOTH，过热亮+去杠杆不亮 → red=False
    T12 MARGIN_RED_MODE=BOTH，过热亮+去杠杆亮 → red=True
    T13 value/threshold：有 net_leverage 时用 net 对 NET；否则用 g5 对 G5
    T14 detail 明细含 5 日增速 / 回撤 / 红逻辑 关键字段
    T15 数据量不足（仅 20 日增速缺）：仍正常判断，明细中 20 日显示为 —
    T16 storage：append_margin_row + read_margin_history 同日去重 + 跨月去重
    T17 storage：MARGIN_FIELDS 与 append_margin_row 参数一一对应
    T18 正常市：5日增速 1.2%、20日 5%、占比 9%、杠杆 1.2%、回撤 0 → 全绿
    T19 与 monitor RANGE_WEIGHTED_SIGNALS 对齐：config 默认含 margin_leverage
"""

from __future__ import annotations

import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# 把 alarming_monitor 目录加入 sys.path 最前，避免根目录 config 命名冲突
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
from indicators import check_margin_leverage
from storage import (
    MARGIN_FIELDS,
    append_margin_row,
    read_margin_history,
    _margin_csv_dir,
    _margin_path,
)


# ====================================================================
# Fixture：构造融资余额序列
# ====================================================================

def _make_margin_df(base_rzye: float = 2.0e12,
                    n_days: int = 25,
                    # 每日相对增长率（最后一天下标末），可覆盖
                    daily_growth_pct: list = None,
                    rzmre_daily: float = 1.5e11,   # 融资买入额（元），每日 1500 亿
                    rqye_ratio: float = 0.01,      # 融券余额 = rzye * rqye_ratio
                    ) -> pd.DataFrame:
    """构造 margin_df 伪序列。返回 len=n_days 行，末行最新。"""
    if daily_growth_pct is None:
        daily_growth_pct = [0.0] * n_days
    assert len(daily_growth_pct) == n_days

    dates, rzye_list, rzmre_list, rqye_list, rzrqye_list = [], [], [], [], []
    # 日期：从 ref_date 往前 n_days 个工作日（简化：直接连号，S6 不关心非交易日）
    start = pd.Timestamp('2026-08-28') - pd.tseries.offsets.BDay(n_days - 1)
    for i, d in enumerate(pd.bdate_range(start=start, periods=n_days)):
        dates.append(d.strftime('%Y-%m-%d'))
        # rzye 累计：以 base_rzye 为"末行值"，往前反推
        # 令 growth[i] 为 i 相对 i-1 的日增长率，则 rzye[i] = rzye[i-1]*(1+g)
        # 为简单起见，给定 daily_growth_pct 后，从第一天 g1...gN 迭代生成，最后把 rzye[N] 缩放为 base_rzye
        pass

    # 先迭代，再缩放
    rzye_tmp = [1.0]
    for g in daily_growth_pct[1:]:
        rzye_tmp.append(rzye_tmp[-1] * (1 + g / 100.0))
    scale = base_rzye / rzye_tmp[-1]
    for i in range(n_days):
        rzye = rzye_tmp[i] * scale
        rqye = rzye * rqye_ratio
        rzye_list.append(rzye)
        rqye_list.append(rqye)
        rzmre_list.append(rzmre_daily)
        rzrqye_list.append(rzye + rqye)

    return pd.DataFrame({
        'date': dates,
        'rzye': rzye_list,
        'rzmre': rzmre_list,
        'rqye': rqye_list,
        'rzrqye': rzrqye_list,
        'rqmcl': [1e8] * n_days,
        'rqyl':  [3e9] * n_days,
    })


def _cfg(**over) -> AlarmConfig:
    import copy
    cfg = AlarmConfig()
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


# ====================================================================
# T1-T15 指标纯函数
# ====================================================================

def test_T1_empty_df():
    r = check_margin_leverage(None, None, 80e12, _cfg())
    assert r['data_sufficient'] is False
    assert r['red'] is False
    assert '两融汇总为空' in r['detail']


def test_T2_missing_rzye_col():
    df = pd.DataFrame({'date': ['2026-08-28'], 'foo': [1.0]})
    r = check_margin_leverage(df, None, 80e12, _cfg())
    assert r['data_sufficient'] is False
    assert '缺少列' in r['detail']


def test_T3_too_few_rows():
    df = _make_margin_df(n_days=3)
    r = check_margin_leverage(df, None, 80e12, _cfg())
    assert r['data_sufficient'] is False
    assert '有效行 3 < 6' in r['detail']


def test_T4_hot_grow_5d():
    """5日融资增速 > 3% 过热亮红。"""
    # 末 5 日日增 0.7% → 5日总 ≈ (1.007)^5-1 ≈ 3.54%
    n = 25
    g = [0.0] * n
    for i in range(5):
        g[n - 1 - i] = 0.7 if i > 0 else 0.0  # 末值不用算增长率，前 5 天各 +0.7%
    # 实际上：daily_growth_pct[x] 是 day x 相对 day x-1 的增长；为了让最后 5 个区间都有涨，
    # 设置 indices 19..23（即对应第 20..24 天相对前一天）为 0.7%。
    g = [0.0] * n
    for idx in (n - 5, n - 4, n - 3, n - 2, n - 1):
        g[idx] = 0.7
    df = _make_margin_df(n_days=n, daily_growth_pct=g)
    r = check_margin_leverage(df, 1.0e12, 80e12, _cfg())
    assert r['red'] is True
    md = r['margin_detail']
    assert md['hot_any'] is True
    assert md['grow_5d'] > 0.03


def test_T5_hot_grow_20d():
    """20日融资增速 > 8%。"""
    n = 25
    g = [0.0] * n
    # 最后 20 个区间每天 0.4% → 1.004^20 - 1 ≈ 8.3%
    for idx in range(n - 20, n):
        g[idx] = 0.4
    df = _make_margin_df(n_days=n, daily_growth_pct=g)
    r = check_margin_leverage(df, 1.0e12, 80e12, _cfg())
    assert r['red'] is True
    assert r['margin_detail']['grow_20d'] > 0.08


def test_T6_hot_buy_ratio():
    """融资买入额/两市成交额 = 1500亿 / 10000亿 = 15% > 12%。"""
    df = _make_margin_df(n_days=25, rzmre_daily=1.5e11)  # 每日融资买入 1500 亿
    cfg = _cfg()
    # market_amount_today = 1 万亿 = 1e12
    r = check_margin_leverage(df, market_amount_today=1e12, total_market_cap=80e12, cfg=cfg)
    assert r['red'] is True
    assert r['margin_detail']['buy_ratio'] == pytest.approx(0.15, rel=1e-6)


def test_T7_hot_net_leverage():
    """净多头杠杆 = (rzye - rqye)/cap。构造 rqye_ratio=0.005，base=2.6e12 → 净≈2.587e12/80e12≈3.23%>1.8%。"""
    df = _make_margin_df(base_rzye=2.6e12, n_days=25, rqye_ratio=0.005)
    r = check_margin_leverage(df, market_amount_today=2.0e12, total_market_cap=80e12, cfg=_cfg())
    assert r['red'] is True
    assert r['margin_detail']['net_leverage'] > 0.018


def test_T8_delev_5d():
    """5日回撤 -3% < -2%。"""
    n = 25
    g = [0.0] * n
    # 最后 5 天每日 -0.6% → 约 -2.97%
    for idx in (n - 5, n - 4, n - 3, n - 2, n - 1):
        g[idx] = -0.6
    df = _make_margin_df(n_days=n, daily_growth_pct=g)
    r = check_margin_leverage(df, 2e12, 80e12, _cfg())
    assert r['red'] is True
    assert r['margin_detail']['delev_5d'] < -0.02


def test_T9_delev_10d():
    """10日回撤 -4% < -3.5%。"""
    n = 25
    g = [0.0] * n
    # 最后 10 天每日 -0.42% → (0.9958)^10 - 1 ≈ -4.1%
    for idx in range(n - 10, n):
        g[idx] = -0.42
    df = _make_margin_df(n_days=n, daily_growth_pct=g)
    r = check_margin_leverage(df, 2e12, 80e12, _cfg())
    assert r['red'] is True
    assert r['margin_detail']['delev_10d'] < -0.035


def test_T10_no_branch_hit():
    """无分支命中 → green。

    指标：
      * 5/20 日增速 = 0（余额恒定）→ < 3% / 8% ✓
      * buy_ratio = 1500亿 / 3万亿 = 5% < 12% ✓（调高 market_amount 到 3e12）
      * 净杠杆 = (1.4e12 - 0.014e12) / 80e12 = 1.386e12 / 80e12 = 1.7325% < 1.8% ✓
    """
    df = _make_margin_df(base_rzye=1.4e12, n_days=25, rzmre_daily=1.5e11, rqye_ratio=0.01)
    r = check_margin_leverage(df, market_amount_today=3e12, total_market_cap=80e12, cfg=_cfg())
    assert r['red'] is False, f"不该亮，明细={r['detail']}"
    assert r['margin_detail']['hot_any'] is False
    assert r['margin_detail']['delev_any'] is False


def test_T11_mode_both_needs_two():
    """MARGIN_RED_MODE=BOTH 单过热 → 不红。"""
    n = 25
    g = [0.0] * n
    for idx in (n - 5, n - 4, n - 3, n - 2, n - 1):
        g[idx] = 0.7
    df = _make_margin_df(n_days=n, daily_growth_pct=g)
    r = check_margin_leverage(df, 1e12, 80e12, _cfg(MARGIN_RED_MODE='BOTH'))
    assert r['red'] is False
    assert '红逻辑 BOTH' in r['detail']


def test_T12_mode_both_true():
    """MARGIN_RED_MODE=BOTH，过热+去杠杆双命中 → 红。

    构造方法：阈值用极端放宽，使得 20 日增速 10% > 0 (过热) 且 5 日回撤 -0.01% < 0 (去杠杆)，
    然后用超宽阈值让两端都命中。
    更简单：直接放宽阈值。
    """
    df = _make_margin_df(n_days=25, daily_growth_pct=[0.0] * 25)  # 全不变
    # 阈值全 0：过热只要 >0 就亮，去杠杆只要 <0 就亮。这样全不变都不亮。
    # 再构造：g20>0 （给一点日微涨）且 5 日末有微跌。
    n = 25
    g = [0.0] * n
    for i in range(14, n - 5):  # 第 14..19 区间涨
        g[i] = 0.4
    # 最后 2 个区间稍跌（idx 24 对应最后 1 天相对前一天）
    g[n - 1] = -0.05
    g[n - 2] = -0.05
    df = _make_margin_df(n_days=n, daily_growth_pct=g)
    cfg = _cfg(
        MARGIN_GROWTH_5D=0.0001,
        MARGIN_GROWTH_20D=0.0001,
        MARGIN_BUY_RATIO=0.999,
        MARGIN_NET_LEVERAGE=0.999,
        MARGIN_DELEV_5D=-0.0001,
        MARGIN_DELEV_10D=-0.0001,
        MARGIN_RED_MODE='BOTH',
    )
    r = check_margin_leverage(df, 1e12, 80e12, cfg)
    # 20 日有正向区间：g20 应当是正的 → 过热亮
    # 5 日末有跌：g5 应当是负的 → 去杠杆亮
    md = r['margin_detail']
    assert md['hot_any'] is True, f"g20={md.get('grow_20d')}, g5={md.get('grow_5d')}"
    assert md['delev_any'] is True, f"d5={md.get('delev_5d')}, d10={md.get('delev_10d')}"
    assert r['red'] is True


def test_T13_value_threshold_mapping():
    """value/threshold 对应：若有 net_leverage 用 net；否则用 g5。"""
    # T13a: 有 net → net 对
    df = _make_margin_df(base_rzye=2.6e12, rqye_ratio=0.005)
    r = check_margin_leverage(df, 2e12, 80e12, _cfg())
    assert r['threshold'] == pytest.approx(0.018, rel=1e-6)
    # T13b: 无 rqye 列 → net_leverage 为 None，用 g5
    df_b = df.drop(columns=['rqye', 'rzrqye'])
    # g5 此时应为 0 → 阈值对应 MARGIN_GROWTH_5D=0.03
    r2 = check_margin_leverage(df_b, 2e12, 80e12, _cfg())
    assert r2['threshold'] == pytest.approx(0.03, rel=1e-6)
    # value = g5 = 0（全不增长）
    assert r2['value'] == pytest.approx(0.0, abs=1e-9)


def test_T14_detail_keywords():
    """明细含关键字段。"""
    df = _make_margin_df(n_days=25)
    r = check_margin_leverage(df, 2e12, 80e12, _cfg())
    for kw in ['最新融资余额', '5日融资增速', '20日融资增速', '融资买入/两市',
               '净多头杠杆', '5日回撤', '10日回撤', '红逻辑']:
        assert kw in r['detail'], f"缺少关键字段: {kw}"


def test_T15_missing_20d_growth():
    """行不够长时缺 10/20d 段不打印；行数够时不会缺。
    构造 n_days=9 → 5d 可算，10d 和 20d 都缺。
    """
    # 数据充足阈值是 len(rzye) >= 6；n_days=9 时刚好过数据充足线，但缺 10d/20d
    df = _make_margin_df(base_rzye=1.4e12, n_days=9, rzmre_daily=1.5e11, rqye_ratio=0.01)
    r = check_margin_leverage(df, 3e12, 80e12, _cfg())
    assert r['data_sufficient'] is True
    assert r['margin_detail']['grow_20d'] is None
    assert r['margin_detail']['delev_10d'] is None
    # 明细不打印「20日融资增速」和「10日回撤」
    assert '20日融资增速' not in r['detail']
    assert '10日回撤' not in r['detail']
    # 但 5 日增速 / 5 日回撤都有
    assert '5日融资增速' in r['detail']
    assert '5日回撤' in r['detail']


def test_T18_normal_market_all_green():
    """典型正常市：温和+低杠杆，全绿。"""
    n = 25
    g = [0.0] * n
    for i in range(n - 20, n):
        g[i] = 0.2  # 20 日日 0.2% → ≈ 4.08% < 8%
    df = _make_margin_df(n_days=n, daily_growth_pct=g, rzmre_daily=1.0e11)  # 日买 1000 亿
    # 两市成交额 = 2 万亿 → buy_ratio = 1000/20000 = 5% < 12%
    # 净杠杆 = (2e12 - 0.01*2e12) / 80e12 ≈ 2.475% — 等等，这会超 1.8%！
    # 调低 base_rzye 至 1.4e12 → (1.4 - 0.014) / 80 ≈ 1.73% < 1.8% ✓
    df2 = _make_margin_df(base_rzye=1.4e12, n_days=n, daily_growth_pct=g,
                          rzmre_daily=1.0e11, rqye_ratio=0.01)
    r = check_margin_leverage(df2, market_amount_today=2e12, total_market_cap=80e12, cfg=_cfg())
    assert r['red'] is False, f"不该亮，明细={r['detail']}"


def test_T19_config_range_weighted_includes_margin():
    """默认配置震荡市加权含 S6 margin_leverage（与 S4/S5 口径一致）。"""
    cfg = AlarmConfig()
    assert 'margin_leverage' in cfg.RANGE_WEIGHTED_SIGNALS
    assert 'dividend_strength' in cfg.RANGE_WEIGHTED_SIGNALS
    assert 'growth_breakdown' in cfg.RANGE_WEIGHTED_SIGNALS


# ====================================================================
# T16/T17 storage CSV 自累积
# ====================================================================

@pytest.fixture
def _tmp_margin_dir(tmp_path, monkeypatch):
    """把 margin CSV 目录临时指向 tmp_path。"""
    # storage.py 用 cfg 的 MARGIN_CSV_DIR 覆盖
    import storage as _st
    real_dir_fn = _st._margin_csv_dir

    def _fake_dir(cfg=None):
        pp = Path(str(tmp_path))
        if cfg is not None:
            # 让 cfg.MARGIN_CSV_DIR 不起实际作用，统一用 tmp_path
            pass
        pp.mkdir(parents=True, exist_ok=True)
        return pp

    monkeypatch.setattr(_st, '_margin_csv_dir', _fake_dir)
    yield tmp_path


def test_T16_append_and_read(_tmp_margin_dir):
    """同日两次去重 + 跨月合并返回。"""
    # 写同一日期两次 → CSV 内只保留最新
    cfg = _cfg()
    p = append_margin_row('2026-08-28', 2.0e12, 1.5e11, 2e10, 2.02e12, 1e8, 3e9, cfg)
    df_1 = pd.read_csv(p, encoding='utf-8-sig')
    assert len(df_1) == 1
    # 再写同一日：余额 2.05e12（新值）
    append_margin_row('2026-08-28', 2.05e12, 1.5e11, 2e10, 2.07e12, 1e8, 3e9, cfg)
    df_2 = pd.read_csv(p, encoding='utf-8-sig')
    assert len(df_2) == 1, '同日应覆盖'
    assert df_2['rzye_yuan'].iloc[0] == pytest.approx(2.05e12, rel=1e-6)

    # 写另一日
    append_margin_row('2026-08-27', 2.0e12, 1.0e11, 1.9e10, 2.02e12, 9e7, 2.9e9, cfg)
    df_3 = pd.read_csv(p, encoding='utf-8-sig')
    assert len(df_3) == 2

    # 写跨月
    append_margin_row('2026-07-31', 1.9e12, 9e10, 1.8e10, 1.92e12, 8e7, 2.7e9, cfg)
    # read_margin_history 应返回 3 行（近 25 日，2026-07-31 ≤ 8-28 肯定在范围内）
    hist = read_margin_history(days=40, cfg=cfg)
    assert hist is not None and len(hist) >= 3
    # 列名归一化
    assert {'date', 'rzye', 'rzmre', 'rqye'}.issubset(hist.columns)
    # 升序
    dates = list(hist['date'])
    assert dates == sorted(dates)


def test_T17_margin_fields_match_params():
    """MARGIN_FIELDS 与 append_margin_row 关键形参对齐。"""
    import inspect
    sig = inspect.signature(append_margin_row)
    params = list(sig.parameters.keys())  # ['date_str', 'rzye_yuan', 'rzmre_yuan', 'rqye_yuan', ...]
    # 去掉 date_str → 对应 MARGIN_FIELDS 的 date
    assert MARGIN_FIELDS[0] == 'date'
    want = params[1:7]   # rzye_yuan..rqyl
    assert MARGIN_FIELDS[1:] == list(want), (MARGIN_FIELDS[1:], want)


# ====================================================================
# Item1 防御性：脏数据不崩流程，降级灰灯
# ====================================================================

def test_T20_rzye_base_zero_does_not_raise():
    """_ret 基准为 0 → 返回 None，不抛 ZeroDivisionError。"""
    df = _make_margin_df(n_days=10)
    # 把倒数第 6 行（5 日增速的基准）设为 0，模拟脏数据
    df.loc[df.index[-6], 'rzye'] = 0.0
    # 不抛异常即通过；5 日增速因 base=0 被置 None
    r = check_margin_leverage(df, 8e12, 80e12, _cfg())
    assert r['data_sufficient'] is True
    md = r.get('margin_detail', {})
    assert md.get('grow_5d') is None  # base=0 → 该周期增速被置 None


def test_T21_rzye_with_nan_drops_nan():
    """rzye 含 NaN → dropna 后仍可计算，不崩。"""
    df = _make_margin_df(n_days=12)
    df.loc[df.index[-3], 'rzye'] = float('nan')
    # 不抛异常即通过
    r = check_margin_leverage(df, 8e12, 80e12, _cfg())
    assert r['data_sufficient'] is True


def test_T22_malformed_df_returns_gray_not_raise():
    """整体异常（rzye 全 inf）→ try/except 捕获，返回灰灯 data_sufficient=False。"""
    df = _make_margin_df(n_days=10)
    df['rzye'] = float('inf')
    r = check_margin_leverage(df, 8e12, 80e12, _cfg())
    # 不抛，降级灰灯
    assert r['red'] is False
    assert r['data_sufficient'] is False


def test_T23_inf_value_does_not_crash_round():
    """reporter.format_csv_row 对 inf/NaN 用哨兵值，不抛 OverflowError。"""
    from reporter import format_csv_row
    fake_result = {
        'run_time': '2026-09-06 10:00:00', 'ref_date': '2026-09-05',
        'risk_level': 'normal', 'red_count': 0, 'effective_red_count': 0,
        'effective_threshold': 4, 'market_state': 'trend', 'adx_value': 25,
        'position_advice': '保持', 'advice_desc': 'x', 'data_sufficient': True,
        'signals': [
            {'name': 'turnover_ratio_high', 'value': float('inf'), 'red': False},
            {'name': 'volume_price_divergence', 'value': float('nan'), 'red': False},
            {'name': 'margin_leverage', 'value': 0.0123, 'red': False},
        ],
    }
    row = format_csv_row(fake_result)
    assert row['s1_turnover_ratio'] == '∞'
    assert row['s1_red'] is False
    assert row['s2_divergence'] == ''
    assert row['s6_net_leverage'] == 0.0123


def test_T24_main_legu_parse_exception_does_not_crash(tmp_path, monkeypatch):
    """main._gather_data 中 legu 解析异常 → 跳过今日，不抛。"""
    # 模拟 fetch_breadth_today 返回脏 DF，row.get 返回 None 导致 int(None) 抛
    import main as main_mod
    class _Cfg:
        LOOKBACK_DAYS = 10
        DIVIDEND_BASKET = []
        GROWTH_BASKET = []
        TURNOVER_INDEX_SH = 'sh000001'
        TURNOVER_INDEX_SZ = 'sz399001'
        BREADTH_INDEX = 'sh000300'
        BREADTH_INDEX_FALLBACK = 'sh000001'
        MARGIN_USE_HISTORY_CSV = False
        MARGIN_LOOKBACK_DAYS = 25
        KDJ_LOOKBACK_DAYS = 260
        KDJ_INDEX = 'sh000300'
        KDJ_INDEX_FALLBACK = 'sh000001'
        ADX_PERIOD = 14
        VOLUME_LOOKBACK_DAYS = 5
    cfg = _Cfg()
    dirty_df = pd.DataFrame([{'date': '2026-09-05', 'up_count': None, 'down_count': None, 'flat_count': None, 'ad_ratio': None}])
    monkeypatch.setattr(main_mod, 'fetch_breadth_today', lambda: dirty_df)
    monkeypatch.setattr(main_mod, 'fetch_market_amount', lambda *a, **k: None)
    monkeypatch.setattr(main_mod, 'fetch_index_daily', lambda *a, **k: None)
    monkeypatch.setattr(main_mod, 'fetch_etf_daily', lambda *a, **k: None)
    monkeypatch.setattr(main_mod, 'fetch_margin_summary', lambda *a, **k: None)
    monkeypatch.setattr(main_mod, 'read_breadth_history', lambda *a, **k: None)
    monkeypatch.setattr(main_mod, 'append_breadth_today', lambda *a, **k: None)
    # 不抛异常即通过
    main_mod._gather_data(cfg, '2026-09-05')

