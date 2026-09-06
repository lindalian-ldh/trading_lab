# 市场大跌预警监控系统（alarming_monitor）实现计划

## 摘要

在 `services/alarming_monitor/` 下新建一个独立服务，基于三大类指标分解出的 **5 个原子警戒信号**，每日盘后输出红绿灯状态 + 降仓位建议，并将每次运行结果追加写入 CSV 历史记录便于复盘。

数据源：**akshare（主，已安装）** + **adata（备份，需新增依赖 `pip install adata`）**。
板块代表：**代表性 ETF 篮子**（高股息 vs 成长）。
输出：**CSV 历史记录**（主）+ 轻量控制台摘要。

---

## 一、当前状态分析（基于 Phase 1 探索）

### 1.1 服务结构约定（已确认）

参照 [services/sell_monitor/](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/sell_monitor) 这一最相似的"监控类"服务，每个服务统一为：

- `main.py` —— `argparse` CLI + 跑完即退，`if __name__ == "__main__": sys.exit(main())`
- `config.py` —— dataclass 配置中心 + 注册表（如 `ExitConfig` / `_CONFIG_REGISTRY`），所有阈值/参数集中，无硬编码
- `data_loader.py` —— 多数据源适配（优先 → 兜底），列标准化，含 `_suppress_output` 压制噪声
- 领域逻辑模块（如 `monitor.py`）—— 纯函数判定引擎
- `reporter.py` —— 控制台/文件输出
- `storage.py` —— 持久化（CSV 按天/月分文件）
- `pyproject.toml` —— 子服务依赖面文档（`package = false`，实际依赖由根 [pyproject.toml](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/pyproject.toml) 统一声明）
- `test_*.py` —— 单元测试（pytest）
- `README.md` —— 文档

### 1.2 关键约定（来自 memory 与代码）

- **配置驱动**：所有数值参数进 `config.py` dataclass，`main.py` 不出现硬编码数值（硬约束，对齐用户偏好）。
- **CSV 输出**：交易/信号类记录走 CSV 按天/月分文件（见 [storage.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/sell_monitor/storage.py) 的 `_CSV_DIR = _TRADING_LAB_DIR / "data" / "sell_signals"` 模式）。
- **路径派生**：`_SERVICE_DIR.parent.parent / "data" / ...`，不依赖 CWD。
- **性能**：总执行 < 1 分钟（用户硬要求）。全市场 `stock_zh_a_spot_em` 单次 HTTP 即可（~2-5s），指数/ETF 20 日历史只取少量标的，满足约束。
- **数据源现状**：根 `pyproject.toml` 已含 `akshare>=1.12`、`efinance`、`baostock`、`pandas`、`numpy`。`adata` **未安装**，需新增。
- **bankuai-service 已用** `stock_zh_a_spot_em` 的 `circulation_value`（流通市值）/`volume` 字段，证明该端点可用且字段稳定，本服务复用同款取数。

### 1.3 用户明确决策（Phase 2 已澄清）

| 维度 | 用户选择 |
|------|----------|
| 数据源 | akshare（主）+ adata（备份，`pip install adata`） |
| 指标3板块代表 | 代表性个股/ETF 篮子 |
| 输出形式 | CSV 历史记录（主）；控制台摘要保留为轻量辅助 |

---

## 二、指标 → 5 个原子信号设计

用户给的 3 大指标中，指标1含"放量不涨"附加信号、指标3含"高股息逆势走强 + 成长破位"双信号。分解为 5 个原子红灯信号，使"超过3个亮红灯"的表述自洽（5 选 ≥4 即"超过3"）：

