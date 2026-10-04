"""龙虎榜累积层（core/lhb_store.py）单元测试。

**全部离线**：不调用 zzshare，`trading_days` 也被 monkeypatch。
"""

from __future__ import annotations

import pandas as pd
import pytest

import core.lhb_store as L


# ====================================================================
# 夹具
# ====================================================================

@pytest.fixture
def store(tmp_path, monkeypatch):
    """把存储目录指到临时目录。"""
    monkeypatch.setattr(L, "STORE_DIR", tmp_path / "lhb")
    return tmp_path / "lhb"


def _raw_list_item(code="000002", name="万科Ａ", concepts="801007:房地产,801678:物业服务"):
    return {
        "stock_code": code, "stock_name": name, "concepts": concepts,
        "buy_in": 71384600.0, "turnover": 5158050000.0, "turnover_ratio": 13.48,
        "quote_change": 4.41, "amplitude": 17.89, "capitalization": 50824800000.0,
        "circ_price": 41386400000.0, "t_type": 1, "join_num": 4,
        "up_reason": "日振幅值达15%", "up_desc": "昨日首板",
    }


# ====================================================================
# 日期工具
# ====================================================================

@pytest.mark.parametrize("raw,expect", [
    ("20260930", "2026-09-30"),
    ("2026-09-30", "2026-09-30"),
    ("2026/09/30", "2026-09-30"),
])
def test_norm_date(raw, expect):
    assert L._norm_date(raw) == expect


def test_ymd():
    assert L._ymd("2026-09-30") == "20260930"


# ====================================================================
# concepts 解析
# ====================================================================

def test_parse_concepts_basic():
    assert L._parse_concepts("801007:房地产,801678:物业服务") == [
        ("801007", "房地产"), ("801678", "物业服务")]


@pytest.mark.parametrize("raw", [None, "", "  ", ",,", "nonsense"])
def test_parse_concepts_degenerate(raw):
    got = L._parse_concepts(raw)
    assert isinstance(got, list)
    if raw == "nonsense":
        assert got == [("nonsense", "")]
    else:
        assert got == []


def test_parse_concepts_keeps_code_without_name():
    assert L._parse_concepts("803001:") == [("803001", "")]


# ====================================================================
# Tier 1 归一化
# ====================================================================

def test_normalize_list_day_happy_path():
    sdf, cdf = L.normalize_list_day([_raw_list_item()], "20260930")
    assert len(sdf) == 1 and len(cdf) == 2
    row = sdf.iloc[0]
    assert row["date"] == "2026-09-30"
    assert row["stock_code"] == "000002"
    assert row["buy_in"] == 71384600.0
    assert row["turnover_ratio"] == 13.48
    # Tier2 字段应为空且标记未抓
    assert pd.isna(row["buy_total"]) and pd.isna(row["sell_total"])
    assert pd.isna(row["buy_sell_ratio"])
    assert bool(row["detail_fetched"]) is False
    assert list(sdf.columns) == L.STOCK_COLUMNS
    assert list(cdf.columns) == L.CONCEPT_COLUMNS
    assert set(cdf["plate_code"]) == {"801007", "801678"}


@pytest.mark.parametrize("raw", [None, [], [None], ["x"], [{}]])
def test_normalize_list_day_degenerate(raw):
    sdf, cdf = L.normalize_list_day(raw, "20260930")
    assert sdf.empty and cdf.empty
    assert list(sdf.columns) == L.STOCK_COLUMNS
    assert list(cdf.columns) == L.CONCEPT_COLUMNS


def test_normalize_list_day_zero_pads_and_skips_blank_code():
    items = [_raw_list_item(code="2371"), _raw_list_item(code="")]
    sdf, _ = L.normalize_list_day(items, "20260930")
    assert sdf["stock_code"].tolist() == ["002371"]     # 空代码被丢弃，短的补零


def test_normalize_list_day_dedupes_stock_and_concept():
    dup = [_raw_list_item(), _raw_list_item()]
    sdf, cdf = L.normalize_list_day(dup, "20260930")
    assert len(sdf) == 1
    assert len(cdf) == 2          # 同一只票的题材不重复


