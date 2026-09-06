#!/usr/bin/env bash
# crawler_news 子服务环境依赖安装与目录骨架初始化脚本
#
# 功能：
#   1. 校验 uv 工具链可用
#   2. 执行 uv sync 安装根 pyproject.toml 中声明的 akshare 等运行时依赖
#   3. 验证 akshare 可正常导入并打印版本号
#   4. 创建 crawler_news 子服务目录骨架与初始 stock_mapping.json
#      （数据输出到项目根 data/ 下，与其他 service 共用）
#
# 用法：
#   chmod +x scripts/setup_crawler_news.sh
#   ./scripts/setup_crawler_news.sh
#
# 产出：
#   data/raw/news/.gitkeep
#   data/reports/news/.gitkeep
#   data/stock_mapping.json   (初始空映射表)
#   services/crawler_news/logs/.gitkeep
#
# 前置：uv 在 PATH 中；根目录已存在 pyproject.toml（含 akshare>=1.12 依赖）。
# 幂等：重复执行不会覆盖已存在的 stock_mapping.json，仅补齐缺失目录与 .gitkeep。
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

SERVICE_DIR="$PROJECT_DIR/services/crawler_news"
DATA_DIR="$PROJECT_DIR/data"

echo "=============================================="
echo "  crawler_news 阶段零：环境依赖与目录骨架"
echo "=============================================="

# ---------- [1/4] 校验 uv 工具链 ----------
echo "[1/4] 校验 uv 工具链"
# 优先用 PATH 中的 uv；找不到则 fallback 到常见安装路径（macOS Homebrew / curl / cargo）
UV_BIN=""
for candidate in "$(command -v uv 2>/dev/null)" "/opt/homebrew/bin/uv" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
  if [[ -n "$candidate" && -x "$candidate" ]]; then
    UV_BIN="$candidate"
    break
  fi
done
if [[ -z "$UV_BIN" ]]; then
  echo "❌ 未找到 uv 命令，请先安装：curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi
echo "    ✓ uv 路径：$UV_BIN"

# ---------- [2/4] uv sync 安装依赖 ----------
echo "[2/4] 执行 uv sync 安装依赖（根 pyproject.toml 含 akshare>=1.12）"
if ! "$UV_BIN" sync; then
  echo "❌ uv sync 失败，请检查根 pyproject.toml 与网络后重试"
  exit 2
fi
echo "    ✓ uv sync 完成"

# ---------- [3/4] 验证 akshare 可导入 ----------
echo "[3/4] 验证 akshare 可导入"
if ! "$UV_BIN" run python -c "import akshare as ak; print('    ✓ akshare version:', ak.__version__)"; then
  echo "❌ akshare 导入失败，请检查 uv sync 是否成功安装 akshare"
  exit 3
fi

# ---------- [4/4] 创建目录骨架与初始 stock_mapping.json ----------
echo "[4/4] 创建目录骨架与初始 stock_mapping.json"

# 数据输出目录：共用项目根 data/
mkdir -p "$DATA_DIR/raw/news"
mkdir -p "$DATA_DIR/reports/news"

# 服务私有目录：日志
mkdir -p "$SERVICE_DIR/logs"

# .gitkeep 占位，让 git 跟踪空目录
touch "$DATA_DIR/raw/news/.gitkeep"
touch "$DATA_DIR/reports/news/.gitkeep"
touch "$SERVICE_DIR/logs/.gitkeep"

# stock_mapping.json 仅在不存在时初始化（幂等，不覆盖已有映射）
if [[ ! -f "$DATA_DIR/stock_mapping.json" ]]; then
  cat > "$DATA_DIR/stock_mapping.json" <<'EOF'
{
  "_comment": "股票代码↔名称映射表（code 为键，name 为值）。tagger.py 通过遍历反查 name→code。可手动维护，或由 bankuai-service 板块扫描结果沉淀。",
  "_example": "格式如 \"600519\": \"贵州茅台\"",
  "600519": "贵州茅台",
  "000001": "平安银行"
}
EOF
  echo "    ✓ 初始化 stock_mapping.json（含 2 条示例）"
else
  echo "    ✓ stock_mapping.json 已存在，跳过初始化"
fi

echo ""
echo "=============================================="
echo "  ✅ 阶段零完成"
echo "=============================================="
echo "目录骨架："
echo "  data/                              # 项目根共用数据目录"
echo "    ├── raw/news/                    # 原始 JSON Lines（按月归档 {YYYY-MM}/{source}_{date}.json）"
echo "    ├── reports/news/                # 报告输出（{start}_{end}_report.{md|html}）"
echo "    └── stock_mapping.json           # 股票代码↔名称映射表"
echo "  services/crawler_news/"
echo "    ├── logs/                        # 日志按日切分（crawler_news_YYYY-MM-DD.log）"
echo "    └── pyproject.toml               # 依赖面文档（akshare>=1.12, playwright 降级备用）"
echo ""
echo "下一步：进入阶段一（config.py + logger.py）"
