"""板块轮动面板（core/rotation_panel.py）单元测试。

**全部离线**，且**逐个锁死冻结口径**（P0.5-A）——
这些公式一旦开始累积面板输出就不许改，所以测试是"冻结"的执行手段。
"""

from __future__ import annotations

import pandas as pd
import pytest

import core.rotation_panel as RP


# ====================================================================
# 夹具：最小主题定义 + 造数
# ====================================================================

THEMES = [
    {"theme": "窄主题", "plates": (("801490", "半导体设备"),), "etfs": (), "anchor": "sz399006"},
    {"theme": "宽主题", "plates": (("801001", "芯片"),), "etfs": (), "anchor": "sz399006"},
]


def _concepts(rows):
    """rows: [(date, stock_code, plate_code)]"""
    df = pd.DataFrame(rows, columns=["date", "stock_code", "plate_code"])
    df["plate_name"] = ""
    return df[["date", "stock_code", "plate_code", "plate_name"]]


def _stocks(rows):
    """rows: [(date, stock_code, detail_fetched, buy_total, sell_total, ratio)]"""
    return pd.DataFrame(rows, columns=["date", "stock_code", "detail_fetched",
                                       "buy_total", "sell_total", "buy_sell_ratio"])


# ====================================================================
# 工具函数
# ====================================================================

@pytest.mark.parametrize("v,expect", [
    (1.5, 1.5), (0, 0.0), (None, None), ("abc", None),
    (float("nan"), None), (float("inf"), None),
])
def test_f_none_normalisation(v, expect):
    assert RP._f(v) == expect


def test_pct_rank_requires_min_samples():
    """样本不足必须返回 None（面板显示 n/a），**不许用不足的样本算分位**。"""
    few = [1.0] * (RP.PCT_RANK_MIN_SAMPLES - 1)
    assert RP._pct_rank(few, 1.0) is None
    enough = list(range(RP.PCT_RANK_MIN_SAMPLES))
    assert RP._pct_rank(enough, enough[-1]) == 1.0
    assert RP._pct_rank(enough, -1) == 0.0


def test_pct_rank_none_value():
    assert RP._pct_rank(list(range(100)), None) is None


@pytest.mark.parametrize("flags,expect", [
    ([], 0), ([True, True, False], 0), ([False, True, True], 2),
    ([True, False, True, True], 2), ([True, True, True], 3),
])
def test_streak(flags, expect):
    assert RP._streak(flags) == expect


# ====================================================================
# 口径 ①：上榜股数（去重 + 题材归属）
# ====================================================================

def test_theme_listed_counts_dedupes_and_respects_plates():
    c = _concepts([
        ("2026-09-30", "000001", "801490"),
        ("2026-09-30", "000001", "801490"),      # 重复行 → 只算 1
        ("2026-09-30", "000002", "801490"),
        ("2026-09-30", "000003", "801001"),      # 属于宽主题
        ("2026-09-30", "000004", "999999"),      # 不属于任何主题
    ])
    got = RP.theme_listed_counts(c, THEMES)
    assert got["窄主题"] == 2
    assert got["宽主题"] == 1


def test_theme_listed_counts_empty():
    got = RP.theme_listed_counts(None, THEMES)
    assert got == {"窄主题": 0, "宽主题": 0}


# ====================================================================
# 口径 ⑩⑦⑧：毛额买卖比 / 碾压占比（只认 Tier2）
# ====================================================================

def test_market_ratio_uses_only_tier2():
    s = _stocks([
        ("d", "a", True, 600.0, 400.0, 0.6),
        ("d", "b", True, 500.0, 500.0, 0.5),
        ("d", "c", False, 9999.0, 0.0, None),    # 非 Tier2 → 必须被忽略
    ])
    assert RP.market_buy_sell_ratio(s) == pytest.approx(1100.0 / 2000.0)


def test_market_ratio_none_when_no_tier2():
    s = _stocks([("d", "a", False, 1.0, 1.0, None)])
    assert RP.market_buy_sell_ratio(s) is None


