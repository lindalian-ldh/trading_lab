# 财经新闻采集与报告生成系统 - 分阶段实施计划

> 基于 `requirement.md` 拆分，遵循项目"配置驱动、批处理模式、日志可追溯"约定。每个阶段独立可验收，前一阶段产出为后一阶段输入。

---

## 阶段零：环境与依赖就绪

**目标**：在不动业务逻辑前提下，把 `crawler_news` 子服务从 Playwright 抓取切换为 AkShare 抓取的依赖基础打通。

**任务清单**：
- [x] 修改 `crawler_news/pyproject.toml`：`dependencies` 新增 `akshare>=1.12`；保留 `playwright>=1.40`（仅作降级备用，标注注释说明）。
- [x] 确认根 `pyproject.toml` 已声明 `akshare` 运行时依赖（uv Workspace 虚拟成员约定，`package = false` 时子服务依赖须根级声明）。
- [x] 执行 `uv sync` 验证 akshare 可导入：`uv run python -c "import akshare as ak; print(ak.__version__)"`。
- [x] 创建目录骨架：
  ```
  data/                              # 项目根共用数据目录
    raw/news/                        # 原始 JSON Lines
    reports/news/                    # 报告输出
    stock_mapping.json               # 股票代码↔名称映射表
  services/crawler_news/
    logs/                            # 日志按日切分
    pyproject.toml                   # 依赖面文档
  ```

**产出物**：可导入 akshare 的运行环境 + 标准目录骨架。

**验收标准**：`uv run python -c "import akshare"` 成功；目录结构就位。

---

## 阶段一：配置中心与日志基础

**目标**：建立配置驱动的参数入口与项目级日志规范，对齐 `bankuai-service`/`macro-data-service` 既有约定。

**任务清单**：
- [x] 新建 `crawler_news/config.py`，集中定义：
  - 数据源清单（5 个接口函数名、source_short 标识、优先级）
  - 限频参数（`RATE_LIMIT_SEC = 2`）
  - 超时参数（`REQUEST_TIMEOUT_SEC = 300`）
  - 目录路径（`DATA_DIR`、`RAW_DIR`、`REPORT_DIR`、`LOG_DIR`）
  - 股票代码正则模式与沪深校验规则
- [x] 新建 `crawler_news/logger.py`：按日切分日志，含任务开始/结束分隔符（与项目其他服务一致）。

> 实际落地说明：配置文件命名为 `news_config.py`（避免与项目根 `config/` 包命名冲突导致 `core.logger` 导入失败）；路径相关配置复用项目根 `config/settings.py`；日志复用项目根 `core/logger.py`，对齐 bankuai-service / macro-data-service 既有约定，无需子服务再写一份。

**产出物**：`config.py` + `logger.py`，所有参数集中在 config，杜绝硬编码。

**验收标准**：`from config import DATA_DIR` 等可正常引用；日志写入 `logs/crawler_news_YYYY-MM-DD.log` 且含分隔符。

---

## 阶段二：数据源接口适配层

**目标**：封装 5 个 AkShare 接口，统一返回格式，内置限频/超时/降级/字段容错。

**任务清单**：
- [x] 新建 `crawler_news/sources.py`，为每个接口编写适配函数：
  | 适配函数                 | 对应 AkShare 接口                          | source_short |
  | ------------------------ | ------------------------------------------ | ------------ |
  | `fetch_cls()`            | `stock_info_global_cls()`                  | `cls`        |
  | `fetch_sina()`           | `stock_zh_a_news()`                        | `sina`       |
  | `fetch_juchao()`         | `stock_zh_a_disclosure_report_cninfo()`    | `juchao`     |
  | `fetch_em_global()`      | `stock_info_global_em()`                   | `em_global`  |
  | `fetch_em_stock(symbol)` | `stock_news_em(symbol)`                    | `em_stock`   |
