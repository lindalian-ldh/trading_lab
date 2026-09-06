# 连板龙头 & 大盘温度功能实施方案

## 一、Repo 调研结论

### 现有架构模式（严格复用，不重造轮子）

`alarming_monitor` service 已形成清晰的 **配置 → 数据加载 → 指标/分析 → 报告 → 存储 → 入口编排** 分层：

| 层 | 现有模块 | 新增功能如何嵌入 |
|----|---------|----------------|
| 配置层 | [config.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/config.py) | `AlarmConfig` 追加 Section 7：连板/温度参数（温度档阈值、ST过滤开关、历史CSV目录） |
| 数据加载层 | [data_loader.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/data_loader.py) | 新增 3 个函数：`fetch_uplimit_stocks(date_str)`、`fetch_uplimit_hot(date_str)`、`fetch_uplimit_reason(code, date_str)`，全部走 `cache.py @cached` 双层缓存 |
| 指标/分析层 | [indicators.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/indicators.py) + [rotation.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/rotation.py)（纯函数无 IO） | **新增 `linkban.py`**：纯函数分析层。输入 DataFrame/两个日期数据，输出统一 dict：概况+梯队+龙头+晋级率+温度分+等级+建议 |
| 报告层 | [reporter.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/reporter.py) | 新增 `format_linkban_report(linkban_result)` 生成用户指定文本报告、`linkban_csv_fields` 头 + `format_linkban_csv_row` |
| 存储层 | [storage.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/storage.py) | 新增 `log_linkban_signal` → `data/alarming_signals/linkban_YYYY-MM.csv` 按月追加 |
| 入口编排层 | [main.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/main.py) | 新增 `--linkban-only` 互斥模式 + `--no-linkban` 跳过开关；`_run_once` 新增 模块四（默认启用），拼入全景 Markdown |
| 测试层 | [test_monitor.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/test_monitor.py) | 新增 `class TestLinkban`（mock DataFrame，不触网） |
| 依赖层 | `pyproject.toml` | 新增 `zzshare` 依赖 |

### 关键约束
1. **零硬编码**：所有温度档阈值（7/5/3板，20/10只，50%/30%晋级率）全部进 `config.py`
2. **字段名兼容性**：`uplimit_stocks` 不同版本字段名可能不同（`ts_code`/`ticker`/`code`、`continue_day_cnt`/`连续涨停天数`/`continue_days`），在 `fetch_uplimit_stocks` 中实现 **字段别名归一化**（normalize 为统一小写字段：`code`, `name`, `continue_cnt`, `limit_up_time`, `seal_money`, `limit_up_reason`, `pct_chg`, `is_st`）
3. **缓存**：涨停复盘接口结果加 `cache.py @cached`（TTL=6h，非交易日数据极少变），但默认带 `freshness_check=False`（历史涨停数据不可变，无需每日强制重拉）
4. **参考日自动决策**：沿用现有 `_resolve_default_ref_date` 机制（不重新写），传递给 linkban
5. **非交易日返回空数据**：data_loader 返回 None 或空 df，分析层 `data_sufficient=False`，报告层显示"今日非交易日/数据不足"不抛错

---

## 二、需要新增 / 修改的文件

### 2.1 新增文件（2 个）

| 文件 | 说明 |
|------|------|
| `services/alarming_monitor/linkban.py` | 连板分析纯函数层：字段归一化、过滤ST、梯队分布、晋级率计算、温度评分（0-100）、温度等级映射、操作建议 |
| `services/alarming_monitor/test_linkban.py` | 单元测试（mock 涨停 DataFrame，不触网）。覆盖：空数据非交易日、ST过滤、梯队计数、7/5/3 板各档温度、晋级率（分子分母）、字段别名归一化、龙头股选择规则、3 档温度等级 |

### 2.2 修改文件（6 个）

