"""腾讯源行情数据层（core/marketdata_tx.py）单元测试。

**全部离线**：网络入口 `_fetch_raw_tx` 一律被 monkeypatch，
以保证测试可重复、不依赖外部服务可用性。
"""

from __future__ import annotations

import logging

import pandas as pd
import pytest

import core.marketdata_tx as m


# ====================================================================
# 夹具
# ====================================================================

def _mk(dates, closes) -> pd.DataFrame:
    """构造最小 OHLCV DataFrame。"""
    return pd.DataFrame({
        "date": pd.to_datetime(dates),
        "open": closes, "high": closes, "low": closes, "close": closes,
    })


@pytest.fixture
def cache_dir(tmp_path):
    return tmp_path / "cache"


# ====================================================================
# 代码归一化与 kind 判定
# ====================================================================

@pytest.mark.parametrize("raw,kind,expect", [
    ("sh000001", None, "sh000001"),
    ("SH000001", None, "sh000001"),
    ("sh.000001", None, "sh000001"),
    ("sh.399006", None, "sh399006"),      # 前缀与数字不一致时以前缀为准（不做数字推断）
    ("sz399006", None, "sz399006"),
    ("512480", "equity", "sh512480"),
    ("600547", "equity", "sh600547"),
    ("002371", "equity", "sz002371"),
    ("159516", "equity", "sz159516"),
])
def test_normalize_tx_code(raw, kind, expect):
    assert m.normalize_tx_code(raw, kind) == expect


@pytest.mark.parametrize("raw", ["000001", "000688", "399006"])
def test_normalize_tx_code_bare_digits_must_not_guess(raw):
    """纯数字**不得**被猜测 —— 000001 指数是上证综指(sh)、个股是平安银行(sz)。"""
    with pytest.raises(m.MarketDataError):
        m.normalize_tx_code(raw)          # kind 未知
    with pytest.raises(m.MarketDataError):
        m.normalize_tx_code(raw, "index")  # 显式 index 也不行


@pytest.mark.parametrize("raw", ["", "   ", "abc", "sh12", "sh1234567", "sh.123.4"])
def test_normalize_tx_code_rejects_garbage(raw):
    with pytest.raises(m.MarketDataError):
        m.normalize_tx_code(raw, "equity")


def test_resolve_kind_registered_index():
    assert m.resolve_kind("sh000001") == m.KIND_INDEX
    assert m.resolve_kind("sz399006") == m.KIND_INDEX
    assert m.resolve_kind("sh000688") == m.KIND_INDEX


@pytest.mark.parametrize("kind", ["index", "idx", "指数"])
def test_resolve_kind_accepts_index_aliases(kind):
    assert m.resolve_kind("sh512480", kind) == m.KIND_INDEX


@pytest.mark.parametrize("kind", ["equity", "etf", "stock", "个股"])
def test_resolve_kind_accepts_equity_aliases(kind):
    assert m.resolve_kind("sh512480", kind) == m.KIND_EQUITY


def test_resolve_kind_unknown_code_raises_not_guesses():
    """未登记代码必须报错（强制显式），而不是猜成个股。"""
    with pytest.raises(m.MarketDataError):
        m.resolve_kind("sh512480", None)


def test_resolve_kind_rejects_bogus_kind():
    with pytest.raises(m.MarketDataError):
        m.resolve_kind("sh512480", "banana")


# ====================================================================
# 列语义诚实性（本仓库明确反对静默填 0）
# ====================================================================

def test_missing_volume_becomes_nan_not_zero():
    """指数接口无 volume 时必须留 NaN —— 填 0 会被误读成"成交量为零"。"""
    df = _mk(["2026-01-01"], [1.0])
    out = m._reindex_columns(df)
    assert list(out.columns) == list(m.OHLCV_COLUMNS)
    assert out["volume"].isna().all()
    assert out["amount"].isna().all()
    assert not (out["volume"] == 0).any()


def test_reindex_drops_rows_without_close_and_sorts():
    df = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-03", "2026-01-01", "2026-01-02"]),
        "close": [3.0, 1.0, None],
    })
    out = m._reindex_columns(df)
    assert len(out) == 2
    assert out["close"].tolist() == [1.0, 3.0]


