# Trading Lab

模块化盘后交易分析工具箱（MacBook / Apple Silicon）。基于 `uv` Workspace 的 Python Monorepo，跑完即退的批处理模式，时序数据存 Parquet、元数据存 SQLite、爬虫原始数据存 JSON，预留 AI 接口，附 Playwright 轻量爬虫。

## 目录结构

```
trading_lab/
├── .env / .env.example       # 环境变量（AI_ENABLED 等）
├── .python-version           # 3.11
├── pyproject.toml            # 根 uv workspace 配置
├── config/settings.py        # Pydantic BaseSettings 配置
├── core/                     # 共享核心库（logger/storage/exchange/ai_client）
├── services/                 # 4 个独立微工具（各有 pyproject.toml）
│   ├── fetch_klines/         # 抓取日线（ccxt + pandas）
│   ├── calc_indicators/      # 计算技术指标（pandas + numpy）
│   ├── crawler_news/         # Playwright 爬虫
│   └── generate_report/      # 生成复盘摘要（jinja2）
├── data/                     # raw/klines(Parquet) html_cache(JSON) trading.db(SQLite)
├── scripts/run_all.py        # 串行编排所有工具
├── scripts/setup_cron.sh     # 一键安装 Crontab
└── logs/                     # 按天分割日志
```

## 快速开始

### 1. 安装 uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. 同步依赖（自动创建共享 .venv，含所有 workspace 成员依赖）

```bash
cd trading_lab
uv sync
```

### 3. （仅爬虫需要）安装 Playwright 浏览器内核（一次性）

```bash
uv run playwright install chromium
```

## 手动运行单个脚本

所有 `main.py` 支持 `--date`（YYYY-MM-DD，默认昨天）和 `--symbol`（可选）：

```bash
uv run services/fetch_klines/main.py --date 2026-08-06 --symbol BTC/USDT
uv run services/calc_indicators/main.py --date 2026-08-06
uv run services/crawler_news/main.py --date 2026-08-06
uv run services/generate_report/main.py --date 2026-08-06
```

## 一键串行执行（盘后批处理）

按 `fetch_klines -> calc_indicators -> crawler_news -> generate_report` 顺序串行执行，
自动以“昨天”为参数，每任务超时 600s：

```bash
uv run scripts/run_all.py
```

## 安装每日定时任务

```bash
chmod +x scripts/setup_cron.sh
./scripts/setup_cron.sh
```

将写入 Crontab：每日 16:30 执行 `uv run scripts/run_all.py`，输出追加到 `logs/cron.log`。

## 测试

```bash
uv run pytest
```

## 数据存储约定

| 类型 | 位置 | 格式 |
|------|------|------|
| K线 / 指标 | `data/raw/klines/{SYMBOL}/{YYYY-MM-DD}.parquet` | Parquet |
| 爬虫原始数据 | `data/html_cache/news_{date}.json` | JSON |
| 交易记录 / 运行日志 | `data/trading.db` | SQLite |
| 复盘报告 | `data/reports/report_{date}.txt` | 文本 |

## 设计原则

- 跑完即退，无常驻 `while True` 循环。
- `core` 仅做无状态纯函数，杜绝过度抽象。
- 所有 `main` 入口 `try-except` 包裹，失败以非零状态码退出。
- AI 仅占位（Mock 返回 0.5），不引入 openai/anthropic 等重包。
