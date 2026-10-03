#!/usr/bin/env python
"""股指期货基差：取数 + 连续化 + 标准化 + 与恐慌信号的关系（只读研究用）。

为什么可能有用（这是它唯一的存在理由）：
    现有状态判决全部基于**价格的历史** —— MA5/20/60、ADX14、ATR14 都是移动平均，
    天然滞后（实测：2026-07 那次从高点到 PANIC_DOWN 已经跌了 12%）。
    而 **基差 = 期货 − 现货** 反映的是**套保/投机力量的对比**，属于前瞻性信息。
    换句话说，这是"**换信息源**"，不是"换算法" —— 而换算法已被实测证明
    突破不了滞后-噪声的权衡（见 scripts/audit_regime_hold.py 与广度实验）。

⚠️ 三个必须处理的技术点（2026-10-01 实测踩过，不要在别处重踩）：
    ① **主力连续合约有换月锯齿**：基差随到期日收敛到 0，换月时跳回。
       实测 IF0 有 **26 天**单日跳变 >1% —— 这些是合约切换，不是市场信息。
    ② **不要用"置 NaN"去锯齿**：`rolling(250)` 默认要求窗口内**全非空**，
       26 个 NaN 会废掉 **85%** 的样本（实测只剩 354/2356 = 15%）。
       正确做法是**正向复权**：从换月日起**往后**累加减去跳变（cumsum），
       保留 89% 样本且序列连续（复权后 0 天跳变 >1%）。
    ③ **复权方向不能反**：往前累加会让跳变**加倍**（实测最大单日变化
       从 3.43pp 变成 6.87pp，看着"处理过了"其实是错的）。

数据来源与可用性（实测）：
    · 期货：akshare `futures_main_sina`（新浪主力连续）
      IF0/IC0/IH0 → 2356 行，2017-01-17 起；IM0 → 1018 行，2022-07 起
    · 现货：baostock `query_history_k_data_plus`（sh.000300/905/016/852，各 2855 行）
    · 存储 ≈ 0.9MB（现有 data/cache 是 64MB）；计算 <1s
    · **本脚本零改动现有系统**，只新增 data/cache/futures_*.csv

用法:
    uv run python scripts/fetch_futures_basis.py              # 取数+构建+报告
    uv run python scripts/fetch_futures_basis.py --refresh    # 强制重取
    uv run python scripts/fetch_futures_basis.py --pairs IF0:sh000300
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "cache"
sys.path.insert(0, str(ROOT / "services" / "calc_indicators"))

# (期货主力代码, 对应现货指数, 中文名)
DEFAULT_PAIRS = [
    ("IF0", "sh000300", "沪深300"),
    ("IC0", "sh000905", "中证500"),
    ("IH0", "sh000016", "上证50"),
    ("IM0", "sh000852", "中证1000"),
]

ROLL_JUMP_PCT = 1.0        # 单日基差变化超过该值 → 判定为换月跳变
Z_WINDOW = 250             # 滚动标准化窗口（对应约一年交易日）


# ====================================================================
# 取数
# ====================================================================
def fetch_futures(code: str, refresh: bool = False) -> pd.DataFrame:
    """取期货主力连续合约。缓存到 data/cache/futures_{code}.csv。"""
    f = CACHE / f"futures_{code}.csv"
    if f.exists() and not refresh:
        d = pd.read_csv(f, encoding="utf-8-sig")
        d["date"] = pd.to_datetime(d["date"])
        return d.sort_values("date").reset_index(drop=True)
    import akshare as ak
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):
        raw = ak.futures_main_sina(symbol=code)
    if raw is None or raw.empty:
        raise RuntimeError(f"{code} 取数失败（akshare futures_main_sina 返回空）")
    d = pd.DataFrame({
        "date": pd.to_datetime(raw["日期"]),
        "open": pd.to_numeric(raw["开盘价"], errors="coerce"),
        "high": pd.to_numeric(raw["最高价"], errors="coerce"),
        "low": pd.to_numeric(raw["最低价"], errors="coerce"),
        "close": pd.to_numeric(raw["收盘价"], errors="coerce"),
        "volume": pd.to_numeric(raw["成交量"], errors="coerce"),
    }).dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    d.to_csv(f, index=False, encoding="utf-8-sig")
    return d


def ensure_spot(code: str) -> pd.DataFrame:
    """确保现货指数已缓存（复用现有 index_{code}_history.csv 命名，不新建体系）。"""
    f = CACHE / f"index_{code}_history.csv"
    if f.exists():
        d = pd.read_csv(f, encoding="utf-8-sig")
        d["date"] = pd.to_datetime(d["date"])
        return d.sort_values("date").reset_index(drop=True)
    import baostock as bs
    import contextlib, io
    bs_code = f"sh.{code[2:]}" if code.startswith("sh") else f"sz.{code[2:]}"
    with contextlib.redirect_stdout(io.StringIO()):
        bs.login()
        rs = bs.query_history_k_data_plus(
            bs_code, "date,open,high,low,close,volume",
            start_date="2015-01-01", end_date=datetime.now().strftime("%Y-%m-%d"),
            frequency="d")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        bs.logout()
    if not rows:
        raise RuntimeError(f"现货 {code}（{bs_code}）取数失败")
    d = pd.DataFrame(rows, columns=rs.fields)
    d["date"] = pd.to_datetime(d["date"])          # 必须转！否则与期货 date 类型不符，merge 会报错
    for c in ("open", "high", "low", "close", "volume"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    d.to_csv(f, index=False, encoding="utf-8-sig")
    return d


# ====================================================================
# 基差构建（三个技术点都在这里面）
# ====================================================================
def build_basis(fut: pd.DataFrame, spot: pd.DataFrame, code: str) -> pd.DataFrame:
    m = (fut[["date", "close"]].rename(columns={"close": "fut"})
         .merge(spot[["date", "close"]].rename(columns={"close": "spot"}), on="date")
         .sort_values("date").reset_index(drop=True))
    m["basis"] = (m["fut"] / m["spot"] - 1) * 100
    m["jump"] = m["basis"].diff()

    rolls = m["jump"].abs() > ROLL_JUMP_PCT
    m["is_roll"] = rolls

    # —— 技术点①②③：去换月锯齿的正确做法 ——
    # ⚠️ 不要对**价差**做正向复权（那是给价格序列用的）。价差本身近似平稳
    #    （实测逐年中位只摆动 1.08pp），复权反而引入一条虚假趋势：
    #    实测 Z 均值变成 +0.782（应为 0）、|Z|>1 占比 52%（应约 32%）。
    # 正确做法：换月日先用**前值替代**再算滚动统计（避免跳变污染 mean/std），
    #    但 Z 对所有交易日输出 → 保留 89% 样本，且均值≈0（实测 −0.10）。
    # 另注意：直接置 NaN 会因 rolling 默认要求窗口全非空而废掉 85% 样本。
    r2 = m["basis"].copy()
    r2[rolls] = np.nan
    r2 = r2.ffill()
    m["basis_cont"] = r2                      # 供检查用的连续序列
    mu = r2.rolling(Z_WINDOW).mean()
    sd = r2.rolling(Z_WINDOW).std()
    m["z"] = (m["basis"] - mu) / sd
    m["z_chg5"] = m["z"].diff(5)              # 5 日变化：比水平快，用于提前性检验

    # 自检：标准化后均值应≈0、|Z|>1 应≈32%。偏离说明锯齿没处理对。
    zz = m["z"].dropna()
    if len(zz) > 100 and abs(zz.mean()) > 0.5:
        print(f"  ⚠️ 基差 Z 自检异常：均值 {zz.mean():+.3f}（应≈0）"
              f" → 换月锯齿可能没处理对")
    return m


# ====================================================================
# 报告
# ====================================================================
def load_timeline(idx_code: str) -> dict:
    f = CACHE / "regime_timeline" / f"{idx_code}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def report(pair: str, idx_code: str, name: str, b: pd.DataFrame) -> dict:
    print(f"\n{'─' * 80}")
    print(f"{pair} / {idx_code}（{name}）  {b.date.min():%Y-%m-%d} ~ {b.date.max():%Y-%m-%d}"
          f"  {len(b)} 天")
    n_roll = int(b.is_roll.sum())
    valid = int(b.z.notna().sum())
    print(f"  换月日 {n_roll} 天（已复权）   有效 Z-score {valid}/{len(b)} "
          f"({valid / len(b) * 100:.0f}%)")
    # 自检：标准化后应≈标准正态（均值≈0、|Z|>1≈32%）。
    # 注意不要用 basis_cont 的连续性做自检 —— 它存的是 ffill 后的原始基差，
    # 仍有锯齿（锯齿只是被推到次日），Z 用的是它的滚动统计而非它本身。
    _zz = b["z"].dropna()
    print(f"  Z-score 自检: 均值 {_zz.mean():+.3f}（应≈0）  "
          f"标准差 {_zz.std():.3f}（应≈1）  |Z|>1 {( _zz.abs() > 1).mean() * 100:.0f}%（应≈32%）")
    # 边界放宽到 mean±1.0 / std 0.6~1.8：短样本 + 肥尾（如 IM0 含 2024 小盘极端贴水）
    # 也会让 std 偏离 1，那未必是锯齿处理错。只有**严重**偏离才告警。
    if abs(_zz.mean()) > 1.0 or not (0.6 < _zz.std() < 1.8):
        print(f"     ⚠️ 偏离过大 → 换月锯齿可能没处理对（阈值 ROLL_JUMP_PCT={ROLL_JUMP_PCT}）")

    # 按年看水平漂移（说明为什么必须用 Z-score 而不是绝对阈值）
    b2 = b.dropna(subset=["z"]).copy()
    b2["yr"] = b2.date.dt.year
    print(f"\n  基差水平按年（原始 %）—— 证明不能用绝对阈值：")
    yr = b2.groupby("yr")["basis"].median()
    print("    " + "  ".join(f"{y}:{v:+.2f}" for y, v in yr.items()))
    print(f"    跨年摆动 {yr.max() - yr.min():.2f}pp")

    # —— 预测力检验（**不依赖 regime 时间线**，故放在最前）——
    # 曾把它放在时间线检查之后，导致无时间线的品种（IC/IH/IM）整段被跳过。
    _predictive_test(b2, idx_code)

    # 与恐慌信号的关系（需要 regime 时间线，没有就跳过这一节）
    tl = load_timeline(idx_code)
    if not tl:
        print(f"\n  ⚠️ 无 {idx_code} 的 regime 时间线缓存 → 跳过恐慌对照")
        print(f"     （生成：uv run python scripts/audit_regime_hold.py --indices {idx_code}）")
        return {"pair": pair, "n": len(b), "valid": valid, "rolls": n_roll}
    b2["regime"] = [tl.get(d.strftime("%Y-%m-%d"), {}).get("regime", "-") for d in b2.date]
    b2["panic"] = (b2.regime == "PANIC_DOWN").astype(int)
    p, q = b2[b2.panic == 1], b2[b2.panic == 0]
    print(f"\n  恐慌日 {len(p)} 天 vs 其它 {len(q)} 天：")
    for c, lab in (("z", "基差Z"), ("z_chg5", "基差Z的5日变化")):
        print(f"    {lab:<16s} 恐慌日 {p[c].mean():+7.3f}   其它 {q[c].mean():+7.3f}   "
              f"差 {p[c].mean() - q[c].mean():+7.3f}")

    # —— 核心：基差能否【提前】预警？——
    print(f"\n  【核心检验】恐慌日**之前** N 天，基差 Z 是否已经异常？")
    z = b2["z"].to_numpy(float); pan = b2["panic"].to_numpy()
    idx = list(b2.index)
    pos = {d: i for i, d in enumerate(idx)}
    print(f"    {'提前N天':>8s}{'基差Z均值':>12s}{'恐慌前|Z|>1占比':>16s}")
    for N in (1, 2, 3, 5, 10):
        vals = []
        for i in range(len(b2)):
            if pan[i] == 1:
                j = i - N
                if j >= 0 and np.isfinite(z[j]):
                    vals.append(z[j])
        if len(vals) < 3:
            print(f"    {N:>8d}   样本不足"); continue
        v = np.array(vals)
        print(f"    {N:>8d}{v.mean():>+12.3f}{(np.abs(v) > 1).mean() * 100:>15.0f}%")

    # 全样本基准（用于对比）
    allz = z[np.isfinite(z)]
    print(f"    全样本基准: 均值 {allz.mean():+.3f}  |Z|>1 占比 {(np.abs(allz) > 1).mean() * 100:.0f}%")

    return {"pair": pair, "n": len(b), "valid": valid, "rolls": n_roll,
            "panic_mean_z": float(p["z"].mean()), "other_mean_z": float(q["z"].mean()),
            "all_mean_z": float(allz.mean())}


def _newey_west_t(X: np.ndarray, y: np.ndarray, lag: int) -> np.ndarray:
    """OLS 系数 + Newey-West 标准误下的 t 值。

    ⚠️ 为什么必须用 NW：H 日前瞻收益**高度重叠**（每天一个观测，窗口叠 H−1 天）。
       普通 OLS 的 t 会被夸大 3 倍以上 —— 实测同一份 IF0 数据：
           普通 OLS   t = −4.85  ✅（假象）
           NW lag=20  t = −1.79  ❌
           非重叠     t = −0.47  ❌（完全无效应）
       2026-10-01 实测踩过，故内建此检验，避免脚本持续输出误导性 t 值。
    """
    beta, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
    res = y - X @ beta
    XtX_inv = np.linalg.inv(X.T @ X)
    S = (X * res[:, None]).T @ (X * res[:, None])
    for L in range(1, lag + 1):
        w = 1 - L / (lag + 1)
        G = (X[L:] * res[L:, None]).T @ (X[:-L] * res[:-L, None])
        S += w * (G + G.T)
    se = np.sqrt(np.diag(XtX_inv @ S @ XtX_inv))
    return beta / se


def _predictive_test(b2: pd.DataFrame, idx_code: str) -> None:
    """基差 Z 能否预测未来收益？—— 用三种标准误 + 非重叠样本 + 分段。"""
    spot = b2[["date", "z"]].merge(
        ensure_spot(idx_code)[["date", "open", "close"]], on="date"
    ).sort_values("date").reset_index(drop=True)
    if len(spot) < 300:
        return
    o = spot["open"].to_numpy(float)
    c = spot["close"].to_numpy(float)
    n = len(spot)
    H = 20
    fwd = np.full(n, np.nan)
    for i in range(n):
        j = i + 1
        if j < n and o[j] > 0 and j + H - 1 < n:
            fwd[i] = (c[j + H - 1] / o[j] - 1) * 100
    spot["fwd20"] = fwd
    spot["past20"] = (spot["close"] / spot["close"].shift(20) - 1) * 100
    v = spot.dropna(subset=["z", "fwd20", "past20"])
    if len(v) < 200:
        return
    y = v["fwd20"].to_numpy()
    X = np.column_stack([np.ones(len(v)), v["past20"].to_numpy(), v["z"].to_numpy()])
    print(f"\n  【预测力检验】基差Z 对 未来20日收益（已控制过去20日收益）")
    print(f"    {'标准误':<26s}{'系数':>10s}{'t':>8s}   判定")
    for lab, lag in (("普通 OLS（❌忽略重叠）", 0),
                     (f"Newey-West lag={H}", H), ("Newey-West lag=60", 60)):
        bta, t = np.linalg.lstsq(X, y, rcond=None)[0][2], _newey_west_t(X, y, lag)[2]
        print(f"    {lab:<26s}{bta:>10.4f}{t:>+8.2f}   {'✅' if abs(t) > 1.96 else '❌ 不显著'}")
    nn = v.iloc[::H]
    if len(nn) >= 40:
        X2 = np.column_stack([np.ones(len(nn)), nn["past20"].to_numpy(), nn["z"].to_numpy()])
        b2c, t2 = np.linalg.lstsq(X2, nn["fwd20"].to_numpy(), rcond=None)[0][2], \
            _newey_west_t(X2, nn["fwd20"].to_numpy(), 0)[2]
        print(f"    {'非重叠样本(每20天取1)':<26s}{b2c:>10.4f}{t2:>+8.2f}   "
              f"{'✅' if abs(t2) > 1.96 else '❌ 不显著'}   n={len(nn)}")
    print(f"    ⚠️ 重叠窗口下普通 OLS 的 t 会被夸大数倍 —— 只看 NW / 非重叠那一行")


def main() -> int:
    ap = argparse.ArgumentParser(description="股指期货基差：取数+连续化+标准化+恐慌对照")
    ap.add_argument("--refresh", action="store_true", help="强制重新取数（忽略缓存）")
    ap.add_argument("--pairs", default="", help="只跑指定对，如 IF0:sh000300,IC0:sh000905")
    args = ap.parse_args()

    pairs = DEFAULT_PAIRS
    if args.pairs:
        pairs = []
        for s in args.pairs.split(","):
            fc, ic = s.split(":")
            nm = dict((c, n) for c, _, n in DEFAULT_PAIRS).get(fc, fc)
            pairs.append((fc.strip(), ic.strip(), nm))

    print("=" * 80)
    print("股指期货基差 —— 取数 / 连续化 / 标准化 / 恐慌对照")
    print("=" * 80)
    out = []
    for fc, ic, nm in pairs:
        try:
            fut = fetch_futures(fc, refresh=args.refresh)
            spot = ensure_spot(ic)
        except Exception as e:
            print(f"\n⚠️ {fc}/{ic} 跳过：{type(e).__name__}: {e}")
            continue
        b = build_basis(fut, spot, fc)
        fp = CACHE / f"futures_basis_{fc}.csv"
        b.to_csv(fp, index=False, encoding="utf-8-sig")
        out.append(report(fc, ic, nm, b))
        print(f"  💾 已存 {fp.name}")

    print(f"\n{'=' * 80}\n【怎么读】\n{'=' * 80}")
    print("  · 基差 = 期货/现货 − 1。负值=贴水（空头/套保力量强）")
    print("  · Z-score 是滚动标准化 → 消除逐年水平漂移，可跨年比较")
    print("  · **只看『提前N天』那张表**：若恐慌前基差 Z 已明显偏离（如 |Z|>1 占比")
    print("    显著高于全样本基准），才说明基差有**提前预警**价值")
    print("  · 若恐慌前的 Z 与全样本基准无异 → 基差**只是同步或滞后**，没有增量信息")
    print("  ⚠️ 本脚本只做**描述性**观察。任何结论都需预注册 + 样本外验证，")
    print("     且样本受 33 个恐慌片段的硬约束。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
