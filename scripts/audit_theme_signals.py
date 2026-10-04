#!/usr/bin/env python
"""P2.2 频次审计 —— **在谈收益之前**先问：信号到底一年来几次？

## 为什么必须先做这一步

方案文档第五章坑 1：**L1 与 L2 不独立**，"三层同时满足"的联合频次可能坍缩到 0~2 次/年。
"一年 3~5 次"是**从 PANIC_DOWN 抄来的**（创业板指 4.4 次/年），**不能自动迁移**。
所以本脚本在跑任何收益回测之前，先用实测数字回答频次问题 ——
若联合频次不达标，按 pre-registered **kill criterion 3** 退化为"只观察"，
**而不是**回头去放宽冻结参数。

## 窗口纪律（P0.5 §⑥-3/§⑦）

```
预热（不计入）   < 2016-01-01        ← 均线/摆动点需要历史
主窗口 A+B       2016-01-01 ~ 2023-12-31   ← 本脚本**只**统计这里
OOS 锁定段       2024-01-01 ~ 2026-09-30   ← **已锁定，本脚本一行都不算**
```

## 频次口径

**片段数（episodes）**，不是天数：相邻（索引间隔 ≤3 日）的 True 合并为同一片段 ——
与 [`audit_regime_hold.py`](audit_regime_hold.py) 的 `episodes()` 一致，
也就是"恐慌择时 33 个片段 / 10 年 ≈ 3.3 次/年"的同一口径。

## 用法

    .venv/bin/python scripts/audit_theme_signals.py            # 频次报告
    .venv/bin/python scripts/audit_theme_signals.py --gate     # 联合频次 <3 次/年 ⇒ 退出码 1
    .venv/bin/python scripts/audit_theme_signals.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.marketdata_tx import fetch_index_history  # noqa: E402
from core.theme_timing import EPISODE_GAP, episode_spans, layer_masks  # noqa: E402
from core.theme_universe import THEMES, WATCH_ONLY, all_observed  # noqa: E402

WARMUP_END = "2015-12-31"
MAIN_START = "2016-01-01"
MAIN_END = "2023-12-31"
OOS_START = "2024-01-01"       # **锁定：不计算**
OOS_END = "2026-09-30"

LAYERS = ("L1", "L2", "RS", "L1+L2", "L1+L2+RS")
MIN_JOINT_PER_YEAR = 3.0       # P0.5 §⑥-5 验收线
KILL_JOINT_PER_YEAR = 2.0      # kill criterion 3：< 2 次/年 ⇒ 只观察


def audit_theme(theme: dict, online: bool = False) -> dict:
    name = theme["theme"]
    idx, anchor = theme.get("index"), theme.get("anchor")
    rec = {"theme": name, "index": idx, "anchor": anchor, "bars": 0, "years": 0.0,
           "degenerate_rs": idx == anchor, "freq": {}, "day_share": {}, "note": ""}

    d = fetch_index_history(idx, refresh=online, prefer_cache=not online) if idx else None
    a = fetch_index_history(anchor, refresh=online, prefer_cache=not online) if anchor else None
    if d is None or d.empty:
        rec["note"] = "❌ 主题指数无数据"
        return rec

    masks = layer_masks(d, None if (a is None or rec["degenerate_rs"]) else a)
    w = masks[(masks["date"] >= MAIN_START) & (masks["date"] <= MAIN_END)]
    w = w.reset_index(drop=True)
    rec["bars"] = len(w)
    if len(w) < 30:
        rec["note"] = "❌ 主窗口样本不足 30 根"
        return rec
    years = (w["date"].iloc[-1] - w["date"].iloc[0]).days / 365.25
    rec["years"] = round(years, 2)

    for k in LAYERS:
        ep = len(episode_spans(w[k], gap=EPISODE_GAP))
        rec["freq"][k] = {"episodes": ep, "per_year": round(ep / years, 2)}
        rec["day_share"][k] = round(float(w[k].mean()), 3)

    joint = rec["freq"]["L1+L2"]["per_year"]
    if rec["degenerate_rs"]:
        rec["note"] = "⚠️ index == anchor ⇒ 比价恒为 1，**RS 层不可用（N/A）**，组合退化为 L1+L2"
    elif joint < KILL_JOINT_PER_YEAR:
        rec["note"] = f"❌ 联合频次 {joint} 次/年 < {KILL_JOINT_PER_YEAR} ⇒ 触发 kill criterion 3"
    elif joint < MIN_JOINT_PER_YEAR:
        rec["note"] = f"⚠️ 联合频次 {joint} 次/年 < 验收线 {MIN_JOINT_PER_YEAR}"
    else:
        rec["note"] = f"✅ 联合频次 {joint} 次/年 ≥ {MIN_JOINT_PER_YEAR}"
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description="P2.2 频次审计")
    ap.add_argument("--online", action="store_true", help="联网刷新指数缓存")
    ap.add_argument("--gate", action="store_true",
                    help=f"任一主题联合频次 <{MIN_JOINT_PER_YEAR} 次/年 ⇒ 退出码 1")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--themes-only", action="store_true",
                    help="只看 THEMES（10 个含题材的主题），不含 WATCH_ONLY 观察项")
    args = ap.parse_args()

    rows = [audit_theme(t, online=args.online) for t in (THEMES if args.themes_only else all_observed())]

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print("=" * 122)
        print(f"P2.2 频次审计 —— 主窗口 {MAIN_START} ~ {MAIN_END}（预热 <{WARMUP_END}）")
        print(f"     🔒 OOS 锁定段 {OOS_START} ~ {OOS_END} **未计算**（P0.5 §⑦：只看一次）")
        print(f"     口径：片段数（相邻 ≤{EPISODE_GAP} 日合并为 1 段）/ 年")
        print("=" * 122)
        print(f"  {'主题':<14}{'指数':<9}{'锚':<9}{'bars':>5}{'年':>5}  "
              + "  ".join(f"{k:>11}" for k in LAYERS))
        print("  " + "-" * 116)
        for r in rows:
            if not r["freq"]:
                print(f"  {r['theme']:<14}{str(r['index']):<9}{str(r['anchor']):<9}"
                      f"{r['bars']:>5}{r['years']:>5}  {r['note']}")
                continue
            cells = "  ".join(
                f"{r['freq'][k]['episodes']:>3}({r['freq'][k]['per_year']:>4.2f}/y)"
                for k in LAYERS)
            print(f"  {r['theme']:<14}{str(r['index']):<9}{str(r['anchor']):<9}"
                  f"{r['bars']:>5}{r['years']:>5}  {cells}")

        # —— 独立价格序列去重（10 个主题 ≠ 10 个独立检验）——
        print("\n【独立价格序列】（⚠️ 共用同一条指数 = **不是独立检验**，多重检验压力要按这个数算）")
        by_idx: dict = {}
        for r in rows:
            by_idx.setdefault(r["index"], []).append(r["theme"])
        for idx, names in sorted(by_idx.items(), key=lambda kv: -len(kv[1])):
            print(f"  {str(idx):<9} {len(names)} 个主题: {', '.join(names)}")
        print(f"  ⇒ 共 **{len(by_idx)} 条**不同价格序列（主题数 {len(rows)}）")

        print("\n【逐主题判定】")
        for r in rows:
            print(f"  {r['theme']:<14} {r['note']}")

        print("\n【状态天数占比】（L1/RS 是状态；L2 是事件）")
        for r in rows:
            if not r["freq"]:
                continue
            print(f"  {r['theme']:<14} " + "  ".join(
                f"{k}={r['day_share'][k]*100:>5.1f}%" for k in LAYERS))

        joint = [r["freq"].get("L1+L2", {}).get("per_year") for r in rows if r["freq"]]
        joint = [x for x in joint if x is not None]
        if joint:
            print(f"\n【结论】联合频次（L1+L2 同日 AND）: 中位 {pd.Series(joint).median():.2f} 次/年 "
                  f"/ 范围 {min(joint):.2f}~{max(joint):.2f}；验收线 {MIN_JOINT_PER_YEAR}、"
                  f"kill 线 {KILL_JOINT_PER_YEAR}")
            if max(joint) < MIN_JOINT_PER_YEAR:
                print("  ❌ 全部低于验收线 ⇒ 联合层**不得**进入仓位逻辑（P0.5 §⑥-5）")
            if max(joint) < KILL_JOINT_PER_YEAR:
                print("  ❌ 全部低于 kill 线 ⇒ **kill criterion 3 触发**：退化为『只观察』，")
                print("     且**不允许**回头放宽冻结参数来救频次（那属于修改预注册）")

    if args.gate:
        bad = [r for r in rows if not r["freq"]
               or r["freq"]["L1+L2"]["per_year"] < MIN_JOINT_PER_YEAR]
        print(f"\n❌ 频次门禁未通过：{len(bad)}/{len(rows)} 个主题的联合频次 <{MIN_JOINT_PER_YEAR} 次/年")
        for r in bad:
            print(f"   · {r['theme']:<14} {r['freq'].get('L1+L2', {}).get('per_year', 'N/A')}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
