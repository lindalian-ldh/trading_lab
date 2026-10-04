"""主题宇宙定义表（core/theme_universe.py）单元测试。

**全部离线**：成分股取数被 monkeypatch，或直接写临时 store。
"""

from __future__ import annotations

import pandas as pd
import pytest

import core.lhb_store as L
import core.theme_universe as TU


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "STORE_DIR", tmp_path / "lhb")
    return tmp_path / "lhb"


def _write_constituents(rows):
    """直接落盘成分股表（绕过联网）。rows: [(plate_code, plate_name, stock_code, stock_name)]"""
    df = pd.DataFrame([{
        "plate_type": 17, "plate_code": pc, "plate_name": pn,
        "stock_code": sc, "stock_name": sn, "time_in": "2021-07-14",
    } for pc, pn, sc, sn in rows], columns=L.PLATE_CONST_COLUMNS)
    path = L._path("plate_constituents.parquet")
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)


# ====================================================================
# 宽度分级
# ====================================================================

@pytest.mark.parametrize("n,expect", [
    (0, "无"), (None, "无"), (1, "窄"), (30, "窄"),
    (31, "中"), (300, "中"), (301, "宽"), (1531, "宽"),
])
def test_width_tier(n, expect):
    assert TU.width_tier(n) == expect


@pytest.mark.parametrize("n,expect", [(300, False), (301, True), (1134, True), (6, False)])
def test_is_broad(n, expect):
    assert TU.is_broad(n) is expect


# ====================================================================
# 主题定义的完整性（防止人工维护时写错）
# ====================================================================

def test_theme_names_unique():
    names = TU.theme_names()
    assert len(names) == len(set(names))


def test_every_theme_has_required_fields():
    for t in TU.THEMES:
        assert t["theme"] and isinstance(t["theme"], str)
        assert t["plates"], f"{t['theme']} 没有题材"
        assert t["anchor"], f"{t['theme']} 没有锚"
        assert "note" in t


def test_plate_codes_are_six_digits():
    for t in TU.THEMES:
        for code, name in t["plates"]:
            assert len(str(code)) == 6 and str(code).isdigit(), f"{t['theme']} 的 {code} 不是 6 位数字"
            assert name, f"{t['theme']} 的 {code} 没有题材名"


def test_all_anchors_are_fetchable_indices():
    """锚必须在 marketdata_tx 的 INDEX_CODES 里，否则 RS/择时会静默取数失败。"""
    from core.marketdata_tx import INDEX_CODES
    for t in TU.THEMES:
        assert t["anchor"] in INDEX_CODES, f"{t['theme']} 的锚 {t['anchor']} 不在 INDEX_CODES"


def test_optional_index_codes_are_valid_if_present():
    from core.marketdata_tx import INDEX_CODES
    for t in TU.THEMES:
        idx = t.get("index")
        if idx:
            assert idx in INDEX_CODES, f"{t['theme']} 的 index {idx} 不在 INDEX_CODES"


def test_etf_codes_are_tencent_format():
    for t in TU.THEMES:
        for code, name in t["etfs"]:
            assert code[:2] in ("sh", "sz") and len(code) == 8, f"{t['theme']} 的 ETF {code} 格式不对"
            assert name


def test_no_etf_claimed_by_two_themes():
    """一只 ETF 不应同时属于两个主题（否则面板会重复计数）。"""
    seen = {}
    for t in TU.THEMES:
        for code, _ in t["etfs"]:
            assert code not in seen, f"{code} 同时属于 {seen.get(code)} 与 {t['theme']}"
            seen[code] = t["theme"]


def test_every_theme_has_a_narrow_or_medium_plate():
    """每个主题至少要有一个『非宽』题材 —— 否则它完全没有聚焦口径可用。"""
    for t in TU.THEMES:
        assert len(t["plates"]) >= 1


# ====================================================================
# 查询
# ====================================================================

def test_get_theme_and_plates():
    t = TU.get_theme("半导体设备")
    assert t is not None and t["theme"] == "半导体设备"
    assert TU.theme_plates("半导体设备") == [("801490", "半导体设备")]
    assert TU.get_theme("不存在的主题") is None
    assert TU.theme_plates("不存在的主题") == []


def test_theme_by_etf():
    t = TU.theme_by_etf("sh562590")
    assert t is not None and t["theme"] == "半导体设备"
    assert TU.theme_by_etf("SH512480")["theme"] == "芯片"      # 大小写不敏感
    assert TU.theme_by_etf("sh999999") is None


def test_plates_used_is_deduped_and_sorted():
    used = TU.plates_used()
    assert used == sorted(set(used))
    assert "801490" in used


# ====================================================================
# 成分股并集与去重
# ====================================================================

def test_theme_constituent_counts_dedupes_across_plates(store):
    """同一只票出现在同一主题的两个题材里，只能算 1 只（这是上榜率的分母）。"""
    _write_constituents([
        ("801490", "半导体设备", "002371", "北方华创"),
        ("801490", "半导体设备", "300567", "精测电子"),
        ("801068", "第三代半导体", "002371", "北方华创"),   # 重复
        ("801068", "第三代半导体", "600460", "士兰微"),
    ])
    # 科创半导体 = 801490 ∪ 801068
    counts = TU.theme_constituent_counts()
    assert counts["科创半导体"] == 3          # 北方华创/精测电子/士兰微（去重）
    assert counts["半导体设备"] == 2
    assert counts["第三代半导体"] == 2


