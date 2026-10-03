#!/usr/bin/env python3
"""双因子打分原型 —— 与 A/B/C 门控**并行**运行，不替代它。

⚠️ 定位与风险（先读这段再用）
    这是**样本内原型**，权重**未经拟合**（默认等权 50/50）。

⚠️⚠️ 适用范围限制（2026-09-30 实测发现，非常重要）
    这两个因子的单调性**只在 `rec_type == "signal"` 池内成立**，出了这个池子就失效：

        池子                            双因子分数 Q1→Q5（同日超额）
        signal 池（A或B & C & 门控）      −0.164 → +0.866   ✅ 单调递增
        候选池（A 或 B 通过）              +0.158 → +0.177   ❌ U 形，不单调
        全部存活票                        +0.308 → +0.150   ❌ U 形，不单调

    单看 ATR% 更清楚（方向会翻转）：
        signal 池   Q1 −0.531 → Q5 +0.678   （高波好）
        全样本      Q1 +0.119 → Q5 −0.391   （高波差，方向相反）

    ⇒ **它只能当"系统已经触发的那批候选"内部的排序依据（tie-breaker），
      不能当"在全部股票里挑最好的"的横截面选股因子。**
    ⇒ 而 signal 池仅占全部样本的约 3%，日常 20 只自选里往往一只都不在池内 ——
      **所以它的日常实用价值尚未证明。** 请把它当"待验证假设"记录，而不是工具。

为什么是这两个因子（在 signal 池内实测）
    · ATR%    —— 五等分单调递增：Q1 −0.531pp(t=−6.82) → Q5 +0.678pp(t=+4.47)
    · 距MA20% —— 单调递减：      Q1 +0.410pp(t=+3.09) → Q4 −0.276pp(t=−2.94)
    两因子相关 −0.388（相关但不冗余）。3×3 交叉：
        最好格 = 高波 + 超跌 = +0.917pp
        最差格 = 低波 + 超跌 = −1.350pp     （相差 2.27pp）
    经济含义：**急速暴跌会反弹，缓慢阴跌会继续跌。**
    注意方向与 A/B 相反 —— A 要"回踩到均线"（低波、贴近均线），实测是最差的方向。

为什么不能给 A/B/C 的布尔值打分
    把"通过维度数"当分数，同日超额**完全单调递减**：
        0维 +0.060pp(t=+3.74) / 1维 −0.029 / 2维 −0.096 / 3维 **−1.227pp**(t=−2.49)
    给 A✅B✅ 最高分 = 把历史最差的一组排第一。

打分定义
    逐日横截面百分位（robust，不依赖线性假设）：
        score = 100 × [ w_atr × rank(atr_pct) + w_dev × (1 − rank(dev_ma20)) ]
    用**百分位**而非 z 分数：实测关系是单调但未必线性，排序也不受极值影响。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

SCORE_VERSION = "proto-1"
SCORE_WEIGHTS = {"atr_pct": 0.5, "dev_ma20": 0.5}

FEATURE_KEYS = [
    "atr_pct", "atr_abs", "dev_ma5", "dev_ma20", "vol_ratio",
    "amt20_yi", "close", "ret5_pre", "ret20_pre", "dd250",
]


def features_from_df(df: pd.DataFrame, config) -> dict:
    """从截至当日的日线里算出打分所需的原始特征（不含横截面排名）。"""
    import main as M

    close = float(df["close"].iloc[-1])
    atr = M.calc_atr(df, config.ATR_PERIOD)
    atr_abs = float(atr.iloc[-1]) if len(atr) else float("nan")
    devs = {}
    for p in (5, 20):
        ma = M.calc_ma(df["close"], p).iloc[-1]
        devs[p] = float((close - ma) / ma * 100) if ma and np.isfinite(ma) else float("nan")
    vma = M.calc_ma(df["volume"], config.VOLUME_MA_PERIOD).iloc[-1]
    amt = float((df["close"] * df["volume"]).tail(20).mean())
    hi = df["close"].rolling(250, min_periods=60).max().iloc[-1]
    return {
        "close": close,
        "atr_abs": atr_abs,
        "atr_pct": (atr_abs / close * 100) if close and np.isfinite(atr_abs) else float("nan"),
        "dev_ma5": devs[5],
        "dev_ma20": devs[20],
        "vol_ratio": float(df["volume"].iloc[-1] / vma) if vma and np.isfinite(vma) else float("nan"),
        "amt20_yi": amt / 1e8 if np.isfinite(amt) else float("nan"),
        "ret5_pre": (float(close / df["close"].iloc[-6] - 1) * 100) if len(df) > 6 else float("nan"),
        "ret20_pre": (float(close / df["close"].iloc[-21] - 1) * 100) if len(df) > 21 else float("nan"),
        "dd250": (float(close / hi - 1) * 100) if np.isfinite(hi) and hi else float("nan"),
    }


def hard_vetoes(f: dict, config, stop_dist_pct: float | None = None,
                symbol: str = "") -> list[str]:
    """硬否决层（风控，不是打分）。沿用已验证的生存性规则 + C 的止损过紧。

    实测依据见 scripts/find_survival_filter.py：
        不筛选 −0.297%/10日 → 加生存性否决后 −0.056%（不显著）。
    """
    from etf import is_etf
    out: list[str] = []
    mp = float(getattr(config, "MIN_ENTRY_PRICE", 3.0))
    ma = float(getattr(config, "MIN_ENTRY_AMOUNT", 2e7)) / 1e8
    # 价格否决不适用于 ETF（无面值退市；ETF 净值常在 0.5~2 元）
    if not (symbol and is_etf(symbol)) and np.isfinite(f.get("close", np.nan)) and f["close"] < mp:
        out.append(f"低价 {f['close']:.2f} < {mp:g}")
    if np.isfinite(f.get("amt20_yi", np.nan)) and f["amt20_yi"] < ma:
        out.append(f"低流动性 {f['amt20_yi']:.2f}亿 < {ma:.1f}亿")
    # C 靠极紧止损过关的那批：止损距离 <1% 时，盈亏比是被"分母变小"抬上去的
    if stop_dist_pct is not None and np.isfinite(stop_dist_pct) and stop_dist_pct < 1.0:
        out.append(f"止损过紧 {stop_dist_pct:.2f}% < 1%")
    return out


def rank_and_score(frame: pd.DataFrame, w_atr: float = 0.5, w_dev: float = 0.5,
                   date_col: str = "date", atr_col: str = "atr_pct",
                   dev_col: str = "dev_ma20",
                   exclude: "pd.Series | None" = None) -> pd.DataFrame:
    """逐日横截面百分位 → 双因子分数（0~100，越高越"按历史方向越好"）。

    必须在**同一天的候选集合内部**排名 —— 这才是它被使用的方式。
    单只标的无法排名，此时 score 记 NaN（不假装有排序）。

    atr_col / dev_col 可指定：台账里这两列名为 atr_pct / dev_ma20_pct。

    exclude：需要**排除出排名**的行（布尔 Series）。用途是把 ETF 剔出去 ——
        本卡的边界与分位都在 300 只**个股**上测得（样本内 0 只 ETF），
        把 ETF 和个股放进同一个横截面算百分位是**类别错误**：
        ETF 净值 0.6~1.6 元 vs 个股 10~30 元，ATR%/偏离的分布根本不同。
        被排除的行 score 记 NaN（与"样本不足"同一种表达：不假装有排序）。
    """
    d = frame.copy()
    d["rank_atr"] = np.nan
    d["rank_dev"] = np.nan
    d["score_double"] = np.nan
    d["score_n_valid"] = 0
    ex = (pd.Series(False, index=d.index) if exclude is None
          else exclude.reindex(d.index).fillna(False).astype(bool))
    d["score_excluded"] = ex
    for _, idx in d.groupby(date_col).groups.items():
        idx = idx[~ex.loc[idx]]           # 排除 ETF 等不该参与排名的行
        if len(idx) < 3:
            continue                      # 少于 3 只无法构成横截面，明确不给分
        g = d.loc[idx]
        a = pd.to_numeric(g[atr_col], errors="coerce")
        v = pd.to_numeric(g[dev_col], errors="coerce")
        if a.notna().sum() < 3 or v.notna().sum() < 3:
            continue
        ra = a.rank(pct=True)
        rv = v.rank(pct=True)
        d.loc[idx, "rank_atr"] = ra * 100
        d.loc[idx, "rank_dev"] = rv * 100
        sc = w_atr * ra + w_dev * (1 - rv)
        d.loc[idx, "score_double"] = sc * 100
        d.loc[idx, "score_n_valid"] = int(sc.notna().sum())
    return d


def describe() -> str:
    return (f"双因子打分 {SCORE_VERSION}｜等权 {SCORE_WEIGHTS['atr_pct']:.0%} ATR% + "
            f"{SCORE_WEIGHTS['dev_ma20']:.0%} (100−距MA20百分位)｜"
            f"样本内、权重未拟合 —— 仅用于并行观察，勿直接据以下单")


# ==================== 人工判读卡（样本内实测） ====================
# 用途：单只标的无法算横截面分数（见 rank_and_score 的说明），但可以把它落到
# 下面这张 3×3 的格子里，读出该格的历史超额。
#
# ⚠️ 三条硬约束：
#   1. 只在 signal 池内有效（A或B通过 且 C通过 且 门控放行）。全样本里方向会翻转：
#      ATR% 全样本 Q1 +0.119 → Q5 −0.391，与 signal 池内(Q1 −0.531 → Q5 +0.678)相反。
#   2. 样本内（300只 × 2016-2026），无样本外验证。
#   3. 幅度很小：最好的格 +0.917pp/10日，而单笔 10 日收益 SD = 8.72pp（噪声 9 倍）。
#
# 分档边界（signal 池 n=21877 的三分位）：

CARD_ATR_BOUNDS = (2.69, 3.91)        # 低波 <2.69 | 中波 2.69~3.91 | 高波 >3.91
CARD_DEV_BOUNDS = (-5.31, -1.81)      # 超跌 <-5.31 | 中间 -5.31~-1.81 | 贴近/上方 >-1.81

# (行=波动档, 列=偏离档) → (同日超额 pp, t 值, 样本数)
CARD_CELLS: dict[tuple[str, str], tuple[float, float, int]] = {
    ("低波", "超跌"):     (-1.350, -5.88, 1023),
    ("低波", "中间"):     (-0.341, -3.47, 2699),
    ("低波", "贴近/上方"): (-0.237, -2.94, 3571),
    ("中波", "超跌"):     (-0.169, -1.22, 2382),
    ("中波", "中间"):     (-0.228, -1.99, 2775),
    ("中波", "贴近/上方"): (+0.081, +0.54, 2135),
    ("高波", "超跌"):     (+0.917, +6.24, 3888),
    ("高波", "中间"):     (+0.695, +3.49, 1818),
    ("高波", "贴近/上方"): (-0.426, -1.67, 1586),
}


def _band(v: float, bounds: tuple[float, float], labels: tuple[str, str, str]) -> str:
    if not np.isfinite(v):
        return "未知"
    lo, hi = bounds
    return labels[0] if v < lo else (labels[1] if v <= hi else labels[2])


def interpret(atr_pct: float, dev_ma20: float) -> dict:
    """把单只标的落到判读卡的格子里，返回分档与该格实测值。

    注意：**判读顺序是先看 ATR，再看距MA20** —— 低波那一整行在实测里全是负的
    （-1.350 / -0.341 / -0.237，|t| 均 >2.9），所以 ATR 低时无论多超跌都不该买。
    """
    row = _band(atr_pct, CARD_ATR_BOUNDS, ("低波", "中波", "高波"))
    col = _band(dev_ma20, CARD_DEV_BOUNDS, ("超跌", "中间", "贴近/上方"))
    cell = CARD_CELLS.get((row, col))
    out = {"row": row, "col": col, "cell": f"{row} × {col}",
           "atr_band": f"低波<{CARD_ATR_BOUNDS[0]}" if row == "低波" else
                       (f"高波>{CARD_ATR_BOUNDS[1]}" if row == "高波" else
                        f"中波 {CARD_ATR_BOUNDS[0]}~{CARD_ATR_BOUNDS[1]}"),
           "dev_band": f"超跌<{CARD_DEV_BOUNDS[0]}" if col == "超跌" else
                       (f"贴近/上方>{CARD_DEV_BOUNDS[1]}" if col == "贴近/上方" else
                        f"中间 {CARD_DEV_BOUNDS[0]}~{CARD_DEV_BOUNDS[1]}")}
    if cell:
        out.update({"excess": cell[0], "t": cell[1], "n": cell[2],
                    "verdict": ("✅ 有强证据" if (cell[1] > 2 and cell[0] > 0)
                                else ("⛔ 有强负证据" if cell[1] < -2 else "➖ 证据不足"))})
    else:
        out.update({"excess": float("nan"), "t": float("nan"), "n": 0,
                    "verdict": "未知"})

    # —— 边界脆弱性检查（2026-10-01 加入）——
    # 动因：实测 603773 距MA20=-1.71%，距分档边界(-1.81%)仅 0.10pp，而邻格
    # 从 -0.426pp 跳到 +0.695pp —— **股价动 0.1% 结论反转 1.12pp**。
    # 硬分档必然有这个毛病，所以必须显式告知"格位不稳"，而不是假装它是确定的。
    # 判据用**相对**距离（占该档宽度的比例），避免不同量纲下阈值不一致。
    ROWS = ("低波", "中波", "高波"); COLS = ("超跌", "中间", "贴近/上方")
    widths = (CARD_ATR_BOUNDS[1] - CARD_ATR_BOUNDS[0],
              CARD_DEV_BOUNDS[1] - CARD_DEV_BOUNDS[0])
    near = []
    for val, bounds, w, labels, axis in (
            (atr_pct, CARD_ATR_BOUNDS, widths[0], ROWS, "ATR%"),
            (dev_ma20, CARD_DEV_BOUNDS, widths[1], COLS, "距MA20%")):
        if not np.isfinite(val):
            continue
        for b in bounds:
            d = abs(val - b)
            if d <= 0.10 * w:                     # 距边界 ≤ 该档宽度的 10%
                cur_i = labels.index(_band(val, bounds, labels))
                # 探针要落在边界的**另一侧**：val > b 说明当前在上一档，
                # 邻档在下方，故向下探（b - eps）；反之向上探。
                probe = b - 1e-9 if val > b else b + 1e-9
                alt_i = labels.index(_band(probe, bounds, labels))
                nb = (ROWS[alt_i], col) if axis == "ATR%" else (row, COLS[alt_i])
                nc = CARD_CELLS.get(nb)
                near.append({
                    "axis": axis, "value": val, "bound": b, "dist": d,
                    "pct_of_band": d / w * 100,
                    "neighbor_cell": f"{nb[0]} × {nb[1]}",
                    "neighbor_excess": nc[0] if nc else float("nan"),
                    "neighbor_t": nc[1] if nc else float("nan"),
                    "swing": abs((nc[0] - cell[0]) if (nc and cell) else float("nan")),
                })
    out["near_boundary"] = near
    return out
