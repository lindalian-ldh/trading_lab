#!/usr/bin/env python
"""主题宇宙审计 CLI —— 主题 ↔ 题材 ↔ 成分股 ↔ ETF ↔ 宽基锚。

**为什么需要它**：Phase 1 面板的"上榜率"需要一个分母（成分股数），
Phase 2 的主题择时需要一个"主题 ↔ 指数/ETF"的映射。
而 zzshare 题材宽度差异极大（半导体设备 6 只 ~ 人工智能 1531 只），
**不审计就会把宽题材的稀释当成信号**。

用法:
    # ① 审计（默认，不联网；读缓存）
    .venv/bin/python scripts/list_themes.py

    # ② 首次/刷新成分股（联网，每个题材 1 次调用）
    .venv/bin/python scripts/list_themes.py --refresh

    # ③ 只看主题用到的 ETF（与行情层覆盖对账）
    .venv/bin/python scripts/list_themes.py --etfs

    # ④ 导出某主题的成分股
    .venv/bin/python scripts/list_themes.py --constituents 半导体设备

    # ⑤ 机器可读
    .venv/bin/python scripts/list_themes.py --json

退出码：
    0 = 审计通过（所有主题有成分股、锚可用）
    1 = 有问题（无成分股 / 锚不可用）—— 可作门禁
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.theme_universe import (  # noqa: E402
    audit,
    build_constituents,
    etf_coverage,
    format_audit,
    get_theme,
    load_theme_constituents,
    plates_used,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="主题宇宙审计")
    ap.add_argument("--refresh", action="store_true",
                    help="联网刷新成分股（每个题材 1 次调用）")
    ap.add_argument("--etfs", action="store_true", help="列出主题用到的 ETF")
    ap.add_argument("--constituents", metavar="主题", help="导出某主题的成分股")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    # —— 导出某主题成分股 ——
    if args.constituents:
        t = get_theme(args.constituents)
        if t is None:
            from core.theme_universe import theme_names
            print(f"未知主题: {args.constituents}\n可用: {', '.join(theme_names())}")
            return 1
        cons = load_theme_constituents(args.constituents)
        if cons.empty:
            print(f"⚠️ {args.constituents} 无成分股缓存 —— 先跑 --refresh")
            return 1
        # 按题材归组展示（并集前先去重展示来源）
        for (pcode, pname), sub in cons.groupby(["plate_code", "plate_name"], sort=True):
            print(f"\n【{pname}（{pcode}）】{sub['stock_code'].nunique()} 只")
            print("  " + "、".join(f"{r.stock_code} {r.stock_name}"
                                   for r in sub.sort_values("stock_code").itertuples()))
        uniq = cons.drop_duplicates(subset=["stock_code"])
        print(f"\n合计（跨题材去重）：{len(uniq)} 只")
        return 0

    # —— ETF 清单 ——
    if args.etfs:
        df = etf_coverage()
        if args.json:
            print(json.dumps(df.to_dict(orient="records"), ensure_ascii=False, indent=2))
            return 0
        print("=" * 76)
        print(f"主题用到的 ETF（{len(df)} 只）")
        print("=" * 76)
        for _, r in df.iterrows():
            print(f"  {r['etf']:<10} {r['name']:<22} ← {r['theme']}")
        print("=" * 76)
        print("  对账：.venv/bin/python scripts/verify_marketdata.py --only etf")
        return 0

    # —— 刷新成分股 ——
    if args.refresh:
        build_constituents(force=True, verbose=True)
        print()

    # —— 审计 ——
    if args.json:
        df = audit(verbose=False)
        print(json.dumps(df.to_dict(orient="records"), ensure_ascii=False, indent=2))
    else:
        df = audit(verbose=True)
        print(f"\n  用到的题材编码（去重 {len(plates_used())} 个）: {','.join(plates_used())}")

    bad = df[(df["n_constituents"] == 0) | (~df["anchor_ok"])]
    if bad.empty:
        print("\n✅ 审计通过：所有主题均有成分股，锚均可用")
        return 0
    print(f"\n❌ 审计未通过：{len(bad)} 个主题有问题（见上表 [缺题材] / 锚 ❌）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
