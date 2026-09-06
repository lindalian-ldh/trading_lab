# 连板龙头 & 大盘温度 — 模块四技术文档

基于 zzshare SDK 的每日连板数据自动分析模块，通过量化"连板高度、梯队分布、晋级率、封单金额"等指标，输出大盘温度评分（0-100 分），衡量市场短线的风险偏好与赚钱效应。

---

## 一、功能目标

连板高度（最高板）和连板梯队是衡量市场情绪和短线交易热度的重要指标。本模块每日自动获取全量涨停股数据，输出以下核心指标：

| 指标 | 说明 | 温度含义 |
|------|------|----------|
| 最高连板数 | 当日市场最高连板高度 | 越高 → 情绪越热 |
| 连板梯队分布 | 2板、3板、4板...各有多少只 | 梯队完整 → 赚钱效应好 |
| 连板股总数 | 当日所有连板股票数量（≥2板） | 越多 → 市场越活跃 |
| 涨停股总数 | 当日所有涨停股票数量（含首板） | 市场整体参与度 |
| 晋级率 | 昨日连板股中今日继续涨停的比例 | 越高 → 接力意愿强 |
| 封单金额 | 龙头股的封单大小 | 越大 → 资金态度坚决 |
| 涨停原因 | 龙头股的题材/概念 | 判断主线方向 |

---

## 二、架构与数据流

```
┌─────────────────────────────────────────────────────────┐
│                    main.py (入口)                         │
│  --linkban-only / --no-linkban 参数解析 · 流程编排        │
└──────────────────┬──────────────────────────────────────┘
                   │
         ┌─────────▼──────────┐
         │  data_loader.py     │
         │  fetch_uplimit_stocks│  ← zzshare review_uplimit_reason
         │  fetch_uplimit_hot   │    (主力) / uplimit_stocks (fallback)
         │  fetch_uplimit_reason│
         └─────────┬──────────┘
                   │
         ┌─────────▼──────────┐
         │  linkban.py (纯函数) │
         │  analyze_linkban()   │
         │  梯队分布 + 晋级率   │
         │  龙头识别 + 温度评分  │
         └─────────┬──────────┘
                   │
    ┌──────────────┼──────────────────┐
    │              │                  │
┌───▼────┐  ┌──────▼───────┐  ┌──────▼──────┐
│reporter│  │  storage.py  │  │   main.py   │
│ASCII+MD│  │ CSV 月度落盘  │  │ 控制台打印   │
│+CSV行  │  │ linkban_YM  │  │ 总耗时统计   │
└────────┘  └──────────────┘  └─────────────┘
```

### 核心文件职责

| 文件 | 职责 |
|------|------|
| [config.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/config.py) Section 7 | 配置参数：ST 过滤、梯队最小板数、温度评分阈值表、温度等级映射、CSV 目录 |
| [data_loader.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/data_loader.py) | 数据获取层：`fetch_uplimit_stocks`（主力 `review_uplimit_reason` → fallback `uplimit_stocks`）、`fetch_uplimit_hot`、`fetch_uplimit_reason`；字段别名归一化 + 同股多板块去重 |
| [linkban.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/linkban.py) | 纯函数分析层（零 IO）：`analyze_linkban` 梯队分布 + 晋级率 + 龙头识别 + 温度评分；`_lookup_score` 档分查表；`_normalize_code` 代码归一化 |
| [reporter.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/reporter.py) | 输出层：`format_linkban_report` ASCII 等宽报告；`format_linkban_csv_row` CSV 行展平（20 字段）；`generate_markdown_report` 全景报告拼接连板章节 |
| [storage.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/storage.py) | CSV 持久化：`log_linkban_signal` 按月追加 `linkban_YYYY-MM.csv`（同日去重）；`read_linkban_history` 跨月读取 |
| [main.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/main.py) | 入口：`--linkban-only` / `--no-linkban` 参数、`_gather_linkban_data` 数据获取、`_run_once` 流程编排 |
| [test_linkban.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/test_linkban.py) | 单元测试：19 个用例覆盖 `_lookup_score`、`_normalize_code`、`analyze_linkban` 全路径（空数据/ST 过滤/梯队/晋级率/龙头/温度）、报告格式化 |

---

## 三、数据获取层（data_loader.py）

### 3.1 数据源：zzshare SDK

| 接口 | 用途 | 返回类型 |
|------|------|----------|
| `review_uplimit_reason(date1='YYYYMMDD')` | **主力**：涨停复盘，返回全量涨停股（含首板+连板），每板块下含 stocks 列表 | `list[dict]`，179 个板块，展平后 ~1000 条 |
| `uplimit_stocks(date1='YYYYMMDD')` | **fallback**：仅返回连板股（≥2板），字段较少且可能不全 | `list[dict]` |
| `uplimit_hot(date1='YYYYMMDD')` | 涨停热门板块（辅助） | `list[dict]` |
| `stock_uplimit_reason(code, date)` | 个股涨停原因回查（龙头原因缺失时） | `str` |

