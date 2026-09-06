#!/usr/bin/env bash
# 一键安装盘后批处理 Crontab 定时任务（每日 16:30 执行）。
# 用法：chmod +x scripts/setup_cron.sh && ./scripts/setup_cron.sh
set -euo pipefail

# 动态解析项目根目录绝对路径（本脚本位于 scripts/ 下）
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CRON_ENTRY="30 16 * * * cd ${PROJECT_DIR} && uv run scripts/run_all.py >> logs/cron.log 2>&1"

# 避免重复安装：先剔除旧条目再追加
( crontab -l 2>/dev/null | grep -v -F "${PROJECT_DIR}/scripts/run_all.py" || true ) \
    | { cat; echo "${CRON_ENTRY}"; } \
    | crontab -

echo "✅ 已安装 Cron 任务："
echo "   ${CRON_ENTRY}"
echo ""
echo "查看当前任务:  crontab -l"
echo "移除该任务:    crontab -l | grep -v '${PROJECT_DIR}' | crontab -"
echo ""
echo "注意：首次运行前请确认"
echo "  1) 已安装 uv 并在 PATH 中"
echo "  2) 已在项目根执行过 uv sync"
echo "  3) Playwright 浏览器内核已安装: uv run playwright install chromium"
