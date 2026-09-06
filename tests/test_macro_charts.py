"""macro-charts 边界条件单元测试（覆盖需求 7.1~7.5）。

通过 monkeypatch query._load_records / query._db_has_data 构造场景，
不依赖真实数据库与网络。render 烟雾测试在 plotly 未安装时自动跳过。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

SERVICE_DIR = Path(__file__).resolve().parent.parent / "services" / "macro-charts"
sys.path.insert(0, str(SERVICE_DIR))

import query  # noqa: E402


def _recs(indicator, dates, values, unit="%", freq="M", name="X", country="CN"):
    return [
        {"indicator": indicator, "name": name, "country": country, "frequency": freq,
         "date": d, "value": v, "unit": unit, "source": "akshare",
         "source_url": "x", "fetch_time": "2026-08-08 10:00:00", "extra": "{}"}
        for d, v in zip(dates, values)
    ]


@pytest.fixture
def db_ok(monkeypatch):
    """假定数据库有数据；_load_records 由各用例自定义。"""
    monkeypatch.setattr(query, "_db_has_data", lambda table="macro_data": True)
    return monkeypatch


# --------------------------- 7.1 数据量边界 --------------------------- #

def test_err_db_empty(monkeypatch):
    monkeypatch.setattr(query, "_db_has_data", lambda table="macro_data": False)
    r = query.query_chart("CPI")
    assert r["ok"] is False and r["error_code"] == 1001
    assert "暂无数据" in r["message"]


def test_err_indicator_not_found(db_ok, monkeypatch):
    monkeypatch.setattr(query, "_load_records", lambda ind, c, table="macro_data": [])
    r = query.query_chart("CPI")
    assert r["ok"] is False and r["error_code"] == 1002


def test_single_point(db_ok, monkeypatch):
    monkeypatch.setattr(query, "_load_records",
                        lambda ind, c, table="macro_data": _recs("CPI", ["2026-06-01"], [0.5]))
    r = query.query_chart("CPI")
    assert r["ok"] is True
    s = r["series"][0]
    assert s["single_point"] is True
    assert any("仅1期" in w for w in s["warnings"])


def test_two_records(db_ok, monkeypatch):
    monkeypatch.setattr(query, "_load_records",
                        lambda ind, c, table="macro_data": _recs("CPI", ["2026-05-01", "2026-06-01"], [0.3, 0.5]))
    r = query.query_chart("CPI")
    s = r["series"][0]
    assert s["few_samples"] is True
    assert s["count"] == 2


def test_downsample(db_ok, monkeypatch):
    dates = [d.strftime("%Y-%m-%d") for d in pd.date_range("1976-01-01", periods=600, freq="MS")]
    vals = list(range(600))
    monkeypatch.setattr(query, "_load_records", lambda ind, c, table="macro_data": _recs("CPI", dates, vals))
    r = query.query_chart("CPI")
    s = r["series"][0]
    assert s["downsampled"] is True
    assert s["original_count"] == 600
    assert s["count"] <= 200 and s["count"] >= 190
    assert any("降采样" in w for w in s["warnings"])


def test_no_data_in_range(db_ok, monkeypatch):
    monkeypatch.setattr(query, "_load_records",
                        lambda ind, c, table="macro_data": _recs("CPI", ["2026-05-01", "2026-06-01"], [0.3, 0.5]))
    r = query.query_chart("CPI", start_date="2099-01-01", end_date="2099-12-31")
    s = r["series"][0]
    assert s["count"] == 0
    assert any("所选时间段内无匹配记录" in w for w in s["warnings"])


# --------------------------- 7.2 日期/时间边界 --------------------------- #

def test_err_date_format(db_ok):
    r = query.query_chart("CPI", start_date="2020/01/01")
    assert r["ok"] is False and r["error_code"] == 1003


def test_start_gt_end_swap(db_ok, monkeypatch):
    monkeypatch.setattr(query, "_load_records",
                        lambda ind, c, table="macro_data": _recs("CPI", ["2026-05-01", "2026-06-01"], [0.3, 0.5]))
    r = query.query_chart("CPI", start_date="2026-12-01", end_date="2020-01-01")
    assert r["ok"] is True
    assert r["series"][0]["count"] == 2  # 交换后区间覆盖数据


def test_start_before_earliest_clamped(db_ok, monkeypatch):
    monkeypatch.setattr(query, "_load_records",
                        lambda ind, c, table="macro_data": _recs("CPI", ["2026-05-01", "2026-06-01"], [0.3, 0.5]))
    r = query.query_chart("CPI", start_date="2000-01-01", end_date="2026-12-31")
    assert r["ok"] is True
    assert r["series"][0]["count"] == 2  # 起始被截到最早，结束被截到今天


def test_limit_applies(db_ok, monkeypatch):
    dates = [d.strftime("%Y-%m-%d") for d in pd.date_range("2020-01-01", periods=20, freq="MS")]
    monkeypatch.setattr(query, "_load_records",
                        lambda ind, c, table="macro_data": _recs("CPI", dates, list(range(20))))
    r = query.query_chart("CPI", limit=5)
    assert r["series"][0]["count"] == 5
    assert r["series"][0]["dates"][-1] == dates[-1]


def test_limit_ignored_when_date_range_given(db_ok, monkeypatch):
    dates = [d.strftime("%Y-%m-%d") for d in pd.date_range("2020-01-01", periods=20, freq="MS")]
    monkeypatch.setattr(query, "_load_records",
                        lambda ind, c, table="macro_data": _recs("CPI", dates, list(range(20))))
    r = query.query_chart("CPI", start_date="2020-01-01", end_date="2020-06-01", limit=5)
    assert r["series"][0]["count"] == 6  # 日期范围优先，limit 被忽略


def test_missing_dates_gap(db_ok, monkeypatch):
    # 缺 2026-03
    dates = ["2026-01-01", "2026-02-01", "2026-04-01", "2026-05-01"]
    monkeypatch.setattr(query, "_load_records", lambda ind, c, table="macro_data": _recs("CPI", dates, [1, 2, 4, 5]))
    r = query.query_chart("CPI")
    s = r["series"][0]
    assert len(s["gaps"]) == 1
    assert s["gaps"][0]["start"] == "2026-02-01"
    assert s["gaps"][0]["end"] == "2026-04-01"


# --------------------------- 7.3 数值边界 --------------------------- #

def test_no_fluctuation(db_ok, monkeypatch):
    monkeypatch.setattr(query, "_load_records",
                        lambda ind, c, table="macro_data": _recs("CPI", ["2026-04-01", "2026-05-01", "2026-06-01"],
                                             [1.0, 1.0, 1.0]))
    r = query.query_chart("CPI")
    s = r["series"][0]
    assert s["no_fluctuation"] is True
    assert any("无波动" in w for w in s["warnings"])


def test_all_negative(db_ok, monkeypatch):
    monkeypatch.setattr(query, "_load_records",
                        lambda ind, c, table="macro_data": _recs("CPI", ["2026-04-01", "2026-05-01", "2026-06-01"],
                                             [-1.1, -2.2, -0.5]))
    r = query.query_chart("CPI")
    assert r["series"][0]["all_negative"] is True
    assert r["ok"] is True


def test_precision_rounding(db_ok, monkeypatch):
    monkeypatch.setattr(query, "_load_records",
                        lambda ind, c, table="macro_data": _recs("CPI", ["2026-05-01", "2026-06-01"],
                                             [0.123456, 0.987654]))
    r = query.query_chart("CPI")
    s = r["series"][0]
    assert s["values_display"] == [0.12, 0.99]
    assert s["values"][0] == pytest.approx(0.123456)  # 原始精度保留


# --------------------------- 7.4 多指标对比 --------------------------- #

def test_multi_indicator_compare(db_ok, monkeypatch):
    def loader(ind, c, table="macro_data"):
        if ind == "CPI":
            return _recs("CPI", ["2026-05-01", "2026-06-01"], [0.3, 0.5])
        if ind == "PPI":
            return _recs("PPI", ["2026-05-01", "2026-06-01"], [4.1, 4.3])
        return []
    monkeypatch.setattr(query, "_load_records", loader)
    r = query.query_chart("CPI,PPI")
    assert r["ok"] is True
    assert r["compare"] is True
    assert len(r["series"]) == 2


def test_too_many_indicators(db_ok, monkeypatch):
    monkeypatch.setattr(query, "_load_records", lambda ind, c, table="macro_data": _recs(ind, ["2026-06-01"], [1.0]))
    r = query.query_chart("CPI,PPI,GDP,M2")
    assert r["ok"] is False
    assert "建议分开展示" in r["message"]


def test_frequency_change_detected(db_ok, monkeypatch):
    recs = _recs("GDP", ["2026-01-01", "2026-04-01"], [5.0, 5.1], freq="Q")
    recs[1]["frequency"] = "M"  # 频率变更
    monkeypatch.setattr(query, "_load_records", lambda ind, c, table="macro_data": recs)
    r = query.query_chart("GDP")
    assert len(r["series"][0]["frequency_changes"]) == 1


# --------------------------- 7.5 修订 --------------------------- #

def test_revision_status_parsed(db_ok, monkeypatch):
    recs = _recs("CPI", ["2026-05-01", "2026-06-01"], [0.3, 0.5])
    recs[0]["extra"] = '{"revision_status": "preliminary"}'
    recs[1]["extra"] = '{"revision_status": "revised"}'
    monkeypatch.setattr(query, "_load_records", lambda ind, c, table="macro_data": recs)
    r = query.query_chart("CPI")
    s = r["series"][0]
    assert s["revision_status"] == ["preliminary", "revised"]
    assert s["revised"] is True


# --------------------------- render 烟雾测试 --------------------------- #

def test_render_html_smoke(db_ok, monkeypatch):
    pytest.importorskip("plotly")
    import render  # noqa: E402  (plotly 已装才导入)
    monkeypatch.setattr(query, "_load_records",
                        lambda ind, c, table="macro_data": _recs("CPI", ["2026-05-01", "2026-06-01"], [0.3, 0.5]))
    r = query.query_chart("CPI")
    html = render.render_html(r)
    assert "<html" in html.lower() or "plotly" in html.lower()


def test_render_html_single_point(db_ok, monkeypatch):
    """单点数据渲染不应崩溃（回归测试：yref='domain' 曾导致 ValueError）。"""
    pytest.importorskip("plotly")
    import render  # noqa: E402
    monkeypatch.setattr(query, "_load_records",
                        lambda ind, c, table="macro_data": _recs("GOLD", ["2026-08-08"], [923.43]))
    r = query.query_chart("GOLD")
    assert r["series"][0]["single_point"] is True
    html = render.render_html(r)
    assert "<html" in html.lower() or "plotly" in html.lower()


def test_render_error_html():
    pytest.importorskip("plotly")
    import render  # noqa: E402
    html = render.render_html({"ok": False, "message": "暂无数据", "warnings": [], "series": []})
    assert "暂无数据" in html


# --------------------------- macro_us_data 表（美国及商品） --------------------------- #

def test_us_table_routes_loader(db_ok, monkeypatch):
    """table=macro_us_data 时 _load_records 收到正确的 table 参数。"""
    seen = {}

    def loader(ind, c, table="macro_data"):
        seen["table"] = table
        seen["country"] = c
        return _recs("UNRATE", ["2026-06-01", "2026-07-01"], [4.2, 4.1],
                     unit="%", name="失业率", country="US")

    monkeypatch.setattr(query, "_load_records", loader)
    r = query.query_chart("UNRATE", country="US", table="macro_us_data")
    assert r["ok"] is True
    assert seen["table"] == "macro_us_data"
    assert seen["country"] == "US"
    assert r["series"][0]["indicator"] == "UNRATE"
    assert "[US]" in r["title"]


def test_us_table_db_empty(monkeypatch):
    """macro_us_data 表为空时返回 1001。"""
    monkeypatch.setattr(query, "_db_has_data", lambda table="macro_data": False)
    r = query.query_chart("UNRATE", country="US", table="macro_us_data")
    assert r["ok"] is False and r["error_code"] == 1001


def test_us_table_indicator_not_found(db_ok, monkeypatch):
    monkeypatch.setattr(query, "_load_records", lambda ind, c, table="macro_data": [])
    r = query.query_chart("FOO", country="US", table="macro_us_data")
    assert r["ok"] is False and r["error_code"] == 1002


def test_us_table_multi_compare(db_ok, monkeypatch):
    """美国指标多指标对比（双Y轴）。"""
    def loader(ind, c, table="macro_data"):
        if ind == "CPIAUCSL":
            return _recs("CPIAUCSL", ["2026-05-01", "2026-06-01"], [330.0, 332.5],
                         unit="指数", name="消费者价格指数", country="US")
        if ind == "UNRATE":
            return _recs("UNRATE", ["2026-05-01", "2026-06-01"], [4.2, 4.1],
                         unit="%", name="失业率", country="US")
        return []
    monkeypatch.setattr(query, "_load_records", loader)
    r = query.query_chart("CPIAUCSL,UNRATE", country="US", table="macro_us_data")
    assert r["ok"] is True
    assert r["compare"] is True
    assert len(r["series"]) == 2


def test_us_table_render_html(db_ok, monkeypatch):
    pytest.importorskip("plotly")
    import render  # noqa: E402
    monkeypatch.setattr(query, "_load_records",
                        lambda ind, c, table="macro_data": _recs(
                            "UNRATE", ["2026-06-01", "2026-07-01"], [4.2, 4.1],
                            unit="%", name="失业率", country="US"))
    r = query.query_chart("UNRATE", country="US", table="macro_us_data")
    html = render.render_html(r)
    assert "失业率" in html or "UNRATE" in html


def test_default_table_is_macro_data(db_ok, monkeypatch):
    """不传 table 时默认读 macro_data（向后兼容）。"""
    seen = {}
    monkeypatch.setattr(query, "_load_records",
                        lambda ind, c, table="macro_data": (seen.__setitem__("t", table) or _recs(
                            "CPI", ["2026-06-01"], [0.5])))
    r = query.query_chart("CPI")
    assert r["ok"] is True
    assert seen["t"] == "macro_data"


# --------------------------- SGE 贵金属现货（macro_us_data 表，日频 D） --------------------------- #

def test_sge_gold_daily_single_point(db_ok, monkeypatch):
    """GOLD 上海金基准价：日频(D)单点渲染，验证频率与单位正确传递。"""
    monkeypatch.setattr(query, "_load_records",
                        lambda ind, c, table="macro_data": _recs(
                            "GOLD", ["2026-08-05"], [923.43],
                            unit="元/克", freq="D", name="黄金现货(上海金基准价)", country="US"))
    r = query.query_chart("GOLD", country="US", table="macro_us_data")
    assert r["ok"] is True
    s = r["series"][0]
    assert s["indicator"] == "GOLD"
    assert s["frequency"] == "D"
    assert s["unit"] == "元/克"
    assert s["single_point"] is True
    assert any("仅1期" in w for w in s["warnings"])
    assert "[US]" in r["title"]


def test_sge_gold_silver_compare_dual_axis(db_ok, monkeypatch):
    """GOLD,SILVER 双轴对比：日频(D)，不同单位(元/克 vs 元/千克)。
    README 核心示例命令，验证两个 SGE 贵金属指标能同图对比。"""
    def loader(ind, c, table="macro_data"):
        if ind == "GOLD":
            return _recs("GOLD", ["2026-08-04", "2026-08-05"], [920.0, 923.43],
                         unit="元/克", freq="D", name="黄金现货(上海金基准价)", country="US")
        if ind == "SILVER":
            return _recs("SILVER", ["2026-08-04", "2026-08-05"], [14946.0, 15100.0],
                         unit="元/千克", freq="D", name="白银现货(上海银基准价)", country="US")
        return []
    monkeypatch.setattr(query, "_load_records", loader)
    r = query.query_chart("GOLD,SILVER", country="US", table="macro_us_data")
    assert r["ok"] is True
    assert r["compare"] is True
    assert len(r["series"]) == 2
    # 两指标单位不同，应保留各自单位（渲染层据此生成双 Y 轴）
    units = {s["unit"] for s in r["series"]}
    assert units == {"元/克", "元/千克"}
    inds = {s["indicator"] for s in r["series"]}
    assert inds == {"GOLD", "SILVER"}


def test_sge_metals_render_html_dual_axis(db_ok, monkeypatch):
    """GOLD,SILVER 双轴对比 HTML 渲染烟雾测试（含 yaxis2）。"""
    pytest.importorskip("plotly")
    import render  # noqa: E402
    def loader(ind, c, table="macro_data"):
        if ind == "GOLD":
            return _recs("GOLD", ["2026-08-04", "2026-08-05"], [920.0, 923.43],
                         unit="元/克", freq="D", name="黄金现货(上海金基准价)", country="US")
        if ind == "SILVER":
            return _recs("SILVER", ["2026-08-04", "2026-08-05"], [14946.0, 15100.0],
                         unit="元/千克", freq="D", name="白银现货(上海银基准价)", country="US")
        return []
    monkeypatch.setattr(query, "_load_records", loader)
    r = query.query_chart("GOLD,SILVER", country="US", table="macro_us_data")
    html = render.render_html(r)
    assert "plotly" in html.lower()
    assert "黄金现货" in html
    assert "白银现货" in html
    # 双 Y 轴：HTML 应包含 yaxis2 配置
    assert "yaxis2" in html


def test_sge_gold_daily_gap_detection(db_ok, monkeypatch):
    """GOLD 日频(D)数据跳过周末应被检测为缺口（08-05 周三 → 08-10 周一）。"""
    # 2026-08-05(周三), 08-06(周四), 08-10(周一)：08-07~08-09 周末缺失
    recs = _recs("GOLD", ["2026-08-05", "2026-08-06", "2026-08-10"],
                 [923.43, 925.0, 928.0],
                 unit="元/克", freq="D", name="黄金现货(上海金基准价)", country="US")
    monkeypatch.setattr(query, "_load_records", lambda ind, c, table="macro_data": recs)
    r = query.query_chart("GOLD", country="US", table="macro_us_data")
    assert r["ok"] is True
    gaps = r["series"][0]["gaps"]
    assert len(gaps) >= 1
    # 缺口前后有效点应为 08-06 和 08-10
    assert gaps[0]["start"] == "2026-08-06"
    assert gaps[0]["end"] == "2026-08-10"


# --------------------------- 三 Y 轴（3 指标单位各异） --------------------------- #

def test_assign_yaxes_three_distinct_units():
    """3 个指标单位各不相同 → 分配 y1/y2/y3 三轴。"""
    import render  # noqa: E402  (_assign_yaxes 不依赖 plotly)
    series = [
        {"unit": "%"},
        {"unit": "元/克"},
        {"unit": "指数"},
    ]
    assert render._assign_yaxes(series) == ["y1", "y2", "y3"]


def test_assign_yaxes_same_unit_shared():
    """同单位共享轴：CPI(%)、PPI(%) 同走 y1，GDP(十亿美元) 走 y2，无 y3。"""
    import render  # noqa: E402
    series = [
        {"unit": "%"},        # CPI → y1
        {"unit": "%"},        # PPI → y1（复用）
        {"unit": "十亿美元"},  # GDP → y2
    ]
    assert render._assign_yaxes(series) == ["y1", "y1", "y2"]


def test_three_axis_render_html(db_ok, monkeypatch):
    """DGS10,GOLD,DTWEXBGS 三指标（单位各异）HTML 渲染含 yaxis3 且 anchor=free。"""
    pytest.importorskip("plotly")
    import render  # noqa: E402

    def loader(ind, c, table="macro_data"):
        if ind == "DGS10":
            return _recs("DGS10", ["2026-07-01", "2026-08-01"], [4.3, 4.5],
                         unit="%", freq="D", name="10年期国债收益率", country="US")
        if ind == "GOLD":
            return _recs("GOLD", ["2026-07-01", "2026-08-01"], [900.0, 923.0],
                         unit="元/克", freq="D", name="黄金现货", country="US")
        if ind == "DTWEXBGS":
            return _recs("DTWEXBGS", ["2026-07-01", "2026-08-01"], [120.0, 119.7],
                         unit="指数", freq="D", name="名义广义美元指数", country="US")
        return []
    monkeypatch.setattr(query, "_load_records", loader)
    r = query.query_chart("DGS10,GOLD,DTWEXBGS", country="US", table="macro_us_data")
    assert r["ok"] is True
    assert len(r["series"]) == 3
    html = render.render_html(r)
    # 三轴：HTML 应包含 yaxis2 与 yaxis3 配置
    assert "yaxis2" in html
    assert "yaxis3" in html
    # 第三轴脱离绘图区域锚定（anchor=free，Plotly 紧凑 JSON 无空格）
    assert '"anchor":"free"' in html
    # 三个指标名均出现在图例
    assert "10年期国债收益率" in html
    assert "黄金现货" in html
    assert "名义广义美元指数" in html


def test_three_axis_no_yaxis3_when_units_repeat(db_ok, monkeypatch):
    """3 指标但仅 2 种单位（DGS10/DGS1 同为 %，GOLD 元/克）→ 只双轴，无 yaxis3。"""
    pytest.importorskip("plotly")
    import render  # noqa: E402

    def loader(ind, c, table="macro_data"):
        if ind == "DGS10":
            return _recs("DGS10", ["2026-07-01", "2026-08-01"], [4.3, 4.5],
                         unit="%", freq="D", name="10年期国债收益率", country="US")
        if ind == "DGS1":
            return _recs("DGS1", ["2026-07-01", "2026-08-01"], [4.8, 4.6],
                         unit="%", freq="D", name="1年期国债收益率", country="US")
        if ind == "GOLD":
            return _recs("GOLD", ["2026-07-01", "2026-08-01"], [900.0, 923.0],
                         unit="元/克", freq="D", name="黄金现货", country="US")
        return []
    monkeypatch.setattr(query, "_load_records", loader)
    r = query.query_chart("DGS10,DGS1,GOLD", country="US", table="macro_us_data")
    assert r["ok"] is True
    html = render.render_html(r)
    # 同单位共享轴：DGS10/DGS1 均走 y1，GOLD 走 y2 → 有 yaxis2，无 yaxis3
    assert "yaxis2" in html
    assert "yaxis3" not in html


# --------------------------- 多组合并 HTML（render_html_multi） --------------------------- #

def _multi_results(monkeypatch):
    """构造 2 组结果：CPI(单线) + GOLD,SILVER(双轴对比)，均走 macro_us_data 表。"""
    monkeypatch.setattr(query, "_db_has_data", lambda table="macro_data": True)

    def loader(ind, c, table="macro_data"):
        if ind == "CPI":
            return _recs("CPI", ["2026-05-01", "2026-06-01"], [0.3, 0.5],
                         name="居民消费价格指数", country="US")
        if ind == "GOLD":
            return _recs("GOLD", ["2026-08-04", "2026-08-05"], [920.0, 923.43],
                         unit="元/克", freq="D", name="黄金现货", country="US")
        if ind == "SILVER":
            return _recs("SILVER", ["2026-08-04", "2026-08-05"], [14946.0, 15100.0],
                         unit="元/千克", freq="D", name="白银现货", country="US")
        return []

    monkeypatch.setattr(query, "_load_records", loader)
    r1 = query.query_chart("CPI", country="US", table="macro_us_data")
    r2 = query.query_chart("GOLD,SILVER", country="US", table="macro_us_data")
    return [r1, r2]


def test_render_html_multi_smoke(monkeypatch):
    """2 组结果 → 2 个 <section>、2 个图表 div、单个 <head>、plotly.js 只内联一次。"""
    pytest.importorskip("plotly")
    import plotly.offline as pyo
    import render  # noqa: E402
    results = _multi_results(monkeypatch)
    assert all(r["ok"] for r in results)

    html = render.render_html_multi(results)
    assert "<!DOCTYPE html>" in html
    # 每组一个 <section>
    assert html.count("<section>") == 2
    # 每个 ok 组产生一个 plotly 图表 div（plotly 库源码本身不含该字符串，计数=组数）
    assert html.count("plotly-graph-div") == 2
    # plotly.js 只在 <head> 内联一次：整个页面仅一个 <head>，且完整 plotly 源码只出现一次
    assert html.count("<head>") == 1 and html.count("</head>") == 1
    assert html.count(pyo.get_plotlyjs()) == 1
    # 标题含组数统计
    assert "2/2 组" in html
    # 两组指标名均出现
    assert "黄金现货" in html and "白银现货" in html


def test_render_html_multi_preserves_dual_axis(monkeypatch):
    """合并 HTML 中第二组 GOLD,SILVER 仍保留双 Y 轴（yaxis2）。"""
    pytest.importorskip("plotly")
    import render  # noqa: E402
    results = _multi_results(monkeypatch)
    html = render.render_html_multi(results)
    assert "yaxis2" in html  # GOLD,SILVER 单位不同 → 双轴


def test_render_html_multi_with_error_group(monkeypatch):
    """含失败组：失败组渲染为提示块，成功组仍出图，标题统计 1/2。"""
    pytest.importorskip("plotly")
    import render  # noqa: E402
    ok_result = _multi_results(monkeypatch)[0]
    err_result = {"ok": False, "error_code": 1002,
                  "message": "该指标未收录，请检查名称是否正确：FOO",
                  "warnings": [], "series": []}
    html = render.render_html_multi([ok_result, err_result])
    assert html.count("<section>") == 2
    # 仅 1 个 ok → 1 个图表 div
    assert html.count("plotly-graph-div") == 1
    assert "1/2 组" in html
    assert "该指标未收录" in html  # 错误提示出现在页面中


def test_render_html_multi_empty():
    """空列表不应崩溃，输出 0/0 组的空页面。"""
    pytest.importorskip("plotly")
    import render  # noqa: E402
    html = render.render_html_multi([])
    assert "<!DOCTYPE html>" in html
    assert "0/0 组" in html
    assert "<section>" not in html


def test_render_html_single_backward_compat(monkeypatch):
    """单组 render_html 仍是自包含 HTML（full_html + 内联 plotly.js），向后兼容。"""
    pytest.importorskip("plotly")
    import render  # noqa: E402
    monkeypatch.setattr(query, "_db_has_data", lambda table="macro_data": True)
    monkeypatch.setattr(query, "_load_records",
                        lambda ind, c, table="macro_data": _recs("CPI", ["2026-05-01", "2026-06-01"], [0.3, 0.5]))
    r = query.query_chart("CPI")
    html = render.render_html(r)
    # 自包含完整 HTML 页面（plotly to_html full_html=True）
    assert "<html" in html.lower()
    assert "plotly" in html.lower()


def test_default_html_name():
    """默认 HTML 文件名：单组用 _ 连指标，多组用 __ 连组。"""
    import importlib.util
    # 按绝对路径加载 main.py，避免与 macro-data-service 的 main.py 在 sys.path 上的影子冲突
    spec = importlib.util.spec_from_file_location("_macro_charts_main", SERVICE_DIR / "main.py")
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    assert main._default_html_name(["CPI"]) == "CPI.html"
    assert main._default_html_name(["CPI,PPI"]) == "CPI_PPI.html"
    assert main._default_html_name(["CPI,PPI", "GOLD,SILVER"]) == "CPI_PPI__GOLD_SILVER.html"
    assert main._default_html_name(["CPI,PPI", "GOLD,SILVER", "DGS10"]) == "CPI_PPI__GOLD_SILVER__DGS10.html"