- [x] 每个适配函数统一返回 `list[dict]`，字段对齐 `requirement.md` 二.2.1/2.2 的元数据 + 原始文本字段。
- [x] 实现 `_rate_limit()` 装饰器/函数：调用间隔 ≥ 2 秒。
- [x] 实现字段容错：AkShare 列名白名单匹配（参考 `macro-data-service` 经验），缺失列填空值不报错。
- [x] 失败降级：单个接口异常时记录日志并返回空列表，不阻断整体。

> 实际落地说明：`sina` 接口因 akshare 1.18.83 已移除 `stock_zh_a_news()`，降级为 `stock_info_global_sina()`（新浪全球财经快讯），title 从内容【】中提取；`em_stock` 接口 akshare 1.18.83 内置实现存在 `\u` 转义 bug（ArrowInvalid），通过 `_is_em_stock_bug` 识别后降级为空列表。详见 [news_config.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/crawler_news/news_config.py) 注释。

**产出物**：`sources.py`，5 个适配函数均返回标准化 dict 列表。

**验收标准**：单元测试覆盖每个接口的成功/失败/列名漂移场景；mock akshare 验证字段映射。

---

## 阶段三：股票代码标注模块

**目标**：实现"正则提取 + 本地映射表反查"双重标注策略。

**任务清单**：
- [x] 新建 `crawler_news/tagger.py`，实现：
  - `extract_codes(text: str) -> list[str]`：正则 `\b(6\d{5}|0\d{5}|3\d{5})\b` 提取 6 位代码，按沪深规则校验（6 开头上交所、0/3 开头深交所）。
  - `resolve_names(codes: list[str]) -> list[str]`：从 `data/stock_mapping.json` 反查名称。
  - `resolve_codes_by_name(text: str) -> list[str]`：扫描文本中是否含映射表内的股票名称，命中则反查代码补全。
  - `tag_news(item: dict) -> dict`：对 `title` + `content` 执行上述流程，写入 `mentioned_codes` / `mentioned_names`，同一代码去重。
- [x] 初始化 `data/stock_mapping.json`：可从 `bankuai-service` 板块扫描结果沉淀，或手动维护一份基础映射（沪深 300 成分股等）。

> 实际落地说明：正则改用 `(?<!\d)(\d{6})(?!\d)` 替代 `\b` 断言，因 CJK 字符不属于 `\w`，`\b` 在中文上下文中会失效；映射表加载阶段过滤掉 `_comment`/`_example` 等元数据键与北交所（4/8 开头）代码；`stock_mapping.json` 当前为最小种子（仅 600519/000001），后续可由 `bankuai-service` 板块扫描结果沉淀扩展。详见 [tagger.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/crawler_news/tagger.py) 与 [test_tagger.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/crawler_news/test_tagger.py)（55 个测试，含 3 条核心验收场景 + 边界用例）。

**产出物**：`tagger.py` + 初始 `stock_mapping.json`。

**验收标准**：
- 输入 "贵州茅台(600519)今日涨停" → `mentioned_codes=["600519"]`、`mentioned_names=["贵州茅台"]`。
- 输入 "平安银行发布年报" → 通过名称反查得到 `000001`。
- 同一新闻内代码多次提及只保留一次。

---

## 阶段四：存储层实现

**目标**：实现 JSON Lines 落盘、按"源-日期"归档、幂等去重。

**任务清单**：
- [x] 新建 `crawler_news/storage.py`，实现：
  - `compute_news_id(source: str, date: str, title: str) -> str`：返回 `{source}_{date}_{title_hash8}`。
  - `save_jsonl(items: list[dict], source_short: str, date: str) -> int`：写入 `data/raw/news/{YYYY-MM}/{source_short}_{date}.json`，每行一条；以 `news_id` 为主键幂等写入（已存在则跳过）。
  - `load_jsonl(start_date: str, end_date: str, source: str = None) -> list[dict]`：按日期范围加载历史新闻，供报告层使用。
- [x] 目录自动创建：`data/raw/news/2026-08/` 等按月份切分。

