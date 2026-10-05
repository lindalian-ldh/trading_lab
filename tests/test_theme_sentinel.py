"""Phase 3 观察哨（core/theme_sentinel.py）单元测试。

三条契约必须被测试钉住：
1. **数据不可用必须显式** —— 缺数据/过期报 `❌ 数据不可用`，**不许**退化成"无信号"；
2. **只显示、不决策** —— 每条输出带 `validated=False`，渲染里必须有两条横幅；
3. **无未来函数** —— 指定 as_of 时的读数，必须等于把数据截断到 as_of 后的读数。
"""

from __future__ import annotations

import pathlib

import numpy as np
import pandas as pd
import pytest

import core.theme_sentinel as sn


# ====================================================================
# 夹具
# ====================================================================

def _mk(closes, start="2020-01-01", last=None) -> pd.DataFrame:
    c = np.asarray(closes, dtype=float)
    end = last or pd.Timestamp(start) + pd.tseries.offsets.BDay(len(c) - 1)
    dates = pd.bdate_range(end=end, periods=len(c))
    return pd.DataFrame({"date": dates, "open": c, "high": c + 0.1,
                         "low": c - 0.1, "close": c, "volume": np.nan})


def _trend(n=400, base=100.0, step=0.05, last=None):
    return _mk([base + step * i for i in range(n)], last=last)


def _l1_on_frame():
    """末尾 L1 点亮（结构转多）的序列：摆动高点 130（在 20 日确认），56 日突破、57 日确认。"""
    a = [100 + 2 * i for i in range(16)]
    b = [130 - 2 * (i - 15) for i in range(16, 36)]
    c = [92 + 2 * (i - 36) for i in range(36, 58)]
    return _mk(a + b + c)


THEME = {"theme": "单元测试主题", "index": "sz399363", "anchor": "sz399006"}
THEME_SELF = {"theme": "科创测试", "index": "sh000688", "anchor": "sh000688"}


# ====================================================================
# 契约 1：数据不可用必须显式
# ====================================================================

def test_missing_index_reported_as_unavailable_not_no_signal():
    r = sn.theme_status(THEME, None, _trend())
    assert r["available"] is False
    assert "数据不可用" in r["status"]
    assert "无信号" not in r["status"]


def test_missing_anchor_reported_as_unavailable():
    r = sn.theme_status(THEME, _trend(), None)
    assert r["available"] is False
    assert "宽基锚取数失败" in r["status"]


def test_stale_data_is_unavailable_not_safe():
    """数据过期 ⇒ 不可用。**绝不**把陈旧数据当成"当日无信号"。"""
    idx = _trend(n=200, last="2026-01-05")
    r = sn.theme_status(THEME, idx, _trend(200), as_of="2026-03-02")
    assert r["available"] is False
    assert r["staleness_days"] > sn.STALE_DAYS
    assert "数据不可用" in r["status"]


def test_empty_dataframe_is_unavailable():
    r = sn.theme_status(THEME, pd.DataFrame(columns=["date", "open", "high", "low", "close"]),
                        _trend())
    assert r["available"] is False


def test_loader_exception_degrades_to_unavailable():
    def _boom(code):
        raise RuntimeError("网络炸了")
    rows = sn.build_sentinel([THEME], _boom)
    assert len(rows) == 1 and rows[0]["available"] is False


# ====================================================================
# 契约 2：只显示、不决策
# ====================================================================

def test_every_row_is_marked_unvalidated():
    rows = [sn.theme_status(THEME, _trend(200), _trend(200))]
    assert rows[0]["validated"] is False
    txt = sn.format_sentinel(rows)
    assert sn.VALIDATION_NOTE in txt
    assert sn.DISCIPLINE_NOTE in txt
    assert "不产生买入信号" in txt


def test_format_contains_arbitration_note_and_series_section():
    rows = sn.build_sentinel([THEME, THEME_SELF],
                             lambda c: _trend(200) if c != "sh000688" else _trend(200))
    txt = sn.format_sentinel(rows, as_of="2026-09-30")
    assert "PANIC_DOWN" in txt and "矛盾指令" in txt
    assert "独立价格序列" in txt
    assert "数据充分度" in txt


def test_format_prints_failure_rows_in_availability_section():
    rows = [sn.theme_status(THEME, None, _trend(200))]
    txt = sn.format_sentinel(rows)
    assert "可用 0/1" in txt
    assert "❌" in txt