| 文件 | 修改内容 |
|------|---------|
| `config.py` | `AlarmConfig` 新增 Section 7：连板/温度全部参数（见下方 3.2 详细列表） |
| `data_loader.py` | 新增 3 个函数（见 3.3） + 字段别名归一化辅助函数 `_normalize_uplimit_columns(df)` + 所有接口走 `cache.py @cached` |
| `storage.py` | 新增 `log_linkban_signal(result)` → `data/alarming_signals/linkban_YYYY-MM.csv`（与 alarming 系列同目录，独立按月份） |
| `reporter.py` | 新增 `format_linkban_report(result)`（严格按用户"输出示例"的 `==========` 格式） + 在 `generate_markdown_report` 底部追加一个新的 `### 🔥 连板龙头与大盘温度` 章节（当 linkban_result 传入时） |
| `main.py` | 新增 `--linkban-only`、`--no-linkban` 开关；`_run_once` 新增模块四流程；`_resolve_default_ref_date` 复用；当全模式（预警+轮动+连板）时 Markdown 拼接连板章节；`--linkban-only` 时单独输出文本报告 |
| `pyproject.toml` | dependencies 加 `zzshare`（无版本锁，实测后用户若指定再收紧） |

---

## 三、分步实现方案

### Step 1：依赖与配置（config.py + pyproject.toml）

#### 1.1 `pyproject.toml` dependencies 追加：
```toml
dependencies = [
    ...,
    "zzshare",
]
```

#### 1.2 `AlarmConfig` 新增 Section 7（连板 & 大盘温度）全部参数：

```python
# ===== Section 7: 连板龙头 & 大盘温度 =====
ZZSHARE_TOKEN: Optional[str] = None        # 未配时走匿名（30次/分钟）
LINKBAN_FILTER_ST: bool = True             # 是否过滤 ST/*ST 股
LINKBAN_MIN_BOARD_FOR_TIER: int = 2        # 最小板数计入梯队（≥2板即连板梯队）

# 温度评分：最高板 0-40 分（分档阈值）
TEMP_MAX_BOARD_SCORE: dict = field(default_factory=lambda: {
    # (min_board_ge, score) 按降序首条匹配
    7: 40,  # ≥7板 40分
    5: 30,  # ≥5板 30分
    3: 20,  # ≥3板 20分
    0: 10,  # <3板 10分（兜底）
})
# 温度评分：连板总数 0-30 分
TEMP_TOTAL_SCORE: dict = field(default_factory=lambda: {
    20: 30,  # ≥20只 30分
    10: 20,  # ≥10只 20分
    0:  10,  # <10只 10分
})
# 温度评分：晋级率 0-30 分
TEMP_JINJI_SCORE: dict = field(default_factory=lambda: {
    0.5: 30,  # ≥50% 30分
    0.3: 20,  # ≥30% 20分
    0.0: 10,  # <30% 10分
})
# 温度等级映射（综合分 → 等级文案）
TEMP_LEVEL_MAP: dict = field(default_factory=lambda: {
    70: ("🔥 高温", "可积极参与主线题材，注意高位分化风险"),
    50: ("🌤️ 常温", "市场正常，精选个股操作"),
    0:  ("❄️ 低温", "市场低迷，观望为主或控制仓位"),
})

# 连板 CSV 历史目录
LINKBAN_CSV_DIR: str = "data/alarming_signals"   # 与 alarming_*.csv 同目录
```

说明：为了和用户的需求严格对齐但又能配置化，把 `7/5/3 板`、`20/10 只`、`50%/30%`、`70/50 分` 全部做成 dict 查表（`按 key 降序，第一个满足 key ≤ value/score` 或 `≥` 取分）。

---

### Step 2：数据加载层（data_loader.py）

#### 2.1 新增三个 `fetch_*` 函数，统一签名：

