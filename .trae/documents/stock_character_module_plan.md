# 新增 stock_character 服务模块：股票特性分析（连板基因 + 游资席位）

## Context（背景与动机）

当前 `services/` 下已有 alarming_monitor（连板龙头温度）、bankuai-service（板块轮动）、sell_monitor（持仓监控）、yanbao-info（研报摘要）等独立服务模块，但缺少**个股层面的股性分析**能力：即对单只股票的"妖股基因"（历史连板特征 + 高波动属性）和"游资席位偏好"（龙虎榜席位买卖稳定性）进行量化刻画。

本模块解决两个场景：
1. **连板基因识别**：遍历个股涨停历史，提取最高连板天数、连板次数占比、近期日振幅，筛选"具备妖股基因但当前形态刚启动"的标的。
2. **游资席位偏好**：解析龙虎榜买卖席位（机构专用 vs 知名游资），结合次日涨跌幅判定"偏爱锁仓"或"一日游"风险。

## 关键设计依据（基于实测）

| zzshare 接口 | 实测状态 | 用途 |
|---|---|---|
| `stock_uplimit_reason_history(code, page, pageSize)` | ✅ 可用，返回 dict{'items':[...]}，含 date1/stock_code/up_limit_keep_times/reason/fengdan_money | 功能一：个股涨停历史 |
| `lhb_list(date1)` | ✅ 可用，返回 list[dict]，含 buy_group_icons/sell_group_icons（席位 name/amount/youzi_icon） | 功能二：龙虎榜席位明细 |
| `lhb_stock_history` / `lhb_trader_history` | ❌ 返回 None（API 不稳定） | 不可用，改用 lhb_list 跨日迭代 |
| `uplimit_stocks(date1)` / `review_uplimit_reason(date1)` | ✅ 可用（alarming_monitor 已封装） | --scan 模式获取候选股票池 |

**个股日线**：复用 sell_monitor 的 efinance（主）+ baostock（兜底）模式。

## 模块结构

```
services/stock_character/
├── __init__.py
├── config.py            # StockCharConfig dataclass，所有阈值/限频/路径集中
├── data_loader.py       # zzshare + 日线数据获取，限频，缓存
├── gene.py              # 功能一：连板基因分析（纯函数，零 IO）
├── hot_money.py         # 功能二：游资席位分析（纯函数，零 IO）
├── reporter.py          # ASCII + CSV + Markdown 输出
├── storage.py           # CSV 月度持久化
├── main.py              # CLI 入口，--codes/--scan/--gene-only/--hotmoney-only
└── test_character.py    # 单元测试
```

遵循 [alarming_monitor](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor) 的分层约定：data_loader（IO+限频+缓存）→ gene/hot_money（纯函数分析）→ reporter（输出）→ storage（落盘）→ main（编排）。

## 实现步骤

### 步骤 1：config.py — 配置中心

参考 [yanbao-info/config.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/yanbao-info/config.py) 的 dataclass + `from_env` 模式。

```python
@dataclass
class StockCharConfig:
    # ===== 取数限频 =====
    zzshare_token: Optional[str] = None                    # None → 匿名(30次/分钟)
    history_request_interval_seconds: float = 0.5          # 历史接口调用间隔(秒)，防 429
    request_timeout: int = 30

    # ===== 历史窗口 =====
    gene_history_days: int = 365                           # 连板基因回溯窗口(近一年)
    gene_recent_days: int = 90                             # 妖股定义窗口(近三个月)
    kline_recent_days: int = 180                           # 日振幅计算窗口(近6个月)

    # ===== 妖股基因阈值 =====
    gene_min_max_boards: int = 2                           # 历史最高连板数下限
    gene_min_consecutive_2plus: int = 1                    # 近期2连板+次数下限
    gene_min_amplitude: float = 5.0                       # 日振幅均值下限(%)
    gene_recent_limitup_days: int = 30                     # "刚启动"窗口：近期有涨停但未过热
    gene_current_hot_threshold: int = 3                   # 当前连板≥此值视为"已过热"，非刚启动

    # ===== 游资席位映射表（可扩展）=====
    institution_seat_name: str = "机构专用"
    hot_money_seats: dict = field(default_factory=lambda: {
        "炒股养家": ["华泰证券股份有限公司深圳益田路证券营业部"],
        "方新侠": ["兴业证券股份有限公司陕西分公司"],
        # ... 更多知名游资常用席位，用户可在 config 追加
    })

    # ===== 次日溢价/核按钮阈值 =====
    premium_threshold: float = 3.0                         # 次日涨幅≥3% → 锁仓信号
    dump_threshold: float = -5.0                           # 次日跌幅≥5% → 一日游风险
    hot_money_min_buy_count: int = 2                       # 游资买入≥N次才判定偏好

    # ===== 输出路径 =====
    csv_dir: str = "data/stock_character"
    reports_dir: str = "data/reports/stock_character"

    @classmethod
    def from_env(cls, **overrides) -> "StockCharConfig": ...
```

