#!/usr/bin/env python3
"""配对检验：回答「入场时机（A/B 组合 + 门控）相对不筛选有没有增量」。

为什么需要这个模块（而不是继续看 `backtest.py` 的分组均值）：

    池化均值无法回答这个问题，因为信号日与对照日的**日期构成不同**：
    信号天然集中在急跌/超卖的日子，而对照铺满全部交易日。两组的差异里混着
    "日子不同"和"信号不同"两件事 —— 报告的 §2（`A✅B✅` 胜率 43.1% 反而最差）
    极可能就是这种辛普森悖论。**逐日配对**把日期固定住，只留信号这一个差异。

输入：`backtest.py --paired --out xxx.csv` 产出的记录明细，每行一根 K 线，带：
    symbol, date, rec_type, close, a_pass, b_pass, c_pass, rr_ratio, regime,
    mfe, mae, atr_pct, gate_vetoes, n_vetoes, b_*, ret5/ret10/ret20

对照口径（**预注册**，只有这两个，避免多重比较膨胀）：
    signal vs nosignal  —— 主判据："有信号"相对"自然交易日"有没有增量
    signal vs vetoed    —— 次判据：门控拦掉的那批是否更差（注意 vetoed 内混有三种否决，
                           无法归因到单条规则，故只作参考）

统计方法（两条独立路径，互为交叉验证）：
    方法A 逐日配对差 t 检验 —— 直白、稳健，主力口径
    方法B 双向固定效应（个股 + 日期）+ 日期聚类稳健标准误 —— 消除"哪只票"和"哪天"

区间估计：**日期分块 bootstrap**（块长默认 20 交易日，对应 20 日重叠窗口）。
    绝不对单条记录做独立 bootstrap：同一日期/同一区间内的收益高度相关，
    独立重采样会把标准误低估约 √20 倍，把噪声报成显著。

用法：
    uv run python services/calc_indicators/paired_stats.py \
        --in data/reports/paired/paired_300x2016.csv
    uv run python services/calc_indicators/paired_stats.py --in x.csv --compare signal_vetoed --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HORIZONS = (5, 10, 20)
# 双边交易成本（%）：A股双边约 0.1~0.2%。判据看"扣成本后"。
ROUND_TRIP_COSTS = (0.0, 0.1, 0.2)
MIN_TRADING_DAYS = 5          # 某只票参与比较所需的最少交易日


# ==================== 数据准备 ====================

def load_records(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, keep_default_na=False, na_values=[""])
    need = {"symbol", "date", "rec_type"} | {f"ret{h}" for h in HORIZONS}
    missing = need - set(df.columns)
    if missing:
        raise SystemExit(f"❌ 输入缺少列: {sorted(missing)}\n"
                         f"   请用 backtest.py --paired --out 生成")
    for h in HORIZONS:
        df[f"ret{h}"] = pd.to_numeric(df[f"ret{h}"], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    return df


def build_daily_panel(df: pd.DataFrame, treat_mask, control_mask, horizon: int,
                      min_per_side: int = 1, min_days: int = 20,
                      label: str = "") -> pd.DataFrame:
    """构造**日期级**配对面板：同一交易日里"处理组那批票"vs"对照组那批票"。

    为什么配对单位是日期而不是 (symbol, date)：
        同一只票在同一天**不可能**既被判为处理组又被判为对照组。
        若按 (symbol,date) 配对，cell 恒为空 —— 那是把"配对"理解错了。

        正确的问题形式是："在同一个交易日，符合处理条件的那批票，
        与同期不符合的票相比，谁的表现更好"。日期把市场环境固定住
        （消掉 beta/择时混杂），票的差异就是信号的增量。

    treat_mask / control_mask: 布尔 Series（与 df 同索引），由 _mask_for 生成。

    Returns: DataFrame[date, treat_mean, treat_n, control_mean, control_n]
             已丢弃任一侧样本不足的交易日（丢弃量显式报出）。
    """
    col = f"ret{horizon}"
    sub = df.loc[treat_mask | control_mask, ["date", col]].copy()
    sub["_grp"] = np.where(treat_mask.reindex(sub.index).to_numpy(), "treat", "control")
    sub = sub.dropna(subset=[col])
    if sub.empty:
        return pd.DataFrame(columns=["date", "treat_mean", "treat_n",
                                     "control_mean", "control_n"])
    g = sub.groupby(["date", "_grp"])[col].agg(["mean", "size"]).unstack("_grp")
    if ("mean", "treat") not in g.columns or ("mean", "control") not in g.columns:
        return pd.DataFrame(columns=["date", "treat_mean", "treat_n",
                                     "control_mean", "control_n"])
    out = pd.DataFrame({
        "date": g.index,
        "treat_mean": g[("mean", "treat")].to_numpy(),
        "treat_n": g[("size", "treat")].fillna(0).to_numpy(),
        "control_mean": g[("mean", "control")].to_numpy(),
        "control_n": g[("size", "control")].fillna(0).to_numpy(),
    })
    n0 = len(out)
    out = out[(out["treat_n"] >= min_per_side) & (out["control_n"] >= min_per_side)]
    out = out.dropna(subset=["treat_mean", "control_mean"]).reset_index(drop=True)
    if len(out) < n0:
        print(f"  \u2139\ufe0f {label or ''}{horizon}日口径：剔除 {n0 - len(out)} 个单边为空的交易日")
    if len(out) < min_days:
        return out
    return out


def _mask_for(df: pd.DataFrame, rec_type) -> pd.Series:
    """把 rec_type 规格（字符串 或 字符串列表=并集）转成布尔 Series。"""
    types = [rec_type] if isinstance(rec_type, str) else list(rec_type)
    return df["rec_type"].isin(types)


def diff_series(df: pd.DataFrame, treat, control, horizon: int):
    """给定两组规格，返回"逐日配对差序列"（无样本则返回空 Series）。

    无条件计算、不判断是否"有意义"——调用方（A/B 分解的边际效应、交互作用）
    需要把四格**全部**先算成序列，再在共同日期上做差分。
    """
    tm = treat if isinstance(treat, pd.Series) else _mask_for(df, treat)
    cm = control if isinstance(control, pd.Series) else _mask_for(df, control)
    tm = tm.reindex(df.index).fillna(False).astype(bool)
    cm = cm.reindex(df.index).fillna(False).astype(bool) & ~tm
    panel = build_daily_panel(df, tm, cm, horizon)
    return daily_differences(panel)


def test_pair(df: pd.DataFrame, treat, control, horizon: int, n_boot: int,
              block: int, label: str = "") -> dict:
    """对给定的处理组/对照组规格跑一次完整配对检验，返回结果行。

    treat/control 可以是 rec_type 名，也可以是布尔 Series（A/B 分解用）。
    """
    tm = treat if isinstance(treat, pd.Series) else _mask_for(df, treat)
    cm = control if isinstance(control, pd.Series) else _mask_for(df, control)
    tm = tm.reindex(df.index).fillna(False).astype(bool)
    cm = cm.reindex(df.index).fillna(False).astype(bool)
    # 处理组优先：同一行不能同时属于两边
    cm = cm & ~tm

    # 结构守卫：掩码完全相同（或重叠后一侧为空）→ 无法配对，必须显式报错。
    # 典型场景：A/B 分解里 "A❌B❌" 与基准 "nosignal" 本就是同一个集合。
    if not tm.any() or not (cm & ~tm).any():
        return {"horizon": horizon,
                "error": f"处理组与对照组同一集合或一侧为空（treat={int(tm.sum())} 行）"}

    panel = build_daily_panel(df, tm, cm, horizon, label=label)
    if panel.empty:
        return {"horizon": horizon, "error": "无可用配对样本"}
    diffs = daily_differences(panel)
    bs = block_bootstrap(diffs, n_boot=n_boot, block=block)
    t = welch_t(diffs)
    fe = reg_date_fe(df, tm, cm, horizon)
    ac = autocorr_diagnostics(diffs, block)

    # 退化守卫：样本太小时分块 bootstrap 的区间会缩成一个点（ci_lo==ci_hi），
    # 那会被误读成"精确且显著"。此时改用日期聚类 t 区间，并显式标注来源，
    # 绝不允许"宽度为 0 的区间"参与显著性判定。
    degenerate = bool(
        np.isfinite(bs.get("ci_lo", np.nan)) and np.isfinite(bs.get("ci_hi", np.nan))
        and abs(bs["ci_hi"] - bs["ci_lo"]) < 1e-9)
    ci_src = "block_bootstrap"
    if degenerate or not np.isfinite(bs.get("ci_lo", np.nan)):
        bs["ci_lo"], bs["ci_hi"] = fe["ci_lo"], fe["ci_hi"]
        ci_src = "date_fe_t（分块 bootstrap 退化，样本不足）"
    return {
        "horizon": horizon,
        "ci_source": ci_src,
        "ci_degenerate": degenerate,
        "treat_mean": float(panel["treat_mean"].mean()),
        "control_mean": float(panel["control_mean"].mean()),
        "n_days": int(len(panel)),
        "treat_n_total": int(panel["treat_n"].sum()),
        "control_n_total": int(panel["control_n"].sum()),
        "treat_n_med": float(panel["treat_n"].median()),
        "control_n_med": float(panel["control_n"].median()),
        "diff": bs["mean"], "ci_lo": bs["ci_lo"], "ci_hi": bs["ci_hi"], "boot_se": bs["se"],
        "t_stat": t["t"], "p_raw": t["p"], "daily_se": t["se"], "daily_sd": t["sd"],
        "date_fe_beta": fe["beta"], "date_fe_se": fe["se"], "date_fe_p": fe["p"],
        "mde": min_detectable_effect(t["se"]),
        "lag1_autocorr": ac["lag1"], "n_eff": ac["n_eff"],
        "win_rate_treat": float((panel["treat_mean"] > 0).mean() * 100),
        "win_rate_control": float((panel["control_mean"] > 0).mean() * 100),
    }


# ==================== 方法 A：逐日配对差 ====================

def daily_differences(panel: pd.DataFrame, treat: str = "treat_mean",
                      control: str = "control_mean") -> pd.Series:
    """日期级配对差序列（= 当日两组均值之差），按日期排序。"""
    if panel.empty:
        return pd.Series(dtype=float, name="diff")
    d = panel.copy()
    d["diff"] = d[treat] - d[control]
    return d.set_index("date")["diff"].sort_index()


def block_bootstrap(diffs: pd.Series, n_boot: int = 1000, block: int = 20,
                    seed: int = 20260928) -> dict:
    """日期分块 bootstrap：对**连续 block 个交易日**重采样，保留窗口内自相关。

    返回点估计、百分位区间、以及 bootstrap 分布的标准误。
    """
    x = np.asarray(diffs, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    # 关键守卫：空序列或全 NaN 必须报"不可估"，
    # 否则会把 NaN 序列的退化区间(ci_lo==ci_hi)当成"显著"报出去。
    if n < 3:
        return {"mean": np.nan, "ci_lo": np.nan, "ci_hi": np.nan, "se": np.nan,
                "n_days": n, "degenerate": True}
    block = max(1, min(block, n))
    n_blocks = int(np.ceil(n / block))
    starts_max = n - block
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot)
    for b in range(n_boot):
        starts = rng.integers(0, starts_max + 1, size=n_blocks)
        sample = np.concatenate([x[s:s + block] for s in starts])[:n * block]
        means[b] = sample.mean()
    return {
        "mean": float(x.mean()),
        "ci_lo": float(np.percentile(means, 2.5)),
        "ci_hi": float(np.percentile(means, 97.5)),
        "se": float(means.std(ddof=1)),
        "n_days": int(n),
        "block": int(block),
        "n_boot": int(n_boot),
    }


def welch_t(diffs: pd.Series) -> dict:
    """逐日配对差的单样本 t 检验 + 正态近似 p（不引入 scipy 依赖）。"""
    from math import erf, sqrt
    x = np.asarray(diffs, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 3:
        return {"t": np.nan, "p": np.nan, "se": np.nan, "sd": np.nan}
    sd = x.std(ddof=1)
    se = sd / sqrt(n)
    if se == 0:
        return {"t": np.nan, "p": np.nan, "se": 0.0, "sd": sd}
    t = x.mean() / se
    p = 2 * (1 - 0.5 * (1 + erf(abs(t) / sqrt(2))))
    return {"t": float(t), "p": float(p), "se": float(se), "sd": float(sd)}


# ==================== 方法 B：双向固定效应（日期聚类稳健）====================

def reg_date_fe(df: pd.DataFrame, treat, control, horizon: int) -> dict:
    """把两组堆成长表，跑 y ~ 1{treat} + 日期固定效应，标准误按**日期聚类**。

    与方法A 的关系：这个 beta 就是"逐日配对差"（按当日样本量加权），
    同时给出按日期聚类的正确标准误。两条路径一致 → 日级聚合无问题；
    不一致 → 说明各交易日的样本量差异在起作用，需要查（故两个都报）。

    聚类必须按**日期**：同一天的截面收益高度相关（共同市场因子），
    按行聚类会把标准误低估一个数量级，把噪声报成显著。

    用 FWL 手工实现，避免引入 statsmodels 依赖：
      ỹ = y − 当日均值，x̃ = x − 当日均值（日期 within 变换）
      beta = Σ(x̃ỹ)/Σ(x̃²)，e = ỹ − beta·x̃
      Var(beta) = CR1 × Σ_g(Σ_{i∈g} x̃_i e_i)² / (Σx̃²)²
    """
    col = f"ret{horizon}"
    tm = treat if isinstance(treat, pd.Series) else _mask_for(df, treat)
    cm = control if isinstance(control, pd.Series) else _mask_for(df, control)
    tm = tm.reindex(df.index).fillna(False).astype(bool)
    cm = cm.reindex(df.index).fillna(False).astype(bool) & ~tm
    sub = df.loc[tm | cm, ["date", col]].copy()
    sub["_t"] = tm.reindex(sub.index).to_numpy().astype(float)
    sub = sub.dropna(subset=[col])
    if sub.empty:
        return {"beta": np.nan, "se": np.nan, "p": np.nan, "n_obs": 0, "n_dates": 0}
    y = sub[col].to_numpy(dtype=float)
    x = sub["_t"].to_numpy(dtype=float)
    dat = sub["date"].to_numpy()
    if len(np.unique(x)) < 2 or sub["date"].nunique() < 3:
        return {"beta": np.nan, "se": np.nan, "p": np.nan,
                "n_obs": int(len(sub)), "n_dates": int(sub["date"].nunique())}

    ys, xs = pd.Series(y), pd.Series(x)
    yt = y - ys.groupby(dat).transform("mean").to_numpy()
    xt = x - xs.groupby(dat).transform("mean").to_numpy()
    sxx = float((xt * xt).sum())
    if sxx <= 0:
        return {"beta": np.nan, "se": np.nan, "p": np.nan,
                "n_obs": int(len(sub)), "n_dates": int(sub["date"].nunique())}
    beta = float((xt * yt).sum() / sxx)
    resid = yt - beta * xt

    meat = pd.Series(xt * resid).groupby(dat).sum().to_numpy()
    G, N = len(meat), len(sub)
    K = sub["date"].nunique() + 1
    corr = (G / max(G - 1, 1)) * ((N - 1) / max(N - K, 1))
    var = corr * float((meat ** 2).sum()) / (sxx ** 2)
    se = float(np.sqrt(var)) if var > 0 else np.nan
    from math import erf, sqrt
    t = beta / se if se and se > 0 else np.nan
    p = (2 * (1 - 0.5 * (1 + erf(abs(t) / sqrt(2))))) if np.isfinite(t) else np.nan
    return {"beta": beta, "se": se, "t": float(t) if np.isfinite(t) else np.nan,
            "p": float(p) if np.isfinite(p) else np.nan,
            "ci_lo": beta - 1.96 * se if np.isfinite(se) else np.nan,
            "ci_hi": beta + 1.96 * se if np.isfinite(se) else np.nan,
            "n_obs": int(N), "n_dates": int(G)}



# ==================== 功效 ====================

def min_detectable_effect(se: float, power_z: float = 2.80) -> float:
    """可检出的最小效应（%）= 2.80 × SE（α=0.05 双侧 + 80% 功效）。

    用途：把"不显著"翻译成"这个样本本来就不可能检出多小的效应"，
    避免把"没测出来"误读成"不存在"。
    """
    return float(power_z * se) if np.isfinite(se) else float("nan")


def autocorr_diagnostics(diffs: pd.Series, block: int) -> dict:
    """日度差的自相关诊断 —— 用于判断 block 长度是否合适（不改变任何结论）。

    为什么必须报出来：分块 bootstrap 的块长决定区间宽度。
      · 若日度差**无**自相关 → 分块会给过宽的区间（结论偏保守，但不会误报显著）
      · 若日度差**强**自相关且块长太短 → 会低估 SE，把噪声报成显著（危险方向）

    所以这里给出 lag1 自相关与"有效独立样本数"（D / (1+2Σρ) 的 AR(1) 近似），
    让读者能自行判断 block=20 是偏保守还是偏激进。
    有效样本量同时也是"按日期条数估精度会高估多少倍"的度量：
    重叠前视窗口会让它显著小于实际交易日数。
    """
    x = diffs.to_numpy(dtype=float)
    n = len(x)
    if n < 10:
        return {"lag1": np.nan, "n_days": n, "n_eff": np.nan, "block": block}
    xc = x - x.mean()
    denom = float((xc * xc).sum())
    lag1 = float((xc[:-1] * xc[1:]).sum() / denom) if denom > 0 else np.nan
    # AR(1) 近似：n_eff = n / (1 + 2ρ/(1−ρ))
    if np.isfinite(lag1) and -1 < lag1 < 1:
        rho = abs(lag1)
        n_eff = n * (1 - rho) / (1 + rho)
    else:
        n_eff = np.nan
    return {"lag1": lag1, "n_days": int(n), "n_eff": float(n_eff), "block": int(block)}


def bh_adjust(pvals: list, labels: list) -> list:
    """Benjamini-Hochberg FDR 校正（多重比较）。返回校正后 p 值（原顺序）。"""
    items = [(p, i) for i, p in enumerate(pvals) if np.isfinite(p)]
    m = len(items)
    out = [np.nan] * len(pvals)
    if m == 0:
        return out
    items.sort()
    prev = 1.0
    for rank, (p, idx) in enumerate(reversed(items), 1):
        k = m - rank + 1
        val = min(prev, p * m / k)
        out[idx] = val
        prev = val
    return out


# ==================== 报告 ====================

COMPARES = {
    "signal_nosignal": ("signal", "nosignal",
                        "有信号 vs 自然交易日（主判据）"),
    "signal_vetoed": ("signal", "vetoed",
                      "有信号 vs 被门控拦掉（参考：vetoed 内混有三种否决）"),
}


def _meta(df: pd.DataFrame, n_boot: int, block: int) -> dict:
    return {
        "rows": int(len(df)),
        "symbols": int(df["symbol"].nunique()),
        "dates": int(df["date"].nunique()),
        "date_min": str(df["date"].min().date()),
        "date_max": str(df["date"].max().date()),
        "rec_type_counts": {k: int(v) for k, v in df["rec_type"].value_counts().items()},
        "n_boot": n_boot, "block": block,
    }


def _contrast(a, b):
    """两条"逐日差序列"在**共同日期**上相减；任一侧为空/无交集则返回 None。

    必须对齐到共同日期再相减：否则会把"只有 A 有数据的日期"当成差值，
    等价于拿两个不同样本比 —— 正是本模块要消灭的错误。
    """
    if a is None or b is None or len(a) == 0 or len(b) == 0:
        return None
    common = a.index.intersection(b.index)
    if len(common) == 0:
        return None
    d = (a.reindex(common) - b.reindex(common)).dropna()
    return d if len(d) else None


def _apply_bh(results: list, key: str = "p_raw") -> None:
    """对所有结果行做 BH 校正，写回 p_bh；并补上扣成本后的区间。"""
    adj = bh_adjust([r.get(key, np.nan) for r in results], ["" for _ in results])
    for r, a in zip(results, adj):
        r["p_bh"] = a
        if np.isfinite(r.get("diff", np.nan)):
            r["ci_after_cost"] = {f"{c:.1f}": [r["ci_lo"] - c, r["ci_hi"] - c]
                                  for c in ROUND_TRIP_COSTS}


def analyse(df: pd.DataFrame, compares: list, n_boot: int, block: int) -> dict:
    out = {"meta": _meta(df, n_boot, block), "results": []}
    for name in compares:
        treat, control, label = COMPARES[name]
        for h in HORIZONS:
            row = {"compare": name, "label": label, **test_pair(
                df, treat, control, h, n_boot, block, label=f"{name}/")}
            out["results"].append(row)
    _apply_bh(out["results"])
    return out


# ==================== A / B 的 2×2 分解 ====================

# 分解的对照基准：nosignal（当日 A、B 都没过的票）。
# 为什么不用 signal 当基准：signal 要求 C 也过 + 门控放行，会把 C 与门控的影响
# 混进"A/B 的增量"里。nosignal 只要求 A、B 都没过，是最干净的"无 A/B 信号"对照。
AB_BASE = "nosignal"


def analyse_ab_decomposition(df: pd.DataFrame, n_boot: int, block: int,
                             base: str = AB_BASE) -> dict:
    """A/B 的 2×2 因子分解 —— 回答"A、B 各自有没有增量，A✅B✅ 是不是真的最差"。

    设计（预注册）：
      · 基准 = `nosignal`（A、B 都没通过）。处理组限定在 `a_pass or b_pass`
        的样本内，切四格：A✅B✅ / A✅B❌ / A❌B✅ / A❌B❌。
        （A❌B❌ 不可能是 signal，只是"其他日子"，四格不代表全部时间）
      · 边际效应（这才是"A 的增量"）：
          A | B=❌  = (A✅B❌) − (A❌B❌)     ← 在 B 未通过时，A 通过带来的增量
          A | B=✅  = (A✅B✅) − (A❌B✅)     ← 在 B 通过时，A 的增量
          B | A=❌ / B | A=✅ 同理
        边际效应**不需要重新取数**：它就是把两个格子的逐日差再做一次同日相减。
      · 交互作用 = (A✅B✅ − A❌B✅) − (A✅B❌ − A❌B❌)。
        显著为负 ⇒ "两个条件都亮反而更差"确实存在，且不是日期混杂造成的。

    注意：`A✅B✅` 若为负，可能是**共线性**（RSI超卖与回踩常在同一根触发，
    同一件事被数了两遍），而不是 A/B 本身有害 —— 报告另给 B 子信号分解。
    """
    a = df["a_pass"].astype(bool)
    b = df["b_pass"].astype(bool)
    cells = {
        "A✅B✅": a & b,
        "A✅B❌": a & ~b,
        "A❌B✅": ~a & b,
        "A❌B❌": ~a & ~b,
    }
    base_mask = _mask_for(df, base)
    results = []
    for h in HORIZONS:
        rows = {}
        for name, mask in cells.items():
            r = test_pair(df, mask, base_mask, h, n_boot, block, label=f"{name}/")
            r.update({"cell": name, "horizon": h, "kind": "cell",
                      "label": f"{name} vs {base}"})
            if name == "A❌B❌":
                # 它**就是**基准 nosignal 本身（A、B 都没过），拿它和基准比是自我比较。
                # 必须登记进 results（保证四格齐全、可审计），但：
                #   · 不带任何数值（test_pair 的守卫会给 error，不要覆盖）
                #   · 报告层用 note 跳过它
                r = {**r, "note": "与基准同集合，不作「格 vs 基准」报告"}
            results.append(r)
            rows[name] = r
        # 四格的逐日差序列（相对基准的增量）。
        # 关键恒等式：**A❌B❌ 就是基准 nosignal 本身**（A、B 都没过）。
        # 所以：
        #   · 它不能"自己 vs 自己"（自我比较，守卫会拒绝并返回空序列）；
        #   · 它相对基准的增量**恒等于 0**——在下面的差分之差里，base−base 项自动消失，
        #     所以直接记为 None 即可，不需要也不应该去构造它。
        base_series = None
        diffs = {}
        for name, mask in cells.items():
            if name == "A❌B❌":
                diffs[name] = None          # = base − base ≡ 0
                continue
            diffs[name] = diff_series(df, mask, base_mask, h)
        # 边际效应 = "相对基准的增量"之差。
        #   A|B✅ = (A✅B✅ − base) − (A❌B✅ − base)   ← 与两格直接相减恒等
        #   B|A✅ = (A✅B✅ − base) − (A✅B❌ − base)
        #   含 A❌B❌ 的两个边际效应，其 A❌B❌ 项 ≡ 0，故退化为单格相对基准的增量。
        # 这样做避开了"两格从不在同一天出现"导致的空交集（实测：A✅B✅ 与 A❌B✅
        # 按构造永不同日，2 只票的样本上直接相减必然为空）。
        contrasts = {
            "A | B=❌": ("A✅B❌", "A❌B❌"),
            "A | B=✅": ("A✅B✅", "A❌B✅"),
            "B | A=❌": ("A❌B✅", "A❌B❌"),
            "B | A=✅": ("A✅B✅", "A✅B❌"),
        }
        for cname, (x, y) in contrasts.items():
            if x == "A❌B❌" or y == "A❌B❌":
                # 缺失项视为 0 → 边际效应 = 另一格相对基准的增量
                other = x if y == "A❌B❌" else y
                d = diffs.get(other)
            else:
                d = _contrast(diffs.get(x), diffs.get(y))
            if d is None or len(d) < 3:
                results.append({"cell": cname, "horizon": h, "kind": "marginal",
                                "label": cname,
                                "error": ("两格从不在同一天出现，无法做同日对比"
                                          if d is None else "构成格样本不足")})
                continue
            bs = block_bootstrap(d, n_boot=n_boot, block=block)
            t = welch_t(d)
            results.append({
                "cell": cname, "horizon": h, "kind": "marginal", "label": cname,
                "diff": bs["mean"], "ci_lo": bs["ci_lo"], "ci_hi": bs["ci_hi"],
                "t_stat": t["t"], "p_raw": t["p"], "daily_se": t["se"],
                "mde": min_detectable_effect(t["se"]),
                "n_days": int(len(d)),
                "lag1_autocorr": autocorr_diagnostics(d, block)["lag1"],
            })
        # 交互作用
        # 交互作用 = A 的增量(B=✅) − A 的增量(B=❌)。
        # A❌B❌ 项 ≡ 0（它就是基准），故 A 的增量(B=❌) 退化为 A✅B❌ 相对基准的增量。
        d1 = _contrast(diffs.get("A✅B✅"), diffs.get("A❌B✅"))
        d2 = diffs.get("A✅B❌")
        d = _contrast(d1, d2)
        if d is not None and len(d) >= 3:
            bs = block_bootstrap(d, n_boot=n_boot, block=block)
            t = welch_t(d)
            results.append({
                "cell": "交互作用 (A×B)", "horizon": h, "kind": "interaction",
                "label": "交互作用 (A×B)",
                "diff": bs["mean"], "ci_lo": bs["ci_lo"], "ci_hi": bs["ci_hi"],
                "t_stat": t["t"], "p_raw": t["p"], "daily_se": t["se"],
                "mde": min_detectable_effect(t["se"]), "n_days": int(len(d)),
            })
        else:
            results.append({"cell": "交互作用 (A×B)", "horizon": h,
                            "kind": "interaction", "label": "交互作用 (A×B)",
                            "error": "构成格样本不足"})
        # B 子信号分解（回答"是不是 RSI超卖 在拖累 B"）
        for flag in ("b_macd_cross", "b_rsi_oversold", "b_vol_surge", "b_macd_red"):
            if flag not in df.columns:
                continue
            m = df[flag].astype(str).str.lower().isin(["true", "1"])
            r = test_pair(df, m, base_mask, h, n_boot, block, label=f"{flag}/")
            r.update({"cell": flag, "horizon": h, "kind": "b_subsignal",
                      "label": f"{flag} vs {base}"})
            results.append(r)
    _apply_bh([r for r in results if not r.get("error")])
    return {"meta": _meta(df, n_boot, block), "base": base, "results": results}


# ==================== 量比阈值扫描 ====================

VOL_THRESHOLDS = (1.5, 1.8, 2.0, 2.3, 2.6, 3.0)


def vol_ratio_profile(df: pd.DataFrame) -> dict:
    """量比的分布诊断（决定"阈值扫描"有没有空间）。

    实测教训：signal 的量比最大值约 2.85、P99 约 2.54。
    所以把 VOLUME_SURGE_RATIO 提到 3.0 会让"放量触发"的信号**归零** ——
    不是"过滤掉差信号"，而是"没有信号"。
    """
    if "vol_ratio" not in df.columns:
        return {"available": False, "reason": "缺少 vol_ratio 列（需重跑 backtest 生成）"}
    out = {"available": True, "by_type": {}}
    for t in ("signal", "vetoed", "nosignal"):
        v = pd.to_numeric(df.loc[df["rec_type"] == t, "vol_ratio"], errors="coerce").dropna()
        if v.empty:
            continue
        out["by_type"][t] = {
            "n": int(len(v)), "median": float(v.median()), "mean": float(v.mean()),
            "p90": float(v.quantile(.90)), "p99": float(v.quantile(.99)),
            "max": float(v.max()),
        }
    return out


def analyse_vol_thresholds(df: pd.DataFrame, n_boot: int, block: int,
                           control: str = "nosignal!") -> dict:
    """扫描放量阈值 + 检验量比在信号内部有无预测力。

    两个问法必须区分（这是本函数存在的理由）：
      A. 提高 VOLUME_SURGE_RATIO → 改变"哪些 bar 算信号"。阈值一高，
         放量触发源失效 → 信号量骤降。回答的是"这个触发源该不该留"。
      B. 在**已成立的信号内部**量比高低的收益差异（单调性）。
         回答的是"放量是利好还是利空"——这才是"改阈值能否改善"的前提。
         若 B 显示高量比更差，那"提高阈值"必然更差；反之才值得调阈值。

    对照组口径：默认用「当日 nosignal」作为基准（与主判据一致）。
    """
    if "vol_ratio" not in df.columns:
        return {"meta": _meta(df, n_boot, block),
                "results": [{"error": "缺少 vol_ratio 列，需先重跑 backtest --paired"}]}
    d = df.copy()
    d["vol_ratio"] = pd.to_numeric(d["vol_ratio"], errors="coerce")
    base = _mask_for(d, "nosignal")
    results = []

    # —— A. 阈值扫描 ——
    for thr in VOL_THRESHOLDS:
        m = d["rec_type"].eq("signal") & (d["vol_ratio"] > thr)
        for h in HORIZONS:
            if int(m.sum()) < 20:
                results.append({"cell": f"阈值>{thr}", "horizon": h, "kind": "vol_threshold",
                                "n_rows": int(m.sum()), "error": "样本不足（<20 行）"})
                continue
            r = test_pair(d, m, base, h, n_boot, block, label=f"vol>{thr}/")
            r.update({"cell": f"阈值>{thr}", "horizon": h, "kind": "vol_threshold",
                      "threshold": thr, "n_rows": int(m.sum())})
            results.append(r)

    # —— B. 信号内部的量比分档（单调性检验）——
    sig = d["rec_type"].eq("signal") & d["vol_ratio"].notna()
    if int(sig.sum()) >= 100:
        vr = d.loc[sig, "vol_ratio"]
        # 按信号集内的量比分位切 4 档，保证每档样本量相近
        qs = [vr.quantile(q) for q in (0.0, 0.25, 0.5, 0.75, 1.0)]
        for k in range(4):
            lo, hi = qs[k], qs[k + 1]
            m = sig & (d["vol_ratio"] >= lo) & (d["vol_ratio"] <= hi if k == 3
                                                else d["vol_ratio"] < hi)
            for h in HORIZONS:
                if int(m.sum()) < 20:
                    continue
                r = test_pair(d, m, base, h, n_boot, block, label=f"vol档{k+1}/")
                r.update({"cell": f"信号内量比Q{k+1} [{lo:.2f},{hi:.2f}]", "horizon": h,
                          "kind": "vol_within_signal", "n_rows": int(m.sum()),
                          "vol_lo": float(lo), "vol_hi": float(hi)})
                results.append(r)
    _apply_bh([r for r in results if not r.get("error")])
    return {"meta": _meta(d, n_boot, block),
            "vol_profile": vol_ratio_profile(d), "results": results}


# ==================== 波动率匹配对照 ====================

VOL_BUCKETS = ((0.0, 0.2, "Q1 最低波"), (0.2, 0.4, "Q2"),
               (0.4, 0.6, "Q3"), (0.6, 0.8, "Q4"), (0.8, 1.01, "Q5 最高波"))


def add_atr_rank(df: pd.DataFrame, col: str = "atr_pct",
                 out: str = "atr_rank") -> pd.DataFrame:
    """给每行加"当日内 ATR% 横截面分位"。

    必须**按日**排名：ATR% 的绝对水平随市场整体波动率大幅漂移
    （2018/2022 高、2017/2019 低），跨日比较没有意义。
    """
    d = df.copy()
    d[out] = d.groupby("date")[col].rank(pct=True)
    return d


def vol_profile(df: pd.DataFrame, treat: str = "signal",
                control: str = "nosignal") -> dict:
    """诊断：处理组与对照组的波动率是否本就不同（决定匹配有没有必要）。

    这一步必须**先做**：如果两组的 ATR% 分布本就几乎重合，那"用波动率解释
    组间差异"就是错的，匹配也就无从"消除"什么。
    """
    if "atr_pct" not in df.columns:
        return {"available": False, "reason": "缺少 atr_pct 列"}
    d = df.dropna(subset=["atr_pct"]).copy()
    d["_rank"] = d.groupby("date")["atr_pct"].rank(pct=True)
    out = {"available": True}
    for name, t in (("treat", treat), ("control", control)):
        sub = d[d["rec_type"] == t]
        out[name] = {
            "rec_type": t,
            "atr_median": float(sub["atr_pct"].median()),
            "atr_mean": float(sub["atr_pct"].mean()),
            "rank_median": float(sub["_rank"].median()),
            "n": int(len(sub)),
        }
    day_med = d.groupby("date")["atr_pct"].median()
    tsub = d[d["rec_type"] == treat].join(day_med.rename("_day_med"), on="date")
    out["treat_vs_day_median"] = float((tsub["atr_pct"] / tsub["_day_med"]).median())
    return out


def analyse_vol_matched(df: pd.DataFrame, n_boot: int, block: int,
                        treat: str = "signal", control: str = "nosignal") -> dict:
    """波动率匹配后的配对检验 + 分档结果。

    做法：按"当日内 ATR% 横截面分位"切 5 档，**在每档内部**做同日配对比较
    （信号票 vs 同档的对照票）。若负增量在每档内部依然显著，则它不是波动率造成的。

    同时报一张"增量 × 波动档"表 —— 这比单一匹配数字更有信息量：
      · 若负增量集中在高档而低档为 0 → 说明是波动率效应（但前提是两组波动分布不同）
      · 若各档都负 → 是信号本身的问题
    """
    if "atr_pct" not in df.columns:
        return {"meta": _meta(df, n_boot, block),
                "results": [{"error": "缺少 atr_pct 列，无法做波动率匹配"}]}
    d = df.dropna(subset=["atr_pct"]).copy()
    d["atr_rank"] = d.groupby("date")["atr_pct"].rank(pct=True)

    tm_all = _mask_for(d, treat)
    cm_all = _mask_for(d, control)
    results = []
    for h in HORIZONS:
        matched = {}
        for lo, hi, name in VOL_BUCKETS:
            in_bucket = (d["atr_rank"] >= lo) & (d["atr_rank"] < hi)
            tm = tm_all & in_bucket
            cm = cm_all & in_bucket
            if int(tm.sum()) == 0:
                results.append({"cell": name, "horizon": h, "kind": "vol_bucket",
                                "label": f"{treat} vs {control} @ {name}",
                                "n_rows": 0, "error": "该波动档内无处理组样本"})
                continue
            r = test_pair(d, tm, cm, h, n_boot, block, label=f"{name}/")
            r.update({"cell": name, "horizon": h, "kind": "vol_bucket",
                      "label": f"{treat} vs {control} @ {name}",
                      "n_rows": int(tm.sum()), "n_ctrl_rows": int(cm.sum())})
            results.append(r)
            matched[name] = r
        # 全档合计：把所有档的当日均值按样本量加权（等价于"只保留有匹配的日子"）
        parts = []
        for lo, hi, name in VOL_BUCKETS:
            r = matched.get(name)
            if not r or r.get("error"):
                continue
            in_bucket = (d["atr_rank"] >= lo) & (d["atr_rank"] < hi)
            ser = diff_series(d, tm_all & in_bucket, cm_all & in_bucket, h)
            if len(ser):
                parts.append(ser)
        if parts:
            pooled = pd.concat(parts).groupby(level=0).mean().sort_index()
            bs = block_bootstrap(pooled, n_boot=n_boot, block=block)
            t = welch_t(pooled)
            results.append({
                "cell": "匹配后合计", "horizon": h, "kind": "vol_matched_total",
                "label": f"{treat} vs {control}（波动率匹配）",
                "diff": bs["mean"], "ci_lo": bs["ci_lo"], "ci_hi": bs["ci_hi"],
                "t_stat": t["t"], "p_raw": t["p"], "daily_se": t["se"],
                "mde": min_detectable_effect(t["se"]), "n_days": int(len(pooled)),
                "lag1_autocorr": autocorr_diagnostics(pooled, block)["lag1"],
            })
    _apply_bh([r for r in results if not r.get("error")])
    return {"meta": _meta(df, n_boot, block),
            "vol_profile": vol_profile(df, treat, control),
            "results": results}


# ==================== C 方向：按门控否决原因切分 ====================

RR_ONLY = "否决原因=仅RR_RATIO"
RR_INCL = "否决原因=含RR_RATIO"
OTHER_VETO = "否决原因=非RR（REGIME/RSI）"


def analyse_c_direction(df: pd.DataFrame, n_boot: int, block: int,
                        treat: str = "signal") -> dict:
    """C 的方向：把 `vetoed` 按否决原因切开，回答"赔率门槛拦掉的是不是更差"。

    ⚠️ 这是**方向性参考，不是 C 的净增量**。三个必须随结果一起读的保留：
      1. C 不通过 ⇒ 必然触发 RR_RATIO 否决（main.py check_entry_gate ③），
         所以 `仅RR_RATIO` 这一桶**本质上就是 C❌**。测的是"RR 门槛拦掉的那批"，
         而不是"C 作为独立维度的贡献"。
      2. 门控是否决制的：只要 C❌ 就出局，**不存在"C❌ 但门控放行"的样本**。
         因此对照是"选择过的"，不是随机对照。
      3. 该桶里 A 或 B 至少有一个通过（否则归 nosignal），所以它同时受了 A/B 的影响。
      干净的 C 隔离需要给门控加独立开关（ENTRY_GATE_RR_RATIO_ENABLED）后重跑，
      属后续工作；本函数只用来**定方向**。
    """
    if "gate_vetoes" not in df.columns:
        return {"meta": _meta(df, n_boot, block),
                "results": [{"label": "缺少 gate_vetoes 列", "error": "无法切分"}]}
    v = df["gate_vetoes"].fillna("").astype(str)
    is_vetoed = df["rec_type"] == "vetoed"
    has_rr = v.str.contains("RR_RATIO")
    masks = {
        RR_ONLY: is_vetoed & has_rr & ~v.str.contains("REGIME_BREAKOUT") & ~v.str.contains("RSI_EXTREME"),
        RR_INCL: is_vetoed & has_rr,
        OTHER_VETO: is_vetoed & ~has_rr,
    }
    treat_mask = _mask_for(df, treat)
    results = []
    for h in HORIZONS:
        for name, mask in masks.items():
            n = int(mask.sum())
            if n == 0:
                results.append({"cell": name, "horizon": h, "kind": "c_direction",
                                "label": f"{name} vs {treat}", "n_rows": 0,
                                "error": "该桶为空（本样本无此类否决）"})
                continue
            r = test_pair(df, mask, treat_mask, h, n_boot, block, label=f"{name}/")
            r.update({"cell": name, "horizon": h, "kind": "c_direction",
                      "label": f"{name} vs {treat}", "n_rows": n})
            results.append(r)
    _apply_bh([r for r in results if not r.get("error")])
    return {"meta": _meta(df, n_boot, block),
            "bucket_sizes": {k: int(m.sum()) for k, m in masks.items()},
            "results": results}


def print_report(res: dict) -> None:
    m = res["meta"]
    print("=" * 108)
    print("配对检验：入场时机（A/B 组合 + 门控）相对不筛选有没有增量")
    print("=" * 108)
    print(f"  样本: {m['rows']:,} 行  {m['symbols']} 只标的  {m['dates']:,} 个交易日  "
          f"({m['date_min']} ~ {m['date_max']})")
    print(f"  构成: {m['rec_type_counts']}")
    print("  配对单位: **交易日**（同日比较「有信号的那批票」vs「没信号的那批票」）")
    print(f"  区间估计: 日期分块 bootstrap（块长 {m['block']} 日 × {m['n_boot']} 次）")
    print()

    for r in res["results"]:
        if r.get("error"):
            print(f"\n【{r['label']}｜{r['horizon']}日】{r['error']}")
            continue
        print(f"\n【{r['label']}｜持有 {r['horizon']} 日】")
        print(f"  有效交易日 {r['n_days']:,} 个   "
              f"（每日处理组中位 {r['treat_n_med']:.0f} 只 / 对照组中位 {r['control_n_med']:.0f} 只）")
        print(f"  处理组均值 {r['treat_mean']:+.3f}%   对照组均值 {r['control_mean']:+.3f}%   "
              f"胜率 {r['win_rate_treat']:.1f}% vs {r['win_rate_control']:.1f}%")
        print(f"  ── 增量（逐日配对差）──────────────────────────────────")
        print(f"    点估计          {r['diff']:+.3f}%")
        print(f"    95% CI          [{r['ci_lo']:+.3f}%, {r['ci_hi']:+.3f}%]"
          f"   来源: {r.get('ci_source', 'block_bootstrap')}")
        sig = "✅ 不含 0" if (r['ci_lo'] > 0 or r['ci_hi'] < 0) else "❌ 含 0（无法排除无效应）"
        print(f"    显著性          {sig}    t={r['t_stat']:.2f}  p={r['p_raw']:.3f}  "
              f"BH校正后 p={r['p_bh']:.3f}")
        print(f"    日期FE交叉验证  beta={r['date_fe_beta']:+.3f}%  日期聚类SE={r['date_fe_se']:.3f}  "
              f"p={r['date_fe_p']:.3f}")
        print(f"    可检出最小效应  ±{r['mde']:.3f}%（本样本功效下限）")
        if np.isfinite(r.get("lag1_autocorr", np.nan)):
            print(f"    日度差自相关    lag1={r['lag1_autocorr']:+.3f}  "
                  f"有效独立样本={r['n_eff']:.0f}（实际 {r['n_days']} 个交易日）")
        ac = r["ci_after_cost"]
        print(f"    扣成本后 CI     0.0%: [{ac['0.0'][0]:+.3f}, {ac['0.0'][1]:+.3f}]   "
              f"0.2%: [{ac['0.2'][0]:+.3f}, {ac['0.2'][1]:+.3f}]")
    print()
    print("=" * 108)
    print("读法（先看这条）：")
    print("  · '可检出最小效应' > 0.25% → 本样本**不足以**对系统声称的 +0.25% 量级下结论；")
    print("    '不显著'不等于'无效'，只等于'没测出来'。")
    print("  · 只有 CI 含 0 且 MDE 已 ≤0.25%，才有资格说'入场系统没有可测出的增量'。")
    print("=" * 108)



def _fmt_test(r: dict, indent: str = "    ") -> str:
    """把一条检验结果格式化成一行（点估计 + CI + 显著性标记）。"""
    if r.get("error"):
        return f"{indent}{r.get('cell', r.get('label','?')):<20s} —  {r['error']}"
    star = ""
    if np.isfinite(r.get("ci_lo", np.nan)):
        if r["ci_lo"] > 0:
            star = "  ✅+"
        elif r["ci_hi"] < 0:
            star = "  ✅−"
        else:
            star = "  ❌含0"
    p_bh = r.get("p_bh", np.nan)
    p_s = f"{p_bh:.3f}" if np.isfinite(p_bh) else "—"
    mde = r.get("mde", np.nan)
    mde_s = f"±{mde:.2f}%" if np.isfinite(mde) else "—"
    return (f"{indent}{r.get('cell', r.get('label','?')):<20s}"
            f"{r['diff']:>+8.3f}%  [{r['ci_lo']:>+7.3f}, {r['ci_hi']:>+7.3f}]"
            f"  p(BH)={p_s:>6s}  MDE={mde_s:>7s}  n日={r.get('n_days', 0):>4d}{star}")


def print_ab_report(res: dict) -> None:
    base = res.get("base", AB_BASE)
    print()
    print("=" * 108)
    print(f"A / B 的 2×2 分解（全部对照 = rec_type「{base}」）")
    print("=" * 108)
    print("读法：这不是「A 单独好不好」，而是「在 A、B 都参与判断的前提下，各自的边际贡献」。")
    print("      四格 vs 基准 = 该组合的增量；边际效应 = 同层内两格之差；交互作用 = 差分再差分。")
    for h in HORIZONS:
        rows = [r for r in res["results"] if r.get("horizon") == h]
        cells = [r for r in rows if r.get("kind") == "cell" and not r.get("note")]
        marg = [r for r in rows if r.get("kind") == "marginal"]
        inter = [r for r in rows if r.get("kind") == "interaction"]
        subs = [r for r in rows if r.get("kind") == "b_subsignal"]
        print(f"\n【持有 {h} 日】")
        print("  ── 四格 vs 基准 ──────────────────────────────────────────────────")
        for r in cells:
            print(_fmt_test(r))
        print("  ── 边际效应（这才是「A/B 各自的增量」）───────────────────────────")
        for r in marg:
            print(_fmt_test(r))
        for r in inter:
            print("  ── 交互作用 ────────────────────────────────────────────────────")
            print(_fmt_test(r))
        print("  ── B 的子信号（检验「是不是 RSI超卖 在拖累 B」）───────────────────")
        for r in subs:
            print(_fmt_test(r))
    print()
    print("  ⚠️ 多重比较：本节共 " + str(len([r for r in res["results"] if not r.get("error")]))
          + " 个检验，p 已做 BH 校正（p(BH)）。看校正后的值，不要看原始 p。")
    print("=" * 108)


def print_c_report(res: dict) -> None:
    print()
    print("=" * 108)
    print("C 的方向：按门控否决原因切分「被拦掉的那批」")
    print("=" * 108)
    if res.get("bucket_sizes"):
        print(f"  桶大小（全样本行数）: {res['bucket_sizes']}")
    print()
    print("  ⚠️ 这是方向性参考，**不是 C 的净增量**。必须连带读这三条保留：")
    print("     1. C 不过 ⇒ 必然触发 RR_RATIO 否决，故「仅RR_RATIO」桶本质上就是 C❌；")
    print("        测的是「RR 门槛拦掉的那批」而非 C 的独立贡献。")
    print("     2. 门控是否决制的 —— 不存在「C❌ 但门控放行」的样本，对照是被选择过的。")
    print("     3. 该桶内 A 或 B 至少一个通过，故结果同时受了 A/B 的影响。")
    print("     结论方向（正/负）可用；要定量 C 需给门控加独立开关后重跑。")
    for h in HORIZONS:
        rows = [r for r in res["results"] if r.get("horizon") == h]
        print(f"\n【持有 {h} 日】")
        for r in rows:
            print(_fmt_test(r))
    print("=" * 108)


def print_vol_threshold_report(res: dict) -> None:
    print()
    print("=" * 108)
    print("量比阈值扫描：提高 VOLUME_SURGE_RATIO 能不能把放量信号救回来？")
    print("=" * 108)
    vp = res.get("vol_profile", {})
    if not vp.get("available"):
        print(f"  ⚠️ {vp.get('reason', '无诊断信息')}")
        for r in res.get("results", []):
            if r.get("error"):
                print(f"  {r['error']}"); break
        print("=" * 108); return
    print("  先看有没有扫描空间（signal 的量比上界决定了阈值能提多高）：")
    for t, st in vp["by_type"].items():
        print(f"    {t:<10} n={st['n']:>7,}  中位 {st['median']:.3f}  "
              f"P90 {st['p90']:.3f}  P99 {st['p99']:.3f}  **最大 {st['max']:.3f}**")
    print()
    for h in HORIZONS:
        rows = [r for r in res["results"] if r.get("horizon") == h]
        thr = [r for r in rows if r.get("kind") == "vol_threshold"]
        within = [r for r in rows if r.get("kind") == "vol_within_signal"]
        print(f"\n【持有 {h} 日】")
        print("  ── A. 阈值扫描：signal 限定为 vol_ratio > X（对照 = nosignal）──")
        for r in thr:
            print(_fmt_test(r))
        if within:
            print("  ── B. 信号内部按量比分档（检验单调性：量比越高越好还是越差）──")
            for r in within:
                print(_fmt_test(r))
    print()
    print("  ⚠️ 读法：若 A 里阈值一提高样本就崩溃（见上表「最大」值），说明这条路本身走不通；")
    print("     若 B 显示高档更差 → 提高阈值必然更差；只有 B 单调向上才值得调阈值。")
    print("=" * 108)


def print_vol_report(res: dict) -> None:
    print()
    print("=" * 108)
    print("波动率匹配对照：负增量是不是「高波动票」造成的？")
    print("=" * 108)
    vp = res.get("vol_profile", {})
    if vp.get("available"):
        t, c = vp["treat"], vp["control"]
        print(f"  先看前提（两组波动率是否本就不同）：")
        print(f"    {t['rec_type']:<10} ATR% 中位 {t['atr_median']:.3f}%   "
              f"当日横截面分位中位 {t['rank_median']:.3f}   n={t['n']:,}")
        print(f"    {c['rec_type']:<10} ATR% 中位 {c['atr_median']:.3f}%   "
              f"当日横截面分位中位 {c['rank_median']:.3f}   n={c['n']:,}")
        print(f"    处理组 / 当日中位 = {vp['treat_vs_day_median']:.3f}x"
              f"   （接近 1.0 说明处理组波动与当日中位相当，匹配无从「消除」差异）")
    print()
    print("  做法：按当日内 ATR% 横截面分位切 5 档，在各档**内部**做同日配对比较。")
    for h in HORIZONS:
        rows = [r for r in res["results"] if r.get("horizon") == h]
        buckets = [r for r in rows if r.get("kind") == "vol_bucket"]
        totals = [r for r in rows if r.get("kind") == "vol_matched_total"]
        print(f"\n【持有 {h} 日】")
        for r in buckets:
            print(_fmt_test(r))
        for r in totals:
            print("  " + "-" * 70)
            print(_fmt_test(r))
    print()
    print("  读法：若「匹配后合计」仍显著为负，且各档内部都负，则负增量不是波动率造成的。")
    print("=" * 108)


def _parse_args(argv):
    ap = argparse.ArgumentParser(description="配对检验：入场时机的增量贡献")
    ap.add_argument("--in", dest="infile", required=True, help="backtest.py --paired --out 的 CSV")
    ap.add_argument("--compare", default="signal_nosignal,signal_vetoed",
                    help=f"逗号分隔，可选: {','.join(COMPARES)}")
    ap.add_argument("--n-boot", type=int, default=1000, help="bootstrap 次数（默认 1000）")
    ap.add_argument("--block", type=int, default=20, help="bootstrap 块长（交易日，默认 20）")
    ap.add_argument("--json", default="", help="结果 JSON 输出路径")
    ap.add_argument("--ab", action="store_true",
                    help="附：A/B 的 2×2 分解（含边际效应、交互作用、B 子信号）")
    ap.add_argument("--c-direction", action="store_true",
                    help="附：C 的方向（按门控否决原因切分，仅定方向）")
    ap.add_argument("--vol-threshold", action="store_true",
                    help="附：量比阈值扫描 + 信号内量比单调性（需 CSV 含 vol_ratio 列）")
    ap.add_argument("--vol-match", action="store_true",
                    help="附：波动率匹配对照（按当日 ATR% 分档，检验负增量是否为波动率假象）")
    ap.add_argument("--all", action="store_true",
                    help="等价于 --ab --c-direction --vol-match --vol-threshold")
    return ap.parse_args(argv)


def main() -> int:
    args = _parse_args(sys.argv[1:])
    compares = [c.strip() for c in args.compare.split(",") if c.strip()]
    bad = [c for c in compares if c not in COMPARES]
    if bad:
        print(f"❌ 未知对照: {bad}；可选 {list(COMPARES)}")
        return 1

    df = load_records(args.infile)
    payload = {}
    res = analyse(df, compares, args.n_boot, args.block)
    print_report(res)
    payload["main"] = res

    if args.ab or args.all:
        ab = analyse_ab_decomposition(df, args.n_boot, args.block)
        print_ab_report(ab)
        payload["ab_decomposition"] = ab
    if args.c_direction or args.all:
        cd = analyse_c_direction(df, args.n_boot, args.block)
        print_c_report(cd)
        payload["c_direction"] = cd
    if args.vol_threshold or args.all:
        vt = analyse_vol_thresholds(df, args.n_boot, args.block)
        print_vol_threshold_report(vt)
        payload["vol_thresholds"] = vt
    if args.vol_match or args.all:
        vm = analyse_vol_matched(df, args.n_boot, args.block)
        print_vol_report(vm)
        payload["vol_matched"] = vm

    if args.json:
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"结果已写入: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
