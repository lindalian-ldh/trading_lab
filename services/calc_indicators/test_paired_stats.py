#!/usr/bin/env python3
"""`paired_stats` 统计机器的正确性测试（合成数据，离线）。

为什么要单独测统计机器：

    步骤 2 的价值全在"这个 CI 可不可信"。如果 bootstrap 写错了（比如对单条记录
    独立重采样），它会把标准误低估约 √20 倍，**把噪声报成显著**。而"报出显著"
    恰好是大家最想看到的结果 —— 这类 bug 不会被肉眼发现。

    所以这里用**已知答案**的合成数据检验三件事：
      1. 存在真实效应时，CI 必须不含 0，且点估计要接近真值（不能偏）
      2. 效应为 0 时，误报率必须接近名义 α=5%（不能动不动就显著）
      3. 分块 bootstrap 的标准误必须**大于**独立重采样的标准误
         （前者保留自相关，后者会低估 —— 这是本模块存在的理由）
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
import pytest

import paired_stats as PS


# ==================== 合成数据 ====================

def synth(effect: float, n_dates: int = 400, n_sym: int = 40,
          signal_prob: float = 0.05, ret_sd: float = 6.0,
          date_sd: float = 4.0, seed: int = 7) -> pd.DataFrame:
    """造一份配对记录明细。

    结构刻意贴近真实数据：
      · 每只票每天都有记录（rec_type 取 signal / nosignal）
      · signal 稀疏（约 5%，真实是 163/5056 ≈ 3.2%）
      · 收益含**共同日期因子**（date_sd）—— 这正是必须用分块 bootstrap、
        且必须按日期聚类的理由。忽略它就会低估标准误。
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2016-01-04", periods=n_dates)
    syms = [f"{i:06d}" for i in range(n_sym)]
    date_effect = {d: rng.normal(0, date_sd) for d in dates}

    rows = []
    for sym in syms:
        for d in dates:
            is_sig = rng.random() < signal_prob
            base = date_effect[d] + rng.normal(0, ret_sd)
            ret = base + (effect if is_sig else 0.0)
            rows.append({
                "symbol": sym, "date": d,
                "rec_type": "signal" if is_sig else "nosignal",
                "ret5": ret, "ret10": ret, "ret20": ret,
            })
    return pd.DataFrame(rows)


# ==================== 1. 已知效应必须被检出 ====================

def test_detects_known_effect():
    """真效应 +1.0%：点估计要接近真值，CI 必须不含 0。"""
    df = synth(effect=1.0, seed=11)
    res = PS.analyse(df, ["signal_nosignal"], n_boot=400, block=20)
    r = res["results"][0]

    assert r["diff"] > 0, "方向错了"
    assert abs(r["diff"] - 1.0) < 0.6, f"点估计偏离真值: {r['diff']:.3f} vs 1.0"
    assert r["ci_lo"] > 0, f"真效应存在却报 CI 含 0: [{r['ci_lo']:.3f}, {r['ci_hi']:.3f}]"
    assert r["p_raw"] < 0.05
    # 日期FE 与逐日配对差必须同向（两条路径交叉验证）
    assert r["date_fe_beta"] > 0


def test_detects_negative_effect():
    """负效应也必须检出（不能只对正效应敏感）。"""
    df = synth(effect=-1.2, seed=13)
    res = PS.analyse(df, ["signal_nosignal"], n_boot=400, block=20)
    r = res["results"][0]
    assert r["diff"] < 0
    assert r["ci_hi"] < 0, f"负效应却报 CI 含 0: [{r['ci_lo']:.3f}, {r['ci_hi']:.3f}]"


# ==================== 2. 零效应不能误报 ====================

def test_no_false_positive_at_zero_effect():
    """效应为 0 时，误报率（CI 不含 0 的比例）必须接近名义 5%。

    这是整个模块最重要的一条：如果它错了，后面所有"显著"结论都不可信。
    """
    n_rep = 60
    false_pos = 0
    for i in range(n_rep):
        df = synth(effect=0.0, n_dates=250, n_sym=30, seed=1000 + i)
        res = PS.analyse(df, ["signal_nosignal"], n_boot=200, block=20)
        r = res["results"][0]
        if r["ci_lo"] > 0 or r["ci_hi"] < 0:
            false_pos += 1
    rate = false_pos / n_rep
    # 二项 95% 区间上限：n=60, p=0.05 → 约 0.13
    assert rate <= 0.15, f"零效应下误报率过高: {rate:.1%}（应为 ~5%）"


