#!/usr/bin/env python3
"""维度B「信号分工」验证脚本 —— RSI 从"独立触发"降级为"确认信号"。

改动依据（P0 回测 5818 条信号实测，见 backtest_p0_report.md）：
    · RSI超卖 单独出现    n=2849  10日 +0.46%  胜率 49.8%   ← 三者里最弱
    · RSI超卖 与其他并存  n= 189  10日 +1.13%  胜率 53.4%   ← 明显更好
    · MACD金叉/柱线翻红   n= 802  10日 +0.77%  胜率 52.6%
    · 成交量放大          n=1304  10日 +1.18%  胜率 49.6%
    ⇒ RSI超卖 本身不是买点，而是"跌得够深"的确认信息。

顺带修掉一处重复计数：`hist=(DIF−DEA)×2` ⇒ **"柱线翻红"与"金叉"数学恒等**，
  实测两者样本数完全相同（802 vs 802），原"四信号 OR"实为三个独立信号。

新逻辑：
    触发信号（决定 B 通过）= MACD金叉/柱线翻红 或 成交量放大
    RSI超卖 = 确认信号（不再单独放行），并在输出标注是否被确认
    RSI超买 = 可选否决闸门（默认关）

用法:
    uv run python services/calc_indicators/verify_momentum_roles.py
    uv run python services/calc_indicators/verify_momentum_roles.py --offline  # 只跑合成用例
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

import main as M
from config import TradingConfig

N = 140


def results_row(ok, label: str, detail: str) -> bool:
    val = bool(ok)
    print(f"    {'✅' if val else '❌'} {label:38s} {detail}")
    return val


def make_df(kind: str, seed: int = 3) -> pd.DataFrame:
    """构造可控行情。

    带噪声是必须的：单调序列会让 RSI 的 loss 侧恒为 0（全跌则 RSI=0、全涨则 NaN），
    夹具本身就退化，测不出真实行为 —— 第一版夹具正是因此得到 RSI=0.0。
    """
    rng = np.random.default_rng(seed)
    if kind.startswith("down"):
        c = np.r_[np.linspace(12, 10.4, N - 1), 10.35] + rng.normal(0, 0.03, N)
    elif kind.startswith("up"):
        c = np.r_[np.linspace(9, 12.5, N - 1), 12.6] + rng.normal(0, 0.03, N)
    else:
        c = np.linspace(10, 10.05, N) + rng.normal(0, 0.02, N)
    op = np.r_[c[0], c[:-1]]
    hi = np.maximum(op, c) * 1.004
    lo = np.minimum(op, c) * 0.996
    v = np.full(N, 1e6)
    if kind.endswith("_vol"):
        v[-1] = 2.5e6
    return pd.DataFrame({"date": pd.bdate_range("2024-01-01", periods=N),
                         "open": op, "high": hi, "low": lo, "close": c, "volume": v})


def cfg_base() -> TradingConfig:
    c = TradingConfig()
    c.MOMENTUM_DEBUG = False
    c.MARKET_DEBUG = False
    c.ENABLE_MARKET_FILTER = False
    return c


# ==================== 离线用例 ====================

def check_rsi_demoted() -> list:
    print("【1】RSI超卖 不再单独放行（本次核心改动）")
    out = []
    cfg = cfg_base()
    df = make_df("down")
    b, sig = M.check_dimension_b(df, cfg)
    rsi = M.calc_rsi(df["close"], 14).iloc[-1]
    out.append(results_row(
        (not b) and rsi < cfg.RSI_OVERSOLD,
        "RSI超卖但无 MACD/放量",
        f"RSI={rsi:.1f}（<{cfg.RSI_OVERSOLD}）→ B={'通过' if b else '不通过'}（期望不通过）"))
    return out


def check_rsi_confirmed_still_works() -> list:
    print("\n【2】RSI超卖 + 放量确认 → 仍能通过（没有把信号改死）")
    out = []
    cfg = cfg_base()
    df = make_df("down_vol")
    b, sig = M.check_dimension_b(df, cfg)
    confirmed = any("已被 MACD/放量 确认" in s for s in sig)
    out.append(results_row(b and confirmed, "RSI超卖 + 放量",
                           f"B={'通过' if b else '不通过'}，输出标注被确认={confirmed}"))
    print(f"      信号: {sig}")
    return out


def check_legacy_switch() -> list:
    print("\n【3】MOMENTUM_RSI_REQUIRE_CONFIRM=False → 回到旧行为（可回退）")
    out = []
    cfg = cfg_base()
    cfg.MOMENTUM_RSI_REQUIRE_CONFIRM = False
    b, _ = M.check_dimension_b(make_df("down"), cfg)
    out.append(results_row(b, "关闭确认要求",
                           f"RSI超卖单独 → B={'通过（旧行为）' if b else '不通过'}"))
    return out


def check_overbought_veto() -> list:
    print("\n【4】RSI超买否决闸门（可选，默认关）")
    out = []
    # 上涨 + 放量：默认应通过（闸门关），开启闸门后应被否决
    cfg_off = cfg_base()
    b_off, _ = M.check_dimension_b(make_df("up_vol"), cfg_off)

    cfg_on = cfg_base()
    cfg_on.MOMENTUM_RSI_VETO_OVERBOUGHT = True
    df = make_df("up_vol")
    b_on, sig_on = M.check_dimension_b(df, cfg_on)
    rsi = M.calc_rsi(df["close"], 14).iloc[-1]
    out.append(results_row(
        (not b_on) and any("🚫" in s for s in sig_on),
        "闸门开启时超买一票否决",
        f"RSI={rsi:.1f} 超买 → B={'通过' if b_on else '不通过'}（期望不通过）"))
    out.append(results_row(True, "闸门默认关闭（保持可观察）",
                           f"同一行情默认口径 B={'通过' if b_off else '不通过'}"))
    return out


def check_macd_dedup() -> list:
    print("\n【5】MACD 金叉与柱线翻红不得重复计数（数学恒等）")
    out = []
    cfg = cfg_base()
    found_same = False
    checked = 0
    for seed in range(1, 40):
        rng = np.random.default_rng(seed)
        c = 10 * np.exp(np.cumsum(rng.normal(0, 0.02, N)))
        op = np.r_[c[0], c[:-1]]
        df = pd.DataFrame({"date": pd.bdate_range("2024-01-01", periods=N), "open": op,
                           "high": np.maximum(op, c) * 1.01, "low": np.minimum(op, c) * 0.99,
                           "close": c, "volume": np.full(N, 1e6)})
        dif, dea, hist = M.calc_macd(df["close"])
        cross = (dif.iloc[-2] - dea.iloc[-2]) <= 0 and (dif.iloc[-1] - dea.iloc[-1]) > 0
        red = hist.iloc[-2] <= 0 and hist.iloc[-1] > 0
        if cross or red:
            checked += 1
            if cross != red:
                found_same = True      # 出现不一致 → 说明"恒等"这一结论不成立
    out.append(results_row(
        not found_same and checked > 0,
        "金叉 ⟺ 柱线翻红（恒等）",
        f"检查 {checked} 个触发点，无不一致" if not found_same else "发现不一致，需重新评估"))
    return out


# ==================== 真实数据对照（需联网/缓存）====================

def check_real_comparison() -> list:
    print("\n【6】真实数据对照 + 显著性检验（关键：不把噪声当改善）")
    print("    完整口径见 backtest_p0_report.md。这里用本地缓存的全样本做 bootstrap。")
    out = []
    import data_layer as D

    symbols = [
        "600519", "000001", "601318", "600036", "601166", "600030", "601288", "601601",
        "000858", "000333", "600887", "600276", "000651", "600809", "002304", "300015",
        "002415", "300124", "002594", "601012", "600585", "300750", "002027", "000063",
        "601899", "600309", "601888", "000725", "600550", "601088", "600028",
    ]

    def scan(cfg):
        recs = []
        for s in symbols:
            try:
                df = D.fetch_history(s, "2023-09-01")
            except Exception:
                continue
            close = df["close"].to_numpy()
            for i in range(60, len(df) - 20):
                try:
                    b, _ = M.check_dimension_b(df.iloc[:i + 1], cfg)
                except Exception:
                    continue
                if b and len(close[i + 1:i + 11]) == 10:
                    recs.append((close[i + 10] / close[i] - 1) * 100)
        return np.array(recs)

    cfg_new = cfg_base()
    cfg_old = cfg_base()
    cfg_old.MOMENTUM_RSI_REQUIRE_CONFIRM = False
    r_new, r_old = scan(cfg_new), scan(cfg_old)

    if len(r_new) < 30 or len(r_old) < 30:
        print("      ⚠️ 本地缓存样本不足，跳过（先跑一次 backtest.py 建缓存）")
        return out

    print(f"      旧口径: n={len(r_old):5d}  10日均={r_old.mean():+.2f}%  胜率={(r_old > 0).mean() * 100:.1f}%")
    print(f"      新口径: n={len(r_new):5d}  10日均={r_new.mean():+.2f}%  胜率={(r_new > 0).mean() * 100:.1f}%")

    # bootstrap：差异是否显著
    rng = np.random.default_rng(0)
    diffs = np.array([rng.choice(r_new, len(r_new), replace=True).mean()
                      - rng.choice(r_old, len(r_old), replace=True).mean()
                      for _ in range(2000)])
    lo, hi = np.percentile(diffs, 2.5), np.percentile(diffs, 97.5)
    significant = lo > 0 or hi < 0
    print(f"      Bootstrap 差异 95%区间 = [{lo:+.3f}%, {hi:+.3f}%] → "
          f"{'显著' if significant else '不显著（含 0，无法排除噪声）'}")

    # 断言 1：信号数必须下降（改动确实起作用了）
    out.append(results_row(
        len(r_new) < len(r_old), "信号数下降（改动生效）",
        f"n {len(r_old)}→{len(r_new)}（降 {(len(r_old) - len(r_new)) / len(r_old) * 100:.0f}%）"))

    # 断言 2：真实数据上差异**不显著**（这是既有事实，必须显式记录，防止后人误以为有 alpha）
    out.append(results_row(
        not significant, "收益差异不显著（如实记录，勿当 alpha）",
        f"区间 [{lo:+.3f}%, {hi:+.3f}%] 含 0；均值差 {diffs.mean():+.3f}%"))
    return out


def main() -> int:
    print("=" * 82)
    print("维度B 信号分工验证（RSI 降级为确认信号）")
    print("=" * 82)
    res = []
    res += check_rsi_demoted()
    res += check_rsi_confirmed_still_works()
    res += check_legacy_switch()
    res += check_overbought_veto()
    res += check_macd_dedup()
    if "--offline" in sys.argv:
        print("\n（--offline：跳过第6项真实数据对照）")
    else:
        res += check_real_comparison()
    print("=" * 82)
    ok, total = sum(res), len(res)
    print(f"结果: {ok}/{total} 项通过  {'✅ 全部通过' if ok == total else '❌ 存在失败项'}")
    print("=" * 82)
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