| # | 信号名 | 触发条件（阈值进 config.py，可调） |
|---|--------|-------------------------------------|
| S1 | `turnover_ratio_high` | 今日两市成交额合计 / **总市值合计** > `TURNOVER_RATIO_THRESHOLD`（默认 **0.030，即3.0%**）。分母取 `stock_zh_a_spot_em` 的"总市值"列（含非流通股），A 股总市值~100-110万亿 × 3% = 3-3.3万亿，接近历史峰值成交额~3万亿，情绪沸腾时亮灯。 |
| S2 | `volume_price_divergence` | 放量不涨：**近5日日均成交额 > 近20日日均成交额 × `VOLUME_HIGH_MULT`（默认 1.3）** 且 指数（沪深300）近5日涨幅 ≤ `INDEX_FLAT_PCT`（默认0）。⚠️ 口径统一为"日均÷日均"修正量级错误（旧版"5日总量 vs 20日日均"量级差5倍会持续误灯）；乘数 1.3 要求成交量明显高于均量才灯，避免震荡市持续误灯。 |
| S3 | `ad_ratio_bearish` | 采用用户给定逻辑：**`max(ad_ratio[-20:]) > AD_HIGH_REF`（默认2.5）且 `ad_ratio[-1] < AD_LOW_THRESHOLD`（默认1.0）`** —— 过去20日内日涨跌家数比曾超过2.5，且今日比值跌破1.0。比"20日均值回落"更直接捕捉"从高位快速回落"。 |
| S4 | `dividend_strength` | 逆势走强：**沪深300近20日涨幅 < 0（市场下跌）且 高股息篮子近20日涨幅 > `DIVIDEND_STRONG_PCT`（默认0，绝对正收益）且 (高股息篮子涨幅 - 成长篮子涨幅) > `DIVIDEND_SPREAD_PCT`（默认0.03，超额成长3%）**。原"涨幅>0"过弱，现要求市场跌时高股息逆势涨 + 显著跑赢成长。 |
| S5 | `growth_breakdown` | 成长篮子（芯片ETF+计算机ETF）近20日涨幅 < `GROWTH_BREAK_PCT`（默认-0.05）**或** 跌破近20日收盘均线。 |

**降仓位规则**（`config.py` 可调表，按 red_count 降序匹配首条）：
- red_count ≥ `REDUCE_THRESHOLD`（默认 4，即"超过3"）→ `8成 → 5成`（重仓降至半仓以下）
- red_count ≥ `WARN_THRESHOLD`（默认 2）→ `8成 → 7成`（小幅降低或保持观察）
- 其他 → 维持当前仓位

> 默认阈值取 4 严格对应"超过3个"；用户可在 `config.py` 一处调低为 3（"3个及以上"宽松口径）。此为可调项，非阻塞。

---

## 三、提议变更（文件级）

新建目录 `services/alarming_monitor/`，共 9 个文件：

### 3.1 `config.py` —— 配置中心（dataclass + 注册表）

仿 [config.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/sell_monitor/config.py)。包含：

- `@dataclass AlarmConfig`：上述全部阈值字段 + ETF 篮子定义 + 指数代码 + 降仓位映射表 `POSITION_ADVICE: List[Tuple[int,str,str]]`
- `ConservativeAlarmConfig(AlarmConfig)`：更严阈值（`TURNOVER_RATIO_THRESHOLD=0.025`、`VOLUME_HIGH_MULT=1.5`、`REDUCE_THRESHOLD=3` 等，提前预警）
- `StrictAlarmConfig(AlarmConfig)`：更宽阈值（`TURNOVER_RATIO_THRESHOLD=0.035`、`VOLUME_HIGH_MULT=1.2`、`REDUCE_THRESHOLD=4` 等，仅极端信号预警）
- `_CONFIG_REGISTRY` + `get_alarm_config(profile)` + `list_profiles()`

ETF 篮子默认值（用户选"代表性个股/ETF 篮子"，ETF 抗个股噪声，优先 ETF，代码进 config 可换个股）：
- 高股息篮子：`DIVIDEND_BASKET = ["512800", "516070"]`（银行ETF、公用事业ETF）
- 成长篮子：`GROWTH_BASKET = ["159995", "512720"]`（芯片ETF、计算机ETF）
- 指数：`BREADTH_INDEX = "sh000300"`（沪深300，用于 S2 放量不涨 + S4 逆势判定）
- 历史窗口：`LOOKBACK_DAYS = 20`（S2/S3/S4/S5 通用近20日）

阈值默认值（已在 S1-S5 标定，集中于此便于复核）：
```python
TURNOVER_RATIO_THRESHOLD = 0.030   # S1: 成交额/总市值 > 3.0%
VOLUME_HIGH_MULT = 1.3             # S2: 近5日日均 > 近20日日均 × 1.3（修正量级口径）
INDEX_FLAT_PCT = 0.0               # S2: 指数5日涨幅 ≤ 0
AD_HIGH_REF = 2.5                  # S3: 过去20日日涨跌比曾 > 2.5
AD_LOW_THRESHOLD = 1.0             # S3: 今日涨跌比 < 1.0
DIVIDEND_STRONG_PCT = 0.0          # S4: 高股息篮子绝对收益 > 0
DIVIDEND_SPREAD_PCT = 0.03         # S4: 高股息-成长超额 > 3%
GROWTH_BREAK_PCT = -0.05           # S5: 成长篮子近20日涨幅 < -5%
REDUCE_THRESHOLD = 4               # 5信号中 ≥4 红 → 8成→5成（"超过3"严格口径）
WARN_THRESHOLD = 2                 # ≥2 红 → 8成→7成
```

### 3.2 `data_loader.py` —— 数据获取层（akshare 主，adata 备份）

仿 [data_loader.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/sell_monitor/data_loader.py) 的多源适配 + `_suppress_output` 压制噪声 + 列标准化。提供以下纯函数：

| 函数 | 用途 | akshare 端点（主） | adata 兜底 |
|------|------|---------------------|------------|
| `fetch_market_spot()` | 全市场快照（成交额/**总市值**/涨跌幅，用于 S1、S3 今日涨跌家数） | `ak.stock_zh_a_spot_em()`（返回 代码/名称/涨跌幅/成交额/**总市值**/流通市值 等，bankuai-service 已验证字段可用） | `adata.stock.market.get_market()` 聚合 |
| `fetch_index_daily(symbol, days)` | 指数日线（close + 成交额，用于 S2） | `ak.stock_zh_index_daily_em(symbol=...)`（优先，含 amount）→ 兜底 `ak.stock_zh_index_daily(symbol=...)`（volume 字段） | adata 指数接口 |
| `fetch_etf_daily(symbol, days)` | ETF 日线（close，用于 S4/S5） | `ak.fund_etf_hist_em(symbol=..., period="daily", adjust="qfq")` | adata ETF/K线 |
| `fetch_breadth_history(days)` | 涨跌家数历史（用于 S3 20日均值） | `ak.stock_market_activity_legu`（若可用，含历史涨跌家数）→ 不可用则由 `fetch_market_spot()` 仅得今日，历史段降级 | adata 市场广度（若有） |

返回标准化 DataFrame，列名英文小写：`date/open/high/low/close/volume/amount`，`date` 为 `YYYY-MM-DD` 字符串。所有函数失败返回 `None`，调用方决定降级。

**降级策略**（关键，写入函数文档）：
- S3 历史段取不到 → 仅用今日涨跌家数比，标记 `data_sufficient=False`，20日均值信号置灰（不亮红灯），控制台提示数据不足。
- 任一数据源失败 → log warning，继续其余信号，不整体崩溃。

### 3.3 `indicators.py` —— 5 信号计算（纯函数，无 IO）

每个函数接收标准化 DataFrame + `AlarmConfig`，返回 dict：`{name, red: bool, value: float, threshold: float, detail: str}`。

- `check_turnover_ratio(spot_df, cfg)` → S1：`sum(成交额) / sum(总市值)` 对比 `TURNOVER_RATIO_THRESHOLD`
- `check_volume_price_divergence(index_df, cfg)` → S2：近5日日均成交额 / 近20日日均成交额 对比 `VOLUME_HIGH_MULT`，且指数5日涨幅 ≤ `INDEX_FLAT_PCT`（**日均÷日均修正量级**）
- `check_ad_ratio(ad_ratio_series, cfg)` → S3：`max(ad_ratio[-20:]) > AD_HIGH_REF and ad_ratio[-1] < AD_LOW_THRESHOLD`
- `check_dividend_strength(index_df, etf_prices, cfg)` → S4：沪深300近20日涨幅<0 且 高股息篮子>0 且 (高股息-成长)>`DIVIDEND_SPREAD_PCT`（**逆势走强**）
- `check_growth_breakdown(etf_prices, cfg)` → S5：成长篮子近20日涨幅 < `GROWTH_BREAK_PCT` 或跌破近20日均线

纯函数便于单测，不依赖网络。

### 3.4 `monitor.py` —— 聚合引擎

`evaluate_signals(indicator_results: list, cfg) -> dict`：
- 统计 `red_count`
- 按 `POSITION_ADVICE` 表匹配首条 ≥ red_count 的建议 → `position_advice`（如 "8成 → 5成"）
- 计算 `risk_level`：`high`（red_count≥REDUCE_THRESHOLD）/ `warn`（≥WARN_THRESHOLD）/ `normal`
- 返回结构化 dict，含全部信号明细 + 聚合结论 + 时间戳

### 3.5 `reporter.py` —— 输出

- `print_summary(result)`：控制台轻量摘要（红绿灯 ASCII + red_count + 仓位建议一行），所有服务都有控制台输出，保留以维持一致体验；可选 `--quiet` 静默。
- `format_csv_row(result)`：把结果展平为 CSV 行 dict。

### 3.6 `storage.py` —— CSV 持久化

仿 [storage.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/sell_monitor/storage.py) 路径派生：
- `_CSV_DIR = _TRADING_LAB_DIR / "data" / "alarming_signals"`
- `log_alarm_signal(result) -> Path`：追加一行到 `data/alarming_signals/alarming_YYYY-MM.csv`（按月分文件，追加模式，首行写表头）。
- `list_history(months=3)`：读取近 N 个月 CSV 返回 DataFrame（`--history` 子命令用）。

CSV 字段：`run_time, ref_date, risk_level, red_count, s1_turnover_ratio, s1_red, s2_divergence, s2_red, s3_ad_ratio_ma20, s3_red, s4_dividend_ret, s4_red, s5_growth_ret, s5_red, position_advice, data_sufficient`。

### 3.7 `main.py` —— argparse CLI 入口

仿 [main.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/sell_monitor/main.py) 风格。CLI：

```bash
# 默认：跑全部 5 信号，输出控制台摘要 + 写 CSV
uv run services/alarming_monitor/main.py

