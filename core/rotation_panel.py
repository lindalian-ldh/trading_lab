"""板块轮动资金确认板 —— 纯函数聚合层（**零 IO**）。

> 口径已冻结，见 [`scripts/增强方案v2-...md`](../../scripts/增强方案v2-板块轮动面板与ETF择时.md)
> 的「P0.5-A · 面板口径冻结」。**本模块的公式不得在累积开始后擅自修改。**

**为什么放在 `core/` 而不是 `services/alarming_monitor/`**（2026-10-04）：
本模块是**零 IO 纯函数**，只依赖 pandas 与 `core.theme_universe`。
放在服务目录需要把该目录加进 `sys.path`，而 alarming_monitor 也有自己的 `config.py`
⇒ 会把 `config` 包遮蔽掉，报 `No module named 'config.settings'; 'config' is not a package`
（**这正是交接文档里记录的"services 间 config.py 同名冲突"**）。
放 `core/` 后无需任何 sys.path 操作，且其它服务可复用。
**集成（P1.5）仍然发生在 alarming_monitor**，只是它 import 本模块。

## 定位（硬约束）

**这是一块"仪表盘"，不是"信号源"。** 它只回答"钱在往哪个方向聚"，不回答"买什么"：

- **不产生买入信号、不占仓位权重**（需求 #1 的全部价值在"确认/证伪"与"防呆"）
- 主体**只有 5~10 行**（你的主题），**不做全市场板块涨幅榜**（那是同花顺的强项，抄它必输）
- 任何缺失一律 `n/a` —— **绝不填 0、绝不估算**

## 与 App 的差别（唯一的存在理由）

同花顺告诉你"这个板块今天涨得好不好"（价格口径）；本板告诉你
"**龙虎榜口径下，钱是不是真在往我这个方向聚，而且这个共识在增强还是衰减**"。
唯一崭新的数字是**归一化上榜率**（`n_listed / n_constituents`）——
它让"6 只成分股里 3 只上榜(50%)"与"500 只里 9 只上榜(1.8%)"可比。

## 关键口径说明（为什么这么设计）

1. **上榜率必须归一化**：题材宽度差异极大（半导体设备 6 只 ↔ 人工智能 1531 只）。
   实测：**原始上榜股日排序与归一化上榜率排序几乎相反** ——
   芯片 6466 股日（最多）但上榜率仅 1.34%；半导体设备 306 股日（最少）但上榜率 30.54%（最高）。
2. **买卖比要看"超额"，不看绝对值**：实测基线就是 **0.50**（上榜门槛要求买卖双方都够大），
   全市场 4092 个股日的均值 0.520 / 中位 0.508。而**全市场日均值的时序 std 仅 0.0226**，
   个股横截面 std 0.127 ⇒ 聚合已压掉噪声，**有信息的是"主题 − 当日全市场"的偏离**。
3. **分位只用题材自身历史**：跨题材比绝对值会被宽度污染；比"跟自己比"才干净。

## 已知局限（2026-10-04 实测，**必须在面板上标注，不许掩盖**）

1. **`热度` 列只对部分题材可得**：`plates_rank(17, limit>=300)` 实测**硬上限返回 254 条**
   （与 `limit` 无关），只覆盖当日**有活跃度**的题材。
   而板子的窄题材（`801490 半导体设备`、`801068 第三代半导体`、光刻/气体/封测、云计算的三个题材）
   **都不在这 254 条里** ⇒ 它们的 `热度` 恒为 `n/a`。
   ⇒ **这一列对聚焦型题材系统性地不可用**；面板保留它只是为了宽题材（芯片/消费电子/电网/黄金等）。
   ⚠️ 同时注意：显示的 `#26` 是"在这 254 条内的名次"，**不是全市场 520 个题材的名次**。
2. **`连榜` 对宽题材无区分度**：宽题材几乎天天有票上榜 ⇒ streak 会饱和在回看窗口长度。
   面板对饱和情况显示 `N+`（见 `format_panel`）。
3. **成分股是当前快照**：`plates_stocks` 有 `time_in` 但 `time_out` 为空
   ⇒ 已退出的成分股看不到，`n_constituents` 有残余幸存者偏差。
4. **13.6% 的 LHB 板块行取不到成分股**（不在 `plates_list(17)` 内，如 信创/存储/数据要素/DeepSeek）
   ⇒ 这些题材只能给 `n_listed`，不能给 `listed_rate`。
"""