def test_normalize_list_day_handles_missing_concepts():
    item = _raw_list_item(concepts="")
    sdf, cdf = L.normalize_list_day([item], "20260930")
    assert len(sdf) == 1 and cdf.empty


def test_normalize_list_day_numeric_junk_becomes_nan():
    item = _raw_list_item()
    item["buy_in"] = "abc"
    item["turnover_ratio"] = None
    sdf, _ = L.normalize_list_day([item], "20260930")
    assert pd.isna(sdf.iloc[0]["buy_in"])
    assert pd.isna(sdf.iloc[0]["turnover_ratio"])


# ====================================================================
# Tier 2 归一化（买卖比是"板块资金温度计"的核心量）
# ====================================================================

def _raw_detail(buy_total=688965000.0, sell_total=617580000.0):
    return {
        "detail": {"buy_total": buy_total, "sell_total": sell_total,
                   "buy_in": buy_total - sell_total, "turnover": 5158050000.0,
                   "turnover_ratio": 13.48, "circ_price": 41386400000.0,
                   "quote_change": 4.41, "join_num": 4, "up_reason": "日振幅值达15%"},
        "traders": [
            {"rank": 1, "type": 1, "trader_id": "8800", "trader_name": "深股通专用",
             "buy_amount": 212787000.0, "sell_amount": 121614000.0,
             "youzi_icon": None, "group_icon": None},
            {"rank": 1, "type": 2, "trader_id": "8800", "trader_name": "深股通专用",
             "buy_amount": 212787000.0, "sell_amount": 121614000.0,
             "youzi_icon": None, "group_icon": None},
            {"rank": 4, "type": 1, "trader_id": "896", "trader_name": "机构专用",
             "buy_amount": 94843200.0, "sell_amount": 34988300.0,
             "youzi_icon": None, "group_icon": None},
        ],
    }


def test_normalize_detail_computes_ratio_and_splits_seats():
    row, seats = L.normalize_detail(_raw_detail(), "20260930", "000002")
    assert row["buy_total"] == 688965000.0
    assert row["sell_total"] == 617580000.0
    # 万科A 实测: 6.89亿买 / 6.18亿卖 → 买卖胶着（比值≈0.527）
    assert row["buy_sell_ratio"] == pytest.approx(0.5273, abs=1e-4)
    assert row["detail_fetched"] is True
    assert len(seats) == 3
    assert set(seats["side"]) == {"buy", "sell"}
    assert list(seats.columns) == L.SEAT_COLUMNS
    # 席位表按 (date, code, side, rank) 去重
    assert seats.duplicated(subset=["date", "stock_code", "side", "rank"]).sum() == 0


def test_normalize_detail_ratio_is_none_ish_when_totals_missing():
    raw = _raw_detail()
    raw["detail"].pop("buy_total")
    raw["detail"].pop("sell_total")
    row, _ = L.normalize_detail(raw, "20260930", "000002")
    assert pd.isna(row["buy_sell_ratio"])


def test_normalize_detail_ratio_avoids_divide_by_zero():
    row, _ = L.normalize_detail(_raw_detail(0.0, 0.0), "20260930", "000002")
    assert pd.isna(row["buy_sell_ratio"])


@pytest.mark.parametrize("raw", [None, [], "x", 123])
def test_normalize_detail_non_dict_returns_none_stock(raw):
    row, seats = L.normalize_detail(raw, "20260930", "000002")
    assert row is None
    assert seats.empty
    assert list(seats.columns) == L.SEAT_COLUMNS


def test_normalize_detail_handles_no_traders():
    row, seats = L.normalize_detail({"detail": {"buy_total": 10.0, "sell_total": 10.0}}, "20260930", "1")
    assert row["buy_sell_ratio"] == pytest.approx(0.5)
    assert seats.empty


# ====================================================================
# 存储：追加去重
# ====================================================================

def test_append_parquet_insert_then_update(store):
    p = store / "t.parquet"
    d1 = pd.DataFrame({"k": ["a", "b"], "v": [1, 2]})
    assert L._append_parquet(p, d1, ["k"], ["k", "v"]) == 2
    d2 = pd.DataFrame({"k": ["b", "c"], "v": [99, 3]})
    assert L._append_parquet(p, d2, ["k"], ["k", "v"]) == 3
    out = pd.read_parquet(p).sort_values("k")
    assert out["v"].tolist() == [1, 99, 3]      # b 被新值覆盖