```python
@cached(ttl_hours=6, freshness_check=False, key_prefix="linkban_uplimit")
def fetch_uplimit_stocks(date_ymd: str) -> Optional[pd.DataFrame]:
    """
    获取指定日期涨停股列表。
    date_ymd: 'YYYYMMDD' 字符串
    返回 normalize 后的 DataFrame，统一字段：
      code, name, continue_cnt(int), limit_up_time,
      seal_money(float, 元), limit_up_reason, pct_chg, is_st(bool)
    失败/非交易日空数据 → None
    """
    # 步骤：
    # 1) try: import zzshare; api = zzshare.get_api(cfg.ZZSHARE_TOKEN)
    #    except ImportError: log → None
    # 2) try: df = api.uplimit_stocks(date1=date_ymd)
    #    except Exception as e: log → None
    # 3) df = _normalize_uplimit_columns(df)   ← 字段别名归一化
    # 4) if df is None or empty: return None
    # 5) 数值列安全转型（continue_cnt → int，seal_money → float）
    # 6) return df

@cached(ttl_hours=6, freshness_check=False, key_prefix="linkban_hot")
def fetch_uplimit_hot(date_ymd: str) -> Optional[pd.DataFrame]:
    """涨停热门板块（辅助，连板报告里暂不强依赖）。"""

# 不缓存（单个股查询，结果固定）
def fetch_uplimit_reason(code: str, date_ymd: str) -> Optional[str]:
    """获取个股指定日期的涨停原因（字符串摘要）。失败 None。"""
```

#### 2.2 字段别名归一化辅助（核心健壮性设计）

`_normalize_uplimit_columns(df)`：对输入 df 的列做 **别名表查找**，所有字段映射为统一小写键。别名表在函数里硬编码为 dict（可随测试继续扩展）：

```python
_COL_ALIASES = {
    "code":       ["ts_code", "ticker", "code", "股票代码", "证券代码"],
    "name":       ["name", "股票名称", "证券简称", "名称"],
    "continue_cnt": ["连续涨停天数", "continue_day_cnt", "continue_days", "连板天数", "连续板数"],
    "limit_up_time": ["limit_up_time", "首次涨停时间", "涨停时间"],
    "seal_money": ["seal_money", "封单金额", "封单资金", "封单(元)"],
    "limit_up_reason": ["limit_up_reason", "涨停原因", "概念", "题材"],
    "pct_chg":    ["pct_chg", "涨跌幅", "涨幅"],
    "is_st":      ["is_st", "ST标记", "是否ST"],
}
```
对每一个目标列，从 `df.columns` 找第一个别名命中的列；命中失败补 `None/NaN/False`（is_st 默认 False）。

---

### Step 3：纯函数分析层（linkban.py · 新增文件）

输入：
- `today_df`：今日 `fetch_uplimit_stocks` 返回（或 None/空）
- `yesterday_df`：昨日 `fetch_uplimit_stocks` 返回（或 None/空，用于晋级率）
- `cfg`：`AlarmConfig` 配置

输出（**统一 dict 结构**，所有可选 None 都要给默认值便于 reporter 稳定渲染）：

```python
{
    'ref_date':       'YYYY-MM-DD',
    'data_sufficient': bool,        # False 时报告显示"数据不足/非交易日"
    'reason':        str,            # 不足原因
    # —— 连板概况 ——
    'total_limit_up': int,           # 当日涨停总数(所有涨停)
    'total_consecutive': int,        # 连板股总数(continue_cnt ≥ 2)
    'max_board':      int,           # 最高连板数(0 表示无)
    # —— 连板梯队 ——
    'tier_distribution': {
        7: [{'code': '000017.SZ', 'name': '深中华A'}, ...],
        5: [...],
        4: [...],
        3: [...],
        2: [...],
    },  # key 从高到低。只包含 ≥2 的板数，空的不出现
    'tier_counts': {7: 1, 5: 2, 4: 3, 3: 5, 2: 7},  # 同 key，仅计数
    # —— 龙头股详情 ——
    'dragon_head': {
        'code':   str,
        'name':   str,
        'board_cnt': int,
        'limit_up_reason': str,   # 优先用 uplimit_stocks 自带字段，缺失时调 fetch_uplimit_reason 回查
        'seal_money_yuan': float,  # 封单金额（元）
    } or None,   # 无连板 → None
    # —— 情绪指标 ——
    'jinji_rate':  float or None,  # 昨日连板(≥2板)股今日继续涨停的比例
    'jinji_today_continued': int,  # 晋级数(分子)
    'jinji_yesterday_total': int,  # 昨日连板股总数(分母)
    'temp_score':  int,            # 0-100
    'temp_level':  str,            # "🔥 高温（市场极度活跃）"
    'temp_advice': str,            # 操作建议文案
}
```