from __future__ import annotations

import math
from typing import Iterable, Optional

import pandas as pd

# ====================================================================
# 冻结参数（**改动必须新开版本号并重算历史**）
# ====================================================================

PANEL_VERSION = "v1"
PCT_RANK_WINDOW = 120        # 分位回看窗口（交易日）
PCT_RANK_MIN_SAMPLES = 60    # 分位最小有效样本，不足则 n/a
ROLL_WINDOWS = (3, 5)        # 持续性滚动窗口
CRUSH_THRESHOLD = 0.65       # "买方碾压"阈值
NEW_ENTRY_LOOKBACK = 3       # 🆕 新进：前 N 日均为 0
COOLING_PCT_RANK = 0.20      # 💤 冷却：分位 ≤ 该值

NA = "n/a"

# 状态标签
TAG_NEW = "🆕 新进"
TAG_STRENGTHENING = "🔥 共识增强"
TAG_COOLING = "💤 冷却"
TAG_FLAT = "➖ 持平"
# 优先级：只取第一个命中
TAG_PRIORITY = (TAG_NEW, TAG_STRENGTHENING, TAG_COOLING, TAG_FLAT)


# ====================================================================
# 工具
# ====================================================================

def _f(v) -> Optional[float]:
    """安全转 float；NaN/None/空 → None（**不是 0**）。"""
    try:
        if v is None:
            return None
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except (TypeError, ValueError):
        return None


def _pct_rank(values: Iterable[float], value: float) -> Optional[float]:
    """value 在 values 中的百分位（0~1）。样本不足或 value 为 None → None。"""
    if value is None:
        return None
    vals = [v for v in (_f(x) for x in values) if v is not None]
    if len(vals) < PCT_RANK_MIN_SAMPLES:
        return None
    n_le = sum(1 for v in vals if v <= value)
    return n_le / len(vals)


def _streak(flags: list) -> int:
    """从末尾往前数连续 True 的个数。"""
    n = 0
    for f in reversed(flags):
        if f:
            n += 1
        else:
            break
    return n


# ====================================================================
# 单日聚合
# ====================================================================

def theme_listed_counts(concepts_df: pd.DataFrame,
                        themes: Iterable[dict]) -> dict:
    """{主题: 当日上榜股数} —— 口径 ①。

    归属：`lhb_concepts.plate_code ∈ theme['plates']`；去重键 `(date, stock_code)`。
    """
    out = {}
    if concepts_df is None or concepts_df.empty:
        return {t["theme"]: 0 for t in themes}
    c = concepts_df
    keys = ["date", "stock_code"]
    for t in themes:
        codes = {str(code) for code, _ in t["plates"]}
        sub = c[c["plate_code"].astype(str).isin(codes)]
        out[t["theme"]] = int(sub.drop_duplicates(subset=keys).shape[0]) if not sub.empty else 0
    return out


def market_buy_sell_ratio(stocks_df: pd.DataFrame) -> Optional[float]:
    """口径 ⑩：当日**全市场** Tier2 毛额买卖比。Tier2 未覆盖 → None。"""
    if stocks_df is None or stocks_df.empty:
        return None
    t2 = stocks_df[stocks_df.get("detail_fetched").fillna(False).astype(bool)] \
        if "detail_fetched" in stocks_df.columns else stocks_df.iloc[0:0]
    bt = _f(pd.to_numeric(t2["buy_total"], errors="coerce").sum()) if not t2.empty else None
    st = _f(pd.to_numeric(t2["sell_total"], errors="coerce").sum()) if not t2.empty else None
    if bt is None or st is None or (bt + st) <= 0:
        return None
    return bt / (bt + st)


