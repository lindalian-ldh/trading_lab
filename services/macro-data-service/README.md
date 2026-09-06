# macro-data-service 宏观数据获取模块

从 akshare 与 FRED（圣路易斯联储）获取中国及美国主要宏观经济指标，标准化为统一 JSON 模型后本地持久化（SQLite / Parquet）。具备高扩展性、可追溯性，盘后批量执行。

## 数据模型

每条记录字段（`models.MacroRecord`）：

| 字段 | 说明 |
|------|------|
| indicator | 指标英文简称（CPI / PPI / GDP / UNRATE / CPIAUCSL 等） |
| name | 指标中文全称 |
| country | 国家代码（CN / US，ISO 3166-1 alpha-2） |
| frequency | 频率 D/W/M/Q/Y |
| date | 数据日期 YYYY-MM-DD |
| value | 数值（float） |
| unit | 单位（% / 指数 / 亿元 / 十亿美元 / 吨 等） |
| source | 数据来源（akshare / FRED） |
| source_url | 可追溯标识 `akshare:{func}@{version}` / `FRED:{series_id}` |
| fetch_time | 获取时间 |
| extra | 扩展字段（如预测值/前值、库存增减） |

**唯一键**：`indicator + date + country`。

## 采集指标清单

数据按国家分存两张表：

- **macro_data**：中国宏观指标（country=CN），来源 akshare
- **macro_us_data**：美国宏观及商品指标（country=US），来源 FRED / akshare

### 中国宏观（macro_data 表，country=CN）

| indicator | 名称 | 频率 | 单位 | akshare 函数 |
|-----------|------|------|------|--------------|
| CPI | 居民消费价格指数 | M | % | macro_china_cpi |
| PPI | 工业生产者出厂价格指数 | M | % | macro_china_ppi |
| GDP | 国内生产总值（同比） | Q | % | macro_china_gdp |
| PMI | 制造业PMI | M | 指数 | macro_china_pmi |
| M2 | M2货币供应量 | M | 亿元 | macro_china_money_supply |
| LPR1Y | LPR-1年期 | M | % | macro_china_lpr |
| LPR5Y | LPR-5年期 | M | % | macro_china_lpr |
| SF | 社会融资规模增量 | M | 亿元 | macro_china_shrzgm |
| IP | 工业增加值 | M | % | macro_china_gyzjz |
| UE | 城镇调查失业率 | M | % | macro_china_urban_unemployment |

### 美国及商品（macro_us_data 表，country=US）

| indicator | 名称 | 频率 | 单位 | 来源 | FRED series_id / akshare 函数 |
|-----------|------|------|------|------|--------------------------------|
| GDP | 名义GDP | Q | 十亿美元 | FRED | GDP |
| GDPC1 | 实际GDP | Q | 十亿美元 | FRED | GDPC1 |
| CPIAUCSL | 消费者价格指数(CPI) | M | 指数 | FRED | CPIAUCSL |
| CPILFESL | 核心CPI | M | 指数 | FRED | CPILFESL |
| PPIACO | 生产者价格指数(PPI) | M | 指数 | FRED | PPIACO |
| UNRATE | 失业率 | M | % | FRED | UNRATE |
| PAYEMS | 非农就业人数 | M | 千人 | FRED | PAYEMS |
| ICSA | 初请失业金人数 | W | 人 | FRED | ICSA |
| DGS6MO | 6个月期国债收益率 | D | % | FRED | DGS6MO |
| DGS1 | 1年期国债收益率 | D | % | FRED | DGS1 |
| DGS10 | 10年期国债收益率 | D | % | FRED | DGS10 |
| GS10 | 10年期国债收益率(月度) | M | % | FRED | GS10 |
| DTWEXBGS | 名义广义美元指数 | D | 指数 | FRED | DTWEXBGS |
| DTWEXM | 名义主要货币美元指数(已停止) | D | 指数 | FRED | DTWEXM |
| DTWEXO | 名义其他重要贸易伙伴美元指数(已停止) | D | 指数 | FRED | DTWEXO |
| RTWEXBGS | 实际广义美元指数 | M | 指数 | FRED | RTWEXBGS |
| GOLD | 黄金现货(上海金基准价) | D | 元/克 | akshare | spot_golden_benchmark_sge |
| SILVER | 白银现货(上海银基准价) | D | 元/千克 | akshare | spot_silver_benchmark_sge |
| USA_PHS | 未决房屋销售月率 | M | % | akshare | macro_usa_phs |
| GOLD_INV | 黄金库存(COMEX) | D | 吨 | akshare | macro_cons_gold |
| SILVER_INV | 白银库存(COMEX) | D | 吨 | akshare | macro_cons_silver |

