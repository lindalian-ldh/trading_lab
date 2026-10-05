"""Phase 2 · 主题级择时的三个独立布尔层 —— 严格实现 P0.5 冻结基准 v1。

**规范来源**：[`scripts/增强方案v2-板块轮动面板与ETF择时.md`](../scripts/增强方案v2-板块轮动面板与ETF择时.md)
的「P0.5 冻结基准 v1」一节。本模块的**任何参数改动都必须在方案文档记一条修订记录**，
不许在这里悄悄调。

## 三个层（各自可独立调用）

| 层 | 语义 | 函数 |
|---|---|---|
| **L1** | 结构破坏（**看涨反转**：不再创新低 + 突破前一反弹高点 + 1 根 K 线确认） | :func:`l1_structure_break` |
| **L2** | 均线转向（MA10 上穿 MA30 且 MA30 近 5 根回归斜率 ≥ 0） | :func:`l2_ma_turn` |
| **RS** | 相对强度（比价 20 日走强 **且** 价格 > MA20 —— 必须绑定绝对价格，防坑 3） | :func:`rs_strength` |

**❌ 不实现"量能放大"条件**（方案文档第五章坑 2：放量阈值 1.5→3.0 单调恶化，先验为负）。

## 口径约定（与 `scripts/audit_regime_hold.py` 一致）

1. **每层返回布尔掩码（mask）**，不是"事件日"。L1/L2/RS 都是**状态**：
   L2 的"上穿"当日本来就只有 1 天，L1/RS 的状态可能持续多日。
2. **频次 = 片段数（episodes）**，不是天数：相邻（间隔 ≤3 日）的 True 合并为 1 段。
   这正是"一年 3~5 次"的计数口径（`audit_regime_hold.py` 的 `episodes()`）。
3. **效应 = 掩码内 `fwd_ret` 均值 − 非掩码 `fwd_ret` 均值**（pp），入场为**次日开盘**。
   对"仓位中枢调档"而言，"在这个状态下持有"的平均收益才是正确的统计量。
4. **无未来函数**：所有条件只用截至 t 日的数据；唯一的前视是"次日确认"，
   在 :func:`l1_structure_break` 里通过 `shift` 把信号**落到确认日**（不是提前到突破日）。

## 层组合（P0.5 冻结基准 §④）

**同日 AND**：`L1+L2 = L1(s) ∧ L2(s)`，`L1+L2+RS = L1(s) ∧ L2(s) ∧ RS(s)`。
若联合频次 < 2 次/年 ⇒ 按 kill criterion 3 退化为"只观察"，
**不允许**改成"N 日内先后出现"来救频次。
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ====================================================================
# 冻结参数（P0.5 冻结基准 v1）—— 改动必须记修订记录
# ====================================================================

K_SWING = 5              # ① 分型摆动点确认窗口 k
L1_LOW_LOOKBACK = 20     # ① "不再创新低"回看根数（前 20 日最低收盘）
L1_CONFIRM_BARS = 1      # ① 确认根数
MA_FAST = 10             # ② MA10
MA_SLOW = 30             # ② MA30
MA_SLOPE_WINDOW = 5      # ② MA30 斜率回归窗口
MA_SLOPE_MIN = 0.0       # ② 斜率阈值（≥ 0）
RS_WINDOW = 20           # ③ 比价窗口
RS_MA_WINDOW = 20        # ③ 绝对价格绑定用的 MA20
EPISODE_GAP = 3          # 频次计数：间隔 ≤3 日的 True 合并为同一片段

#: 各层的 `(fast, slow)` 覆盖（None = 用默认）；仅供审计脚本标注用
LAYER_SPEC = {
    "L1": {"k": K_SWING, "low_lookback": L1_LOW_LOOKBACK, "confirm_bars": L1_CONFIRM_BARS},
    "L2": {"ma_fast": MA_FAST, "ma_slow": MA_SLOW,
           "slope_window": MA_SLOPE_WINDOW, "slope_min": MA_SLOPE_MIN},
    "RS": {"window": RS_WINDOW, "ma_window": RS_MA_WINDOW},
}


class ThemeTimingError(ValueError):
    """输入数据不满足口径要求（列缺失、长度不足、日期未对齐等）。"""


# ====================================================================
# 输入规范化
# ====================================================================

REQUIRED_COLUMNS = ("date", "open", "high", "low", "close")


def _prepare(df: pd.DataFrame, what: str) -> pd.DataFrame:
    """规范化：date 升序去重、数值列转 float、校验必需列。"""
    if df is None or len(df) == 0:
        raise ThemeTimingError(f"{what} 为空")
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ThemeTimingError(f"{what} 缺少列 {missing}（需要 {list(REQUIRED_COLUMNS)}）")
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out = out.dropna(subset=["date"])
    for c in ("open", "high", "low", "close"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=["close"])
    out = out.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    return out.reset_index(drop=True)


# ====================================================================
# L1 · 结构破坏（看涨反转）
# ====================================================================

def swing_points(df: pd.DataFrame, k: int = K_SWING) -> pd.DataFrame:
    """分型摆动点（P0.5 §①：k=5，**严格**极值，右侧 k 根走完才确认）。

    第 i 根是摆动高点 ⟺ ``high_i`` 严格大于 ``[i-k, i+k]`` 内其余 2k 根的 ``high``。
    摆动低点同理取最小。⇒ 摆动点在**第 i+k 根收盘时**才被确认。

    Returns:
        与 ``df`` 等长的 DataFrame，列 ``swing_high`` / ``swing_low``（布尔，标记在**端点 i** 上）
        与 ``high_price`` / ``low_price``（端点的极值价格；非端点为 NaN）。
    """
    d = _prepare(df, "swing_points 输入")
    n = len(d)
    high = d["high"].to_numpy(float)
    low = d["low"].to_numpy(float)
    k = int(k)

    is_sh = np.zeros(n, dtype=bool)
    is_sl = np.zeros(n, dtype=bool)
    if n >= 2 * k + 1:
        for i in range(k, n - k):
            others_h = np.concatenate([high[i - k:i], high[i + 1:i + k + 1]])
            if high[i] > others_h.max():
                is_sh[i] = True
            others_l = np.concatenate([low[i - k:i], low[i + 1:i + k + 1]])
            if low[i] < others_l.min():
                is_sl[i] = True

    return pd.DataFrame({
        "date": d["date"],
        "swing_high": is_sh,
        "swing_low": is_sl,
        "high_price": np.where(is_sh, high, np.nan),
        "low_price": np.where(is_sl, low, np.nan),
    })


def swing_high_asof(df: pd.DataFrame, k: int = K_SWING) -> pd.Series:
    """每个交易日 t 上「**截至 t 日已确认的**最近一个摆动高点」的价格（无则 NaN）。

    确认时点 = 端点 i + k。这是 L1「突破前一反弹高点」用的 ``H_last``。
    """
    d = _prepare(df, "swing_high_asof 输入")
    sp = swing_points(d, k=k)
    n = len(d)
    conf = np.full(n, np.nan)
    idx = np.where(sp["swing_high"].to_numpy(bool))[0]
    for i in idx:
        c = i + int(k)
        if c < n:
            conf[c] = float(sp["high_price"].iloc[i])   # 在**确认日**才可见
    return pd.Series(conf, index=d.index, name="h_last").ffill()


def l1_state(df: pd.DataFrame,
             k: int = K_SWING,
             low_lookback: int = L1_LOW_LOOKBACK) -> pd.Series:
    """L1 的**原始状态**（未做"次日确认"）—— ``close > H_last`` ∧ ``不再创新低``。

    :func:`l1_structure_break` 用它算出正式掩码；单独暴露出来是为了让观察哨能显示
    "**条件今天已满足，但按冻结规则还需下一根 K 线确认**"这种状态。
    """
    d = _prepare(df, "l1_state 输入")
    close = d["close"]
    h_last = swing_high_asof(d, k=k)
    not_new_low = close > close.shift(1).rolling(int(low_lookback),
                                                 min_periods=int(low_lookback)).min()
    st = (close > h_last) & not_new_low
    st.index = d.index
    st.name = "L1_state"
    return st


def l1_pending(df: pd.DataFrame, **kwargs) -> bool:
    """最后一根 K 线上「条件已满足、但还没被下一根确认」⇒ True。"""
    st = l1_state(df, **{k: v for k, v in kwargs.items()
                         if k in ("k", "low_lookback")})
    if len(st) == 0:
        return False
    return bool(st.iloc[-1])


def l1_structure_break(df: pd.DataFrame,
                       k: int = K_SWING,
                       low_lookback: int = L1_LOW_LOOKBACK,
                       confirm_bars: int = L1_CONFIRM_BARS) -> pd.Series:
    """**L1 结构破坏（看涨反转）** 布尔掩码 —— P0.5 冻结基准 §①。

    状态：``close_t > H_last`` ∧ ``close_t > min(close_{t-lookback} … close_{t-1})``。
    确认：次日收盘**仍站上** t 日的 ``H_last``；**信号落到确认日**（无未来函数）。

    ⚠️ **方向是看涨**（下跌结构被破坏 ⇒ 调升仓位中枢）。
    禁止实现成"结构破坏 ⇒ 看跌减仓"（P0.5 修订记录 #1）。
    """
    d = _prepare(df, "l1_structure_break 输入")
    close = d["close"]
    h_last = swing_high_asof(d, k=k)
    state = l1_state(d, k=k, low_lookback=low_lookback)

    # 次日确认：close_{t+1} > H_last(t)；再把信号移到确认日 t+1
    confirmed_at_next = (state & (close.shift(-1) > h_last)).fillna(False)
    mask = confirmed_at_next.shift(int(confirm_bars), fill_value=False).astype(bool)
    mask.index = d.index
    mask.name = "L1"
    return mask


# ====================================================================
# L2 · 均线转向
# ====================================================================

def _ols_slope(y: np.ndarray) -> float:
    """OLS 斜率（x = 0…n-1）。用于 MA30 的"由负转平"判定。"""
    w = len(y)
    if w < 2:
        return np.nan
    x = np.arange(w, dtype=float)
    xm = x.mean()
    denom = float(((x - xm) ** 2).sum())
    if denom == 0:
        return np.nan
    return float(((x - xm) * (y - y.mean())).sum() / denom)


def l2_ma_turn(df: pd.DataFrame,
               fast: int = MA_FAST,
               slow: int = MA_SLOW,
               slope_window: int = MA_SLOPE_WINDOW,
               slope_min: float = MA_SLOPE_MIN) -> pd.Series:
    """**L2 均线转向** 布尔掩码 —— P0.5 冻结基准 §②。

    ``MA_fast`` 上穿 ``MA_slow``（前一日 ≤、当日 >）**且** ``MA_slow`` 在最近
    ``slope_window`` 根上的 OLS 斜率 ≥ ``slope_min``。
    """
    d = _prepare(df, "l2_ma_turn 输入")
    close = d["close"]
    ma_f = close.rolling(int(fast), min_periods=int(fast)).mean()
    ma_s = close.rolling(int(slow), min_periods=int(slow)).mean()

    cross = (ma_f.shift(1) <= ma_s.shift(1)) & (ma_f > ma_s)
    slope = ma_s.rolling(int(slope_window), min_periods=int(slope_window)).apply(
        _ols_slope, raw=True)
    mask = (cross & (slope >= float(slope_min))).fillna(False).astype(bool)
    mask.name = "L2"
    return mask


# ====================================================================
# RS · 相对强度
# ====================================================================

def _align_closes(theme: pd.DataFrame, anchor: pd.DataFrame) -> pd.DataFrame:
    """按**共同交易日**内连接对齐两个收盘价序列（不做 ffill，避免用陈旧锚价）。"""
    a = _prepare(theme, "rs_strength 主题序列")[["date", "close"]].rename(
        columns={"close": "close_theme"})
    b = _prepare(anchor, "rs_strength 锚序列")[["date", "close"]].rename(
        columns={"close": "close_anchor"})
    m = a.merge(b, on="date", how="inner").sort_values("date").reset_index(drop=True)
    if len(m) < 2:
        raise ThemeTimingError(
            "主题指数与宽基锚**没有共同交易日** —— 无法计算比价（请检查代码是否取错标的）")
    dropped = len(a) + len(b) - 2 * len(m)
    if dropped > 0:
        logger.debug("rs_strength 对齐丢弃 %d 行（主题 %d / 锚 %d → 共同 %d）",
                     dropped, len(a), len(b), len(m))
    return m


def rs_strength(theme: pd.DataFrame,
                anchor: pd.DataFrame,
                window: int = RS_WINDOW,
                ma_window: int = RS_MA_WINDOW) -> pd.Series:
    """**RS 相对强度** 布尔掩码 —— P0.5 冻结基准 §③。

    比价 ``R = close_theme / close_anchor``；走强 ⟺ ``R_t > R_{t-window}``；
    **且** 绝对价格 ``close_theme_t > MA(ma_window)_theme_t``（防方案文档坑 3：
    主题与宽基同跌、主题跌得少 ⇒ RS 上升但价格仍下行 ⇒ 假阳性）。

    Returns:
        索引为**主题序列的日期**的布尔 Series（对齐后缺失的日期为 False，
        **不 fill 比价**，避免用陈旧锚价造出假的"走强"）。
    """
    m = _align_closes(theme, anchor)
    close = m["close_theme"]
    ratio = close / m["close_anchor"]
    strong = ratio > ratio.shift(int(window))
    above = close > close.rolling(int(ma_window), min_periods=int(ma_window)).mean()
    aligned = (strong & above).fillna(False).astype(bool)

    t = _prepare(theme, "rs_strength 主题序列")
    lookup = pd.Series(aligned.to_numpy(bool), index=pd.DatetimeIndex(m["date"]))
    vals = lookup.reindex(pd.DatetimeIndex(t["date"])).to_numpy()
    mask = pd.Series(pd.Series(vals).fillna(False).astype(bool).to_numpy(),
                     index=t.index, name="RS")
    return mask


# ====================================================================
# 组合与频次
# ====================================================================

def combine_layers(l1: pd.Series, l2: pd.Series,
                   rs: Optional[pd.Series] = None) -> dict:
    """按 P0.5 §④ **同日 AND** 组合三层。返回 ``{'L1':…, 'L1+L2':…, 'L1+L2+RS':…}``。

    ``rs`` 为 None 时只返回前两组。三个 Series 必须**等长同索引**（由调用方保证）。
    """
    if not (len(l1) == len(l2)) or (rs is not None and len(rs) != len(l1)):
        raise ThemeTimingError("组合三层的 Series 长度不一致 —— 必须来自同一条主题序列")
    l1 = l1.fillna(False).astype(bool)
    l2 = l2.fillna(False).astype(bool)
    out = {"L1": l1, "L1+L2": (l1 & l2)}
    if rs is not None:
        out["L1+L2+RS"] = (l1 & l2 & rs.fillna(False).astype(bool))
    return out


def episode_spans(mask: pd.Series, gap: int = EPISODE_GAP) -> list:
    """把布尔掩码压成**片段**：相邻（索引间隔 ≤ gap）的 True 属同一片段。

    Returns:
        ``[(start_idx, end_idx, n_days), …]``。频次口径是 ``len(...)``，
        与 `scripts/audit_regime_hold.py` 的 `episodes()` 一致。
    """
    pos = np.where(np.asarray(mask, dtype=bool))[0]
    if len(pos) == 0:
        return []
    spans, s, prev = [], pos[0], pos[0]
    for p in pos[1:]:
        if p - prev <= int(gap):
            prev = p
            continue
        spans.append((int(s), int(prev), int(prev - s + 1)))
        s = prev = p
    spans.append((int(s), int(prev), int(prev - s + 1)))
    return spans


def episodes_per_year(mask: pd.Series, dates: pd.Series,
                      gap: int = EPISODE_GAP) -> float:
    """**片段数 / 年**（P0.5 §⑥-5 的频次口径）。"""
    spans = episode_spans(mask, gap=gap)
    if not spans:
        return 0.0
    d = pd.to_datetime(pd.Series(dates).reset_index(drop=True))
    years = max((d.iloc[-1] - d.iloc[0]).days / 365.25, 1e-9)
    return len(spans) / years


def layer_masks(theme_df: pd.DataFrame,
                anchor_df: Optional[pd.DataFrame] = None,
                *,
                k: int = K_SWING,
                low_lookback: int = L1_LOW_LOOKBACK,
                confirm_bars: int = L1_CONFIRM_BARS,
                ma_fast: int = MA_FAST,
                ma_slow: int = MA_SLOW,
                slope_window: int = MA_SLOPE_WINDOW,
                slope_min: float = MA_SLOPE_MIN,
                rs_window: int = RS_WINDOW,
                rs_ma_window: int = RS_MA_WINDOW) -> pd.DataFrame:
    """一次性算出三层掩码与两组组合（列：``L1 / L2 / RS / L1+L2 / L1+L2+RS``）。

    ``anchor_df`` 为 None 时跳过 RS（列全 False，并打 WARNING）。
    """
    d = _prepare(theme_df, "layer_masks 主题序列")
    l1 = l1_structure_break(d, k=k, low_lookback=low_lookback, confirm_bars=confirm_bars)
    l2 = l2_ma_turn(d, fast=ma_fast, slow=ma_slow, slope_window=slope_window,
                    slope_min=slope_min)
    if anchor_df is None:
        logger.warning("layer_masks: 未提供宽基锚 —— RS 层不可计算，组合将退化为 L1+L2")
        rs = pd.Series(False, index=d.index, name="RS")
    else:
        rs = rs_strength(d, anchor_df, window=rs_window, ma_window=rs_ma_window)
    combo = combine_layers(l1, l2, rs)
    out = pd.DataFrame({"date": d["date"], "close": d["close"], "L1": l1, "L2": l2,
                        "RS": rs})
    for name, m in combo.items():
        if name != "L1":
            out[name] = m
    return out


__all__ = [
    "ThemeTimingError",
    "K_SWING", "L1_LOW_LOOKBACK", "L1_CONFIRM_BARS",
    "MA_FAST", "MA_SLOW", "MA_SLOPE_WINDOW", "MA_SLOPE_MIN",
    "RS_WINDOW", "RS_MA_WINDOW", "EPISODE_GAP", "LAYER_SPEC",
    "swing_points", "swing_high_asof",
    "l1_state", "l1_pending", "l1_structure_break", "l2_ma_turn", "rs_strength",
    "combine_layers", "episode_spans", "episodes_per_year", "layer_masks",
]