def theme_buy_sell_stats(concepts_df: pd.DataFrame,
                         stocks_df: pd.DataFrame,
                         theme: dict) -> dict:
    """口径 ⑦⑧：某主题当日的毛额买卖比与碾压占比（仅用 Tier2 覆盖到的个股）。"""
    empty = {"buy_sell_ratio": None, "crush_share": None, "n_tier2": 0}
    if concepts_df is None or concepts_df.empty or stocks_df is None or stocks_df.empty:
        return empty
    codes = {str(c) for c, _ in theme["plates"]}
    sub = concepts_df[concepts_df["plate_code"].astype(str).isin(codes)][["date", "stock_code"]]
    if sub.empty:
        return empty
    sub = sub.drop_duplicates()
    if "detail_fetched" not in stocks_df.columns:
        return empty
    t2 = stocks_df[stocks_df["detail_fetched"].fillna(False).astype(bool)]
    if t2.empty:
        return empty
    m = sub.merge(t2[["date", "stock_code", "buy_total", "sell_total", "buy_sell_ratio"]],
                  on=["date", "stock_code"], how="inner")
    if m.empty:
        return empty
    bt = _f(pd.to_numeric(m["buy_total"], errors="coerce").sum())
    st = _f(pd.to_numeric(m["sell_total"], errors="coerce").sum())
    ratio = bt / (bt + st) if (bt is not None and st is not None and (bt + st) > 0) else None
    r = pd.to_numeric(m["buy_sell_ratio"], errors="coerce").dropna()
    crush = float((r >= CRUSH_THRESHOLD).mean()) if len(r) else None
    return {"buy_sell_ratio": ratio, "crush_share": crush, "n_tier2": int(len(m))}


# ====================================================================
# 时序指标（口径 ④⑤⑥ + 状态标签）
# ====================================================================

def add_time_series_metrics(daily: pd.DataFrame) -> pd.DataFrame:
    """给"逐日逐主题"长表补上滚动/分位/连榜/状态标签。

    Args:
        daily: 需含列 ``date / theme / n_listed / listed_rate``（按日期升序）。

    Returns:
        追加 ``roll3 / roll5 / rate_pct_rank / streak / tag`` 的副本。
        分位按**每个主题自身**的滚动窗口计算（跨题材不可比，见模块 docstring）。
    """
    if daily is None or daily.empty:
        return daily
    df = daily.sort_values(["theme", "date"]).reset_index(drop=True).copy()

    for w in ROLL_WINDOWS:
        # ⚠️ min_periods=w（**不是 1**）：滚动和必须真的要满 w 天。
        # 若用 min_periods=1，序列开头会把"递减的日频数据"累加成"递增的滚动和"
        # ⇒ 误报 🔥 共识增强（已实测踩到）。不满窗口时给 NaN，标签逻辑自然不触发。
        df[f"roll{w}"] = (df.groupby("theme")["n_listed"]
                          .transform(lambda s, w=w: s.rolling(w, min_periods=w).sum()))

    df["streak"] = df.groupby("theme")["n_listed"].transform(_rolling_streak)

    def _rank(s: pd.Series) -> pd.Series:
        return s.rolling(PCT_RANK_WINDOW, min_periods=PCT_RANK_MIN_SAMPLES).apply(
            lambda w: _pct_rank(w, w.iloc[-1]), raw=False)
    df["rate_pct_rank"] = df.groupby("theme")["listed_rate"].transform(_rank)

    df["tag"] = [_tag_for(i, df) for i in range(len(df))]
    return df


def _rolling_streak(s: pd.Series) -> pd.Series:
    """连续 >0 的计数（逐元素）。"""
    out, run = [], 0
    for v in s:
        run = run + 1 if (v is not None and v > 0) else 0
        out.append(run)
    return pd.Series(out, index=s.index)


def _tag_for(i: int, df: pd.DataFrame) -> str:
    """状态标签：优先级 🆕 > 🔥 > 💤 > ➖（只取第一个命中）。"""
    row = df.iloc[i]
    theme = row["theme"]
    hist = df[(df["theme"] == theme)].reset_index(drop=True)
    pos = hist.index[hist["date"] == row["date"]]
    if len(pos) == 0:
        return TAG_FLAT
    k = int(pos[0])

    # 🆕：当日 ≥1 且前 N 日均为 0
    if (row.get("n_listed") or 0) >= 1 and k >= 1:
        prev = hist["n_listed"].iloc[max(0, k - NEW_ENTRY_LOOKBACK):k]
        if len(prev) >= 1 and (prev.fillna(0) == 0).all():
            return TAG_NEW

    # 🔥：roll5 连续 2 日严格递增
    r5 = hist["roll5"]
    if k >= 2 and _f(r5.iloc[k]) is not None and _f(r5.iloc[k - 1]) is not None \
            and _f(r5.iloc[k - 2]) is not None:
        if r5.iloc[k] > r5.iloc[k - 1] > r5.iloc[k - 2]:
            return TAG_STRENGTHENING

    # 💤：分位 ≤ 0.20，或 roll5 连续 2 日下降
    pr = _f(row.get("rate_pct_rank"))
    if pr is not None and pr <= COOLING_PCT_RANK:
        return TAG_COOLING
    if k >= 2 and _f(r5.iloc[k]) is not None and _f(r5.iloc[k - 1]) is not None \
            and _f(r5.iloc[k - 2]) is not None:
        if r5.iloc[k] < r5.iloc[k - 1] < r5.iloc[k - 2]:
            return TAG_COOLING

    return TAG_FLAT


