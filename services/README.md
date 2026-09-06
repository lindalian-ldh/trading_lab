# Services 子服务使用指南

本目录是 `trading_lab` 的服务集合，每个子目录为一个独立服务，遵循 `argparse` CLI + 跑完即退的批量模式，统一由根目录 `run_all.py` 串行调度。

目前包含的服务：

| 服务 | 作用 | 数据存储 |
|------|------|----------|
| [macro-data-service](macro-data-service/README.md) | 中国/美国/商品宏观指标采集（akshare + FRED） | SQLite `macro_data` / `macro_us_data` 表 或 Parquet |
| [macro-charts](macro-charts/README.md) | 宏观指标时序可视化（Plotly 折线图，多 Y 轴） | 读取上述表，输出 JSON / 自包含 HTML |
| fetch_klines | K 线行情抓取 | Parquet |
| calc_indicators | 技术指标计算 | Parquet |
| crawler_news | 新闻爬虫 | JSON / SQLite |
| generate_report | 报告生成 | HTML / Markdown |

> 通用前置：所有命令均需在 `trading_lab` 目录下执行，并先运行 `uv sync` 安装依赖。

```bash
cd trading_lab
uv sync                # 首次：安装 akshare / plotly / pandas-datareader / pyarrow / pyyaml 等
```

---

## 一、macro-data-service 宏观数据采集

### 1.1 默认行为

- 不带任何参数：每个启用指标只取**最近 1 条**记录并写入 SQLite。
- 默认存储：SQLite (`data/trading.db`)，默认模式：`incremental`（增量去重）。
- CLI 参数覆盖 `config.yaml` 同名配置；单次执行 300 秒超时保护。

### 1.2 CLI 参数总览

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `--date` | string (YYYY-MM-DD) | 今天 | 基准日期，仅作日志记录 |
| `--indicator` | string | `all` | 指标键名，多个用逗号分隔（如 `cpi_cn` / `sge_gold,sge_silver`） |
| `--storage` | `sqlite` / `parquet` | 配置文件值 | 覆盖存储方式 |
| `--mode` | `incremental` / `overwrite` | 配置文件值 | 增量去重 / 全量覆盖 |
| `--recent` | int | 1 | 取最近 N 条；**0 = 全部历史** |
| `--reset` | flag | false | 获取前清空两表 + Parquet 目录 |

### 1.3 常用命令样例

```bash
cd trading_lab

# —— 基础用法 ——
uv run services/macro-data-service/main.py                          # 默认：所有启用指标，每条取最近1条
uv run services/macro-data-service/main.py --date 2026-08-08        # 指定基准日期（仅日志记录用）

# —— 指标筛选（--indicator）——
uv run services/macro-data-service/main.py --indicator cpi_cn              # 单个中国指标
uv run services/macro-data-service/main.py --indicator fred_unrate         # 单个 FRED 美国指标
uv run services/macro-data-service/main.py --indicator sge_gold,sge_silver # 多指标(逗号分隔，无空格亦可)
uv run services/macro-data-service/main.py --indicator cpi_cn,ppi_cn,m2_cn # 批量中国月度指标
uv run services/macro-data-service/main.py --indicator all                 # 显式取全部

# —— 控制取数范围（--recent）——
uv run services/macro-data-service/main.py --recent 1              # 最近 1 条（默认）
uv run services/macro-data-service/main.py --recent 12             # 最近 12 期
uv run services/macro-data-service/main.py --recent 60             # 最近 60 期（约 5 年月度数据）
uv run services/macro-data-service/main.py --recent 0              # 0 = 全部历史

# —— 切换存储（--storage）——
uv run services/macro-data-service/main.py --storage sqlite        # SQLite（默认，写 macro_data / macro_us_data 表）
uv run services/macro-data-service/main.py --storage parquet       # Parquet（按 indicator/year 分区）

# —— 写入模式（--mode）——
uv run services/macro-data-service/main.py --mode incremental      # 增量去重（默认，相同主键 INSERT OR REPLACE）
uv run services/macro-data-service/main.py --mode overwrite        # 全量覆盖（先删该指标旧行再插入，用于数据修正）

# —— 重置数据（--reset）——
uv run services/macro-data-service/main.py --reset                 # 清空 macro_data/macro_us_data 两表 + data/macro* 目录后重抓
uv run services/macro-data-service/main.py --reset --recent 0      # 清空后拉全部历史（首次初始化推荐组合）

# —— 组合用法 ——
uv run services/macro-data-service/main.py --indicator cpi_cn --recent 60 --mode overwrite      # 修正 CPI 最近 5 年数据
uv run services/macro-data-service/main.py --indicator sge_gold,sge_silver --recent 30           # 贵金属最近 30 天
uv run services/macro-data-service/main.py --storage parquet --mode overwrite --recent 0         # Parquet 全量重算
```

