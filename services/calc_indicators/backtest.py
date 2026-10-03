#!/usr/bin/env python3
"""P0 回测引擎：把现有三维度信号在历史上逐根重放，统计真实表现并**按市场状态分层**。

目的（回答一个具体问题）：
    现在的阈值是"拍"出来的 —— RSI 30、偏离 0.5%、盈亏比 2.0，对**所有行情**用同一套。
    regime_plan.md 里的预判是：`TREND_DOWN + RSI超卖` 应为负期望（在下跌趋势里抄底）。
    P0 就是用数据检验这类预判，而不是靠感觉。

P0 刻意保持"最小闭环"，只回答"信号准不准"，不含实盘细节：
    · 入场：维度A / 维度B 通过（维度C 只记录，不参与离场）
    · 离场：**固定持有 N 日**（5/10/20）—— 最无偏，不做任何参数选择
    · 另记一条"结构性止损"口径（ATR 止损/止盈）作为参考，避免只看固定持有期产生幻觉
    · 指标：平均收益、胜率、MFE（最大有利波动）、MAE（最大不利波动）、先触止损率
    · 分层：按**入场当日的市场状态**（regime A~E 只读模块）分组

两个关键的正确性保障：
    1. **无未来函数**：每根只用 df.iloc[:i+1]；已由 verify 审计确认指标全是滚动窗口
    2. **regime 按日期缓存**：市场状态是全市场共享的，同一天只算一次，
       并且**只使用该日及之前的指数数据**（regime_detector 内部切片）

用法:
    # 30 只 × 3 年（默认，数据走本地缓存）
    uv run python services/calc_indicators/backtest.py

    # 指定标的与区间
    uv run python services/calc_indicators/backtest.py --symbols 600519,000001 --start 2021-09-01

    # 只看缓存统计，不跑回测
    uv run python services/calc_indicators/backtest.py --cache-stats

    # 强制重新拉数（慎用，会触网并被限流）
    uv run python services/calc_indicators/backtest.py --refresh
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

import main as M
from config import TradingConfig

logger = logging.getLogger(__name__)

# 默认回测池：跨行业、含大小盘、上市时间足够早（避免新股导致样本不足）。
# 显式写死而非"按市值取前 N"是为了可复现，也避免引入选股偏差。
DEFAULT_SYMBOLS = [
    # 金融
    "600519", "000001", "601318", "600036", "601166", "600030", "601288", "601601",
    # 消费/医药
    "000858", "000333", "600887", "600276", "000651", "600809", "002304", "300015",
    # 科技/制造
    "002415", "300124", "002594", "601012", "600585", "300750", "002027", "000063",
    # 周期/资源
    "601899", "600309", "601888", "000725", "600550", "601088", "600028",
]

HOLD_PERIODS = (5, 10, 20)
# 结构性止损扫描：单一倍数会产生误导（实测 2×ATR ≈ 3.3~6.8%，而 ATR% 本身 1.7~3.4%，
# 20 个交易日内几乎必然先触及 → "先触止损率"恒等于 100%，等于没信息）。
# 改为扫多档倍数，并直接给出 MAE 分布，让"止损该放多远"变成可读的数字。
STOP_ATR_MULTS = (2.0, 3.0, 4.0, 6.0, 8.0)
PROFIT_RR = 2.0            # 止盈 = 止损距离 × 该倍数（固定结构，不参与调参）


# ==================== 单标的扫描 ====================

REC_TYPES = ("signal", "vetoed", "below_bc", "nosignal")


def _classify_record(a_pass: bool, b_pass: bool, c_pass: bool,
                     gate_passed: bool) -> str:
    """把一根 K 线归入配对检验的四类之一。

    这三类"非信号"样本此前都被 `continue` 丢掉了，而它们正是"入场时机有没有用"
    的对照组（同一个股、同一交易日、只是系统没给信号 → 反事实）。

        signal    : 有信号且门控放行 —— 实盘会真的开仓
        vetoed    : A 或 B 通过，但被入口门控否决（门控的边际贡献看这一类）
        below_bc  : A 或 B 通过、门控放行，但 C（赔率）不通过 → 实盘不开仓
                    实测注意：门控的 RR_RATIO 否决项（[main.py] check_entry_gate ③）
                    与 C 判定同源 —— C 不过 ⇒ RR_RATIO 否决 ⇒ 门控不过 ⇒ 归入 vetoed。
                    故在 ENTRY_GATE_RR_RATIO 开启（默认）时本类**结构性不可达**，恒为 0；
                    仅当用 -o ENTRY_GATE_RR_RATIO_ENABLED=false 关掉该项时才可能出现。
        nosignal  : A、B 都没通过 —— 最宽的自然对照组
    """
    if a_pass or b_pass:
        if not gate_passed:
            return "vetoed"
        return "signal" if c_pass else "below_bc"
    return "nosignal"


def _eval_gate(a_pass: bool, a_msg: str, b_signals: list, c_pass: bool,
               c_info: dict, sub: pd.DataFrame, regime_today: dict,
               config: TradingConfig, symbol: str = "") -> dict:
    """入口门控单点求值（回测里必须与实盘同一套逻辑，见 check_entry_gate）。

    symbol 用于 ETF 识别：价格否决不适用于 ETF（见 check_entry_gate 的 SURVIVABILITY）。

    a_msg="" 时（A 未通过、调用方不持有通过说明）退回 `_is_fresh_breakout` 自行复算入场性质。

    门控内部一律求值、**只记录不筛选**：配对阵型需要区分"门控拦掉"与"门控放行"，
    而两者可以由同一份完整结果切开（gate_passed）。异常时保守放行（与改造前一致）。
    """
    try:
        return M.check_entry_gate(regime_today, a_pass, a_msg, b_signals,
                                  c_pass, c_info, sub, config, symbol=symbol)
    except Exception as e:
        logger.debug("门控异常: %s", e)
        return {"passed": True, "vetoes": [], "rules": [], "_error": True}


def scan_symbol(symbol: str, df: pd.DataFrame, config: TradingConfig,
                regime_by_date: dict, index_df: pd.DataFrame = None,
                warmup: int = 60, horizon: int = 20,
                vetoed_count: list = None, record_control: bool = False) -> list:
    """在一只标的上逐根重放，返回记录列表。

    vetoed_count: 单元素 list 作为可变计数器，累计"被入口门控拦掉"的样本数。

    record_control: 是否**额外**落盘"非信号"行（对照组）。
        False（默认）：只返回 signal 行 —— 与改造前**逐格一致**，零回归。
        True：额外返回 vetoed / below_bc / nosignal 三类行，供配对检验使用。
              每行带 `rec_type` 列区分，调用方用 `rec_type == "signal"` 取回原口径。

    ⚠️ 对照组不引入未来函数：`check_dimension_*` 全部只用 `df.iloc[:i+1]`，
    大盘指数已按当日切片注入（见下），与 signal 行走**完全相同**的计算路径。

    ⚠️ 无未来函数的两处关键处理：
      1. 个股指标：只用 df.iloc[:i+1]
      2. **大盘过滤**：check_dimension_b 在 ENABLE_MARKET_FILTER=True 时会自己去
         `check_market_environment` 取"当前"指数 —— 回测里那是未来数据。
         故这里按当日切片注入历史指数（只用该日及之前），并在扫描结束后还原。
    """
    records = []
    if vetoed_count is None:
        vetoed_count = [0]
    n = len(df)
    close = df["close"].to_numpy()
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    dates = df["date"]
    index_dates = index_df["date"] if index_df is not None else None

    import market_filter as MF
    original_fetch = MF._fetch_index

    try:
        for i in range(warmup, n - horizon):
            sub = df.iloc[:i + 1]
            cur_date = dates.iloc[i]

            # —— 大盘过滤按当日切片注入（避免未来函数）——
            if index_df is not None and index_dates is not None:
                pos = index_dates.searchsorted(cur_date, side="right")
                if pos > 0:
                    hist_idx = index_df.iloc[:pos].reset_index(drop=True)
                    MF._fetch_index = lambda *a, **k: hist_idx

            try:
                a_pass, a_msg = M.check_dimension_a(sub, config)
                b_pass, b_signals = M.check_dimension_b(sub, config)
                c_pass, c_info = M.check_dimension_c(sub, config)
            except Exception as e:  # 单根异常不该中断整轮回测
                logger.debug("扫描异常 %s i=%d: %s", symbol, i, e)
                continue

            is_candidate = bool(a_pass or b_pass)

            # —— 入口门控（与实盘同一套逻辑，保证回测口径 = 实盘口径）——
            # regime 由 regime_by_date 提供（按当日切片预计算，无未来函数）。
            regime_today = regime_by_date.get(cur_date.strftime("%Y-%m-%d"), {}) or {}

            if not is_candidate:
                # A、B 都没过 → 实盘不会开仓。默认直接丢弃（与改造前一致）；
                # 配对模式下必须保住这一行 —— 它是最宽的自然对照组。
                # 注意：门控仍要求值，用于把"被门控拦掉"与"门控放行"分开，
                # 但不消耗额外的数据请求（只用已切片的 sub）。
                if not record_control:
                    continue
                gate = _eval_gate(False, "", b_signals, c_pass, c_info, sub,
                                  regime_today, config, symbol=symbol)
                rec_type = _classify_record(False, False, c_pass,
                                            bool(gate.get("passed", True)))
            else:
                gate = _eval_gate(a_pass, a_msg, b_signals, c_pass, c_info, sub,
                                  regime_today, config, symbol=symbol)
                gate_passed = bool(gate.get("passed", True))
                rec_type = _classify_record(a_pass, b_pass, c_pass, gate_passed)
                if rec_type == "vetoed":
                    # 门控未通过 → 实盘会放弃这笔交易，回测里也不计入 signal。
                    # 仍然记录被否决的样本数，便于量化"门控拦掉了多少"。
                    vetoed_count[0] += 1
                if not gate_passed and not record_control:
                    continue
                if rec_type == "below_bc" and not record_control:
                    continue          # 改造前：C 不通过的行也不进统计
            gate_vetoes = [v["rule"] for v in gate.get("vetoes", [])]
            # 门控在全部路径上都会被求值（只记录不筛选），故恒为 True；
            # 门控内部异常时保守放行，用 _error 标记出来便于事后核对。
            gate_evaluated = True

            # 记录维度B 里究竟触发了哪些信号 —— 用于判断"该不该把某个信号降级为过滤器"。
            # 不能只看 B 的聚合结果：A✅B✅ 之所以最差，必须定位到是哪个 B 信号在拖累。
            b_flags = {
                "b_macd_cross": any("MACD金叉" in s for s in b_signals),
                "b_macd_red": any("MACD柱线翻红" in s for s in b_signals),
                "b_rsi_oversold": any("RSI超卖" in s for s in b_signals),
                "b_vol_surge": any("成交量放大" in s for s in b_signals),
            }
            # 关键归因字段（2026-09-28 加入）：
            # b_pass=True 但四个 b_* 全为 False 是**合法状态**，来源已实测确认 ——
            #   维度B 在「大盘环境通过」且未触发任何个股动量时也判定通过
            #   （关闭 ENABLE_MARKET_FILTER 后此类行数降为 0）。
            # 即：此时 B 是"被 A 单独触发、B 只是没反对"，而不是"个股动量触发"。
            # 单看 b_* 会把这两种情形混为一谈，故显式落一列。
            momentum_passed = bool(
                b_flags["b_macd_cross"] or b_flags["b_rsi_oversold"] or b_flags["b_vol_surge"])

            entry = close[i]
            if entry <= 0:
                continue

            atr_series = M.calc_atr(sub, config.ATR_PERIOD)
            atr = float(atr_series.iloc[-1]) if len(atr_series) else np.nan
            has_atr = np.isfinite(atr) and atr > 0

            # —— 量比的**连续值**（2026-09-28 加入）——
            # 原先只落 b_vol_surge 这个布尔结果，导致"扫 VOLUME_SURGE_RATIO 阈值"
            # 这类问题无法在既有数据上回答（布尔值丢掉了量比的大小信息）。
            # 口径必须与 main.check_dimension_b 完全一致：
            #   量比 = 当日成交量 / MA(成交量, VOLUME_MA_PERIOD)，阈值比较用**严格大于**。
            _vol_ma = M.calc_ma(sub["volume"], config.VOLUME_MA_PERIOD)
            _avg_vol = float(_vol_ma.iloc[-1]) if len(_vol_ma) else np.nan
            vol_ratio = (float(sub["volume"].iloc[-1]) / _avg_vol
                         if np.isfinite(_avg_vol) and _avg_vol > 0 else np.nan)
            # 信号日之前的 5 日收益（不含信号日）—— 用于诊断"追高/接刀"程度，
            # 且与 ret5（信号日之后）严格区分，避免未来函数。
            ret5_pre = (float(close[i] / close[i - 5] - 1) * 100
                        if i >= 5 and close[i - 5] > 0 else np.nan)

            # 前视窗口内：收益 / MFE / MAE / 多档止损触发
            fwd_close = close[i + 1: i + 1 + horizon]
            fwd_high = high[i + 1: i + 1 + horizon]
            fwd_low = low[i + 1: i + 1 + horizon]
            if len(fwd_close) < horizon:
                continue

            rets = {h: (fwd_close[h - 1] / entry - 1) * 100
                    for h in HOLD_PERIODS if h <= len(fwd_close)}
            mfe = (fwd_high.max() / entry - 1) * 100
            mae = (fwd_low.min() / entry - 1) * 100

            rec = {
                "symbol": symbol,
                "date": cur_date.strftime("%Y-%m-%d"),
                "close": entry,
                # rec_type 让"原口径"与"对照组"在同一张表里共存：
                #   df[df.rec_type == "signal"] 即改造前的全部内容。
                "rec_type": rec_type,
                "gate_evaluated": gate_evaluated,
                "momentum_passed": momentum_passed,
                "a_pass": bool(a_pass),
                "b_pass": bool(b_pass),
                "c_pass": bool(c_pass),
                "dim_passed": int(a_pass) + int(b_pass) + int(c_pass),
                "rr_ratio": c_info["ratio"],
                "regime": (regime_by_date.get(cur_date.strftime("%Y-%m-%d"), {}) or {}).get("regime", "UNKNOWN"),
                "regime_label": (regime_by_date.get(cur_date.strftime("%Y-%m-%d"), {}) or {}).get("label", ""),
                "mfe": mfe,
                "mae": mae,
                "atr_pct": (atr / entry * 100) if has_atr else np.nan,
                "vol_ratio": vol_ratio,
                "ret5_pre": ret5_pre,
                "gate_vetoes": ",".join(gate_vetoes),
                "n_vetoes": len(gate_vetoes),
                **b_flags,
                **{f"ret{h}": rets.get(h, np.nan) for h in HOLD_PERIODS},
            }

            # 多档止损：止损被触及 = 窗口内最低价跌破 入场 − k×ATR
            for k in STOP_ATR_MULTS:
                key = f"stop{k:g}"
                if not has_atr:
                    rec[key] = np.nan
                    continue
                stop = entry - k * atr
                rec[key] = bool(fwd_low.min() <= stop)
            records.append(rec)
    finally:
        MF._fetch_index = original_fetch      # 必须还原，否则污染后续调用
    return records


# ==================== 汇总统计 ====================

def _stats(g: pd.DataFrame) -> dict:
    out = {"n": len(g)}
    for h in HOLD_PERIODS:
        col = f"ret{h}"
        if col in g:
            s = g[col].dropna()
            out[f"ret{h}"] = s.mean() if len(s) else np.nan
            out[f"win{h}"] = (s > 0).mean() * 100 if len(s) else np.nan
    out["mfe"] = g["mfe"].mean()
    out["mae"] = g["mae"].mean()
    out["mae_med"] = g["mae"].median()
    out["atr_pct"] = g["atr_pct"].mean() if "atr_pct" in g else np.nan
    # 多档止损触发率（率越低 = 该止损越宽松）
    for k in STOP_ATR_MULTS:
        col = f"stop{k:g}"
        if col in g:
            valid = g[col].dropna()
            out[col] = valid.mean() * 100 if len(valid) else np.nan
    return out


def print_table(title: str, rows: list, key_col: str = "分组") -> None:
    print(f"\n{title}")
    header = (f"  {key_col:<22s}{'样本':>6s}{'5日均%':>8s}{'10日均%':>9s}{'20日均%':>9s}"
              f"{'胜率10':>8s}{'MFE%':>7s}{'MAE中位':>8s}")
    print(header)
    print("  " + "-" * (len(header) - 2))
    for name, st in rows:
        f = lambda v, p=2: ("—" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:.{p}f}")
        print(f"  {name:<22s}{st['n']:>6d}{f(st.get('ret5')):>8s}{f(st.get('ret10')):>9s}"
              f"{f(st.get('ret20')):>9s}{f(st.get('win10'),1):>8s}{f(st.get('mfe')):>7s}"
              f"{f(st.get('mae_med')):>8s}")


def print_stop_scan(df: pd.DataFrame, title: str = "【止损距离扫描】") -> None:
    """给出 MAE 分布 + 多档 ATR 止损的触发率。

    为什么需要这个：单一"止损率"很容易误导 —— 2×ATR 止损（约 3~7%）在 20 个交易日里
    几乎必然被触及，触发率恒等于 100%，看不出任何信息。
    扫多档倍数才能回答"止损该放多远"。
    """
    print(f"\n{title}")
    maes = df["mae"].dropna()
    if maes.empty:
        print("  （无数据）")
        return
    print(f"  MAE（最大不利波动）分位数: "
          f"P10={maes.quantile(.10):.2f}%  P25={maes.quantile(.25):.2f}%  "
          f"中位={maes.median():.2f}%  P75={maes.quantile(.75):.2f}%  "
          f"最差={maes.min():.2f}%")
    print(f"  平均 ATR% = {df['atr_pct'].mean():.2f}%"
          if "atr_pct" in df else "")
    parts = []
    for k in STOP_ATR_MULTS:
        col = f"stop{k:g}"
        if col in df:
            v = df[col].dropna()
            if len(v):
                parts.append(f"{k:g}×ATR({k * df['atr_pct'].mean():.1f}%)={v.mean() * 100:.0f}%")
    if parts:
        print("  止损触发率: " + "  ".join(parts))


def report(records: list, print_tables: bool = True) -> dict:
    df = pd.DataFrame(records)
    if df.empty:
        print("⚠️  没有任何信号记录（样本为空）")
        return {}

    print(f"\n{'=' * 104}")
    print(f"回测结果：{df['symbol'].nunique()} 只标的，{len(df)} 条信号，"
          f"区间 {df['date'].min()} ~ {df['date'].max()}")
    print(f"{'=' * 104}")

    if print_tables:
        print_table("【总体】", [("全部信号", _stats(df))])
        print_stop_scan(df, "【总体 · 止损距离扫描】")

        rows = []
        for a in (True, False):
            for b in (True, False):
                sub = df[(df["a_pass"] == a) & (df["b_pass"] == b)]
                if len(sub):
                    rows.append((f"A={'✅' if a else '❌'} B={'✅' if b else '❌'}", _stats(sub)))
        print_table("【按维度通过情况】", rows, "维度组合")

        rows = []
        for r, sub in df.groupby("regime"):
            label = sub["regime_label"].iloc[0]
            rows.append((f"{r}({label})", _stats(sub)))
        rows.sort(key=lambda x: -x[1]["n"])
        print_table("【按市场状态 regime】", rows, "市场状态")
        print_stop_scan(df, "【全样本 · 止损距离扫描（用于定止损）】")

        rows = []
        for (r, a), sub in df.groupby(["regime", "a_pass"]):
            if len(sub) >= 5:
                rows.append((f"{r} + A{'✅' if a else '❌'} ({len(sub)})", _stats(sub)))
        if rows:
            print_table("【市场状态 × 维度A】(仅样本≥5)", rows, "状态×维度")

        # 预判检验：下跌/恐慌趋势里的逆势信号
        mask = df["regime"].isin(["TREND_DOWN", "PANIC_DOWN"]) & df["b_pass"] & ~df["a_pass"]
        sub = df[mask]
        if len(sub):
            print_table("【预判检验】下跌/恐慌市中的 B 类信号（典型逆势抄底）",
                        [(f"{len(sub)} 条", _stats(sub))], "说明")

        # 最大回撤式风险：有多少信号会先亏 8% 以上
        deep = (df["mae"] <= -8).mean() * 100
        print(f"\n【风险提示】MAE ≤ −8% 的信号占比: {deep:.1f}%"
              f"（这些信号用任何 8% 以内的止损都会被扫掉）")
    return {"trades": df}


# ==================== 主流程 ====================

def build_regime_timeline(index_df: pd.DataFrame, warmup: int = 70) -> dict:
    """按交易日预计算市场状态（同日只算一次）。

    严格只用"该日及之前"的指数数据：对每个日期取 index_df.iloc[:k+1] 交给
    regime_detector（其内部只用滚动窗口，已被 look-ahead 审计确认）。

    注意：必须保存/还原 regime_detector._fetch_index_data ——
    否则这个用于模拟历史取数的 lambda 会泄漏到后续代码，让"当前"判定也拿到历史切片。
    """
    import regime_detector as RD
    original_fetch = RD._fetch_index_data
    cfg = TradingConfig()
    cfg.REGIME_DEBUG = False
    timeline = {}
    try:
        for k in range(warmup, len(index_df)):
            sub = index_df.iloc[:k + 1].reset_index(drop=True)
            RD._fetch_index_data = lambda _cfg, _df=sub: _df
            # 必须清缓存：regime_detector 的进程内缓存键是 (基准, 今天日期)，
            # 而这里对**同一天**反复注入不同的历史切片。
            # 不清缓存 → 所有交易日都会返回第 k=warmup 天的结果
            # （实测把 867 个交易日全判成 RANGE_HIGH，静默毁掉整个状态分层）。
            RD._REGIME_CACHE.clear()
            try:
                r = RD.detect_regime(cfg)
            except Exception:
                continue
            if r.get("available"):
                key = index_df["date"].iloc[k].strftime("%Y-%m-%d")
                timeline[key] = {"regime": r["regime"], "label": r["label"]}
    finally:
        RD._fetch_index_data = original_fetch
        RD._REGIME_CACHE.clear()
    return timeline


def make_config(overrides: list | None = None) -> TradingConfig:
    """构造回测用配置：默认档 + 可选 -o 字段覆盖。

    必须与 main.py 用同一套 apply_overrides，否则"回测口径 = 实盘口径"的约定会破。
    """
    cfg = TradingConfig()
    if overrides:
        from config import apply_overrides
        apply_overrides(cfg, list(overrides), {})
    # 回测下的调试输出一律关闭（逐根重放会刷屏，且拖慢速度）
    cfg.PULLBACK_DEBUG = False
    cfg.MOMENTUM_DEBUG = False
    cfg.MARKET_DEBUG = False
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description="P0 回测：三维度信号 × 市场状态分层")
    ap.add_argument("--symbols", default="", help="逗号分隔的 6 位代码；留空则用 --universe 抽样的股票池")
    ap.add_argument("--universe", action="store_true", default=True,
                    help="按等距抽样构建股票池（默认开启；含区间内退市股票以消除生存者偏差）")
    ap.add_argument("--n", type=int, default=300, help="等距抽样数量（默认 300）")
    ap.add_argument("--offset", type=int, default=0, help="等距抽样相位，用于换一批样本做稳健性检验")
    ap.add_argument("--refresh-universe", action="store_true", help="强制重建股票池清单")
    ap.add_argument("--legacy-31", action="store_true",
                    help="用改造前的 31 只手挑样本（仅用于对照，含严重生存者偏差）")
    ap.add_argument("--start", default="2023-09-01", help="起始日 YYYY-MM-DD")
    ap.add_argument("--end", default="", help="结束日（留空=今天）")
    ap.add_argument("--min-interval", type=float, default=0.6, help="取数间隔秒（防限流）")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存强制重拉（慎用）")
    ap.add_argument("--cache-stats", action="store_true", help="只看缓存统计")
    ap.add_argument("--out", default="", help="信号明细 CSV 输出路径")
    ap.add_argument("--paired", action="store_true",
                    help=("配对模式：额外落盘'非信号'对照组（rec_type != signal）到 --out，\n"
                          "  用于回答'入场时机（A/B/C/门控）相对不筛选是否有增量'。\n"
                          "  屏幕统计仍只吃 signal 行，与原口径逐格一致；\n"
                          "  CSV 里用 rec_type 列区分（signal/vetoed/below_bc/nosignal）。\n"
                          "  配对分析本身见 paired_stats.py（步骤 2）。"))
    ap.add_argument("-o", "--override", action="append", dest="overrides", default=None,
                    metavar="KEY=VALUE",
                    help=("单字段覆盖（可重复），语法与 main.py 的 -o 一致。\n"
                          "  例: -o MOMENTUM_TRIGGERS=MACD\n"
                          "  例: -o ENABLE_MARKET_FILTER=false\n"
                          "  例: -o MIN_RR_RATIO=2.0\n"
                          "  为什么回测需要它：验证\"禁用某个触发源后增量是否回到 0\"这类\n"
                          "  假设时，必须能在**不重新取数**的前提下换配置重放。"))
    ap.add_argument("--paired-horizon", type=int, default=20,
                    help="配对模式统一的前视窗口（默认 20，取最大持有期以保证各 h 口径可比）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                            datefmt="%H:%M:%S")
    else:
        logging.basicConfig(level=logging.WARNING)

    import data_layer as D

    if args.cache_stats:
        print("【本地缓存统计】")
        D.print_cache_stats()
        return 0

    # —— 样本选择：等距抽样（含退市股）优先 ——
    universe_info = None
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        print(f"\n【0/4】样本来源: 手动指定 {len(symbols)} 只")
    elif args.legacy_31:
        symbols = DEFAULT_SYMBOLS
        print(f"\n【0/4】样本来源: 旧 31 只手挑池 ⚠️ 含严重生存者偏差，仅用于对照")
    else:
        import universe as U
        print("\n【0/4】构建股票池（等距抽样，含区间内退市股票）")
        universe_info = U.load_or_build(start=args.start, n=args.n,
                                       offset=args.offset, refresh=args.refresh_universe)
        U.print_universe_summary(universe_info)
        symbols = universe_info["symbols"]

    end = args.end or datetime.now().strftime("%Y-%m-%d")

    print("=" * 100)
    print(f"P0 回测：{len(symbols)} 只标的 × {args.start} ~ {end}")
    print("=" * 100)
    print("\n【1/4】取数（优先本地缓存，未命中才触网；间隔 %.1fs 防限流）" % args.min_interval)
    data: dict = {}
    for i, sym in enumerate(symbols, 1):
        try:
            df = D.fetch_history(sym, args.start, end,
                                 min_interval=args.min_interval, force=args.refresh)
            data[sym] = df
            print(f"  {i:3d}/{len(symbols)} {sym}: {len(df)} 根  "
                  f"{df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}", flush=True)
        except Exception as e:
            print(f"  {i:3d}/{len(symbols)} {sym}: ❌ {type(e).__name__}: {str(e)[:60]}", flush=True)

    if not data:
        print("\n❌ 无任何可用数据，回测终止")
        return 1

    print("\n【2/4】预计算市场状态时间线（同一天只算一次，仅用当日及之前指数数据）")
    from market_filter import _fetch_index
    # 指数历史长度：覆盖回测区间 + 60 根预热（MA60/ADX/ATR分位需要）
    span_days = (pd.Timestamp(end) - pd.Timestamp(args.start)).days
    idx_bars = int(span_days * 0.72) + 130          # ≈ 交易日数 + 预热余量
    idx = _fetch_index("sh000001", idx_bars, 45, prefer="baostock")
    if idx is None or idx.empty:
        print("  ⚠️ 指数取数失败 → 市场状态全部记 UNKNOWN（分层将失去意义）")
        regime_timeline = {}
    else:
        idx = idx[idx["date"] <= pd.Timestamp(end)].reset_index(drop=True)
        regime_timeline = build_regime_timeline(idx)
        from collections import Counter
        c = Counter(v["regime"] for v in regime_timeline.values())
        print(f"  指数 {len(idx)} 根（{idx['date'].iloc[0].date()} ~ {idx['date'].iloc[-1].date()}）")
        print(f"  覆盖 {len(regime_timeline)} 个交易日，状态分布: {dict(c)}")
        # 健全性护栏：真实市场状态必然多种并存。若只剩一种，几乎一定是
        # 缓存/注入出了问题（曾实测把 867 天全判成 RANGE_HIGH）—— 显式报警而非静默出结果。
        if len(c) == 1 and len(regime_timeline) > 5:
            print(f"  ⚠️  状态分布只有一种（{list(c)[0]}），极可能是缓存未重置导致的历史状态被覆盖；"
                  f"\n      分层统计不可信，请先修复取数注入逻辑。")

    print("\n【3/4】逐根重放信号（无未来函数）")
    all_records = []
    vetoed_total = 0
    scan_horizon = args.paired_horizon if args.paired else 20
    for i, (sym, df) in enumerate(data.items(), 1):
        cfg = make_config(args.overrides)
        vc = [0]
        recs = scan_symbol(sym, df, cfg, regime_timeline, index_df=idx,
                           horizon=scan_horizon, vetoed_count=vc,
                           record_control=args.paired)
        vetoed_total += vc[0]
        all_records.extend(recs)
        n_sig = sum(1 for r in recs if r["rec_type"] == "signal")
        extra = "" if not args.paired else f"（对照组 {len(recs) - n_sig} 行）"
        print(f"  {i:3d}/{len(data)} {sym}: {n_sig} 条信号{extra}"
              f"（门控拦掉 {vc[0]} 条）", flush=True)
    total_raw = sum(1 for r in all_records if r["rec_type"] == "signal") + vetoed_total
    if total_raw:
        print(f"\n  ⛔ 入口门控拦截: {vetoed_total}/{total_raw} 条"
              f"（{vetoed_total / total_raw * 100:.0f}%）；"
              f"剩余 {total_raw - vetoed_total} 条进入统计")
        print("     说明：门控 = 震荡市禁突破 + RSI极端超买 + 赔率不足，与实盘同一套判定。")

    if args.paired:
        from collections import Counter
        c = Counter(r["rec_type"] for r in all_records)
        print("\n  📊 配对样本构成（--paired）：")
        for t in REC_TYPES:
            print(f"     {t:<10}{c.get(t, 0):>8,} 行")
        print(f"     {'合计':<10}{len(all_records):>8,} 行")

    print("\n【4/4】统计")
    # 屏幕统计只吃 signal 行：保证与原口径逐格一致（对照组只在 --out 的 CSV 里）。
    signal_records = ([r for r in all_records if r["rec_type"] == "signal"]
                      if args.paired else all_records)
    result = report(signal_records)
    trades = result.get("trades")
    if trades is not None and not trades.empty and args.out:
        # --paired 时写全量（含对照组），否则写与原口径相同的 signal 明细
        out_df = pd.DataFrame(all_records) if args.paired else trades
        out_df.to_csv(args.out, index=False)
        print(f"\n记录明细已写入: {args.out}"
              f"（{len(out_df)} 行{'，含对照组' if args.paired else ''}）")
        if args.paired:
            print("  → 下一步：paired_stats.py 做逐日配对差 + 分块 bootstrap")
    elif args.paired and not args.out:
        print("\n  ⚠️ --paired 已开启但未指定 --out，对照组不会被落盘。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