def test_append_parquet_on_empty_store_dir(store):
    p = store / "t.parquet"
    assert L._append_parquet(p, pd.DataFrame(), ["k"], ["k", "v"]) == 0
    assert not p.exists()


def test_loaders_on_empty_store_return_typed_empty_frames(store):
    for fn, cols in ((L.load_stocks, L.STOCK_COLUMNS),
                     (L.load_concepts, L.CONCEPT_COLUMNS),
                     (L.load_seats, L.SEAT_COLUMNS)):
        df = fn()
        assert df.empty
        assert list(df.columns) == cols


def test_load_stocks_date_filter(store):
    sdf, _ = L.normalize_list_day([_raw_list_item()], "2026-09-30")
    sdf2, _ = L.normalize_list_day([_raw_list_item(code="600000")], "2026-10-08")
    L._append_parquet(store / "lhb_stocks.parquet", sdf, ["date", "stock_code"], L.STOCK_COLUMNS)
    L._append_parquet(store / "lhb_stocks.parquet", sdf2, ["date", "stock_code"], L.STOCK_COLUMNS)
    assert len(L.load_stocks()) == 2
    assert len(L.load_stocks(start="2026-10-01")) == 1
    assert L.load_stocks(end="2026-09-30")["stock_code"].tolist() == ["000002"]


# ====================================================================
# Tier2 upsert 必须保留 Tier1 字段（否则会抹掉 stock_name/concepts）
# ====================================================================

def test_upsert_stock_detail_preserves_tier1_fields(store):
    sdf, _ = L.normalize_list_day([_raw_list_item()], "2026-09-30")
    L._append_parquet(store / "lhb_stocks.parquet", sdf, ["date", "stock_code"], L.STOCK_COLUMNS)

    row, _ = L.normalize_detail(_raw_detail(), "2026-09-30", "000002")
    L._upsert_stock_detail(row)

    got = L.load_stocks().iloc[0]
    assert got["buy_total"] == 688965000.0            # Tier2 写入
    assert bool(got["detail_fetched"]) is True
    assert got["stock_name"] == "万科Ａ"               # Tier1 保留
    assert got["concepts_raw"] == "801007:房地产,801678:物业服务"
    assert got["capitalization"] == 50824800000.0


def test_upsert_stock_detail_does_not_overwrite_with_nan(store):
    sdf, _ = L.normalize_list_day([_raw_list_item()], "2026-09-30")
    L._append_parquet(store / "lhb_stocks.parquet", sdf, ["date", "stock_code"], L.STOCK_COLUMNS)
    row, _ = L.normalize_detail({"detail": {"buy_total": 10.0, "sell_total": 10.0}}, "2026-09-30", "000002")
    L._upsert_stock_detail(row)
    got = L.load_stocks().iloc[0]
    assert got["stock_name"] == "万科Ａ"                # 未被 NaN 抹掉
    assert got["buy_in"] == 71384600.0                 # Tier1 的 buy_in 保留（Tier2 该字段缺→NaN 不覆盖）


def test_upsert_stock_detail_noop_when_row_absent(store):
    sdf, _ = L.normalize_list_day([_raw_list_item()], "2026-09-30")
    L._append_parquet(store / "lhb_stocks.parquet", sdf, ["date", "stock_code"], L.STOCK_COLUMNS)
    row, _ = L.normalize_detail(_raw_detail(), "2026-01-01", "999999")   # 不存在的行
    L._upsert_stock_detail(row)                                          # 不应抛
    assert len(L.load_stocks()) == 1


# ====================================================================
# 状态：断点续跑 / 缺口统计
# ====================================================================

def test_state_roundtrip(store):
    assert L.load_state() == {"list": {}, "detail": {}}
    L.save_state({"list": {"2025-01-02": 80}, "detail": {}})
    st = L.load_state()
    assert st["list"] == {"2025-01-02": 80}
    assert "updated_at" in st


def test_state_corrupt_file_rebuilds(store):
    store.mkdir(parents=True, exist_ok=True)
    (store / "_state.json").write_text("{not json", encoding="utf-8")
    assert L.load_state() == {"list": {}, "detail": {}}