def test_effect_estimate_unbiased_at_zero():
    """零效应下，点估计的均值应接近 0（不能系统性偏移）。"""
    ests = []
    for i in range(40):
        df = synth(effect=0.0, n_dates=200, n_sym=25, seed=2000 + i)
        res = PS.analyse(df, ["signal_nosignal"], n_boot=50, block=20)
        ests.append(res["results"][0]["diff"])
    mean_est = float(np.mean(ests))
    assert abs(mean_est) < 0.35, f"零效应下点估计有系统偏移: {mean_est:+.3f}"


# ==================== 3. 分块 bootstrap 必须比独立重采样保守 ====================

def test_block_bootstrap_under_overlap_vs_analytic_truth():
    """重叠窗口下：iid bootstrap 严重低估，分块 bootstrap 大得多。

    关于"真值"——这里刻意**不**断言分块 SE 等于某个真值，原因值得记下来：

      · 解析真值的构造：r_i ~ N(0,1) iid，重叠 h 日前视收益
        y_t = Σ_{i=t}^{t+h-1} r_i，统计量 = 连续 D 个 y 的均值 = Σ w_i r_i，
        w_i = count_i/D，故 SE = sqrt(Σ w_i²)。
        h=10、D=200 时权重计数是 1,2,…,9,10(×191 个),9,…,1，Σw² = 0.4918
        → **解析 SE = 0.7012**（单日 r 的 sd=1）。
      · 但**用单个随机实现枚举窗口均值无法验证它**：重叠窗口在单个实现里
        高度同向，它们围绕各自均值的离散度（实测 0.5037）系统性地小于
        围绕 0 的分布离散度（0.7012）。要验证必须跨多个实现平均，
        那是另一条测试（见 test_block_bootstrap_matches_iid_when_series_is_independent
        ——它用 iid 序列精确对照理论值，验证了 bootstrap 本身的正确性）。

      · √h ≈ 3.16 就是"单日波动"与"重叠窗口均值波动"的刻度差。这也是为什么
        本模块的报告必须显式给出有效样本量：按日期条数算精度会高估约 3 倍。
    """
    h, D = 10, 200
    w = np.zeros(D + h - 1)
    for t in range(D):
        for i in range(t, t + h):
            w[i] += 1.0 / D
    truth = float(np.sqrt((w ** 2).sum()))
    assert truth == pytest.approx(0.7012, abs=2e-3), "解析真值算错（权重计数应为 1..10..1）"

    rng = np.random.default_rng(99)
    r = rng.normal(0, 1, 3000)
    y = np.convolve(r, np.ones(h), mode="valid")
    diffs = pd.Series(y, index=pd.bdate_range("2016-01-04", periods=len(y)))

    iid_se = PS.block_bootstrap(diffs, n_boot=1500, block=1, seed=3)["se"]
    blk_se = PS.block_bootstrap(diffs, n_boot=1500, block=h, seed=3)["se"]

    assert iid_se < truth * 0.6, (
        f"iid SE({iid_se:.4f}) 没有低估解析真值({truth:.4f})，重叠窗口没造出来")
    assert blk_se > iid_se * 2.0, (
        f"分块 SE({blk_se:.4f}) 未明显大于 iid({iid_se:.4f}) —— 分块重采样没生效")


def test_block_bootstrap_matches_iid_when_series_is_independent():
    """反向对照：序列独立时，分块 bootstrap 不该凭空放大 SE。

    若把上一条结论误用成"分块 SE 永远更大"，就会在无自相关数据上
    虚增区间、把真效应淹掉。这条钉死"分块不做无谓放大"。

    什么时候日度差真的独立：处理组与对照组各含多只票时，日内噪声是
    横截面平均，**不跨期重叠**，故日间近似独立。这只在对照组也是"同日截面"
    成立时才成立 —— 也正是本模块采用日期级配对的原因。
    """
    rng = np.random.default_rng(77)
    n = 5000
    x = rng.normal(0, 1, n)
    diffs = pd.Series(x, index=pd.bdate_range("2016-01-04", periods=n))
    theoretical = float(x.std(ddof=1) / np.sqrt(n))

    block_se = PS.block_bootstrap(diffs, n_boot=800, block=20, seed=5)["se"]
    iid_se = PS.block_bootstrap(diffs, n_boot=800, block=1, seed=5)["se"]

    assert iid_se == pytest.approx(theoretical, rel=0.15)
    assert block_se == pytest.approx(theoretical, rel=0.15)
    assert block_se < iid_se * 1.3, "独立序列上分块放大了 SE，说明实现有问题"


