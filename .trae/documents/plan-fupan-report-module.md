# 实施计划：新增 services/fupan-report 资金流水多维度复盘模块

## Summary

在 `services/fupan-report/` 下新增一个独立复盘模块，读取招商证券导出的历史资金流水 `.xls/.xlsx` 文件，按 **成本 / 绩效 / 择时 / 仓位 / 纪律** 五大维度自动化量化复盘，在控制台打印格式化报告并落盘到 `data/reports/`。

遵循项目现有 services 约定：`main.py` 入口 + 独立 `pyproject.toml` + `README.md` + `config.py` 配置驱动。

## Current State Analysis（探索结论）

基于对项目架构的探索：

- **模块组织**：`services/` 下每个服务是一个独立目录，标准结构为 `main.py`(入口) + `pyproject.toml`(独立依赖，`[tool.uv] package = false`) + `README.md`(文档) + 可选 `config.py`/`data_loader.py`/`reporter.py`/测试文件。参考 [services/generate_report/main.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/generate_report/main.py)、[services/alarming_monitor/](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor)。
- **入口约定**：所有服务以 `main.py` 为入口，用 `argparse` 解析参数，通过 `uv run services/xxx/main.py` 或 `python services/xxx/main.py` 调用。
- **配置驱动**：全局 [config/settings.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/config/settings.py) 派生 `reports_dir` 等路径；各模块自带 `config.py` 放阈值（见 [services/alarming_monitor/config.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/config.py)）。所有阈值须可配置，禁止硬编码（项目硬约束）。
- **报告落盘约定**：现有 [generate_report/main.py#L88-L91](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/generate_report/main.py#L88-L91) 把报告写到 `data/reports/report_{date}.txt` 并 print。
- **日志**：项目用 `core.logger.get_logger`（缺失时降级 logging），见 [generate_report/main.py#L18](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/generate_report/main.py#L18)。
- **服务编排**：[scripts/run_all.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/scripts/run_all.py) 的 `SERVICES` 注册表编排各服务。本模块依赖用户本地资金流水文件输入（`-f` 参数），不适合自动场景编排，**不注册**到 run_all.py，作为按需手动调用的独立工具。
- **无既有 xls 处理代码**：项目未处理过 xls/xlsx 资金流水文件，需新引入 `xlrd`/`openpyxl` 读取。

## Assumptions & Decisions（假设与决策）

1. **输出格式**：控制台打印格式化文本 + 落盘 `data/reports/fupan_{date}.txt`（与现有 generate_report 约定一致）。需求标题的"HTML"理解为泛指输出形式，交付物示例仅详述控制台格式，故**不生成 HTML 文件**，避免过度工程化。
2. **脚本命名**：遵循项目约定用 `main.py` 入口（而非字面的 `review.py`）。README 给出命令 `python services/fupan-report/main.py -f 历史资金流水.xls`，等价于需求中的 `python review.py -f 历史资金流水.xls`。
3. **不注册 run_all.py**：模块依赖用户本地文件输入，按需手动调用。
4. **依赖**：仅 `pandas`、`numpy`、`openpyxl`（读 xlsx）、`xlrd`（读老 xls）。不引入 jinja2 等额外模板库，报告用 Python 字符串拼接（参考 generate_report 也用 Template，但本模块格式更表格化，直接拼接更清晰）。
5. **日期范围**：报告标题的复盘区间取数据中最早与最晚成交日期。
6. **胜率估算口径**：按"成交价格 > 该标的加权平均买入价"判定单笔卖出是否盈利，统计占比。加权平均买入价在该标的所有买入记录上按数量加权。
7. **重仓市值估算**：按"剩余数量 × 该标的最新一笔成交价"估算（需求明确此口径）。
8. **配置项**：佣金率预警阈值(5‱)、Top榜数量(盈利3/亏损按需求取Top拖累3)、单笔佣金前N(5)、前三大重仓(3)、现金理财过滤关键词均放 `config.py`。

## Proposed Changes（拟新增文件清单）

新增目录 `services/fupan-report/`，共 7 个文件。不修改任何现有文件。

### 1. `services/fupan-report/__init__.py`
空文件，标记为 Python 包（与 alarming_monitor 一致）。

### 2. `services/fupan-report/config.py`
**what**: 配置中心，所有阈值与常量。
**why**: 满足项目硬约束"所有阈值必须可配置，禁止硬编码"。
**how**: 定义模块级常量：
- `REQUIRED_COLUMNS`: 必需列名列表（证券代码/证券名称/成交日期/成交价格/成交数量/发生金额/剩余数量/佣金/印花税/经手费/证管费/过户费）。用于 data_loader 校验。
- `CASH_PRODUCT_KEYWORDS`: `["天添利", "产品申购", "产品赎回"]`，过滤现金理财记录。
- `COMMISSION_RATE_WARN_BPS = 5`：佣金率预警阈值(‱)。
- `TOP_N_PROFIT = 3`、`TOP_N_LOSS = 3`：盈利榜/亏损榜数量。
- `TOP_N_HIGH_COMMISSION = 5`：单笔佣金最高前N笔。
- `TOP_N_HOLDINGS = 3`：前三大重仓。
- `OUTPUT_FILENAME_PREFIX = "fupan_"`：落盘文件名前缀。
- 规费合并说明：经手费/证管费/过户费统一计入"规费"合计。

### 3. `services/fupan-report/data_loader.py`
**what**: 数据读取与清洗层。
**why**: 鲁棒性需求——缺列明确报错、自动剔除现金理财、类型归一。
**how**:
- `load_flow(file_path: str) -> pd.DataFrame`：
  - 按扩展名分流：`.xls` 用 `xlrd` 引擎，`.xlsx` 用 `openpyxl`。
  - `pd.read_excel(...)`。
  - 列存在性校验：遍历 `REQUIRED_COLUMNS`，缺失则 `raise ValueError(f"缺少关键列：{col}")`。
  - 类型转换：`成交日期`（int 如 20260908）→ `pd.to_datetime(format="%Y%m%d")`；`成交价格/成交数量/发生金额/剩余数量/佣金/印花税/经手费/证管费/过户费` → `float`。
  - 过滤：剔除 `证券名称` 包含 `CASH_PRODUCT_KEYWORDS` 任一关键词的记录（`.str.contains("|".join(...), na=False)` 取反）。
  - 返回清洗后 DataFrame，附 `__cleaned_count__`（剔除条数）用于报告提示。

### 4. `services/fupan-report/analyzers.py`
**what**: 五大维度分析器，纯函数无 IO。
**why**: 每个维度独立可测、可复用，符合 alarming_monitor 的 indicators.py 纯函数分层约定。
**how**: 每个函数接收清洗后 DataFrame，返回 dict：

- `analyze_cost(df) -> dict`
  - `total_turnover = df["发生金额"].abs().sum()`
  - `total_commission`、`total_stamp_tax`、`total_fees`（规费=经手费+证管费+过户费之和，三者缺失列按0计）
  - `commission_rate_bps = total_commission / total_turnover * 10000`
  - `warn = commission_rate_bps > COMMISSION_RATE_WARN_BPS`
  - `top_commission_trades`: 单笔佣金降序前5，含日期/证券名称/佣金/买卖方向
- `analyze_performance(df) -> dict`
  - 按 `证券名称` 分组：`buy_cost = 发生金额[成交数量>0].abs().sum()`（买入支出）、`sell_income = 发生金额[成交数量<0].abs().sum()`（卖出收入）。注意发生金额买入为负、卖出为正，故 `buy_cost = -发生金额[买入].sum()`，`sell_income = 发生金额[卖出].sum()`。
  - `realized_pnl = sell_income - buy_cost`
  - `trade_count = 分组size`
  - 盈利榜（realized_pnl 降序 Top3）、亏损榜（realized_pnl 升序 Top3）
- `analyze_timing(df) -> dict`
  - 仅对同时有买卖的标的计算：
  - `avg_buy_price = Σ(成交价格*成交数量)[买入] / Σ成交数量[买入]`
  - `avg_sell_price = Σ(成交价格*|成交数量|)[卖出] / Σ|成交数量|[卖出]`
  - `spread_rate = (avg_sell - avg_buy) / avg_buy * 100`
  - 判定：正→"高抛低吸有效"，负→"低抛高吸，操作反向"
  - 返回每个标的明细列表
- `analyze_capital(df) -> dict`
  - `daily_net_flow`: 按成交日期汇总 `发生金额.sum()`（正=净卖出回笼，负=净买入加仓）
  - 重仓：取每个标的最后一条记录的 `剩余数量`，× 该标的最新成交价，降序取前3
  - `daily_avg_buy = df[成交数量>0][发生金额].abs().sum() / 交易日天数`
- `analyze_discipline(df) -> dict`
  - `buy_count = (成交数量>0).sum()`、`sell_count = (成交数量<0).sum()`、`sell_buy_ratio = sell_count/buy_count`
  - 胜率：对每笔卖出，查该标的加权平均买入价（同 timing 口径），`成交价格 > avg_buy_price` 计为盈利卖出，统计占比 `win_rate`
  - `gross_pnl = sell_income_total - buy_cost_total`（扣费前）
  - `net_pnl = gross_pnl - total_commission - total_stamp_tax - total_fees`（扣费后）
  - 结论：`net_pnl>=0 → "盈利，建议维持/适度交易频率"`；`net_pnl<0 → "亏损，建议减少交易频率、关注佣金侵蚀"`

### 5. `services/fupan-report/reporter.py`
**what**: 报告渲染与落盘。
**why**: 输出格式与业务计算解耦，便于后续二次开发（如增加可视化图表）。
**how**:
- `render_report(cost, perf, timing, capital, discipline, date_range, cleaned_count) -> str`：按需求第三部分格式拼接文本（含五大模块标题、数值千分位格式化、预警⚠️标记、结论行）。
- `format_money(x) -> str`：千分位 + 两位小数（如 `123,456.78`）。
- `save_report(text: str, date_str: str) -> Path`：写入 `settings.reports_dir / f"fupan_{date_str}.txt"`，目录不存在则 mkdir。

### 6. `services/fupan-report/main.py`
**what**: 入口，编排全流程。
**why**: 遵循项目 `main.py` 入口约定。
**how**:
- `argparse`：`-f/--file`（必填，资金流水文件路径）。
- 流程：`load_flow` → 依次调用五大 analyzer → `render_report` → `print` + `save_report`。
- 日志用 `core.logger.get_logger("fupan_report")`（try/except 降级 logging，兼容无 core 环境）。
- 日期范围：从清洗后 df 取 `成交日期.min()/max()`。
- 异常：缺列报错清晰打印；文件不存在报错。
- `return 0` 成功 / `1` 失败，`if __name__ == "__main__": sys.exit(main())`。

### 7. `services/fupan-report/pyproject.toml`
**what**: 独立依赖声明。
**how**: 参考 [services/generate_report/pyproject.toml](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/generate_report/pyproject.toml) 格式：
```toml
[project]
name = "trading-lab-fupan-report"
version = "0.1.0"
description = "招商证券资金流水多维度量化复盘（成本/绩效/择时/仓位/纪律）"
requires-python = ">=3.11"
dependencies = ["pandas>=2.0", "numpy>=1.26", "openpyxl>=3.1", "xlrd>=2.0"]

[tool.uv]
package = false
```

### 8. `services/fupan-report/README.md`
**what**: 使用文档（验收标准要求）。
**how**: 包含：
- 功能简介与五大维度说明
- 依赖安装（`uv sync` 或 `pip install pandas numpy openpyxl xlrd`）
- **常用命令**：
  - `python services/fupan-report/main.py -f 历史资金流水.xls`
  - `uv run services/fupan-report/main.py -f 历史资金流水.xls`
- 输入列规范表（与需求 Schema 一致）
- 输出示例（截取控制台报告片段）
- 二次开发指引（analyzers.py 各函数签名、reporter 扩展点）
- 注意事项：现金理财自动过滤、佣金率>5‱预警、胜率为估算口径

## Verification Steps（验收步骤）

1. **依赖可用**：`python -c "import pandas, numpy, openpyxl, xlrd"` 无报错。
2. **脚本无报错读取 XLS**：`python services/fupan-report/main.py -f <示例xls>` 正常退出，打印报告。
3. **五大模块标题清晰**：输出含 `【1. 交易成本复盘】`~`【5. 纪律与总结】`，与需求交付物格式一致。
4. **数值人工复核**：
   - 总交易额 = Σ|发生金额|；
   - 实际佣金率 = 总佣金/总交易额×10000（单位‱）；
   - 净盈亏 = 总卖出金额 - 总买入金额 - 总费用。
5. **预警生效**：构造佣金率>5‱的测试数据，输出含 `⚠️ 警告：佣金过高`。
6. **缺列报错**：删除某列的测试文件，运行报 `缺少关键列：xxx`。
7. **现金理财过滤**：含"天添利"记录的数据，报告剔除条数提示生效。
8. **落盘**：`data/reports/fupan_{date}.txt` 生成且内容与控制台一致。
9. **README 命令**：按 README 常用命令可直接运行成功。

## 不做项（明确排除）

- 不生成 HTML 报告文件（避免过度工程）。
- 不注册到 `scripts/run_all.py`（按需手动调用）。
- 不修改任何现有文件。
- 不引入可视化图表库（需求明确"便于后续二次开发"即可，本期不实现）。