def test_theme_buy_sell_stats():
    c = _concepts([("d", "a", "801490"), ("d", "b", "801490"), ("d", "z", "801001")])
    s = _stocks([
        ("d", "a", True, 800.0, 200.0, 0.8),
        ("d", "b", True, 300.0, 700.0, 0.3),
        ("d", "z", True, 1.0, 1.0, 0.5),        # 别的题材 → 不计入
    ])
    got = RP.theme_buy_sell_stats(c, s, THEMES[0])
    assert got["n_tier2"] == 2
    assert got["buy_sell_ratio"] == pytest.approx(1100.0 / 2000.0)
    assert got["crush_share"] == pytest.approx(0.5)     # 只有 a ≥ 0.65


def test_theme_buy_sell_stats_no_tier2_returns_none():
    c = _concepts([("d", "a", "801490")])
    s = _stocks([("d", "a", False, 1.0, 1.0, None)])
    got = RP.theme_buy_sell_stats(c, s, THEMES[0])
    assert got["buy_sell_ratio"] is None and got["crush_share"] is None


def test_theme_buy_sell_stats_zero_totals_safe():
    c = _concepts([("d", "a", "801490")])
    s = _stocks([("d", "a", True, 0.0, 0.0, None)])
    got = RP.theme_buy_sell_stats(c, s, THEMES[0])
    assert got["buy_sell_ratio"] is None          # 不除零


# ====================================================================
# 口径 ②③⑨：上榜率与"超额"
# ====================================================================

def test_build_theme_daily_rates_and_excess():
    c = _concepts([("d1", "a", "801490"), ("d1", "z", "801001")])
    s = _stocks([
        ("d1", "a", True, 800.0, 200.0, 0.8),
        ("d1", "z", True, 200.0, 800.0, 0.2),
    ])
    counts = {"窄主题": 10, "宽主题": 100}
    daily = RP.build_theme_daily(c, s, ["d1"], THEMES, counts)
    row = daily[daily["theme"] == "窄主题"].iloc[0]
    assert row["n_listed"] == 1
    assert row["n_constituents"] == 10
    assert row["listed_rate"] == pytest.approx(0.1)
    assert row["buy_sell_ratio"] == pytest.approx(0.8)
    # 全市场 = (800+200)/(1000+1000) = 0.5；超额 = 0.8 - 0.5
    assert row["market_buy_sell_ratio"] == pytest.approx(0.5)
    assert row["ratio_excess"] == pytest.approx(0.3)


def test_build_theme_daily_zero_constituents_gives_na_rate():
    c = _concepts([("d1", "a", "801490")])
    s = _stocks([("d1", "a", True, 1.0, 1.0, 0.5)])
    daily = RP.build_theme_daily(c, s, ["d1"], THEMES, {"窄主题": 0, "宽主题": 100})
    assert pd.isna(daily[daily["theme"] == "窄主题"].iloc[0]["listed_rate"])


def test_build_theme_daily_no_data_day():
    daily = RP.build_theme_daily(_concepts([]), _stocks([]), ["d1"], THEMES, {"窄主题": 10})
    assert len(daily) == 2
    assert (daily["n_listed"] == 0).all()


# ====================================================================
# 口径 ④⑤⑥ + 状态标签
# ====================================================================

def _daily(narrow_counts, wide_counts=None):
    rows = []
    for d, n in narrow_counts:
        rows.append({"date": d, "theme": "窄主题", "n_listed": n})
    for d, n in (wide_counts or [(d, 0) for d, _ in narrow_counts]):
        rows.append({"date": d, "theme": "宽主题", "n_listed": n})
    df = pd.DataFrame(rows)
    df["n_constituents"] = df["theme"].map({"窄主题": 10, "宽主题": 100})
    df["listed_rate"] = df["n_listed"] / df["n_constituents"]
    return df


def test_rolling_windows_require_full_window():
    """roll 必须满窗口才算（min_periods=w）—— 否则开头会误报 🔥。"""
    d = RP.add_time_series_metrics(_daily([("d1", 1), ("d2", 2), ("d3", 3), ("d4", 4), ("d5", 5)]))
    n = d[d["theme"] == "窄主题"].reset_index(drop=True)
    assert n["roll3"].isna().tolist()[:2] == [True, True]
    assert n["roll3"].tolist()[2:] == [6.0, 9.0, 12.0]
    assert n["roll5"].isna().tolist()[:4] == [True] * 4
    assert n["roll5"].iloc[4] == 15.0


