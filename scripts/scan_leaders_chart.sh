#!/usr/bin/env bash
# 板块龙头扫描 + 入场指标图生成 一键脚本
#
# 功能：
#   1. 调用 bankuai-service 扫描前 N 个热门板块（默认 3），提取每个板块的一级领涨龙头
#   2. 对每只龙头股票调用 calc_indicators 生成入场指标 PNG 图片
#   3. 支持透传 -p 配置档参数（如 aggressive_short）到 calc_indicators
#
# 用法：
#   chmod +x scripts/scan_leaders_chart.sh
#   ./scripts/scan_leaders_chart.sh                                       # 默认: 前3板块, default 配置
#   ./scripts/scan_leaders_chart.sh -p aggressive_short                   # 传入配置档
#   ./scripts/scan_leaders_chart.sh -p aggressive_short -p long_term      # 叠加配置档
#   ./scripts/scan_leaders_chart.sh --top-n 5                             # 前5个板块
#   ./scripts/scan_leaders_chart.sh --date 2026-08-12                     # 指定日期
#   ./scripts/scan_leaders_chart.sh --plate-type 行业                     # 行业板块
#   ./scripts/scan_leaders_chart.sh --sectors 半导体,AI算力               # 仅扫描指定板块
#   ./scripts/scan_leaders_chart.sh --sectors "半导体,新能源,消费" --open   # 指定板块+自动打开
#   ./scripts/scan_leaders_chart.sh --open                                # 自动打开图片
#
# 产出：
#   data/reports/bankuai/bankuai_{date}_{plate_type}.json     板块扫描报告
#   data/reports/{当天日期}-png/three/  满足3个维度的指标图
#   data/reports/{当天日期}-png/two/    满足2个维度的指标图
#   data/reports/{当天日期}-png/one/    满足1个维度的指标图
#
# 前置：已在项目根执行 uv sync；ZZSHARE_TOKEN 已写入 .env；uv 在 PATH 中。
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# ==================== 参数解析 ====================
PROFILES=()
DATE=""
TOP_N=3
PLATE_TYPE="概念"
OPEN_FLAG=""
SECTORS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -p|--profile)
      [[ $# -lt 2 ]] && { echo "❌ $1 需要参数" >&2; exit 1; }
      PROFILES+=("$2"); shift 2 ;;
    --date)
      [[ $# -lt 2 ]] && { echo "❌ $1 需要参数" >&2; exit 1; }
      DATE="$2"; shift 2 ;;
    --top-n)
      [[ $# -lt 2 ]] && { echo "❌ $1 需要参数" >&2; exit 1; }
      TOP_N="$2"; shift 2 ;;
    --plate-type)
      [[ $# -lt 2 ]] && { echo "❌ $1 需要参数" >&2; exit 1; }
      PLATE_TYPE="$2"; shift 2 ;;
    --sectors)
      [[ $# -lt 2 ]] && { echo "❌ $1 需要参数" >&2; exit 1; }
      SECTORS="$2"; shift 2 ;;
    --open)
      OPEN_FLAG="--open"; shift ;;
    -h|--help)
      sed -n '2,24p' "$0"
      exit 0 ;;
    *)
      echo "❌ 未知参数: $1" >&2
      exit 1 ;;
  esac
done

# 默认日期 = 昨天（macOS date）
if [[ -z "$DATE" ]]; then
  DATE=$(date -v-1d +%Y-%m-%d)
fi

# 配置档描述
PROFILE_DESC="${PROFILES[*]:-default}"

# --sectors 模式：直接透传给 bankuai-service，由 Python 层过滤
if [[ -n "$SECTORS" ]]; then
  SECTOR_COUNT=$(echo "$SECTORS" | tr ',' '\n' | grep -c .)
  SCAN_DESC="指定 $SECTOR_COUNT 个板块: ${SECTORS}"
  SECTORS_ARG="--sectors $SECTORS"
  # sectors 模式下 top-n 仅影响 hot_sectors 截取，不影响 target 过滤
  SCAN_TOP_N=$TOP_N
else
  SCAN_DESC="前 $TOP_N 个热门板块"
  SECTORS_ARG=""
  SCAN_TOP_N=$TOP_N
fi

# uv 可用性检查
if ! command -v uv >/dev/null 2>&1; then
  echo "❌ 未找到 uv，请先安装并加入 PATH（https://docs.astral.sh/uv/）" >&2
  exit 1
fi

# 路径
# TODAY：当天日期（YYYYMMDD），用于 PNG 子目录名和文件名匹配
#        calc_indicators 的 chart.py 用 datetime.now() 生成文件名，与此一致
TODAY=$(date +%Y%m%d)
REPORT_DIR="$PROJECT_DIR/data/reports/bankuai"
REPORT_FILE="$REPORT_DIR/bankuai_${DATE}_${PLATE_TYPE}.json"
CHART_DIR="$PROJECT_DIR/data/reports/${TODAY}-png"
mkdir -p "$CHART_DIR"
# 按通过维度数分档归档
mkdir -p "$CHART_DIR/three" "$CHART_DIR/two" "$CHART_DIR/one"

echo "############################################################"
echo "  板块龙头扫描 + 入场指标图  $(date '+%Y-%m-%d %H:%M:%S')"
echo "  项目目录:   $PROJECT_DIR"
echo "  扫描日期:   $DATE"
echo "  板块类型:   $PLATE_TYPE"
echo "  扫描范围:   $SCAN_DESC"
echo "  配置档:     $PROFILE_DESC"
echo "  自动打开:   ${OPEN_FLAG:-否}"
echo "############################################################"

# ==================== Step 1: 板块扫描 ====================
echo ""
echo ">>> [1/2] 调用 bankuai-service 扫描板块（${SCAN_DESC}）"
echo "    （约 $((SCAN_TOP_N + 1)) 次 API 调用 × 2s 限频）"

uv run services/bankuai-service/main.py \
  --date "$DATE" \
  --plate-type "$PLATE_TYPE" \
  --top-n "$SCAN_TOP_N" \
  --bottom-n 0 \
  $SECTORS_ARG \
  --quiet

if [[ ! -f "$REPORT_FILE" ]]; then
  echo "❌ 板块扫描报告未生成: $REPORT_FILE" >&2
  exit 1
fi

echo "    ✅ 报告已保存: $REPORT_FILE"

# ==================== Step 2: 提取一级领涨龙头 + 生成指标图 ====================
echo ""
echo ">>> [2/2] 提取一级领涨龙头并生成入场指标图"

# 用 Python 解析 JSON，输出 "板块名|股票代码|股票名称" 行
# bankuai-service 已通过 --sectors 过滤，sector_leaders 中仅包含目标板块
STOCK_LIST=$(uv run python -c "
import json
with open('${REPORT_FILE}', encoding='utf-8') as f:
    report = json.load(f)
leaders = report.get('sector_leaders', {})
for name, data in leaders.items():
    tier1 = data.get('tier1', [])
    for stock in tier1:
        code = stock.get('code', '')
        sname = stock.get('name', '')
        if code:
            print(f'{name}|{code}|{sname}')
")

if [[ -z "$STOCK_LIST" ]]; then
  echo "⚠️  未提取到任何一级领涨龙头，流程结束"
  exit 0
fi

# 去重（同一股票可能在多个板块出现，按股票代码去重只处理一次）
STOCK_LIST=$(echo "$STOCK_LIST" | sort -u -t'|' -k2,2)

TOTAL=$(echo "$STOCK_LIST" | wc -l | tr -d ' ')
echo "    共 $TOTAL 只一级领涨龙头（已按股票代码去重）"
echo ""

# ==================== 未入选标的列表 ====================
echo ">>> 未入选任何梯队的标的（一级/二级/三级 均不满足）"
echo ""

uv run python -c "
import json
with open('${REPORT_FILE}', encoding='utf-8') as f:
    report = json.load(f)
leaders = report.get('sector_leaders', {})
has_any = False
for name, data in leaders.items():
    unselected = data.get('unselected', [])
    if unselected:
        has_any = True
        avg = data.get('avg_change', 0)
        print(f'  [{name}] 板块均幅={avg:.2f}%  未入选={len(unselected)}只:')
        for s in unselected:
            code = s.get('code', '')
            sname = s.get('name', '')
            ch = s.get('change', 0)
            tr = s.get('turnover_rate', 0)
            vr = s.get('vol_ratio', 0)
            print(f'    {code} {sname}  涨幅={ch:.2f}%  换手={tr:.2f}%  量比={vr:.2f}')
        print()
if not has_any:
    print('  (所有标的均已入选某个梯队)')
"

echo ""

# 逐只生成指标图
SUCCESS=0
FAILED=0
SKIPPED=0
THREE_CNT=0
TWO_CNT=0
ONE_CNT=0
IDX=0
while IFS='|' read -r sector code name; do
  IDX=$((IDX + 1))
  echo "  [$IDX/$TOTAL] $sector → $code $name"

  # 构造 calc_indicators 命令（透传 -p 配置档 + -r 生成图片 + --name 传入股票名称）
  # --skip-all-fail: 三维度全不通过时跳过 PNG 生成（退出码 2）
  # 注意：不传 --open 给 calc_indicators，而是移动文件后由脚本统一打开
  CMD=(uv run services/calc_indicators/main.py "$code" --name "$name")
  for p in "${PROFILES[@]+"${PROFILES[@]}"}"; do
    CMD+=("-p" "$p")
  done
  CMD+=("-r" "--skip-all-fail")

  # 运行 calc_indicators，捕获输出到临时文件用于解析
  TMP_OUT=$(mktemp)
  "${CMD[@]}" > "$TMP_OUT" 2>&1
  EXIT_CODE=$?
  cat "$TMP_OUT"

  if [[ $EXIT_CODE -eq 2 ]]; then
    # 三维度均不通过，跳过
    echo "    ⏭️  三维度均不通过，暂不建议开仓 —— 跳过"
    SKIPPED=$((SKIPPED + 1))
  elif [[ $EXIT_CODE -eq 0 ]]; then
    # calc_indicators 默认输出到 data/reports/report_{code}_{TODAY}.png
    # 解析总结行，按通过维度数分档归档
    #   "三维度均通过"        → 3 通过 → three/
    #   "维度 X 未通过"        → 2 通过 → two/
    #   "维度 X, Y 未通过"     → 1 通过 → one/
    SUMMARY=$(grep -E "建议开仓" "$TMP_OUT" | tail -1)
    if echo "$SUMMARY" | grep -q "三维度均通过"; then
      PASS_COUNT=3
      TIER_DIR="three"
    else
      # 提取 "维度 " 与 " 未通过" 之间的失败维度列表，统计逗号数+1 = 失败维度数
      FAILED_LIST=$(echo "$SUMMARY" | sed -E 's/.*维度 (.+) 未通过.*/\1/')
      FAILED_N=$(echo "$FAILED_LIST" | tr -cd ',' | wc -c | tr -d ' ')
      FAILED_N=$((FAILED_N + 1))
      PASS_COUNT=$((3 - FAILED_N))
      if [[ $PASS_COUNT -eq 2 ]]; then
        TIER_DIR="two"
      else
        TIER_DIR="one"
      fi
    fi

    SRC_PNG="$PROJECT_DIR/data/reports/report_${code}_${TODAY}.png"
    DST_PNG="$CHART_DIR/$TIER_DIR/report_${code}_${TODAY}.png"
    if [[ -f "$SRC_PNG" ]]; then
      mv "$SRC_PNG" "$DST_PNG"
      echo "    📁 已归档(通过${PASS_COUNT}维 → $TIER_DIR/): $DST_PNG"
      [[ -n "$OPEN_FLAG" ]] && open "$DST_PNG"
    else
      echo "    ⚠️  未找到生成的 PNG: $SRC_PNG"
    fi
    SUCCESS=$((SUCCESS + 1))
    case "$TIER_DIR" in
      three) THREE_CNT=$((THREE_CNT + 1)) ;;
      two)   TWO_CNT=$((TWO_CNT + 1)) ;;
      one)   ONE_CNT=$((ONE_CNT + 1)) ;;
    esac
  else
    echo "    ⚠️  生成失败，继续下一只"
    FAILED=$((FAILED + 1))
  fi
  rm -f "$TMP_OUT"
  echo ""
done <<< "$STOCK_LIST"

# ==================== 汇总 ====================
echo "############################################################"
echo "  完成 ✅  $(date '+%Y-%m-%d %H:%M:%S')"
echo "  板块扫描报告: $REPORT_FILE"
echo "  指标图目录:   $CHART_DIR"
echo "  成功: $SUCCESS (三通过: $THREE_CNT, 两通过: $TWO_CNT, 一通过: $ONE_CNT)  跳过(三维全不通过): $SKIPPED  失败: $FAILED  总计: $TOTAL"
echo ""
echo "  分档目录:"
echo "    three/ (3维通过): $THREE_CNT 只    open \"$CHART_DIR/three\""
echo "    two/   (2维通过): $TWO_CNT 只    open \"$CHART_DIR/two\""
echo "    one/   (1维通过): $ONE_CNT 只    open \"$CHART_DIR/one\""
echo "############################################################"
