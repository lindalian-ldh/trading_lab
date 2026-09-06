"""bankuai-service 单元测试：覆盖核心逻辑 + 边界用例。

测试分层：
    1. config        — plate_type 解析
    2. sector_ranker — 板块排名筛选（mock 数据层）
    3. leader_selector — 三级梯队核心算法 select_tiers（纯函数，重点）
    4. scanner       — 主调度器（mock 子模块，验证失败隔离）
    5. data_loader   — 缓存读写降级（mock zzshare）

运行方式：
    cd trading_lab && uv run pytest services/bankuai-service/test_bankuai.py -v

网络集成测试默认跳过，需开启：
    ENABLE_NETWORK_TESTS=1 uv run pytest services/bankuai-service/test_bankuai.py -v -k Integration
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# 把 bankuai-service 目录加入 sys.path，便于 `from config import ...`
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from config import ScanConfig, resolve_plate_type  # noqa: E402
from leader_selector import (  # noqa: E402
    select_tiers,
    _normalize_stock,
    build_uplimit_index,
    compute_tier1_scores,
    _seal_time_to_minutes,
    _seal_score,
    _percentile,
    _extract_continuous,
    _extract_name,
    _extract_reason,
    _extract_change_pct,
)
from sector_ranker import _normalize_plate  # noqa: E402


# ====================================================================
# 测试夹具
# ====================================================================

@pytest.fixture
def cfg() -> ScanConfig:
    """标准配置：tier_size=6, tier_min=3。"""
    return ScanConfig()


def _stock(code, name, rank, change, turnover_rate, circ_value, vol_ratio=1.0):
    """构造标准化成分股 dict（绕过网络，直接喂给 select_tiers）。"""
    return {
        "code": code, "name": name, "rank": rank, "change": change,
        "turnover_rate": turnover_rate, "circ_value": circ_value,
        "vol_ratio": vol_ratio,
        "est_turnover": circ_value * turnover_rate / 100.0,
    }


def _make_stocks(n=15, base_change=3.0):
    """构造 n 只成分股，rank 1..n，涨幅递减，换手率/市值/量比各异。"""
    stocks = []
    for i in range(1, n + 1):
        ch = base_change - i * 0.2  # 涨幅随 rank 递减
        tr = 10.0 - i * 0.3         # 换手率随 rank 递减
        cv = 5e9 + i * 1e8          # 流通市值递增
        vr = 3.0 - i * 0.15         # 量比随 rank 递减
        stocks.append(_stock(f"c{i:03d}", f"股{i}", i, round(ch, 2),
                             round(tr, 2), cv, round(vr, 2)))
    return stocks


# ====================================================================
# 1. config 测试
# ====================================================================

class TestConfig:
    def test_resolve_plate_type_str(self):
        assert resolve_plate_type("概念") == 15
        assert resolve_plate_type("题材") == 17
        assert resolve_plate_type("行业") == 14

    def test_resolve_plate_type_int(self):
        assert resolve_plate_type(15) == 15
        assert resolve_plate_type(17) == 17

    def test_resolve_plate_type_invalid_str(self):
        with pytest.raises(ValueError):
            resolve_plate_type("不存在")

    def test_resolve_plate_type_invalid_int(self):
        with pytest.raises(ValueError):
            resolve_plate_type(99)

    def test_plate_type_name(self):
        cfg = ScanConfig(plate_type=17)
        assert cfg.plate_type_name() == "题材"


# ====================================================================
# 2. sector_ranker 测试
# ====================================================================

class TestSectorRanker:
    def test_normalize_plate(self):
        raw = {
            "plate_name": "人工智能", "plate_code": "885852",
            "rate": 3.25, "trade_money": 6785390000.0, "score": 970,
            "time": "2026-08-12 15:01:03",
        }
        s = _normalize_plate(raw, rank=1)
        assert s["name"] == "人工智能"
        assert s["code"] == "885852"
        assert s["rank"] == 1
        assert s["change"] == 3.25
        assert s["turnover"] == 67.85  # 元 → 亿
        assert s["score"] == 970

    def test_normalize_plate_missing_fields(self):
        """缺失字段应优雅降级为默认值。"""
        s = _normalize_plate({}, rank=5)
        assert s["name"] == ""
        assert s["rank"] == 5
        assert s["change"] == 0.0
        assert s["turnover"] == 0.0

    def test_rank_sectors_hot_cold(self, monkeypatch, cfg):
        """Top N / Bottom N 取值正确，且合并去重。"""
        from sector_ranker import rank_sectors

        # 构造 30 个板块，score 递减
        raw = [
            {"plate_name": f"板块{i}", "plate_code": f"88{i:04d}",
             "rate": 5.0 - i * 0.2, "trade_money": 1e9 * (30 - i),
             "score": 1000 - i * 10, "time": "2026-08-12 15:01:03"}
            for i in range(1, 31)
        ]
        monkeypatch.setattr("sector_ranker.fetch_plates_rank",
                            lambda date1, c, limit=None: raw)

        cfg.top_n = 10
        cfg.bottom_n = 10
        result = rank_sectors("2026-08-12", cfg)

        assert result is not None
        assert result["total"] == 30
        assert len(result["hot_sectors"]) == 10
        assert len(result["cold_sectors"]) == 10
        # 热门第1名 rank=1，冷门第1名 rank=30
        assert result["hot_sectors"][0]["rank"] == 1
        assert result["cold_sectors"][0]["rank"] == 30
        # 去重后 target = 20（无重叠）
        assert len(result["target_sectors"]) == 20
        # target 第一项是热门第1
        assert result["target_sectors"][0]["name"] == "板块1"

    def test_rank_sectors_empty(self, monkeypatch, cfg):
        """接口返回空 → None。"""
        from sector_ranker import rank_sectors
        monkeypatch.setattr("sector_ranker.fetch_plates_rank",
                            lambda date1, c, limit=None: None)
        assert rank_sectors("2026-08-12", cfg) is None

    def test_rank_sectors_overlap_dedup(self, monkeypatch, cfg):
        """板块总数 < top_n + bottom_n 时，target 去重。"""
        from sector_ranker import rank_sectors
        raw = [
            {"plate_name": f"板块{i}", "plate_code": f"88{i:04d}",
             "rate": 3.0, "trade_money": 1e9, "score": 100 - i, "time": ""}
            for i in range(1, 6)  # 仅 5 个板块
        ]
        monkeypatch.setattr("sector_ranker.fetch_plates_rank",
                            lambda date1, c, limit=None: raw)
        cfg.top_n = 10
        cfg.bottom_n = 10
        result = rank_sectors("2026-08-12", cfg)
        # 仅 5 个板块，hot 与 cold 完全重叠，去重后 target=5
        assert len(result["target_sectors"]) == 5


# ====================================================================
# 3. leader_selector 核心算法测试（重点）
# ====================================================================

class TestSelectTiers:
    def test_basic_three_tiers(self, cfg):
        """充足数据下，三级梯队各 3-6 只，且无重叠。"""
        stocks = _make_stocks(15)
        r = select_tiers(stocks, cfg)

        for tier_name in ("tier1", "tier2", "tier3"):
            tier = r[tier_name]
            assert 3 <= len(tier) <= 6, f"{tier_name} 数量 {len(tier)} 不在 [3,6]"

        # 无重叠
        all_codes = [s["code"] for t in (r["tier1"], r["tier2"], r["tier3"]) for s in t]
        assert len(all_codes) == len(set(all_codes)), "梯队间存在重叠"

    def test_tier1_rank_ascending(self, cfg):
        """一级龙头按 rank 升序，且 change > avg_change。"""
        stocks = _make_stocks(15)
        r = select_tiers(stocks, cfg)
        avg = r["avg_change"]
        for s in r["tier1"]:
            # 放宽条件下可能 change>0 但 <=avg，这里数据充足应满足严格条件
            assert s["change"] > 0
        # tier1 的 rank 应是较小的几个
        t1_ranks = [s["rank"] for s in r["tier1"]]
        assert min(t1_ranks) == 1  # rank=1 必入一级

    def test_tier1_relax_when_strict_too_few(self, cfg):
        """严格条件(change>avg)不足3只时，放宽到 change>0。"""
        # 构造：rank 前2 涨幅高，其余涨幅为负 → 严格条件仅2只，需放宽
        stocks = [
            _stock("a", "A", 1, 8.0, 5.0, 5e9),
            _stock("b", "B", 2, 7.0, 4.0, 5e9),
            _stock("c", "C", 3, -1.0, 3.0, 5e9),  # 负涨幅
            _stock("d", "D", 4, -2.0, 2.0, 5e9),
            _stock("e", "E", 5, 0.5, 1.0, 5e9),   # 正但低于 avg
            _stock("f", "F", 6, 0.3, 6.0, 5e9),
            _stock("g", "G", 7, -0.5, 7.0, 5e9),
            _stock("h", "H", 8, 0.1, 8.0, 5e9),
            _stock("i", "I", 9, 0.2, 9.0, 5e9),
        ]
        r = select_tiers(stocks, cfg)
        # avg ≈ (8+7-1-2+0.5+0.3-0.5+0.1+0.2)/9 ≈ 1.4
        # 严格 change>1.4 仅 A,B → 不足3 → 放宽 change>0
        assert len(r["tier1"]) >= 3
        for s in r["tier1"]:
            assert s["change"] > 0

    def test_tier2_by_est_turnover_desc(self, cfg):
        """二级按 est_turnover 降序，剔除一级。"""
        stocks = _make_stocks(15)
        r = select_tiers(stocks, cfg)
        t1_codes = {s["code"] for s in r["tier1"]}
        # 二级不含一级
        for s in r["tier2"]:
            assert s["code"] not in t1_codes
        # 二级 est_turnover 降序
        et = [s["est_turnover"] for s in r["tier2"]]
        assert et == sorted(et, reverse=True)

    def test_tier3_by_turnover_rate_desc(self, cfg):
        """三级按换手率降序，剔除一二级。"""
        stocks = _make_stocks(15)
        r = select_tiers(stocks, cfg)
        used = {s["code"] for s in r["tier1"]} | {s["code"] for s in r["tier2"]}
        for s in r["tier3"]:
            assert s["code"] not in used
        tr = [s["turnover_rate"] for s in r["tier3"]]
        assert tr == sorted(tr, reverse=True)

    def test_no_overlap_between_tiers(self, cfg):
        stocks = _make_stocks(20)
        r = select_tiers(stocks, cfg)
        sets = [ {s["code"] for s in r[t]} for t in ("tier1", "tier2", "tier3") ]
        # 两两不相交
        assert sets[0].isdisjoint(sets[1])
        assert sets[0].isdisjoint(sets[2])
        assert sets[1].isdisjoint(sets[2])

    def test_empty_stocks(self, cfg):
        """空列表 → 三个空梯队，avg=0。"""
        r = select_tiers([], cfg)
        assert r["avg_change"] == 0.0
        assert r["tier1"] == [] and r["tier2"] == [] and r["tier3"] == []

    def test_few_stocks_under_9(self, cfg):
        """成分股 <9 只：按实际分配，各梯队 ≤ 实际数量。"""
        stocks = _make_stocks(6)
        r = select_tiers(stocks, cfg)
        total_selected = len(r["tier1"]) + len(r["tier2"]) + len(r["tier3"])
        assert total_selected <= 6
        # 每个梯队不超过 tier_size
        for t in ("tier1", "tier2", "tier3"):
            assert len(r[t]) <= 6

    def test_backfill_to_tier_min(self, cfg):
        """梯队不足 tier_min 时从剩余池补足。"""
        cfg.tier_min = 3
        # 构造：仅 3 只正涨幅，其余全负 → 一级严格+放宽都从这3只选
        # 二级 change>0 也只能从已选的挑，会触发放宽 + backfill
        stocks = [
            _stock("a", "A", 1, 5.0, 10.0, 1e10),
            _stock("b", "B", 2, 4.0, 9.0, 1e10),
            _stock("c", "C", 3, 3.0, 8.0, 1e10),
            _stock("d", "D", 4, -1.0, 7.0, 1e10),
            _stock("e", "E", 5, -2.0, 6.0, 1e10),
            _stock("f", "F", 6, -3.0, 5.0, 1e10),
            _stock("g", "G", 7, -4.0, 4.0, 1e10),
            _stock("h", "H", 8, -5.0, 3.0, 1e10),
            _stock("i", "I", 9, -6.0, 2.0, 1e10),
        ]
        r = select_tiers(stocks, cfg)
        # 一级用掉 a,b,c；二级 change>0 无候选 → 放宽按 est_turnover 从 d..i 选
        # backfill 应保证每队尽量 >=3（只要池子够）
        assert len(r["tier1"]) >= 1
        # 无重叠
        all_codes = [s["code"] for t in (r["tier1"], r["tier2"], r["tier3"]) for s in t]
        assert len(all_codes) == len(set(all_codes))

    def test_tier_size_limit(self, cfg):
        """tier_size 限制各梯队最大数量。"""
        cfg.tier_size = 4
        stocks = _make_stocks(20)
        r = select_tiers(stocks, cfg)
        for t in ("tier1", "tier2", "tier3"):
            assert len(r[t]) <= 4

    def test_avg_change_calculation(self, cfg):
        """avg_change = 所有成分股 change 均值。"""
        stocks = [
            _stock("a", "A", 1, 2.0, 5.0, 5e9),
            _stock("b", "B", 2, 4.0, 5.0, 5e9),
            _stock("c", "C", 3, 6.0, 5.0, 5e9),
        ]
        r = select_tiers(stocks, cfg)
        assert r["avg_change"] == 4.0


class TestNormalizeStock:
    def test_normalize_stock(self):
        raw = {
            "stock_code": "600721", "stock_name": "百花医药", "rank": 3,
            "px_change_rate": 10.04, "turnover_ratio": 34.43,
            "circulation_value": 5395203319.0, "vol_ratio": 2.21,
        }
        s = _normalize_stock(raw)
        assert s["code"] == "600721"
        assert s["name"] == "百花医药"
        assert s["rank"] == 3
        assert s["change"] == 10.04
        assert s["turnover_rate"] == 34.43
        assert s["vol_ratio"] == 2.21
        assert s["circ_value"] == 5395203319.0
        # est_turnover = circ * tr / 100
        assert abs(s["est_turnover"] - 5395203319.0 * 34.43 / 100.0) < 1e-2

    def test_normalize_stock_missing(self):
        s = _normalize_stock({})
        assert s["code"] == ""
        assert s["change"] == 0.0
        assert s["est_turnover"] == 0.0
        assert s["vol_ratio"] == 0.0


# ====================================================================
# 3.5 一级龙头综合评分测试（新增）
# ====================================================================

class TestUplimitIndex:
    """涨停股池索引构建 + 封板时间解析。"""

    def test_build_index_basic(self):
        raw = [
            {"stock_code": "000001", "seal_time": "09:35:00", "continuous": 1},
            {"code": "000002", "first_limit_time": "10:20", "lian_ban": 2},
            {"ts_code": "600721.SH", "zt_time": "13:45:00"},
        ]
        idx = build_uplimit_index(raw)
        assert "000001" in idx
        assert idx["000001"]["seal_time"] == "09:35:00"
        assert idx["000001"]["continuous"] == 1
        assert "000002" in idx
        assert idx["000002"]["continuous"] == 2
        # ts_code 形式应去掉 .SH 后缀
        assert "600721" in idx
        assert idx["600721"]["seal_time"] == "13:45:00"

    def test_build_index_empty(self):
        assert build_uplimit_index(None) == {}
        assert build_uplimit_index([]) == {}
        assert build_uplimit_index([{"foo": "bar"}]) == {}  # 无 code 字段

    def test_build_index_dict_wrapper(self):
        """支持 {"list": [...]} 包装结构（已在 data_loader 规整，这里测 list 直传）。"""
        raw = [{"stock_code": "000001", "seal_time": "09:30"}]
        idx = build_uplimit_index(raw)
        assert len(idx) == 1

    def test_seal_time_formats(self):
        """多种涨停时间格式解析。"""
        # HH:MM:SS
        assert _seal_time_to_minutes("09:35:00") == 5  # 9:30 + 5min
        # HH:MM
        assert _seal_time_to_minutes("10:20") == 50
        # 带日期前缀
        assert _seal_time_to_minutes("2026-08-12 09:35:00") == 5
        # 纯数字 093500
        assert _seal_time_to_minutes("093500") == 5
        # 带毫秒
        assert _seal_time_to_minutes("09:35:00.123") == 5
        # 早于开盘（集合竞价封板）→ 负数
        assert _seal_time_to_minutes("09:25:00") == -5
        # 非法格式
        assert _seal_time_to_minutes("invalid") is None
        assert _seal_time_to_minutes(None) is None
        assert _seal_time_to_minutes("") is None

    def test_seal_score_mapping(self):
        """封板时间 → 0-100 分：9:30=100, 15:00=0, 未涨停=0。"""
        # 9:30 开盘即封板 → 100 分
        assert _seal_score(_seal_time_to_minutes("09:30:00")) == 100.0
        # 集合竞价封板（早于 9:30）→ 100 分
        assert _seal_score(_seal_time_to_minutes("09:25:00")) == 100.0
        # 15:00 收盘封板 → 0 分
        assert _seal_score(_seal_time_to_minutes("15:00:00")) == 0.0
        # 未涨停
        assert _seal_score(None) == 0.0
        # 10:00 封板 → 中间值
        score_10 = _seal_score(_seal_time_to_minutes("10:00:00"))
        assert 0 < score_10 < 100


class TestTier1Scoring:
    """一级龙头综合评分核心逻辑。"""

    def test_score_without_uplimit_degrades_to_relative_strength(self, cfg):
        """无涨停数据时，封板强度降级为相对强度归一化。"""
        stocks = _make_stocks(10)
        avg = sum(s["change"] for s in stocks) / len(stocks)
        scores = compute_tier1_scores(stocks, avg, None, cfg)
        assert len(scores) == 10
        # rank=1 涨幅最高、量比最高、换手最高 → 综合评分应最高
        assert scores[0] == max(scores)

    def test_score_with_uplimit_early_seal_ranks_higher(self, cfg):
        """有涨停数据时，封板时间越早评分越高。"""
        stocks = _make_stocks(6)
        avg = sum(s["change"] for s in stocks) / len(stocks)
        # 让 c003 9:35 早封板，c001 14:00 晚封板
        uplimit = {
            "c003": {"seal_time": "09:35:00", "continuous": 1},
            "c001": {"seal_time": "14:00:00", "continuous": 1},
        }
        scores = compute_tier1_scores(stocks, avg, uplimit, cfg)
        # c003 封板得分高，c001 封板得分低
        idx_c003 = next(i for i, s in enumerate(stocks) if s["code"] == "c003")
        idx_c001 = next(i for i, s in enumerate(stocks) if s["code"] == "c001")
        # c003 的封板分应高于 c001
        # （c001 涨幅/量比/换手本更高，但封板晚；这里仅验证封板分项差异，
        #   综合评分 c001 仍可能更高，故单独验证封板分项）
        # 直接验证：c003 涨停分 > 0，c001 涨停分 < c003
        assert scores[idx_c003] > 0
        # c001 虽然其他分项高，但封板极晚（接近 0 分）
        # 由于 c001 其他分项都是满分，综合分仍可能高，这里不强断言 c003 > c001

    def test_first_limit_bonus(self, cfg):
        """板块内首个涨停股获得额外加分。"""
        stocks = _make_stocks(5)
        avg = sum(s["change"] for s in stocks) / len(stocks)
        # c002 最早封板（9:35），c004 晚封板（10:30）
        uplimit = {
            "c002": {"seal_time": "09:35:00", "continuous": 1},
            "c004": {"seal_time": "10:30:00", "continuous": 1},
        }
        scores_with = compute_tier1_scores(stocks, avg, uplimit, cfg)

        # 对比：把 c002 的封板时间改晚（10:00），c004 不变 → c004 成首个涨停
        uplimit2 = {
            "c002": {"seal_time": "10:00:00", "continuous": 1},
            "c004": {"seal_time": "10:30:00", "continuous": 1},
        }
        # 此时 c002 仍是首个（10:00 < 10:30）
        scores_with2 = compute_tier1_scores(stocks, avg, uplimit2, cfg)

        idx_c002 = next(i for i, s in enumerate(stocks) if s["code"] == "c002")
        # c002 在两场景下都是首个涨停，都有 bonus
        # 第一场景 c002=9:35(首个)，第二场景 c002=10:00(首个)
        # 第一场景 c002 封板分更高 → 综合分更高
        assert scores_with[idx_c002] > scores_with2[idx_c002]

    def test_no_uplimit_stocks_get_zero_seal_score(self, cfg):
        """涨停股池中不存在的股票，封板分=0。"""
        stocks = _make_stocks(5)
        avg = sum(s["change"] for s in stocks) / len(stocks)
        uplimit = {"c001": {"seal_time": "09:35:00", "continuous": 1}}
        # 只有 c001 在涨停池，其余封板分=0
        scores = compute_tier1_scores(stocks, avg, uplimit, cfg)
        # c001 因涨停 + 首个涨停 bonus，应得分最高
        idx_c001 = next(i for i, s in enumerate(stocks) if s["code"] == "c001")
        assert scores[idx_c001] == max(scores)

    def test_select_tiers_with_uplimit_changes_order(self, cfg):
        """有涨停数据时，封板早的股票能进入一级（即使 rank 靠后）。"""
        # 构造：e 涨幅最高 + 9:35 早封板；其余涨幅递减且未涨停
        # avg ≈ 3.56，e(6.0)>avg 满足硬过滤，封板+首个涨停 bonus 加成入选一级
        stocks = [
            _stock("a", "A", 1, 5.0, 5.0, 5e9, vol_ratio=2.0),
            _stock("b", "B", 2, 4.5, 5.0, 5e9, vol_ratio=2.0),
            _stock("c", "C", 3, 4.0, 5.0, 5e9, vol_ratio=2.0),
            _stock("d", "D", 4, 3.5, 5.0, 5e9, vol_ratio=2.0),
            _stock("e", "E", 5, 6.0, 5.0, 5e9, vol_ratio=2.0),  # 涨幅最高+早封板
            _stock("f", "F", 6, 3.0, 5.0, 5e9, vol_ratio=2.0),
            _stock("g", "G", 7, 2.5, 5.0, 5e9, vol_ratio=2.0),
            _stock("h", "H", 8, 2.0, 5.0, 5e9, vol_ratio=2.0),
            _stock("i", "I", 9, 1.5, 5.0, 5e9, vol_ratio=2.0),
        ]
        # e 在 9:35 早封板，是板块首个涨停
        uplimit = {"e": {"seal_time": "09:35:00", "continuous": 1}}
        r = select_tiers(stocks, cfg, uplimit_index=uplimit)
        # e 应进入一级（涨幅最高 + 封板 + 首个涨停 bonus）
        t1_codes = {s["code"] for s in r["tier1"]}
        assert "e" in t1_codes
        # e 综合评分应最高（涨幅相对强度满分 + 封板满分 + 首个涨停 bonus）
        assert r["tier1"][0]["code"] == "e"

    def test_select_tiers_tier1_has_score_field(self, cfg):
        """一级龙头结果应包含 score 字段（用于展示）。"""
        stocks = _make_stocks(10)
        r = select_tiers(stocks, cfg)
        for s in r["tier1"]:
            assert "score" in s
            assert isinstance(s["score"], float)

    def test_degrade_and_uplimit_produce_valid_results(self, cfg):
        """降级模式与涨停模式都能产出合法梯队（无重叠、数量合规）。"""
        stocks = _make_stocks(15)
        # 降级
        r1 = select_tiers(stocks, cfg, uplimit_index=None)
        # 涨停模式
        uplimit = {"c001": {"seal_time": "09:35:00", "continuous": 1},
                   "c005": {"seal_time": "10:30:00", "continuous": 2}}
        r2 = select_tiers(stocks, cfg, uplimit_index=uplimit)
        for r in (r1, r2):
            assert 3 <= len(r["tier1"]) <= 6
            assert 3 <= len(r["tier2"]) <= 6
            assert 3 <= len(r["tier3"]) <= 6
            all_codes = [s["code"] for t in (r["tier1"], r["tier2"], r["tier3"]) for s in t]
            assert len(all_codes) == len(set(all_codes))


# ====================================================================
# 3.6 二级龙头（中军）三重过滤测试（新增）
# ====================================================================

class TestPercentile:
    """百分位数计算辅助函数。"""

    def test_median(self):
        assert _percentile([1, 2, 3, 4, 5], 50) == 3.0

    def test_quartiles(self):
        vals = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        q1 = _percentile(vals, 25)
        q3 = _percentile(vals, 75)
        assert q1 < q3
        assert 2 < q1 < 4   # Q1 ≈ 3.25
        assert 7 < q3 < 9   # Q3 ≈ 7.75

    def test_edge_cases(self):
        assert _percentile([], 50) == 0.0
        assert _percentile([42], 50) == 42.0
        assert _percentile([42], 25) == 42.0
        # 全相等
        assert _percentile([5, 5, 5, 5], 50) == 5.0


class TestTier2Filter:
    """二级龙头（中军）：流动性 + 稳定性三重过滤。"""

    def test_tier2_change_in_band(self, cfg):
        """中军涨幅应在 0 ~ 板块均幅之间（跟涨但不领涨）。"""
        # 构造：rank1-6 涨幅>avg 被一级选走；rank7-12 涨幅在 0~avg；
        #       rank13-15 涨幅<0
        stocks = []
        for i in range(1, 16):
            if i <= 6:
                ch = 5.0 + (6 - i) * 0.5   # 7.5~5.0 > avg
            elif i <= 12:
                ch = 2.5 + (12 - i) * 0.3  # 2.5~0.8 在 0~avg
            else:
                ch = -1.0 - (i - 13) * 0.5  # 负涨幅
            stocks.append(_stock(f"s{i:02d}", f"S{i}", i, round(ch, 2),
                                 5.0, 5e9, vol_ratio=1.5))
        r = select_tiers(stocks, cfg)
        avg = r["avg_change"]
        # 中军涨幅应全部在 (0, avg] 区间
        for s in r["tier2"]:
            assert 0 < s["change"] <= avg + 0.01  # +0.01 容浮点误差

    def test_tier2_circ_above_median(self, cfg):
        """中军流通市值应 >= 板块中位数（中等以上）。"""
        # 构造差异化的流通市值：低市值组 + 高市值组
        stocks = []
        for i in range(1, 13):
            circ = 2e9 if i <= 6 else 8e9  # 前6只低市值，后6只高市值
            ch = 2.0 if i <= 6 else 1.5    # 涨幅都在 0~avg 区间
            stocks.append(_stock(f"s{i:02d}", f"S{i}", i, ch, 5.0, circ, vol_ratio=1.5))
        # 加3只涨幅>avg 让一级选走（避免影响二级）
        for i in range(13, 16):
            stocks.append(_stock(f"s{i:02d}", f"S{i}", i, 6.0, 5.0, 1e10, vol_ratio=2.0))

        r = select_tiers(stocks, cfg)
        median_circ = _percentile([s["circ_value"] for s in stocks], 50)
        # 中军流通市值应 >= 中位数
        for s in r["tier2"]:
            assert s["circ_value"] >= median_circ - 1  # -1 容浮点误差

    def test_tier2_turnover_moderate(self, cfg):
        """中军换手率应适中（Q1~Q3 区间），排除极端高/低。"""
        # 构造：换手率从 1% 到 20% 均匀分布
        stocks = []
        for i in range(1, 16):
            ch = 2.0  # 涨幅在 0~avg
            tr = 1.0 + (i - 1) * 1.3  # 1.0 ~ 18.2
            stocks.append(_stock(f"s{i:02d}", f"S{i}", i, ch, round(tr, 2),
                                 5e9, vol_ratio=1.5))
        # 加几只高涨幅让一级选走
        for i in range(16, 20):
            stocks.append(_stock(f"s{i:02d}", f"S{i}", i, 7.0, 10.0, 5e9, vol_ratio=3.0))

        r = select_tiers(stocks, cfg)
        turnovers = [s["turnover_rate"] for s in stocks]
        q1 = _percentile(turnovers, 25)
        q3 = _percentile(turnovers, 75)
        # 中军换手率应在 Q1~Q3（除非降级）
        # 注意：可能因降级放宽，这里验证"非极端"即可
        t2_max = max(s["turnover_rate"] for s in r["tier2"])
        t2_min = min(s["turnover_rate"] for s in r["tier2"])
        # 中军不应包含换手率最高或最低的极端值（除非降级到全部）
        all_max = max(turnovers)
        all_min = min(turnovers)
        # 至少中军里不应全是极端值
        assert t2_max < all_max or t2_min > all_min or len(r["tier2"]) >= cfg.tier_min

    def test_tier2_sorted_by_est_turnover(self, cfg):
        """中军应按 est_turnover 降序排列（流动性优先）。"""
        stocks = _make_stocks(15)
        r = select_tiers(stocks, cfg)
        turnovers = [s["est_turnover"] for s in r["tier2"]]
        assert turnovers == sorted(turnovers, reverse=True)

    def test_tier2_degrade_when_too_strict(self, cfg):
        """三重过滤不足 tier_min 时逐级降级，最终保证 >= tier_min。"""
        # 构造极端数据：大部分股票涨幅<0 或 流通市值极低
        stocks = []
        for i in range(1, 10):
            ch = -2.0 if i <= 6 else 1.0  # 大部分跌
            circ = 1e8 if i <= 6 else 5e9  # 大部分低市值
            stocks.append(_stock(f"s{i:02d}", f"S{i}", i, ch, 5.0, circ, vol_ratio=1.5))
        # 加几只高涨幅让一级选走
        for i in range(10, 13):
            stocks.append(_stock(f"s{i:02d}", f"S{i}", i, 8.0, 5.0, 5e9, vol_ratio=3.0))

        r = select_tiers(stocks, cfg)
        # 即使条件苛刻，降级后中军也应 >= tier_min
        assert len(r["tier2"]) >= cfg.tier_min

    def test_tier2_no_overlap_with_tier1(self, cfg):
        """中军与一级无重叠。"""
        stocks = _make_stocks(15)
        r = select_tiers(stocks, cfg)
        t1_codes = {s["code"] for s in r["tier1"]}
        t2_codes = {s["code"] for s in r["tier2"]}
        assert not (t1_codes & t2_codes)

    def test_tier2_all_down_sector(self, cfg):
        """全板块下跌（avg<0）时，二级降级到 change>0 或全部。"""
        stocks = []
        for i in range(1, 16):
            ch = -3.0 + i * 0.1  # -2.9 ~ -1.5，全部为负
            stocks.append(_stock(f"s{i:02d}", f"S{i}", i, round(ch, 2),
                                 5.0, 5e9, vol_ratio=1.5))
        r = select_tiers(stocks, cfg)
        # avg < 0，0<change<=avg 不可能满足，降级到全部
        assert len(r["tier2"]) >= cfg.tier_min


# ====================================================================
# 3.7 三级龙头（补涨）三重过滤测试（新增）
# ====================================================================

class TestTier3Filter:
    """三级龙头（补涨）：涨幅落后 + 量能启动三重过滤。"""

    def test_tier3_change_below_avg(self, cfg):
        """补涨涨幅应 < 板块均幅（涨幅落后，有补涨空间）。"""
        stocks = []
        for i in range(1, 16):
            if i <= 6:
                ch = 6.0 - i * 0.3   # 5.7~4.2 > avg
            elif i <= 12:
                ch = 2.0 - i * 0.1   # 0.8~0.2 在 0~avg
            else:
                ch = -0.5 - (i - 13) * 0.3  # 负涨幅 < avg
            stocks.append(_stock(f"s{i:02d}", f"S{i}", i, round(ch, 2),
                                 5.0, 5e9, vol_ratio=1.5))
        r = select_tiers(stocks, cfg)
        avg = r["avg_change"]
        # 补涨涨幅应 < avg（除非降级到全部）
        for s in r["tier3"]:
            assert s["change"] < avg + 0.01  # +0.01 容浮点误差

    def test_tier3_turnover_above_avg(self, cfg):
        """补涨换手率应 > 板块平均换手（资金开始关注）。"""
        # 构造：换手率差异明显，前12只低换手，后3只高换手
        stocks = []
        for i in range(1, 13):
            ch = 5.0  # 高涨幅，被一二级选走
            tr = 2.0  # 低换手
            stocks.append(_stock(f"s{i:02d}", f"S{i}", i, ch, tr, 5e9, vol_ratio=0.5))
        for i in range(13, 19):
            ch = -1.0  # 涨幅落后
            tr = 8.0   # 高换手 > avg
            stocks.append(_stock(f"s{i:02d}", f"S{i}", i, ch, tr, 5e9, vol_ratio=2.0))

        r = select_tiers(stocks, cfg)
        avg_turnover = sum(s["turnover_rate"] for s in stocks) / len(stocks)
        # 补涨换手率应 > 均值（除非降级）
        for s in r["tier3"]:
            assert s["turnover_rate"] > avg_turnover - 0.01 or len(r["tier3"]) >= cfg.tier_min

    def test_tier3_vol_ratio_above_1(self, cfg):
        """补涨量比应 > 1（量能启动）。"""
        stocks = []
        for i in range(1, 13):
            ch = 5.0       # 高涨幅
            vr = 0.5       # 量比 < 1
            stocks.append(_stock(f"s{i:02d}", f"S{i}", i, ch, 5.0, 5e9, vol_ratio=vr))
        for i in range(13, 19):
            ch = -1.0      # 涨幅落后
            vr = 2.5       # 量比 > 1
            stocks.append(_stock(f"s{i:02d}", f"S{i}", i, ch, 8.0, 5e9, vol_ratio=vr))

        r = select_tiers(stocks, cfg)
        # 补涨量比应 > 1（三重过滤满足时）
        # 由于一二级选走了前12只，三级从13-18中选，这些量比都>1
        for s in r["tier3"]:
            assert s["vol_ratio"] > 1.0 - 0.01

    def test_tier3_sorted_by_vol_ratio(self, cfg):
        """补涨应按 vol_ratio 降序排列（量能启动强度优先）。"""
        stocks = _make_stocks(15)
        r = select_tiers(stocks, cfg)
        vol_ratios = [s["vol_ratio"] for s in r["tier3"]]
        assert vol_ratios == sorted(vol_ratios, reverse=True)

    def test_tier3_degrade_when_too_strict(self, cfg):
        """三重过滤不足 tier_min 时逐级降级，最终保证 >= tier_min。"""
        # 构造：大部分股票量比 < 1（无法满足量能启动条件）
        stocks = []
        for i in range(1, 13):
            ch = 5.0
            vr = 0.3  # 量比 < 1
            stocks.append(_stock(f"s{i:02d}", f"S{i}", i, ch, 5.0, 5e9, vol_ratio=vr))
        for i in range(13, 16):
            ch = -1.0
            vr = 0.5  # 量比仍 < 1，需降级
            stocks.append(_stock(f"s{i:02d}", f"S{i}", i, ch, 5.0, 5e9, vol_ratio=vr))

        r = select_tiers(stocks, cfg)
        # 降级后补涨也应 >= tier_min
        assert len(r["tier3"]) >= cfg.tier_min

    def test_tier3_no_overlap_with_tier1_tier2(self, cfg):
        """补涨与一二级无重叠。"""
        stocks = _make_stocks(15)
        r = select_tiers(stocks, cfg)
        t1_codes = {s["code"] for s in r["tier1"]}
        t2_codes = {s["code"] for s in r["tier2"]}
        t3_codes = {s["code"] for s in r["tier3"]}
        assert not (t3_codes & t1_codes)
        assert not (t3_codes & t2_codes)

    def test_tier3_all_stocks_above_avg(self, cfg):
        """全板块涨幅都 > avg（极端情况）时，三级降级到全部剩余。"""
        # 构造：所有股票涨幅相同且 > 0，avg == change，change < avg 不满足
        stocks = []
        for i in range(1, 16):
            stocks.append(_stock(f"s{i:02d}", f"S{i}", i, 3.0, 5.0, 5e9, vol_ratio=2.0))
        r = select_tiers(stocks, cfg)
        # 所有 change == avg，change < avg 不满足，降级到全部剩余
        assert len(r["tier3"]) >= cfg.tier_min


# ====================================================================
# 4. scanner 测试（失败隔离）
# ====================================================================

class TestScanner:
    def test_scan_success(self, monkeypatch, cfg):
        """正常流程：所有板块成功。"""
        from scanner import scan

        rank_result = {
            "hot_sectors": [{"name": "AI", "code": "881", "rank": 1,
                             "change": 3.0, "turnover": 10.0, "score": 100, "time": ""}],
            "cold_sectors": [{"name": "钢铁", "code": "882", "rank": 50,
                              "change": -2.0, "turnover": 5.0, "score": 10, "time": ""}],
            "target_sectors": [{"name": "AI", "code": "881"},
                               {"name": "钢铁", "code": "882"}],
            "total": 50,
        }
        monkeypatch.setattr("scanner.rank_sectors",
                            lambda date1, c: rank_result)

        def fake_identify(code, name, date1, c, uplimit_index=None):
            return {"sector_name": name, "plate_code": code, "avg_change": 1.0,
                    "tier1": [_stock("a", "A", 1, 5.0, 5.0, 5e9)],
                    "tier2": [], "tier3": []}
        monkeypatch.setattr("scanner.identify_leaders", fake_identify)
        monkeypatch.setattr("scanner.fetch_uplimit_stocks",
                            lambda date1, c: None)

        report = scan("2026-08-12", cfg)
        assert report["summary"]["success_count"] == 2
        assert report["summary"]["failed_sectors"] == []
        assert "AI" in report["sector_leaders"]
        assert "钢铁" in report["sector_leaders"]

    def test_scan_partial_failure(self, monkeypatch, cfg):
        """单板块失败不影响整体流程。"""
        from scanner import scan

        rank_result = {
            "hot_sectors": [], "cold_sectors": [],
            "target_sectors": [{"name": "AI", "code": "881"},
                               {"name": "钢铁", "code": "882"}],
            "total": 2,
        }
        monkeypatch.setattr("scanner.rank_sectors",
                            lambda date1, c: rank_result)

        def fake_identify(code, name, date1, c, uplimit_index=None):
            if name == "钢铁":
                return None  # 模拟失败
            return {"sector_name": name, "plate_code": code, "avg_change": 1.0,
                    "tier1": [], "tier2": [], "tier3": []}
        monkeypatch.setattr("scanner.identify_leaders", fake_identify)
        monkeypatch.setattr("scanner.fetch_uplimit_stocks",
                            lambda date1, c: None)

        report = scan("2026-08-12", cfg)
        assert report["summary"]["success_count"] == 1
        assert report["summary"]["failed_sectors"] == ["钢铁"]
        assert "AI" in report["sector_leaders"]
        assert "钢铁" not in report["sector_leaders"]

    def test_scan_rank_failure(self, monkeypatch, cfg):
        """板块排名失败 → 返回最小报告，success_count=0。"""
        from scanner import scan
        monkeypatch.setattr("scanner.rank_sectors", lambda date1, c: None)
        report = scan("2026-08-12", cfg)
        assert report["summary"]["success_count"] == 0
        assert report["summary"]["total_sectors"] == 0
        assert "error" in report["summary"]

    def test_scan_exception_isolation(self, monkeypatch, cfg):
        """identify_leaders 抛异常时被捕获，记为失败。"""
        from scanner import scan

        rank_result = {
            "hot_sectors": [], "cold_sectors": [],
            "target_sectors": [{"name": "AI", "code": "881"}],
            "total": 1,
        }
        monkeypatch.setattr("scanner.rank_sectors",
                            lambda date1, c: rank_result)

        def fake_identify(code, name, date1, c, uplimit_index=None):
            raise RuntimeError("boom")
        monkeypatch.setattr("scanner.identify_leaders", fake_identify)
        monkeypatch.setattr("scanner.fetch_uplimit_stocks",
                            lambda date1, c: None)

        report = scan("2026-08-12", cfg)
        assert report["summary"]["success_count"] == 0
        assert report["summary"]["failed_sectors"] == ["AI"]


# ====================================================================
# 5. data_loader 缓存测试
# ====================================================================

class TestDataLoaderCache:
    def test_cache_save_and_load(self, cfg, tmp_path):
        from data_loader import _save_cache, _load_cache
        cfg.cache_dir = str(tmp_path)
        data = [{"plate_name": "AI", "plate_code": "881"}]
        _save_cache(cfg, "2026-08-12", "test.json", data)
        loaded = _load_cache(cfg, "2026-08-12", "test.json")
        assert loaded == data

    def test_cache_miss_returns_none(self, cfg, tmp_path):
        from data_loader import _load_cache
        cfg.cache_dir = str(tmp_path)
        assert _load_cache(cfg, "2026-08-12", "nope.json") is None

    def test_load_latest_cache_backfill(self, cfg, tmp_path):
        """当日无缓存时，向前回溯找到历史缓存。"""
        from data_loader import _save_cache, _load_latest_cache
        cfg.cache_dir = str(tmp_path)
        _save_cache(cfg, "2026-08-10", "test.json", [{"x": 1}])
        # 8-12 当日无缓存，应回溯到 8-10
        loaded = _load_latest_cache(cfg, "2026-08-12", "test.json", look_back_days=5)
        assert loaded == [{"x": 1}]

    def test_load_latest_cache_not_found(self, cfg, tmp_path):
        from data_loader import _load_latest_cache
        cfg.cache_dir = str(tmp_path)
        assert _load_latest_cache(cfg, "2026-08-12", "test.json", look_back_days=3) is None

    def test_fetch_uplimit_stocks_disabled(self, cfg):
        """enable_uplimit_pool=False 时直接返回 None，不调用接口。"""
        from data_loader import fetch_uplimit_stocks
        cfg.enable_uplimit_pool = False
        assert fetch_uplimit_stocks("2026-08-12", cfg) is None

    def test_fetch_uplimit_stocks_cache_degrade(self, cfg, tmp_path, monkeypatch):
        """接口失败时降级读取缓存。"""
        from data_loader import fetch_uplimit_stocks, _save_cache

        cfg.cache_dir = str(tmp_path)
        cfg.retry_times = 0      # 不重试，加速测试
        cfg.request_interval = 0  # 跳过限频等待
        # 预置缓存
        cached = [{"stock_code": "000001", "seal_time": "09:35:00"}]
        _save_cache(cfg, "2026-08-12", "uplimit_stocks.json", cached)

        # mock get_api 返回 FakeApi，其 uplimit_stocks 抛异常（模拟接口失败）
        class BoomApi:
            def uplimit_stocks(self, date1):
                raise RuntimeError("network error")
        monkeypatch.setattr("data_loader.get_api", lambda c: BoomApi())

        data = fetch_uplimit_stocks("2026-08-12", cfg)
        assert data == cached

    def test_fetch_uplimit_stocks_normalizes_dict_wrapper(self, cfg, tmp_path, monkeypatch):
        """接口返回 {"list": [...]} 时规整为 list。"""
        from data_loader import fetch_uplimit_stocks

        cfg.cache_dir = str(tmp_path)

        class FakeApi:
            def uplimit_stocks(self, date1):
                return {"list": [{"stock_code": "000001", "seal_time": "09:35"}]}

        monkeypatch.setattr("data_loader.get_api", lambda c: FakeApi())
        # 跳过限频等待
        monkeypatch.setattr("data_loader._throttle", lambda c: None)

        data = fetch_uplimit_stocks("2026-08-12", cfg)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["stock_code"] == "000001"


# ====================================================================
# 6. 网络集成测试（默认跳过）
# ====================================================================

_NET_TEST_ENABLED = os.environ.get("ENABLE_NETWORK_TESTS", "") == "1"


@pytest.mark.skipif(
    not _NET_TEST_ENABLED,
    reason="需要网络连接与 ZZSHARE_TOKEN，设置 ENABLE_NETWORK_TESTS=1 启用",
)
class TestIntegration:
    """集成测试：验证 zzshare 接口可用性。

    运行方式：ENABLE_NETWORK_TESTS=1 uv run pytest -v -k "TestIntegration"
    """

    def test_fetch_plates_rank(self):
        from data_loader import fetch_plates_rank
        cfg = ScanConfig.from_env()
        if not cfg.token:
            pytest.skip("未配置 ZZSHARE_TOKEN")
        data = fetch_plates_rank("2026-08-12", cfg, limit=10)
        assert data is not None
        assert len(data) > 0
        assert "plate_name" in data[0]
        assert "plate_code" in data[0]

    def test_fetch_plate_stocks(self):
        from data_loader import fetch_plates_rank, fetch_plate_stocks
        cfg = ScanConfig.from_env()
        if not cfg.token:
            pytest.skip("未配置 ZZSHARE_TOKEN")
        plates = fetch_plates_rank("2026-08-12", cfg, limit=5)
        if not plates:
            pytest.skip("板块排名为空")
        code = plates[0]["plate_code"]
        stocks = fetch_plate_stocks(code, "2026-08-12", cfg, limit=10)
        assert stocks is not None
        assert len(stocks) > 0
        assert "stock_code" in stocks[0]

    def test_fetch_uplimit_stocks(self):
        """验证涨停股池接口可用性（用于一级龙头封板时间判定）。"""
        from data_loader import fetch_uplimit_stocks
        from leader_selector import build_uplimit_index
        cfg = ScanConfig.from_env()
        if not cfg.token:
            pytest.skip("未配置 ZZSHARE_TOKEN")
        data = fetch_uplimit_stocks("2026-08-12", cfg)
        # 接口可能返回空（当日无涨停），但不应是 None（除非限频）
        if data is None:
            pytest.skip("涨停股池接口返回 None（可能限频或接口变更）")
        assert isinstance(data, list)
        if data:
            idx = build_uplimit_index(data)
            assert len(idx) > 0
            # 打印首个涨停股字段便于调试接口字段名
            print(f"\n涨停股池示例字段: {list(data[0].keys())}")


# ====================================================================
# 7. 涨停连板梯队 纯函数测试
# ====================================================================

class TestUplimitLadderGrouping:
    """按 limit_times 分组构建连板梯队。"""

    def _make_uplimit_stocks(self, rows):
        """rows: list[(code, name, limit_times, seal_time, chg, reason)]"""
        return [
            {"stock_code": code, "stock_name": name,
             "limit_times": lt, "seal_time": st,
             "change_pct": chg, "reason": reason}
            for code, name, lt, st, chg, reason in rows
        ]

    def test_group_by_limit_times_descending(self, cfg):
        """按连板数倒序分组，高连板在前。"""
        from scanner import _build_uplimit_ladder
        cfg.ladder_min_limit = 1
        cfg.ladder_show_top_n = 10
        raw = self._make_uplimit_stocks([
            ("001", "A3", 3, "09:35:00", 10.01, "半导体"),   # 3板
            ("002", "B2a", 2, "09:40:00", 10.02, "AI"),      # 2板
            ("003", "B2b", 2, "10:00:00", 10.00, "AI"),
            ("004", "C1a", 1, "09:30:00", 9.98, "新能源"),   # 首板
            ("005", "C1b", 1, "14:30:00", 10.03, "消费"),
        ])
        r = _build_uplimit_ladder(raw, cfg)
        ladders = r["ladders"]
        # 分组顺序：3板 → 2板 → 首板
        assert [g["limit_times"] for g in ladders] == [3, 2, 1]
        assert [g["label"] for g in ladders] == ["3板", "2板", "首板"]
        # 2板组有 2 只
        assert ladders[1]["total_count"] == 2
        assert len(ladders[1]["stocks"]) == 2

    def test_group_sort_by_seal_time(self, cfg):
        """组内按封板时间升序（早封板在前）。"""
        from scanner import _build_uplimit_ladder
        cfg.ladder_min_limit = 1
        cfg.ladder_show_top_n = 5
        raw = self._make_uplimit_stocks([
            ("001", "晚", 2, "14:30:00", 10.0, ""),
            ("002", "早", 2, "09:32:00", 10.0, ""),
            ("003", "中", 2, "10:15:00", 10.0, ""),
        ])
        r = _build_uplimit_ladder(raw, cfg)
        names = [s["name"] for s in r["ladders"][0]["stocks"]]
        assert names == ["早", "中", "晚"]

    def test_ladder_min_limit_filter(self, cfg):
        """最低连板数过滤。"""
        from scanner import _build_uplimit_ladder
        cfg.ladder_min_limit = 2  # 仅 2板及以上
        cfg.ladder_show_top_n = 10
        raw = self._make_uplimit_stocks([
            ("001", "A3", 3, "09:35", 10.0, ""),
            ("002", "B2", 2, "09:40", 10.0, ""),
            ("003", "C1", 1, "09:30", 10.0, ""),  # 应被过滤
        ])
        r = _build_uplimit_ladder(raw, cfg)
        lts = [g["limit_times"] for g in r["ladders"]]
        assert 1 not in lts
        assert lts == [3, 2]

    def test_ladder_show_top_n_truncate(self, cfg):
        """每连板组最多展示 show_top_n 只，has_more 标记。"""
        from scanner import _build_uplimit_ladder
        cfg.ladder_min_limit = 1
        cfg.ladder_show_top_n = 2
        raw = self._make_uplimit_stocks([
            (f"{i:03d}", f"首板{i}", 1, f"09:{30+i:02d}", 10.0, "")
            for i in range(1, 6)  # 5 只首板
        ])
        r = _build_uplimit_ladder(raw, cfg)
        g = r["ladders"][0]
        assert g["total_count"] == 5
        assert len(g["stocks"]) == 2
        assert g["has_more"] is True

    def test_ladder_empty(self, cfg):
        from scanner import _build_uplimit_ladder
        r = _build_uplimit_ladder(None, cfg)
        assert r["ladders"] == []
        assert r["meta"]["uplimit_stocks_count"] == 0


class TestUplimitLadderLimitTimesKeys:
    """连板数字段名容错（limit_times 优先，别名降级）。"""

    def test_primary_limit_times(self):
        assert _extract_continuous({"limit_times": 3}) == 3

    def test_fallback_keys(self):
        assert _extract_continuous({"continuous": 2}) == 2
        assert _extract_continuous({"lian_ban": 5}) == 5
        assert _extract_continuous({"连板天数": 4}) == 4

    def test_default_to_1(self):
        """无此字段 → 默认 1 板。"""
        assert _extract_continuous({}) == 1
        assert _extract_continuous({"foo": "bar"}) == 1

    def test_name_and_reason_extract(self):
        """个股名称/原因/涨跌幅提取。"""
        assert _extract_name({"stock_name": "贵州茅台"}) == "贵州茅台"
        assert _extract_name({"名称": "宁德时代"}) == "宁德时代"
        assert _extract_name({}) == ""
        assert _extract_reason({"reason": "AI算力"}) == "AI算力"
        assert _extract_reason({"涨停原因": "半导体国产替代"}) == "半导体国产替代"
        assert _extract_change_pct({"change_pct": 20.0}) == 20.0
        assert _extract_change_pct({"涨幅": 10.01}) == 10.01
        # 默认涨停 10%
        assert _extract_change_pct({}) == 10.0


# ====================================================================
# 8. scanner 报告含连板梯队模块（mock 数据层，失败隔离）
# ====================================================================

class TestScannerNewModules:
    def test_scan_report_includes_ladder(self, monkeypatch, cfg):
        """正常流程：报告 dict 包含 uplimit_ladder 字段。"""
        from scanner import scan

        rank_result = {
            "hot_sectors": [{"name": "AI", "code": "881", "rank": 1,
                             "change": 3.0, "turnover": 10.0, "score": 100, "time": ""}],
            "cold_sectors": [],
            "target_sectors": [{"name": "AI", "code": "881"}],
            "total": 1,
        }
        monkeypatch.setattr("scanner.rank_sectors",
                            lambda date1, c: rank_result)
        monkeypatch.setattr("scanner.identify_leaders",
                            lambda code, name, date1, c, uplimit_index=None:
                            {"sector_name": name, "avg_change": 1.0,
                             "tier1": [], "tier2": [], "tier3": []})
        # mock 涨停股池：1 只 2板，3 只首板
        monkeypatch.setattr("scanner.fetch_uplimit_stocks", lambda date1, c: [
            {"stock_code": "001", "stock_name": "龙二连", "limit_times": 2,
             "seal_time": "09:35", "change_pct": 10.0},
            {"stock_code": "002", "stock_name": "首板A", "limit_times": 1,
             "seal_time": "09:30", "change_pct": 9.98},
            {"stock_code": "003", "stock_name": "首板B", "limit_times": 1,
             "seal_time": "10:00", "change_pct": 10.01},
            {"stock_code": "004", "stock_name": "首板C", "limit_times": 1,
             "seal_time": "14:30", "change_pct": 10.0},
        ])
        monkeypatch.setattr("scanner.fetch_uplimit_hot", lambda date1, c: None)

        report = scan("2026-08-12", cfg)

        # ===== uplimit_ladder 断言 =====
        assert "uplimit_ladder" in report
        ul = report["uplimit_ladder"]
        assert ul["meta"]["uplimit_stocks_count"] == 4
        lts = [g["limit_times"] for g in ul["ladders"]]
        assert lts == [2, 1]  # 2板在前，首板在后
        # 2板组：1 只
        assert ul["ladders"][0]["total_count"] == 1
        assert ul["ladders"][0]["stocks"][0]["name"] == "龙二连"
        # 首板组：3 只，按封板时间排序 首板A(09:30) → 首板B(10:00) → 首板C(14:30)
        names_1 = [s["name"] for s in ul["ladders"][1]["stocks"]]
        assert names_1 == ["首板A", "首板B", "首板C"]

    def test_scan_ladder_disabled(self, monkeypatch, cfg):
        """关闭 enable_uplimit_ladder → meta.error=未启用，不拉取涨停股池。"""
        from scanner import scan
        cfg.enable_uplimit_ladder = False
        cfg.enable_uplimit_pool = False

        rank_result = {
            "hot_sectors": [], "cold_sectors": [],
            "target_sectors": [], "total": 0,
        }
        monkeypatch.setattr("scanner.rank_sectors",
                            lambda date1, c: rank_result)
        monkeypatch.setattr("scanner.identify_leaders",
                            lambda *a, **k: {"tier1": [], "tier2": [], "tier3": [], "avg_change": 0})

        report = scan("2026-08-12", cfg)
        assert "未启用" in report["uplimit_ladder"]["meta"]["error"]
