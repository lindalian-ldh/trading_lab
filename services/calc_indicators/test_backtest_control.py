#!/usr/bin/env python3
"""`backtest.scan_symbol(record_control=...)` 的离线回归测试。

为什么必须存在这个测试（步骤 1 的验收条件）：

    改造的全部风险在于"为了拿到对照组，悄悄改了原口径"。所以本文件把三条契约
    钉死 —— 只要它们不破，`--paired` 就不可能污染既有的 P0 报告数字：

      契约1（零回归）：`record_control=False` 与 `True` 下的 **signal 行必须逐格相同**。
      契约2（完整性）：`record_control=True` 时，同一批 K 线里每个样本都恰好落进
                       signal / vetoed / below_bc / nosignal 之一，四类之和 == 迭代次数。
      契约3（分类正确）：rec_type 与 (a_pass, b_pass, c_pass, gate.passed) 的映射
                        符合 _classify_record 的定义，且 vetoed 行必然 gate_evaluated。

全部离线：维度判定与门控都用夹具替换（真实现依赖指数/baostock，不适合单测）。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
import pytest

import backtest as B
from config import TradingConfig

N_BARS = 100
WARMUP = 60
HORIZON = 20
# 12 根一循环，确保四类记录都出现、且带不必要的分支也被覆盖
PATTERN = [
    # (a_pass, b_pass, c_pass, gate_passed)
    (True, True, True, True),      # signal
    (True, True, True, False),     # vetoed
    (True, True, False, True),     # below_bc
    (False, False, True, True),    # nosignal
    (True, False, True, True),     # signal
    (False, True, True, True),     # signal
    (True, False, False, True),    # below_bc
    (False, True, False, True),    # below_bc
    (False, False, False, True),   # nosignal
    (True, False, False, False),   # vetoed
    (False, True, True, False),    # vetoed
    (True, True, False, False),    # vetoed
]


def make_df(n: int = N_BARS) -> pd.DataFrame:
    """构造"看起来正常"的日线：唯一目的是让 scan_symbol 不因数据异常提前 continue。"""
    rng = np.random.default_rng(20260928)
    close = 10.0 + np.cumsum(rng.normal(0, 0.05, n))
    close = np.maximum(close, 1.0)
    op = np.r_[close[0], close[:-1]]
    return pd.DataFrame({
        "date": pd.bdate_range("2020-01-01", periods=n),
        "open": op,
        "high": np.maximum(op, close) * 1.01,
        "low": np.minimum(op, close) * 0.99,
        "close": close,
        "volume": rng.integers(1e6, 5e6, n).astype(float),
    })


def make_config() -> TradingConfig:
    """关掉一切需要外部数据的只读模块，保证测试不触网。"""
    cfg = TradingConfig()
    cfg.PULLBACK_DEBUG = False
    cfg.MOMENTUM_DEBUG = False
    cfg.MARKET_DEBUG = False
    cfg.ENABLE_MARKET_FILTER = False      # 否则维度B 会去取大盘指数
    cfg.ENABLE_REGIME_DETECTOR = False
    cfg.REGIME_DEBUG = False
    return cfg


@pytest.fixture()
def patched(monkeypatch):
    """用夹具替换维度判定与门控：返回预设结论矩阵。"""
    def _iter_index(i):
        return (i - WARMUP) % len(PATTERN)

    def fake_a(sub, config):
        p = _iter_index(len(sub) - 1)
        return PATTERN[p][0], ("突破前高" if PATTERN[p][0] else "结构不符")

    def fake_b(sub, config):
        p = _iter_index(len(sub) - 1)
        return PATTERN[p][1], (["MACD金叉"] if PATTERN[p][1] else [])

    def fake_c(sub, config):
        p = _iter_index(len(sub) - 1)
        return PATTERN[p][2], {"ratio": 3.0 if PATTERN[p][2] else 0.5,
                               "price": 10.0, "stop": 9.0, "take": 12.0,
                               "loss_space": 1.0, "profit_space": 2.0,
                               "stop_mode": "near_low", "profit_mode": "resistance",
                               "stop_detail": {}}

    def fake_gate(regime, a_pass, a_msg, b_signals, c_pass, c_info, df, config):
        i = len(df) - 1
        p = _iter_index(i)
        passed = PATTERN[p][3]
        return {"passed": passed,
                "vetoes": [] if passed else [{"rule": "RR_RATIO", "detail": "夹具"}],
                "rules": [], "p_required": 0.25, "excluded": False, "enabled": True}
    # symbol 是 2026-09-30 新增的形参（用于 ETF 免价格检查）。
    # 必须跟上真实签名：_eval_gate 会捕获所有异常并"保守放行"，
    # 签名不匹配会让门控**静默失效**（所有行都变成放行、vetoed 类消失）。
    _fake_gate_impl = fake_gate

    def fake_gate(regime, a_pass, a_msg, b_signals, c_pass, c_info, df, config,
                  symbol=""):
        return _fake_gate_impl(regime, a_pass, a_msg, b_signals, c_pass, c_info,
                               df, config)

    monkeypatch.setattr(B.M, "check_dimension_a", fake_a)
    monkeypatch.setattr(B.M, "check_dimension_b", fake_b)
    monkeypatch.setattr(B.M, "check_dimension_c", fake_c)
    monkeypatch.setattr(B.M, "check_entry_gate", fake_gate)
    return PATTERN


def test_signal_rows_identical_with_and_without_control(patched):
    """契约1：零回归 —— 这是步骤 1 最重要的断言。"""
    df, cfg = make_df(), make_config()

    only = B.scan_symbol("000001", df, cfg, {}, index_df=None,
                         warmup=WARMUP, horizon=HORIZON, record_control=False)
    both = B.scan_symbol("000001", df, cfg, {}, index_df=None,
                         warmup=WARMUP, horizon=HORIZON, record_control=True)
    sig = [r for r in both if r["rec_type"] == "signal"]

    assert len(only) > 0, "夹具应至少产出一个 signal 行"
    assert len(only) == len(sig), (
        f"signal 行数不一致: 默认 {len(only)} vs --paired {len(sig)}")
    assert only == sig, "signal 行内容与顺序必须逐格一致（零回归契约被破坏）"


def test_every_bar_lands_in_exactly_one_class(patched):
    """契约2：四类之和 == 迭代次数，且不含未分类样本。"""
    df, cfg = make_df(), make_config()
    both = B.scan_symbol("000001", df, cfg, {}, index_df=None,
                         warmup=WARMUP, horizon=HORIZON, record_control=True)

    n_iter = len(range(WARMUP, len(df) - HORIZON))
    assert len(both) == n_iter, f"记录数 {len(both)} != 迭代次数 {n_iter}"
    assert set(r["rec_type"] for r in both) <= set(B.REC_TYPES)
    # 夹具的 12 循环覆盖全部四类
    assert set(r["rec_type"] for r in both) == set(B.REC_TYPES)


def test_classification_matches_definition(patched):
    """契约3：rec_type 与四元组 (a,b,c,gate) 的映射必须符合定义。

    注意：夹具直接把门控结果喂进来，所以四类都能出现（含 below_bc）。
    真实运行中 below_bc 在默认配置下不可达 —— 见 _classify_record docstring。
    """
    df, cfg = make_df(), make_config()
    both = B.scan_symbol("000001", df, cfg, {}, index_df=None,
                         warmup=WARMUP, horizon=HORIZON, record_control=True)

    for r in both:
        expected = B._classify_record(r["a_pass"], r["b_pass"], r["c_pass"],
                                      r["n_vetoes"] == 0)
        assert r["rec_type"] == expected, (
            f"{r['date']} 分类错误: 得 {r['rec_type']} 应为 {expected} "
            f"(a={r['a_pass']} b={r['b_pass']} c={r['c_pass']} vetoes={r['n_vetoes']})")
        # 门控在配对模式下对每一行都求了值
        assert r["gate_evaluated"] is True
        if r["rec_type"] == "vetoed":
            assert r["n_vetoes"] >= 1
        if r["rec_type"] == "signal":
            assert r["n_vetoes"] == 0 and r["c_pass"] is True
            assert r["a_pass"] or r["b_pass"]


def test_default_mode_has_no_control_rows(patched):
    """默认模式（--paired 关闭）不得出现任何 rec_type != signal 的行。"""
    df, cfg = make_df(), make_config()
    only = B.scan_symbol("000001", df, cfg, {}, index_df=None,
                         warmup=WARMUP, horizon=HORIZON, record_control=False)
    assert all(r["rec_type"] == "signal" for r in only)


def test_vetoed_counter_is_not_double_counted(patched):
    """vetoed 计数在两种模式下必须一致（它喂给屏幕上的'门控拦截'统计）。"""
    df, cfg = make_df(), make_config()
    v1, v2 = [0], [0]
    B.scan_symbol("000001", df, cfg, {}, index_df=None,
                  warmup=WARMUP, horizon=HORIZON, vetoed_count=v1, record_control=False)
    both = B.scan_symbol("000001", df, cfg, {}, index_df=None,
                         warmup=WARMUP, horizon=HORIZON, vetoed_count=v2,
                         record_control=True)
    assert v1[0] == v2[0], f"vetoed 计数不一致: {v1[0]} vs {v2[0]}"
    assert v2[0] == sum(1 for r in both if r["rec_type"] == "vetoed")


def test_classify_record_pure_cases():
    """_classify_record 的判定表（不依赖行情夹具）。"""
    assert B._classify_record(True, True, True, True) == "signal"
    assert B._classify_record(True, False, True, True) == "signal"
    assert B._classify_record(False, True, True, True) == "signal"
    assert B._classify_record(True, True, True, False) == "vetoed"
    assert B._classify_record(False, True, True, False) == "vetoed"
    assert B._classify_record(True, True, False, True) == "below_bc"
    assert B._classify_record(False, True, False, True) == "below_bc"
    assert B._classify_record(False, False, True, True) == "nosignal"
    assert B._classify_record(False, False, False, False) == "nosignal"


# ==================== MOMENTUM_TRIGGERS 白名单契约 ====================

def test_make_config_applies_overrides():
    """-o 覆盖必须真的作用到 config，且与 main.py 用同一套 apply_overrides。"""
    cfg = B.make_config(["MOMENTUM_TRIGGERS=MACD"])
    assert list(cfg.MOMENTUM_TRIGGERS) == ["MACD"]
    # 调试开关必须被强制关掉（回测逐根重放会刷屏）
    assert cfg.PULLBACK_DEBUG is False and cfg.MOMENTUM_DEBUG is False

    default = B.make_config(None)
    assert set(default.MOMENTUM_TRIGGERS) == {"MACD", "RSI_OVERSOLD", "VOLUME_SURGE"}


def test_momentum_triggers_default_is_backward_compatible():
    """默认白名单必须包含全部三个触发源 —— 否则会静默改变既有信号口径。"""
    cfg = TradingConfig()
    assert set(cfg.MOMENTUM_TRIGGERS) == {"MACD", "RSI_OVERSOLD", "VOLUME_SURGE"}


@pytest.mark.parametrize("triggers", [
    ["MACD"], ["MACD", "RSI_OVERSOLD"], ["VOLUME_SURGE"],
])
def test_whitelist_only_affects_passing_not_recording(triggers, monkeypatch):
    """白名单只改 B 是否通过；命中仍必须被记录（否则诊断列语义被破坏）。

    这是本次改动最容易犯的错：为了"过滤掉放量"，把 signals 一起删掉，
    会让 b_vol_surge 诊断列永远为 False，事后无法归因。
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import main as M

    cfg = TradingConfig()
    cfg.MOMENTUM_TRIGGERS = list(triggers)
    cfg.MOMENTUM_DEBUG = False
    cfg.MARKET_DEBUG = False
    cfg.ENABLE_MARKET_FILTER = False       # 隔离：只看个股动量

    # 构造一个必定触发"成交量放大"的行情（末根放量 3 倍）
    n = 120
    rng = np.random.default_rng(3)
    close = 10.0 + np.cumsum(rng.normal(0, 0.02, n))
    vol = np.full(n, 1e6)
    vol[-1] = 5e6
    df = pd.DataFrame({
        "date": pd.bdate_range("2020-01-01", periods=n),
        "open": close, "high": close * 1.01, "low": close * 0.99,
        "close": close, "volume": vol,
    })

    passed, signals = M.check_dimension_b(df, cfg)
    vol_hit = any("成交量放大" in s for s in signals)
    assert vol_hit, "成交量放大必须仍被记录在 signals 里（诊断口径不变）"
    if "VOLUME_SURGE" not in triggers:
        assert not passed, "放量未在白名单内时不应放行 B"


def test_empty_whitelist_never_passes_b(monkeypatch):
    """空白名单 = 不允许任何触发源 → B 永不通过（必须是显式行为，不能悄悄放行）。"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import main as M

    cfg = TradingConfig()
    cfg.MOMENTUM_TRIGGERS = []
    cfg.MOMENTUM_DEBUG = False
    cfg.ENABLE_MARKET_FILTER = False

    n = 120
    rng = np.random.default_rng(5)
    close = 10.0 + np.cumsum(rng.normal(0, 0.02, n))
    vol = np.full(n, 1e6); vol[-1] = 5e6
    df = pd.DataFrame({
        "date": pd.bdate_range("2020-01-01", periods=n),
        "open": close, "high": close * 1.01, "low": close * 0.99,
        "close": close, "volume": vol,
    })
    passed, _ = M.check_dimension_b(df, cfg)
    assert not passed


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