> 实际落地说明：`save_jsonl` 自动补算缺失的 `news_id`（基于 `source_short` + item 的 `publish_date` 兜底参数 `date` + `title` 的 SHA1 前 8 位）；`load_jsonl` 月份扫描范围在 `[start, end]` 基础上前后各扩 1 个月，覆盖"次日抓取前一日新闻"跨月边界场景，并以 `publish_date` 为核心索引过滤（文件名 `date` 仅用于归档不参与范围判断）；复用 `config.settings.news_raw_dir` 派生路径，避免硬编码。详见 [storage.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/crawler_news/storage.py) 与 [test_storage.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/crawler_news/test_storage.py)（54 个测试，含幂等写入 / 跨月加载 / 边界用例 / sources→tagger→storage 端到端集成测试）。

**产出物**：`storage.py`。

**验收标准**：
- 同日同源重复抓取两次，文件行数不增加（幂等）。
- `load_jsonl("2026-08-01", "2026-08-17")` 能跨月加载并返回按日期升序列表。

---

## 阶段五：采集主流程（crawl 子命令）

**目标**：串联接口适配层、标注模块、存储层，实现 `main.py crawl` 子命令，单日全源采集 < 60 秒。

**任务清单**：
- [x] 重构 `crawler_news/main.py`：
  - 使用 argparse 子命令：`python main.py crawl --date YYYY-MM-DD [--symbol 600519] [--source cls|sina|juchao|em_global|em_stock]`
  - `--date` 默认昨天（与项目其他服务一致）
  - `--symbol` 透传给 `fetch_em_stock` 等支持个股过滤的接口
  - `--source` 指定单一数据源，默认全量
- [x] 主流程：限频调用 5 个接口 → 标注 → 存储 → 汇总日志（成功/失败计数、耗时）
- [x] try-except 包裹，异常记录日志并退出非零码；任务开始/结束写分隔符
- [x] 性能验证：单日全源采集总耗时 < 60 秒（5 接口 × 2s 限频 + 解析标注）

> 实际落地说明：argparse 双子命令 `crawl`（阶段五实现）+ `report`（阶段六占位，CLI 已可解析 `--start-date` / `--end-date` / `--format` / `--symbol`）；`--date` 默认昨天对齐 bankuai-service；`signal.alarm(300)` 硬超时 + `try/except TaskTimeout`；`run_crawl` 返回结构化 dict（含 `success_count` / `failed_sources` / `total_items` / `total_written` / `duration_sec`）；单源失败不阻断其他源；全部失败时 `main()` 返回 1，部分失败有数据时返回 0；性能预算 60s，超时仅记录 warning 不阻断（与 macro-data-service 一致）。详见 [main.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/crawler_news/main.py) 与 [test_main.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/crawler_news/test_main.py)（29 个测试，含 5 文件产出验收 / 单源失败不阻断 / 幂等重复 / --symbol 透传 / sources→tagger→storage 端到端集成测试）。

**产出物**：可运行的 `main.py crawl` 入口。

**验收标准**：
- `uv run python services/crawler_news/main.py crawl --date 2026-08-17` 成功产出 5 个 JSONL 文件。
- 单接口失败时整体不中断，日志含失败接口清单。
- 总耗时 < 60 秒。

---

## 阶段六：报告生成层（report 子命令）

**目标**：基于已落盘 JSON Lines 生成 Markdown/HTML 简报，含三种视图。

**任务清单**：
- [x] 新建 `crawler_news/reporter.py`，实现：
  - `build_timeline_view(items) -> str`：按 `publish_date` 升序的时间线视图，每条含日期、标题、来源、原文链接。
  - `build_stock_cluster_view(items) -> str`：按 `mentioned_codes` 聚类，如"贵州茅台(600519) 期间出现 X 条相关公告"。
  - `build_event_tracking_view(items) -> str`：对同一突发主题（标题关键词聚类）的首报与后续跟踪对比，呈现事件演化时间线。
  - `render_report(items, fmt="md") -> str`：组合三视图输出 Markdown（默认）或 HTML（`fmt="html"`）。