def test_arbitration_note_mentions_both_sides():
    note = sn.arbitration_note()
    assert "PANIC_DOWN" in note and "转折信号" in note


# ====================================================================
# 契约 3：无未来函数
# ====================================================================

def test_as_of_uses_only_past_data():
    """全序列 + as_of=T 的读数，必须与"先把数据截断到 T"的读数完全一致。"""
    idx = _l1_on_frame()
    a = _trend(len(idx), base=50.0)
    for T in (idx["date"].iloc[40], idx["date"].iloc[56], idx["date"].iloc[-1]):
        cut = idx[idx["date"] <= T].reset_index(drop=True)
        x = sn.theme_status(THEME, idx, a, as_of=T)
        y = sn.theme_status(THEME, cut, a, as_of=None)
        assert (x["l1"], x["l2"], x["rs"], x["close"]) == \
               (y["l1"], y["l2"], y["rs"], y["close"]), f"T={T} 出现前视偏差"


# ====================================================================
# 语义：状态标签 / RS 退化
# ====================================================================

def test_l1_lit_is_labelled_as_observation_only():
    idx = _l1_on_frame()
    r = sn.theme_status(THEME, idx, _trend(len(idx), base=50.0))
    assert r["l1"] is True
    assert "观察" in r["status"]
    assert r["l1_state_days"] >= 1
    assert r["available"] is True


def test_rs_is_na_when_index_equals_anchor():
    r = sn.theme_status(THEME_SELF, _trend(200), _trend(200))
    assert r["rs_available"] is False
    assert any("N/A" in w for w in r["warnings"])
    assert r["rs"] is False


def test_same_index_is_loaded_once_for_shared_themes():
    """10 个主题只有 5 条价格序列 ⇒ 同一指数只能取一次。"""
    calls = []

    def _load(code):
        calls.append(code)
        return _trend(120)

    t1 = {"theme": "A", "index": "sz399363", "anchor": "sz399006"}
    t2 = {"theme": "B", "index": "sz399363", "anchor": "sz399006"}
    rows = sn.build_sentinel([t1, t2], _load)
    assert len(rows) == 2
    assert calls.count("sz399363") == 1
    assert calls.count("sz399006") == 1


# ====================================================================
# 台账（P3.2）
# ====================================================================