# ====================================================================
# 面板装配
# ====================================================================

def build_theme_daily(concepts_df: pd.DataFrame,
                      stocks_df: pd.DataFrame,
                      dates: Iterable[str],
                      themes: Iterable[dict],
                      counts: dict) -> pd.DataFrame:
    """把 [(日期 × 主题)] 展开成逐日长表（口径 ①②③⑦⑧⑨）。

    Args:
        concepts_df: 全量（或窗口内）LHB 题材明细 —— 需列 date/stock_code/plate_code
        stocks_df:   全量（或窗口内）LHB 个股表 —— 需列 date/stock_code/detail_fetched/buy_total/sell_total
        dates:       要展开的交易日（升序）
        themes:      主题定义（core.theme_universe.THEMES）
        counts:      {主题: 成分股并集去重数}
    """
    themes = list(themes)
    rows = []
    for d in dates:
        c_day = concepts_df[concepts_df["date"] == d] if concepts_df is not None and not concepts_df.empty else None
        s_day = stocks_df[stocks_df["date"] == d] if stocks_df is not None and not stocks_df.empty else None
        listed = theme_listed_counts(c_day, themes) if c_day is not None else {t["theme"]: 0 for t in themes}
        mkt = market_buy_sell_ratio(s_day)
        for t in themes:
            n = int(listed.get(t["theme"], 0))
            nc = int(counts.get(t["theme"], 0) or 0)
            bs = theme_buy_sell_stats(c_day, s_day, t) if c_day is not None else \
                {"buy_sell_ratio": None, "crush_share": None, "n_tier2": 0}
            ratio, mkt_r = bs["buy_sell_ratio"], mkt
            excess = (ratio - mkt_r) if (ratio is not None and mkt_r is not None) else None
            rows.append({
                "date": d,
                "theme": t["theme"],
                "n_listed": n,
                "n_constituents": nc,
                "listed_rate": (n / nc) if nc > 0 else None,
                "buy_sell_ratio": ratio,
                "crush_share": bs["crush_share"],
                "n_tier2": bs["n_tier2"],
                "market_buy_sell_ratio": mkt_r,
                "ratio_excess": excess,
            })
    return pd.DataFrame(rows)


def attach_theme_rank(daily: pd.DataFrame, rank_df: pd.DataFrame) -> pd.DataFrame:
    """把题材热度（plates_rank(17)）按 (日期, 主题) 附上。

    一个主题可能对应多个题材 ⇒ 取其中 **score 最高**（最热）的那个作为代表，并记录其名次。
    """
    if daily is None or daily.empty:
        return daily
    out = daily.copy()
    for col in ("rank", "rate", "score", "trade_money", "volume_ration"):
        out[col] = None
    if rank_df is None or rank_df.empty:
        return out

    # 主题 → 题材编码集合
    from core.theme_universe import THEMES
    plates_of = {t["theme"]: {str(c) for c, _ in t["plates"]} for t in THEMES}
    rk = rank_df.copy()
    rk["plate_code"] = rk["plate_code"].astype(str)
    for i, row in out.iterrows():
        codes = plates_of.get(row["theme"], set())
        sub = rk[(rk["date"] == row["date"]) & (rk["plate_code"].isin(codes))]
        if sub.empty:
            continue
        best = sub.sort_values("score", ascending=False, na_position="last").iloc[0]
        for col in ("rank", "rate", "score", "trade_money", "volume_ration"):
            out.at[i, col] = best.get(col)
    return out