> **主力接口选择原因**：`uplimit_stocks` 仅返回连板股且数据严重不全（2026-08-28 实测仅返回 1 只，实际涨停 82 只）。`review_uplimit_reason` 展平板块 stocks 后返回全部 82 只，与 `sentiment_hot_day` 的 `ztjs=82` 吻合。

### 3.2 字段别名归一化

zzshare 不同接口返回的字段名不一致，通过 `_LINK_UPLIMIT_COL_ALIASES` 映射到统一列名：

| 统一列名 | 源字段别名（按优先级） | 类型 |
|----------|----------------------|------|
| `code` | stock_code, ts_code, ticker, symbol | str |
| `name` | stock_name, 股票名称, 名称 | str |
| `continue_cnt` | up_limit_keep_times, 连续涨停天数, keep_times | int |
| `limit_up_time` | up_limit_time, 首次涨停时间 | str |
| `seal_money` | fengdan_money, seal_money, 封单金额 | float（元） |
| `limit_up_reason` | reason, up_limit_desc, 涨停原因, 概念 | str |
| `pct_chg` | pct_chg, 涨跌幅, 涨幅 | float |
| `is_st` | is_st, ST标记 | bool |

> **注意**：`reason` 优先于 `up_limit_desc`——后者只是"2连板"这样的连板描述，前者才是完整涨停原因文本（如"棉花+中报增长"）。`fengdan_money` 来自 `review_uplimit_reason`，单位为元（如 189528000.0 = ~1.9 亿元）。

### 3.3 板块去重 `_dedup_uplimit_by_code`

`review_uplimit_reason` 按板块（plate_code）把同一股票拆成多行。去重逻辑按 code 分组，保留每组中**最优**的一行：

1. `continue_cnt` 高的优先（连板数更多）
2. `seal_money` 大的优先（封单更强）
3. `limit_up_reason` 非空的优先
4. `limit_up_time` 早的优先（首板时间更早）

### 3.4 缓存策略

涨停数据是**日级快照**（同日多行，每行一只股票），不适合 `@cached` 的时间序列缓存逻辑（按 date 去重会把同日 82 行压成 1 行）。因此 `fetch_uplimit_stocks` **不使用 `@cached` 装饰器**，每次运行直接调用 zzshare 接口。每次运行最多调用 2 次（今日 + 昨日），耗时 <1s，对整体性能无影响。

### 3.5 `_suppress_output` 调用约定

zzshare 内部使用 `print()` 输出调试信息，会污染 Markdown 报告。所有 zzshare 调用必须包裹在 `_suppress_output(lambda: _zz_call(...))` 中，在 fd 级别（dup2 /dev/null）压制 stdout/stderr。

---

## 四、分析层（linkban.py）

### 4.1 主入口 `analyze_linkban`

```python
def analyze_linkban(today_df, yesterday_df, ref_date, cfg, reason_loader_fn=None) -> dict
```

输入：归一化 DataFrame（或 None/空）、参考日、配置对象。输出统一 dict 结构，零 IO。

### 4.2 处理流程

