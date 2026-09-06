# yanbao-info — 券商研报摘要与短线信号提取

输入股票代码，自动获取最近发布的券商研报，下载 PDF 并解析，通过关键词+正则规则提取评级/目标价/盈利预测/催化剂/风险等结构化字段，结合一致预期与当前股价产出"短线博弈信号"判断，输出 JSON + Markdown 摘要报告（跑完即退）。

***

## 一、常用命令

所有命令均在项目根目录 `trading_lab/` 下通过 `uv run` 执行。

### 1.1 基础用法

```bash
# 默认：取最近 1 份研报，输出 JSON + Markdown
uv run services/yanbao-info/main.py --symbol 600036

# 取最近 3 份研报
uv run services/yanbao-info/main.py --symbol 600036 --top-n 3

# 仅输出 JSON
uv run services/yanbao-info/main.py --symbol 600036 --format json

# 仅输出 Markdown
uv run services/yanbao-info/main.py --symbol 600036 --format md

# 跳过 PDF 解析（仅用研报列表元数据，最快）
uv run services/yanbao-info/main.py --symbol 600036 --no-pdf

# 自定义输出路径（按 --format 自动派生 .json/.md 后缀）
uv run services/yanbao-info/main.py --symbol 600036 --out data/yanbao/reports/cmb

# 安静模式（不打印控制台摘要）
uv run services/yanbao-info/main.py --symbol 600036 --quiet

# 详细日志（INFO 级别）
uv run services/yanbao-info/main.py --symbol 600036 --verbose
```

### 1.2 单元测试

```bash
# 运行全部测试（覆盖 extractor / signal_judge / pdf_parser / data_loader 降级路径）
uv run pytest services/yanbao-info/test_yanbao.py -v

# 仅运行某一类测试
uv run pytest services/yanbao-info/test_yanbao.py -v -k "extract_rating"
uv run pytest services/yanbao-info/test_yanbao.py -v -k "judge_target_price"
```

### 1.3 退出码

| 退出码 | 含义                     |
| --- | ---------------------- |
| 0   | 成功（含"未找到研报"场景，视为业务正常态） |
| 1   | 失败（未捕获异常 / CLI 参数错误）   |

***

## 二、CLI 参数详解

| 参数          | 类型   | 默认值    | 说明                                                                                |
| ----------- | ---- | ------ | --------------------------------------------------------------------------------- |
| `--symbol`  | str  | **必填** | 股票代码（如 `600036` / `000001` / `300750`）                                            |
| `--top-n`   | int  | 1      | 取最近 N 份研报处理（第一份作为主报告，其余追加到历史快照）                                                   |
| `--format`  | enum | `both` | 输出格式：`json` / `md` / `both`                                                       |
| `--out`     | path | None   | 输出路径；不指定时默认 `data/yanbao/reports/{symbol}_{date}.json/.md`。指定后按 `--format` 自动派生后缀 |
| `--no-pdf`  | flag | False  | 跳过 PDF 下载与解析，仅用研报列表元数据（最快路径）                                                      |
| `--quiet`   | flag | False  | 不打印控制台摘要                                                                          |
| `--verbose` | flag | False  | 详细日志（INFO 级别，默认 WARNING）                                                          |

***

## 三、配置参数（config.py `YanbaoConfig`）

所有可调参数集中于 [config.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/yanbao-info/config.py)，`main.py` 不出现硬编码数值。

### 3.1 取数参数

| 参数                 | 默认值 | 说明                   |
| ------------------ | --- | -------------------- |
| `top_n`            | 1   | 取最近 N 份研报            |
| `request_interval` | 2.0 | 限频间隔（秒），相邻请求间隔 ≥ 此值  |
| `request_timeout`  | 30  | PDF 下载与单次接口请求超时（秒）   |
| `retry_times`      | 2   | 失败重试次数（不含首次；仅网络异常重试） |

### 3.2 PDF 解析参数

| 参数                   | 默认值 | 说明                  |
| -------------------- | --- | ------------------- |
| `pdf_max_pages`      | 50  | 最多解析页数（防超长报告耗时）     |
| `pdf_text_min_chars` | 100 | 文本字符数 < 此值视为扫描版 PDF |

### 3.3 PDF 下载请求头（防东方财富防盗链）

| 参数               | 默认值                           | 说明                    |
| ---------------- | ----------------------------- | --------------------- |
| `pdf_user_agent` | Chrome 120 UA                 | 下载 PDF 使用的 User-Agent |
| `pdf_referer`    | `https://data.eastmoney.com/` | Referer 头             |

### 3.4 短线信号阈值

| 参数                             | 默认值  | 说明                               |
| ------------------------------ | ---- | -------------------------------- |
| `target_price_upside_strong`   | 0.30 | 目标价隐含涨幅 ≥30% 视为**强信号**           |
| `target_price_upside_moderate` | 0.15 | 15%-30% 视为**中信号**；<15% 视为**弱信号** |
| `catalyst_recent_days`         | 30   | 催化剂事件窗口期（天），近期落地视为有效             |
| `forecast_revision_threshold`  | 0.05 | 研报预测值高于一致预期 5% 视为**上调**          |
| `history_keep_count`           | 20   | 历史快照保留份数（超出自动淘汰最旧）               |