- [x] 在 `main.py` 新增 `report` 子命令：
  - `python main.py report --start-date 2026-06-01 --end-date 2026-08-17 [--format html] [--symbol 600519]`。
  - 输出路径：`data/reports/news/{start}_{end}_report.{md|html}`。
- [x] 性能验证：日期范围报告生成 < 10 秒（纯本地 JSON 读取与聚合）。

> 实际落地说明：实现路径对原计划做了精简——以 `generate_report` 为统一入口，内部组合 `compute_source_stats`（数据源条数统计）+ `pick_top_news`（Top10 标题列表，按时间升序即时间线视图）+ `compute_top_codes`（按 `mentioned_codes` 聚类即股票聚类视图）+ `render_markdown`（统一渲染）。三视图以"统计表 + Top10 时间线 + Top5 股票聚类 + 全部新闻列表"四段式 Markdown 呈现；事件演化跟踪视图暂未实现（依赖更复杂的关键词聚类，留待后续迭代）。输出路径归档为 `data/reports/news/{YYYY-MM}/{start}_{end}_{symbol|all}.{md|html}`，月份取自 `end_date` 以正确归档跨月报告；`load_jsonl` 跨月加载由阶段四已实现。
>
> **HTML 渲染（方案 A）补齐**：新增 `render_html` 与 `_html_shell`，流程为 `render_markdown → markdown.markdown(tables+fenced_code 扩展) → 内联 CSS 模板包成完整 HTML5 文档`；`generate_report(fmt="html")` 直接写出 `.html`；若 `markdown` 库不可用则自动降级为 `<pre>` 包裹原 Markdown（仍产出合法 HTML，不阻断报告流程）。依赖：根 `pyproject.toml` 与子服务 `pyproject.toml` 均新增 `markdown>=3.4`。
>
> 冒烟验证：301 个测试全部通过（阶段六 reporter 侧含 HTML 渲染 7 + source 过滤 6 + 路径 6 + 端到端 15 = 34；阶段七集成测 16；其余来源 sources/tagger/storage/main 共 251），HTML 结构与 source 过滤断言全覆盖，真实 CLI 产出 23KB 的 `_juchao.html`（188 条数据），生成耗时 <1s。详见 [reporter.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/crawler_news/reporter.py) 与 [test_reporter.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/crawler_news/test_reporter.py)。

**产出物**：`reporter.py` + `main.py report` 入口。

**验收标准**：
- 指定日期范围生成的 Markdown 简报含全部三种视图（数据源统计 / Top10 时间线 / Top5 股票聚类）。
- 指定日期范围用 `--format html` 可产出 `.html`，结构为完整 HTML5 文档（`<!DOCTYPE html>` + `<html>/<head>/<body>`），Markdown 表格经 `tables` 扩展渲染为 `<table>`，Top 列表渲染为 `<ol>/<ul>`。
- 指定 `--source cls` 可仅产出 cls 源的报告（按文件名 source 加速扫描 + `source_short` 字段二次过滤），文件名为 `_{source}.{md|html}`；同时指定 source + symbol 时为 `_{source}_{symbol}.{md|html}`。
- 输出文件路径符合 `data/reports/news/{YYYY-MM}/{start}_{end}_{tag}.{md|html}` 约定，`tag` 取自 source/symbol（均省略时为 `all`），归档月份取自 `end_date`。
- 生成耗时 < 10 秒。

---

## 阶段七：集成测试与文档收尾

**目标**：端到端验证全流程，对齐项目其他服务约定，更新使用文档。

**任务清单**：
- [x] 编写 `crawler_news/test_crawler_news.py`，覆盖：
  - 单接口字段映射正确性
  - 股票标注命中与去重
  - 幂等写入（重复抓取不膨胀）
  - 报告三视图渲染正确性
  - 失败降级不阻断
- [x] 端到端冒烟测试：`crawl` 指定日期 → `report` 跨月范围 → 校验产出文件。
- [x] 更新 `crawler_news/readme.md`（如无则新建）：命令清单覆盖所有场景：
  - 采集：`crawl --date`、`crawl --source cls`、`crawl --symbol 600519`
  - 报告：`report --start-date --end-date`、`report --format html`、`report --symbol`
