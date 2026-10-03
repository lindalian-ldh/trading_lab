#!/usr/bin/env python3
"""校验 `-o KEY=VALUE` 覆盖是否真的改变了信号集合。

起因：run_abc_factorial.sh 的 4 格因子设计里，
    rsi_off  (-o MOMENTUM_TRIGGERS=MACD,VOLUME_SURGE)
    rr_off   (-o ENTRY_GATE_RR_RATIO_ENABLED=false)
两个 cell 的 signal 集合**逐一相同**，而 default 也是同一个集合 ——
说明这两个覆盖是空操作，因子设计实际退化成 1 个因子 2 个水平。

本脚本用一小批标的把三/四种配置各跑一遍，直接比对 (symbol,date) 集合。
不需要网络（全部命中本地缓存）。

用法：
    python scripts/check_override_effect.py            # 默认 15 只，约 1~2 分钟/格
    python scripts/check_override_effect.py --n 30
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv/bin/python"
BT = ROOT / "services/calc_indicators/backtest.py"
OUT = ROOT / "data/reports/paired/_override_check"
TMP = ROOT / "logs/_override_check"

DEFAULT_SYMBOLS = ("600000,600012,600023,600036,600054,600063,600073,600082,600093,600103,"
                   "600112,600121,600131,600143,600155,600165,600175,600185,600195,600206,"
                   "600216,600226,600235,600246,600257,600268,600278,600288,600299,600309")

CASES = {
    "default":   [],
    "vol_off":   ["MOMENTUM_TRIGGERS=MACD,RSI_OVERSOLD"],
    "rsi_off":   ["MOMENTUM_TRIGGERS=MACD,VOLUME_SURGE"],
    "rr_off":    ["ENTRY_GATE_RR_RATIO_ENABLED=false"],
    "both_off":  ["MOMENTUM_TRIGGERS=MACD,RSI_OVERSOLD", "ENTRY_GATE_RR_RATIO_ENABLED=false"],
}


def signal_set(path: Path) -> set:
    d = pd.read_csv(path, usecols=["symbol", "date", "rec_type"], dtype={"symbol": str})
    g = d[d.rec_type == "signal"]
    return set(zip(g.symbol, g.date))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    ap.add_argument("--start", default="2016-01-01")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)

    sets: dict[str, set] = {}
    for tag, ovs in CASES.items():
        csv = OUT / f"{tag}.csv"
        cmd = [str(PY), str(BT), "--symbols", args.symbols,
               "--start", args.start, "--paired", "--out", str(csv)]
        for o in ovs:
            cmd += ["-o", o]
        print(f">>> 运行 {tag}  override={ovs or '（无）'}", flush=True)
        with open(TMP / f"{tag}.log", "w") as fh:
            rc = subprocess.call(cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT)
        if rc != 0 or not csv.exists():
            print(f"    ❌ {tag} 失败 rc={rc}，见 {TMP / f'{tag}.log'}")
            return 1
        sets[tag] = signal_set(csv)
        print(f"    signal n={len(sets[tag])}", flush=True)

    base = sets["default"]
    print("\n=== 各覆盖相对 default 的实际效果 ===")
    print(f"{'cell':10s} {'signal n':>9s} {'与default相同?':>15s} {'新增':>7s} {'消失':>7s}  判定")
    for tag in CASES:
        if tag == "default":
            continue
        same = sets[tag] == base
        verdict = "⚠️ 空操作（覆盖无效）" if same else "有效"
        print(f"{tag:10s} {len(sets[tag]):>9d} {str(same):>15s} "
              f"{len(sets[tag] - base):>7d} {len(base - sets[tag]):>7d}  {verdict}")

    n_distinct = len({frozenset(v) for v in sets.values()})
    print(f"\n共 {len(CASES)} 个配置，实际只产生 {n_distinct} 种不同的信号集合。")
    if n_distinct < len(CASES):
        print("⇒ 因子设计存在退化：被判定为「空操作」的 cell 不能用来论证任何结论。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