# ====================================================================
# 缓存读写
# ====================================================================

def test_cache_path_naming_convention(tmp_path):
    assert m.cache_path(m.KIND_INDEX, "sh000001", cache_dir=tmp_path).name == \
        "index_sh000001_tx_history.csv"
    assert m.cache_path(m.KIND_EQUITY, "sh512480", cache_dir=tmp_path).name == \
        "equity_sh512480_tx_history.csv"


def test_cache_roundtrip(cache_dir):
    p = m.cache_path(m.KIND_INDEX, "sh000001", cache_dir=cache_dir)
    df = m._reindex_columns(_mk(["2026-01-01", "2026-01-02"], [1.0, 2.0]))
    m.save_cache(p, df)
    back = m.load_cache(p)
    assert back is not None
    assert len(back) == 2
    assert back["close"].tolist() == [1.0, 2.0]
    assert back["date"].dtype.kind == "M"


def test_load_cache_missing_returns_none(cache_dir):
    assert m.load_cache(cache_dir / "nope.csv") is None


def test_load_cache_corrupt_returns_none_and_warns(cache_dir, caplog):
    p = cache_dir / "bad.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("this,is\x00not,csv\n\x00\xff", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        got = m.load_cache(p)
    assert got is None


def test_load_cache_empty_returns_none(cache_dir):
    p = cache_dir / "empty.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("date,close\n", encoding="utf-8")
    assert m.load_cache(p) is None


# ====================================================================
# 合并：新数据覆盖同日旧值
# ====================================================================

def test_merge_history_fresh_overrides_same_date():
    cached = _mk(["2026-01-01", "2026-01-02"], [1.0, 2.0])
    fresh = _mk(["2026-01-02", "2026-01-03"], [9.0, 3.0])
    out = m.merge_history(cached, fresh)
    assert out["close"].tolist() == [1.0, 9.0, 3.0]      # 2026-01-02 被覆盖
    assert out["date"].is_unique


def test_merge_history_handles_none_either_side():
    d = _mk(["2026-01-01"], [1.0])
    assert len(m.merge_history(None, d)) == 1
    assert len(m.merge_history(d, None)) == 1
    assert m.merge_history(None, None) is None
    assert m.merge_history(d, pd.DataFrame()) is not None


def test_merge_history_dedupes_within_input():
    dup = _mk(["2026-01-01", "2026-01-01"], [1.0, 5.0])
    out = m.merge_history(dup, None)
    assert len(out) == 1
    assert out["close"].iloc[0] == 5.0


# ====================================================================
# fetch_history：缓存优先 / 过期回退 / 显式失败
# ====================================================================

def test_fetch_prefer_cache_offline(cache_dir, monkeypatch):
    """prefer_cache=True 且缓存存在 → 不联网。"""
    p = m.cache_path(m.KIND_INDEX, "sh000001", cache_dir=cache_dir)
    m.save_cache(p, m._reindex_columns(_mk(["2026-01-01", "2026-01-02"], [1.0, 2.0])))

    def _boom(*a, **k):
        raise AssertionError("不应联网")
    monkeypatch.setattr(m, "_fetch_raw_tx", _boom)

    out = m.fetch_history("sh000001", cache_dir=cache_dir, prefer_cache=True)
    assert len(out) == 2


def test_fetch_writes_cache_on_success(cache_dir, monkeypatch):
    monkeypatch.setattr(m, "_fetch_raw_tx",
                        lambda k, c: m._reindex_columns(_mk(["2026-01-01"], [7.0])))
    out = m.fetch_history("sh000001", cache_dir=cache_dir)
    assert len(out) == 1
    assert m.cache_path(m.KIND_INDEX, "sh000001", cache_dir=cache_dir).exists()


def test_fetch_returns_none_on_failure_without_cache(cache_dir, monkeypatch, caplog):
    monkeypatch.setattr(m, "_fetch_raw_tx", lambda k, c: None)
    with caplog.at_level(logging.WARNING):
        out = m.fetch_history("sh000001", cache_dir=cache_dir, allow_stale=False)
    assert out is None
    assert any("腾讯源取数失败" in r.message for r in caplog.records)