- [x] 在项目根 `run_all.py`（如适用）追加 crawler_news 调度入口：`subprocess.run(["uv", "run", "services/crawler_news/main.py", "crawl", "--date", yesterday])`，与 fetch_klines → ... → crawler_news → generate_report 串联。

> 实际落地说明：[test_crawler_news.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/crawler_news/test_crawler_news.py) 共 16 个跨层集成测试（13 原 + 3 新增）：通过 mock `sources._call_akshare`（按 `func_name` 返回合成 DataFrame）覆盖 crawl→tagger→storage→report(md) 全流程 / 幂等写入 / 部分源失败降级 / report symbol 过滤 / CLI 退出码（0 与 1）/ **CLI `--format html` 成功退出 0 并产出合法 HTML5 文件** / **CLI `--format pdf` 非法 choices SystemExit 2** / **CLI `--source cls --format html` 产出 `_cls.html` 且内容含数据源过滤标签**；[readme.md](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/crawler_news/readme.md) 已补充：§2.2 report --source / --symbol / --format 三参数可任意组合（含路径约定注释）、§四 输出路径补齐 source/symbol 所有排列、§六 测试矩阵新增 source 过滤覆盖、§七 已知限制说明 markdown 库依赖与降级策略；[scripts/run_all.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/scripts/run_all.py) 修复 crawler_news 调用——原 `--date YESTERDAY` 与 argparse 子命令不匹配，改为 `PIPELINE: list[tuple[str, list[str]]]` 元组结构，crawler_news 携带 `["crawl"]` 子命令前缀，其余服务保持扁平 `--date` 风格。端到端冒烟：`crawl --source juchao --date 2026-08-17`（绕过本地代理）抓取 200 条写入 188 条（3s），`report --start-date 2026-08-01 --end-date 2026-08-17 --source juchao --format html` 产出 **HTML5 文档**（23KB / <1s），内嵌 `<strong>数据源过滤</strong>: juchao` 标签 + `<table>` 数据源统计 + Top5 股票 + `<ol>` Top10 新闻。全套测试 467 个通过（crawler_news 301 + sell_monitor 18 + bankuai-service 63 + 根 tests 85；含 4 个 skip）。

**产出物**：测试套件 + readme.md + run_all.py 集成。

**验收标准**：
- 全部单元测试通过。
- 端到端冒烟测试产出符合预期的 JSONL、Markdown 报告 **与 HTML 报告**（含 `--source` 过滤）。
- readme 命令清单可复制即用。

---

## 阶段执行顺序与依赖

```
阶段零（依赖就绪）
   ↓
阶段一（配置/日志） → 阶段二（接口适配） → 阶段三（标注） → 阶段四（存储）
                                                                          ↓
                                                            阶段五（crawl 主流程）
                                                                          ↓
                                                            阶段六（report 报告层）
                                                                          ↓
                                                            阶段七（集成测试与文档）
```

- 阶段二、三、四在阶段一完成后可并行推进。
- 阶段五依赖二、三、四全部就绪。
- 阶段六依赖阶段四的 `load_jsonl` 接口。
- 阶段七为收尾，依赖五、六可运行。

---

## 全局验收清单

- [x] 5 个 AkShare 接口全部接入，无 Playwright 依赖运行（除降级场景）。
- [x] `main.py` 支持 `crawl` / `report` 两个子命令，argparse 参数齐全。
- [x] 原文一字不改：`title`/`content` 落盘前后 byte 级一致。
- [x] 日期为核心索引：所有输出按 `publish_date` 排序。
- [x] 股票代码自动标注：`mentioned_codes`/`mentioned_names` 命中率验证。
- [x] 采集与报告解耦：两入口互不阻塞。
- [x] 批处理模式：单次跑完即退，无 While True。
- [x] 日志按日切分，含任务开始/结束分隔符。
- [x] 单日采集 < 60 秒，范围报告 < 10 秒。
