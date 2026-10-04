#!/usr/bin/env python
"""腾讯源行情数据层体检（P0.1 验收脚本）。

回答一个问题：**Phase 1 / Phase 2 需要的标的，腾讯源到底拿不拿得到、有多少历史？**

三组标的：
    ① 宽基指数     —— regime / 择时的基准
    ② 主题指数     —— 主题级择时的价格锚（长历史，用于验证）
    ③ ETF + 成分股 —— 实际执行载体 + 合成主题指数的原料

两种模式：
    默认（离线）  —— 只读本地缓存，不联网，**秒回**。用于日常巡检。
    --online     —— 联网取数并写缓存（首次填充 / 定期刷新用）。

退出码：
    0 = 所有必需标的都有数据
    1 = 有必需标的缺失（可直接当 CI/人工门禁使用）

用法:
    .venv/bin/python scripts/verify_marketdata.py              # 离线体检
    .venv/bin/python scripts/verify_marketdata.py --online     # 联网填充+体检
    .venv/bin/python scripts/verify_marketdata.py --online --only etf
    .venv/bin/python scripts/verify_marketdata.py --json       # 机器可读
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.marketdata_tx import (  # noqa: E402
    KIND_EQUITY,
    KIND_INDEX,
    MarketDataError,
    describe_cache,
    fetch_equity_history,
    fetch_index_history,
)

# ====================================================================
# 标的清单（**每条都必须实测过才登记**）
# ====================================================================

# ① 宽基指数：regime / 择时基准
BROAD_INDICES = [
    ("sh000001", "上证综指"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000300", "沪深300"),
    ("sh000905", "中证500"),
    ("sh000852", "中证1000"),
    ("sh000016", "上证50"),
    ("sz399005", "中小100"),
    ("sh000688", "科创50"),
]

# ② 主题指数：主题级择时的价格锚（历史长 → 用于验证；ETF 自身历史太短）
THEME_INDICES = [
    ("sz399363", "国证半导体芯片类"),
    ("sh000998", "中证TMT"),
    ("sh000827", "中证环保"),
    ("sz399808", "中证新能"),
    ("sz399976", "中证新能车"),
    ("sz399989", "中证医疗"),
    ("sz399997", "中证白酒"),
    ("sh000819", "有色金属"),
]

# ③ 实盘 ETF（用户 11 只持仓/跟踪）
ETFS = [
    ("sh562590", "半导体材料设备"),
    ("sz159516", "半导体材料设备"),
    ("sh561980", "半导体产业"),
    ("sh512480", "半导体"),
    ("sh512760", "芯片"),
    ("sh516510", "云计算与大数据"),
    ("sh561310", "消费电子"),
    ("sh561380", "电网设备"),
    ("sh588170", "科创半导体材料设备"),
    ("sz159781", "科创创业50"),
    ("sh517520", "黄金产业"),
]

# ④ 成分股抽样：半导体设备(801490, 6 只) —— 验证"用成分股合成主题指数"这条路
CONSTITUENTS = [
    ("sz002371", "北方华创"),
    ("sz300567", "精测电子"),
    ("sz300604", "长川科技"),
    ("sh600641", "万业企业"),
    ("sh603690", "至纯科技"),
    ("sh688012", "中微公司"),
]

GROUPS = {
    "broad": (BROAD_INDICES, KIND_INDEX, "宽基指数"),
    "theme": (THEME_INDICES, KIND_INDEX, "主题指数"),
    "etf": (ETFS, KIND_EQUITY, "实盘 ETF"),
    "stock": (CONSTITUENTS, KIND_EQUITY, "主题成分股(抽样)"),
}

# 验收门槛：这些组必须有数据（成分股抽样只是"能看到"的证据，不参与门禁）
REQUIRED_GROUPS = ("broad", "theme", "etf")
# 最少历史根数：主题指数要够长，才能做双体制校验
MIN_BARS = {"broad": 1000, "theme": 1000, "etf": 200, "stock": 200}


def _collect(group: str, online: bool) -> list:
    codes, kind, _label = GROUPS[group]
    rows = []
    for code, name in codes:
        rec = {"group": group, "code": code, "name": name, "kind": kind,
               "ok": False, "rows": 0, "first": None, "last": None, "note": ""}
        if online:
            fn = fetch_index_history if kind == KIND_INDEX else fetch_equity_history
            try:
                # allow_stale=False：体检要的是"现在能不能取到"，不是"缓存里有没有"
                df = fn(code, allow_stale=False)
            except MarketDataError as e:
                rec["note"] = f"解析失败: {e}"
                rows.append(rec)
                continue
            if df is None or df.empty:
                rec["note"] = "取数失败（详见 WARNING 日志）"
                rows.append(rec)
                continue
            rec.update(ok=True, rows=len(df),
                       first=df["date"].iloc[0].date().isoformat(),
                       last=df["date"].iloc[-1].date().isoformat())
        else:
            # 离线：读缓存体检
            info = describe_cache([(code, kind)]).iloc[0]
            if bool(info["cached"]):
                rec.update(ok=True, rows=int(info["rows"]),
                           first=info["first"], last=info["last"])
            else:
                rec["note"] = "无缓存（用 --online 填充）"
        rows.append(rec)
    return rows


def _print_table(rows: list, label: str) -> None:
    if not rows:
        return
    print(f"\n【{label}】")
    print(f"  {'代码':<10} {'名称':<22} {'根数':>6}  {'起':<11} {'止':<11} {'状态'}")
    print("  " + "-" * 78)
    for r in rows:
        mark = "✅" if r["ok"] else "❌"
        print(f"  {r['code']:<10} {r['name'][:20]:<22} {r['rows']:>6}  "
              f"{(r['first'] or '-'):<11} {(r['last'] or '-'):<11} {mark} {r['note']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="腾讯源行情数据层体检")
    ap.add_argument("--online", action="store_true",
                    help="联网取数并写缓存（默认只读缓存，不联网）")
    ap.add_argument("--only", choices=sorted(GROUPS), action="append",
                    help="只检查指定组（可重复）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    selected = args.only or sorted(GROUPS)
    all_rows: list = []
    for g in selected:
        all_rows.extend(_collect(g, online=args.online))

    if args.json:
        print(json.dumps(all_rows, ensure_ascii=False, indent=2))
    else:
        mode = "联网" if args.online else "离线(只读缓存)"
        print("=" * 82)
        print(f"腾讯源行情数据层体检 —— 模式: {mode}   标的数: {len(all_rows)}")
        print("=" * 82)
        for g in selected:
            grp_rows = [r for r in all_rows if r["group"] == g]
            _print_table(grp_rows, GROUPS[g][2])

        print("\n【结论】")
        for g in selected:
            grp_rows = [r for r in all_rows if r["group"] == g]
            ok = sum(1 for r in grp_rows if r["ok"])
            need = MIN_BARS.get(g, 0)
            short = [r["code"] for r in grp_rows if r["ok"] and r["rows"] < need]
            gate = "必需" if g in REQUIRED_GROUPS else "参考"
            flag = "✅" if ok == len(grp_rows) else ("❌" if g in REQUIRED_GROUPS else "⚠️")
            line = f"  {flag} {GROUPS[g][2]:<20} {ok}/{len(grp_rows)} 可取   [{gate}]"
            if short:
                line += f"  ⚠️ 历史 <{need} 根: {', '.join(short)}"
            print(line)
        if not args.online:
            print("\n  ℹ️  离线模式只反映缓存现状；要联网填充/刷新请加 --online")

    # —— 门禁 ——
    failed = [r for r in all_rows
              if r["group"] in REQUIRED_GROUPS and not r["ok"]]
    if failed:
        print(f"\n❌ 体检未通过：{len(failed)} 个必需标的无数据 → "
              f"{', '.join(r['code'] for r in failed)}")
        return 1
    print("\n✅ 体检通过：所有必需标的均有数据")
    return 0


if __name__ == "__main__":
    sys.exit(main())