# 指定基准日（默认昨日）
uv run services/alarming_monitor/main.py --date 2026-08-19

# 配置档
uv run services/alarming_monitor/main.py --profile conservative

# 只跑部分指标（逗号分隔）
uv run services/alarming_monitor/main.py --indicator turnover,dividend,growth

# 不写 CSV
uv run services/alarming_monitor/main.py --no-csv

# 查看近 3 个月历史
uv run services/alarming_monitor/main.py --history

# 静默（仅写 CSV，无控制台输出）
uv run services/alarming_monitor/main.py --quiet

# 详细日志
uv run services/alarming_monitor/main.py --verbose
```

主流程：加载 config → 解析 CLI → init 数据 → 调 data_loader 取数 → 调 indicators 计算 → 调 monitor 聚合 → reporter 打印 → storage 写 CSV。`--indicator` 过滤信号子集（不影响 5 信号定义，仅跳过未选计算）。退出码 0 成功 / 1 参数错误 / 2 数据全失败。

### 3.8 `pyproject.toml` —— 依赖面文档

```toml
[project]
name = "trading-lab-alarming-monitor"
version = "0.1.0"
description = "市场大跌预警监控系统（三大类指标·5原子信号·降仓位建议）"
requires-python = ">=3.11"
dependencies = ["pandas>=2.0", "numpy>=1.26", "akshare>=1.12", "adata"]

