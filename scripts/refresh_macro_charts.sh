#!/usr/bin/env bash
# 宏观核心组合 dashboard 刷新脚本（中国 + 美国，各一个 HTML）
#
# 功能：
#   1. 刷新数据库：拉取 8 个中国 + 9 个美国宏观指标的最新数据（macro-data-service）
#   2. 生成中国宏观 dashboard HTML：5 组核心组合（macro-charts，; 分隔多图纵向堆叠）
#   3. 生成美国及全球资产 dashboard HTML：6 组核心组合
#
# 用法：
#   chmod +x scripts/refresh_macro_charts.sh
#   ./scripts/refresh_macro_charts.sh                     # 增量追加（默认 60 期）
#   RECENT=120 ./scripts/refresh_macro_charts.sh         # 自定义取数期数
#   RECENT=0 ./scripts/refresh_macro_charts.sh --reset   # 全量重建（清空后拉全部历史）
#
# 产出：
#   data/reports/charts/macro_dashboard_CN.html
#   data/reports/charts/macro_dashboard_US.html
#
# 前置：已在项目根执行 uv sync；FRED_API_KEY 已写入 .env；uv 在 PATH 中。
# 数据刷新失败不阻断图表生成（图表将基于库内已存数据渲染）。
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

RECENT="${RECENT:-60}"                       # 取最近 N 期，默认 60（月频≈5年，日频≈3个月）
RESET_FLAG=""
# 解析 --reset 参数：清空数据库后重拉全量
if [[ "${1:-}" == "--reset" ]]; then
  RESET_FLAG="--reset"
  echo "⚠️  将清空 macro_data / macro_us_data 两表后重新获取数据"
  shift
fi

CHARTS_DIR="$PROJECT_DIR/data/reports/charts"
mkdir -p "$CHARTS_DIR"

# 中国宏观指标键（macro_data 表，对应 macro-data-service REGISTRY）
CN_KEYS="cpi_cn,ppi_cn,gdp_cn,pmi_cn,m2_cn,lpr_cn,social_financing_cn,unemployment_cn"
# 美国及商品指标键（macro_us_data 表）
US_KEYS="fred_unrate,fred_payems,fred_cpiaucsl,fred_dgs10,fred_gs10,fred_dtwexbgs,sge_gold,sge_silver,fred_ppiaco,fred_dgs6mo"

# 中国 5 组核心组合（; 分隔不同图，, 分隔同图对比）
#   增长周期(PMI,GDP) / 通胀剪刀差(CPI,PPI) / 信用传导(M2,SF) /
#   政策利率与地产(LPR5Y,SF) / 就业与消费复苏(UE,CPI)
CN_COMBOS="PMI,GDP;CPI,PPI;M2,SF;LPR5Y,SF;UE,CPI"
# 美国 6 组核心组合
#   劳动力(UNRATE,PAYEMS) / 通胀-利率(CPIAUCSL,GS10) / 避险跷跷板(DTWEXBGS,GOLD) /
#   贵金属联动(GOLD,SILVER) / 生产端传导(PPIACO,CPIAUCSL) / 美债曲线(DGS6MO,DGS10)
#   注：通胀-利率组用 GS10(月度) 而非 DGS10(日度)，与月频 CPIAUCSL 对齐；
#       美债曲线组 DGS6MO,DGS10 均为日度，保持不变。
US_COMBOS="UNRATE,PAYEMS;CPIAUCSL,GS10;DTWEXBGS,GOLD;GOLD,SILVER;PPIACO,CPIAUCSL;DGS6MO,DGS10"

echo "############################################################"
echo "  宏观核心组合 dashboard 刷新  $(date '+%Y-%m-%d %H:%M:%S')"
echo "  项目目录: $PROJECT_DIR"
echo "  取数期数: RECENT=$RECENT"
echo "  重置模式: ${RESET_FLAG:-否（增量追加）}"
echo "############################################################"

# uv 可用性检查
if ! command -v uv >/dev/null 2>&1; then
  echo "❌ 未找到 uv，请先安装并加入 PATH（https://docs.astral.sh/uv/）" >&2
  exit 1
fi

# 容错执行：某步失败不中断后续步骤
run_step() {
  local desc="$1"; shift
  echo ""
  echo ">>> $desc"
  if "$@"; then
    echo "    ✅ 完成"
  else
    echo "    ⚠️  失败（继续执行后续步骤，图表将使用库内已存数据）"
  fi
}

# ---------------------------------------------------------------- #
# 1. 刷新数据库（中国 + 美国，分两次调用，各自享 300s 超时预算）
# ---------------------------------------------------------------- #
run_step "[1/4] 刷新中国宏观数据（macro_data）" \
  uv run services/macro-data-service/main.py $RESET_FLAG --indicator "$CN_KEYS" --recent "$RECENT"

run_step "[2/4] 刷新美国及商品数据（macro_us_data）" \
  uv run services/macro-data-service/main.py $RESET_FLAG --indicator "$US_KEYS" --recent "$RECENT"

# ---------------------------------------------------------------- #
# 2. 生成 dashboard HTML（; 多组合并到同一页面，纵向堆叠）
# ---------------------------------------------------------------- #
run_step "[3/4] 生成中国宏观 dashboard（5 组组合）" \
  uv run services/macro-charts/main.py --table macro_data \
    --indicator "$CN_COMBOS" \
    --output html --out "$CHARTS_DIR/macro_dashboard_CN.html"

run_step "[4/4] 生成美国及全球资产 dashboard（6 组组合）" \
  uv run services/macro-charts/main.py --table macro_us_data \
    --indicator "$US_COMBOS" \
    --output html --out "$CHARTS_DIR/macro_dashboard_US.html"

echo ""
echo "############################################################"
echo "  完成 ✅"
echo "  中国 dashboard: $CHARTS_DIR/macro_dashboard_CN.html"
echo "  美国 dashboard: $CHARTS_DIR/macro_dashboard_US.html"
echo ""
echo "  浏览器打开:"
echo "    open \"$CHARTS_DIR/macro_dashboard_CN.html\""
echo "    open \"$CHARTS_DIR/macro_dashboard_US.html\""
echo "############################################################"