def test_fetch_strict_raises(cache_dir, monkeypatch):
    monkeypatch.setattr(m, "_fetch_raw_tx", lambda k, c: None)
    with pytest.raises(m.MarketDataError):
        m.fetch_history("sh000001", cache_dir=cache_dir, allow_stale=False, strict=True)


def test_fetch_stale_fallback_warns_and_returns_cache(cache_dir, monkeypatch, caplog):
    """联网失败但缓存存在：allow_stale=True 时回退，且必须明确告警。"""
    p = m.cache_path(m.KIND_INDEX, "sh000001", cache_dir=cache_dir)
    m.save_cache(p, m._reindex_columns(_mk(["2026-01-01"], [1.0])))
    monkeypatch.setattr(m, "_fetch_raw_tx", lambda k, c: None)

    with caplog.at_level(logging.WARNING):
        out = m.fetch_history("sh000001", cache_dir=cache_dir, allow_stale=True)
    assert out is not None and len(out) == 1
    assert any("退回过期缓存" in r.message for r in caplog.records)


def test_fetch_allow_stale_false_never_serves_stale(cache_dir, monkeypatch):
    """日频决策路径：宁可"不可用"，也不给过期数据。"""
    p = m.cache_path(m.KIND_INDEX, "sh000001", cache_dir=cache_dir)
    m.save_cache(p, m._reindex_columns(_mk(["2026-01-01"], [1.0])))
    monkeypatch.setattr(m, "_fetch_raw_tx", lambda k, c: None)
    assert m.fetch_history("sh000001", cache_dir=cache_dir, allow_stale=False) is None


def test_fetch_refresh_ignores_cache(cache_dir, monkeypatch):
    p = m.cache_path(m.KIND_INDEX, "sh000001", cache_dir=cache_dir)
    m.save_cache(p, m._reindex_columns(_mk(["2026-01-01"], [1.0])))
    monkeypatch.setattr(m, "_fetch_raw_tx",
                        lambda k, c: m._reindex_columns(_mk(["2026-02-02"], [2.0])))
    out = m.fetch_history("sh000001", cache_dir=cache_dir, refresh=True)
    assert out["close"].tolist() == [2.0]      # 缓存被忽略，只留新数据


def test_fetch_merges_cache_and_fresh(cache_dir, monkeypatch):
    p = m.cache_path(m.KIND_INDEX, "sh000001", cache_dir=cache_dir)
    m.save_cache(p, m._reindex_columns(_mk(["2026-01-01"], [1.0])))
    monkeypatch.setattr(m, "_fetch_raw_tx",
                        lambda k, c: m._reindex_columns(_mk(["2026-01-02"], [2.0])))
    out = m.fetch_history("sh000001", cache_dir=cache_dir)
    assert out["close"].tolist() == [1.0, 2.0]


def test_fetch_network_exception_is_contained(cache_dir, monkeypatch):
    def _boom(k, c):
        raise RuntimeError("网络炸了")
    monkeypatch.setattr(m, "_fetch_raw_tx", _boom)
    assert m.fetch_history("sh000001", cache_dir=cache_dir, allow_stale=False) is None


# ====================================================================
# bars 切片 / 防未来函数
# ====================================================================

def test_bars_slice_and_full(cache_dir, monkeypatch):
    monkeypatch.setattr(m, "_fetch_raw_tx", lambda k, c: m._reindex_columns(
        _mk([f"2026-01-{d:02d}" for d in range(1, 11)], [float(d) for d in range(1, 11)])))
    assert len(m.fetch_history("sh000001", cache_dir=cache_dir, bars=3)) == 3
    assert len(m.fetch_history("sh000001", cache_dir=cache_dir, bars=0)) == 10
    assert len(m.fetch_history("sh000001", cache_dir=cache_dir, bars=999)) == 10