def test_missing_days_uses_state(store, monkeypatch):
    days = ["2025-01-02", "2025-01-03", "2025-01-06"]
    monkeypatch.setattr(L, "trading_days", lambda s=None, e=None, **k: days)
    L.save_state({"list": {"2025-01-02": 80}, "detail": {}})
    assert L.missing_days("2025-01-01", "2025-01-10") == ["2025-01-03", "2025-01-06"]


def test_status_reports_counts(store, monkeypatch):
    monkeypatch.setattr(L, "trading_days", lambda s=None, e=None, **k: ["2025-01-02"])
    sdf, cdf = L.normalize_list_day([_raw_list_item()], "2025-01-02")
    L._append_parquet(store / "lhb_stocks.parquet", sdf, ["date", "stock_code"], L.STOCK_COLUMNS)
    L._append_parquet(store / "lhb_concepts.parquet", cdf,
                      ["date", "stock_code", "plate_code"], L.CONCEPT_COLUMNS)
    L.save_state({"list": {"2025-01-02": 1}, "detail": {}})
    st = L.status()
    assert st["days_recorded"] == 1
    assert st["stock_rows"] == 1
    assert st["concept_rows"] == 2
    assert st["distinct_plates"] == 2
    assert st["detail_days"] == 0


def test_update_rejects_bad_tier(store, monkeypatch):
    monkeypatch.setattr(L, "trading_days", lambda s=None, e=None, **k: [])
    with pytest.raises(ValueError):
        L.update(tier=3)


def test_update_noop_when_nothing_missing(store, monkeypatch):
    monkeypatch.setattr(L, "trading_days", lambda s=None, e=None, **k: ["2025-01-02"])
    L.save_state({"list": {"2025-01-02": 80}, "detail": {}})
    stats = L.update(start="2025-01-01", end="2025-01-05", verbose=False)
    assert stats["days_fetched"] == 0
    assert stats["days_skipped"] == 1


def test_update_records_empty_days_to_avoid_retry(store, monkeypatch):
    """空结果（节假日/无上榜）也必须记录，否则每天都会重试同一天。"""
    monkeypatch.setattr(L, "trading_days", lambda s=None, e=None, **k: ["2025-01-02"])

    class FakeApi:
        def lhb_list(self, date1=None):
            return []
    monkeypatch.setattr(L, "_api", lambda: FakeApi())

    stats = L.update(start="2025-01-01", end="2025-01-05", throttle=0, verbose=False)
    assert stats["days_fetched"] == 1 and stats["stocks"] == 0
    assert L.load_state()["list"] == {"2025-01-02": 0}
    # 再跑一次应跳过
    assert L.update(start="2025-01-01", end="2025-01-05", throttle=0, verbose=False)["days_fetched"] == 0


def test_update_aborts_after_consecutive_failures(store, monkeypatch):
    days = [f"2025-01-{d:02d}" for d in range(2, 15)]
    monkeypatch.setattr(L, "trading_days", lambda s=None, e=None, **k: days)

    class BoomApi:
        def lhb_list(self, date1=None):
            raise RuntimeError("限频")
    monkeypatch.setattr(L, "_api", lambda: BoomApi())
    monkeypatch.setattr(L, "MAX_CONSECUTIVE_FAILURES", 3)
    monkeypatch.setattr(L, "MAX_RETRY", 0)

    stats = L.update(start="2025-01-01", end="2025-01-31", throttle=0, verbose=False)
    assert stats["aborted"] is True
    assert stats["failures"] == 3            # 到阈值即停，不打满
    assert stats["days_fetched"] == 0


def test_update_tier1_writes_store(store, monkeypatch):
    monkeypatch.setattr(L, "trading_days", lambda s=None, e=None, **k: ["2026-09-30"])

    class FakeApi:
        def lhb_list(self, date1=None):
            return [_raw_list_item()]
    monkeypatch.setattr(L, "_api", lambda: FakeApi())

    stats = L.update(start="2026-09-01", end="2026-10-01", throttle=0, verbose=False)
    assert stats["stocks"] == 1 and stats["concepts"] == 2
    stocks = L.load_stocks()
    assert stocks.iloc[0]["stock_code"] == "000002"
    assert len(L.load_concepts()) == 2