#### 3.1 龙头股识别规则（防"并列最高板"时选哪一只）：
按优先级：
1. `continue_cnt` 最高（板数越多越龙一）
2. `seal_money` 封单金额最大（封不住的不算龙一）
3. 首次涨停时间最早（一字板优先）

#### 3.2 晋级率计算规则：
- 分母 = `yesterday_df` 中 `continue_cnt ≥ LINKBAN_MIN_BOARD_FOR_TIER (2)` 的股票数量（**注意 code 的归一化格式统一**：今日 df 的 `code` 可能是 `000017.SZ` / `000017`，昨日 df 也同理，比较前统一 strip `.SZ/.SH`，转 6 位纯数字）
- 分子 = 分母集合 ∩ `today_df` 中任意涨停的股票集合（continue_cnt ≥ 1 即可，不要求还 ≥2）
- 分母为 0 → `jinji_rate = None`，温度评分时按 0 档计算（得 10 分）

#### 3.3 温度评分：
按用户的 3 段公式，但**读配置表实现**（不硬编码 7/5/3 等）：对每个分项，**按配置表 key 从大到小遍历**，找到第一个满足 `value ≥ key` 的分档，取对应分数。代码结构：

```python
def _lookup_score(value, table: dict, min_score_when_none=10):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return min_score_when_none
    for threshold in sorted(table.keys(), reverse=True):
        if value >= threshold:
            return table[threshold]
    return min_score_when_none
```

综合分 = `_lookup(max_board, cfg.TEMP_MAX_BOARD_SCORE)` + `_lookup(total_consecutive, cfg.TEMP_TOTAL_SCORE)` + `_lookup(jinji_rate, cfg.TEMP_JINJI_SCORE)`。

温度等级按同样查表法（key=综合分阈值，对应 `(level_text, advice_text)`）。

---

### Step 4：报告层（reporter.py）

#### 4.1 新增 `format_linkban_report(result: dict) -> str`：
**严格匹配用户"输出示例"的文本模板**，即：
- 首行 `========== YYYY-MM-DD 大盘温度报告 ==========`
- `【连板概况】` 显示涨停总数、连板总数、最高连板
- `【连板梯队】` 按板数从高到低，每行 `N板: M只  → 名称1、名称2、名称3（≥4只时超过显示为 "..." 后缀不截断太多，取前 5 个 + "等M只"）`
- `【龙头股详情】` 6 行：名称 / 代码 / 连板 / 涨停原因 / 封单金额（以"亿"为单位，seal_money_yuan/1e8 保留 2 位小数）
- `【情绪指标】` 晋级率（分母=0 显示 "—"）、温度评分、温度等级、操作建议
- 末行 `==========================================`

#### 4.2 修改 `generate_markdown_report(alarm, rotation, linkban=None)`：
在末尾追加新章节（当 linkban 非空时）：
```markdown
### 🔥 连板龙头与大盘温度

<直接放 format_linkban_report 的文本，用 ``` 代码块包裹以保留等宽对齐，或保持纯文本（按用户偏好选更可读的形式）>
```

说明：因为 linkban 的输出是 **等宽对齐的 ASCII 报告**，放在 Markdown 的 \`\`\`text 代码块里最稳，不会被 Markdown 表格渲染破坏格式。

#### 4.3 新增 `LINKBAN_CSV_FIELDS` + `format_linkban_csv_row(result)`：
字段：`run_time, ref_date, total_limit_up, total_consecutive, max_board, tier_2_cnt, tier_3_cnt, tier_4_cnt, tier_5_cnt, tier_ge6_cnt, jinji_rate, jinji_molecule, jinji_denominator, temp_score, temp_level, dragon_code, dragon_name, dragon_board, dragon_reason, dragon_seal_money_yuan, data_sufficient`

---

### Step 5：存储层（storage.py）

新增：
```python
def log_linkban_signal(result: dict) -> Path:
    """写入 data/alarming_signals/linkban_YYYY-MM.csv，按月追加。"""
