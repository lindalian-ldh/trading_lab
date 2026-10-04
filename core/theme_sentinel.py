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

from core.theme_timing import EPISODE_GAP, episode_spans, layer_masks

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


def arbitration_note() -> str:
    """P2.6 的最终形态（原「仲裁表」退化为一句话）。"""
    return ("PANIC_DOWN（左侧，已验证但体制依赖）**可以**下指令；"
            "转折信号（右侧，未通过验证）**不可以** ⇒ 两者结构上不可能给出矛盾指令。")


def _last_span(mask: pd.Series):
    spans = episode_spans(mask, gap=EPISODE_GAP)
    return spans[-1] if spans else None


def theme_status(theme: dict,
                 index_df: Optional[pd.DataFrame],
                 anchor_df: Optional[pd.DataFrame],
                 as_of=None) -> dict:
    """单个主题的观察哨读数（**pure**；不取数、不落盘）。

    Args:
        theme: `core.theme_universe.THEMES` 里的一项。
        index_df / anchor_df: 主题指数 / 宽基锚日线；None 表示**取数失败**。
        as_of: 观测日（None = 序列最后一根）。**只用到 as_of 及之前的数据**。
    """
    name = theme.get("theme", "?")
    idx_code, anchor_code = theme.get("index"), theme.get("anchor")
    rec = {
        "theme": name, "index": idx_code, "anchor": anchor_code,
        "validated": False,
        "as_of": None, "bars": 0, "last_bar": None, "staleness_days": None,
        "available": False, "status": "", "warnings": [],
        "l1": False, "l2": False, "rs": False, "rs_available": idx_code != anchor_code,
        "l1_state_days": 0, "l1_span_start": None, "l1_span_end": None,
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

    a = anchor_df if rec["rs_available"] else None
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
        if rec["l1"]:
            rec["l1_state_days"] = n

    l2_pos = np.where(masks["L2"].to_numpy(bool))[0]
    if len(l2_pos):
        rec["l2_recent_days_ago"] = int(len(d) - 1 - l2_pos[-1])

    # —— 只显示的状态标签（优先级从高到低）——
    if rec["l1"] and rec["l2"]:
        rec["status"] = "🔵🔵 结构+均线双确认（观察）"
    elif rec["l1"] and rec["rs"]:
        rec["status"] = "🔵📈 结构转多 + 相对强度（观察）"
    elif rec["l1"]:
        days = rec["l1_state_days"]
        rec["status"] = (f"🔵 结构转多（观察，第 {days} 日）" if days <= 1
                         else f"🔹 结构维持（观察，已 {days} 日）")
    elif rec["l1_span_end"] is not None:
        rec["status"] = "➖ 结构已破（观察期内曾转多）"
    elif rec["rs"] and rec["rs_available"]:
        rec["status"] = "📈 相对强度走强（价格未转多）"
    else:
        rec["status"] = "➖ 无信号"

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

    rows = []
    for t in themes:
        rows.append(theme_status(t, _load(t.get("index")), _load(t.get("anchor")),
                                 as_of=as_of))
    return rows


def format_sentinel(rows: list, as_of=None) -> str:
    """ASCII 渲染（含**数据充分度**段与两条硬约束横幅）。"""
    lines = []
    lines.append("=" * 104)
    lines.append(f"主题转折观察哨 —— 观测日 {as_of or '(各序列最后一根)'}    "
                 f"主题数 {len(rows)}")
    lines.append("=" * 104)
    lines.append(f"  {VALIDATION_NOTE}")
    lines.append(f"  {DISCIPLINE_NOTE}")
    lines.append("  " + arbitration_note())
    lines.append("")
    lines.append(f"  {'主题':<15}{'指数':<9}{'锚':<9}{'根数':>6}  {'最后交易日':<12}"
                 f"{'L1':>4}{'L2':>4}{'RS':>4}  状态")
    lines.append("  " + "-" * 100)
    for r in rows:
        if not r["available"]:
            lines.append(f"  {r['theme']:<15}{str(r['index']):<9}{str(r['anchor']):<9}"
                         f"{r['bars']:>6}  {(r['last_bar'] or '-'):<12}"
                         f"{'?':>4}{'?':>4}{'?':>4}  {r['status']}")
            continue
        rs = "✅" if r["rs"] else ("N/A" if not r["rs_available"] else "·")
        lines.append(f"  {r['theme']:<15}{str(r['index']):<9}{str(r['anchor']):<9}"
                     f"{r['bars']:>6}  {r['last_bar']:<12}"
                     f"{'🔵' if r['l1'] else '·':>4}{'⚡' if r['l2'] else '·':>4}{rs:>4}"
                     f"  {r['status']}")

    lines.append("")
    lines.append("【数据充分度】")
    bad = [r for r in rows if not r["available"]]
    stale = [r for r in rows if r["available"] and (r["staleness_days"] or 0) > STALE_DAYS]
    short = [r for r in rows if r["available"] and r["bars"] < MIN_BARS]
    degenerate = [r for r in rows if r["available"] and not r["rs_available"]]
    lines.append(f"  · 可用 {len(rows) - len(bad)}/{len(rows)}   不可用 {len(bad)}   "
                 f"过期 {len(stale)}   历史不足 {len(short)}   RS 不可用 {len(degenerate)}")
    for r in bad:
        lines.append(f"  ❌ {r['theme']}: {r['status']}")
    for r in degenerate:
        lines.append(f"  ➖ {r['theme']}: RS 层 N/A（index == anchor）")
    for r in short:
        lines.append(f"  ⚠️ {r['theme']}: {'；'.join(r['warnings'])}")

    lines.append("")
    lines.append("【独立价格序列】（⚠️ 共用同一条指数 = 不是独立观测）")
    by_idx: dict = {}
    for r in rows:
        by_idx.setdefault(r["index"], []).append(r["theme"])
    for idx, names in sorted(by_idx.items(), key=lambda kv: (-len(kv[1]), str(kv[0]))):
        lines.append(f"  {str(idx):<9} {len(names)} 个主题: {', '.join(names)}")
    lines.append(f"  ⇒ 共 {len(by_idx)} 条不同价格序列（主题数 {len(rows)}）")

    lines.append("")
    lines.append(f"  {DISCIPLINE_NOTE}")
    lines.append(f"  {VALIDATION_NOTE}")
    return "\n".join(lines)


__all__ = [
    "DISCIPLINE_NOTE", "VALIDATION_NOTE", "STALE_DAYS", "MIN_BARS",
    "arbitration_note", "theme_status", "build_sentinel", "format_sentinel",
]
