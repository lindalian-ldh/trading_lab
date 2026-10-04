"""主题宇宙定义表 —— 主题 ↔ 题材(801xxx) ↔ 成分股 ↔ ETF ↔ 宽基锚。

## ⚠️ 2026-10-04 换指数（半导体系 4 个主题）

半导体系（半导体设备 / 半导体设备材料 / 芯片 / 第三代半导体）的 `index`
由 `sz399363` 改为 **`sh000039` 上证信息**（P0.5 修订记录 #11）。

**为什么**：`sz399363` 与半导体系 ETF 的前复权日收益相关性只有 0.714~0.838
（半导体材料设备 **0.714**、半导体产业 **0.773**，**低于 0.8 的验收线**）。

**怎么选的（规则在跑数之前声明，不是事后挑最好的）**：在 4 个已过 ≥10 年门槛的
候选（`sh000039` / `sh000935` / `sh000993` / `sz399811`）里，取
「半导体系 5 只 ETF × 2 个窗口（全重叠 / 近 500 日）中**最小相关性**」最大者。
实测 `sh000039` 最小相关性 **0.835**（其余 0.824 / 0.823 / 0.820）。

**已知代价（不许掩盖）**：上证信息是**宽泛 IT 指数**（含软件、通信、云计算），
相关性 ≥0.8 只说明「日收益同步」，**不代表它代表"半导体设备"**。
语义正确的做法仍是**合成指数**（P0.4c）。另外这次选择本身带**选择偏差**
（68 × 11 次相关性取最大）⇒ 用它的重跑只能算**样本内探索性**，**OOS 段仍锁定**。

## 为什么需要它

1. **Phase 1 面板的"上榜率"分母**：`上榜率 = 上榜家数 / 成分股数` —— 没有成分股数就没法归一化，
   而**不归一化就无法跨题材比较**（半导体设备 6 只 vs 人工智能 1531 只）。
2. **Phase 2 主题级择时**：需要"主题 ↔ 现成指数/ETF ↔ 宽基锚"的映射来做 RS 与转折判定。
3. **防止"题材宽度"污染信号**：zzshare 题材的成分股数差异极大，见下表。

## 关键设计：主题 = 窄题材的**组合**，不是单个宽题材

实测成分股数（2026-10-03，`plates_stocks(17)`）：

| 题材 | 编码 | 成分股 |
|---|---|---|
| 半导体设备 | 801490 | **6** |
| 电子气体 | 801453 | 11 |
| 光刻机 | 801476 | 16 |
| 芯片封测 | 801150 | 17 |
| 光刻胶 | 801222 | 34 |
| 第三代半导体 | 801068 | 49 |
| 黄金 | 801048 | 89 |
| 云计算 | 801260 | 122 |
| 电气设备 | 801562 | 203 |
| 智能电网 | 801346 | **500** |
| 科创板 | 801351 | 618 |
| 消费电子 | 801328 | **724** |
| **芯片** | **801001** | **1134** |
| 人工智能 | 801085 | **1531** |

⇒ 若把"半导体"定义成 `801001 芯片`（1134 只），上榜率会被 1000+ 只稀释到几乎无区分度；
而定义成 `801490 半导体设备 ∪ 801222 光刻胶 ∪ 801476 光刻机 ∪ 801453 电子气体 ∪ 801150 芯片封测`
（去重后几十只）就聚焦得多。**本模块因此支持一个主题由多个题材求并集。**

## 宽度分级（决定了"上榜率"能不能用）

| 级别 | 成分股数 | 含义与用法 |
|---|---|---|
| **窄** | ≤ 30 | 上榜率极敏感，最适合"持续性"观察 |
| **中** | 31 ~ 300 | 可用；建议配合"相对自身历史的分位" |
| **宽** | > 300 | ⚠️ 上榜率被稀释，**必须**配分位使用，或改用更窄的题材组合 |

## 数据来源与诚实性

- 题材编码/名称：`zzshare.plates_list(plate_type=17)`（520 个；见 `core/lhb_store.fetch_theme_table`）
- 成分股：`zzshare.plates_stocks(plate_type=17, plate_code)`（带 `time_in`，缓存于
  `data/lhb/plate_constituents.parquet`）
- ⚠️ **不在 `plates_list(17)` 内的编码取不到成分股**（实测 13.6% 的行，如 信创/存储/数据要素/DeepSeek）
  ⇒ 这些题材在面板里**只能给上榜家数，不能给上榜率**。本模块的审计会明确标出来。
- ⚠️ `plates_stocks` 返回的是**当前成分股**（含 `time_in` 但 `time_out` 为空）
  ⇒ **已退出的成分股看不到**，存在残余幸存者偏差。对"资金温度计"用途可接受，
  但**不可宣称无偏**。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import pandas as pd

from core.lhb_store import (
    PLATE_CONST_COLUMNS,
    fetch_plate_constituents,
    load_plate_constituents,
)

logger = logging.getLogger(__name__)

# ====================================================================
# 宽度分级阈值
# ====================================================================

NARROW_MAX = 30        # ≤ 30 只 = 窄（上榜率极敏感）
BROAD_MIN = 300        # > 300 只 = 宽（上榜率被稀释，必须配分位）


def width_tier(n: int) -> str:
    """成分股数 → 宽度级别（'窄' / '中' / '宽' / '无'）。"""
    if n is None or n <= 0:
        return "无"
    if n <= NARROW_MAX:
        return "窄"
    if n <= BROAD_MIN:
        return "中"
    return "宽"


def is_broad(n: int) -> bool:
    return n is not None and n > BROAD_MIN


# ====================================================================
# 主题定义（**人工维护**，改动请连同 note 一起更新）
# ====================================================================
# 字段说明：
#   theme   : 主题名（面板上显示的名字）
#   plates  : [(题材编码, 题材名), ...] —— **并集**构成该主题
#   etfs    : [(腾讯代码, 简称), ...] —— 实际执行载体
#   anchor  : 宽基锚（RS 与择时对照；必须是 core.marketdata_tx.INDEX_CODES 内的代码）
#   index   : 可选，交易所可取的现成主题指数（Phase 2 用于拿长历史）
#   note    : 口径说明 / 已知问题
THEMES: tuple = (
    {
        "theme": "半导体设备",
        "plates": (("801490", "半导体设备"),),
        "etfs": (("sh562590", "半导体材料设备"), ("sz159516", "半导体材料设备")),
        "anchor": "sz399006",
        "index": "sh000039",
        "note": "最聚焦的核心主题（仅 6 只成分股）；你的主线。"
                "⚠️ 2026-10-04 指数由 sz399363 换为 sh000039 上证信息（原指数与 ETF 相关性仅 0.714）；"
                "上证信息是宽泛 IT 指数，属**便宜的近似**，语义正确的做法仍是合成指数",
    },
    {
        "theme": "半导体设备材料",
        "plates": (("801490", "半导体设备"), ("801222", "光刻胶"),
                   ("801476", "光刻机"), ("801453", "电子气体"),
                   ("801150", "芯片封测")),
        "etfs": (("sh561980", "半导体产业"),),
        "anchor": "sz399006",
        "index": "sh000039",
        "note": "设备+材料+封测的并集；比单看 801001 芯片聚焦得多。"
                "⚠️ 2026-10-04 指数由 sz399363 换为 sh000039 上证信息（原指数与 ETF 相关性仅 0.773）",
    },
    {
        "theme": "芯片",
        "plates": (("801001", "芯片"),),
        "etfs": (("sh512480", "半导体"), ("sh512760", "芯片")),
        "anchor": "sz399006",
        "index": "sh000039",
        "note": "⚠️ 1134 只，过宽 —— 上榜率会被稀释，务必配『相对自身历史分位』使用。"
                "指数 2026-10-04 由 sz399363 换为 sh000039 上证信息（相关性 0.937~0.962）",
    },
    {
        "theme": "第三代半导体",
        "plates": (("801068", "第三代半导体"),),
        "etfs": (),
        "anchor": "sz399006",
        "index": "sh000039",
        "note": "49 只，宽度适中。指数 2026-10-04 由 sz399363 换为 sh000039 上证信息"
                "（该主题**无 ETF**，相关性无法校验 ⇒ 按 kill criterion 5 只显示不验证）",
    },
    {
        "theme": "云计算与大数据",
        "plates": (("801260", "云计算"), ("801103", "大数据"), ("801550", "国资云")),
        "etfs": (("sh516510", "云计算与大数据"),),
        "anchor": "sz399006",
        "index": "sh000998",
        "note": "三题材并集（约 229 条记录，去重后更少）",
    },
    {
        "theme": "消费电子",
        "plates": (("801328", "消费电子"),),
        "etfs": (("sh561310", "消费电子"),),
        "anchor": "sz399006",
        "index": "sh000998",
        "note": "⚠️ 724 只，过宽",
    },
    {
        "theme": "电网设备",
        "plates": (("801346", "智能电网"),),
        "etfs": (("sh561380", "电网设备"),),
        "anchor": "sh000001",
        "index": "sz399808",
        "note": "⚠️ 801346 有 500 只，偏宽。若需更窄口径可改用 801562 电气设备(203) 替代；"
                "原先把两者并集(533)反而比单用 801346 更宽 —— 并集只应用于『补全口径』",
    },
    {
        "theme": "科创半导体",
        "plates": (("801490", "半导体设备"), ("801068", "第三代半导体")),
        "etfs": (("sh588170", "科创半导体材料设备"),),
        "anchor": "sh000688",
        "index": "sh000688",
        "note": "用半导体题材（设备+三代）代理；ETF 另加了『科创板上市』一层过滤，"
                "本口径**无法复现**（成分股会多于 ETF 实际持仓）。"
                "原先并入 801351 科创板(618) 会把口径拉宽到 667，已移除",
    },
    {
        "theme": "科创创业50",
        "plates": (("801351", "科创板"),),
        "etfs": (("sz159781", "科创创业50"),),
        "anchor": "sh000688",
        "index": "sh000688",
        "note": "⚠️ 618 只，过宽；该 ETF 是宽基性质，主题择时意义有限",
    },
    {
        "theme": "黄金",
        "plates": (("801048", "黄金"),),
        "etfs": (("sh517520", "黄金产业"),),
        "anchor": "sh000001",
        "index": "sh000819",
        "note": "89 只；⚠️ 商品类与 A 股低相关，任何 A 股锚都意义有限（沿用既有警示口径）",
    },
)


# ====================================================================
# 查询
# ====================================================================

def theme_names() -> list:
    return [t["theme"] for t in THEMES]


def get_theme(theme: str) -> Optional[dict]:
    for t in THEMES:
        if t["theme"] == theme:
            return t
    return None


def theme_by_etf(tx_code: str) -> Optional[dict]:
    """按 ETF 代码反查主题。"""
    code = str(tx_code).strip().lower()
    for t in THEMES:
        for c, _ in t["etfs"]:
            if c.lower() == code:
                return t
    return None


def theme_plates(theme: str) -> list:
    t = get_theme(theme)
    return list(t["plates"]) if t else []


# ====================================================================
# 成分股（并集）
# ====================================================================

def build_constituents(force: bool = False, verbose: bool = True) -> pd.DataFrame:
    """拉取所有主题涉及的题材成分股并落盘（每个题材 1 次调用）。

    返回 (theme, plate_code, plate_name, stock_code, stock_name) 长表 ——
    同一个股票若属于同一主题的多个题材，会出现多行；**去重用 `theme_constituent_counts`**。
    """
    plates = sorted({(code, name) for t in THEMES for code, name in t["plates"]})
    if verbose:
        print(f"拉取成分股：{len(plates)} 个题材（去重后）")
    for i, (code, name) in enumerate(plates, 1):
        df = fetch_plate_constituents(code, force=force)
        if verbose:
            print(f"  [{i}/{len(plates)}] {code} {name:<12} {len(df):>5} 只")

    # 组装 theme 维度
    cons = load_plate_constituents()
    if cons is None or cons.empty:
        return pd.DataFrame(columns=["theme"] + [c for c in PLATE_CONST_COLUMNS if c != "plate_type"])
    rows = []
    for t in THEMES:
        codes = {c for c, _ in t["plates"]}
        sub = cons[cons["plate_code"].astype(str).isin(codes)].copy()
        if sub.empty:
            continue
        sub["theme"] = t["theme"]
        rows.append(sub)
    if not rows:
        return pd.DataFrame(columns=["theme"] + [c for c in PLATE_CONST_COLUMNS if c != "plate_type"])
    out = pd.concat(rows, ignore_index=True)
    cols = ["theme", "plate_code", "plate_name", "stock_code", "stock_name", "time_in"]
    return out[cols].reset_index(drop=True)


def load_theme_constituents(theme: Optional[str] = None) -> pd.DataFrame:
    """读已缓存的成分股（不联网），可按主题过滤。"""
    rows = []
    for t in THEMES:
        if theme is not None and t["theme"] != theme:
            continue
        codes = {c for c, _ in t["plates"]}
        sub = load_plate_constituents()
        if sub is None or sub.empty:
            continue
        sub = sub[sub["plate_code"].astype(str).isin(codes)].copy()
        if sub.empty:
            continue
        sub["theme"] = t["theme"]
        rows.append(sub)
    if not rows:
        return pd.DataFrame(columns=["theme", "plate_code", "plate_name",
                                     "stock_code", "stock_name", "time_in"])
    out = pd.concat(rows, ignore_index=True)
    return out[["theme", "plate_code", "plate_name", "stock_code",
                "stock_name", "time_in"]].reset_index(drop=True)


def theme_constituent_counts() -> dict:
    """{主题: 去重后的成分股数}。**这是"上榜率"的分母。**"""
    cons = load_theme_constituents()
    if cons.empty:
        return {t["theme"]: 0 for t in THEMES}
    g = cons.drop_duplicates(subset=["theme", "stock_code"]).groupby("theme").size()
    return {t["theme"]: int(g.get(t["theme"], 0)) for t in THEMES}


# ====================================================================
# 审计
# ====================================================================

def audit(verbose: bool = True) -> pd.DataFrame:
    """体检：每个主题的成分股数、宽度级别、ETF/锚的有效性。

    **不联网**（只读缓存）。若成分股未拉取，先跑 `build_constituents()`。
    """
    from core.marketdata_tx import INDEX_CODES

    counts = theme_constituent_counts()
    cons = load_theme_constituents()
    rows = []
    for t in THEMES:
        codes = {c for c, _ in t["plates"]}
        sub = cons[cons["theme"] == t["theme"]] if not cons.empty else cons
        got_plates = sorted(set(sub["plate_code"].astype(str))) if not sub.empty else []
        missing_plates = sorted(codes - set(got_plates))
        n = counts.get(t["theme"], 0)
        n_stocks_union = int(sub["stock_code"].nunique()) if not sub.empty else 0
        rows.append({
            "theme": t["theme"],
            "plates": ",".join(codes),
            "n_plates": len(codes),
            "n_plates_ok": len(got_plates),
            "plates_missing": ",".join(missing_plates),
            "n_constituents": n_stocks_union,
            "width": width_tier(n_stocks_union),
            "too_broad": is_broad(n_stocks_union),
            "n_etf": len(t["etfs"]),
            "anchor": t["anchor"],
            "anchor_ok": t["anchor"] in INDEX_CODES,
            "note": t["note"],
        })
    df = pd.DataFrame(rows)
    if verbose:
        print(format_audit(df))
    return df


def format_audit(df: pd.DataFrame) -> str:
    """把 audit() 的结果排成一张可读的等宽表。"""
    if df is None or df.empty:
        return "（无主题定义）"
    lines = []
    lines.append("=" * 108)
    lines.append("主题宇宙定义表（主题 = 多个窄题材的并集）")
    lines.append("=" * 108)
    head = (f"  {'主题':<16} {'成分股':>6} {'宽度':<4} {'题材数':>5} {'ETF':>4} "
            f"{'锚':<9} {'锚OK':<5} 备注")
    lines.append(head)
    lines.append("  " + "-" * 104)
    for _, r in df.iterrows():
        mark = "⚠️" if r["too_broad"] else "  "
        note = str(r["note"])
        if r["plates_missing"]:
            note = f"[缺题材 {r['plates_missing']}] " + note
        lines.append(
            f"  {r['theme']:<16} {r['n_constituents']:>6} {r['width']:<4} "
            f"{r['n_plates']:>5} {r['n_etf']:>4} {r['anchor']:<9} "
            f"{'✅' if r['anchor_ok'] else '❌':<5} {mark}{note[:46]}")
    lines.append("  " + "-" * 104)

    n_broad = int(df["too_broad"].sum())
    n_bad_anchor = int((~df["anchor_ok"]).sum())
    n_no_cons = int((df["n_constituents"] == 0).sum())
    lines.append(f"  合计 {len(df)} 个主题 | 过宽 {n_broad} 个 | 锚不可用 {n_bad_anchor} 个 "
                 f"| 无成分股 {n_no_cons} 个")
    if n_no_cons:
        lines.append("  ⚠️ 无成分股 ⇒ 该主题**只能给上榜家数，不能给上榜率**（不许编一个率出来）")
    if n_broad:
        lines.append("  ⚠️ 过宽（>300 只）⇒ 上榜率被稀释，**必须**配『相对自身历史分位』使用，"
                     "或改用更窄的题材组合")
    lines.append("  宽度分级：窄 ≤30 | 中 31~300 | 宽 >300")
    lines.append("=" * 108)
    return "\n".join(lines)


def etf_coverage() -> pd.DataFrame:
    """列出所有主题用到的 ETF（供与 marketdata_tx 覆盖情况对账）。"""
    rows = []
    for t in THEMES:
        for code, name in t["etfs"]:
            rows.append({"theme": t["theme"], "etf": code, "name": name})
    return pd.DataFrame(rows, columns=["theme", "etf", "name"])


def plates_used() -> list:
    """返回所有主题用到的题材编码（去重、排序）。"""
    return sorted({c for t in THEMES for c, _ in t["plates"]})


__all__ = [
    "THEMES",
    "NARROW_MAX",
    "BROAD_MIN",
    "width_tier",
    "is_broad",
    "theme_names",
    "get_theme",
    "theme_by_etf",
    "theme_plates",
    "build_constituents",
    "load_theme_constituents",
    "theme_constituent_counts",
    "audit",
    "format_audit",
    "etf_coverage",
    "plates_used",
]