def test_update_max_days_limits_batch(store, monkeypatch):
    days = ["2025-01-02", "2025-01-03", "2025-01-06"]
    monkeypatch.setattr(L, "trading_days", lambda s=None, e=None, **k: days)

    class FakeApi:
        def lhb_list(self, date1=None):
            return [_raw_list_item()]
    monkeypatch.setattr(L, "_api", lambda: FakeApi())

    stats = L.update(start="2025-01-01", end="2025-01-10", throttle=0,
                     max_days=2, verbose=False)
    assert stats["days_fetched"] == 2
    assert len(L.missing_days("2025-01-01", "2025-01-10")) == 1


# ====================================================================
# 空结果护栏（防止把"尚未发布"的真实交易日永久封死）
# ====================================================================

def test_empty_result_today_is_not_recorded(store, monkeypatch):
    """当天（尚未过去一天）返回空 ⇒ 判为「尚未发布」，**不写 state**，下次重试。"""
    today = L._today_str()
    monkeypatch.setattr(L, "trading_days", lambda s=None, e=None, **k: [today])

    class EmptyApi:
        def lhb_list(self, date1=None):
            return []
    monkeypatch.setattr(L, "_api", lambda: EmptyApi())

    stats = L.update(start=today, end=today, throttle=0, verbose=False)
    assert stats["days_pending"] == 1
    assert stats["days_fetched"] == 0
    assert L.load_state()["list"] == {}                      # 关键：没有被记录
    # 下次运行仍会尝试（不会被当成已完成）
    monkeypatch.setattr(L, "trading_days", lambda s=None, e=None, **k: [today])
    assert L.update(start=today, end=today, throttle=0, verbose=False)["days_pending"] == 1


def test_empty_result_past_day_is_recorded(store, monkeypatch):
    """过去的交易日返回空 ⇒ 可以记录 0（真·无上榜），避免反复重试。"""
    monkeypatch.setattr(L, "trading_days", lambda s=None, e=None, **k: ["2025-01-02"])

    class EmptyApi:
        def lhb_list(self, date1=None):
            return []
    monkeypatch.setattr(L, "_api", lambda: EmptyApi())

    stats = L.update(start="2025-01-01", end="2025-01-05", throttle=0, verbose=False)
    assert stats["days_fetched"] == 1 and stats["days_pending"] == 0
    assert L.load_state()["list"] == {"2025-01-02": 0}


def test_is_publish_risky_boundary(monkeypatch):
    monkeypatch.setattr(L, "_today_str", lambda: "2026-10-03")
    assert L._is_publish_risky("2026-10-03") is True     # 今天：有风险
    assert L._is_publish_risky("2026-10-04") is True     # 未来：有风险
    assert L._is_publish_risky("2026-10-02") is False    # 昨天：安全


# ====================================================================
# Tier 2 的待处理判据必须看 state['detail']，而不是 state['list']
# ====================================================================

def test_tier2_processes_days_already_tier1(store, monkeypatch):
    """**关键回归**：Tier1 已回填的日期，Tier2 仍必须能补上。

    修复前 todo 依据 state['list']，导致 `--tier 2` 对已回填日期整体跳过 → 永远补不上。
    """
    days = ["2025-01-02", "2025-01-03"]
    monkeypatch.setattr(L, "trading_days", lambda s=None, e=None, **k: days)

    calls = {"list": 0, "detail": 0}

    class FakeApi:
        def lhb_list(self, date1=None):
            calls["list"] += 1
            return [{"stock_code": "000002", "stock_name": "万科Ａ", "concepts": "801007:房地产"}]
        def lhb_detail(self, date1=None, stock_code=None):
            calls["detail"] += 1
            return _raw_detail()
    monkeypatch.setattr(L, "_api", lambda: FakeApi())

    # 先只做 Tier 1
    s1 = L.update(start="2025-01-01", end="2025-01-10", tier=1, throttle=0, verbose=False)
    assert s1["days_fetched"] == 2 and calls["list"] == 2 and calls["detail"] == 0

    # 再做 Tier 2：必须处理这 2 天，且**不重复调用 lhb_list**
    s2 = L.update(start="2025-01-01", end="2025-01-10", tier=2, throttle=0, verbose=False)
    assert s2["detail_fetched"] == 2
    assert calls["list"] == 2          # Tier1 未重复取
    assert calls["detail"] == 2
    assert set(L.load_state()["detail"].keys()) == set(days)

    # Tier2 结果必须真的写进了个股表，且 Tier1 字段未被抹掉
    allrows = L.load_stocks()
    got = allrows[(allrows["date"] == "2025-01-02") & (allrows["stock_code"] == "000002")].iloc[0]
    assert got["buy_total"] == 688965000.0
    assert got["stock_name"] == "万科Ａ"
    assert got["concepts_raw"] == "801007:房地产"
    assert bool(got["detail_fetched"]) is True
    # 两天都应有毛额（Tier2 覆盖了两天）
    assert allrows["detail_fetched"].fillna(False).astype(bool).all()
    assert len(L.load_seats()) == 6          # 每天 3 条席位 × 2 天


