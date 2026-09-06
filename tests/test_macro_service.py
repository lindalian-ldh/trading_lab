"""macro-data-service 单元测试。

通过 mock fetchers._call_akshare 返回构造 DataFrame，验证：
  - 日期归一化
  - 单指标获取与字段标准化
  - LPR 多期限拆分
  - SQLite 增量去重 / 全量覆盖
  - Parquet 增量去重
不依赖真实 akshare 与网络。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pandas as pd
import pytest

# 服务目录名含连字符，无法作为包导入；将其加入 sys.path 后按模块名导入
SERVICE_DIR = Path(__file__).resolve().parent.parent / "services" / "macro-data-service"
sys.path.insert(0, str(SERVICE_DIR))

import fetchers  # noqa: E402
import storage  # noqa: E402


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    """用临时目录替换 settings，避免污染真实 logs/data。"""
    fake = types.SimpleNamespace(
        db_path=tmp_path / "trading.db",
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        fred_api_key="",
    )
    monkeypatch.setattr(storage, "settings", fake)
    monkeypatch.setattr(fetchers, "settings", fake)
    return fake


# --------------------------- 日期归一化 --------------------------- #

def test_normalize_date_variants():
    assert fetchers._normalize_date("2024年7月") == "2024-07-01"
    assert fetchers._normalize_date("2024年07月份") == "2024-07-01"
    assert fetchers._normalize_date("2024年二季度") == "2024-06-30"
    assert fetchers._normalize_date("2024Q2") == "2024-06-30"
    assert fetchers._normalize_date("202407") == "2024-07-01"
    assert fetchers._normalize_date("2024-7") == "2024-07-01"
    assert fetchers._normalize_date("2024-07-15") == "2024-07-15"
    assert fetchers._normalize_date(None) is None
    assert fetchers._normalize_date("") is None


# --------------------------- 单指标获取 --------------------------- #

def test_get_cpi_cn_mock(fake_settings, monkeypatch):
    df = pd.DataFrame({
        "月份": ["2024年6月", "2024年7月"],
        "同比增长": [0.2, 0.5],
    })
    monkeypatch.setattr(fetchers, "_call_akshare", lambda name, **kw: df)
    monkeypatch.setattr(fetchers, "_akshare_version", lambda: "test-1.0")

    recs = fetchers.get_cpi_cn()
    assert len(recs) == 2
    r0 = recs[0]
    assert r0["indicator"] == "CPI"
    assert r0["country"] == "CN"
    assert r0["frequency"] == "M"
    assert r0["unit"] == "%"
    assert r0["date"] == "2024-06-01"
    assert r0["value"] == pytest.approx(0.2)
    assert r0["source"] == "akshare"
    assert "macro_china_cpi" in r0["source_url"]


def test_get_cpi_cn_value_col_fallback(fake_settings, monkeypatch):
    """数值列名不在候选时，应回退到第一个可转数值的列。"""
    df = pd.DataFrame({
        "月份": ["2024年1月", "2024年2月"],
        "奇怪列名": [1.1, 2.2],
    })
    monkeypatch.setattr(fetchers, "_call_akshare", lambda name, **kw: df)
    monkeypatch.setattr(fetchers, "_akshare_version", lambda: "x")
    recs = fetchers.get_cpi_cn()
    assert len(recs) == 2
    assert recs[0]["value"] == pytest.approx(1.1)


def test_get_cpi_cn_recent_periods(fake_settings, monkeypatch):
    df = pd.DataFrame({"月份": [f"2024年{i}月" for i in range(1, 6)],
                       "同比增长": [0.1, 0.2, 0.3, 0.4, 0.5]})
    monkeypatch.setattr(fetchers, "_call_akshare", lambda name, **kw: df)
    monkeypatch.setattr(fetchers, "_akshare_version", lambda: "x")
    recs = fetchers.get_cpi_cn(recent_periods=2)
    assert len(recs) == 2
    assert recs[-1]["date"] == "2024-05-01"


def test_get_cpi_cn_empty(fake_settings, monkeypatch):
    monkeypatch.setattr(fetchers, "_call_akshare", lambda name, **kw: pd.DataFrame())
    assert fetchers.get_cpi_cn() == []


def test_get_cpi_cn_error_returns_empty(fake_settings, monkeypatch):
    def boom(name, **kw):
        raise RuntimeError("network down")
    monkeypatch.setattr(fetchers, "_call_akshare", boom)
    monkeypatch.setattr(fetchers, "_with_retry", lambda fn, r, d: fn())  # 跳过重试等待
    assert fetchers.get_cpi_cn() == []


# --------------------------- LPR 多期限 --------------------------- #

def test_get_lpr_cn_split(fake_settings, monkeypatch):
    df = pd.DataFrame({
        "报告日": ["2024-07-22", "2024-08-20"],
        "1年LPR": [3.45, 3.35],
        "5年LPR": [3.95, 3.85],
    })
    monkeypatch.setattr(fetchers, "_call_akshare", lambda name, **kw: df)
    monkeypatch.setattr(fetchers, "_akshare_version", lambda: "x")
    recs = fetchers.get_lpr_cn()
    inds = sorted(r["indicator"] for r in recs)
    assert inds == ["LPR1Y", "LPR1Y", "LPR5Y", "LPR5Y"]
    # 同一日期两条，indicator 不同，维持唯一键
    dates = {(r["indicator"], r["date"]) for r in recs}
    assert len(dates) == 4
    assert all(r["extra"]["term"] in ("1Y", "5Y") for r in recs)


# --------------------------- SQLite 存储 --------------------------- #

def _sample_records():
    return [
        {"indicator": "CPI", "name": "居民消费价格指数", "country": "CN", "frequency": "M",
         "date": "2024-07-01", "value": 0.5, "unit": "%", "source": "akshare",
         "source_url": "akshare:x@t", "fetch_time": "2026-08-08 10:00:00", "extra": {}},
        {"indicator": "CPI", "name": "居民消费价格指数", "country": "CN", "frequency": "M",
         "date": "2024-06-01", "value": 0.2, "unit": "%", "source": "akshare",
         "source_url": "akshare:x@t", "fetch_time": "2026-08-08 10:00:00", "extra": {}},
    ]


def test_sqlite_incremental_dedup(fake_settings):
    recs = _sample_records()
    n1 = storage.save_records_sqlite(recs, overwrite=False)
    assert n1 == 2
    n2 = storage.save_records_sqlite(recs, overwrite=False)  # 重复应被忽略
    assert n2 == 0
    df = storage.load_records_sqlite(indicator="CPI")
    assert len(df) == 2


def test_sqlite_overwrite(fake_settings):
    recs = _sample_records()
    storage.save_records_sqlite(recs, overwrite=False)
    # 修改其中一条的 value，覆盖模式应更新
    recs[0] = {**recs[0], "value": 0.9}
    storage.save_records_sqlite(recs, overwrite=True)
    df = storage.load_records_sqlite(indicator="CPI")
    assert len(df) == 2  # 仍为 2 条，不会因覆盖翻倍
    row = df[df["date"] == recs[0]["date"]].iloc[0]
    assert row["value"] == pytest.approx(0.9)


def test_sqlite_overwrite_deletes_stale(fake_settings):
    """overwrite 应删除新数据中不再存在的旧记录（修复 INSERT OR REPLACE 残留旧行的 bug）。"""
    # 先插入 3 条历史记录（含一条 2024-05-01）
    old = _sample_records()  # 2 条：2024-07-01, 2024-06-01
    old.append({**old[0], "date": "2024-05-01", "value": 0.4})
    storage.save_records_sqlite(old, overwrite=False)
    assert len(storage.load_records_sqlite(indicator="CPI")) == 3
    # overwrite 只写 2 条（不含 2024-05-01），多余的旧记录应被删除
    new = _sample_records()
    storage.save_records_sqlite(new, overwrite=True)
    df = storage.load_records_sqlite(indicator="CPI")
    assert len(df) == 2
    assert "2024-05-01" not in set(df["date"])  # 不在新批次的旧记录已清除


# --------------------------- Parquet 存储 --------------------------- #

def test_parquet_incremental_dedup(fake_settings):
    recs = _sample_records()
    storage.save_records_parquet(recs, overwrite=False)
    # 再次写入（含一条新值），应合并去重
    recs2 = [{**recs[0], "value": 0.55}, recs[1]]
    storage.save_records_parquet(recs2, overwrite=False)
    out = fake_settings.data_dir / "macro" / "CPI" / "2024" / "CPI_CN_2024.parquet"
    assert out.exists()
    df = pd.read_parquet(out)
    assert len(df) == 2  # 去重后仍 2 条
    row = df[df["date"] == "2024-07-01"].iloc[0]
    assert row["value"] == pytest.approx(0.55)  # 保留最新


# --------------------------- 注册表 --------------------------- #

def test_registry_keys():
    expected = {
        "cpi_cn", "ppi_cn", "gdp_cn", "pmi_cn", "m2_cn", "lpr_cn",
        "social_financing_cn", "industrial_production_cn", "unemployment_cn",
    }
    assert expected.issubset(set(fetchers.REGISTRY))


def test_config_loads_and_indicator_flag():
    from config_loader import is_indicator_enabled, load_config

    cfg = load_config()
    assert cfg["storage"]["type"] in ("sqlite", "parquet")
    assert is_indicator_enabled(cfg, "cpi_cn") is True


# --------------------------- 美国及商品指标 --------------------------- #

def test_get_usa_phs_normalizes_time_col(fake_settings, monkeypatch):
    """phs 用 时间 列(2026年07月)作日期，最新一期现值 NaN 应被丢弃。"""
    df = pd.DataFrame({
        "时间": ["2026年07月", "2026年06月", "2026年05月"],
        "前值": [-2.4, 3.2, 1.1],
        "现值": [float("nan"), -2.4, 3.2],
        "发布日期": ["2026-08-11", "2026-07-09", "2026-06-09"],
    })
    monkeypatch.setattr(fetchers, "_call_akshare", lambda name, **kw: df)
    monkeypatch.setattr(fetchers, "_akshare_version", lambda: "x")
    recs = fetchers.get_usa_phs(recent_periods=1)
    assert len(recs) == 1
    assert recs[0]["date"] == "2026-06-01"
    assert recs[0]["value"] == pytest.approx(-2.4)
    # extra 含发布日期与前值
    assert recs[0]["extra"]["发布日期"] == "2026-07-09"
    assert recs[0]["extra"]["前值"] == pytest.approx(3.2)


def test_get_cons_gold_extra_fields(fake_settings, monkeypatch):
    """黄金库存：值取总库存，extra 收入增持/减持与总价值。"""
    df = pd.DataFrame({
        "商品": ["黄金", "黄金"],
        "日期": ["2026-08-06", "2026-08-07"],
        "总库存": [1014.719, 1017.537],
        "增持/减持": [0.571, 2.818],
        "总价值": [1.39e11, 1.42e11],
    })
    monkeypatch.setattr(fetchers, "_call_akshare", lambda name, **kw: df)
    monkeypatch.setattr(fetchers, "_akshare_version", lambda: "x")
    recs = fetchers.get_cons_gold(recent_periods=1)
    assert len(recs) == 1
    r = recs[0]
    assert r["indicator"] == "GOLD_INV"
    assert r["unit"] == "吨"
    assert r["value"] == pytest.approx(1017.537)
    assert r["extra"]["增持/减持"] == pytest.approx(2.818)
    assert r["extra"]["总价值"] == pytest.approx(1.42e11)


def test_us_indicators_set_and_registry():
    assert fetchers.US_INDICATORS == {
        "usa_phs", "cons_gold", "cons_silver",
        "fred_gdp", "fred_gdpc1", "fred_cpiaucsl", "fred_cpilfesl",
        "fred_ppiaco", "fred_unrate", "fred_payems", "fred_icsa",
        "fred_dgs6mo", "fred_dgs1", "fred_dgs10", "fred_gs10",
        "fred_dtwexbgs", "fred_dtwexm", "fred_dtwexo", "fred_rtwexbgs",
        "sge_gold", "sge_silver",
    }
    assert fetchers.US_INDICATORS.issubset(set(fetchers.REGISTRY))


# --------------------------- 美国表存储 --------------------------- #

def _us_sample_records():
    return [
        {"indicator": "USA_PHS", "name": "美国未决房屋销售月率", "country": "US", "frequency": "M",
         "date": "2026-06-01", "value": -2.4, "unit": "%", "source": "akshare",
         "source_url": "akshare:x@t", "fetch_time": "2026-08-08 10:00:00",
         "extra": {"前值": 3.2, "发布日期": "2026-07-09"}},
    ]


def test_sqlite_us_table_separate_from_cn(fake_settings):
    """美国指标写入 macro_us_data 表，与 macro_data 表隔离。"""
    recs = _us_sample_records()
    n = storage.save_records_sqlite(recs, overwrite=False, table=storage.TABLE_US)
    assert n == 1
    # macro_data 表不应含此记录
    cn_df = storage.load_records_sqlite(table=storage.TABLE)
    assert len(cn_df) == 0
    # macro_us_data 表应有此记录
    us_df = storage.load_records_sqlite(table=storage.TABLE_US)
    assert len(us_df) == 1
    assert us_df.iloc[0]["indicator"] == "USA_PHS"


def test_sqlite_us_table_incremental_dedup(fake_settings):
    recs = _us_sample_records()
    assert storage.save_records_sqlite(recs, overwrite=False, table=storage.TABLE_US) == 1
    assert storage.save_records_sqlite(recs, overwrite=False, table=storage.TABLE_US) == 0
    df = storage.load_records_sqlite(table=storage.TABLE_US)
    assert len(df) == 1


def test_sqlite_us_table_overwrite(fake_settings):
    recs = _us_sample_records()
    storage.save_records_sqlite(recs, overwrite=False, table=storage.TABLE_US)
    recs[0] = {**recs[0], "value": 0.5}
    storage.save_records_sqlite(recs, overwrite=True, table=storage.TABLE_US)
    df = storage.load_records_sqlite(table=storage.TABLE_US)
    assert len(df) == 1
    assert df.iloc[0]["value"] == pytest.approx(0.5)


def test_reset_macro_us_db(fake_settings):
    storage.save_records_sqlite(_us_sample_records(), table=storage.TABLE_US)
    assert storage.reset_macro_us_db() == 1
    assert len(storage.load_records_sqlite(table=storage.TABLE_US)) == 0


def test_parquet_us_subroot(fake_settings):
    """美国指标 Parquet 写入 data/macro_us 目录。"""
    recs = _us_sample_records()
    storage.save_records_parquet(recs, overwrite=False, sub_root="macro_us")
    out = fake_settings.data_dir / "macro_us" / "USA_PHS" / "2026" / "USA_PHS_US_2026.parquet"
    assert out.exists()
    # 中国目录不应存在该文件
    cn_out = fake_settings.data_dir / "macro" / "USA_PHS" / "2026" / "USA_PHS_US_2026.parquet"
    assert not cn_out.exists()


def test_us_indicator_config_enabled():
    from config_loader import is_indicator_enabled, load_config

    cfg = load_config()
    for key in fetchers.US_INDICATORS:
        assert is_indicator_enabled(cfg, key) is True, f"{key} 应启用"


# --------------------------- FRED 指标 --------------------------- #

def _fred_df():
    """模拟 FRED 返回：索引为观测日期(DatetimeIndex)，列为 series_id。"""
    idx = pd.to_datetime(["2025-06-01", "2025-07-01"])
    return pd.DataFrame({"UNRATE": [4.1, 4.2]}, index=idx)


def test_get_fred_unrate_mock(fake_settings, monkeypatch):
    monkeypatch.setattr(fetchers, "_call_fred", lambda sid, start, end: _fred_df())
    monkeypatch.setattr(fetchers, "_pdr_version", lambda: "test-1.0")
    recs = fetchers.get_fred_unrate()
    assert len(recs) == 2
    r = recs[-1]
    assert r["indicator"] == "UNRATE"
    assert r["country"] == "US"
    assert r["frequency"] == "M"
    assert r["unit"] == "%"
    assert r["date"] == "2025-07-01"
    assert r["value"] == pytest.approx(4.2)
    assert r["source"] == "FRED"
    assert "UNRATE" in r["source_url"]


def test_get_fred_gdp_recent_one(fake_settings, monkeypatch):
    """recent_periods=1 应只取最近一条。"""
    idx = pd.to_datetime(["2025-04-01", "2025-07-01"])
    df = pd.DataFrame({"GDP": [28000.0, 28500.0]}, index=idx)
    monkeypatch.setattr(fetchers, "_call_fred", lambda sid, start, end: df)
    monkeypatch.setattr(fetchers, "_pdr_version", lambda: "x")
    recs = fetchers.get_fred_gdp(recent_periods=1)
    assert len(recs) == 1
    assert recs[0]["date"] == "2025-07-01"
    assert recs[0]["value"] == pytest.approx(28500.0)


def test_get_fred_nan_dropped(fake_settings, monkeypatch):
    """FRED 数据缺失(NaN)应被丢弃。"""
    idx = pd.to_datetime(["2025-06-01", "2025-07-01", "2025-08-01"])
    df = pd.DataFrame({"ICSA": [250000.0, float("nan"), 240000.0]}, index=idx)
    monkeypatch.setattr(fetchers, "_call_fred", lambda sid, start, end: df)
    monkeypatch.setattr(fetchers, "_pdr_version", lambda: "x")
    recs = fetchers.get_fred_icsa()
    assert len(recs) == 2
    assert recs[-1]["date"] == "2025-08-01"


def test_get_fred_no_api_key_returns_empty(fake_settings, monkeypatch):
    """未配置 FRED_API_KEY 时应返回 [] 且不抛异常。"""
    # fake_settings.fred_api_key == ""
    monkeypatch.setattr(fetchers, "_with_retry", lambda fn, r, d: fn())  # 不重试等待
    monkeypatch.setattr(fetchers, "_pdr_version", lambda: "x")
    assert fetchers.get_fred_unrate() == []


def test_get_fred_error_returns_empty(fake_settings, monkeypatch):
    def boom(sid, start, end):
        raise RuntimeError("network down")
    monkeypatch.setattr(fetchers, "_call_fred", boom)
    monkeypatch.setattr(fetchers, "_with_retry", lambda fn, r, d: fn())
    monkeypatch.setattr(fetchers, "_pdr_version", lambda: "x")
    assert fetchers.get_fred_gdp() == []


def test_fred_indicators_in_us_table(fake_settings, monkeypatch):
    """FRED 指标写入 macro_us_data 表（通过 US_INDICATORS 路由）。"""
    idx = pd.to_datetime(["2025-07-01"])
    monkeypatch.setattr(fetchers, "_call_fred", lambda sid, start, end: pd.DataFrame({"UNRATE": [4.2]}, index=idx))
    monkeypatch.setattr(fetchers, "_pdr_version", lambda: "x")
    recs = fetchers.get_fred_unrate(recent_periods=1)
    assert "fred_unrate" in fetchers.US_INDICATORS  # 路由依据
    storage.save_records_sqlite(recs, overwrite=False, table=storage.TABLE_US)
    df = storage.load_records_sqlite(table=storage.TABLE_US)
    assert (df["indicator"] == "UNRATE").any()


# --------------------------- FRED 国债收益率 --------------------------- #

def test_get_fred_dgs10_mock(fake_settings, monkeypatch):
    """10年期国债收益率：日度数据，取最近一条。"""
    idx = pd.to_datetime(["2025-07-01", "2025-07-02"])
    df = pd.DataFrame({"DGS10": [4.35, 4.40]}, index=idx)
    monkeypatch.setattr(fetchers, "_call_fred", lambda sid, start, end: df)
    monkeypatch.setattr(fetchers, "_pdr_version", lambda: "x")
    recs = fetchers.get_fred_dgs10(recent_periods=1)
    assert len(recs) == 1
    r = recs[0]
    assert r["indicator"] == "DGS10"
    assert r["country"] == "US"
    assert r["frequency"] == "D"
    assert r["unit"] == "%"
    assert r["date"] == "2025-07-02"
    assert r["value"] == pytest.approx(4.40)
    assert r["source"] == "FRED"


def test_get_fred_dgs1_mock(fake_settings, monkeypatch):
    """1年期国债收益率。"""
    idx = pd.to_datetime(["2025-07-01"])
    df = pd.DataFrame({"DGS1": [4.30]}, index=idx)
    monkeypatch.setattr(fetchers, "_call_fred", lambda sid, start, end: df)
    monkeypatch.setattr(fetchers, "_pdr_version", lambda: "x")
    recs = fetchers.get_fred_dgs1()
    assert len(recs) == 1
    assert recs[0]["indicator"] == "DGS1"
    assert recs[0]["value"] == pytest.approx(4.30)


def test_get_fred_dgs6mo_mock(fake_settings, monkeypatch):
    """6个月期国债收益率。"""
    idx = pd.to_datetime(["2025-07-01"])
    df = pd.DataFrame({"DGS6MO": [4.25]}, index=idx)
    monkeypatch.setattr(fetchers, "_call_fred", lambda sid, start, end: df)
    monkeypatch.setattr(fetchers, "_pdr_version", lambda: "x")
    recs = fetchers.get_fred_dgs6mo()
    assert len(recs) == 1
    assert recs[0]["indicator"] == "DGS6MO"
    assert recs[0]["value"] == pytest.approx(4.25)


def test_get_fred_gs10_mock(fake_settings, monkeypatch):
    """10年期国债收益率（月度）：与 DGS10 口径一致但 frequency=M，便于与月频指标同图。"""
    idx = pd.to_datetime(["2025-06-01", "2025-07-01"])
    df = pd.DataFrame({"GS10": [4.30, 4.40]}, index=idx)
    monkeypatch.setattr(fetchers, "_call_fred", lambda sid, start, end: df)
    monkeypatch.setattr(fetchers, "_pdr_version", lambda: "x")
    recs = fetchers.get_fred_gs10(recent_periods=1)
    assert len(recs) == 1
    r = recs[0]
    assert r["indicator"] == "GS10"
    assert r["country"] == "US"
    assert r["frequency"] == "M"   # 关键：月频（区别于 DGS10 的日频 D）
    assert r["unit"] == "%"
    assert r["date"] == "2025-07-01"
    assert r["value"] == pytest.approx(4.40)
    assert r["source"] == "FRED"
    assert "fred_gs10" in fetchers.US_INDICATORS
    assert "fred_gs10" in fetchers.REGISTRY


def test_fred_dgs_in_us_indicators():
    """DGS 指标应注册在 US_INDICATORS 中，路由到 macro_us_data 表。"""
    for key in ("fred_dgs6mo", "fred_dgs1", "fred_dgs10"):
        assert key in fetchers.US_INDICATORS
        assert key in fetchers.REGISTRY


# --------------------------- FRED 美元指数 --------------------------- #

def test_get_fred_dtwexbgs_mock(fake_settings, monkeypatch):
    """名义广义美元指数：日度数据。"""
    idx = pd.to_datetime(["2026-07-30", "2026-07-31"])
    df = pd.DataFrame({"DTWEXBGS": [119.68, 119.70]}, index=idx)
    monkeypatch.setattr(fetchers, "_call_fred", lambda sid, start, end: df)
    monkeypatch.setattr(fetchers, "_pdr_version", lambda: "x")
    recs = fetchers.get_fred_dtwexbgs(recent_periods=1)
    assert len(recs) == 1
    r = recs[0]
    assert r["indicator"] == "DTWEXBGS"
    assert r["country"] == "US"
    assert r["frequency"] == "D"
    assert r["unit"] == "指数"
    assert r["date"] == "2026-07-31"
    assert r["value"] == pytest.approx(119.70)
    assert r["source"] == "FRED"


def test_get_fred_dtwexm_mock(fake_settings, monkeypatch):
    """名义主要货币美元指数（已停止更新）。"""
    idx = pd.to_datetime(["2019-12-30", "2019-12-31"])
    df = pd.DataFrame({"DTWEXM": [90.75, 90.82]}, index=idx)
    monkeypatch.setattr(fetchers, "_call_fred", lambda sid, start, end: df)
    monkeypatch.setattr(fetchers, "_pdr_version", lambda: "x")
    recs = fetchers.get_fred_dtwexm(recent_periods=1)
    assert len(recs) == 1
    assert recs[0]["indicator"] == "DTWEXM"
    assert recs[0]["date"] == "2019-12-31"
    assert recs[0]["value"] == pytest.approx(90.82)


def test_get_fred_dtwexo_mock(fake_settings, monkeypatch):
    """名义其他重要贸易伙伴美元指数（已停止更新）。"""
    idx = pd.to_datetime(["2019-12-31"])
    df = pd.DataFrame({"DTWEXO": [169.09]}, index=idx)
    monkeypatch.setattr(fetchers, "_call_fred", lambda sid, start, end: df)
    monkeypatch.setattr(fetchers, "_pdr_version", lambda: "x")
    recs = fetchers.get_fred_dtwexo()
    assert len(recs) == 1
    assert recs[0]["indicator"] == "DTWEXO"
    assert recs[0]["value"] == pytest.approx(169.09)


def test_get_fred_rtwexbgs_mock(fake_settings, monkeypatch):
    """实际广义美元指数：月度数据。"""
    idx = pd.to_datetime(["2026-06-01", "2026-07-01"])
    df = pd.DataFrame({"RTWEXBGS": [115.20, 115.44]}, index=idx)
    monkeypatch.setattr(fetchers, "_call_fred", lambda sid, start, end: df)
    monkeypatch.setattr(fetchers, "_pdr_version", lambda: "x")
    recs = fetchers.get_fred_rtwexbgs(recent_periods=1)
    assert len(recs) == 1
    r = recs[0]
    assert r["indicator"] == "RTWEXBGS"
    assert r["frequency"] == "M"
    assert r["unit"] == "指数"
    assert r["date"] == "2026-07-01"
    assert r["value"] == pytest.approx(115.44)


def test_fred_dollar_indices_in_us_indicators():
    """4 个美元指数指标应注册在 US_INDICATORS 中，路由到 macro_us_data 表。"""
    for key in ("fred_dtwexbgs", "fred_dtwexm", "fred_dtwexo", "fred_rtwexbgs"):
        assert key in fetchers.US_INDICATORS
        assert key in fetchers.REGISTRY


# --------------------------- 贵金属现货（akshare SGE） --------------------------- #

def test_get_sge_gold_mock(fake_settings, monkeypatch):
    """黄金现货(上海金基准价)：日度，取晚盘价，元/克。"""
    df = pd.DataFrame({
        "交易时间": [pd.Timestamp("2026-08-06"), pd.Timestamp("2026-08-07")],
        "晚盘价": [4281.10, 4267.85],
        "早盘价": [4290.0, 4275.0],
    })
    monkeypatch.setattr(fetchers, "_call_akshare", lambda name, **kw: df)
    monkeypatch.setattr(fetchers, "_akshare_version", lambda: "x")
    recs = fetchers.get_sge_gold(recent_periods=1)
    assert len(recs) == 1
    r = recs[0]
    assert r["indicator"] == "GOLD"
    assert r["country"] == "US"
    assert r["frequency"] == "D"
    assert r["unit"] == "元/克"
    assert r["date"] == "2026-08-07"
    assert r["value"] == pytest.approx(4267.85)  # 取晚盘价
    assert r["source"] == "akshare"
    assert "spot_golden_benchmark_sge" in r["source_url"]


def test_get_sge_silver_mock(fake_settings, monkeypatch):
    """白银现货(上海银基准价)：日度，取晚盘价，元/千克。"""
    df = pd.DataFrame({
        "交易时间": [pd.Timestamp("2026-08-06"), pd.Timestamp("2026-08-07")],
        "晚盘价": [14946.0, 15100.0],
        "早盘价": [14542.0, 15166.0],
    })
    monkeypatch.setattr(fetchers, "_call_akshare", lambda name, **kw: df)
    monkeypatch.setattr(fetchers, "_akshare_version", lambda: "x")
    recs = fetchers.get_sge_silver(recent_periods=1)
    assert len(recs) == 1
    r = recs[0]
    assert r["indicator"] == "SILVER"
    assert r["frequency"] == "D"
    assert r["unit"] == "元/千克"
    assert r["date"] == "2026-08-07"
    assert r["value"] == pytest.approx(15100.0)  # 取晚盘价
    assert r["source"] == "akshare"


def test_sge_metals_in_us_indicators():
    """SGE 黄金/白银应注册在 US_INDICATORS 中，路由到 macro_us_data 表。"""
    for key in ("sge_gold", "sge_silver"):
        assert key in fetchers.US_INDICATORS
        assert key in fetchers.REGISTRY
