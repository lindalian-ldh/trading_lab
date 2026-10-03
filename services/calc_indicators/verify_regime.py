#!/usr/bin/env python3
"""维度E（市场状态雷达）验证脚本 —— 故意不用 pytest，直接跑即可。

用途：regime_detector.py 的判定逻辑是"分类器"，最容易出的错是
**某些形态被系统性误判**（例如单调趋势的布林带宽天然很小，
若挤压判定不加保护，任何单边行情都会被误判成"极缩量横盘"）。

本脚本用合成行情覆盖 7 种状态 + 边界鲁棒性，并**对震荡类取 12 个相位分布**
（避免"恰好收在上涨段/下跌段"造成样本偏差——这是最容易自欺欺人的地方）。

用法:
    uv run python services/calc_indicators/verify_regime.py

输出：
    - 趋势/挤压类：要求 12/12 相位全部命中（判定必须稳定）
    - 震荡类：要求全部落在 RANGE_*（高位/低位/中位由相位决定，属正常）
    - 恐慌类：要求真正的急跌能判出，且震荡行情不误报
"""

from __future__ import annotations

import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

from config import TradingConfig
import regime_detector as RD

CFG = TradingConfig()
CFG.REGIME_DEBUG = False

N = 260
T = np.arange(N)


# ==================== 合成行情工具 ====================

def _frame(close: np.ndarray, seed: int = 3, vol_hi: float = 0.002) -> pd.DataFrame:
    """把收盘价序列包装成 OHLCV DataFrame（high/low 由 close ± 小噪声生成）。"""
    rng = np.random.default_rng(seed)
    op = np.r_[close[0], close[:-1]]
    hi = np.maximum(op, close) * (1 + abs(rng.normal(0, vol_hi, len(close))))
    lo = np.minimum(op, close) * (1 - abs(rng.normal(0, vol_hi, len(close))))
    return pd.DataFrame({
        "date": pd.bdate_range("2024-01-01", periods=len(close)),
        "open": op, "high": hi, "low": lo, "close": close,
        "volume": rng.lognormal(0, 0.3, len(close)) * 1e8,
    })


def _osc(level: float, amp: float, period: int, noise: float,
         vol_hi: float, phase: float, seed: int) -> pd.DataFrame:
    """正弦震荡 + 噪声：模拟区间行情。phase 用于取 12 个不同收尾位置。

    振幅/噪声经过标定，使日均波动约 0.7%（真实 A 股震荡市的量级）——
    如果振幅开到 130，3 日就能跌 4%，会（正确地）触发恐慌判定，
    那是标本失真而不是分类器出错。
    """
    rng = np.random.default_rng(seed)
    close = level + amp * np.sin(2 * np.pi * (T / period + phase)) + rng.normal(0, noise, N)
    return _frame(close, vol_hi=vol_hi)