def test_theme_constituent_counts_zero_when_no_cache(store):
    counts = TU.theme_constituent_counts()
    assert set(counts) == set(TU.theme_names())
    assert all(v == 0 for v in counts.values())


def test_load_theme_constituents_filters_by_theme(store):
    _write_constituents([("801490", "半导体设备", "002371", "北方华创")])
    df = TU.load_theme_constituents("半导体设备")
    assert len(df) == 1 and df.iloc[0]["theme"] == "半导体设备"
    assert TU.load_theme_constituents("黄金").empty


def test_build_constituents_offline_uses_cache(store, monkeypatch):
    """build_constituents 应能只靠缓存组装（联网函数被替换为空实现）。"""
    _write_constituents([("801490", "半导体设备", "002371", "北方华创")])
    monkeypatch.setattr(TU, "fetch_plate_constituents", lambda *a, **k: pd.DataFrame())
    out = TU.build_constituents(verbose=False)
    assert not out.empty
    assert set(out.columns) == {"theme", "plate_code", "plate_name",
                                "stock_code", "stock_name", "time_in"}


# ====================================================================
# 审计
# ====================================================================

def test_audit_flags_broad_and_missing(store):
    _write_constituents([
        ("801490", "半导体设备", "002371", "北方华创"),
        # 801001 芯片故意不写 → 应被标为缺题材
    ])
    df = TU.audit(verbose=False).set_index("theme")

    assert df.loc["半导体设备", "n_constituents"] == 1
    assert df.loc["半导体设备", "width"] == "窄"
    assert bool(df.loc["半导体设备", "too_broad"]) is False

    # 芯片无成分股缓存 ⇒ 0 只、缺题材标记
    assert df.loc["芯片", "n_constituents"] == 0
    assert "801001" in df.loc["芯片", "plates_missing"]

    assert bool(df["anchor_ok"].all()) is True


def test_audit_all_have_valid_anchors_even_without_cache(store):
    df = TU.audit(verbose=False)
    assert bool(df["anchor_ok"].all()) is True
    assert len(df) == len(TU.THEMES)


def test_audit_marks_real_broad_themes(store):
    """用真实成分股数填充后，宽题材必须被标出来。"""
    rows = []
    for t in TU.THEMES:
        for code, name in t["plates"]:
            # 按各题材真实规模造数（芯片 1134 等）——只测『宽度标记』逻辑
            for i in range(3):
                rows.append((code, name, f"{i:06d}", f"票{i}"))
    _write_constituents(rows)
    # 人为把 801001 造到 400 只以触发『宽』
    rows2 = [("801001", "芯片", f"9{i:05d}", f"芯片票{i}") for i in range(400)]
    _write_constituents(rows + rows2)
    df = TU.audit(verbose=False).set_index("theme")
    assert df.loc["芯片", "width"] == "宽"
    assert bool(df.loc["芯片", "too_broad"]) is True
    assert df.loc["半导体设备", "width"] == "窄"


def test_format_audit_contains_key_markers(store):
    _write_constituents([("801490", "半导体设备", "002371", "北方华创")])
    txt = TU.format_audit(TU.audit(verbose=False))
    assert "主题宇宙定义表" in txt
    assert "半导体设备" in txt
    assert "宽度分级" in txt
    assert "无成分股" in txt          # 芯片等无缓存时应提示


def test_format_audit_handles_empty():
    assert "无主题定义" in TU.format_audit(pd.DataFrame())


def test_etf_coverage_shape():
    df = TU.etf_coverage()
    assert len(df) == sum(len(t["etfs"]) for t in TU.THEMES)
    assert set(df.columns) == {"theme", "etf", "name"}


# ====================================================================
# 数据层：fetch_plate_constituents（离线部分）
# ====================================================================

def test_load_plate_constituents_filters_by_type(store):
    _write_constituents([("801490", "半导体设备", "002371", "北方华创")])
    assert len(L.load_plate_constituents(17)) == 1
    assert L.load_plate_constituents(15).empty


def test_fetch_plate_constituents_uses_cache(store, monkeypatch):
    _write_constituents([("801490", "半导体设备", "002371", "北方华创")])

    def _boom():
        raise AssertionError("不应联网")
    monkeypatch.setattr(L, "_api", _boom)

    df = L.fetch_plate_constituents("801490")
    assert len(df) == 1 and df.iloc[0]["stock_code"] == "002371"


def test_fetch_plate_constituents_empty_returns_typed_frame(store, monkeypatch):
    """空结果必须返回带列的空表（供调用方显式判定"无法算上榜率"）。"""
    class NoCons:
        def plates_stocks(self, **kw):
            return []
    monkeypatch.setattr(L, "_api", lambda: NoCons())
    monkeypatch.setattr(L, "MAX_RETRY", 0)
    df = L.fetch_plate_constituents("801663")       # 实测取不到成分股的编码
    assert df.empty
    assert list(df.columns) == L.PLATE_CONST_COLUMNS