def test_tier2_fills_missing_tier1_in_same_pass(store, monkeypatch):
    """全新的一天走 Tier 2 时，应在同一次循环里先补 Tier 1。"""
    monkeypatch.setattr(L, "trading_days", lambda s=None, e=None, **k: ["2025-01-02"])

    class FakeApi:
        def lhb_list(self, date1=None):
            return [{"stock_code": "000002", "stock_name": "万科Ａ", "concepts": "801007:房地产"}]
        def lhb_detail(self, date1=None, stock_code=None):
            return _raw_detail()
    monkeypatch.setattr(L, "_api", lambda: FakeApi())

    s = L.update(start="2025-01-01", end="2025-01-05", tier=2, throttle=0, verbose=False)
    assert s["days_fetched"] == 1 and s["detail_fetched"] == 1
    assert L.load_state()["list"] == {"2025-01-02": 1}
    assert L.load_state()["detail"] == {"2025-01-02": 1}


def test_tier2_skips_days_already_detailed(store, monkeypatch):
    days = ["2025-01-02"]
    monkeypatch.setattr(L, "trading_days", lambda s=None, e=None, **k: days)

    class FakeApi:
        def lhb_list(self, date1=None):
            return [{"stock_code": "000002", "stock_name": "万科Ａ", "concepts": ""}]
        def lhb_detail(self, date1=None, stock_code=None):
            return _raw_detail()
    monkeypatch.setattr(L, "_api", lambda: FakeApi())

    L.update(start="2025-01-01", end="2025-01-05", tier=2, throttle=0, verbose=False)
    again = L.update(start="2025-01-01", end="2025-01-05", tier=2, throttle=0, verbose=False)
    assert again["detail_fetched"] == 0


def test_tier2_bulk_upsert_writes_once_per_day(store, monkeypatch):
    """Tier 2 必须按天批量落盘（不是每股重写整个 parquet）。"""
    monkeypatch.setattr(L, "trading_days", lambda s=None, e=None, **k: ["2025-01-02"])

    class FakeApi:
        def lhb_list(self, date1=None):
            return [{"stock_code": "000002", "stock_name": "万科Ａ", "concepts": ""},
                    {"stock_code": "600000", "stock_name": "浦发银行", "concepts": ""}]
        def lhb_detail(self, date1=None, stock_code=None):
            return _raw_detail()
    monkeypatch.setattr(L, "_api", lambda: FakeApi())

    writes = {"n": 0}
    orig = L._upsert_stock_details_bulk

    def counting(rows):
        writes["n"] += 1
        return orig(rows)
    monkeypatch.setattr(L, "_upsert_stock_details_bulk", counting)

    s = L.update(start="2025-01-01", end="2025-01-05", tier=2, throttle=0, verbose=False)
    assert s["detail_fetched"] == 2
    assert writes["n"] == 1                       # 2 只股票只写 1 次


def test_bulk_upsert_ignores_unknown_keys(store):
    """Tier2 不应凭空插入库中不存在的 (date, code)。"""
    sdf, _ = L.normalize_list_day([_raw_list_item()], "2026-09-30")
    L._append_parquet(store / "lhb_stocks.parquet", sdf,
                      ["date", "stock_code"], L.STOCK_COLUMNS)
    row, _ = L.normalize_detail(_raw_detail(), "2026-01-01", "999999")
    L._upsert_stock_details_bulk([row])
    assert len(L.load_stocks()) == 1              # 没多出行