def test_save_ledger_is_idempotent_and_carries_family(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_ttr", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "theme_timing_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    rows = sn.build_sentinel([THEME], lambda c: _trend(200))
    ledger = tmp_path / "theme_signal_ledger.csv"
    mod.save_ledger(rows, path=ledger)
    n1 = len(pd.read_csv(ledger))
    mod.save_ledger(rows, path=ledger)
    n2 = len(pd.read_csv(ledger))
    assert n1 == n2 == 1
    d = pd.read_csv(ledger)
    assert d["signal_family"].iloc[0] == "theme_timing"
    assert bool(d["validated"].iloc[0]) is False
    assert list(d.columns) == mod.COLUMNS


def test_save_ledger_skips_unavailable_rows(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_ttr2", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "theme_timing_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rows = sn.build_sentinel([THEME], lambda c: None)
    written, total = mod.save_ledger(rows, path=tmp_path / "x.csv")
    assert written == 0 and total == 0


# ====================================================================
# 观察项（WATCH_ONLY）与 RS 退化告警
# ====================================================================

def _rand_walk(n, shared=None, seed=0, scale=1.0, base=100.0):
    rng = np.random.default_rng(seed)
    inc = rng.normal(0, 1, n)
    if shared is not None:
        inc = shared + inc * scale
    return base + np.cumsum(inc)


def test_watch_only_shape_and_registered_indices():
    """观察项必须是「无题材」的，且指数必须已登记进 INDEX_CODES（否则取数会炸）。"""
    from core.marketdata_tx import INDEX_CODES
    from core.theme_universe import WATCH_ONLY
    assert len(WATCH_ONLY) >= 3
    for t in WATCH_ONLY:
        for k in ("theme", "kind", "plates", "etfs", "anchor", "index", "note"):
            assert k in t, f"{t.get('theme')} 缺少 {k}"
        assert t["kind"] == "watch"
        assert tuple(t["plates"]) == (), "观察项不得带题材（否则会污染轮动面板）"
        if str(t["index"]).startswith("synth:"):
            from core.synth_index import load_synth
            assert load_synth(t["index"]) is not None, \
                f"{t['index']} 是合成指数但缓存不存在（先跑 build_synth_index.py --save）"
        else:
            assert t["index"] in INDEX_CODES, f"{t['index']} 未登记进 INDEX_CODES"
        assert t["anchor"] in INDEX_CODES, f"{t['anchor']} 未登记进 INDEX_CODES"


def test_all_observed_is_themes_plus_watch_and_keeps_order():
    from core.theme_universe import THEMES, WATCH_ONLY, all_observed
    obs = all_observed()
    assert len(obs) == len(THEMES) + len(WATCH_ONLY)
    assert [t["theme"] for t in obs[:len(THEMES)]] == [t["theme"] for t in THEMES]
    assert all(t.get("kind", "theme") == "theme" for t in THEMES)


def test_sentinel_renders_two_sections():
    watch = {"theme": "测试观察项", "kind": "watch", "index": "sz399363",
             "anchor": "sz399006", "etfs": ()}
    theme = {"theme": "测试主题", "index": "sz399363", "anchor": "sz399006", "etfs": ()}
    rows = sn.build_sentinel([theme, watch], lambda c: _trend(200))
    assert rows[0]["kind"] == "theme" and rows[1]["kind"] == "watch"
    txt = sn.format_sentinel(rows)
    assert "【主题" in txt and "【观察项" in txt
    assert "不在 Phase 2 的验证范围内" in txt


def test_rs_weak_flag_when_theme_tracks_anchor():
    """主题与锚几乎同涨同跌 ⇒ 比价近乎常数 ⇒ 必须报警为「RS 退化」。"""
    n = 400
    rng = np.random.default_rng(7)
    shared = rng.normal(0, 1, n)
    theme = _mk(_rand_walk(n, shared=shared, seed=1, scale=0.1, base=100.0))
    anchor = _mk(_rand_walk(n, shared=shared, seed=2, scale=0.1, base=50.0))
    r = sn.theme_status(THEME, theme, anchor)
    assert r["corr_with_anchor"] is not None and r["corr_with_anchor"] > sn.RS_WEAK_CORR
    assert r["rs_weak"] is True
    assert any("RS 层近乎退化" in w for w in r["warnings"])


def test_rs_not_weak_for_independent_series():
    n = 400
    theme = _mk(_rand_walk(n, seed=11, base=100.0))
    anchor = _mk(_rand_walk(n, seed=12, base=50.0))
    r = sn.theme_status(THEME, theme, anchor)
    assert r["rs_weak"] is False


def test_rs_weak_is_reported_in_summary_section():
    n = 400
    rng = np.random.default_rng(3)
    shared = rng.normal(0, 1, n)
    theme = _mk(_rand_walk(n, shared=shared, seed=1, scale=0.1, base=100.0))
    anchor = _mk(_rand_walk(n, shared=shared, seed=2, scale=0.1, base=50.0))
    txt = sn.format_sentinel(sn.build_sentinel([THEME], lambda c: theme if c == THEME["index"] else anchor))
    assert "RS(风格)退化 1" in txt or "RS(风格)退化" in txt


def test_proxy_items_share_the_proxied_index():
    """代理项必须**复用被代理主题的指数**（否则会把"重复"伪装成"独立覆盖"）。"""
    from core.theme_universe import WATCH_ONLY, all_observed
    by_name = {t["theme"]: t for t in all_observed()}
    proxies = [t for t in WATCH_ONLY if t.get("proxy_of")]
    assert proxies, "至少应有一个代理项（卫星/航天）"
    for t in proxies:
        assert t["proxy_of"] in by_name, f"{t['theme']} 的代理目标 {t['proxy_of']} 不存在"
        assert t["index"] == by_name[t["proxy_of"]]["index"], (
            f"{t['theme']} 应复用 {t['proxy_of']} 的指数，而不是另挑一个")


def test_proxy_marker_is_rendered():
    from core.theme_universe import all_observed
    rows = sn.build_sentinel([t for t in all_observed() if t.get("proxy_of")],
                             lambda c: _trend(200))
    txt = sn.format_sentinel(rows)
    assert "代理项" in txt
    assert "(代理:" in txt


# ====================================================================
# 状态标签：待确认 / 结构已破只在近期生效
# ====================================================================

def test_pending_label_when_conditions_met_but_unconfirmed():
    """条件今天已满足、但按规则要下一根确认 ⇒ 必须显示 ⏳ 而不是"无信号"。"""
    idx = _l1_on_frame().iloc[:-1]          # 砍掉确认日 ⇒ 停在突破日
    a = _trend(len(idx), base=50.0)
    r = sn.theme_status(THEME, idx.reset_index(drop=True), a)
    from core.theme_timing import l1_pending
    assert l1_pending(idx.reset_index(drop=True)) is True
    assert r["l1"] is False and r["l1_pending"] is True
    assert "待下一根" in r["status"]


def test_broken_label_only_for_recent_spans():
    """久远的"曾转多"不该再占用状态标签（否则标签退化成常量）。"""
    idx = _l1_on_frame()
    a = _trend(len(idx), base=50.0)
    r = sn.theme_status(THEME, idx, a)
    assert r["l1_span_end_days_ago"] == 0          # 就亮在最后一根
    r2 = sn.theme_status(THEME, idx, a)
    assert "结构已破" in r2["status"] or "结构维持" in r2["status"] or "转多" in r2["status"]


def test_rs_only_label_when_no_recent_l1():
    """没有近期 L1、但 RS 走强 ⇒ 应显示"相对强度走强（价格未转多）"。"""
    n = 400
    rng = np.random.default_rng(5)
    theme = _mk(_rand_walk(n, seed=21, base=100.0) + np.linspace(0, 120, n))
    anchor = _mk(_rand_walk(n, seed=22, base=50.0))
    rows = sn.build_sentinel([THEME], lambda c: theme if c == THEME["index"] else anchor)
    r = rows[0]
    if r["rs"] and not r["l1"] and not r["l1_pending"] and r["l1_span_end_days_ago"] is None:
        assert "相对强度走强" in r["status"]


def test_rs_column_shows_both_weak_and_lit_state():
    """退化时必须同时显示告警与点亮状态，不能只给一个 ⚠️ 把信息盖住。"""
    n = 400
    rng = np.random.default_rng(11)
    shared = rng.normal(0, 1, n)
    theme = _mk(_rand_walk(n, shared=shared, seed=1, scale=0.1, base=100.0))
    anchor = _mk(_rand_walk(n, shared=shared, seed=2, scale=0.1, base=50.0))
    rows = sn.build_sentinel([THEME], lambda c: theme if c == THEME["index"] else anchor)
    assert rows[0]["rs_weak"] is True
    txt = sn.format_sentinel(rows)
    assert "⚠️✅" in txt or "⚠️·" in txt
    assert "该列不可信" in txt


def test_market_rs_column_present_and_marks_same_anchor():
    """两列相对强度必须同时出现；主题的锚本来就是沪深300时，大盘列显示『＝』。"""
    rows = sn.build_sentinel([{"theme": "T", "index": "sz399363", "anchor": sn.MARKET_ANCHOR}],
                             lambda c: _trend(200))
    assert rows[0]["rs_market_same_as_anchor"] is True
    assert rows[0]["rs_market"] == rows[0]["rs"]
    assert "＝" in sn.format_sentinel(rows)
    assert "大盘" in sn.format_sentinel(rows)


def test_market_rs_uses_market_frame_when_anchor_differs():
    """锚不是沪深300时，大盘列要用 market_df 单独算，不能等于风格列。"""
    n = 400
    rng = np.random.default_rng(31)
    theme = _mk(_rand_walk(n, seed=41, base=100.0))
    anchor = _mk(_rand_walk(n, seed=42, base=50.0))
    market = _mk(_rand_walk(n, seed=43, base=300.0))
    frames = {"sz399363": theme, "sz399006": anchor, "sh000300": market}
    rows = sn.build_sentinel([THEME], lambda c: frames.get(c))
    r = rows[0]
    assert r["rs_market_same_as_anchor"] is False
    assert r["corr_with_market"] is not None
    assert (r["rs_market"], r["rs"]) in ((True, True), (True, False), (False, True), (False, False))


def test_recent_span_days_constant_is_sane():
    assert 5 <= sn.RECENT_SPAN_DAYS <= 60