def new_entry_themes(daily: pd.DataFrame, rank_df: pd.DataFrame,
                     universe_rank: int = 20,
                     prev_days: int = NEW_ENTRY_LOOKBACK) -> list:
    """口径 ⑪（对照区）：今日进入题材热度前 N、且**前 N 日都没进过**的题材。

    这用于"全市场题材异动"对照区，帮助发现**宇宙外**的新方向。
    """
    if rank_df is None or rank_df.empty:
        return []
    rk = rank_df.copy()
    rk["plate_code"] = rk["plate_code"].astype(str)
    dates = sorted(rk["date"].unique())
    if not dates:
        return []
    today = dates[-1]
    today_codes = set(rk[(rk["date"] == today) & (rk["rank"] <= universe_rank)]["plate_code"])
    if not today_codes:
        return []
    prev_dates = dates[-(prev_days + 1):-1]
    prev_codes = set(rk[(rk["date"].isin(prev_dates)) & (rk["rank"] <= universe_rank)]["plate_code"])
    new_codes = today_codes - prev_codes
    names = (rk[(rk["date"] == today) & (rk["plate_code"].isin(new_codes))]
             .sort_values("rank")[["plate_code", "plate_name"]])
    return [f"{r.plate_name}" for r in names.itertuples()]


# ====================================================================
# 报告格式化
# ====================================================================

def _fmt_rate(v) -> str:
    x = _f(v)
    return NA if x is None else f"{x * 100:.1f}%"


def _fmt_num(v, nd: int = 2, suffix: str = "") -> str:
    x = _f(v)
    return NA if x is None else f"{x:.{nd}f}{suffix}"


def _fmt_pct(v) -> str:
    x = _f(v)
    return NA if x is None else f"{x * 100:.0f}%"


def _fmt_signed(v, nd: int = 3) -> str:
    x = _f(v)
    return NA if x is None else f"{x:+.{nd}f}"


def _spark(vals: list) -> str:
    """把滚动值画成 sparkline（看"持续性"的形状）。NaN/None 画成空格。"""
    blocks = "▁▂▃▄▅▆▇█"
    nums = [_f(v) for v in vals]
    xs = [v for v in nums if v is not None]
    if not xs:
        return ""
    lo, hi = min(xs), max(xs)
    if hi <= lo:
        return "".join(" " if v is None else blocks[0] for v in nums)
    out = []
    for v in nums:
        if v is None:
            out.append(" ")
            continue
        idx = int(round((v - lo) / (hi - lo) * (len(blocks) - 1)))
        out.append(blocks[max(0, min(idx, len(blocks) - 1))])
    return "".join(out)