def test_status_reports_zero_days(store, monkeypatch):
    monkeypatch.setattr(L, "trading_days", lambda s=None, e=None, **k: [])
    L.save_state({"list": {"2025-01-02": 80, "2025-01-03": 0}, "detail": {}})
    assert L.status()["zero_days"] == ["2025-01-03"]


# ====================================================================
# Tier 2 的滚动窗口（recent）—— 防止每日任务去补历史 300+ 天
# ====================================================================

def _api_factory(calls):
    class FakeApi:
        def lhb_list(self, date1=None):
            calls["list"] += 1
            return [{"stock_code": "000002", "stock_name": "万科Ａ", "concepts": ""}]
        def lhb_detail(self, date1=None, stock_code=None):
            calls["detail"] += 1
            return _raw_detail()
    return FakeApi()


def test_recent_limits_tier2_to_window(store, monkeypatch):
    """recent=2 ⇒ 只补最近 2 天的 detail，更早的日子不碰。"""
    days = ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"]
    monkeypatch.setattr(L, "trading_days", lambda s=None, e=None, **k: days)
    calls = {"list": 0, "detail": 0}
    monkeypatch.setattr(L, "_api", lambda: _api_factory(calls))

    # 先全量 Tier1
    L.update(start="2025-01-01", end="2025-01-10", tier=1, throttle=0, verbose=False)
    assert calls["list"] == 4

    calls["detail"] = 0
    s = L.update(start="2025-01-01", end="2025-01-10", tier=2, recent=2,
                 throttle=0, verbose=False)
    assert s["detail_fetched"] == 2
    assert calls["detail"] == 2                      # 只有窗口内 2 天
    assert set(L.load_state()["detail"].keys()) == {"2025-01-06", "2025-01-07"}


def test_recent_does_not_shrink_tier1_selfhealing(store, monkeypatch):
    """recent 只约束 Tier 2 —— Tier 1 仍必须扫全区间（否则老缺口永远补不上）。"""
    days = ["2025-01-02", "2025-01-03", "2025-01-06"]
    monkeypatch.setattr(L, "trading_days", lambda s=None, e=None, **k: days)
    calls = {"list": 0, "detail": 0}
    monkeypatch.setattr(L, "_api", lambda: _api_factory(calls))

    s = L.update(start="2025-01-01", end="2025-01-10", tier=2, recent=1,
                 throttle=0, verbose=False)
    # Tier1 补齐 3 天（全区间），Tier2 只做最后 1 天
    assert s["days_fetched"] == 3
    assert calls["list"] == 3
    assert calls["detail"] == 1
    assert set(L.load_state()["list"].keys()) == set(days)
    assert set(L.load_state()["detail"].keys()) == {"2025-01-06"}


def test_recent_only_applies_to_tier2(store, monkeypatch):
    days = ["2025-01-02", "2025-01-03", "2025-01-06"]
    monkeypatch.setattr(L, "trading_days", lambda s=None, e=None, **k: days)
    calls = {"list": 0, "detail": 0}
    monkeypatch.setattr(L, "_api", lambda: _api_factory(calls))
    s = L.update(start="2025-01-01", end="2025-01-10", tier=1, recent=1,
                 throttle=0, verbose=False)
    assert s["days_fetched"] == 3                    # Tier1 不受 recent 影响
    assert calls["detail"] == 0


def test_recent_larger_than_range_is_noop(store, monkeypatch):
    days = ["2025-01-02", "2025-01-03"]
    monkeypatch.setattr(L, "trading_days", lambda s=None, e=None, **k: days)
    calls = {"list": 0, "detail": 0}
    monkeypatch.setattr(L, "_api", lambda: _api_factory(calls))
    L.update(start="2025-01-01", end="2025-01-10", tier=1, throttle=0, verbose=False)
    s = L.update(start="2025-01-01", end="2025-01-10", tier=2, recent=999,
                 throttle=0, verbose=False)
    assert s["detail_fetched"] == 2