> **FRED 指标**：indicator 直接使用 FRED series_id（如 UNRATE、CPIAUCSL、DGS10），需在 `.env` 配置 `FRED_API_KEY`（免费申请：https://fred.stlouisfed.org），通过 `pandas-datareader` 获取。
>
> **贵金属现货 vs 库存**：`GOLD`/`SILVER` 为上海黄金交易所(SGE)基准价（价格，CNY 计价，取晚盘价），`GOLD_INV`/`SILVER_INV` 为 COMEX 库存量（吨）——前者反映行情，后者反映仓单变化，二者互补。原 sina 伦敦金/银(hf_XAU/hf_XAG)与 FRED 伦敦金定盘价(GOLDPMGBD228NLBM，已被 FRED API 永久下线)均不可用，故改用 SGE 基准价。
>
> **重名提示**：`GDP` 在两表均存在——中国 `GDP`（macro_data, CN）为同比%，美国 `GDP`（macro_us_data, US）为名义GDP十亿美元。查询/画图时务必按表区分。

## 运行

```bash
cd trading_lab
uv sync                                                          # 首次：安装含 akshare/pyarrow/pyyaml/pandas-datareader 的依赖
uv run services/macro-data-service/main.py                       # 全部启用指标
uv run services/macro-data-service/main.py --indicator cpi_cn        # 单个中国指标
uv run services/macro-data-service/main.py --indicator fred_unrate   # 单个 FRED 指标
uv run services/macro-data-service/main.py --indicator sge_gold,sge_silver  # 多指标(逗号分隔)
uv run services/macro-data-service/main.py --storage parquet     # Parquet 存储
uv run services/macro-data-service/main.py --mode overwrite      # 全量覆盖
uv run services/macro-data-service/main.py --recent 12           # 仅最近 12 期
```

CLI 参数覆盖 `config.yaml` 同名配置。

## 切换存储方式

两种途径（任选其一）：

1. 改 `config.yaml`：`storage.type: sqlite | parquet`
2. 命令行：`--storage sqlite|parquet`

- **SQLite**：中国指标存 `macro_data` 表、美国及商品指标存 `macro_us_data` 表，主键 `(indicator, date, country)` 天然去重。文件 `data/trading.db`。
- **Parquet**：中国指标按 `indicator/year` 分区存 `data/macro/`，美国指标存 `data/macro_us/`，列式高效压缩。

写入模式：`incremental`（默认，增量去重）/ `overwrite`（全量覆盖，先删除该指标旧行再插入，用于数据修正）。

## 新增指标

只需两步，无需改动既有代码：

**akshare 来源**（中国或商品指标）：

1. 在 `fetchers.py` 新增函数，调用通用流程 `_fetch_series(...)`：

   ```python
   def get_fx_reserve_cn(recent_periods: int = 0) -> list[dict]:
       return _fetch_series(
           indicator="FX_RESERVE", name="外汇储备", country="CN", frequency="M",
           unit="万亿美元", ak_func="macro_china_fx_reserves",
           date_cols=["月份"], value_cols=["外汇储备", "数值"],
           recent_periods=recent_periods,
       )
   ```

