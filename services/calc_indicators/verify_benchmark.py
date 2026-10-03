#!/usr/bin/env python3
"""基准指数选择验证脚本 —— 用**注入已知相关性**的方式验证，不依赖网络。

背景（为什么默认不是"按板块换基准"）：
    直觉是"创业板股票看创业板指、深市股票看深证成指"。实测否定了它 ——
    同为创业板股票，最优基准有三种答案：

        300750 宁德时代  vs上证 0.40  vs深证 0.48  vs创业板指 **0.53**  ← 创业板指
        300015 爱尔眼科  vs上证 **0.38** vs深证 0.20  vs创业板指 0.12   ← 上证综指
        300059 东方财富  vs上证 0.63  vs深证 0.57  vs创业板指 0.51   ← 上证综指/沪深300

    且 20 只标的的最优基准分布为：上证 5 / 沪深300 3 / 创业板指 1 / 深证 1。
    ⇒ 按板块硬套会选错一半，正确做法是**按实际相关性**选。

同时必须记住的事实：**个股与任何指数相关性只有 0.15~0.38**（ETF 是 0.80~0.94）。
所以本模块只让"参考基准"更贴合，**不会把弱相关变成强相关**。
verify 里专门有一项检查：强相关关系必须被选中，弱相关必须被标记为 weak。

用法:
    uv run python services/calc_indicators/verify_benchmark.py
    uv run python services/calc_indicators/verify_benchmark.py --offline  # 只跑注入用例
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

import benchmark as B

N = 300


def results_row(ok, label: str, detail: str) -> bool:
    val = bool(ok)
    print(f"    {'✅' if val else '❌'} {label:38s} {detail}")
    return val


def _series_from(seed: int, corr_with: np.ndarray | None = None, rho: float = 0.0) -> pd.Series:
    """生成收益率序列；给定 corr_with 与 rho 时构造与它相关性约等于 rho 的序列。"""
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 1, N)
    if corr_with is None:
        x = noise
    else:
        base = (corr_with - corr_with.mean()) / (corr_with.std() + 1e-12)
        x = rho * base + np.sqrt(max(1e-9, 1 - rho ** 2)) * noise
    idx = pd.bdate_range("2023-01-02", periods=N)
    return pd.Series(x / 100.0, index=idx)


def _fake_index_returns(rho_map: dict, seed: int = 11):
    """构造一个假的 _index_returns：某个"真基准"与标的强相关，其余为噪声。"""
    anchor = _series_from(seed)
    pool = {code: _series_from(seed + i + 1, anchor, rho_map.get(code, 0.0))
            for i, (code, _name) in enumerate(B.CANDIDATES)}
    return anchor, pool


def install_fake(rho_map: dict, seed: int = 11):
    """注入假指数数据，返回 (anchor_returns, 还原函数)。"""
    anchor, pool = _fake_index_returns(rho_map, seed)
    original = B._index_returns

    def fake(code, days=300, min_interval=0.6):
        return pool.get(code)

    B._index_returns = fake
    return anchor, lambda: setattr(B, "_index_returns", original)


# ==================== 用例 ====================

def check_picks_true_benchmark() -> list:
    print("【1】真基准与标的强相关 → 必须被选中（不是被板块或顺序决定）")
    out = []
    for target, rho in [("sz399006", 0.85), ("sh000300", 0.80), ("sz399001", 0.75)]:
        rho_map = {c: 0.05 for c, _ in B.CANDIDATES}
        rho_map[target] = rho
        anchor, restore = install_fake(rho_map)
        try:
            sel = B.select_benchmark(anchor)
        finally:
            restore()
        name = dict(B.CANDIDATES)[target]
        out.append(results_row(
            sel["index"] == target, f"真基准 {target}（ρ≈{rho}）",
            f"选中 {sel['index']}({sel['name']})  ρ={sel['corr']:+.2f}"))
    return out


def check_board_fallacy() -> list:
    print("\n【2】板块启发式会选错 → auto 按相关性选才对")
    out = []
    # 构造一个"创业板股票"：与创业板指弱相关、与沪深300强相关
    # （真实案例：300059 东方财富 vs创业板指 0.51 / vs沪深300 0.64）
    rho_map = {c: 0.03 for c, _ in B.CANDIDATES}
    rho_map["sz399006"] = 0.45      # 创业板指（板块"应该"用的）
    rho_map["sh000300"] = 0.80      # 实际最相关
    anchor, restore = install_fake(rho_map)
    try:
        sel = B.select_benchmark(anchor)
    finally:
        restore()
    out.append(results_row(
        sel["index"] == "sh000300",
        "创业板标的最优基准非创业板指",
        f"选中 {sel['name']}（ρ={sel['corr']:+.2f}）而非创业板指（ρ≈0.45）"))
    return out


def check_weak_correlation_flagged() -> list:
    print("\n【3】与所有指数都弱相关 → 必须标记 weak（不得假装有参考价值）")
    out = []
    rho_map = {c: 0.02 for c, _ in B.CANDIDATES}
    anchor, restore = install_fake(rho_map)
    try:
        sel = B.select_benchmark(anchor)
    finally:
        restore()
    out.append(results_row(
        sel["weak"] and sel["corr"] is not None and abs(sel["corr"]) < B.WEAK_CORR_THRESHOLD,
        "弱相关被标记 weak",
        f"最强仅 ρ={sel['corr']:+.3f}（阈值 {B.WEAK_CORR_THRESHOLD}）→ weak={sel['weak']}"))
    return out


def check_fallback_when_no_data() -> list:
    print("\n【4】指数数据全不可用 → 回退默认，不抛异常")
    out = []
    original = B._index_returns
    B._index_returns = lambda code, days=300, min_interval=0.6: None
    try:
        sel = B.select_benchmark(_series_from(1))
    except Exception as e:
        B._index_returns = original
        out.append(results_row(False, "无数据时回退", f"抛异常 {type(e).__name__}: {e}"))
        return out
    finally:
        B._index_returns = original
    out.append(results_row(
        sel["fallback"] and sel["index"] == B.DEFAULT_INDEX,
        "无数据时回退默认",
        f"index={sel['index']} fallback={sel['fallback']}"))
    return out


def check_default_is_broad_index() -> list:
    print("\n【5】默认基准 = 上证综指（实测它是候选里中位相关性最高的）")
    out = []
    out.append(results_row(
        B.DEFAULT_INDEX == "sh000001" and any(c == "sh000001" for c, _ in B.CANDIDATES),
        "默认 sh000001 且在候选内",
        f"DEFAULT_INDEX={B.DEFAULT_INDEX}，候选 {len(B.CANDIDATES)} 个"))
    # 科创50 不在候选内（baostock 缺失，选了会取数失败）
    out.append(results_row(
        not any(c == "sh000688" for c, _ in B.CANDIDATES),
        "科创50 不在候选（baostock 缺失）",
        "避免选中一个取不到数据的指数"))
    return out


def check_real_symbols() -> list:
    print("\n【6】真实数据抽查（需联网/缓存；验证在真实标的上不崩且结论合理）")
    out = []
    import data_layer as D
    for code, expect_hint in [("300750", "创业板股但最优可能不是创业板指"),
                              ("000333", "与指数几乎不相关，应 weak")]:
        try:
            df = D.fetch_history(code, "2021-09-01")
        except Exception as e:
            print(f"      {code} 取数失败（跳过）: {str(e)[:40]}")
            continue
        sel = B.select_benchmark(df.set_index("date")["close"].pct_change())
        print(f"      {code}: 选中 {sel['index']}({sel['name']}) ρ={sel['corr']:+.3f} weak={sel['weak']}")
        if code == "000333":
            out.append(results_row(sel["weak"], f"{code} 应标记 weak", expect_hint))
        else:
            out.append(results_row(sel["corr"] is not None, f"{code} 能选出基准", expect_hint))
    return out


def main() -> int:
    print("=" * 84)
    print("基准指数选择验证（注入已知相关性，不依赖网络）")
    print("=" * 84)
    res = []
    res += check_picks_true_benchmark()
    res += check_board_fallacy()
    res += check_weak_correlation_flagged()
    res += check_fallback_when_no_data()
    res += check_default_is_broad_index()
    if "--offline" in sys.argv:
        print("\n（--offline：跳过第6项真实数据抽查）")
    else:
        res += check_real_symbols()
    print("=" * 84)
    ok, total = sum(res), len(res)
    print(f"结果: {ok}/{total} 项通过  {'✅ 全部通过' if ok == total else '❌ 存在失败项'}")
    print("=" * 84)
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