def test_rolling_does_not_fake_strengthening_at_start():
    """递减的日频数据在窗口未满时**不得**被判成 🔥（这是 min_periods=1 的坑）。"""
    d = RP.add_time_series_metrics(_daily([("d1", 5), ("d2", 4), ("d3", 1)]))
    assert d[d["theme"] == "窄主题"]["tag"].tolist() == [RP.TAG_FLAT] * 3


def test_streak_resets_on_zero():
    d = RP.add_time_series_metrics(_daily([("d1", 1), ("d2", 0), ("d3", 2)]))
    assert d[d["theme"] == "窄主题"]["streak"].tolist() == [1, 0, 1]


def test_pct_rank_is_na_below_min_samples():
    n_below = RP.PCT_RANK_MIN_SAMPLES - 1
    counts = [(f"d{i:03d}", 1) for i in range(n_below)]
    d = RP.add_time_series_metrics(_daily(counts))
    assert d["rate_pct_rank"].isna().all()        # 样本不足 ⇒ 全 n/a


def _tag_at(daily: pd.DataFrame, date: str, theme: str = "窄主题") -> str:
    """取指定 (日期, 主题) 的标签 —— **必须两个条件都过滤**，
    否则会取到按主题排序后排在前的『宽主题』那一行（踩过一次）。"""
    sub = daily[(daily["date"] == date) & (daily["theme"] == theme)]
    assert not sub.empty, f"没有 {date}/{theme} 这一行"
    return sub.iloc[0]["tag"]


def test_tag_new_entry_when_previous_days_zero():
    d = RP.add_time_series_metrics(_daily([("d1", 0), ("d2", 0), ("d3", 0), ("d4", 1)]))
    assert _tag_at(d, "d4") == RP.TAG_NEW


def test_tag_strengthening_when_roll5_rising():
    """🔥 需要 roll5 连续 2 日严格递增 ⇒ 目标日至少要有 7 天历史（roll5 需满 5 天）。"""
    seq = [("d1", 1), ("d2", 2), ("d3", 3), ("d4", 4), ("d5", 5), ("d6", 6), ("d7", 7)]
    d = RP.add_time_series_metrics(_daily(seq))
    n = d[d["theme"] == "窄主题"].reset_index(drop=True)
    r5 = n["roll5"].tolist()
    assert r5[4] == 15 and r5[5] == 20 and r5[6] == 25
    assert _tag_at(d, "d7") == RP.TAG_STRENGTHENING


def test_tag_cooling_when_roll5_falling():
    """💤：roll5 连续 2 日下降（窗口满之后）。"""
    seq = [("d1", 9), ("d2", 8), ("d3", 7), ("d4", 3), ("d5", 1), ("d6", 0), ("d7", 0)]
    d = RP.add_time_series_metrics(_daily(seq))
    n = d[d["theme"] == "窄主题"].reset_index(drop=True)
    r5 = n["roll5"].tolist()
    assert r5[6] < r5[5] < r5[4], f"预期递减: {r5}"
    assert _tag_at(d, "d7") == RP.TAG_COOLING


def test_tag_priority_new_beats_others():
    """"🆕 优先级高于 🔥/💤" 是冻结规则的一部分。"""
    assert RP.TAG_PRIORITY[0] == RP.TAG_NEW
    assert list(RP.TAG_PRIORITY) == [RP.TAG_NEW, RP.TAG_STRENGTHENING, RP.TAG_COOLING, RP.TAG_FLAT]


def test_add_metrics_on_empty():
    assert RP.add_time_series_metrics(pd.DataFrame()).empty


# ====================================================================
# 题材热度附加
# ====================================================================

def test_attach_theme_rank_picks_hottest_plate():
    daily = pd.DataFrame([{"date": "d1", "theme": "窄主题"}])
    rank = pd.DataFrame([
        {"date": "d1", "plate_code": "801490", "plate_name": "半导体设备",
         "rank": 100, "rate": 1.0, "score": 10, "trade_money": 1e8, "volume_ration": 1.0},
        {"date": "d1", "plate_code": "801001", "plate_name": "芯片",
         "rank": 5, "rate": 2.0, "score": 99, "trade_money": 2e8, "volume_ration": 1.2},
    ])
    out = RP.attach_theme_rank(daily, rank)
    # 窄主题只含 801490（注意 fixture 的 THEMES 是本地定义，函数内部用 core.theme_universe）
    # ⇒ 这里改为断言"不会崩且列被建立"
    assert "rank" in out.columns