def test_slice_as_of_prevents_lookahead():
    df = m._reindex_columns(_mk(["2026-01-01", "2026-01-02", "2026-01-03"], [1.0, 2.0, 3.0]))
    out = m.slice_as_of(df, "2026-01-02")
    assert out["close"].tolist() == [1.0, 2.0]
    assert out["date"].iloc[-1] <= pd.Timestamp("2026-01-02")


def test_slice_as_of_accepts_timestamp():
    df = m._reindex_columns(_mk(["2026-01-01"], [1.0]))
    assert len(m.slice_as_of(df, pd.Timestamp("2026-01-01"))) == 1


# ====================================================================
# 体检工具
# ====================================================================

def test_describe_cache_reports_coverage_and_parse_errors(cache_dir):
    p = m.cache_path(m.KIND_EQUITY, "sh512480", cache_dir=cache_dir)
    m.save_cache(p, m._reindex_columns(_mk(["2026-01-01", "2026-01-02"], [1.0, 2.0])))
    out = m.describe_cache([("sh512480", "equity"), ("sh600000", "equity"), "garbage"],
                           cache_dir=cache_dir)
    assert len(out) == 3
    row = out[out["code"] == "sh512480"].iloc[0]
    assert bool(row["cached"]) is True and row["rows"] == 2
    assert bool(out[out["code"] == "sh600000"].iloc[0]["cached"]) is False
    assert "无法解析" in out[out["code"] == "garbage"].iloc[0]["path"]


# ====================================================================
# 企业行为（份额折算/拆分）检测与前复权
# —— 2026-10-04 P0.4 实测：未复权断崖会把 0.990 的相关性压到 0.666
# ====================================================================

def _mk_split_frame():
    """构造一段含 1:2 份额折算（−50%）的 ETF 序列。"""
    return m._reindex_columns(_mk(
        ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"],
        [2.00, 2.10, 1.05, 1.08, 1.06],        # 01-07 折算：2.10 → 1.05（−50%）
    ))


def test_detect_corporate_actions_finds_split():
    ev = m.detect_corporate_actions(_mk_split_frame())
    assert len(ev) == 1
    row = ev.iloc[0]
    assert pd.Timestamp(row["date"]) == pd.Timestamp("2026-01-07")
    assert row["prev_close"] == pytest.approx(2.10)
    assert row["close"] == pytest.approx(1.05)
    assert row["factor"] == pytest.approx(0.5)
    assert row["ret"] == pytest.approx(-0.5)
    assert list(ev.columns) == list(m.CA_COLUMNS)


def test_detect_corporate_actions_ignores_legal_moves():
    """±10%（主板）与 ±20%（双创）都在涨跌幅限制内，**不得**被误判为企业行为。"""
    legal = m._reindex_columns(_mk(
        ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"],
        [1.00, 1.20, 0.96, 1.10],              # +20% / −20% / +14.6%
    ))
    assert m.detect_corporate_actions(legal).empty


def test_detect_corporate_actions_edge_cases():
    assert m.detect_corporate_actions(None).empty
    assert m.detect_corporate_actions(pd.DataFrame()).empty
    assert m.detect_corporate_actions(_mk(["2026-01-05"], [1.0])).empty


def test_adjust_makes_series_continuous_and_keeps_tail():
    df = _mk_split_frame()
    adj, ev = m.adjust_corporate_actions(df)
    assert len(ev) == 1
    # 折算当日及之后保持原价（前复权以最新价为锚）
    assert adj["close"].iloc[-1] == pytest.approx(1.06)
    # 折算日之前的收盘价乘以 0.5 → 与折算日连续
    assert adj["close"].tolist()[:2] == pytest.approx([1.00, 1.05])
    # 全部日收益都回到涨跌幅限制内
    assert float(adj["close"].pct_change().abs().max()) < m.CORPORATE_ACTION_THRESHOLD
    assert adj["adj_factor"].tolist() == pytest.approx([0.5, 0.5, 1.0, 1.0, 1.0])