### 步骤 2：data_loader.py — 数据获取层

复用 [alarming_monitor/data_loader.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/data_loader.py) 的 zzshare 封装模式（`_get_zzshare_api`/`_zz_call`/`_suppress_output`/`_normalize_uplimit_columns`），新模块自带副本（遵循各服务自包含约定）。

**新增函数：**

| 函数 | 数据源 | 返回 | 说明 |
|---|---|---|---|
| `fetch_stock_uplimit_history(code, cfg)` | `stock_uplimit_reason_history(code, page, pageSize)` | DataFrame: date/continue_cnt/reason/seal_money | 分页拉满（pageSize=100），按 cfg 限频 |
| `fetch_lhb_list(date_ymd, cfg)` | `lhb_list(date1)` | DataFrame: date/stock_code/stock_name/seat_name/seat_type/amount/youzi_icon | 展平 buy_group_icons+sell_group_icons |
| `fetch_lhb_for_stock(code, dates, cfg)` | 迭代 `lhb_list(date)` × N 天 | DataFrame 同上，过滤 code | 限频：每次调用 sleep(interval)；先用 stock_uplimit_history 的日期定位上榜日 |
| `fetch_daily_kline(symbol, days, cfg)` | efinance（主）+ baostock（兜底） | DataFrame: date/open/high/low/close/volume | 复用 sell_monitor 的 `_try_efinance`/`_try_baostock` 模式 |
| `fetch_scan_candidates(date_ymd, cfg)` | `review_uplimit_reason` → `uplimit_stocks` fallback | list[code] | --scan 模式候选池（导入或复用 alarming_monitor 的 fetch_uplimit_stocks 逻辑） |

**限频实现**：`_rate_limited_call(fn, *args)` 装饰器，调用前 `time.sleep(cfg.history_request_interval_seconds)`，记录调用次数到日志。

**缓存策略**：
- `stock_uplimit_reason_history` 结果按 `stock_{code}_history.csv` 缓存到 `data/cache/`，TTL 20h（日内不重复拉）
- 日线数据按 `stock_{code}_kline.csv` 缓存（history 永久 + latest 20h TTL，参考 alarming_monitor 双层缓存）
- `lhb_list` 是日级快照不缓存（同 alarming_monitor 的 fetch_uplimit_stocks 约定）

### 步骤 3：gene.py — 功能一：连板基因分析（纯函数）

参考 [linkban.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/linkban.py) 的零 IO 分析层模式。

```python
def analyze_gene(history_df, kline_df, ref_date, cfg) -> dict:
    """分析单只股票的连板基因。纯函数零 IO。

    逻辑：
    1. 按 gene_history_days 过滤 history_df
    2. 提取 max_boards = max(continue_cnt)
    3. 统计 consecutive_2plus_count / consecutive_3plus_count（≥2板/≥3板次数）
    4. latest_limit_up_date = history_df.date.max()
    5. 日振幅：kline_df 近 kline_recent_days 的 (high-low)/pre_close*100 均值
    6. has_gene = max_boards >= gene_min_max_boards
                 AND consecutive_2plus_count >= gene_min_consecutive_2plus
                 AND avg_amplitude >= gene_min_amplitude
    7. just_starting = latest_limit_up_date 在 gene_recent_limitup_days 内
                       AND 当前未过热（无近5日≥gene_current_hot_threshold 连板记录）
    8. gene_score 0-100：max_boards(40) + consecutive_2plus(30) + amplitude(30) 查表加和
    """
```

**扫描入口**：`scan_genes(candidates, fetch_history_fn, fetch_kline_fn, ref_date, cfg) -> DataFrame`，遍历候选池调用 analyze_gene，过滤 `has_gene AND just_starting`，按 gene_score 降序。

### 步骤 4：hot_money.py — 功能二：游资席位分析（纯函数）

```python
def analyze_hot_money(lhb_records_df, kline_df, ref_date, cfg) -> dict:
    """分析单只股票的游资席位偏好。纯函数零 IO。

    逻辑：
    1. 席位分类：seat_name 匹配 cfg.hot_money_seats 映射表 → 标记 hot_money_name；
       匹配 institution_seat_name → 标记 '机构'；其余 → '其他'
       补充：youzi_icon 非空 → 标记为游资
    2. 统计 hot_money_buy_count（游资买入次数）、institutional_buy_count
    3. 次日涨跌：对每个 lhb date，从 kline_df 取下一交易日 pct_chg
    4. next_day_premium_count = 次日涨幅 ≥ premium_threshold 的次数
       next_day_dump_count = 次日跌幅 ≥ dump_threshold 的次数
    5. preference 判定：
       - hot_money_buy_count >= hot_money_min_buy_count
         AND next_day_premium_count >= 1 → '偏爱锁仓'
       - hot_money_buy_count >= hot_money_min_buy_count
         AND next_day_dump_count >= 1 → '一日游风险'
       - 否则 → '中性'
    6. preference_reason：如 '游资炒股养家2次买入·次日+4.2%溢价'
    """
```

