#!/usr/bin/env bash
# A/B/C 组合分析：4 格因子设计
#   因子1 = 放量触发源（b_vol_surge）是 bug 还是 feature
#   因子2 = 门控 RR_RATIO 否决（C 是否参与放行）
#   因子3 = RSI超卖触发源（由额外一格覆盖）
# 每格 50 只 × 2016 起，缓存热，约 7 分钟/格
set -uo pipefail
cd /Users/a801/Linda/Work/project/gupiao-assistant/trading_lab

# ── 限制 BLAS 线程数，降低发热（2026-09-29 加入）──
# numpy 在本机链接的是 Accelerate（macOS 系统 BLAS），各线程环境变量默认未设，
# 库会在每次调用时自行决定动用几个核 —— 表现为"明明只有一个 python 进程，机器却满负荷"。
# 注意：显式导出优于 export，且必须在 python 启动前设置才生效。
# 但要诚实：本回测的瓶颈是纯 Python 逐根循环（~2500 次/只），单线程。
# 因此这些变量只能消掉 BLAS 的额外多核开销，**降温的主要手段仍是减少总工作量**。
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export VECLIB_MAXIMUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export BLIS_NUM_THREADS=1
PY=.venv/bin/python
BT=services/calc_indicators/backtest.py
SYMS=$(
cat <<'S'
600000,600012,600023,600036,600054,600063,600073,600082,600093,600103,
600112,600121,600131,600143,600155,600165,600175,600185,600195,600206,
600216,600226,600235,600246,600257,600268,600278,600288,600299,600309,
600319,600329,600339,600353,600363,600373,600383,600393,600405,600420,
600433,600452,600466,600479,600489,600500,600510,600519,600520,600530
S
)
D=data/reports/paired
mkdir -p "$D" logs

run() {  # run <标签> <额外-o参数...>
  local tag="$1"; shift
  local out="$D/fact_${tag}.csv"
  echo ">>> [$(date +%H:%M:%S)] 开始 $tag  extra=$*"
  $PY $BT --symbols "$SYMS" --start 2016-01-01 --paired "$@" --out "$out" \
      > "logs/fact_${tag}.log" 2>&1
  local rc=$?
  if [ $rc -eq 0 ] && [ -s "$out" ]; then
    echo ">>> [$(date +%H:%M:%S)] $tag 完成 ($(wc -l < "$out") 行)"
  else
    echo ">>> [$(date +%H:%M:%S)] $tag 失败 rc=$rc"
  fi
}

# run base   # 已完成（98 只），本次不重跑
run vol_off   -o "MOMENTUM_TRIGGERS=MACD,RSI_OVERSOLD"
run rr_off    -o ENTRY_GATE_RR_RATIO_ENABLED=false
run both_off  -o "MOMENTUM_TRIGGERS=MACD,RSI_OVERSOLD" -o ENTRY_GATE_RR_RATIO_ENABLED=false
run rsi_off   -o "MOMENTUM_TRIGGERS=MACD,VOLUME_SURGE"

echo ">>> [$(date +%H:%M:%S)] 全部 5 格完成"
ls -la "$D"/fact_*.csv
