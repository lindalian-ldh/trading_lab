#!/usr/bin/env python3
"""信号台账：把系统对每个候选的判定**全量**落盘，供日后客观分析。

为什么需要（而不是靠记性）：
    · "感觉胜率还可以" 在统计上不可判定 —— 单笔 10 日收益 SD ≈ 8.7pp，
      要检测 +1% 的优势需要约 597 笔，+2% 需要约 149 笔。
    · 回忆只会留下赢的那些（分子），没有分母就算不出胜率。
    · **"被系统挑出来但没买"的票是唯一的对照组** —— 它能把你和系统分开：
        买了的 > 没买的  →  是你的判断在创造价值
        两者无差别        →  你的判断没加分
    · 维度A 的失败原因（a_msg）必须逐条记下，否则无法回答
      "把偏离阈值从 0.5% 放宽到 1.2%，会多出哪些票、它们表现如何"。

用法：
    # 记录你今天的自选/候选池（代码逗号分隔）
    python scripts/log_signal_ledger.py --symbols 600519,000001,601899

    # 从文件读（每行一个代码，# 注释）
    python scripts/log_signal_ledger.py --file watchlist.txt

    # 补记历史某天（会按该日截断数据，不引入未来函数）
    python scripts/log_signal_ledger.py --symbols 600519 --date 2026-09-20

    # 带市场状态（需要联网取指数，较慢）
    python scripts/log_signal_ledger.py --symbols 600519 --with-regime

产出：data/observations/signal_ledger.csv（幂等：同 (date,symbol) 不重复记）
     其中 bought / my_note 两列留空，由你手工填 —— 这两列是整个观察的核心。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/calc_indicators"))

LEDGER = ROOT / "data/observations/signal_ledger.csv"

COLUMNS = [
    "date", "symbol", "name", "close", "amt20_yi", "atr_pct",
    "a_pass", "a_msg", "b_pass", "b_signals",
    "c_pass", "c_ratio", "c_stop", "c_take", "c_stop_dist_pct", "c_stop_mode", "c_profit_mode",
    "gate_passed", "gate_vetoes", "dim_passed", "regime",
    "dev_ma5_pct", "dev_ma10_pct", "dev_ma20_pct",
    "vol_ratio", "ret5_pre_pct", "ret20_pre_pct",
    # —— 双因子打分原型（与 A/B/C 并行记录，用于日后对比谁更准）——
    "score_double", "score_rank", "rank_atr", "rank_dev", "score_n_valid",
    "s_hard_veto", "score_version",
    "bought", "my_note", "logged_at",
]


def resolve_symbols(args) -> list[str]:
    syms: list[str] = []
    if args.symbols:
        syms += [s.strip() for s in args.symbols.split(",") if s.strip()]
    if args.file:
        for line in Path(args.file).read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if line:
                syms += [s.strip() for s in line.replace(",", " ").split() if s.strip()]
    seen, out = set(), []
    for s in syms:
        s = s.zfill(6)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="", help="逗号分隔的代码")
    ap.add_argument("--file", default="", help="代码清单文件（每行一个，# 注释）")
    ap.add_argument("--date", default="", help="判定日 YYYY-MM-DD（默认今天；补记历史时用）")
    ap.add_argument("-p", "--profile", action="append", default=None,
                    help="配置档名（可重复传入以叠加，与 main.py 一致）")
    ap.add_argument("--with-regime", action="store_true", help="同时记录市场状态（需联网）")
    ap.add_argument("--index", default="", help="基准指数（默认用配置里的 sh000001）")
    ap.add_argument("--out", default=str(LEDGER))
    ap.add_argument("--force", action="store_true", help="已存在也覆盖")
    args = ap.parse_args()

    syms = resolve_symbols(args)
    if not syms:
        print("❌ 未提供标的：用 --symbols 或 --file")
        return 1

    import config as C
    import main as M
    import score_double as S

    # 用官方解析器：CLI 传的是别名（ultra_short），不是类名（UltraShortConfig）。
    # 之前写成 getattr(C, args.profile)() —— 传 -p ultra_short 会直接 AttributeError。
    cfg, _srcs = C.merge_profiles(list(args.profile or ["default"]))
    cfg.PULLBACK_DEBUG = False
    cfg.MOMENTUM_DEBUG = False
    cfg.MARKET_DEBUG = False
    if args.index:
        cfg.BENCHMARK_INDEX = args.index

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    old = pd.read_csv(out, dtype={"symbol": str}) if out.exists() else pd.DataFrame(columns=COLUMNS)
    if not old.empty:
        old["symbol"] = old["symbol"].astype(str).str.zfill(6)
    done = set()
    if not old.empty and not args.force:
        done = set(zip(old["date"].astype(str), old["symbol"]))

    regime = {}
    regime_ok = True
    if args.with_regime:
        try:
            import regime_detector as RD
            RD._REGIME_CACHE.clear()
            r = RD.detect_regime(cfg)
            if r.get("available"):
                regime = {"regime": r.get("regime", "UNKNOWN")}
            else:
                regime_ok = False
                print()
                RD.print_regime_result(r)          # 复用同一套醒目警示
        except Exception as e:
            regime_ok = False
            print(f"\n⚠️  市场状态取数异常：{type(e).__name__}: {e}")
            print("    本次记录的 regime 列将为空 —— 这些行不能当作有效的择时观测。")

    rows, skipped, failed = [], 0, 0
    for i, s in enumerate(syms, 1):
        try:
            df = M.fetch_data(s, cfg)
        except SystemExit:
            print(f"  {i:>3d}/{len(syms)} {s}: ✖ 取数失败，跳过")
            failed += 1
            continue
        if df is None or len(df) < 70:
            print(f"  {i:>3d}/{len(syms)} {s}: ✖ 数据不足（{0 if df is None else len(df)} 根）")
            failed += 1
            continue
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        if args.date:
            df = df[df["date"] <= pd.Timestamp(args.date)]
            if len(df) < 70:
                print(f"  {i:>3d}/{len(syms)} {s}: ✖ 该日之前数据不足")
                failed += 1
                continue
        d = df["date"].iloc[-1].strftime("%Y-%m-%d")
        if (d, s) in done:
            skipped += 1
            continue

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                a_pass, a_msg = M.check_dimension_a(df, cfg)
                b_pass, b_signals = M.check_dimension_b(df, cfg)
                c_pass, c_info = M.check_dimension_c(df, cfg)
                gate = M.check_entry_gate(regime or {}, a_pass, a_msg, b_signals,
                                          c_pass, c_info, df, cfg)
            except Exception as e:
                print(f"  {i:>3d}/{len(syms)} {s}: ✖ 判定异常 {type(e).__name__}: {str(e)[:50]}")
                failed += 1
                continue

        close = float(df["close"].iloc[-1])
        amt = (df["close"] * df["volume"]).tail(20).mean()
        atr = M.calc_atr(df, cfg.ATR_PERIOD)
        atr_pct = float(atr.iloc[-1] / close * 100) if len(atr) and close else float("nan")
        devs = {}
        for p in (5, 10, 20):
            ma = M.calc_ma(df["close"], p).iloc[-1]
            devs[p] = float((close - ma) / ma * 100) if ma else float("nan")
        vma = M.calc_ma(df["volume"], cfg.VOLUME_MA_PERIOD).iloc[-1]
        vol_ratio = float(df["volume"].iloc[-1] / vma) if vma else float("nan")
        r5 = float((close / df["close"].iloc[-6] - 1) * 100) if len(df) > 6 else float("nan")
        r20 = float((close / df["close"].iloc[-21] - 1) * 100) if len(df) > 21 else float("nan")
        stop = float(c_info.get("stop", float("nan")))
        stop_dist = (close - stop) / close * 100 if close else float("nan")

        rows.append({
            "date": d, "symbol": s, "name": "", "close": round(close, 4),
            "amt20_yi": round(float(amt) / 1e8, 4), "atr_pct": round(atr_pct, 3),
            "a_pass": bool(a_pass), "a_msg": str(a_msg)[:200],
            "b_pass": bool(b_pass), "b_signals": ";".join(map(str, b_signals))[:200],
            "c_pass": bool(c_pass), "c_ratio": round(float(c_info.get("ratio", float("nan"))), 3),
            "c_stop": round(stop, 4), "c_take": round(float(c_info.get("take", float("nan"))), 4),
            "c_stop_dist_pct": round(stop_dist, 3),
            "c_stop_mode": c_info.get("stop_mode", ""), "c_profit_mode": c_info.get("profit_mode", ""),
            "gate_passed": bool(gate.get("passed")),
            "gate_vetoes": ",".join(v["rule"] for v in gate.get("vetoes", [])),
            "dim_passed": int(bool(a_pass)) + int(bool(b_pass)) + int(bool(c_pass)),
            "regime": (regime or {}).get("regime", ""),
            "dev_ma5_pct": round(devs[5], 3), "dev_ma10_pct": round(devs[10], 3),
            "dev_ma20_pct": round(devs[20], 3),
            "vol_ratio": round(vol_ratio, 3),
            "ret5_pre_pct": round(r5, 3), "ret20_pre_pct": round(r20, 3),
            "score_double": np.nan, "score_rank": np.nan, "rank_atr": np.nan,
            "rank_dev": np.nan, "score_n_valid": 0,
            "s_hard_veto": ";".join(S.hard_vetoes(
                {"close": close, "amt20_yi": float(amt) / 1e8}, cfg,
                (close - stop) / close * 100 if close and np.isfinite(stop) else None,
                symbol=s)),
            "score_version": S.SCORE_VERSION,
            "bought": "", "my_note": "",
            "logged_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        mark = "★三维度全通过" if (a_pass and b_pass and c_pass and gate.get("passed")) else ""
        print(f"  {i:>3d}/{len(syms)} {s} {d} A={'✅' if a_pass else '❌'} "
              f"B={'✅' if b_pass else '❌'} C={'✅' if c_pass else '❌'} {mark}")

    new = pd.DataFrame(rows, columns=COLUMNS)
    if new.empty:
        merged = old
    else:
        merged = pd.concat([old, new], ignore_index=True) if not old.empty else new
        # —— 两遍法：排名必须在「同一天的全部候选」内部算 ——
        # 合并同日已有行 → 去重（新行优先）→ 逐日横截面排名 → 写回。
        # 这样当天分两次记录（先记 20 只、再补 10 只）时，全天的排名会被重算。
        merged["_new"] = 0
        merged.loc[merged.index[-len(new):], "_new"] = 1
        merged = merged.sort_values("_new").drop_duplicates(
            subset=["date", "symbol"], keep="last").drop(columns="_new")
        # 只把评分列取回来 —— 不要就地 rename，否则 dev_ma20_pct 会被永久改名
        # ETF 排除出排名（判读卡在个股上测得，样本内 0 只 ETF；混算百分位是类别错误）
        from etf import is_etf as _is_etf
        _ex = merged["symbol"].astype(str).str.zfill(6).map(_is_etf)
        _tmp = S.rank_and_score(merged.rename(columns={"dev_ma20_pct": "dev_ma20"}),
                                date_col="date", atr_col="atr_pct", dev_col="dev_ma20",
                                exclude=_ex)
        for c in ("rank_atr", "rank_dev", "score_double"):
            merged[c] = pd.to_numeric(_tmp[c], errors="coerce").to_numpy()
        # 同日按分数降序给名次（分数为 NaN 的不参与）
        merged["score_rank"] = np.nan
        for _, idx in merged.groupby("date").groups.items():
            grp = merged.loc[idx]                       # 始终是 DataFrame（单行也安全）
            sc = pd.to_numeric(grp["score_double"], errors="coerce")
            merged.loc[idx, "score_rank"] = sc.rank(ascending=False, method="min").to_numpy()
        merged = merged.sort_values(["date", "score_rank"], na_position="last").reset_index(drop=True)
        # 列顺序对齐 COLUMNS（保留 bought_src 等自动填充列）
        head = [c for c in COLUMNS if c in merged.columns]
        rest = [c for c in merged.columns if c not in head]
        merged = merged[head + rest]
    merged.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n台账: {out}")
    print(f"  新增 {len(new)} 行   跳过已存在 {skipped} 行   失败 {failed} 只   台账总计 {len(merged)} 行")
    if len(new):
        print(f"  今日 3 维全通过: {int(((new.a_pass)&(new.b_pass)&(new.c_pass)).sum())} 只")
        print(f"  其中门控也放行: {int(((new.a_pass)&(new.b_pass)&(new.c_pass)&(new.gate_passed)).sum())} 只")
    if not merged.empty and "score_double" in merged.columns:
        rankable = int(pd.to_numeric(merged["score_double"], errors="coerce").notna().sum())
        _n_etf = int(merged["symbol"].astype(str).str.zfill(6).map(_is_etf).sum()) \
            if "symbol" in merged.columns else 0
        if _n_etf:
            print(f"  ETF {_n_etf} 行已排除出双因子排名（判读卡不适用于 ETF）")
        print(f"  双因子打分（{S.SCORE_VERSION}，样本内原型）：可排名 {rankable} 行"
              + ("  ⚠️ 同日不足 3 只无法构成横截面" if rankable == 0 else ""))
    if args.with_regime and not regime_ok:
        print()
        print("  ⛔ 注意：本次 regime 列全部为空（市场状态取数失败）。")
        print("     这些行**不能当作有效的择时观测** —— 分析时请按日期剔除，")
        print("     或在市场状态恢复后对同一日期重跑本命令补齐。")
    print("\n⚠️  别忘了填 bought 列（1=买了 / 0=没买）—— 它是唯一的对照组：")
    print(f"   {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