def format_panel(today_df: pd.DataFrame,
                 history: pd.DataFrame,
                 new_entries: Optional[list] = None,
                 data_sufficiency: Optional[dict] = None,
                 date: str = "") -> str:
    """把当日各主题指标排成 ASCII 等宽面板。

    Args:
        today_df:  **当日**逐主题指标（含 roll3/roll5/rate_pct_rank/streak/tag/热度列）
        history:   逐日长表（用于画 sparkline；含 date/theme/n_listed）
        new_entries: 宇宙外新进题材名列表（对照区）
        data_sufficiency: 数据充分度信息（可含 window_days 用于判断"连榜饱和"）
    """
    W = 108
    lines = ["=" * W]
    lines.append(f" {date}  板块轮动资金确认板（{PANEL_VERSION}）—— 龙虎榜口径")
    lines.append("=" * W)

    if today_df is None or today_df.empty:
        lines.append("  当日无数据（可能非交易日或厂商尚未发布）")
        lines.append("=" * W)
        return "\n".join(lines)

    # ⚠️ 必须按**归一化上榜率**排序，不能按原始上榜家数 ——
    #    后者会让宽题材（票多）永远排前面，正是本板要避免的误导。
    view = today_df.copy()
    view["_sort"] = pd.to_numeric(view["listed_rate"], errors="coerce")
    view = view.sort_values("_sort", ascending=False, na_position="last")

    win = (data_sufficiency or {}).get("window_days")

    lines.append("")
    lines.append("【一、我的主题 · 龙虎榜资金确认】")
    hdr = (f"  {'主题':<14} {'成分':>4} {'上榜':>5} {'上榜率':>7} {'5日趋势':<7} "
           f"{'连榜':>5} {'买卖比':>7} {'超额':>7} {'碾压':>6} {'热度':>6}  状态")
    lines.append(hdr)
    lines.append("  " + "-" * (W - 4))

    for r in view.itertuples():
        hist = history[history["theme"] == r.theme].sort_values("date").tail(5) \
            if history is not None and not history.empty else None
        spark = _spark(hist["roll5"].tolist() if hist is not None and "roll5" in hist.columns
                       else (hist["n_listed"].tolist() if hist is not None else []))
        rank = _f(getattr(r, "rank", None))
        rank_s = NA if rank is None else f"#{int(rank)}"
        # 连榜：宽题材几乎天天有票上榜 ⇒ streak 会饱和在窗口长度，标注 "N+" 更诚实
        streak = int(getattr(r, "streak", 0) or 0)
        streak_s = f"{streak}+" if (win and streak >= int(win)) else str(streak)
        lines.append(
            f"  {r.theme:<14} {r.n_constituents:>4} {r.n_listed:>5} "
            f"{_fmt_rate(r.listed_rate):>7} {spark:<7} {streak_s:>5} "
            f"{_fmt_num(r.buy_sell_ratio, 3):>7} "
            f"{_fmt_signed(getattr(r, 'ratio_excess', None)):>7} "
            f"{_fmt_pct(getattr(r, 'crush_share', None)):>6} {rank_s:>6}  {r.tag}")

    lines.append("")
    lines.append("【二、全市场题材异动 · 对照】(仅用于发现新方向；密度刻意保持低)")
    if new_entries:
        lines.append(f"  🆕 新进热度前 20（前 3 日不在）: {'、'.join(new_entries[:12])}")
    else:
        lines.append("  🆕 新进: 无（或热度数据不足）")

    lines.append("")
    lines.append("【三、读数备忘】")
    lines.append("  · 上榜率 = 上榜家数 / 成分股数      ← 归一化，可跨题材比较（6 只的题材与 500 只的题材）")
    lines.append("  · 5日趋势 = roll5 sparkline          ← 看持续性，不看单日脉冲")
    lines.append("  · 买卖比 = Σbuy/(Σbuy+Σsell)，**基线就是 0.50**（结构性），")
    lines.append("             所以要看『超额』列 = 该主题 − 当日全市场；差值才有信息")
    lines.append("  · 状态 = 🆕新进 / 🔥共识增强 / 💤冷却 / ➖持平（优先级固定）")
    lines.append("  · 按【上榜率】降序排列（**不是**上榜家数 —— 后者会让票多的宽题材永远靠前）")
    lines.append("  · 连榜显示 `N+` 表示已饱和在回看窗口长度：宽题材几乎天天有票上榜，该列对它无区分度")
    lines.append("  · 热度 = plates_rank(17) 名次，但该接口**硬上限只返回 254 条**（当日有活跃度的题材）")
    lines.append("    ⇒ 窄题材（半导体设备/光刻/气体等）常年 `n/a`，且 `#26` 是这 254 条内的名次")
    lines.append("  · ⚠️ 本板只做确认与证伪，不产生买入信号，不占仓位权重")

    lines.append("")
    lines.append("【四、数据充分度】")
    ds = data_sufficiency or {}
    lines.append(f"  · 面板口径版本: {PANEL_VERSION}（分位窗口 {PCT_RANK_WINDOW} 日 / "
                 f"最小样本 {PCT_RANK_MIN_SAMPLES} 日 / 碾压阈值 {CRUSH_THRESHOLD}）")
    lines.append(f"  · 龙虎榜累积天数: {ds.get('lhb_days', NA)}"
                 f"   Tier2(毛额)覆盖天数: {ds.get('tier2_days', NA)}")
    if ds.get("pct_rank_ready"):
        lines.append("  · 分位可用: ✅（样本已达门槛）")
    else:
        lines.append(f"  · 分位可用: ❌ 样本不足 ⇒ 分位列显示 `{NA}`（这是设计，不是故障）")
    if not ds.get("tier2_ready", True):
        lines.append(f"  · ⚠️ Tier2 未覆盖当日 ⇒ 买卖比/碾压/超额显示 `{NA}`")
    lines.append("=" * W)
    return "\n".join(lines)