2. 在 `REGISTRY` 登记键名：`"fx_reserve_cn": get_fx_reserve_cn`，并在 `config.yaml` 的 `indicators` 下加 `fx_reserve_cn: { enabled: true }`。

**FRED 来源**（美国宏观指标）：

1. 在 `fetchers.py` 新增函数，调用 `_fetch_fred_series(...)`：

   ```python
   def get_fred_fedfunds(recent_periods: int = 0) -> list[dict]:
       return _fetch_fred_series(
           series_id="FEDFUNDS", indicator="FEDFUNDS", name="联邦基金利率",
           frequency="M", unit="%", recent_periods=recent_periods,
       )
   ```

2. 在 `REGISTRY` 登记并在 `US_INDICATORS` 集合添加 `"fred_fedfunds"`（用于路由到 `macro_us_data` 表）；在 `config.yaml` 加开关。确保 `.env` 已配置 `FRED_API_KEY`。

> 若 akshare 返回列名与候选不符，调整该函数的 `value_cols`/`date_cols` 候选列表即可（`_fetch_series` 会自动回退到第一个可转数值的列）。

多值指标（如 LPR 1Y/5Y）参考 `get_lpr_cn` 自行拆分为不同 indicator，以维持唯一键约束。

## 数据溯源日志

每次获取写入 JSON Lines 到 `logs/macrodata-fetch.log`：

```json
{"time":"2026-08-08 14:30:00","func":"get_cpi_cn","source":"akshare:macro_china_cpi","records":120,"status":"success"}
```

异常时追加 `error` 字段，`status` 为 `error`/`empty`/`no_value_column`。

## 容错

- 网络异常重试 `fetch.retry` 次（默认 3），间隔 `retry_delay` 秒。
- 单指标失败不影响其他指标：捕获异常、记录溯源日志后返回 `[]`，批量继续。
- 整体执行 `signal.alarm(300)` 防卡死。
- FRED 指标在未配置 `FRED_API_KEY` 时返回 `[]` 并记日志，不中断批量。

## 测试

```bash
uv run pytest tests/test_macro_service.py
```

单测 mock `fetchers._call_akshare` / `fetchers._call_fred` 返回构造 DataFrame，验证归一化与存储去重，不依赖真实网络。

## 接入每日定时

本服务遵循「跑完即退」，可加入 `scripts/run_all.py` 的 `PIPELINE`，或单独配置 cron：

```cron
30 17 * * * cd /绝对路径/trading_lab && uv run services/macro-data-service/main.py >> logs/macro_cron.log 2>&1
```

## 常用命令

```bash
# 查看中国指标
sqlite3 -header -column data/trading.db "SELECT indicator,date,value,unit FROM macro_data ORDER BY indicator;"

# 查看美国及商品指标
sqlite3 -header -column data/trading.db "SELECT indicator,date,value,unit,source FROM macro_us_data ORDER BY indicator;"

# 交互式查看
sqlite3 data/trading.db        # 进入交互
.tables                        # 列出表
.schema macro_us_data          # 看表结构
SELECT * FROM macro_us_data;   # 查数据
.quit

# 单个 FRED 指标
uv run services/macro-data-service/main.py --indicator fred_unrate
# 拉取历史序列（全量覆盖）
uv run services/macro-data-service/main.py --recent 60 --mode overwrite

# 用 macro-charts 画图（中国指标默认 macro_data 表）
uv run services/macro-charts/main.py --indicator CPI,PPI --output html                        # 中国双轴对比
uv run services/macro-charts/main.py --indicator CPI --start-date 2020-01-01 --limit 60       # 中国带过滤
# 美国指标需指定 --table macro_us_data（country 默认 US）
uv run services/macro-charts/main.py --table macro_us_data --indicator UNRATE --output html    # 美国单指标
uv run services/macro-charts/main.py --table macro_us_data --indicator CPIAUCSL,UNRATE        # 美国双轴对比
```