[tool.uv]
package = false
```

并在根 [pyproject.toml](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/pyproject.toml) 的 `dependencies` 列表追加 `"adata"`（注释：alarming_monitor 备份数据源所需），使 `uv sync` 一次性装齐。

### 3.9 `test_monitor.py` —— 单元测试

仿 [test_monitor.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/sell_monitor/test_monitor.py)。覆盖（构造 mock DataFrame，不触网）：

| 测试场景 | 预期 |
|----------|------|
| S1 成交额/总市值 > 3.0% | red=True；< 3.0% red=False |
| S2 近5日日均 > 近20日日均 ×1.3 且 指数5日涨幅≤0 | red=True；量缩或价涨 red=False |
| S2 量级校验：5日总量 vs 20日日均 的旧错误口径 | 已删除，确保不再出现 |
| S3 max(ad_ratio[-20:])>2.5 且 今日<1.0 | red=True；曾>2.5但今日>1.0 → red=False |
| S3 数据不足（历史<20日） | red=False, data_sufficient=False |
| S4 沪深300跌 + 高股息涨 + 超额成长>3% | red=True；市场涨时 → red=False（非逆势） |
| S5 成长篮子近20日<-5% 或 跌破20日线 | red=True |
| red_count=5 → 8成→5成 | risk_level=high |
| red_count=4 → 8成→5成 | risk_level=high（"超过3"触发） |
| red_count=2 → 8成→7成 | risk_level=warn |
| red_count=1 → 维持 | risk_level=normal |
| POSITION_ADVICE 表按降序匹配首条 | 正确 |

集成测试（需网络）默认跳过（`@pytest.mark.skip`），与 sell_monitor 一致。

### 3.10 `__init__.py` —— 模块说明（单行 docstring）

与 sell_monitor 的 `__init__.py` 一致。

### 3.11 README.md（可选，最后补）

按 sell_monitor README 风格补一份（含 CLI 表、信号表、配置档表、CSV 字段表）。**仅当用户后续要求时才创建**，默认不主动建文档（遵循系统约束"NEVER proactively create documentation"）。

---

## 四、假设与决策

1. **"超过3个亮红灯"口径**：分解为 5 个原子信号后，默认 `REDUCE_THRESHOLD=4`（严格"超过3"，即5信号中≥4红才降仓）。可在 config 调为 3（"3个及以上"宽松口径）。Conservative 档默认调为 3 以提前预警。**可调，非阻塞**。
2. **S1 分母与阈值标定**（已按用户复核确认）：分母取 **总市值**（`stock_zh_a_spot_em` 的"总市值"列，含非流通股），阈值 **3.0%**。已核算 A 股总市值~100-110万亿 × 3% = 3-3.3万亿，接近历史峰值成交额~3万亿，情绪沸腾时可达。原"流通市值+4.5%"不可达，已废弃。
3. **S2 量级口径修正**（按用户反馈）：旧版"近5日成交额(总量) vs 近20日均量(日均)"量级差5倍会持续误灯，已改为"近5日日均 ÷ 近20日日均 × 1.3"。乘数 1.3 为中性默认（用户未明确选，取推荐值），Conservative=1.5 / Strict=1.2 可调。
4. **S3 逻辑**（按用户给定代码）：`max(ad_ratio[-20:]) > 2.5 AND ad_ratio[-1] < 1.0`，比"20日均值回落"更直接捕捉"从高位快速回落"。
5. **S4 逆势走强定义**（按用户反馈）：原"涨幅>0"过弱，已改为"沪深300近20日涨幅<0（市场跌）且 高股息篮子绝对正收益 且 超额成长>3%"，真正捕捉逆势。
6. **ETF 篮子代码**：默认 银行ETF(512800)/公用事业ETF(516070)/芯片ETF(159995)/计算机ETF(512720)，均进 config 可换为个股代码（6位）。取数用 `fund_etf_hist_em` 前复权日线。
7. **S3 历史涨跌家数**：优先 `stock_market_activity_legu`；不可用时仅得今日涨跌比，无法判断"曾>2.5"，信号置灰（red=False, data_sufficient=False）并提示数据不足。唯一可能数据不足的信号，降级而非报错。
8. **指数代表**：S2/S4 均用沪深300（sh000300）；不可取时兜底用上证综指（sh000001）。
9. **adata 安装**：新增依赖到根 `pyproject.toml`，`uv sync` 安装。若 adata 安装失败或 import 失败，data_loader 优雅降级为仅 akshare（log warning，不阻断）。
10. **控制台输出**：尽管用户仅勾选 CSV，仍保留轻量控制台摘要（所有服务一致体验），`--quiet` 可关。CSV 是用户明确选择的"记录"主输出。
11. **不新建 SQLite**：本服务无持仓状态需持久化，仅 CSV 历史，不建表。
12. **基准日**：`--date` 默认昨日（与 sell_monitor 一致），盘后运行取最新完整交易日数据。
13. **不修改其他服务**：仅在根 `pyproject.toml` 追加 `adata` 依赖，不改任何现有服务代码。

---

## 五、验证步骤

1. **依赖安装**：`cd trading_lab && uv sync` → 确认 `adata` 装入 `.venv`。
2. **冒烟运行**：`uv run services/alarming_monitor/main.py --verbose` → 控制台打印 5 信号红绿灯 + 仓位建议，并生成 `data/alarming_signals/alarming_YYYY-MM.csv`。
3. **指定日期**：`uv run services/alarming_monitor/main.py --date 2026-08-19` → 正确读取该日数据。
4. **历史查看**：`uv run services/alarming_monitor/main.py --history` → 打印近 3 月 CSV 汇总。
5. **单元测试**：`uv run pytest services/alarming_monitor/test_monitor.py -v` → 全部 mock 用例通过（集成测试默认 skip）。
6. **降级验证**：临时令 `fetch_breadth_history` 返回 None → S3 应 red=False, data_sufficient=False，不崩溃。
7. **性能**：单次运行 < 1 分钟（spot 单次 HTTP + 少量 ETF/指数 20 日历史）。
8. **回归**：`uv run pytest` 全量 → 现有测试不受影响（仅根 pyproject.toml 加依赖，无代码改动）。

---

## 六、文件清单（最终交付）

```
services/alarming_monitor/
├── __init__.py            # 模块说明
├── pyproject.toml         # 子服务依赖面
├── config.py              # AlarmConfig 配置中心（阈值/篮子/仓位映射）
├── data_loader.py         # akshare主 + adata备 数据获取
├── indicators.py           # 5 原子信号纯函数
├── monitor.py             # 信号聚合 → 仓位建议
├── reporter.py            # 控制台摘要 + CSV 行格式化
├── storage.py             # CSV 持久化（按月分文件）
├── main.py                # argparse CLI 入口
└── test_monitor.py        # 单元测试
```

根 `pyproject.toml` 追加 `"adata"` 依赖（一行，带注释）。

不新建其他文件，不改其他服务。
