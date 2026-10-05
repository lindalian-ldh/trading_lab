"""Phase 3 · 主题转折**观察哨** —— 只显示、不决策（**零 IO**，纯函数）。

## 为什么是「观察哨」而不是「信号」

Phase 2 的结论（[`scripts/增强方案v2-....md`](../scripts/增强方案v2-板块轮动面板与ETF择时.md)
的「P2.1~P2.4 完成记录」）：L1 / L2 / RS 三层**全部未通过 P0.5 冻结基准 v1 的四项判据**，
联合层更被 kill criterion 3 判出局（0.38~0.88 次/年 < 2 次/年）。
⇒ 本模块的输出**不得**进入仓位逻辑。

因此本模块的契约是（P3.3）：

1. **只显示**：输出里的每一行都带 `validated=False`；
2. **不产生仓位指令**：调用方**不得**把它写进仓位/买卖/止损逻辑；
3. **数据不可用必须显式**：缺数据 / 数据过期一律打印 **`❌ 数据不可用`**，
   **绝不静默当成"安全"**（沿用 `regime_detector.py` 的横幅机制）。

## 与 PANIC_DOWN 的关系（P2.6 退化为一句）

`PANIC_DOWN`（左侧）能下指令，转折信号**不能** ⇒ **结构上不可能出现两条矛盾指令**，
所以原计划的「仲裁表」退化为一句说明（见 :func:`arbitration_note`）。
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

import numpy as np
import pandas as pd

from core.theme_timing import (
    EPISODE_GAP,
    episode_spans,
    l1_pending,
    layer_masks,
    rs_strength,
)

logger = logging.getLogger(__name__)

#: 「只显示不决策」契约 —— 任何渲染都必须带上
DISCIPLINE_NOTE = "⚠️ 本板只做确认与证伪，不产生买入信号，不占仓位权重"

#: 未通过预注册验证的显式标注（P0.5 冻结基准 v1 §⑥ 四项判据全部未通过）
VALIDATION_NOTE = ("❌ 未通过 P0.5 预注册判据（L1 t=−0.42~+1.00；联合层 0.38~0.88 次/年 <2）"
                   " ⇒ 仅观察")

#: 数据过期阈值（自然日）：超过即视为"不可用"，而不是"没有信号"
STALE_DAYS = 5
#: 观测可信所需最少根数
MIN_BARS = 60
#: 「≥10 年」的根数门槛（约 2430 根）—— 低于它只能观察、不能当验证依据
SHORT_HISTORY_BARS = 2430
#: 主题与锚的日收益相关性超过此值 ⇒ **RS 层近乎退化**（比价近乎常数）
RS_WEAK_CORR = 0.85
#: **大盘锚**：用于第二列相对强度（``RS大盘``）。
# 为什么要有第二列：主题自己的锚（``THEMES[*]['anchor']``）是**冻结项**（P0.5 §G1），
# 它回答"同风格内部强不强"；但它可能是创业板指/科创50 这种**与主题近乎同一个东西**的锚
# （实测相关系数 0.85~0.90 ⇒ 比价近乎常数、RS 退化，显示 ⚠️）。
# 换掉它要改冻结项；而**加一列显示**不用 —— 所以第二列固定用沪深300，回答"相对大盘强不强"。
# 两者组合起来才有信息量：
#   风格✓大盘✓ = 最强；风格✓大盘✗ = 行业 alpha 在、风格 beta 逆风；
#   风格✗大盘✓ = 搭了风格的车、自身不强；风格✗大盘✗ = 最弱。
# ⚠️ 本列**只用于显示与台账**，不进 P2.2/P2.3 的冻结组合（L1 / L1+L2 / L1+L2+RS），
#    也不参与任何通过/不通过判定 —— 否则就是看到数据后加新假设。
MARKET_ANCHOR = "sh000300"

#: 「结构已破」标签只在最近这么多**交易日**内曾点亮过 L1 时才用
#  （否则几乎所有序列都满足"历史上曾转多" ⇒ 标签退化成常量、毫无信息）
RECENT_SPAN_DAYS = 20


def arbitration_note() -> str:
    """P2.6 的最终形态（原「仲裁表」退化为一句话）。"""
    return ("PANIC_DOWN（左侧，已验证但体制依赖）**可以**下指令；"
            "转折信号（右侧，未通过验证）**不可以** ⇒ 两者结构上不可能给出矛盾指令。")


def _corr_with_anchor(theme_df: pd.DataFrame,
                      anchor_df: pd.DataFrame) -> Optional[float]:
    """主题指数与宽基锚的日收益相关性（用于判断 RS 层是否退化）。"""
    try:
        a = _prepare_like(theme_df)
        b = _prepare_like(anchor_df)
    except Exception:
        return None
    j = pd.concat([a.rename("t"), b.rename("a")], axis=1, sort=True).dropna()
    if len(j) < 200:
        return None
    v = float(j["t"].corr(j["a"]))
    return v if np.isfinite(v) else None


def _prepare_like(df: pd.DataFrame) -> pd.Series:
    d = df[["date", "close"]].copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    d = d.dropna().drop_duplicates(subset=["date"], keep="last").sort_values("date")
    return d.set_index("date")["close"].astype(float).pct_change().dropna()


def _last_span(mask: pd.Series):
    spans = episode_spans(mask, gap=EPISODE_GAP)
    return spans[-1] if spans else None


def theme_status(theme: dict,
                 index_df: Optional[pd.DataFrame],
                 anchor_df: Optional[pd.DataFrame],
                 as_of=None,
                 market_df: Optional[pd.DataFrame] = None) -> dict:
    """单个主题的观察哨读数（**pure**；不取数、不落盘）。

    Args:
        theme: `core.theme_universe.THEMES` 里的一项。
        index_df / anchor_df: 主题指数 / 宽基锚日线；None 表示**取数失败**。
        as_of: 观测日（None = 序列最后一根）。**只用到 as_of 及之前的数据**。
    """
    name = theme.get("theme", "?")
    idx_code, anchor_code = theme.get("index"), theme.get("anchor")
    rec = {
        "theme": name, "kind": theme.get("kind", "theme"),
        "proxy_of": theme.get("proxy_of"),
        "index": idx_code, "anchor": anchor_code,
        "validated": False, "rs_weak": False, "corr_with_anchor": None,
        "as_of": None, "bars": 0, "last_bar": None, "staleness_days": None,
        "available": False, "status": "", "warnings": [],
        "l1": False, "l2": False, "rs": False, "rs_available": idx_code != anchor_code,
        "l1_state_days": 0, "l1_span_start": None, "l1_span_end": None,
        "l1_span_end_days_ago": None, "l1_pending": False,
        "rs_market": False, "rs_market_weak": False,
        "rs_market_same_as_anchor": False, "corr_with_market": None,
        "l2_recent_days_ago": None, "close": None,
    }

    if index_df is None or len(index_df) == 0:
        rec["status"] = "❌ 数据不可用（主题指数取数失败）"
        rec["warnings"].append("主题指数取数为 None —— 明确报不可用，不当成'无信号'")
        return rec
    if anchor_df is None or len(anchor_df) == 0:
        rec["status"] = "❌ 数据不可用（宽基锚取数失败）"
        rec["warnings"].append("宽基锚取数为 None —— RS 层无法计算，不当成'安全'")
        return rec

    d = index_df.copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    if as_of is not None:
        d = d[d["date"] <= pd.Timestamp(as_of)]
    if len(d) == 0:
        rec["status"] = "❌ 数据不可用（观测日之前没有数据）"
        return rec

    last_bar = d["date"].iloc[-1]
    rec.update(bars=int(len(d)), last_bar=last_bar.date().isoformat(),
               close=float(d["close"].iloc[-1]))
    ref = pd.Timestamp(as_of) if as_of is not None else pd.Timestamp(last_bar)
    rec["staleness_days"] = int((pd.Timestamp(ref).normalize()
                                 - pd.Timestamp(last_bar).normalize()).days)
    rec["as_of"] = pd.Timestamp(ref).date().isoformat()

    if rec["staleness_days"] > STALE_DAYS:
        rec["status"] = f"❌ 数据不可用（最后交易日 {rec['last_bar']}，距观测日 {rec['staleness_days']} 天）"
        rec["warnings"].append("数据过期 —— 明确报不可用，不许把陈旧数据当成当日无信号")
        return rec
    if rec["bars"] < MIN_BARS:
        rec["warnings"].append(f"历史仅 {rec['bars']} 根 < {MIN_BARS}，均线/摆动点未预热")
    elif rec["bars"] < SHORT_HISTORY_BARS:
        rec["warnings"].append(
            f"历史 {rec['bars']} 根 < {SHORT_HISTORY_BARS}（≈10 年）⇒ 只能观察、不作验证依据")

    a = anchor_df if rec["rs_available"] else None
    d_ohlc = d.copy()          # 保留 OHLC：l1_pending 需要 high/low
    masks = layer_masks(d, a).reset_index(drop=True)
    # 用 masks 自己回带 date/close —— 保证与掩码**逐行对齐**（_prepare 可能去重/剔 NaN）
    d = masks[["date", "close"]].copy()
    rec["bars"] = int(len(d))
    rec["close"] = float(d["close"].iloc[-1])

    rec["l1"] = bool(masks["L1"].iloc[-1])
    rec["l2"] = bool(masks["L2"].iloc[-1])
    rec["rs"] = bool(masks["RS"].iloc[-1])

    sp = _last_span(masks["L1"])
    if sp is not None:
        s, e, n = sp
        rec["l1_span_start"] = d["date"].iloc[s].date().isoformat()
        rec["l1_span_end"] = d["date"].iloc[e].date().isoformat()
        rec["l1_span_end_days_ago"] = int(len(d) - 1 - e)
        if rec["l1"]:
            rec["l1_state_days"] = n

    # 「条件已满足、等下一根确认」——只可能在最后一根上（否则掩码早就点亮了）
    rec["l1_pending"] = (not rec["l1"]) and bool(l1_pending(d_ohlc))

    l2_pos = np.where(masks["L2"].to_numpy(bool))[0]
    if len(l2_pos):
        rec["l2_recent_days_ago"] = int(len(d) - 1 - l2_pos[-1])

    # —— 只显示的状态标签（优先级从高到低）——
    # —— 第二列：相对**大盘**（沪深300）的强弱。纯显示，不进任何判定 ——
    if rec["rs_available"]:
        if rec["anchor"] == MARKET_ANCHOR:
            rec["rs_market_same_as_anchor"] = True
            rec["rs_market"] = rec["rs"]
            rec["rs_market_weak"] = rec["rs_weak"]
        elif market_df is not None and len(market_df) > 0:
            try:
                rec["rs_market"] = bool(rs_strength(d_ohlc, market_df).iloc[-1])
            except Exception as e:
                logger.warning("RS大盘 计算失败 %s: %s: %s", name, type(e).__name__, e)
            vm = _corr_with_anchor(d_ohlc, market_df)
            rec["corr_with_market"] = None if vm is None else round(vm, 3)
            rec["rs_market_weak"] = bool(vm is not None and vm > RS_WEAK_CORR)

    if not rec["rs_available"]:
        rs_txt = ""
    elif rec["rs"] and rec["rs_market"]:
        rs_txt = "（风格+大盘双强）"
    elif rec["rs"] and not rec["rs_market"]:
        rs_txt = "（风格强，大盘弱）"
    elif rec["rs_market"] and not rec["rs"]:
        rs_txt = "（风格弱，大盘强）"
    else:
        rs_txt = ""
    if rec["l1"] and rec["l2"]:
        rec["status"] = "🔵🔵 结构+均线双确认（观察）" + rs_txt
    elif rec["l1"] and rec["rs"]:
        rec["status"] = "🔵📈 结构转多 + 相对强度（观察）"
    elif rec["l1"]:
        days = rec["l1_state_days"]
        rec["status"] = (f"🔵 结构转多（观察，第 {days} 日）{rs_txt}" if days <= 1
                         else f"🔹 结构维持（观察，已 {days} 日）{rs_txt}")
    elif rec["l1_pending"]:
        rec["status"] = "⏳ 结构条件已满足，待下一根 K 线确认" + rs_txt
    elif (rec["l1_span_end_days_ago"] is not None
          and rec["l1_span_end_days_ago"] <= RECENT_SPAN_DAYS):
        rec["status"] = (f"➖ 结构已破（{rec['l1_span_end_days_ago']} 个交易日前曾转多）"
                         + rs_txt)
    elif rec["rs"] and rec["rs_available"]:
        rec["status"] = "📈 相对强度走强（价格未转多）"
    else:
        rec["status"] = "➖ 无信号"

    if rec["rs_available"]:
        v = _corr_with_anchor(d, anchor_df)
        rec["corr_with_anchor"] = None if v is None else round(v, 3)
        if v is not None and v > RS_WEAK_CORR:
            rec["rs_weak"] = True
            rec["warnings"].append(
                f"主题与锚日收益相关性 {v:.3f} > {RS_WEAK_CORR} ⇒ **RS 层近乎退化**"
                f"（比价近乎常数），RS 点亮不可当独立证据")

    rec["available"] = True
    if not rec["rs_available"]:
        rec["warnings"].append("index == anchor ⇒ 比价恒为 1，RS 层不可用（N/A）")
    return rec


def build_sentinel(themes, loader: Callable[[str], Optional[pd.DataFrame]],
                   as_of=None) -> list:
    """对一批主题算观察哨。``loader(code) -> DataFrame | None`` 由调用方注入（便于测试）。

    **同一指数只算一次**（10 个主题实际只有 5 条价格序列），但每个主题各出一行。
    """
    cache: dict = {}

    def _load(code):
        if code not in cache:
            try:
                cache[code] = loader(code)
            except Exception as e:      # 取数异常必须降级为"不可用"，不许中断整张表
                logger.warning("观察哨取数失败 %s: %s: %s", code, type(e).__name__, e)
                cache[code] = None
        return cache[code]

    market_df = _load(MARKET_ANCHOR)
    rows = []
    for t in themes:
        rows.append(theme_status(t, _load(t.get("index")), _load(t.get("anchor")),
                                 as_of=as_of, market_df=market_df))
    return rows


def format_sentinel(rows: list, as_of=None) -> str:
    """ASCII 渲染（含**数据充分度**段与两条硬约束横幅）。"""
    lines = []
    lines.append("=" * 104)
    n_th = sum(1 for r in rows if r.get("kind", "theme") == "theme")
    lines.append(f"主题转折观察哨 —— 观测日 {as_of or '(各序列最后一根)'}    "
                 f"观察项 {len(rows)} 个（主题 {n_th} + 观察项 {len(rows) - n_th}）")
    lines.append("=" * 104)
    lines.append(f"  {VALIDATION_NOTE}")
    lines.append("  相对强度两列：**风格** = 主题÷自己的锚（冻结项 §G1，回答『同风格内部强不强』）；"
                 f"**大盘** = 主题÷{MARKET_ANCHOR} 沪深300（回答『相对大盘强不强』）")
    lines.append("    符号 ✅=比价走强  ·=未走强  N/A=index 与锚同一个（比价恒为 1）  "
                 f"＝=该主题的锚本来就是沪深300（两列相同）  "
                 f"⚠️=主题与锚相关 >{RS_WEAK_CORR}（比价近乎常数，**该列不可信**）")
    lines.append("    ⚠️ 两条一起看才有信息量：风格✓大盘✗ = 行业 alpha 在、风格 beta 逆风；"
                 "风格✗大盘✓ = 搭了风格的车、自身不强。**两列都只显示、不决策**")
    lines.append(f"  {DISCIPLINE_NOTE}")
    lines.append("  " + arbitration_note())
    def _name(r) -> str:
        lab = r["theme"] + (f"(代理:{r['proxy_of']})" if r.get("proxy_of") else "")
        return f"{lab:<15}"

    def _rs_cell(lit: bool, weak: bool, na: bool = False, same: bool = False) -> str:
        if na:
            return "N/A"
        if same:
            return "＝"
        if weak:
            return "⚠️✅" if lit else "⚠️·"
        return "✅" if lit else "·"

    def _row(r) -> str:
        if not r["available"]:
            return (f"  {_name(r)}{str(r['index']):<15}{str(r['anchor']):<9}"
                    f"{r['bars']:>6}  {(r['last_bar'] or '-'):<12}"
                    f"{'?':>4}{'?':>4}{'?':>4}{'?':>4}  {r['status']}")
        rs = _rs_cell(r["rs"], r["rs_weak"], na=not r["rs_available"])
        rsm = _rs_cell(r["rs_market"], r["rs_market_weak"],
                       same=r["rs_market_same_as_anchor"])
        return (f"  {_name(r)}{str(r['index']):<15}{str(r['anchor']):<9}"
                f"{r['bars']:>6}  {r['last_bar']:<12}"
                f"{'🔵' if r['l1'] else '·':>4}{'⚡' if r['l2'] else '·':>4}{rs:>4}{rsm:>4}"
                f"  {r['status']}")

    def _head(title: str) -> None:
        lines.append("")
        lines.append(f"  {title}")
        lines.append(f"  {'主题':<15}{'指数':<15}{'锚':<9}{'根数':>6}  {'最后交易日':<12}"
                     f"{'L1':>4}{'L2':>4}{'风格':>4}{'大盘':>4}  状态")
        lines.append("  " + "-" * 112)

    themes = [r for r in rows if r.get("kind", "theme") == "theme"]
    watch = [r for r in rows if r.get("kind", "theme") == "watch"]
    if themes:
        _head(f"【主题（含题材，进板块轮动面板）】共 {len(themes)} 个")
        for r in themes:
            lines.append(_row(r))
    if watch:
        _head(f"【观察项（**无题材**，不进板块轮动面板；且**不在 Phase 2 的验证范围内**）】"
              f"共 {len(watch)} 个")
        for r in watch:
            lines.append(_row(r))

    lines.append("")
    lines.append("【数据充分度】")
    bad = [r for r in rows if not r["available"]]
    stale = [r for r in rows if r["available"] and (r["staleness_days"] or 0) > STALE_DAYS]
    short = [r for r in rows if r["available"] and r["bars"] < MIN_BARS]
    hist_short = [r for r in rows if r["available"] and MIN_BARS <= r["bars"] < SHORT_HISTORY_BARS]
    degenerate = [r for r in rows if r["available"] and not r["rs_available"]]
    weak = [r for r in rows if r["available"] and r["rs_weak"]]
    weak_m = [r for r in rows if r["available"] and r["rs_market_weak"]]
    lines.append(f"  · 可用 {len(rows) - len(bad)}/{len(rows)}   不可用 {len(bad)}   "
                 f"过期 {len(stale)}   预热不足 {len(short)}   "
                 f"历史<10年 {len(hist_short)}   "
                 f"RS 不可用 {len(degenerate)}   RS(风格)退化 {len(weak)}   "
                 f"RS(大盘)退化 {len(weak_m)}")
    for r in bad:
        lines.append(f"  ❌ {r['theme']}: {r['status']}")
    for r in degenerate:
        lines.append(f"  ➖ {r['theme']}: RS 层 N/A（index == anchor）")
    for r in hist_short:
        lines.append(f"  ⚠️ {r['theme']}: 历史 {r['bars']} 根 < {SHORT_HISTORY_BARS}（≈10 年）"
                     f"⇒ **只观察、不作验证依据**")
    for r in weak:
        lines.append(f"  ⚠️ {r['theme']}: RS(风格) 退化（与该主题的锚相关 {r['corr_with_anchor']}）")
    for r in weak_m:
        lines.append(f"  ⚠️ {r['theme']}: RS(大盘) 退化（与 {MARKET_ANCHOR} 相关 "
                     f"{r['corr_with_market']}）")
    for r in short:
        lines.append(f"  ⚠️ {r['theme']}: {'；'.join(r['warnings'])}")

    lines.append("")
    lines.append("【独立价格序列】（⚠️ 共用同一条指数 = 不是独立观测）")
    proxies = [r for r in rows if r.get("proxy_of")]
    if proxies:
        lines.append(f"  ⚠️ 代理项 {len(proxies)} 个（无本主题指数，借用别的主题的指数）: "
                     + ", ".join(f"{r['theme']}→{r['proxy_of']}" for r in proxies))
    by_idx: dict = {}
    for r in rows:
        by_idx.setdefault(r["index"], []).append(r["theme"])
    for idx, names in sorted(by_idx.items(), key=lambda kv: (-len(kv[1]), str(kv[0]))):
        lines.append(f"  {str(idx):<9} {len(names)} 个主题: {', '.join(names)}")
    lines.append(f"  ⇒ 共 {len(by_idx)} 条不同价格序列（观察项总数 {len(rows)}）")

    lines.append("")
    lines.append(f"  {DISCIPLINE_NOTE}")
    lines.append(f"  {VALIDATION_NOTE}")
    lines.append("  相对强度两列：**风格** = 主题÷自己的锚（冻结项 §G1，回答『同风格内部强不强』）；"
                 f"**大盘** = 主题÷{MARKET_ANCHOR} 沪深300（回答『相对大盘强不强』）")
    lines.append("    符号 ✅=比价走强  ·=未走强  N/A=index 与锚同一个（比价恒为 1）  "
                 f"＝=该主题的锚本来就是沪深300（两列相同）  "
                 f"⚠️=主题与锚相关 >{RS_WEAK_CORR}（比价近乎常数，**该列不可信**）")
    lines.append("    ⚠️ 两条一起看才有信息量：风格✓大盘✗ = 行业 alpha 在、风格 beta 逆风；"
                 "风格✗大盘✓ = 搭了风格的车、自身不强。**两列都只显示、不决策**")
    return "\n".join(lines)


__all__ = [
    "DISCIPLINE_NOTE", "VALIDATION_NOTE", "STALE_DAYS", "MIN_BARS",
    "SHORT_HISTORY_BARS", "RS_WEAK_CORR", "RECENT_SPAN_DAYS", "MARKET_ANCHOR",
    "arbitration_note", "theme_status", "build_sentinel", "format_sentinel",
]
