#!/usr/bin/env python
"""P0.4c 合成主题指数 CLI —— 建指数、验相关性、出成分股复核清单。

## 三种用法

```bash
# ① 出「成分股复核清单」：按与参照 ETF 的相关性排序（**只作诊断，不作筛选依据**）
.venv/bin/python scripts/build_synth_index.py --plate 801016 --name 稀土 \\
    --rank --ref-etf sz159715

# ② 用**人工复核过的**成分股清单合成指数并验证
.venv/bin/python scripts/build_synth_index.py --plate 801016 --name 稀土 \\
    --include-file data/synth/rare_earth_members.txt \\
    --validate sz159715,sh516780,sh516150 --save

# ③ 只要指数（不验证）
.venv/bin/python scripts/build_synth_index.py --plate 801016 --name 稀土 --save
```

## ⚠️ 两条纪律

1. **不许按"与 ETF 的相关性"筛成分股** —— 那会让验证变成循环论证。
   `--rank` 的输出是**诊断**（把明显不相关的噪声票暴露出来），
   成员资格必须基于**行业归属**（人工判断），并且清单要落盘、写进文档。
2. **相关性 < 0.8 ⇒ 该合成指数按 kill criterion 5 降级为"只显示、不验证"**，
   不许拿它去做 Phase 2 的收益结论。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import synth_index as SI  # noqa: E402
from core.lhb_store import fetch_plate_constituents  # noqa: E402
from core.marketdata_tx import (  # noqa: E402
    INDEX_CODES,
    fetch_equity_history,
    fetch_index_history,
)

CACHE_DIR = ROOT / "data" / "cache"
SYNTH_DIR = ROOT / "data" / "synth"


# ====================================================================
# 取数
# ====================================================================

def load_prices_tencent(codes, *, online: bool, throttle: float = 0.0) -> dict:
    """腾讯源前复权个股 **OHLC 帧**（已按代码阈值修复企业行为）。"""
    out, fail = {}, []
    for i, tx in enumerate(codes, 1):
        d = fetch_equity_history(tx, refresh=online, prefer_cache=not online)
        if d is None or d.empty:
            fail.append(tx)
            continue
        out[tx] = d[["date", "open", "high", "low", "close"]].copy()
        if throttle and i % 20 == 0:
            time.sleep(throttle)
    if fail:
        print(f"  ⚠️ {len(fail)} 只取数失败（已剔除）: {', '.join(fail[:10])}"
              f"{' …' if len(fail) > 10 else ''}")
    return out


def load_prices_baostock(codes) -> dict:
    """baostock 前复权个股价（更干净，但慢，且需要登录）。"""
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
    out, fail = {}, []
    try:
        for tx in codes:
            sym = f"{tx[:2]}.{tx[2:]}"
            rs = bs.query_history_k_data_plus(
                sym, "date,open,high,low,close",
                start_date="2005-01-01", end_date="2099-12-31",
                frequency="d", adjustflag="2")      # 2 = 前复权
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                fail.append(tx)
                continue
            d = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close"])
            d["date"] = pd.to_datetime(d["date"])
            for c in ("open", "high", "low", "close"):
                d[c] = pd.to_numeric(d[c], errors="coerce")
            d["open"] = d["close"]; d["high"] = d["close"]; d["low"] = d["close"]
            out[tx] = d[["date", "open", "high", "low", "close"]]
    finally:
        bs.logout()
    if fail:
        print(f"  ⚠️ baostock {len(fail)} 只取不到（已剔除）: {', '.join(fail[:10])}")
    return out


# ====================================================================
# 成分股清单
# ====================================================================

def read_include_file(path: Path) -> list:
    """读人工清单（每行一个 6 位代码，`#` 注释）。"""
    codes = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        s = line.split("#")[0].strip()
        if s:
            codes.append(s.zfill(6))
    return codes


def load_members(plate: str, include_file: Path | None, exclude: list) -> pd.DataFrame:
    df = fetch_plate_constituents(plate, force=False)
    df = df.drop_duplicates(subset=["stock_code"]).copy()
    df["stock_code"] = df["stock_code"].astype(str).str.zfill(6)
    if include_file is not None:
        keep = set(read_include_file(include_file))
        missing = keep - set(df["stock_code"])
        if missing:
            print(f"  ⚠️ 清单里有 {len(missing)} 个代码不在题材 {plate} 内: "
                  f"{', '.join(sorted(missing)[:8])}…（仍会尝试取数）")
            extra = pd.DataFrame({"stock_code": sorted(missing), "stock_name": "",
                                  "time_in": SI.SYNTH_START})
            df = pd.concat([df, extra], ignore_index=True)
        df = df[df["stock_code"].isin(keep)]
    if exclude:
        ex = {e.zfill(6) for e in exclude}
        df = df[~df["stock_code"].isin(ex)]
    return df.reset_index(drop=True)


# ====================================================================
# 主流程
# ====================================================================

def main() -> int:
    ap = argparse.ArgumentParser(description="合成主题指数（P0.4c）")
    ap.add_argument("--plate", required=True, help="zzshare 题材代码，如 801016 稀土永磁")
    ap.add_argument("--name", required=True, help="合成指数名（用于落盘命名）")
    ap.add_argument("--include-file", type=Path, help="人工复核过的成分股清单（每行一个代码）")
    ap.add_argument("--exclude", default="", help="剔除的代码，逗号分隔")
    ap.add_argument("--source", choices=["tencent", "baostock"], default="tencent")
    ap.add_argument("--start", default=SI.SYNTH_START, help=f"起算日（默认 {SI.SYNTH_START}）")
    ap.add_argument("--min-members", type=int, default=SI.MIN_MEMBERS)
    ap.add_argument("--ignore-time-in", action="store_true",
                    help="忽略 time_in，按**上市日起算**（= 假设这些公司一直是成分股）。"
                         "适用情形：`time_in` 被单一『批量分类日期』主导时"
                         "（如稀土永磁 13 只都是 2018-09-20），此时 time_in 不是真实成分变动")
    ap.add_argument("--validate", default="", help="用于验证的 ETF，逗号分隔")
    ap.add_argument("--rank", action="store_true", help="输出成分股复核清单（诊断用）")
    ap.add_argument("--ref-etf", default="", help="--rank 的参照 ETF")
    ap.add_argument("--online", action="store_true", help="强制联网刷新")
    ap.add_argument("--save", action="store_true", help=f"落盘到 {CACHE_DIR}/synth_*.csv")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    exclude = [c for c in args.exclude.split(",") if c.strip()]
    members = load_members(args.plate, args.include_file, exclude)
    print(f"题材 {args.plate} 成分股: {len(members)} 只"
          + (f"（已按清单过滤）" if args.include_file else "（**未过滤**）"))

    kept, dropped = SI.to_tx_codes(members["stock_code"].tolist())
    if dropped:
        print(f"  ⚠️ 剔除 {len(dropped)} 个腾讯源取不到的代码（如北交所 920xxx）: "
              f"{', '.join(c for c, _ in dropped)}")
    name_of = dict(zip(members["stock_code"].astype(str).str.zfill(6),
                       members["stock_name"].astype(str)))
    codes = [k[0] for k in kept]
    print(f"  可用代码 {len(codes)} 个，取数来源 {args.source} …")

    if args.ignore_time_in:
        n_before = members["time_in"].nunique()
        members = members.copy()
        members["time_in"] = args.start
        print(f"  ⚠️ --ignore-time-in：把 {len(members)} 只的 time_in 一律设为 {args.start}"
              f"（原 time_in 有 {n_before} 个不同取值）")

    prices = (load_prices_baostock(codes) if args.source == "baostock"
              else load_prices_tencent(codes, online=args.online))
    if not prices:
        print("❌ 没有任何个股价可取，终止")
        return 1

    # —— ① 成分股复核清单（诊断，不作筛选依据）——
    if args.rank:
        ref = args.ref_etf.strip()
        ref_df = None
        if ref:
            ref_df = (fetch_index_history(ref, prefer_cache=not args.online)
                      if ref in INDEX_CODES
                      else fetch_equity_history(ref, refresh=args.online))
        rows = []
        for tx, raw in kept:          # to_tx_codes 返回 (tx_code, raw_code)
            fr = prices.get(tx)
            if fr is None or len(fr) == 0:
                continue
            s = pd.Series(fr["close"].to_numpy(), index=pd.DatetimeIndex(fr["date"]))
            px = pd.DataFrame({"date": s.index, "close": s.to_numpy()})
            v, n = (SI.correlation(px, ref_df) if ref_df is not None else (None, 0))
            rows.append({"代码": tx, "名称": name_of.get(raw, "")[:10],
                         "历史(年)": round((s.index[-1] - s.index[0]).days / 365.25, 1),
                         "起始": s.index[0].date().isoformat(),
                         f"与{ref or '参照'}相关": (round(v, 3) if v is not None else None),
                         "重叠日": n})
        t = pd.DataFrame(rows).sort_values(f"与{ref or '参照'}相关", ascending=False,
                                           na_position="last")
        print(f"\n【成分股复核清单】按与 {ref} 的相关性排序（**只作诊断，不作筛选依据**）")
        print(t.to_string(index=False))
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        print("\n⚠️ 请逐行人工判断『是不是这个行业』，把真正的成分股写成清单文件，"
              "再用 --include-file 合成。**不要按相关性高低来筛。**")
        return 0

    # —— ② 合成 + 验证 ——
    synth = SI.build_index_from_ohlc(
        prices,
        members.drop(columns=["plate_type", "plate_code", "plate_name"], errors="ignore"),
        start=args.start, min_members=args.min_members)
    if synth.empty:
        print("❌ 合成结果为空（检查 time_in 与价格区间）")
        return 1
    ok = synth["close"].notna()
    print(f"\n合成指数 {args.name}: {len(synth)} 根，有效 {int(ok.sum())} 根，"
          f"{synth.loc[ok, 'date'].iloc[0].date()} → {synth.loc[ok, 'date'].iloc[-1].date()}"
          f"（{len(synth)} 根中成分股数 中位 {int(synth['n_members'].median())}）")

    verdicts = []
    for etf in [c.strip() for c in args.validate.split(",") if c.strip()]:
        e = fetch_equity_history(etf, refresh=args.online)
        v, n = SI.correlation(synth, e)
        v5, n5 = SI.correlation(synth, e, tail=500)
        flag = "✅ 达标" if (v is not None and v >= SI.MIN_CORR) else "❌ 不达标"
        verdicts.append({"etf": etf, "corr": v, "overlap": n, "corr500": v5, "overlap500": n5})
        print(f"  {flag} vs {etf}: 全重叠 {v if v is None else round(v, 3)}（{n} 日）"
              f" / 近500日 {v5 if v5 is None else round(v5, 3)}")
    if verdicts:
        best = max((x["corr"] or 0) for x in verdicts)
        if best < SI.MIN_CORR:
            print("  ⇒ 按 **kill criterion 5**：该合成指数**不得**用于验证，降级为『只显示』")

    if args.save:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        out = CACHE_DIR / f"synth_{args.name}_history.csv"
        synth.to_csv(out, index=False, encoding="utf-8-sig")
        SYNTH_DIR.mkdir(parents=True, exist_ok=True)
        mem = SYNTH_DIR / f"{args.name}_members.txt"
        mem.write_text("# 合成指数 " + args.name + f" 的成分股（题材 {args.plate}）\n"
                       + "\n".join(f"{k[1]}  # {name_of.get(k[1], '')}" for k in kept) + "\n",
                       encoding="utf-8")
        print(f"\n已落盘: {out}\n         {mem}")

    if args.json:
        print(json.dumps({"name": args.name, "plate": args.plate,
                          "members": len(kept), "bars": int(ok.sum()),
                          "validate": verdicts}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