```
实现：与 `log_alarm_signal` 完全镜像的逻辑（路径派生 → 首行写表头 → DictWriter 追加 → 同日不做去重，因为每次都重跑相同参考日不常用；如要去重则在写前读文件并替换同日期行——本期先不做，与 alarming_*.csv 同口径）。

---

### Step 6：入口层（main.py · 命令行 + 流程编排）

#### 6.1 命令行参数新增：
在互斥组里加 `--linkban-only`（与 `--rotation-only` 并列：即互斥组三选一：预警/轮动/连板 三档 only），并单独加 `--no-linkban` 跳过开关。

同时在 `_parse_args` 的示例里补上：
```
示例:
  仅连板报告:  %(prog)s --linkban-only
  跳过连板:    %(prog)s --no-linkban
```

#### 6.2 `_run_once` 新增 模块四（连板）流程：
```python
# --------- 模块四：连板龙头 & 大盘温度 ---------
linkban_result = None
run_linkban = not args.no_linkban and not args.rotation_only  # --rotation-only 和 --linkban-only 互不包含
if run_linkban:
    from linkban import analyze_linkban
    today_df = fetch_uplimit_stocks(_ymd(args.date))
    yesterday_date = _prev_trading_day(args.date)   # 直接 date 减1天（非交易日 df 为空，晋级率会正确处理）
    yesterday_df = fetch_uplimit_stocks(_ymd(yesterday_date))
    linkban_result = analyze_linkban(today_df, yesterday_df, args.date, cfg)

    # 非静默 + 仅连板模式 → 打印等宽报告
    if not args.quiet and not run_alarms and not run_rotation:
        from reporter import format_linkban_report
        print(format_linkban_report(linkban_result))
```

注意：`_prev_trading_day` 这里不做交易日历判断，直接减 1 天——原因是如果"昨日"是非交易日，`uplimit_stocks` 会返回空数据，晋级率分母会是 0（数据不足），温度评分会自动按低档处理，结果正确但会带一些 "—"。如果后续用户要求精确到前一交易日，再接入交易日历或 CSV 历史回溯。

#### 6.3 全模式 Markdown 拼接：
当 `alarm_result` + `rotation_result` + `linkban_result` 都存在时，`generate_markdown_report`（签名改为接受 `linkban_result=None`）会自动拼接连板章节。

#### 6.4 CSV 落盘：
新增：
```python
# 3. 连板 CSV
if run_linkban and not args.no_csv and linkban_result and linkban_result.get('data_sufficient'):
    try:
        path = log_linkban_signal(linkban_result)
        if not args.quiet:
            print(f'🔥 连板数据已记录: {path}')
    except Exception as e:
        logger.warning("写连板 CSV 失败: %s", e)
