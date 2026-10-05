"""合成主题指数（core/synth_index.py）单元测试 —— **全部离线**。

重点钉住四条口径（它们决定了不同批次的合成指数能不能比）：
1. 等权 **逐日再平衡** 的链式收益（不是价格平均）；
2. **时点还原**：`time_in` 之前不计入；
3. 当日缺数据的个股**当天剔除**，不做 forward-fill；
4. 成分股不足 `min_members` 的日子为 **NaN**（不填 0、不外推）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import core.synth_index as si


def _rets(rows: dict, dates) -> pd.DataFrame:
    """{code: [日收益,…]} → 收益矩阵。"""
    d = pd.DatetimeIndex(pd.to_datetime(dates))
    return pd.DataFrame(rows, index=d)


DATES = pd.bdate_range("2020-01-01", periods=6)


def _cons(*pairs) -> pd.DataFrame:
    return pd.DataFrame([{"stock_code": c, "time_in": t} for c, t in pairs])


# ====================================================================
# 等权链式
# ====================================================================

def test_equal_weight_daily_rebalanced_chaining():
    """两只有已知收益的股票 ⇒ 指数收益 = 两者算术平均，点位按日复利链式相乘。"""
    r = _rets({"sz000001": [0.0, 0.10, -0.10], "sz000002": [0.0, -0.10, 0.10]}, DATES[:3])
    c = _cons(("000001", "2019-01-01"), ("000002", "2019-01-01"))
    out = si.build_index(r, c, start="2019-01-01", min_members=2, base=1000.0)
    assert out["ret"].tolist()[1:] == pytest.approx([0.0, 0.0])
    assert out["close"].tolist() == pytest.approx([1000.0, 1000.0, 1000.0])
    assert out["n_members"].tolist() == [2, 2, 2]


def test_equal_weight_beats_price_average_when_scales_differ():
    """等权收益 ≠ 价格平均 —— 量纲不同的两只票，链式口径不受影响。"""
    r = _rets({"sh600000": [0.0, 0.05], "sz000001": [0.0, 0.15]}, DATES[:2])
    c = _cons(("600000", "2019-01-01"), ("000001", "2019-01-01"))
    out = si.build_index(r, c, start="2019-01-01", min_members=2)
    assert out["ret"].iloc[1] == pytest.approx(0.10)


# ====================================================================
# 时点还原 / 缺数据 / 最少成分股
# ====================================================================

def test_point_in_time_excludes_stock_before_time_in():
    r = _rets({"sz000001": [0.0, 0.10, 0.10], "sz000002": [0.0, -0.10, -0.10]}, DATES[:3])
    c = _cons(("000001", "2019-01-01"), ("000002", "2020-01-03"))   # 第二只第 3 天才进入
    out = si.build_index(r, c, start="2019-01-01", min_members=1)
    assert out["n_members"].tolist() == [1, 1, 2]
    assert out["ret"].iloc[1] == pytest.approx(0.10)               # 只有第一只
    assert out["ret"].iloc[2] == pytest.approx(0.0)                # 两只都在 → 平均为 0


def test_missing_return_is_excluded_that_day_not_forward_filled():
    r = _rets({"sz000001": [0.0, 0.10, 0.10], "sz000002": [0.0, np.nan, 0.10]}, DATES[:3])
    c = _cons(("000001", "2019-01-01"), ("000002", "2019-01-01"))
    out = si.build_index(r, c, start="2019-01-01", min_members=1)
    assert out["n_members"].tolist() == [2, 1, 2]
    assert out["ret"].iloc[1] == pytest.approx(0.10)               # 第二只当天被剔除
    assert out["n_missing"].tolist() == [0, 1, 0]


def test_min_members_guard_yields_nan_not_zero():
    r = _rets({"sz000001": [0.0, 0.10, 0.10, 0.10],
               "sz000002": [0.0, np.nan, np.nan, 0.10]}, DATES[:4])
    c = _cons(("000001", "2019-01-01"), ("000002", "2019-01-01"))
    out = si.build_index(r, c, start="2019-01-01", min_members=2)
    assert np.isnan(out["close"].iloc[1]) and np.isnan(out["close"].iloc[2])
    assert np.isfinite(out["close"].iloc[0]) and np.isfinite(out["close"].iloc[3])


def test_start_date_filters_history():
    r = _rets({"sz000001": [0.0, 0.1, 0.1, 0.1]}, DATES[:4])
    c = _cons(("000001", "2019-01-01"))
    out = si.build_index(r, c, start=str(DATES[2].date()), min_members=1)
    assert out["date"].iloc[0] == DATES[2]


def test_empty_inputs_return_empty_frame():
    empty = si.build_index(pd.DataFrame(), _cons())
    assert empty.empty and list(empty.columns) == ["date", "close", "ret", "n_members", "n_missing"]


# ====================================================================
# 代码转换 / 北交所剔除
# ====================================================================

def test_to_tx_codes_drops_bse_with_reason():
    kept, dropped = si.to_tx_codes(["600111", "000831", "920061"])
    assert [k[0] for k in kept] == ["sh600111", "sz000831"]
    assert len(dropped) == 1 and dropped[0][0] == "920061" and "北交所" in dropped[0][1]


def test_membership_table_ignores_unknown_code_format():
    m = si.membership_table(pd.DataFrame([{"stock_code": "abc", "time_in": "2020-01-01"}]),
                            DATES)
    assert not m.any().any()


# ====================================================================
# 由价格合成 + 相关性
# ====================================================================

def _px(vals, dates):
    return pd.Series(vals, index=pd.DatetimeIndex(pd.to_datetime(dates)), dtype="float64")


def test_build_index_from_prices_matches_manual_returns():
    px = {"sh600000": _px([100, 110, 110], DATES[:3]),
          "sz000001": _px([10, 9, 9], DATES[:3])}
    c = _cons(("600000", "2019-01-01"), ("000001", "2019-01-01"))
    out = si.build_index_from_prices(px, c, start="2019-01-01", min_members=2)
    # 第一根没有"前收" ⇒ 无成分股可用、close 为 NaN；之后两根：+10%/−10% → 0；再 0
    assert np.isnan(out["close"].iloc[0]) and out["n_members"].iloc[0] == 0
    assert out["ret"].tolist()[1:] == pytest.approx([0.0, 0.0])


def test_correlation_perfect_and_none():
    d = pd.bdate_range("2020-01-01", periods=300)
    rng = np.random.default_rng(0)
    inc = rng.normal(0, 0.01, len(d))
    a = pd.DataFrame({"date": d, "close": 1000 * np.cumprod(1 + inc)})
    b = pd.DataFrame({"date": d, "close": 500 * np.cumprod(1 + inc)})
    v, n = si.correlation(a, b)
    assert v == pytest.approx(1.0, abs=1e-9) and n == len(d) - 1
    # 样本不足 ⇒ (None, n)
    v2, n2 = si.correlation(a.head(50), b.head(50))
    assert v2 is None and n2 < si.MIN_OVERLAP


def test_correlation_uses_overlapping_dates_only():
    d = pd.bdate_range("2020-01-01", periods=300)
    a = pd.DataFrame({"date": d, "close": np.linspace(1000, 1200, len(d))})
    b = pd.DataFrame({"date": d[100:], "close": np.linspace(500, 600, len(d) - 100)})
    v, n = si.correlation(a, b)
    assert n == len(d) - 100 - 1


# ====================================================================
# OHLC 合成（L1 的摆动点要用 high/low，不能拿 close 假装）
# ====================================================================

def _ohlc(dates, o, h, l, c) -> pd.DataFrame:
    return pd.DataFrame({"date": pd.DatetimeIndex(pd.to_datetime(dates)),
                         "open": o, "high": h, "low": l, "close": c})


def test_ohlc_synthesis_close_matches_plain_synthesis():
    """OHLC 口径下的 close 必须与只算收益的口径**完全一致**。"""
    dates = DATES[:4]
    f1 = _ohlc(dates, [10, 11, 12, 13], [10.5, 11.5, 12.5, 13.5],
               [9.5, 10.5, 11.5, 12.5], [10, 11, 12, 13])
    f2 = _ohlc(dates, [20, 20, 22, 22], [21, 21, 23, 23],
               [19, 19, 21, 21], [20, 20, 22, 22])
    c = _cons(("600000", "2019-01-01"), ("000001", "2019-01-01"))
    a = si.build_index_from_ohlc({"sh600000": f1, "sz000001": f2}, c,
                                 start="2019-01-01", min_members=2)
    b = si.build_index_from_prices(
        {"sh600000": f1.set_index("date")["close"], "sz000001": f2.set_index("date")["close"]},
        c, start="2019-01-01", min_members=2)
    assert len(a) == len(b)
    assert a["close"].to_numpy()[1:] == pytest.approx(b["close"].to_numpy()[1:])


def test_ohlc_high_low_bracket_close_and_use_field_ratios():
    dates = DATES[:3]
    f1 = _ohlc(dates, [10, 11, 11], [11, 12, 13], [9, 10, 9], [10, 11, 12])
    f2 = _ohlc(dates, [10, 10, 10], [10, 10, 10], [10, 10, 10], [10, 10, 10])
    c = _cons(("600000", "2019-01-01"), ("000001", "2019-01-01"))
    out = si.build_index_from_ohlc({"sh600000": f1, "sz000001": f2}, c,
                                   start="2019-01-01", min_members=2)
    # 第一根用收盘兜底；之后 high ≥ close ≥ low 必须成立
    for _, r in out.iloc[1:].iterrows():
        assert r["high"] >= r["close"] >= r["low"]
        assert r["high"] >= r["open"] >= r["low"]


def test_ohlc_requires_previous_close_so_first_bar_has_no_members():
    """没有前收 ⇒ 当日不可用（第一根只能靠兜底，之后才正常）。"""
    dates = DATES[:3]
    f1 = _ohlc(dates, [10, 11, 12], [10, 11, 12], [10, 11, 12], [10, 11, 12])
    c = _cons(("600000", "2019-01-01"))
    out = si.build_index_from_ohlc({"sh600000": f1}, c, start="2019-01-01", min_members=1)
    assert out["n_members"].tolist() == [0, 1, 1]
    assert np.isnan(out["close"].iloc[0]) and np.isfinite(out["close"].iloc[1])


def test_ohlc_empty_inputs():
    out = si.build_index_from_ohlc({}, _cons())
    assert out.empty and "high" in out.columns