1. **空数据兜底**：`today_df` 为 None/空 → 标记 `data_sufficient=False`，温度分 30（最低档），返回
2. **ST 过滤**：`cfg.LINKBAN_FILTER_ST=True` 时按 `is_st` 列过滤（兜底：name 以 ST/*ST/NST 开头也标记）
3. **基础指标**：涨停总数 = len(df)；连板股 = `continue_cnt >= LINKBAN_MIN_BOARD_FOR_TIER`（默认 2）；最高连板 = max(continue_cnt)
4. **连板梯队**：连板股按 `continue_cnt DESC, seal_money DESC, limit_up_time ASC` 排序，分组到 `tier_distribution[board] = [{code, name, seal_money, limit_up_time, reason}, ...]`
5. **龙头股**：排序后第一行，涨停原因缺失时回调 `reason_loader_fn(code, date_ymd)` 补充
6. **晋级率**：昨日连板股（≥2板）codes ∩ 今日涨停股 codes / 昨日连板股总数
7. **温度评分**：三维度查表加和

### 4.3 温度评分（0-100 分）

| 维度 | 分值 | 满分条件 | 配置项 |
|------|------|----------|--------|
| 最高板 | 0-40 | ≥7板→40分, ≥5板→30分, ≥3板→20分, <3板→10分 | `TEMP_MAX_BOARD_SCORE` |
| 连板总数 | 0-30 | ≥20只→30分, ≥10只→20分, <10只→10分 | `TEMP_TOTAL_SCORE` |
| 晋级率 | 0-30 | ≥50%→30分, ≥30%→20分, <30%→10分 | `TEMP_JINJI_SCORE` |

> 查表逻辑：`_lookup_score(value, table)` 按 key 降序，首个 `value >= key` 命中对应分值。

### 4.4 温度等级映射

| 综合分 | 等级 | 操作建议 |
|--------|------|----------|
| ≥70 | 🔥 高温（市场极度活跃） | 可积极参与主线题材，注意高位分化风险 |
| ≥50 | 🌤️ 常温（市场正常） | 市场正常，精选个股操作 |
| <50 | ❄️ 低温（市场低迷） | 市场低迷，观望为主或控制仓位 |

配置项：`TEMP_LEVEL_MAP`（分数→等级）、`TEMP_ADVICE_BY_LEVEL`（等级→建议文案）。

### 4.5 代码归一化 `_normalize_code`

晋级率计算需要匹配今日/昨日股票代码。`_normalize_code` 把各种格式统一为 6 位纯数字：
- `000017.SZ` / `SZ000017` / `000017` → `000017`
- 空/None/纯非数字 → `''`

### 4.6 输出结构

```python
{
    'ref_date': '2026-08-28',
    'data_sufficient': True,
    'total_limit_up': 82,          # 涨停总数
    'total_consecutive': 23,       # 连板总数
    'max_board': 7,                # 最高连板
    'tier_distribution': {         # 梯队分布（板数降序）
        7: [{'code':'000017','name':'深中华A','seal_money_yuan':10069100.0,...}],
        5: [...], 4: [...], 3: [...], 2: [...]
    },
    'tier_counts': {7:1, 5:1, 4:3, 3:4, 2:14},  # 各板数数量
    'dragon_head': {               # 龙头股
        'code':'000017', 'name':'深中华A',
        'board_cnt':7, 'limit_up_reason':'黄金概念+重组预期',
        'seal_money_yuan':10069100.0
    },
    'jinji_rate': 0.444,           # 晋级率
    'jinji_today_continued': 4,    # 今日继续涨停的昨日连板数
    'jinji_yesterday_total': 9,    # 昨日连板股总数
    'temp_score': 90,              # 温度评分
    'temp_level': '🔥 高温（市场极度活跃）',
    'temp_advice': '可积极参与主线题材，注意高位分化风险',
}
```

---

## 五、输出层（reporter.py）

### 5.1 ASCII 报告 `format_linkban_report`

等宽文本格式，适用于控制台打印和 Markdown `text` 代码块：

```
========== 2026-08-28 大盘温度报告 ==========

【连板概况】
  涨停总数: 82只
  连板总数: 23只
  最高连板: 7板

【连板梯队】
  7板: 1只  → 深中华A
  5板: 1只  → 海鸥住工
  4板: 3只  → 华锦股份、合百股份、中电电机
  3板: 4只  → ...
  2板: 14只  → 昊华科技、沃特股份、...(全部列出)

【龙头股详情】
  名称: 深中华A
  代码: 000017
  连板: 7连板
  涨停原因: 黄金概念+重组预期
  封单金额: 0.10亿

【情绪指标】
  晋级率: 44.4%
  温度评分: 90分
  温度等级: 🔥 高温（市场极度活跃）
  操作建议: 可积极参与主线题材，注意高位分化风险
==========================================
```

> 梯队列表**完整列出**所有股票名称，不截断。

### 5.2 CSV 行 `format_linkban_csv_row`

展平为 20 字段单行，用于月度 CSV 持久化：

| 字段 | 说明 |
|------|------|
| run_time | 运行时间 |
| ref_date | 参考日期 |
| total_limit_up / total_consecutive / max_board | 概况 |
| tier_2_cnt ~ tier_5_cnt / tier_ge6_cnt | 梯队计数（≥6板合并） |
| jinji_rate / jinji_molecule / jinji_denominator | 晋级率 |
| temp_score / temp_level | 温度 |
| dragon_code / dragon_name / dragon_board / dragon_reason / dragon_seal_money_yuan | 龙头详情 |
| data_sufficient | 数据是否充足 |

### 5.3 Markdown 全景报告集成

当连板模块与其他模块（预警/轮动）同时运行时，`generate_markdown_report` 在报告末尾拼接连板章节：

```markdown
### 🔥 连板龙头与大盘温度

```text
（ASCII 报告内容）
```
```

---

## 六、持久化（storage.py）

### 6.1 CSV 月度落盘 `log_linkban_signal`

- 文件名：`data/alarming_signals/linkban_YYYY-MM.csv`（按月分，追加模式）
- 同日重复运行：先去除当日旧行再追加新行（保留最新一条）
- 首次写入自动创建表头

### 6.2 历史读取 `read_linkban_history`

读取近 N 个月（含当月）连板 CSV，合并返回 DataFrame，按 `ref_date` 去重（keep=last）。

---

## 七、配置参数（config.py Section 7）

```python
# ===== Section 7: 连板龙头 & 大盘温度 =====
ZZSHARE_TOKEN: Optional[str] = None        # 未配时走匿名（30次/分钟）
LINKBAN_FILTER_ST: bool = True             # 是否过滤 ST/*ST 股
LINKBAN_MIN_BOARD_FOR_TIER: int = 2        # 最小板数计入梯队（≥2板即连板）

# 温度评分阈值表（按 key 降序，首个 value >= key 命中）
TEMP_MAX_BOARD_SCORE: Dict[int, int]       # 最高板 0-40 分
TEMP_TOTAL_SCORE: Dict[int, int]           # 连板总数 0-30 分
TEMP_JINJI_SCORE: Dict[float, int]         # 晋级率 0-30 分

# 温度等级映射（综合分 → 等级 + 操作建议）
TEMP_LEVEL_MAP: Dict[int, Tuple[str, str]]       # 分数→等级
TEMP_ADVICE_BY_LEVEL: Dict[str, str]             # 等级→建议

LINKBAN_CSV_DIR: str = "data/alarming_signals"   # CSV 目录
```

---

## 八、常用运行命令

> 项目使用 `uv` 管理依赖，首次运行前执行 `uv sync` 安装 zzshare 等依赖。

### 8.1 仅跑连板龙头（跳过预警 + 轮动）
```bash
uv run services/alarming_monitor/main.py --linkban-only
```
- 输出：控制台 ASCII 温度报告 + CSV 落盘 + Markdown 落盘
- 耗时：~0.6s

### 8.2 全流程（预警 + 轮动 + 连板）
```bash
uv run services/alarming_monitor/main.py
```
- 连板章节自动拼入全景 Markdown 报告末尾

### 8.3 跳过连板（仅预警 + 轮动）
```bash
uv run services/alarming_monitor/main.py --no-linkban
```

### 8.4 仅跑预警 + 连板（跳过轮动）
```bash
uv run services/alarming_monitor/main.py --no-rotation
```

### 8.5 指定参考日期
```bash
uv run services/alarming_monitor/main.py --linkban-only --date 2026-08-28
```

### 8.6 切换配置档
```bash
uv run services/alarming_monitor/main.py --linkban-only --profile conservative
uv run services/alarming_monitor/main.py --linkban-only --profile strict
```

### 8.7 静默模式（仅写文件，不打印控制台）
```bash
uv run services/alarming_monitor/main.py --linkban-only --quiet
```

### 8.8 不写文件（仅控制台输出）
```bash
uv run services/alarming_monitor/main.py --linkban-only --no-csv --no-report
```

### 8.9 详细日志（调试数据拉取）
```bash
uv run services/alarming_monitor/main.py --linkban-only -v
```

### 8.10 运行单元测试
```bash
uv run pytest services/alarming_monitor/test_linkban.py -v
# 同时跑回归
uv run pytest services/alarming_monitor/test_linkban.py services/alarming_monitor/test_monitor.py
```

---

## 九、输出文件路径

```
trading_lab/
├── data/
│   ├── alarming_signals/
│   │   └── linkban_2026-08.csv          # 连板温度 CSV（按月分，追加模式）
│   └── reports/rotation/
│       └── 2026-08/
│           └── 2026-08-28_rotation.md   # Markdown 全景报告（含连板章节）
```

---

## 十、开发注意事项

1. **主力接口**：`review_uplimit_reason`（涨停复盘），不是 `uplimit_stocks`（仅连板股且数据不全）
2. **字段确认**：zzshare 不同接口返回字段名可能差异，开发时先 `print(df.columns.tolist())` 确认
3. **频率限制**：未配置 Token 时 zzshare 匿名访问 30 次/分钟；如需高频调用，在 config.py 设置 `ZZSHARE_TOKEN`
4. **非交易日处理**：非交易日调用返回空数据，`analyze_linkban` 会标记 `data_sufficient=False`，温度分 30（最低档）
5. **ST 股过滤**：`LINKBAN_FILTER_ST=True` 时连板梯队里剔除 ST/*ST 股
6. **封单金额单位**：`fengdan_money` 原始单位为元（如 189528000.0 = ~1.9 亿元），报告里显示为"亿"
7. **`_suppress_output` 调用约定**：必须用 `_suppress_output(lambda: _zz_call(...))`，不能写成 `with _suppress_output():`
8. **无缓存**：涨停数据不走 `@cached`（同日多行会被 date 去重压扁），每次运行直接调接口
9. **梯队完整列出**：报告里每个板数的所有股票名称全部列出，不截断不省略
