#!/usr/bin/env python
"""主题指数候选勘探 —— 回答「这个主题能不能扩进来 / 该用哪条指数」。

## 用途

[`core/theme_universe.py`](../core/theme_universe.py) 的 `THEMES` 是**人工维护**的表；
往里加主题（或给现有主题换指数）之前，必须先用本脚本回答两个问题：

1. **腾讯源取不取得到？有多少年历史？**（P0.4 判据一：**≥10 年**）
2. **它跟对应的 ETF 像不像？**（P0.4 判据二：日收益相关性 **≥0.8**）

## ⚠️ 两条必须记住的纪律

- **本脚本天然带选择偏差**：一次扫 N 个指数 × M 只 ETF = N×M 次相关性，然后挑最大的。
  ⇒ 挑出来的结果**必须**当"候选"看，采用时要在方案文档记一条 **P0.5 修订记录**，
  并把用它的重跑当作**样本内探索性**（OOS 段仍然保留、不许顺手打开）。
- **相关性 ≥0.8 ≠ 语义匹配**：宽泛的"电子/信息"指数可能与半导体 ETF 日收益同步到 0.9，
  但它**不代表**"半导体设备"这个主题。语义正确性仍要靠合成指数（P0.4c）解决。

## 2026-10-04 实测结论（68 个行业指数候选）

- 腾讯**可取 68/68**，其中 **65 个 ≥10 年** ⇒ **供给不是瓶颈**。
- 真正的瓶颈是 **≥0.8 相关性**：电网设备最好只有 **0.777**（`sz399233`）⇒ 扩不动。
- 半导体系可换指数（都不需要合成）：`sh000039` 上证电信 17.7y（0.841~0.937）、
  `sh000993` 全指信息 14.3y（0.830~0.899）、`sh000935` 中证信息 17.2y（0.826~0.894）、
  `sz399811` CSSW电子 11.0y（0.833~0.919）。
- ⚠️ `sh000685` 科创芯片相关性最高（0.869~0.963）**但只有 4.3 年** ⇒ **不能用**。

## 用法

    .venv/bin/python scripts/explore_theme_index_candidates.py                 # 内置候选清单
    .venv/bin/python scripts/explore_theme_index_candidates.py --codes sh000039,sz399811
    .venv/bin/python scripts/explore_theme_index_candidates.py --discover      # 从 Sina 列表自动发现
    .venv/bin/python scripts/explore_theme_index_candidates.py --min-years 8 --min-corr 0.75
    .venv/bin/python scripts/explore_theme_index_candidates.py --json
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

from core.marketdata_tx import fetch_equity_history, fetch_index_history  # noqa: E402

#: 内置候选：交易所发布的行业/主题指数（**已实测腾讯可取**）
DEFAULT_CANDIDATES = [
    # 半导体 / 电子 / 信息 / 通信
    "sz399363", "sz399811", "sz399281", "sz399389", "sh000685", "sh000813",
    "sh000032", "sh000038", "sh000039", "sh000040", "sh000935", "sh000993",
    # 行业分类（上证/全指/国证）
    "sh000033", "sh000034", "sh000035", "sh000036", "sh000037", "sh000041",
    "sh000928", "sh000929", "sh000930", "sh000931", "sh000932", "sh000933",
    "sh000934", "sh000936", "sh000937", "sh000986", "sh000987", "sh000988",
    "sh000989", "sh000990", "sh000991", "sh000992", "sh000994", "sh000995",
    "sz399231", "sz399232", "sz399233", "sz399234", "sz399235", "sz399236",
    "sz399237", "sz399238", "sz399239",
    # 窄主题
    "sh000819", "sh000827", "sh000998", "sh000823", "sz399394", "sz399395",
    "sz399417", "sz399441", "sz399618", "sz399674", "sz399808", "sz399959",
    "sz399967", "sz399975", "sz399986", "sz399989", "sz399995", "sz399997",
    "sz399998", "sz399976", "sz399368",
]

#: 默认用来做相关性对照的 ETF（from scripts/verify_marketdata.py）
DEFAULT_ETFS = ["sh562590", "sh561980", "sh512480", "sh512760", "sh516510",
                "sh561310", "sh561380", "sh588170", "sz159781", "sh517520",
                "sz159516"]


def _discover(extra_filter: str = "") -> list:
    """从 Sina 指数字典里发现候选（需联网一次）。"""
    import akshare as ak
    idx = ak.stock_zh_index_spot_sina()
    drop = ("上证50|沪深300|中证500|中证1000|深证成指|创业板指|科创50|中小|综指|国债|债|货币"
            "|基金|ETF|LOF|等权|红利|价值|成长|低波|质量|动量|基本面|央企|国企|民营|ESG"
            "|治理|一带一路|MSCI|富时|标普|恒生|港|海外|全球|美股|美元")
    keep = idx[~idx["名称"].astype(str).str.contains(drop, na=False)]
    keep = keep[keep["代码"].astype(str).str.startswith(("sh000", "sz399"))]
    if extra_filter:
        keep = keep[keep["名称"].astype(str).str.contains(extra_filter, na=False)]
    return sorted(keep["代码"].astype(str).tolist())


def _rets(df) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(dtype="float64")
    d = df[["date", "close"]].copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    d = d.dropna().drop_duplicates(subset=["date"], keep="last").sort_values("date")
    return d.set_index("date")["close"].astype(float).pct_change().dropna()


def _corr(a: pd.Series, b: pd.Series) -> tuple:
    if a.empty or b.empty:
        return None, 0
    j = pd.concat([a.rename("i"), b.rename("e")], axis=1, sort=True).dropna()
    if len(j) < 200:                 # 重叠太短的相关性没有意义
        return None, len(j)
    return round(float(j["i"].corr(j["e"])), 3), len(j)


def main() -> int:
    ap = argparse.ArgumentParser(description="主题指数候选勘探")
    ap.add_argument("--codes", help="逗号分隔的候选指数代码（默认用内置清单）")
    ap.add_argument("--discover", action="store_true", help="从 Sina 指数字典自动发现候选（联网）")
    ap.add_argument("--discover-filter", default="", help="配合 --discover 的名称过滤正则")
    ap.add_argument("--etfs", default=",".join(DEFAULT_ETFS), help="用于相关性的 ETF")
    ap.add_argument("--min-years", type=float, default=10.0, help="历史年限门槛")
    ap.add_argument("--min-corr", type=float, default=0.8, help="相关性门槛")
    ap.add_argument("--top", type=int, default=6, help="每只 ETF 只显示前 N 个候选")
    ap.add_argument("--online", action="store_true", help="强制联网刷新")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    elif args.discover:
        codes = _discover(args.discover_filter)
    else:
        codes = list(DEFAULT_CANDIDATES)

    # —— ① 可取性与历史深度 ——
    depth = []
    for c in codes:
        try:
            d = fetch_index_history(c, refresh=args.online,
                                    prefer_cache=not args.online, allow_stale=True)
        except Exception:
            d = None
        if d is None or d.empty:
            depth.append({"code": c, "bars": 0, "start": None, "years": None, "ok": False})
            continue
        yrs = (d["date"].iloc[-1] - d["date"].iloc[0]).days / 365.25
        depth.append({"code": c, "bars": int(len(d)),
                      "start": d["date"].iloc[0].date().isoformat(),
                      "years": round(yrs, 1), "ok": yrs >= args.min_years})
    dd = pd.DataFrame(depth)

    # —— ② 与 ETF 的相关性（前复权）——
    etfs = [c.strip() for c in args.etfs.split(",") if c.strip()]
    etf_rets = {c: _rets(fetch_equity_history(c, refresh=args.online,
                                              prefer_cache=not args.online)) for c in etfs}
    usable = [r["code"] for _, r in dd.iterrows() if r["ok"]]
    corr = {}
    idx_rets = {}
    for c in usable:
        idx_rets[c] = _rets(fetch_index_history(c, prefer_cache=not args.online,
                                                allow_stale=True))
    for etf, re_ in etf_rets.items():
        if re_.empty:
            continue
        col = {}
        for c in usable:
            v, n = _corr(idx_rets[c], re_)
            if v is not None:
                col[c] = {"corr": v, "overlap": n, "pass": v >= args.min_corr}
        corr[etf] = col

    if args.json:
        print(json.dumps({"depth": depth, "corr": corr,
                          "min_years": args.min_years, "min_corr": args.min_corr},
                         ensure_ascii=False, indent=2))
        return 0

    print("=" * 100)
    print(f"主题指数候选勘探 —— 候选 {len(codes)} 个   判据: 历史 ≥{args.min_years:g} 年 "
          f"且 与 ETF 相关性 ≥{args.min_corr:g}")
    print("=" * 100)

    ok_depth = dd[dd["ok"]]
    print(f"\n【① 历史深度】可取 {int((dd['bars'] > 0).sum())}/{len(dd)}，"
          f"其中 ≥{args.min_years:g} 年 {len(ok_depth)} 个")
    miss = dd[dd["bars"] == 0]
    if len(miss):
        print(f"  ❌ 取不到: {', '.join(miss['code'])}")
    short = dd[(dd["bars"] > 0) & (~dd["ok"])]
    if len(short):
        print("  ⚠️ 历史不足（相关性再高也不能用）:")
        for _, r in short.iterrows():
            print(f"     {r['code']}  {r['bars']:>5} 根  起 {r['start']}  {r['years']:>5.1f} 年")

    print(f"\n【② 与 ETF 的相关性（只看已过历史门槛的 {len(usable)} 个候选）】")
    for etf, col in corr.items():
        if not col:
            print(f"\n  {etf}: 无可比候选（重叠 <200 日）")
            continue
        s = pd.Series({c: v["corr"] for c, v in col.items()}).sort_values(ascending=False)
        hits = [c for c, v in col.items() if v["pass"]]
        print(f"\n  {etf}  （≥{args.min_corr:g} 的候选 {len(hits)} 个）")
        for c, v in s.head(args.top).items():
            mark = "✅" if col[c]["pass"] else "  "
            print(f"     {mark} {c}  corr={v:.3f}  重叠={col[c]['overlap']}")

    # —— ③ 达标汇总 ——
    print("\n【③ 达标汇总】（同时过『历史』与『相关性』两关）")
    all_pass = {}
    for etf, col in corr.items():
        for c, v in col.items():
            if v["pass"]:
                all_pass.setdefault(c, []).append(etf)
    if not all_pass:
        print("  ❌ 没有任何候选同时达标 —— 该主题只能合成指数，或降级为『只显示、不验证』")
    else:
        for c, es in sorted(all_pass.items(), key=lambda kv: -len(kv[1])):
            yrs = dd.loc[dd["code"] == c, "years"].iloc[0]
            print(f"  ✅ {c}  {yrs:>5.1f} 年  匹配 {len(es)}/{len(corr)} 只 ETF: {', '.join(es)}")
    print("\n  ⚠️ 选择偏差提醒：本表是对 N×M 次相关性取最大值的结果。"
          "采用前请记 P0.5 修订记录，并把重跑当作**样本内探索性**（OOS 不许打开）。")
    print("  ⚠️ 相关性 ≥0.8 **不等于**语义匹配：宽泛的行业指数可能不代表你的主题。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