### 1.4 常见指标键名速查

| 键名 | 指标 | 国家 | 表 | 来源 |
|------|------|------|----|----|
| `cpi_cn` / `ppi_cn` / `m2_cn` | CPI / PPI / M2 | CN | macro_data | akshare |
| `gdp_cn` / `pmi_cn` / `ip_cn` | GDP / PMI / 工业增加值 | CN | macro_data | akshare |
| `lpr_cn` | LPR（拆 1Y/5Y） | CN | macro_data | akshare |
| `sf_cn` / `ue_cn` | 社融 / 失业率 | CN | macro_data | akshare |
| `fred_unrate` / `fred_cpi` / `fred_gdp` | 失业率 / CPI / GDP | US | macro_us_data | FRED |
| `fred_dgs10` / `fred_dgs1` | 10年/1年国债收益率 | US | macro_us_data | FRED |
| `sge_gold` / `sge_silver` | 黄金/白银现货基准价 | US | macro_us_data | akshare |
| `gold_inv` / `silver_inv` | COMEX 贵金属库存 | US | macro_us_data | akshare |

> 完整清单见 [macro-data-service/README.md](macro-data-service/README.md) 采集指标清单。
> FRED 系列需在 `.env` 配置 `FRED_API_KEY`（免费申请：https://fred.stlouisfed.org）。

### 1.5 验证数据

```bash
# 查看中国指标
sqlite3 -header -column data/trading.db "SELECT indicator,date,value,unit FROM macro_data ORDER BY indicator;"

# 查看美国及商品指标
sqlite3 -header -column data/trading.db "SELECT indicator,date,value,unit,source FROM macro_us_data ORDER BY indicator;"

# 查询溯源日志
tail -f logs/macrodata-fetch.log
```

---

## 二、macro-charts 宏观数据可视化

### 2.1 默认行为

- **数据驱动**：指标名称/单位/频率从数据库动态读取，新增指标无需改本模块代码。
- 与采集模块解耦，仅依赖数据表（数据契约）。
- 默认表 `macro_data`（中国）；美国及商品需显式 `--table macro_us_data`。
- `--country` 默认随 `--table`：macro_data→CN，macro_us_data→US。
- **`GDP` 在两表均存在**，含义不同，必须用 `--table` 区分。

### 2.2 CLI 参数总览

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `--indicator` | string | 是 | — | 指标英文简称，多个逗号分隔（**最多 3 个**）如 `CPI` / `CPI,PPI` |
| `--table` | `macro_data` / `macro_us_data` | 否 | `macro_data` | 数据表 |
| `--country` | string | 否 | 随 table | 国家代码 CN / US |
| `--start-date` | string (YYYY-MM-DD) | 否 | 最早记录 | 起始日期 |
| `--end-date` | string (YYYY-MM-DD) | 否 | 今天 | 结束日期 |
| `--limit` | int | 否 | 全部 | 最近 N 期（**优先级低于日期范围**） |
| `--chart-type` | string | 否 | `line` | 图表类型（暂只支持 line） |
| `--output` | `json` / `html` | 否 | `json` | 输出格式 |
| `--out` | string | 否 | 见说明 | 输出路径；json 默认终端，html 默认 `data/reports/charts/{indicator}.html` |

> 日期范围优先级：`--start-date` / `--end-date` 任一提供即进入日期范围模式，`--limit` 被忽略。

### 2.3 常用命令样例

