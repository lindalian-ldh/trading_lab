# crawler_news —— 基于 AkShare 的财经新闻采集与报告生成

> 轻量级盘后批处理工具：抓取 5 个 AkShare 财经资讯接口 → 标注股票代码 → 落盘 JSON Lines → 生成 Markdown 与 HTML 简报（HTML 使用 markdown 库渲染 + 内联 CSS，浏览器直接打开）。跑完即退，无驻留进程。
>
> 设计原则对齐 [requirement.md](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/crawler_news/requirement.md)：原文一字不改、日期为核心索引、股票代码自动标注、采集与报告解耦、批处理模式。

---

## 一、环境依赖

- Python 3.11+（项目 `.venv`）
- `akshare>=1.12`（已在 `pyproject.toml` 声明，`uv sync` 自动安装）
- 复用项目根 `core/logger.py` 与 `config/settings.py`，无独立日志/路径配置

```bash
# 首次安装
uv sync

# 验证 akshare 可用
uv run python -c "import akshare; print(akshare.__version__)"
```

> **代理说明**：若本机设置了 `HTTP_PROXY/HTTPS_PROXY` 指向不可用端口（如 `127.0.0.1:31181`），akshare 的 requests 调用会失败并降级返回空数据。需临时取消代理：
> ```bash
> env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy uv run python services/crawler_news/main.py crawl --date 2026-08-17
> ```

---

## 二、命令清单（可复制即用）

所有命令均从项目根 `trading_lab/` 执行。`--date` 默认昨天（与 `bankuai-service` / `sell_monitor` 一致）。

### 1. 采集（crawl 子命令）

```bash
# 抓取昨日全量（5 个接口：cls / sina / juchao / em_global / em_stock）
uv run python services/crawler_news/main.py crawl

# 抓取指定日期
uv run python services/crawler_news/main.py crawl --date 2026-08-17

# 仅抓取单个数据源（可选：cls / sina / juchao / em_global / em_stock）
uv run python services/crawler_news/main.py crawl --source cls
uv run python services/crawler_news/main.py crawl --source juchao --date 2026-08-17

# 抓取与个股相关的新闻（--symbol 透传给 em_stock / juchao 等接口）
uv run python services/crawler_news/main.py crawl --symbol 600519
uv run python services/crawler_news/main.py crawl --symbol 600519 --date 2026-08-17
```

### 2. 报告生成（report 子命令）

```bash
# 生成指定日期范围的 Markdown 简报
uv run python services/crawler_news/main.py report --start-date 2026-08-01 --end-date 2026-08-17

# 跨月范围（归档月份取自 end_date）
uv run python services/crawler_news/main.py report --start-date 2026-07-01 --end-date 2026-08-17

# 仅生成与指定股票相关的报告（按 mentioned_codes 过滤）
uv run python services/crawler_news/main.py report --start-date 2026-08-01 --end-date 2026-08-17 --symbol 600519

# 指定输出格式（默认 md，html 产出完整 HTML5 文档，浏览器可直接打开）
#   md   -> data/reports/news/2026-08/2026-08-01_2026-08-17_all.md
#   html -> data/reports/news/2026-08/2026-08-01_2026-08-17_all.html （markdown 库渲染 + 内联 CSS）
uv run python services/crawler_news/main.py report --start-date 2026-08-01 --end-date 2026-08-17 --format html

# 仅生成指定数据源的报告（cls / sina / juchao / em_global / em_stock）
# 文件名格式：{start}_{end}_{source}.{md|html}
uv run python services/crawler_news/main.py report --start-date 2026-08-01 --end-date 2026-08-17 --source juchao --format html

# 同时指定数据源 + 股票过滤
# 文件名格式：{start}_{end}_{source}_{symbol}.{md|html}
uv run python services/crawler_news/main.py report --start-date 2026-08-01 --end-date 2026-08-17 --source cls --symbol 600519 --format html
```

### 3. 盘后批处理（run_all.py 串联）

`scripts/run_all.py` 已把 `crawler_news crawl` 纳入盘后流水线：

```bash
uv run python scripts/run_all.py
# 依赖顺序：fetch_klines -> calc_indicators -> crawler_news(crawl) -> generate_report
```

---

## 三、数据源说明

| source_short | AkShare 接口 | 说明 |
|--------------|--------------|------|
| `cls` | `stock_info_global_cls` | 财联社全球快讯 |
| `sina` | `stock_info_global_sina` | 新浪全球财经快讯（替代已移除的 `stock_zh_a_news`） |
| `juchao` | `stock_zh_a_disclosure_report_cninfo` | 巨潮资讯（沪深京公告） |
| `em_global` | `stock_info_global_em` | 东财泛财经资讯 |
| `em_stock` | `stock_news_em` | 东财个股新闻（akshare 1.18.83 存在已知 bug，触发时降级为空） |