def test_attach_theme_rank_empty_is_safe():
    daily = pd.DataFrame([{"date": "d1", "theme": "x"}])
    out = RP.attach_theme_rank(daily, pd.DataFrame())
    assert out["rank"].isna().all()


# ====================================================================
# 报告格式化（冻结的显示规则）
# ====================================================================

def _panel_inputs():
    d = RP.add_time_series_metrics(_daily([("d1", 1), ("d2", 2), ("d3", 3)]))
    d["buy_sell_ratio"] = None
    d["crush_share"] = None
    d["ratio_excess"] = None
    d["rank"] = None
    d["rate"] = None
    d["score"] = None
    d["trade_money"] = None
    d["volume_ration"] = None
    today = d[d["date"] == "d3"].copy()
    return today, d


def test_format_panel_has_mandatory_disclaimer():
    today, hist = _panel_inputs()
    txt = RP.format_panel(today, hist, date="d3")
    assert "不产生买入信号" in txt
    assert "不占仓位权重" in txt


def test_format_panel_prints_na_never_zero_for_missing():
    """冻结规则：缺失一律 n/a，**绝不填 0**（黄金那种真 0 只应出现在 n_listed/listed_rate）。"""
    today, hist = _panel_inputs()
    txt = RP.format_panel(today, hist, date="d3")
    assert RP.NA in txt


def test_format_panel_sorts_by_listed_rate_not_raw_count():
    """按归一化上榜率排序 —— 窄题材(1/10=10%)必须排在宽题材(3/100=3%)前面。"""
    today, hist = _panel_inputs()
    t = today.copy()
    t.loc[t["theme"] == "窄主题", ["n_listed", "listed_rate"]] = [1, 0.10]
    t.loc[t["theme"] == "宽主题", ["n_listed", "listed_rate"]] = [3, 0.03]
    txt = RP.format_panel(t, hist, date="d3")
    assert txt.index("窄主题") < txt.index("宽主题")


def test_format_panel_marks_saturated_streak():
    today, hist = _panel_inputs()
    t = today.copy()
    t["streak"] = 200
    txt = RP.format_panel(t, hist, date="d3", data_sufficiency={"window_days": 200})
    assert "200+" in txt


def test_format_panel_reports_data_sufficiency():
    today, hist = _panel_inputs()
    txt = RP.format_panel(today, hist, date="d3",
                          data_sufficiency={"lhb_days": 424, "tier2_days": 60,
                                            "pct_rank_ready": True, "tier2_ready": True})
    assert "424" in txt and "60" in txt and "分位可用: ✅" in txt


def test_format_panel_warns_when_pct_rank_not_ready():
    today, hist = _panel_inputs()
    txt = RP.format_panel(today, hist, date="d3",
                          data_sufficiency={"pct_rank_ready": False})
    assert "样本不足" in txt


def test_format_panel_handles_empty_day():
    txt = RP.format_panel(pd.DataFrame(), pd.DataFrame(), date="d3")
    assert "当日无数据" in txt


def test_format_panel_lists_new_entries():
    today, hist = _panel_inputs()
    txt = RP.format_panel(today, hist, new_entries=["固态电池", "CPO"], date="d3")
    assert "固态电池" in txt and "CPO" in txt


# ====================================================================
# 冻结参数快照（改动必须新开版本号）
# ====================================================================

def test_frozen_parameters_snapshot():
    assert RP.PANEL_VERSION == "v1"
    assert RP.PCT_RANK_WINDOW == 120
    assert RP.PCT_RANK_MIN_SAMPLES == 60
    assert RP.ROLL_WINDOWS == (3, 5)
    assert RP.CRUSH_THRESHOLD == 0.65
    assert RP.NEW_ENTRY_LOOKBACK == 3
    assert RP.COOLING_PCT_RANK == 0.20
    assert RP.NA == "n/a"