def test_adjust_scales_only_ohlc_not_volume_or_amount():
    """份额折算不改变成交额 ⇒ volume/amount 保持原值（复权价 × 未复权量 ≠ 成交额）。"""
    raw = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-05", "2026-01-06"]),
        "open": [2.0, 1.0], "high": [2.1, 1.05], "low": [1.9, 0.98],
        "close": [2.0, 1.0], "volume": [100.0, 250.0], "amount": [20000.0, 25000.0],
    })
    adj, ev = m.adjust_corporate_actions(m._reindex_columns(raw))
    assert len(ev) == 1
    assert adj["volume"].tolist() == [100.0, 250.0]
    assert adj["amount"].tolist() == [20000.0, 25000.0]
    assert adj["close"].tolist() == pytest.approx([1.0, 1.0])


def test_adjust_without_events_adds_identity_factor():
    df = m._reindex_columns(_mk(["2026-01-05", "2026-01-06"], [1.0, 1.1]))
    adj, ev = m.adjust_corporate_actions(df)
    assert ev.empty
    assert adj["adj_factor"].tolist() == [1.0, 1.0]
    assert adj["close"].tolist() == pytest.approx([1.0, 1.1])


def test_adjust_uses_supplied_events_without_recomputing():
    df = _mk_split_frame()
    ev = m.detect_corporate_actions(df)
    adj, ev2 = m.adjust_corporate_actions(df, events=ev)
    assert len(ev2) == len(ev) == 1


def test_fetch_equity_adjusts_by_default_and_reports_events(cache_dir, monkeypatch, caplog):
    monkeypatch.setattr(m, "_fetch_raw_tx", lambda k, c: _mk_split_frame())
    with caplog.at_level(logging.WARNING):
        out = m.fetch_equity_history("sh512480", cache_dir=cache_dir)
    assert "adj_factor" in out.columns
    assert len(out.attrs["corporate_actions"]) == 1
    assert any("企业行为" in r.message for r in caplog.records)


def test_fetch_equity_adjust_false_keeps_raw(cache_dir, monkeypatch):
    monkeypatch.setattr(m, "_fetch_raw_tx", lambda k, c: _mk_split_frame())
    out = m.fetch_equity_history("sh512480", cache_dir=cache_dir, adjust=False)
    assert "adj_factor" not in out.columns
    assert out["close"].tolist() == pytest.approx([2.00, 2.10, 1.05, 1.08, 1.06])


def test_fetch_equity_cache_stores_raw_and_adjusts_on_read(cache_dir, monkeypatch):
    """缓存必须存**原始未复权价**，复权在读出时进行（否则改口径就要重刷全部缓存）。"""
    monkeypatch.setattr(m, "_fetch_raw_tx", lambda k, c: _mk_split_frame())
    m.fetch_equity_history("sh512480", cache_dir=cache_dir)
    raw = m.load_cache(m.cache_path(m.KIND_EQUITY, "sh512480", cache_dir=cache_dir))
    assert "adj_factor" not in raw.columns
    assert raw["close"].iloc[2] == pytest.approx(1.05)
    # 离线快路径同样复权
    out = m.fetch_equity_history("sh512480", cache_dir=cache_dir, prefer_cache=True)
    assert out["close"].iloc[0] == pytest.approx(1.00)


def test_fetch_index_is_never_adjusted(cache_dir, monkeypatch):
    """指数不需要复权：即便出现 +50% 的假断点也不得被改写。"""
    monkeypatch.setattr(m, "_fetch_raw_tx", lambda k, c: m._reindex_columns(_mk(
        ["2026-01-05", "2026-01-06"], [1000.0, 1500.0])))
    out = m.fetch_index_history("sh000001", cache_dir=cache_dir)
    assert "adj_factor" not in out.columns
    assert out["close"].tolist() == pytest.approx([1000.0, 1500.0])


def test_fetch_equity_bars_slice_happens_after_adjustment(cache_dir, monkeypatch):
    """先全序列复权、后切片 —— 否则切片后再算累积因子会算错。"""
    monkeypatch.setattr(m, "_fetch_raw_tx", lambda k, c: _mk_split_frame())
    out = m.fetch_equity_history("sh512480", cache_dir=cache_dir, bars=2)
    assert len(out) == 2
    assert out["adj_factor"].tolist() == [1.0, 1.0]
    assert out["close"].tolist() == pytest.approx([1.08, 1.06])