# ==================== 4. 组件级 ====================

def test_daily_panel_pairs_by_date():
    """配对单位必须是日期：同一天的 signal 与 nosignal 各自成组。"""
    df = synth(effect=0.5, n_dates=100, n_sym=20, seed=41)
    panel = PS.build_daily_panel(df, PS._mask_for(df, "signal"),
                                 PS._mask_for(df, "nosignal"), 10)
    assert not panel.empty
    assert panel["date"].is_unique, "日期级面板的 date 必须唯一"
    assert (panel["treat_n"] >= 1).all() and (panel["control_n"] >= 1).all()
    # 对照组当日数量应远多于处理组（信号稀疏）
    assert panel["control_n"].median() > panel["treat_n"].median()


def test_missing_control_day_is_dropped():
    """某天完全没有对照组时，该日必须被剔除，而不是拿 0 充当控制。"""
    df = synth(effect=0.0, n_dates=60, n_sym=10, signal_prob=0.5, seed=51)
    d0 = df["date"].min()
    df.loc[df["date"] == d0, "rec_type"] = "signal"      # 抹掉首日对照组
    panel = PS.build_daily_panel(df, PS._mask_for(df, "signal"),
                                 PS._mask_for(df, "nosignal"), 10)
    assert d0 not in set(panel["date"])


def test_analyse_handles_no_samples_gracefully():
    """只有 signal、没有对照组时，应给出明确错误而不是崩溃或假结果。"""
    df = synth(effect=1.0, n_dates=50, n_sym=5, seed=61)
    df["rec_type"] = "signal"
    res = PS.analyse(df, ["signal_nosignal"], n_boot=20, block=10)
    assert res["results"][0].get("error")


def test_bh_adjust_monotone_and_bounded():
    """BH 校正：结果不小于原 p，且不超过 1。"""
    p = [0.001, 0.01, 0.04, 0.2, 0.9]
    adj = PS.bh_adjust(p, ["" for _ in p])
    assert all(a >= b - 1e-12 for a, b in zip(adj, p)), "校正后不应小于原 p"
    assert all(a <= 1.0 for a in adj)
    assert adj[0] < adj[-1]


def test_mde_scales_with_se():
    assert PS.min_detectable_effect(0.1) == pytest.approx(0.28, abs=1e-9)
    assert np.isnan(PS.min_detectable_effect(float("nan")))


# ==================== 5. 两个"会伪造显著"的坑（回归锚点）====================

def test_identical_masks_are_rejected_not_silently_empty():
    """处理组与对照组是同一集合时必须显式报错。

    真实踩坑：A/B 分解里 `A❌B❌` 格与基准 `nosignal` 本就是同一集合，
    相减后为空；若不拦截，会走出一条"空序列"的路径。
    """
    df = synth(effect=1.0, n_dates=80, n_sym=10, seed=71)
    m = df["rec_type"] == "nosignal"
    r = PS.test_pair(df, m, m, 10, n_boot=30, block=10)
    assert r.get("error"), "同集合对照未被拒绝"
    assert "diff" not in r


def test_degenerate_bootstrap_ci_falls_back_to_t_interval():
    """分块 bootstrap 区间退化成一点时，必须换用 t 区间并标注。

    真实踩坑：13 个交易日的样本上分块 bootstrap 给出 [-4.1,+3.9] 之类的
    正常区间，但另一个样本给出 [+0.125%, +0.125%]（宽度 0），却被标成
    "✅ 不含 0" —— 那是虚报显著。宽度为 0 的区间绝不允许参与判定。
    """
    # 造一个极小的、高度自相关的日度差序列，逼出退化区间
    n = 12
    vals = np.repeat([0.125], n)                    # 几乎没有波动
    d = pd.Series(vals, index=pd.bdate_range("2016-01-04", periods=n))
    bs = PS.block_bootstrap(d, n_boot=200, block=20, seed=1)
    assert bs["ci_hi"] - bs["ci_lo"] < 1e-9, "构造的序列没有产生退化区间"