### 3.5 评级词汇（用于提取与跳升判断）

按优先级排序，文本中靠前的词汇胜出：

| 字段                    | 默认词汇                      |
| --------------------- | ------------------------- |
| `rating_buy`          | 买入、强推、强烈推荐、Strong Buy、Buy |
| `rating_outperform`   | 增持、推荐、优于大市、Outperform、Add |
| `rating_neutral`      | 中性、持有、同步大市、Neutral、Hold   |
| `rating_underperform` | 减持、Underperform、Reduce    |
| `rating_sell`         | 卖出、回避、Sell                |

### 3.6 一致预期接口与数据源

| 参数                    | 默认值                              | 说明                                      |
| --------------------- | -------------------------------- | --------------------------------------- |
| `consensus_indicator` | `预测年报净利润`                        | akshare `stock_profit_forecast_ths` 指标名 |
| `tencent_quote_url`   | `https://qt.gtimg.cn/q={symbol}` | 腾讯实时行情接口（当前股价）                          |

### 3.7 派生路径

| 方法                 | 路径                     | 说明                      |
| ------------------ | ---------------------- | ----------------------- |
| `yanbao_root()`    | `data/yanbao/`         | 根目录                     |
| `pdfs_dir()`       | `data/yanbao/pdfs/`    | PDF 原文缓存                |
| `cache_dir_path()` | `data/yanbao/cache/`   | 研报列表原始响应缓存              |
| `history_dir()`    | `data/yanbao/history/` | 历史评级快照（用于评级跳升判断）        |
| `reports_dir()`    | `data/yanbao/reports/` | 最终摘要报告（JSON + Markdown） |

***

## 四、核心文件职责

| 文件                                                                                                                          | 职责                                                                                                                                                                     |
| --------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [main.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/yanbao-info/main.py)                  | 入口：CLI 参数解析、流程编排（取列表 → 取股价/一致预期 → 处理研报 → 落盘 + 控制台摘要）                                                                                                                   |
| [config.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/yanbao-info/config.py)              | 配置中心：所有阈值/URL/UA/超时/路径（`YanbaoConfig.from_env`）                                                                                                                        |
| [data\_loader.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/yanbao-info/data_loader.py)   | 数据获取层：`fetch_report_list`（akshare `stock_research_report_em`）、`download_pdf`、`fetch_current_price`、`fetch_consensus_forecast`、`load_history` / `save_history_snapshot` |
| [pdf\_parser.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/yanbao-info/pdf_parser.py)     | PDF 解析：`extract_text_and_tables`（pdfplumber，扫描版检测）                                                                                                                     |
| [extractor.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/yanbao-info/extractor.py)        | 信息提取：评级/目标价/盈利预测/催化剂/风险/首次覆盖（关键词+正则）                                                                                                                                   |
| [signal\_judge.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/yanbao-info/signal_judge.py) | 短线信号判断：5 个独立判断函数 + `judge_signals` 聚合入口                                                                                                                                |
| [reporter.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/yanbao-info/reporter.py)          | 输出层：`build_report` / `build_no_report_found`、`save_json` / `save_markdown`、`print_console_summary`                                                                     |
| [test\_yanbao.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/yanbao-info/test_yanbao.py)   | 单元测试：extractor / signal\_judge / pdf\_parser / data\_loader 降级路径（网络层用 mock）                                                                                            |

***

## 五、短线信号判断逻辑（signal\_judge.py）

5 个独立判断函数，均返回 `dict`（含 `reason` 字段说明判断依据）：

| 函数                            | 判断维度                              | 输出 signal 取值        |
| ----------------------------- | --------------------------------- | ------------------- |
| `judge_target_price_upside`   | (目标价 - 当前股价) / 当前股价               | 强 / 中 / 弱 / 无法判断    |
| `judge_rating_jump`           | 同机构历史评级对比                         | 跳升 / 持平 / 下降 / 无法判断 |
| `judge_forecast_revision`     | 研报预测值 vs 一致预期                     | 上调 / 持平 / 下调 / 无法判断 |
| `judge_catalyst_timeliness`   | 催化剂是否在 `catalyst_recent_days` 内落地 | 近期 / 远期 / 无催化剂      |
| `judge_first_coverage_signal` | 是否为该机构首次覆盖                        | 是 / 否               |

***

## 六、数据落盘约定

```
data/yanbao/
├── pdfs/      PDF 原文缓存（按 symbol + 发布日期 + 机构命名）
├── cache/     研报列表原始响应缓存（便于复盘）
├── history/   历史评级快照（用于评级跳升判断，超出 history_keep_count 自动淘汰）
└── reports/   最终摘要报告（{symbol}_{date}.json / .md）
```

***

## 七、依赖

详见 [pyproject.toml](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/yanbao-info/pyproject.toml)：

- `pdfplumber>=0.10`（PDF 解析）

- `akshare>=1.12`（研报列表 / 一致预期）

- `requests>=2.31`（PDF 下载）

- `pandas>=2.0`

- Python ≥ 3.11

