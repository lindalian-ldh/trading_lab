#!/usr/bin/env python3
"""基准指数选择：按**实际相关性**为标的选择最合适的指数作为"市场"参照。

为什么不是"按板块换基准"：

    直觉是"创业板股票应该看创业板指、深市股票看深证成指"。实测否定了它。
    对 20 只标的分别算"与哪个指数相关性最高"，最优基准分布是：

        上证综指 5 只 / 沪深300 3 只 / 创业板指 1 只 / 深证成指 1 只

    典型反例（250 日收益率相关性）：

        300750 宁德时代  vs上证 0.40  vs深证 0.48  vs创业板指 **0.53**  ← 创业板指更好
        300015 爱尔眼科  vs上证 **0.38** vs深证 0.20  vs创业板指 0.12   ← 用"自己的"指数最差
        300059 东方财富  vs上证 0.63  vs深证 0.57  vs创业板指 0.51   ← 上证最好
        000001 平安银行  vs上证 **0.16** vs深证 -0.13 vs创业板指 -0.21  ← 上证最好

    同为创业板股票，最优基准却有三种答案 ⇒ **按板块硬套会选错一半**。

结论与做法：

    1. 默认保持 `sh000001`（上证综指）—— 实测它是所有候选里
       **中位相关性最高**的（上证 +0.375 / 沪深300 +0.333 / 深证 +0.217 / 创业板指 +0.151），
       作为"国内A股整体系统性风险"的代理最稳。
    2. 想精确对齐，用 `--index auto`：按**过去 N 日收益率相关性**为每只标的挑最相关的指数。
    3. **必须清楚**：个股与任何指数的相关性只有 0.15~0.38，而 ETF 是 0.80~0.94。
       所以对个股而言，维度E/大盘过滤天生偏噪声，它只是**参考信息**（工具本身也没让它否决 A/B/C）。
       `--index auto` 能把这个参考做得更贴合，但**不会把弱相关变成强相关**。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# 候选基准（均已在 baostock 实测可取；科创50 sh000688 在 baostock 缺失，故不列入）
CANDIDATES: list = [
    ("sh000001", "上证综指"),
    ("sh000300", "沪深300"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sz399005", "中小板指"),
    ("sh000905", "中证500"),
    ("sh000852", "中证1000"),
    ("sh000016", "上证50"),
    ("sh000010", "上证180"),
]

DEFAULT_INDEX = "sh000001"

# 相关性低于该值就不显示"最优基准"意义（仅提示参考性弱）
WEAK_CORR_THRESHOLD = 0.30

_INDEX_CACHE_TTL_HOURS = 12


# ==================== 指数行情本地缓存 ====================

def _cache_dir() -> Path:
    p = Path(__file__).resolve().parents[2] / "data/cache/calc_indicators/benchmark"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _index_returns(index_code: str, days: int = 300, min_interval: float = 0.6) -> Optional[pd.Series]:
    """取指数日线收益率序列（本地缓存 12 小时，避免反复触网/被限流）。"""
    from market_filter import _fetch_index

    path = _cache_dir() / f"{index_code}.csv"
    if path.exists():
        age_h = (datetime.now().timestamp() - path.stat().st_mtime) / 3600
        if age_h < _INDEX_CACHE_TTL_HOURS:
            try:
                df = pd.read_csv(path, parse_dates=["date"])
                if len(df) >= 60:
                    return df.set_index("date")["close"].pct_change()
            except Exception:
                pass

    df = _fetch_index(index_code, days, 45, prefer="baostock")
    if df is None or df.empty:
        # 取数失败时回退旧缓存（哪怕过期），保证不中断主流程
        if path.exists():
            try:
                old = pd.read_csv(path, parse_dates=["date"])
                logger.warning("[基准] %s 取数失败，回退过期缓存（%d 根）", index_code, len(old))
                return old.set_index("date")["close"].pct_change()
            except Exception:
                pass
        return None
    try:
        df[["date", "close"]].to_csv(path, index=False)
    except Exception:
        pass
    return df.set_index("date")["close"].pct_change()


# ==================== 相关性选择 ====================

def select_benchmark(symbol_returns: pd.Series, lookback: int = 250,
                     min_interval: float = 0.6) -> dict:
    """为标的挑选最相关的候选指数。

    Args:
        symbol_returns: 标的日收益率序列（index 为日期）
        lookback: 相关性回看根数（默认 250 约一年）

    Returns:
        dict: {
            'index': 选中的指数代码（无有效数据时回退 DEFAULT_INDEX）,
            'name': 指数名,
            'corr': 选中相关性 / None,
            'ranking': [(code, name, corr), ...] 降序,
            'weak': 最强相关性是否偏弱（< WEAK_CORR_THRESHOLD）,
            'fallback': 是否用了默认值兜底,
        }
    """
    ranking = []
    for code, name in CANDIDATES:
        ir = _index_returns(code, min_interval=min_interval)
        if ir is None or len(ir) < 60:
            continue
        j = pd.concat([symbol_returns.rename("a"), ir.rename("b")], axis=1, sort=True).dropna().tail(lookback)
        if len(j) < 60:
            continue
        c = j["a"].corr(j["b"])
        if pd.notna(c):
            ranking.append((code, name, float(c), len(j)))

    if not ranking:
        return {"index": DEFAULT_INDEX, "name": "上证综指", "corr": None,
                "ranking": [], "weak": True, "fallback": True}

    ranking.sort(key=lambda x: -x[2])
    best_code, best_name, best_corr, best_n = ranking[0]
    return {
        "index": best_code, "name": best_name, "corr": best_corr,
        "ranking": ranking, "weak": best_corr < WEAK_CORR_THRESHOLD, "fallback": False,
        "samples": best_n,
    }


def print_benchmark_selection(sel: dict, top_n: int = 4) -> None:
    """打印基准选择结果（让选择过程可对账）。"""
    if sel.get("fallback"):
        print(f"  ⚠️  基准自动选择失败（指数取数不可用），回退默认 {DEFAULT_INDEX}（上证综指）")
        return
    print(f"  🎯 自动选择基准: {sel['index']}（{sel['name']}）  相关性={sel['corr']:+.3f}"
          f"（{sel.get('samples', '?')} 个样本）"
          f"{'  ⚠️ 相关性偏弱' if sel['weak'] else ''}")
    others = sel["ranking"][1:top_n]
    if others:
        txt = "  ".join(f"{n} {c:+.2f}" for _, n, c, _ in others)
        print(f"     其他候选: {txt}")
    if sel["weak"]:
        print(f"     ⚠️ 最强相关性仅 {sel['corr']:+.2f}（阈值 {WEAK_CORR_THRESHOLD}）："
              f"该标的与指数关联很弱，维度E/大盘过滤的参考价值有限。")
        print(f"        （对照：ETF 与标的指数相关性通常 0.80~0.94）")