- 限频：每个接口调用间隔 ≥ 2 秒（`RATE_LIMIT_SEC`）
- 超时：单次调用硬超时 300 秒（daemon 线程 + `join(timeout)`），超时返回空数据不阻塞主流程
- 失败降级：单个接口异常记录日志并返回空列表，不阻断其他源

---

## 四、输出文件路径约定

```
data/
  raw/news/                                    # 原始 JSON Lines（按月归档）
    2026-08/
      cls_2026-08-17.json                      # 每源每日一个文件，每行一条
      juchao_2026-08-17.json
      ...
  reports/news/                                # Markdown / HTML 简报（按月归档，月份取 end_date）
    2026-08/
      2026-08-01_2026-08-17_all.md             # 全量 Markdown 报告
      2026-08-01_2026-08-17_all.html           # 全量 HTML 报告（--format html）
      2026-08-01_2026-08-17_juchao.html         # 按数据源过滤的 HTML 报告（--source juchao）
      2026-08-01_2026-08-17_600519.md           # 按 symbol 过滤的 Markdown 报告
      2026-08-01_2026-08-17_cls_600519.html     # 同时按 source + symbol 过滤的 HTML 报告
  stock_mapping.json                           # 股票代码↔名称映射表（tagger 反查用）
```

- **幂等写入**：以 `news_id`（`{source}_{date}_{title_hash8}`）为主键，同日同源重复抓取不增加行数
- **跨月加载**：`report` 的 `load_jsonl` 自动扫描 `[start, end]` 涉及的所有月份目录

---

## 五、报告结构（Markdown 五段式）

生成的 `.md` / `.html` 含以下段落：

1. **头部**：日期范围、数据源过滤、股票过滤、总条数
2. **数据源统计**：每个 `source_short` 的条数表
3. **区域分布（国内 / 国外）**：按关键词匹配分类，含条数与占比
4. **被提及最多的 5 个股票代码**：按 `mentioned_codes` 聚类，含名称与提及次数
5. **Top 10 新闻**：按 `publish_date` 升序的时间线列表，每条标注 `[国内]` / `[国外]` 区域标签
6. **全部新闻列表**：超过 200 条时自动省略

**区域分类规则**：标题与正文命中任一国外关键词（美股 / 美国 / 日本 / 美联储 / 特斯拉 / 黄金 / 原油 / 比特币 / 俄乌 / 台海 / 制裁 / 关税 等 70+ 个）即标记为"国外"，否则为"国内"。分类在报告生成时动态计算，不修改落盘数据。

> 事件演化跟踪视图（关键词聚类）暂未实现，留待后续迭代。

---

## 六、测试

```bash
# 全量测试（阶段六/七累计 301 个：sources + tagger + storage + reporter(含 HTML/source) + main + 16 跨层端到端）
uv run pytest services/crawler_news/ -v

# 仅跨层集成测试（mock akshare，不访问网络）
uv run pytest services/crawler_news/test_crawler_news.py -v
```

测试矩阵：

| 文件 | 覆盖范围 |
|------|----------|
| `test_sources.py` | 5 接口字段映射 / 列名漂移容错 / 失败降级 / URL 提取 |
| `test_tagger.py` | 正则提取 / 映射表反查 / 去重 / 接口自带代码合并 |
| `test_storage.py` | 幂等写入 / 跨月加载 / news_id 计算 |
| `test_reporter.py` | 统计 / Top10 / Top5 / 空数据 / >200 条省略 / 跨月路径 / **HTML 渲染结构** / **md↔html 等价** / **source 过滤路径与内容** |
| `test_main.py` | crawl 子命令 / report 子命令 / 退出码 / 性能预算 |
| `test_crawler_news.py` | **跨层端到端**：crawl→tagger→storage→report(md/html/source) 全流程 / 失败降级 / symbol 过滤 / CLI 退出码 / 非法 format 退出码 / **CLI --source 过滤** |

---

## 七、已知限制

1. `em_stock`（`stock_news_em`）在 akshare 1.18.83 存在 `\u` 转义 bug（ArrowInvalid），触发时降级为空列表，不抛异常
2. `--format html` 使用 `markdown>=3.4` 库的 `tables` / `fenced_code` 扩展把 Markdown 转 HTML，并套入内联 CSS 模板；若运行环境缺失 `markdown` 库则自动降级为把原 Markdown 包进 `<pre>` 标签（仍产出合法 HTML，但不渲染表格/样式）
3. 事件演化跟踪视图（关键词聚类）未实现
4. `stock_mapping.json` 当前为最小种子（600519/000001），可由 `bankuai-service` 板块扫描结果沉淀扩展