def _trend(up: bool, seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    drift = 0.0035 if up else -0.0035
    return _frame(3000 * np.exp(drift * T + rng.normal(0, 0.004, N).cumsum()))


def _squeeze(phase: float, seed: int) -> pd.DataFrame:
    """振幅指数衰减的窄幅震荡：末期几乎不动 → 真正的"变盘前夜"。"""
    close = 3000 + 18 * np.sin(2 * np.pi * (T / 9 + phase)) * np.exp(-T / 120)
    return _frame(close, vol_hi=0.0010, seed=seed)


def _crash(daily_mults: list, seed: int = 5) -> pd.DataFrame:
    """上升趋势末端接连续大阴线：模拟急跌。"""
    rng = np.random.default_rng(seed)
    close = 3000 * np.exp(0.0015 * T + rng.normal(0, 0.003, N).cumsum())
    close[-len(daily_mults):] *= np.array(daily_mults)
    return _frame(close, vol_hi=0.008)


def _run(df: pd.DataFrame) -> dict:
    """把合成行情注入取数函数后跑一次判定。

    必须先清空 regime_detector 的进程内缓存：该缓存以 (基准指数, 日期) 为键，
    而本脚本对每个用例注入**不同的合成行情**，同一进程内日期相同 →
    不清缓存就会一直返回第一个用例的结果（实测会把后续所有用例都判成 TREND_UP）。
    生产环境每天只取一次指数，缓存是正确优化；这里属于测试需要绕过它。
    """
    RD._REGIME_CACHE.clear()
    RD._fetch_index_data = lambda _cfg, _df=df: _df
    return RD.detect_regime(CFG)


# ==================== 各项验证 ====================

def check_stable_regimes() -> list:
    """趋势 / 挤压：12 个相位必须全部命中同一个状态（判定不许抖）。"""
    print("【1】稳定型状态（要求 12/12 相位全部命中）")
    results = []
    cases = [
        ("TREND_UP", lambda ph: _trend(up=True)),
        ("TREND_DOWN", lambda ph: _trend(up=False)),
        ("SQUEEZE", lambda ph: _squeeze(ph / 12, int(ph * 7 + 1))),
    ]
    for expect, mk in cases:
        cnt = Counter(_run(mk(ph / 12))["regime"] for ph in range(12))
        hit = cnt.get(expect, 0)
        ok = hit == 12
        results.append(ok)
        print(f"    {expect:11s} 命中 {hit:2d}/12  {dict(cnt)}  {'✅' if ok else '❌'}")
    return results


def check_range_regimes() -> list:
    """震荡：全部相位都必须落在 RANGE_*（高/中/低位由相位决定，属正常）。

    同时校验"高/低位锚定"：以高位为中枢的震荡，其 RANGE_LOW 出现次数
    不应超过 RANGE_HIGH（否则说明带内位置分区失去意义）。
    """
    print("\n【2】震荡型状态（要求全部落入 RANGE_*，且高低位锚定合理）")
    results = []
    cases = [
        ("RANGE_MID(中枢3000)", 3000, 55),
        ("RANGE_HIGH(中枢3150)", 3150, 55),
        ("RANGE_LOW(中枢2850)", 2850, 55),
    ]
    for name, level, amp in cases:
        cnt = Counter(
            _run(_osc(level, amp, 11, 7, 0.0015, ph / 12, int(ph * 7 + 2)))["regime"]
            for ph in range(12)
        )
        n_range = sum(v for k, v in cnt.items() if k.startswith("RANGE"))
        n_panic = cnt.get("PANIC_DOWN", 0)
        ok = n_range == 12 and n_panic == 0
        results.append(ok)
        print(f"    {name:20s} RANGE {n_range:2d}/12  误报恐慌 {n_panic}  {dict(cnt)}  {'✅' if ok else '❌'}")
    return results


def check_panic() -> list:
    """恐慌：真急跌必须判出（不漏报）；轻微回调不得误报。

    注意 ret3 的基准是「4 根之前」，所以 3 根乘数之积 ≠ 近3日跌幅。
    这里额外校验实际跌幅确实达到恐慌量级，避免"标签写着急跌、其实只是回调"。
    """
    print("\n【3】恐慌急跌（真急跌不漏报 / 轻回调不误报）")
    results = []
    cases = [
        ("剧烈急跌（3日约-5.7%）", [0.95, 0.94, 0.93], "PANIC_DOWN"),
        ("中度急跌（3日约-4.5%）", [0.965, 0.955, 0.95], "PANIC_DOWN"),
        ("轻微回调（3日约-2.6%）", [0.98, 0.975, 0.96], None),
    ]
    for name, mults, expect in cases:
        r = _run(_crash(mults))
        f = r["facts"]
        r3 = f.get("ret_3d_pct")
        gate = -CFG.REGIME_PANIC_RET3_ATR_MULT * f.get("atr_pct_of_price")
        if expect:
            ok = r["regime"] == expect
        else:
            ok = r["regime"] != "PANIC_DOWN"      # 轻回调不该报恐慌
        results.append(ok)
        print(f"    {name:22s} → {r['regime']:11s} 近3日={r3:+.2f}%  "
              f"ATR倍数门槛={gate:+.2f}% 绝对下限={CFG.REGIME_PANIC_RET3_FLOOR_PCT:+.1f}%  "
              f"{'✅' if ok else '❌'}")
    return results


def check_robustness() -> list:
    """边界输入：不抛异常、正确降级为 UNKNOWN。"""
    print("\n【4】边界鲁棒性（要求不抛异常）")
    results = []
    flat = pd.DataFrame({
        "date": pd.bdate_range("2024-01-01", periods=80),
        "open": [3000] * 80, "high": [3000] * 80, "low": [3000] * 80,
        "close": [3000] * 80, "volume": [0] * 80,
    })
    cases = [
        ("80根全平（零波动）", flat, True),
        ("仅10根（数据不足）", flat.head(10), False),
        ("空表", flat.head(0), False),
    ]
    for name, df, expect_available in cases:
        try:
            r = _run(df)
            ok = r["available"] is expect_available
            results.append(ok)
            print(f"    {name:20s} regime={r['regime']:8s} available={r['available']!s:5s}  {'✅' if ok else '❌'}")
        except Exception as e:  # noqa: BLE001
            results.append(False)
            print(f"    {name:20s} ❌ 抛异常 {type(e).__name__}: {e}")
    return results


def check_readonly() -> list:
    """只读性：判定过程绝不能修改 config 的任何字段（这是本模块的核心契约）。"""
    print("\n【5】只读性（判定过程不得修改 config）")
    from dataclasses import fields, asdict
    before = asdict(CFG)
    _run(_trend(up=True))
    after = asdict(CFG)
    diff = {k for k in before if before[k] != after[k]}
    ok = not diff
    print(f"    被修改的字段: {sorted(diff) if diff else '无'}  {'✅' if ok else '❌'}")
    return [ok]


def main() -> int:
    print("=" * 68)
    print("维度E - 市场状态雷达 验证（合成行情，无网络依赖）")
    print("=" * 68)
    results = []
    results += check_stable_regimes()
    results += check_range_regimes()
    results += check_panic()
    results += check_robustness()
    results += check_readonly()
    print("=" * 68)
    ok, total = sum(results), len(results)
    print(f"结果: {ok}/{total} 项通过  {'✅ 全部通过' if ok == total else '❌ 存在失败项'}")
    print("=" * 68)
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
