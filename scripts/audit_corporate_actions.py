#!/usr/bin/env python
"""企业行为（份额折算/拆分/送转）审计门禁 —— P0.4a 验收脚本。

## 为什么需要它

腾讯源的 **ETF/个股日线不含复权**，份额折算是**断崖式**的：

| 标的 | 日期 | 单日 | 真相 |
|---|---|---|---|
| sh512480 半导体ETF | 2021-03-29 | −48.90% | 1:2 份额折算 |
| sh512480 | 2026-07-03 | −50.70% | 折算 |
| sh512760 芯片ETF | 2020-09-10 | +53.10% | 份额合并 |
| sz159516 | 2026-03-30 | −48.67% | 折算 |

**危害（实测）**：512480 与 512760（两只几乎同质的半导体 ETF）日收益相关性
**原始 0.666 → 剔除断崖后 0.990**。若不修，P0.4 的
「指数 vs ETF 日收益相关性 ≥ 0.8」会把达标的东西判成不达标。

## 本脚本做什么

1. 逐个标的检测 ``|单日收益| > 22%`` 的断点（22% 高于 A 股/ETF 单日涨跌幅上限 20%）；
2. **复权后复查**：若复权后仍有 ``|单日收益| > 22%`` ⇒ 说明该标的可能**没有涨跌幅限制**
   （跨境 ETF / 退市整理股）或阈值不当 ⇒ **门禁失败，人工复核**，绝不静默通过；
3. 打印每一个事件的「前收 → 收 / 因子」，供人工确认是份额折算而不是真实崩溃。

退出码：
    0 = 所有标的复权后都不再有超限断崖
    1 = 有标的复权后仍超限（或必需标的无缓存）

用法:
    .venv/bin/python scripts/audit_corporate_actions.py            # 离线（只读缓存）
    .venv/bin/python scripts/audit_corporate_actions.py --online   # 联网刷新后审计
    .venv/bin/python scripts/audit_corporate_actions.py --all-cached  # 连带审计磁盘上所有 equity 缓存
    .venv/bin/python scripts/audit_corporate_actions.py --json
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.marketdata_tx import (  # noqa: E402
    CORPORATE_ACTION_THRESHOLD,
    LIMIT_BUFFER,
    MIN_BARS_FOR_LISTING_SKIP,
    NEW_LISTING_BARS,
    cache_path,
    fetch_equity_history,
    load_cache,
)


def _load_registry() -> tuple:
    """从 verify_marketdata.py 复用标的清单（避免两处登记表漂移）。"""
    spec = importlib.util.spec_from_file_location(
        "_verify_marketdata_registry", ROOT / "scripts" / "verify_marketdata.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return tuple(mod.ETFS), tuple(mod.CONSTITUENTS)


def _cached_equity_codes() -> list:
    """磁盘上所有 ``equity_*_tx_history.csv`` 缓存对应的代码。"""
    pat = str(ROOT / "data" / "cache" / "equity_*_tx_history.csv")
    out = []
    for p in sorted(glob.glob(pat)):
        stem = Path(p).stem                     # equity_sh512480_tx_history
        out.append(stem.split("_", 1)[1].rsplit("_tx_history", 1)[0])
    return out


def _max_abs_ret(closes: pd.Series) -> float:
    s = pd.to_numeric(closes, errors="coerce").astype(float)
    r = s.pct_change().dropna()
    return 0.0 if r.empty else float(r.abs().max())


def _max_abs_ret_since(df: pd.DataFrame, since: str) -> float:
    d = df[["date", "close"]].copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    d = d[d["date"] >= pd.Timestamp(since)]
    return _max_abs_ret(d["close"])


# A 股涨跌幅限制自 1996-12-16 起实施；此前的价格无限制，超限变动是**真实行情**。
PRICE_LIMIT_EPOCH = "1997-01-01"


def _legal_limit(code: str) -> float:
    """该代码的**法定单日涨跌幅上限**（用于判断"残余超限变动"是否可疑）。

    ⚠️ 这是**报告用**的启发式，不是权威口径：
    ETF 的涨跌幅上限取决于其跟踪指数（双创 20%、主板 10%），仅凭代码无法判定，
    故对 ETF 一律取 20%（最宽口径），宁可漏报也不误报。
    """
    num = code[2:]
    if code.startswith("sh"):
        if num.startswith("688"):
            return 0.20          # 科创板
        if num.startswith("5"):
            return 0.20          # 沪市基金（含 588 科创 ETF）
        return 0.10
    if num.startswith("30"):
        return 0.20              # 创业板
    if num.startswith(("15", "16", "18")):
        return 0.20              # 深市基金（含 159 双创 ETF）
    if num.startswith(("4", "8")):
        return 0.30              # 北交所
    return 0.10


def audit_code(code: str, name: str, online: bool) -> dict:
    rec = {"code": code, "name": name, "bars": 0, "first": None, "last": None,
           "raw_max_ret": None, "adj_max_ret": None, "resid_max_ret": None,
           "legal_limit": _legal_limit(code), "events": [], "verdict": "", "ok": False}
    df = fetch_equity_history(code, refresh=online, prefer_cache=not online)
    if df is None or df.empty:
        rec["verdict"] = "❌ 无数据（用 --online 填充缓存）"
        return rec

    events = df.attrs.get("corporate_actions")
    events = pd.DataFrame() if events is None else events
    # 原始（未复权）序列直接读缓存 —— 缓存里存的**始终是原始价**
    cached = load_cache(cache_path("equity", code))
    raw_max = _max_abs_ret((df if cached is None else cached)["close"])
    # 残余检验只看 1997 以后（此前无涨跌幅限制，超限变动是真实行情）
    resid = _max_abs_ret_since(df, PRICE_LIMIT_EPOCH)
    rec.update(
        bars=len(df),
        first=df["date"].iloc[0].date().isoformat(),
        last=df["date"].iloc[-1].date().isoformat(),
        raw_max_ret=round(raw_max, 6),
        adj_max_ret=round(_max_abs_ret(df["close"]), 6),
        resid_max_ret=round(resid, 6),
        events=[{"date": pd.Timestamp(r["date"]).date().isoformat(),
                 "prev_close": round(float(r["prev_close"]), 4),
                 "close": round(float(r["close"]), 4),
                 "ret_pct": round(100 * float(r["ret"]), 2),
                 "factor": round(float(r["factor"]), 4)}
                for _, r in events.iterrows()],
    )

    tag = f"修复 {len(rec['events'])} 处断崖" if rec["events"] else "无断崖"
    # 0.5pp 容差：双创标的的 ±20% 是合法变动，浮点误差不该被报成可疑
    if resid > CORPORATE_ACTION_THRESHOLD:
        rec["verdict"] = (f"❌ 复权后仍有 {resid*100:.2f}% 单日变动（>法定上限 "
                          f"{rec['legal_limit']*100:.0f}%）⇒ 检测失效，人工复核")
    elif resid > rec["legal_limit"] + 0.005:
        rec["verdict"] = (f"⚠️ {tag}；但 1997 年后仍有 {resid*100:.2f}% 单日变动 > 法定上限 "
                          f"{rec['legal_limit']*100:.0f}% ⇒ 疑似未识别的企业行为（详见已知局限）")
        rec["ok"] = True
    elif rec["events"]:
        rec["verdict"] = f"🔧 {tag}（复权后 1997 年起最大 {resid*100:.2f}%）"
        rec["ok"] = True
    else:
        rec["verdict"] = f"✅ {tag}（最大 {resid*100:.2f}%）"
        rec["ok"] = True
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description="企业行为（份额折算/拆分）审计门禁")
    ap.add_argument("--online", action="store_true", help="联网刷新后再审计")
    ap.add_argument("--all-cached", action="store_true",
                    help="连带审计磁盘上所有 equity 缓存（含临时取过的标的）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    etfs, stocks = _load_registry()
    registry = [(c, n) for c, n in etfs] + [(c, n) for c, n in stocks]
    if args.all_cached:
        known = {c for c, _ in registry}
        registry += [(c, "(磁盘缓存)") for c in _cached_equity_codes() if c not in known]

    rows = [audit_code(c, n, online=args.online) for c, n in registry]

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print("=" * 96)
        print(f"企业行为审计门禁 —— 模式: {'联网' if args.online else '离线(只读缓存)'}   "
              f"阈值: **按代码推定的涨跌幅上限 + {LIMIT_BUFFER*100:.0f}pp**"
              f"（主板 11% / 双创与 ETF 21% / 北交所 31%；前 {NEW_LISTING_BARS} 根豁免）"
              f"   标的数: {len(rows)}")
        print("=" * 96)
        print(f"  {'代码':<10} {'名称':<18} {'根数':>6} {'原始最大':>9} {'复权后':>8} "
              f"{'97后最大':>9} {'法定':>6}  状态")
        print("  " + "-" * 110)
        for r in rows:
            raw = "-" if r["raw_max_ret"] is None else f"{r['raw_max_ret']*100:.2f}%"
            adj = "-" if r["adj_max_ret"] is None else f"{r['adj_max_ret']*100:.2f}%"
            res = "-" if r["resid_max_ret"] is None else f"{r['resid_max_ret']*100:.2f}%"
            print(f"  {r['code']:<10} {r['name'][:16]:<18} {r['bars']:>6} {raw:>9} {adj:>8} "
                  f"{res:>9} {r['legal_limit']*100:>5.0f}%  {r['verdict']}")

        events = [(r["code"], e) for r in rows for e in r["events"]]
        if events:
            print(f"\n【检测到的断崖事件 —— 请人工确认均为份额折算/拆分】（{len(events)} 处）")
            print(f"  {'代码':<10} {'日期':<12} {'前收':>9} {'收盘':>9} {'单日':>9} {'因子':>7}")
            print("  " + "-" * 66)
            for code, e in events:
                print(f"  {code:<10} {e['date']:<12} {e['prev_close']:>9.4f} {e['close']:>9.4f} "
                      f"{e['ret_pct']:>8.2f}% {e['factor']:>7.4f}")

        print("\n【结论】")
        bad = [r for r in rows if not r["ok"]]
        susp = [r for r in rows if r["ok"] and r["resid_max_ret"] is not None
                and r["resid_max_ret"] > r["legal_limit"] + 0.005]
        fixed = [r for r in rows if r["events"] and r["ok"]]
        print(f"  ✅ 无断崖: {sum(1 for r in rows if not r['events'] and r['ok'])}   "
              f"🔧 已修复: {len(fixed)}   ⚠️ 残余可疑: {len(susp)}   ❌ 失败: {len(bad)}")
        print("\n  ⚠️ 已知局限（**不掩盖**）：")
        print(f"     · 阈值现在是**按代码推定**的法定涨跌幅上限 + {LIMIT_BUFFER*100:.0f}pp"
              f"（主板 11% / 双创与 ETF 21% / 北交所 31%），")
        print("       已能抓到过去漏掉的 10 转 3 型除权（实例：sz002371 在 2011-07-01 的 −21.95%）。")
        print("     · **ETF 一律按 20%（最宽口径）**：ETF 的上限取决于跟踪指数，仅凭代码判不了，")
        print("       故取最宽 ⇒ 主板 ETF 在 10%~21% 区间的折算/分红会被**漏报**（安全方向）。")
        print("       实测 11 只 ETF 的份额折算都在 45% 以上，未受影响。")
        print("     · **ST 股（±5%）按 10% 处理** ⇒ 只会漏报 5%~11% 的送转，**不会误报**（安全方向）。")
        print(f"     · **前 {NEW_LISTING_BARS} 根豁免**：新股上市初期无涨跌幅限制；"
              f"只在序列 ≥{MIN_BARS_FOR_LISTING_SKIP} 根时生效。")
        print("     · 1997 年前无涨跌幅限制，超限变动可能是真实行情，故残余检验从 1997 起算。")
        print("     · **北交所（920xxx / 4xxxxx / 8xxxxx）腾讯源取不到**，已在 normalize_tx_code")
        print("       显式拒绝（绝不静默猜成 sh/sz 取错标的）。")

    bad = [r for r in rows if not r["ok"]]
    if bad:
        print(f"\n❌ 审计未通过：{len(bad)} 个标的 → {', '.join(r['code'] for r in bad)}")
        return 1
    print("\n✅ 审计通过：所有标的复权后均无超限断崖")
    return 0


if __name__ == "__main__":
    sys.exit(main())
