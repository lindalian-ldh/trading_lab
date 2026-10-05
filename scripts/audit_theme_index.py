#!/usr/bin/env python
"""主题指数可用性审计（P0.4b）—— 回答「每个主题到底用哪条价格序列？」

## 判据（来自方案文档第五章 / P0.4 验收）

1. **历史 ≥ 10 年日线** —— 统计功效要求：检测 +2pp 需 149 轮（`scripts/进化路径.md`）。
2. **与对应 ETF 的日收益相关性 ≥ 0.8** —— 否则"验证用的价格序列"与"执行用的 ETF"
   不是同一个东西，结论不能迁移（kill criterion 5：降级为"只显示、不验证"）。

## ⚠️ 本审计的前提：**必须先修企业行为断崖**

腾讯 ETF 日线未复权，份额折算是断崖（实测 512480 在 2021-03-29 单日 −48.90%）。
**不修就会把达标判成不达标**：512480 vs 国证半导体芯片 原始 0.683 → 复权后 0.831。
本脚本用 `fetch_equity_history`（默认 `adjust=True`）取数，因此已规避；
请配合 `scripts/audit_corporate_actions.py` 一起看。

## 输出

逐主题：指数代码 / 指数根数与年数 / ETF / 重叠根数 / 相关性（全重叠 + 近 500 日）/ 判定。

- `✅ 可用`      —— 历史 ≥10y 且与 ETF 相关性 ≥0.8
- `❌ 需合成`    —— 有 ETF 但相关性 <0.8（现成指数不能代表该主题）
- `⚠️ 无 ETF`    —— 没有执行载体，相关性无法校验 ⇒ 按 kill criterion 5 只显示不验证
- `⚠️ 历史不足`  —— 指数年数 <10（用户 2026-10-04 决定：科创系接受 6.7y 窗口，须记入文档）

用法:
    .venv/bin/python scripts/audit_theme_index.py              # 离线（只读缓存）
    .venv/bin/python scripts/audit_theme_index.py --online      # 联网刷新 ETF 缓存
    .venv/bin/python scripts/audit_theme_index.py --gate        # 有 ❌/⚠️ 时退出码 1
    .venv/bin/python scripts/audit_theme_index.py --json
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
from core.theme_universe import THEMES, WATCH_ONLY, all_observed  # noqa: E402

MIN_YEARS = 10.0
MIN_CORR = 0.8
MIN_OVERLAP_WARN = 500      # 重叠不足 500 个交易日 ⇒ 相关性脆弱（约 2 年）
TAIL_WINDOW = 500          # 近期窗口（约 2 年）—— 全区间相关性会掩盖近期脱钩


def _returns(df: pd.DataFrame) -> pd.Series:
    """日收益序列（date 为索引，去重升序）。"""
    if df is None or df.empty:
        return pd.Series(dtype="float64")
    d = df[["date", "close"]].copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    d = d.dropna().drop_duplicates(subset=["date"], keep="last").sort_values("date")
    s = d.set_index("date")["close"].astype(float)
    return s.pct_change().dropna()


def _corr(a: pd.Series, b: pd.Series, tail: int | None = None) -> tuple:
    if a.empty or b.empty:
        return None, 0
    j = pd.concat([a.rename("i"), b.rename("e")], axis=1, sort=True).dropna()
    if tail:
        j = j.tail(tail)
    if len(j) < 30:
        return None, len(j)
    return round(float(j["i"].corr(j["e"])), 4), len(j)


def audit_theme(theme: dict, online: bool) -> dict:
    name = theme["theme"]
    idx_code = theme.get("index")
    rec = {"theme": name, "index": idx_code, "index_bars": 0, "index_years": None,
           "index_start": None, "anchor": theme.get("anchor"), "etfs": [],
           "verdict": "", "level": ""}

    di = fetch_index_history(idx_code, refresh=online, prefer_cache=not online) \
        if idx_code else None
    if di is None or di.empty:
        rec.update(verdict="❌ 指数无数据", level="fail")
        return rec
    years = (di["date"].iloc[-1] - di["date"].iloc[0]).days / 365.25
    ri = _returns(di)
    rec.update(index_bars=len(di), index_years=round(years, 1),
               index_start=di["date"].iloc[0].date().isoformat())

    for etf, label in theme.get("etfs", ()):
        de = fetch_equity_history(etf, refresh=online, prefer_cache=not online)
        re_ = _returns(de)
        c_all, n_all = _corr(ri, re_)
        c_tail, n_tail = _corr(ri, re_, tail=TAIL_WINDOW)
        rec["etfs"].append({
            "etf": etf, "label": label,
            "bars": 0 if de is None else len(de),
            "first": None if de is None or de.empty else de["date"].iloc[0].date().isoformat(),
            "overlap": n_all, "corr": c_all,
            "overlap_tail": n_tail, "corr_tail": c_tail,
            "events": 0 if de is None or "corporate_actions" not in de.attrs
                      else len(de.attrs["corporate_actions"]),
        })

    corrs = [e["corr"] for e in rec["etfs"] if e["corr"] is not None]
    reasons = []
    if not rec["etfs"]:
        rec["level"], reasons = "warn", ["无 ETF 执行载体（按 kill criterion 5：只显示、不验证）"]
    elif not corrs:
        rec["level"], reasons = "warn", ["无可比重叠区间"]
    elif max(corrs) < MIN_CORR:
        rec["level"] = "fail"
        reasons.append(f"与 ETF 相关性最高仅 {max(corrs):.3f} < {MIN_CORR} ⇒ 现成指数不能代表该主题，需合成")
    else:
        rec["level"] = "ok"
        reasons.append(f"相关性 {max(corrs):.3f} ≥ {MIN_CORR}")
        short_ov = [e for e in rec["etfs"]
                    if e["corr"] is not None and e["overlap"] < MIN_OVERLAP_WARN]
        if short_ov and len(short_ov) == len([e for e in rec["etfs"] if e["corr"] is not None]):
            reasons.append(f"⚠️ 但全部重叠 <{MIN_OVERLAP_WARN} 个交易日"
                           f"（最短 {min(e['overlap'] for e in short_ov)}）⇒ 相关性脆弱，"
                           f"不可当稳定结论")
    if years < MIN_YEARS:
        tag = f"指数历史 {years:.1f}y < {MIN_YEARS:.0f}y"
        if rec["level"] == "ok":
            rec["level"] = "warn"
        reasons.append(tag + "（2026-10-04 决定：接受该窗口并记入文档，"
                             "但**不作为验证依据**）")
    rec["verdict"] = "；".join(reasons)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description="主题指数可用性审计（P0.4b）")
    ap.add_argument("--online", action="store_true", help="联网刷新缓存")
    ap.add_argument("--gate", action="store_true", help="有 ❌/⚠️ 时退出码 1")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--themes-only", action="store_true",
                    help="只看 THEMES（10 个含题材的主题），不含 WATCH_ONLY 观察项")
    args = ap.parse_args()

    rows = [audit_theme(t, online=args.online) for t in (THEMES if args.themes_only else all_observed())]

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print("=" * 118)
        print(f"主题指数可用性审计（P0.4b）—— 判据: 指数历史 ≥{MIN_YEARS:.0f}y 且 与 ETF 日收益相关性 ≥{MIN_CORR}")
        print("=" * 118)
        print(f"  {'主题':<14} {'指数':<9} {'根数':>6} {'年数':>5}  "
              f"{'ETF':<9} {'重叠':>6} {'corr':>7} {'近500':>7} {'c500':>7}  判定")
        print("  " + "-" * 112)
        for r in rows:
            if not r["etfs"]:
                print(f"  {r['theme']:<14} {str(r['index']):<9} {r['index_bars']:>6} "
                      f"{(r['index_years'] or 0):>5.1f}  {'(无执行 ETF)':<9} "
                      f"{'-':>6} {'-':>7} {'-':>7} {'-':>7}  "
                      f"{'❌' if r['level']=='fail' else '⚠️'} {r['verdict']}")
                continue
            for i, e in enumerate(r["etfs"]):
                head = (f"  {r['theme']:<14} {str(r['index']):<9} {r['index_bars']:>6} "
                        f"{(r['index_years'] or 0):>5.1f}  ") if i == 0 else "  " + " " * 44
                mark = {"ok": "✅", "fail": "❌", "warn": "⚠️"}[r["level"]] if i == 0 else " "
                verdict = r["verdict"] if i == 0 else ""
                print(f"{head}{e['etf']:<9} {e['overlap']:>6} "
                      f"{('%.3f' % e['corr']) if e['corr'] is not None else '-':>7} "
                      f"{e['overlap_tail']:>7} "
                      f"{('%.3f' % e['corr_tail']) if e['corr_tail'] is not None else '-':>7}  "
                      f"{mark} {verdict}")

        ok = [r for r in rows if r["level"] == "ok"]
        warn = [r for r in rows if r["level"] == "warn"]
        fail = [r for r in rows if r["level"] == "fail"]
        print("\n【结论】")
        print(f"  ✅ 可用（现成指数达标）: {len(ok)} 主题 → {', '.join(r['theme'] for r in ok)}")
        print(f"  ❌ 需合成指数:          {len(fail)} 主题 → {', '.join(r['theme'] for r in fail)}")
        print(f"  ⚠️ 降级/受限:           {len(warn)} 主题 → {', '.join(r['theme'] for r in warn)}")
        print("\n  ℹ️ 说明：")
        print("     · 相关性已用**前复权**序列计算（见 core/marketdata_tx.detect_corporate_actions）；")
        print("       若用未复权价，512480 vs sz399363 会被低估为 0.683（真值 0.831）。")
        print("     · 标 '❌ 需合成' 的主题，其现成指数**不得**用于 Phase 2 的收益验证")

    if args.gate:
        bad = [r for r in rows if r["level"] in ("fail", "warn")]
        if bad:
            print(f"\n❌ 门禁未通过：{len(bad)} 个主题未达标 → "
                  f"{', '.join(r['theme'] for r in bad)}")
            return 1
        print("\n✅ 门禁通过：所有主题的现成指数均达标")
    return 0


if __name__ == "__main__":
    sys.exit(main())