```bash
cd trading_lab

# —— JSON 数据（默认输出到终端）——
uv run services/macro-charts/main.py --indicator CPI                            # 默认 macro_data 表，全量
uv run services/macro-charts/main.py --indicator CPI --limit 24                  # 最近 24 期
uv run services/macro-charts/main.py --indicator CPI --start-date 2020-01-01 --limit 60   # 带日期范围（limit 被忽略）
uv run services/macro-charts/main.py --indicator CPI --start-date 2024-01-01 --end-date 2026-08-08  # 双边日期范围

# —— Plotly HTML 文件（默认写到 data/reports/charts/{indicator}.html）——
uv run services/macro-charts/main.py --indicator CPI --output html
uv run services/macro-charts/main.py --indicator M2 --output html --out data/reports/charts/M2.html  # 指定输出路径

# —— 多指标对比（按单位自动分配 Y 轴，最多 3 轴）——
uv run services/macro-charts/main.py --indicator CPI,PPI --output html                       # 同单位(%)共享单轴
uv run services/macro-charts/main.py --indicator CPI,M2 --output html                        # 双轴(% vs 亿元)
uv run services/macro-charts/main.py --table macro_us_data --indicator GOLD,SILVER --output html    # 双轴(元/克 vs 元/千克)
uv run services/macro-charts/main.py --table macro_us_data --indicator DGS10,GOLD,DTWEXBGS --output html  # 三轴(% vs 元/克 vs 指数)

# —— 中国宏观（macro_data 表，country 默认 CN）——
uv run services/macro-charts/main.py --indicator CPI                            # 居民消费价格指数
uv run services/macro-charts/main.py --indicator CPI,PPI                        # CPI vs PPI 双轴
uv run services/macro-charts/main.py --indicator GDP                            # 中国 GDP（同比%）
uv run services/macro-charts/main.py --indicator M2                             # M2 货币供应量（亿元）
uv run services/macro-charts/main.py --indicator LPR1Y,LPR5Y --output html      # LPR 1Y vs 5Y
uv run services/macro-charts/main.py --indicator PMI --start-date 2023-01-01    # 制造业 PMI 带过滤

# —— 美国及商品（macro_us_data 表，country 默认 US）——
uv run services/macro-charts/main.py --table macro_us_data --indicator UNRATE                          # 失业率
uv run services/macro-charts/main.py --table macro_us_data --indicator CPIAUCSL,UNRATE                 # CPI vs 失业率双轴
uv run services/macro-charts/main.py --table macro_us_data --indicator GDP                             # 美国名义 GDP（十亿美元）
uv run services/macro-charts/main.py --table macro_us_data --indicator DGS1,DGS10 --output html        # 国债收益率曲线对比
uv run services/macro-charts/main.py --table macro_us_data --indicator DGS10 --output html             # 10 年期国债收益率
uv run services/macro-charts/main.py --table macro_us_data --indicator GOLD,SILVER --output html       # 黄金白银现货对比
uv run services/macro-charts/main.py --table macro_us_data --indicator GOLD_INV,SILVER_INV             # COMEX 库存对比

# —— 组合用法 ——
uv run services/macro-charts/main.py --indicator CPI --start-date 2020-01-01 --limit 60              # 带过滤的中国 CPI
uv run services/macro-charts/main.py --table macro_us_data --indicator CPIAUCSL,UNRATE --output html --out data/reports/charts/us_cpi_unrate.html
uv run services/macro-charts/main.py --indicator M2 --output json --out data/reports/charts/M2.json   # JSON 输出到文件
uv run services/macro-charts/main.py --indicator CPI,PPI,M2                                          # 三指标对比（同/异单位均可）
```

### 2.4 多 Y 轴分配规则

| 指标组合 | 单位分布 | Y 轴数 | 说明 |
|----------|----------|--------|------|
| `CPI,PPI` | % / % | 1 | 同单位共享单轴 |
| `CPI,M2` | % / 亿元 | 2 | 异单位独立轴 |
| `GOLD,SILVER` | 元/克 / 元/千克 | 2 | 单位不同即独立轴 |
| `DGS10,GOLD,DTWEXBGS` | % / 元/克 / 指数 | 3 | 三种单位走满三轴（上限） |

每个 Y 轴的标题与刻度颜色与对应曲线一致（Viridis 色盲友好色板）。

### 2.5 查询当前可用指标

```bash
sqlite3 data/trading.db "SELECT DISTINCT indicator,country FROM macro_data UNION SELECT DISTINCT indicator,country FROM macro_us_data;"
```

---

## 三、采集 + 可视化联动示例

```bash
cd trading_lab

# 1) 抓取中国 CPI/PPI/M2 最近 36 期
uv run services/macro-data-service/main.py --indicator cpi_cn,ppi_cn,m2_cn --recent 36

# 2) 画 CPI vs PPI 双轴对比
uv run services/macro-charts/main.py --indicator CPI,PPI --output html

# 3) 画 M2 单指标
uv run services/macro-charts/main.py --indicator M2 --output html

# 4) 抓取美国 10Y 国债 + 黄金 + 美元指数，画三轴对比
uv run services/macro-data-service/main.py --indicator fred_dgs10,sge_gold,fred_dtwexbgs --recent 60
uv run services/macro-charts/main.py --table macro_us_data --indicator DGS10,GOLD,DTWEXBGS --output html
```

---

## 四、完整文档

每个模块的详细文档（数据模型、指标清单、扩展指南、错误码、测试）见各自 README：

- [macro-data-service/README.md](macro-data-service/README.md)
- [macro-charts/README.md](macro-charts/README.md)