### 步骤 5：reporter.py — 输出层

参考 [alarming_monitor/reporter.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/reporter.py) 的 ASCII + CSV + Markdown 三格式。

| 函数 | 输出 | 说明 |
|---|---|---|
| `format_gene_scan_report(results_df, ref_date)` | ASCII 等宽表 | 连板基因扫描结果，列出 code/name/max_boards/consecutive/amplitude/score/level |
| `format_gene_csv_row(result)` | CSV 单行 | 展平为 ~12 字段，用于月度落盘 |
| `format_hot_money_report(result, ref_date)` | ASCII 详情 | 单股游资席位分析，列出席位明细 + 次日涨跌 + preference |
| `format_hot_money_csv_row(result)` | CSV 单行 | 展平为 ~10 字段 |
| `generate_markdown_report(gene_df, hotmoney_results, ref_date)` | Markdown | 全景报告，拼接两章节 |

### 步骤 6：storage.py — CSV 持久化

参考 [alarming_monitor/storage.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/storage.py) 的按月分文件 + 同日去重模式。

- `log_gene_signal(result, csv_dir)` → `data/stock_character/gene_YYYY-MM.csv`
- `log_hotmoney_signal(result, csv_dir)` → `data/stock_character/hotmoney_YYYY-MM.csv`

### 步骤 7：main.py — CLI 入口

参考 [alarming_monitor/main.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/main.py) 的 argparse + 编排模式。

```bash
# 默认：扫描今日涨停池，跑连板基因 + 游资席位
uv run services/stock_character/main.py

# 指定个股
uv run services/stock_character/main.py --codes 000017,600550

# 仅跑连板基因扫描
uv run services/stock_character/main.py --gene-only --scan

# 仅跑游资席位分析（需 --codes 指定）
uv run services/stock_character/main.py --hotmoney-only --codes 000017

# 指定参考日
uv run services/stock_character/main.py --date 2026-08-28

# 静默（仅写文件）
uv run services/stock_character/main.py --quiet

# 详细日志
uv run services/stock_character/main.py -v
```

**编排流程**：
1. 解析 args → 构造 StockCharConfig
2. 确定候选池：`--codes` 优先，否则 `--scan` 调 fetch_scan_candidates
3. 若非 `--hotmoney-only`：对每只股票 fetch_history + fetch_kline → analyze_gene → 汇总
4. 若非 `--gene-only`：对每只股票 fetch_lhb_for_stock + fetch_kline → analyze_hot_money → 汇总
5. reporter 格式化 → 控制台打印 + storage 落盘 CSV + Markdown

### 步骤 8：test_character.py — 单元测试

参考 [test_linkban.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/test_linkban.py) 的纯函数测试模式。

- `analyze_gene`：空数据兜底、max_boards 提取、consecutive 统计、amplitude 计算、has_gene/just_starting 判定、score 查表
- `analyze_hot_money`：席位分类（机构/游资/其他）、次日涨跌计算、preference 三分支（锁仓/一日游/中性）、reason 格式化
- reporter：ASCII 格式、CSV 行字段数

## 复用现有代码

| 复用项 | 来源 | 复用方式 |
|---|---|---|
| `_get_zzshare_api`/`_zz_call`/`_suppress_output`/`_normalize_uplimit_columns`/`_dedup_uplimit_by_code` | alarming_monitor/data_loader.py | 复制到新模块 data_loader.py（服务自包含约定） |
| efinance/baostock 日线获取 | sell_monitor/data_loader.py 的 `_try_efinance`/`_try_baostock` | 复制并适配 |
| fetch_uplimit_stocks（scan 候选池） | alarming_monitor/data_loader.py | 复制核心逻辑或直接 import（跨服务） |
| CSV 月度落盘模式 | alarming_monitor/storage.py | 复制同日去重逻辑 |
| dataclass config + from_env | yanbao-info/config.py | 参考结构 |
| argparse + --xxx-only 编排 | alarming_monitor/main.py | 参考模式 |

## 验证方式

```bash
# 1. 单元测试
uv run pytest services/stock_character/test_character.py -v

# 2. 指定个股全流程（功能一+二）
uv run services/stock_character/main.py --codes 000017 -v

# 3. 扫描今日涨停池
uv run services/stock_character/main.py --scan --gene-only

# 4. 验证输出文件
ls data/stock_character/                          # CSV
ls data/reports/stock_character/                   # Markdown

# 5. 验证限频（观察日志中调用间隔）
uv run services/stock_character/main.py --codes 000017 -v 2>&1 | grep "限频\|interval"

# 6. 验证 lhb 接口（data_loader 可独立测试）
uv run python -c "from services.stock_character.data_loader import fetch_lhb_list; print(fetch_lhb_list('20260828'))"
```