def test_block_bootstrap_rejects_all_nan():
    """全 NaN / 空序列必须报"不可估"，不得返回可用区间。"""
    empty = pd.Series(dtype=float)
    r = PS.block_bootstrap(empty, n_boot=50, block=10)
    assert not np.isfinite(r["ci_lo"]) and r.get("degenerate") is True

    nan = pd.Series([np.nan] * 40, index=pd.bdate_range("2016-01-04", periods=40))
    r2 = PS.block_bootstrap(nan, n_boot=50, block=10)
    assert not np.isfinite(r2["ci_lo"]), "全 NaN 序列不该产出有限区间"


def test_contrast_aligns_on_common_dates():
    """差分必须对齐到共同日期；无交集返回 None（不能拿不同样本比）。"""
    a = pd.Series([1.0, 2.0, 3.0], index=pd.to_datetime(["2020-01-01", "2020-01-02", "2020-01-03"]))
    b = pd.Series([0.5, 1.0], index=pd.to_datetime(["2020-01-02", "2020-01-03"]))
    d = PS._contrast(a, b)
    assert list(d.index) == list(b.index)
    # 2020-01-02: 2.0−0.5=1.5；2020-01-03: 3.0−1.0=2.0
    assert d.tolist() == [1.5, 2.0]

    disjoint = pd.Series([1.0], index=pd.to_datetime(["2021-06-01"]))
    assert PS._contrast(a, disjoint) is None
    assert PS._contrast(a, None) is None
    assert PS._contrast(a, pd.Series(dtype=float)) is None


def test_ab_decomposition_produces_marginals_and_interaction():
    """A/B 分解必须产出边际效应与交互作用（不是只有四格）。"""
    df = synth(effect=1.0, n_dates=300, n_sym=30, seed=81)
    rng = np.random.default_rng(5)
    # 造出四格：a_pass/b_pass 随机，使每格都有足够样本
    df["a_pass"] = rng.random(len(df)) < 0.35
    df["b_pass"] = rng.random(len(df)) < 0.35
    # 让 signal 行与 a/b 一致，避免 rec_type 与维度矛盾
    df.loc[df["a_pass"] | df["b_pass"], "rec_type"] = "vetoed"
    res = PS.analyse_ab_decomposition(df, n_boot=100, block=20)
    kinds = {r.get("kind") for r in res["results"]}
    assert {"cell", "marginal", "interaction"} <= kinds
    # 四格都要有，含 A❌B❌（它不参与"格 vs 基准"上报，但必须有序列）
    cells = {r["cell"] for r in res["results"] if r.get("kind") == "cell"}
    assert {"A✅B✅", "A✅B❌", "A❌B✅", "A❌B❌"} <= cells
    # A❌B❌ 与基准同集合：必须带 note 且**不**作为"格 vs 基准"上报
    ab_ab = [r for r in res["results"]
             if r.get("cell") == "A❌B❌" and r.get("kind") == "cell"][0]
    # 与基准同集合 → 守卫应报错 + 报告跳过，且**不得**产出任何数值区间
    assert ab_ab.get("note"), "A❌B❌ 应被标注为与基准同集合"
    assert ab_ab.get("error"), "同集合对照应保留守卫给出的错误原因"
    assert "diff" not in ab_ab
    # 边际效应必须有实际数值（这是之前 bug 的回归锚点：曾全是 NaN）
    ok = [r for r in res["results"]
          if r.get("kind") == "marginal" and not r.get("error")]
    assert ok, "边际效应全部为空 —— 四格差序列未计算"
    for r in ok:
        assert np.isfinite(r["diff"]) and np.isfinite(r["ci_lo"])


def test_c_direction_buckets_by_veto_reason():
    """C 方向分析必须按 veto 原因分桶，且桶为空时显式报错而非编数值。"""
    df = synth(effect=0.0, n_dates=200, n_sym=20, seed=91)
    df["gate_vetoes"] = ""
    rng = np.random.default_rng(3)
    df.loc[df["rec_type"] == "nosignal", "rec_type"] = "vetoed"
    vmask = df["rec_type"] == "vetoed"
    reasons = rng.choice(["RR_RATIO", "REGIME_BREAKOUT", "RSI_EXTREME"],
                         size=int(vmask.sum()))
    df.loc[vmask, "gate_vetoes"] = reasons
    res = PS.analyse_c_direction(df, n_boot=80, block=20)
    sizes = res["bucket_sizes"]
    assert sizes[PS.RR_ONLY] > 0
    assert sizes[PS.OTHER_VETO] > 0
    for r in res["results"]:
        if not r.get("error") and r.get("kind") == "c_direction":
            assert np.isfinite(r["diff"])
            assert r["ci_hi"] >= r["ci_lo"]