```

---

### Step 7：测试层（test_linkban.py · 新增文件）

全部 mock，**不触网、不依赖 zzshare**。构造 2~3 个典型 DataFrame（今日/昨日）测试：

- `test_empty_today_non_trading_day`：今日 df 空 → `data_sufficient=False`，温度分 30（全最低档），低温等级
- `test_st_filtered_out`：今日 df 有 1 只 ST + 1 只正常 → ST 被正确过滤，连板计数不包含 ST
- `test_max_board_7`：构造 7 板龙一 → `max_board=7`，最高板得分 40，温度档至少 ≥ 高温（其余两档都走中/高的话）
- `test_tier_distribution_and_sort`：构造多个 2/3/4/5/7 板的股票 → 梯队计数正确、板数降序、龙头股按"板数→封单→首次涨停时间"优先级选出
- `test_jinji_rate_computation`：构造昨日连板 10 只（3 板×2、2 板×8），今日其中 5 只继续涨停 → 晋级率 50%，得 30 分；分母为 0 时 jinji_rate=None
- `test_field_alias_normalization`：构造列名为 "ts_code/连续涨停天数/封单金额" 的 df（模拟老版本 zzshare）→ 归一化后字段正确命中，结果一致
- `test_temp_level_hot`：max_board=7（40）+ consecutive=20（30）+ jinji_rate=0.5（30）→ score=100，level=高温；score=70 也是高温
- `test_temp_level_mild_low_borderlines`：score=49 → 低温；score=50 → 常温；score=69 → 常温；score=70 → 高温；边界全部覆盖

共约 8 个用例，运行时间 < 1s（不触网）。

---

## 四、潜在依赖/风险 & 处理方案

| 风险 | 影响 | 处理方案 |
|------|------|---------|
| `zzshare` 未安装或 import 失败 | 连板模块不可用，其他模块应继续正常 | `data_loader.fetch_uplimit_stocks` 里 `ImportError` 捕获 → log + 返回 None；`linkban.analyze_linkban` 里 today_df=None → data_sufficient=False，永不抛错。入口层非 linkban-only 模式不应因 linkban 而退出 |
| zzshare 版本字段名变化（用户明确提示过） | 字段归一化失败，连板数全是 0 | `_normalize_uplimit_columns` 里做**宽松别名表**（8 个字段每个都有 3~4 种候选名）。所有 miss 的字段补 NaN/None，分析层里 `continue_cnt` 缺省时视为 0（不算连板梯队） |
| zzshare 30 次/分钟频率限制（无 Token） | 每次运行最多调 2 次（今日 + 昨日），加上 `@cached(6h)` → 正常一天内多次运行仅 2 次真实调用 → 不会碰上限 | 缓存 TTL=6h 已保证；用户首跑 19 只 ETF 轮动 + 2 次涨停共 ~21 次，依然 < 30 次/分钟 |
| 首次运行晋级率分母=0（昨天非交易日 / 缓存空） | 温度分里晋级率那项只拿 10 分（兜底） | reporter 里 jinji_rate 显示 "—"，温度分正确，不抛错 |
| 参考日是周末/节假日 | uplimit_stocks 返回空 df → data_sufficient=False | reporter 显示"今日非交易日或数据不足"，不报错 |
| `uplimit_reason` 字段在 uplimit_stocks 里缺失 | 龙头股的"涨停原因"一栏空白 | 先读 uplimit_stocks 自带 `limit_up_reason`，缺失再调 `stock_uplimit_reason(code, date)` 回查；回查失败显示 "—" |
| 现有 `generate_markdown_report` 签名不兼容（之前只接受 2 参数） | 全模式调用会报错 | 签名改为 `def generate_markdown_report(alarm_result, rotation_result, linkban_result=None)`，老代码传 2 个参数也能正常运行（linkban_result 默认 None，不渲染章节） |
| 等宽文本报告在 Markdown 里格式被破坏（自动换行/表格对齐） | 报告不美观 | 包裹在 \`\`\`text 代码块里，终端和 GitHub 都会按等宽渲染 |

---

## 五、验收标准

1. **命令可用**：
   - `uv run services/alarming_monitor/main.py --linkban-only` → 单独输出 `==========` 格式的大盘温度报告，不报错
   - `uv run services/alarming_monitor/main.py`（默认全流程）→ 输出原有预警+轮动，底部追加 `### 🔥 连板龙头与大盘温度` 代码块章节
   - `uv run services/alarming_monitor/main.py --no-linkban` → 原有行为不变，不加载 linkban 相关模块与调用

2. **功能指标齐全**：报告中必须出现 7 大块元素（涨停总数、连板总数、最高连板、各梯队分布与个股名、龙头6字段、晋级率、温度分+等级+建议），缺失字段显示 "—" 不空位

3. **数据正确**（以 8-29 实际运行一次为准）：连板梯队 + 龙头股 + 晋级率 与东财/同花顺涨停复盘的公开数据对比不明显偏离

4. **测试覆盖**：`test_linkban.py` 全部通过 + 原有 `test_monitor.py` 68/68 保持通过（回归）

5. **配置可改**：修改 `config.py` 中 `TEMP_MAX_BOARD_SCORE`（如把 40 分档调到 ≥6 板）再跑，温度分变化符合预期（不重写代码即可调参）

6. **落盘有效**：首跑后 `data/alarming_signals/linkban_2026-08.csv` 被创建，表头 + 一行数据齐整
