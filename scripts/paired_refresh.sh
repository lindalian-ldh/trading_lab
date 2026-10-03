#!/usr/bin/env bash
# 配对明细（--paired）重跑脚本 —— 消除 data/reports/paired/ 下的大 CSV 只能靠重跑
#
# 为什么需要它：`backtest.py --out` 产出的配对明细约 200MB/300只×10年，
# 不适合进 git。删掉之后必须能一条命令复现，否则数据无法审计。
#
# 用法：
#   ./scripts/paired_refresh.sh                    # 默认 300 只 × 2016 起
#   ./scripts/paired_refresh.sh 100 2016-01-01 out.csv
#
# 耗时：约 9s/只（扫描）+ 未缓存标的的取数（~2.6s/只，限流 0.6s）
#       300 只首次约 50~60 分钟；缓存命中后约 45 分钟。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

N="${1:-300}"
START="${2:-2016-01-01}"
OUT="${3:-data/reports/paired/paired_${N}x${START//-/}.csv}"

mkdir -p "$(dirname "$OUT")"
echo ">>> 配对明细重跑: ${N} 只 × ${START} 起 → $OUT"
uv run services/calc_indicators/backtest.py \
  --start "$START" --n "$N" --paired --out "$OUT"
echo ">>> 完成: $OUT"
echo ">>> 下一步: uv run services/calc_indicators/paired_stats.py --in $OUT --all"