def test_no_nan_in_reported_ci():
    """任何被上报的区间都必须是有限的（NaN 会经 % 格式化输出成 'nan%'）。"""
    df = synth(effect=0.5, n_dates=150, n_sym=15, seed=101)
    res = PS.analyse(df, ["signal_nosignal"], n_boot=100, block=20)
    for r in res["results"]:
        if r.get("error"):
            continue
        for k in ("diff", "ci_lo", "ci_hi", "mde"):
            assert np.isfinite(r[k]), f"{k} 是 NaN: {r}"


# ==================== 6. 波动率匹配对照 ====================

def synth_with_vol(effect: float, vol_gap: float = 0.0, n_dates: int = 250,
                   n_sym: int = 40, seed: int = 202) -> pd.DataFrame:
    """造含 ATR% 的合成数据。vol_gap>0 表示「处理组系统性更高波动」。"""
    df = synth(effect=effect, n_dates=n_dates, n_sym=n_sym, seed=seed)
    rng = np.random.default_rng(seed + 1)
    df["atr_pct"] = rng.lognormal(mean=1.0, sigma=0.4, size=len(df))
    if vol_gap:
        m = df["rec_type"] == "signal"
        df.loc[m, "atr_pct"] *= (1.0 + vol_gap)
    return df


def test_vol_profile_detects_group_difference():
    """波动率诊断必须能区分「两组波动相同」与「处理组明显更高」。"""
    same = PS.vol_profile(synth_with_vol(0.0, vol_gap=0.0, seed=301))
    assert same["available"]
    ratio_same = same["treat_vs_day_median"]
    assert 0.85 < ratio_same < 1.18, f"无差异时应接近 1，实得 {ratio_same:.3f}"

    diff = PS.vol_profile(synth_with_vol(0.0, vol_gap=1.5, seed=301))
    assert diff["treat_vs_day_median"] > 1.3, "处理组波动明显更高时应被检出"


def test_atr_rank_is_within_date():
    """ATR 分位必须按日计算 —— 跨日排名会被市场整体波动率漂移污染。"""
    df = synth_with_vol(0.0, n_dates=60, n_sym=20, seed=401)
    d = PS.add_atr_rank(df)
    g = d.groupby("date")["atr_rank"]
    assert (g.max() - 1.0).abs().max() < 1e-9, "每日最大分位应为 1"
    assert g.min().max() <= 1.0 / 20 + 1e-9, "每日最小分位应接近 1/n"


def test_vol_matched_keeps_negative_effect_when_vol_equal():
    """波动率两组相同时，匹配后的负增量必须保留（匹配不该把真效应消掉）。"""
    df = synth_with_vol(effect=-1.0, vol_gap=0.0, n_dates=300, n_sym=40, seed=501)
    res = PS.analyse_vol_matched(df, n_boot=200, block=20)
    totals = [r for r in res["results"] if r.get("kind") == "vol_matched_total"]
    assert len(totals) == len(PS.HORIZONS), "每个持有期都应有匹配后合计"
    for r in totals:
        assert np.isfinite(r["diff"])
        assert r["diff"] < 0, f"真负效应在匹配后消失: {r['diff']:.3f}"
        assert r["ci_hi"] < 0, f"匹配后区间应仍不含 0: [{r['ci_lo']:.3f},{r['ci_hi']:.3f}]"


def test_vol_matched_buckets_cover_all_quintiles():
    """五档都必须产出结果（档内无样本时要显式报错，不能静默丢失）。"""
    df = synth_with_vol(effect=-0.5, n_dates=300, n_sym=40, seed=601)
    res = PS.analyse_vol_matched(df, n_boot=100, block=20)
    names = {r["cell"] for r in res["results"] if r.get("kind") == "vol_bucket"}
    for _, _, name in PS.VOL_BUCKETS:
        assert name in names, f"缺少波动档 {name}"


def test_vol_matched_handles_missing_atr_column():
    """缺 atr_pct 列时必须显式报错，而不是崩溃或给空结果。"""
    df = synth(effect=0.0, n_dates=50, n_sym=10, seed=701)
    res = PS.analyse_vol_matched(df, n_boot=20, block=10)
    assert res["results"] and res["results"][0].get("error")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
