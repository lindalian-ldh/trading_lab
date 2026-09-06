# 券商研报摘要服务（yanbao-info）实现计划

## 摘要

在 `services/yanbao-info/` 下新建一个独立服务：输入股票代码，自动获取该股票最近发布的券商研报，下载 PDF 并用 `pdfplumber` 解析，通过关键词+正则规则提取评级、目标价、盈利预测、催化剂、风险等结构化字段，结合一致预期与当前股价产出"短线博弈信号"判断，最终输出 JSON + Markdown 摘要报告。

数据源：**akshare**（研报列表 `stock_research_report_em` + 一致预期 `stock_profit_forecast_ths`）+ **Tencent 实时行情**（当前股价，对齐 project_memory 中"Tencent 为主、adata 兜底"的约定）+ **东方财富 PDF 直链**（`pdf.dfcfw.com`，需模拟浏览器 UA 防盗链）。

所有处理文件与结果落盘到 `data/yanbao/` 下，单次执行 < 60 秒。

---

## 一、当前状态分析

### 1.1 服务结构约定（已确认）

参照 [services/bankuai-service/](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/bankuai-service) 与 [services/crawler_news/](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/crawler_news) 两个最相似的"抓取+解析+报告"型服务，每个服务统一为：

- `main.py` —— `argparse` CLI + 跑完即退，`if __name__ == "__main__": sys.exit(main())`
- `config.py` —— dataclass 配置中心 + `from_env()` 工厂，所有阈值/参数集中，无硬编码（用户硬偏好）
- `data_loader.py` —— 数据源适配层（限频 + 重试 + 降级缓存 + 原始响应落盘）
- 领域逻辑模块（本服务为 `pdf_parser.py` + `extractor.py` + `signal_judge.py`）
- `reporter.py` —— 控制台/文件输出
- `pyproject.toml` —— 子服务依赖面文档（`[tool.uv] package = false`，实际依赖由根 [pyproject.toml](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/pyproject.toml) 统一声明）
- `test_*.py` —— 单元测试（pytest）
- `README.md` —— 使用说明（可选，仅在用户要求时创建）

### 1.2 关键约定（来自 memory 与代码）

- **配置驱动**：所有数值参数（阈值、URL、UA、超时、路径）进 `config.py` dataclass，`main.py` 不出现硬编码数值（硬约束，对齐 user_profile）。
- **路径派生**：`_SERVICE_DIR.parent.parent / "data" / "yanbao" / ...`，不依赖 CWD，便于迁移。
- **项目根上 sys.path**：`_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent`，使 `core.logger` 与 `config.settings` 可用（参考 bankuai-service main.py 第 36-39 行）。
- **加载 .env**：`from dotenv import load_dotenv; load_dotenv(_PROJECT_ROOT / ".env")`。
- **限频保护**：相邻请求间隔 ≥ `request_interval`（默认 2 秒），避免触发 429（对齐 crawler_news `_rate_limit()`）。
- **失败重试（仅网络异常）**：仅对网络异常/超时重试 `retry_times` 次；**业务级空结果（akshare 返回空 DataFrame）不重试、不降级读跨日缓存**，直接走"未找到研报"路径（用户硬要求，避免过度尝试）。
- **CSV 输出偏好**：最终摘要报告 JSON + Markdown 双格式；原始结构化数据可选落 CSV（用户偏好 CSV 胜于 Excel）。
- **性能**：单次执行 < 60 秒（用户硬要求"1 分钟内"）。
- **免费数据源**：所有数据源免费，无付费 API key（硬约束）。
- **"未找到研报"语义**：当 `stock_research_report_em` 返回空、或网络异常重试耗尽、或 PDF 列表无可用链接时，**立即输出"未找到研报"占位报告（JSON + Markdown）并退出码 0**，不强行降级、不多次重试。这是业务正常态而非错误。

### 1.3 数据源实测结论

| 用途 | 接口 | 来源 | 字段 | 备注 |
|------|------|------|------|------|
| 研报列表 | `ak.stock_research_report_em(symbol)` | Eastmoney reportapi | 序号/股票代码/股票简称/报告名称/东财评级/机构/近一月个股研报数/{year}-盈利预测-收益/{year}-盈利预测-市盈率/行业/日期/报告PDF链接 | **akshare 直接调用**，PDF 链接形如 `https://pdf.dfcfw.com/pdf/H3_{infoCode}_1.pdf`，列表已含 EPS/PE 预测但**不含目标价** |
| 一致预期（上调判断） | `ak.stock_profit_forecast_ths(symbol, indicator="预测年报净利润")` | 同花顺 | 预测年度/机构数/预测净利润均值/最小/最大/上年每股收益 | **按个股查询**，可获取一致预期均值用于"盈利预测上调"判断 |
| 一致预期详表 | `ak.stock_profit_forecast_ths(symbol, indicator="业绩预测详表-机构")` | 同花顺 | 机构/预测日期/预测年报每股收益/预测年报净利润 | 按机构列出预测，可用于历史对比 |
| 当前股价 | `https://qt.gtimg.cn/q={market}{code}`（Tencent） | 腾讯 | 实时价格/涨跌幅/总市值/流通市值 | **对齐 project_memory** "Tencent 为主、adata 兜底"，akshare 的 `stock_zh_a_spot_em` 不稳定已弃用 |
| 当前股价兜底 | `adata.stock.market.market_snapshot()` | adata | 收盘价/涨跌幅 | 根 `pyproject.toml` 已含 `adata`（alarming_monitor 备份源） |

**重要修正（来自实测）**：
- ❌ 用户原方案 `ak.stock_profit_forecast_em` 实测**按行业过滤**（symbol 是行业名如"船舶制造"，不是股票代码），无法按个股查询，**不可用**。
- ✅ 改用 `ak.stock_profit_forecast_ths(symbol, indicator="预测年报净利润")` 按个股查询一致预期，可满足"盈利预测上调"判断需求。
- ⚠️ `stock_research_report_em` 列表接口返回的"东财评级"字段是**研报评级**（买入/增持/中性/减持/卖出），不含目标价——目标价需从 PDF 正文提取。

### 1.4 局限性确认（对齐用户"局限性说明"）

| 局限 | 处理方案 |
|------|----------|
| 纯规则提取无语义理解 | 接受，规则为主；后续可扩展 jieba/Sentence-BERT |
| 财务表格格式差异大 | `extractor.py` 内置表头模糊匹配（"营业收入"/"营业总收入"/"营收" 均匹配） |
| 历史对比缺失 | 本地缓存 `data/yanbao/history/{symbol}.json` 存最近 N 份报告快照，支持评级变化与目标价变化对比 |
| 扫描版 PDF | `pdf_parser.py` 检测文本为空时记录 `parse_status="scanned_pdf"`，跳过提取不报错 |
| 网络与防盗链 | PDF 下载带 `User-Agent` + `Referer: https://data.eastmoney.com/` 头，重试 2 次后失败记录 `pdf_download_failed` |

---

## 二、文件结构与模块职责

```
services/yanbao-info/
├── main.py              # CLI 入口（argparse，跑完即退）
├── config.py            # YanbaoConfig dataclass + 路径派生
├── data_loader.py       # 数据获取层（研报列表/PDF下载/股价/一致预期）
├── pdf_parser.py        # pdfplumber 文本+表格提取
├── extractor.py         # 规则与正则提取（评级/目标价/盈利预测等）
├── signal_judge.py      # 短线信号判断逻辑层
├── reporter.py          # JSON + Markdown 报告生成
├── test_yanbao.py       # pytest 单元测试
└── pyproject.toml       # 依赖面文档（package = false）
```

数据目录（自动创建）：

```
data/yanbao/
├── pdfs/                # PDF 原文缓存（{symbol}_{date}_{org}.pdf）
├── cache/               # 解析中间结果（{symbol}_{date}_{org}.json）
├── history/             # 历史评级快照（{symbol}.json，最近 N 份）
└── reports/             # 最终摘要报告（{symbol}_{date}.json + .md）
```

### 2.1 各模块职责

#### `config.py` —— 配置中心（dataclass）

```python
@dataclass
class YanbaoConfig:
    # ===== 取数 =====
    top_n: int = 1                    # 取最近 N 份研报（默认 1）
    request_interval: float = 2.0     # 限频间隔（秒）
    request_timeout: int = 30         # PDF 下载超时（秒，PDF 较大）
    retry_times: int = 2              # 失败重试次数（不含首次）

    # ===== PDF 解析 =====
    pdf_max_pages: int = 50           # 最多解析页数（防超长报告）
    pdf_text_min_chars: int = 100     # 文本小于此值视为扫描版

    # ===== 短线信号阈值（对齐用户"短线博弈信号"定义）=====
    target_price_upside_strong: float = 0.30   # 目标价隐含涨幅 ≥30% 视为强信号
    target_price_upside_moderate: float = 0.15 # 15%-30% 视为中信号
    catalyst_recent_days: int = 30             # 催化剂事件窗口期（天）

    # ===== 评级词汇（用于提取与跳升判断）=====
    rating_buy: list[str] = field(default_factory=lambda: ["买入", "强推", "强烈推荐", "Strong Buy"])
    rating_outperform: list[str] = field(default_factory=lambda: ["增持", "推荐", "优于大市", "Outperform"])
    rating_neutral: list[str] = field(default_factory=lambda: ["中性", "持有", "同步大市", "Neutral"])
    rating_underperform: list[str> = field(default_factory=lambda: ["减持", "Underperform"])
    rating_sell: list[str> = field(default_factory=lambda: ["卖出", "回避", "Sell"])

    # ===== 路径派生（不序列化）=====
    cache_dir: Optional[Path] = None   # None 时由 data_loader 决定（默认 data/yanbao/）

    @classmethod
    def from_env(cls, **overrides) -> "YanbaoConfig": ...
```

#### `data_loader.py` —— 数据获取层

复用 crawler_news 的限频装饰器 `_rate_limit()` 与 bankuai-service 的 `_call_with_retry` 重试模式。

核心函数：

```python
def fetch_report_list(symbol: str, cfg: YanbaoConfig) -> dict:
    """调用 ak.stock_research_report_em，返回包含研报列表与状态的 dict。

    返回结构:
        {
            "status": "ok" | "no_report_found" | "fetch_failed",
            "reason": str,        # 状态说明（如"akshare 返回空 DataFrame"/"网络异常: ..."）
            "reports": list[dict], # 按 publish_date 倒序的研报 dict（status != "ok" 时为空）
            "source": "api" | "cache",  # 实际数据来源
        }

    行为:
        - akshare 调用成功且有数据 → status="ok"，原始响应落盘 data/yanbao/cache/{symbol}_list_{date}.json
        - akshare 返回空 DataFrame（业务级空结果）→ status="no_report_found"，**不重试、不降级读跨日缓存**
        - akshare 调用抛网络异常/超时 → 重试 retry_times 次（仅网络异常重试）；
          全部失败 → status="fetch_failed"，**不降级读跨日缓存**（用户硬要求：找不到研报就直接返回，不过度尝试）

    字段标准化（reports 内每个 dict）:
        title/stock_code/stock_name/org_name/publish_date/rating/
        industry/pdf_url/eps_forecast{year}/pe_forecast{year}/research_report_count
    """

def download_pdf(url: str, symbol: str, publish_date: str, org: str,
                 cfg: YanbaoConfig) -> Optional[Path]:
    """下载 PDF 到 data/yanbao/pdfs/{symbol}_{date}_{org}.pdf。
    - 模拟浏览器 UA + Referer 防盗链
    - 缓存命中检测：文件存在且 size > 0 跳过
    - 重试 retry_times 次，失败返回 None（不抛异常，由调用方降级为"仅用研报列表元数据"）
    """

def fetch_current_price(symbol: str, cfg: YanbaoConfig) -> Optional[float]:
    """获取当前股价（Tencent 主 + adata 兜底）。
    - Tencent: https://qt.gtimg.cn/q={market}{code}（沪 1/深 0 前缀）
    - 返回 None 时 signal_judge 标注"无法获取当前股价"
    - **不重试**：单次请求失败即返回 None（当前股价非主流程必需，缺失只影响"目标价空间"信号）
    """

def fetch_consensus_forecast(symbol: str, cfg: YanbaoConfig) -> dict:
    """调用 ak.stock_profit_forecast_ths(symbol, indicator="预测年报净利润")
    返回 {year: consensus_net_profit, ...} 用于"盈利预测上调"判断。
    - **不重试**：失败时返回空 dict，signal_judge 标注"无法获取一致预期"
    """
```

#### `pdf_parser.py` —— PDF 解析层

```python
def extract_text_and_tables(pdf_path: Path, cfg: YanbaoConfig) -> dict:
    """用 pdfplumber 提取全文文本 + 所有表格。
    返回:
        {
            "full_text": str,          # 全文（按页拼接，页间 \n\n）
            "tables": list[pd.DataFrame],  # 表格列表
            "page_count": int,
            "parse_status": "ok" | "scanned_pdf" | "parse_failed",
            "char_count": int,
        }
    - 文本字符数 < pdf_text_min_chars 时标记 scanned_pdf
    - 最多解析 pdf_max_pages 页（防超长报告耗时）
    """
```

#### `extractor.py` —— 信息提取层（核心规则）

对齐用户"信息提取规则"表，每个字段独立函数 + 一个 `extract_all` 聚合入口：

```python
def extract_rating(text: str) -> str:
    """搜索"评级"/"投资评级"等关键词，提取后面的评级词汇。
    匹配评级词表（config.rating_*），返回标准化评级（买入/增持/中性/减持/卖出）。
    """

def extract_target_price(text: str) -> Optional[float]:
    """搜索"目标价"/"12个月目标价"等，提取后面的数字（如 25.60）。
    正则: r"目标价[^\d]{0,10}(\d{1,3}(?:\.\d{1,2})?)"
    """

def extract_earnings_forecast(tables: list, text: str) -> dict:
    """从财务表格中定位"营业收入"/"归母净利润"行，提取对应年份（如 2024E/2025E）数值。
    表头模糊匹配：营收 = {"营业收入","营业总收入","营收"}；
                 归母净利润 = {"归母净利润","归属母公司净利润","归母净利","净利润"}
    返回 {"营收": {year: val}, "归母净利润": {year: val}}
    """

def extract_core_logic(text: str) -> str:
    """抓取"投资要点"/"核心逻辑"/"投资逻辑"标题下的首段文字（3-5 句）。"""

def extract_catalysts(text: str) -> list[str]:
    """查找"催化剂"/"近期驱动"/"即将落地"关键词，提取后面的事件描述列表。"""

def extract_risks(text: str) -> list[str]:
    """定位"风险提示"/"风险因素"部分，提取列表文本。"""

def detect_first_coverage(title: str, text: str) -> bool:
    """检查标题/正文是否出现"首次覆盖"/"首次深度"字眼。"""

def extract_all(parsed: dict, cfg: YanbaoConfig) -> dict:
    """聚合入口，返回完整提取结果 dict。"""
```

#### `signal_judge.py` —— 短线信号判断层

对齐用户"短线信号判断"表：

```python
def judge_target_price_upside(target_price: float, current_price: float,
                              cfg: YanbaoConfig) -> dict:
    """(目标价 - 当前股价) / 当前股价
    返回 {"upside_pct": float, "signal": "强"|"中"|"弱"|"无法判断"}
    """

def judge_rating_jump(current_rating: str, history: list[dict],
                       cfg: YanbaoConfig) -> dict:
    """若当前评级在 rating_buy 且同机构历史最近一次为中性/持有，
    视为评级跳升。返回 {"is_jump": bool, "from": str, "to": str, "reason": str}
    历史缺失时 reason="无历史对比数据"。
    """

def judge_forecast_revision(latest_forecast: dict, consensus: dict,
                              cfg: YanbaoConfig) -> dict:
    """将最新研报的预测值与一致预期对比，超出 consensus * 1.05 视为上调。
    返回 {"is_revision_up": bool, "latest": float, "consensus": float, "reason": str}
    consensus 缺失时 reason="无法获取一致预期"。
    """

def judge_catalyst_timeliness(catalysts: list[str], cfg: YanbaoConfig) -> dict:
    """检查催化剂是否含"即将"/"预计"/"将于"等近期时间词，
    或具体日期（如"9月"/"三季度"）在 catalyst_recent_days 窗口内。
    """

def judge_signals(report: dict, current_price: Optional[float],
                  consensus: dict, history: list[dict],
                  cfg: YanbaoConfig) -> dict:
    """聚合所有信号判断，返回短线信号 dict（对齐用户 JSON 模板的"短线信号"字段）。"""
```

#### `reporter.py` —— 报告生成

```python
def build_report(symbol: str, report_meta: dict, parsed: dict,
                 extracted: dict, signals: dict, current_price: Optional[float],
                 consensus: dict, cfg: YanbaoConfig) -> dict:
    """组装最终报告 dict，对齐用户 JSON 模板结构。"""

def save_json(report: dict, out_path: Path) -> Path:
    """JSON 落盘，ensure_ascii=False，indent=2。"""

def save_markdown(report: dict, out_path: Path) -> Path:
    """Markdown 落盘，含表格 + 列表结构。"""

def print_console_summary(report: dict) -> None:
    """控制台轻量卡片摘要（评级/目标价/隐含涨幅/短线信号一览）。"""
```

#### `main.py` —— CLI 入口

```bash
# 默认：取最近 1 份研报，输出 JSON + Markdown
uv run services/yanbao-info/main.py --symbol 600036

# 取最近 3 份研报
uv run services/yanbao-info/main.py --symbol 600036 --top-n 3

# 仅输出 JSON
uv run services/yanbao-info/main.py --symbol 600036 --format json

# 跳过 PDF 解析（仅用研报列表元数据，最快）
uv run services/yanbao-info/main.py --symbol 600036 --no-pdf

# 自定义输出路径
uv run services/yanbao-info/main.py --symbol 600036 --out data/yanbao/reports/cmb.json

# 安静模式
uv run services/yanbao-info/main.py --symbol 600036 --quiet
```

参数清单：

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `--symbol` | string | 必填 | 股票代码（如 600036/000001/300750） |
| `--top-n` | int | 1 | 取最近 N 份研报 |
| `--format` | `json`/`md`/`both` | `both` | 输出格式 |
| `--out` | string | 自动 | 输出路径（默认 `data/yanbao/reports/{symbol}_{date}.json/.md`） |
| `--no-pdf` | flag | false | 跳过 PDF 下载与解析，仅用研报列表元数据 |
| `--quiet` | flag | false | 不打印控制台摘要 |
| `--verbose` | flag | false | 详细日志（INFO 级别） |

退出码：
- 0 = 成功（含"未找到研报"场景，视为业务正常态）
- 1 = 失败（未捕获异常/CLI 参数错误）

**"未找到研报"返回 0**：akshare 列表为空或网络异常重试耗尽时，产出占位报告并返回 0，不视为脚本失败。

---

## 三、数据流程（Pipeline）

```
1. fetch_report_list(symbol)
   ├── ak.stock_research_report_em(symbol) → DataFrame
   ├── 列标准化 + 按 publish_date 倒序
   ├── 原始响应落盘 data/yanbao/cache/{symbol}_list_{date}.json
   └── 返回 {status, reason, reports, source}
       │
       ├── status == "no_report_found" / "fetch_failed"
       │   └── ★ 早期退出：生成"未找到研报"占位报告
       │         {
       │           "股票代码": symbol,
       │           "状态": "未找到研报",
       │           "原因": reason,
       │           "建议": "检查股票代码是否正确，或该股票近期无券商研报覆盖",
       │           "生成时间": now
       │         }
       │         落盘 JSON + Markdown，控制台打印"未找到研报: {reason}"
       │         退出码 0（业务正常态）
       │
       └── status == "ok" → 继续 2

2. 取 top_n 份，对每份研报：
   2.1 download_pdf(pdf_url, ...)
       ├── 缓存命中（文件存在且 size > 0）→ 跳过下载
       ├── requests.get + UA + Referer 头
       └── 重试 retry_times 次后失败 → parse_status="pdf_download_failed"
           （PDF 下载失败不阻断，降级为"仅用研报列表元数据"）

   2.2 pdf_parser.extract_text_and_tables(pdf_path)
       ├── pdfplumber 逐页提取文本 + 表格
       ├── 文本 < pdf_text_min_chars → parse_status="scanned_pdf"
       └── 最多 pdf_max_pages 页

   2.3 extractor.extract_all(parsed)
       ├── extract_rating
       ├── extract_target_price
       ├── extract_earnings_forecast（优先表格，回退正文）
       ├── extract_core_logic
       ├── extract_catalysts
       ├── extract_risks
       └── detect_first_coverage

3. fetch_current_price(symbol) → Tencent 主 + adata 兜底
   └── 失败返回 None，不重试

4. fetch_consensus_forecast(symbol) → 一致预期净利润（用于上调判断）
   └── 失败返回空 dict，不重试

5. 加载历史快照 data/yanbao/history/{symbol}.json（用于评级跳升判断）

6. signal_judge.judge_signals(...) → 短线信号聚合

7. 更新历史快照：追加本次报告到 history（保留最近 20 份）

8. reporter.build_report + save_json + save_markdown + print_console_summary
```

**"未找到研报"是业务正常态**：股票可能无券商覆盖、或 akshare 列表为空。此场景直接产出占位报告并退出码 0，不进行过度重试或跨日缓存降级（用户硬要求）。

---

## 四、输出结构（对齐用户 JSON 模板）

```json
{
  "股票代码": "600036",
  "股票简称": "招商银行",
  "研报标题": "招商银行深度报告：财富管理龙头，估值修复可期",
  "发布机构": "中信证券",
  "发布日期": "2026-08-25",
  "行业": "银行",
  "评级": "买入",
  "目标价": 45.00,
  "当前股价": 32.50,
  "隐含涨幅": "38.5%",
  "盈利预测": {
    "营收": {"2024E": "3200亿", "2025E": "3450亿"},
    "归母净利润": {"2024E": "1480亿", "2025E": "1650亿"}
  },
  "一致预期净利润": {"2024E": "1450亿", "2025E": "1600亿"},
  "核心逻辑摘要": "（截取的前几句）",
  "催化剂事件": ["预计9月降息利好银行息差", "零售AUM突破10万亿"],
  "风险提示": ["宏观经济下行", "房地产不良暴露"],
  "短线信号": {
    "首次覆盖": false,
    "盈利预测上调": {"is_revision_up": true, "latest": 1480, "consensus": 1450, "reason": "高于一致预期2.1%"},
    "目标价空间": {"upside_pct": 0.385, "signal": "强"},
    "评级变化": {"is_jump": false, "from": "买入", "to": "买入", "reason": "维持买入"},
    "近期催化剂": {"has_recent_catalyst": true, "items": ["预计9月降息利好银行息差"]}
  },
  "PDF链接": "https://pdf.dfcfw.com/pdf/H3_xxx_1.pdf",
  "解析状态": {
    "pdf_downloaded": true,
    "pdf_pages": 28,
    "pdf_parse_status": "ok",
    "fields_extracted": ["评级", "目标价", "盈利预测", "核心逻辑", "催化剂", "风险", "首次覆盖"]
  },
  "生成时间": "2026-08-26 10:30:00"
}
```

`解析状态` 字段记录每个步骤是否成功，便于排查（对齐项目"explicit parameter naming"偏好）。

---

## 五、依赖管理

### 5.1 子 `pyproject.toml`（依赖面文档）

```toml
[project]
name = "trading-lab-yanbao-info"
version = "0.1.0"
description = "券商研报摘要与短线信号提取（akshare + pdfplumber）"
requires-python = ">=3.11"
dependencies = [
    "pdfplumber>=0.10",
    "akshare>=1.12",
    "requests>=2.31",
    "pandas>=2.0",
]

[tool.uv]
package = false
```

### 5.2 根 `pyproject.toml` 新增

在 `dependencies` 列表追加：

```toml
# 券商研报 PDF 解析 (yanbao-info) 所需
"pdfplumber>=0.10",
```

`akshare`、`requests`、`pandas` 已在根 `pyproject.toml` 中声明，无需重复。

---

## 六、测试策略

`test_yanbao.py` 覆盖以下场景（pytest，参考 crawler_news 的测试粒度）：

| 测试函数 | 覆盖点 |
|---------|--------|
| `test_extract_rating_*` | 评级提取：买入/增持/中性/减持/卖出/缺失 |
| `test_extract_target_price` | 目标价正则提取（含小数/整数/缺失） |
| `test_extract_earnings_forecast` | 营收/净利润从表格与正文双路径提取 |
| `test_extract_catalysts` | 催化剂列表提取 |
| `test_extract_risks` | 风险提示列表提取 |
| `test_detect_first_coverage` | 首次覆盖检测（标题/正文双路径） |
| `test_judge_target_price_upside` | 隐含涨幅信号分级（强/中/弱/无法判断） |
| `test_judge_rating_jump` | 评级跳升判断（含历史缺失场景） |
| `test_judge_forecast_revision` | 盈利预测上调判断（含一致预期缺失场景） |
| `test_judge_catalyst_timeliness` | 催化剂时效性判断 |
| `test_pdf_parser_scanned` | 扫描版 PDF 检测（文本为空标记 scanned_pdf） |
| `test_data_loader_pdf_cache_hit` | PDF 缓存命中跳过下载 |

数据获取层与 PDF 下载层不写单测（依赖网络），仅用 mock 验证降级路径。

---

## 七、执行步骤

1. 创建 `services/yanbao-info/` 目录结构
2. 写 `config.py`（YanbaoConfig dataclass + 路径派生）
3. 写 `data_loader.py`（限频装饰器 + 4 个核心函数 + 降级缓存）
4. 写 `pdf_parser.py`（pdfplumber 封装 + 扫描版检测）
5. 写 `extractor.py`（7 个提取函数 + 聚合入口）
6. 写 `signal_judge.py`（5 个判断函数 + 聚合入口）
7. 写 `reporter.py`（JSON + Markdown + 控制台摘要）
8. 写 `main.py`（argparse + Pipeline 编排）
9. 写 `test_yanbao.py`（pytest 单元测试）
10. 写 `pyproject.toml`（依赖面文档）
11. 根 `pyproject.toml` 追加 `pdfplumber>=0.10` 依赖
12. `uv sync` 安装新依赖
13. `uv run pytest services/yanbao-info/test_yanbao.py -v` 跑测试
14. `uv run services/yanbao-info/main.py --symbol 600036` 端到端验证
15. （可选）写 `README.md`（仅用户要求时）

---

## 八、风险与备选

| 风险 | 备选方案 |
|------|----------|
| PDF 下载被防盗链拦截 | 重试 2 次后改用 `curl_cffi`（已在 .venv 中）模拟浏览器 TLS 指纹 |
| pdfplumber 解析大 PDF 慢 | `pdf_max_pages=50` 限制页数，超出部分跳过 |
| 一致预期接口 `stock_profit_forecast_ths` 限频 | 复用 `_rate_limit()` 间隔 2 秒；失败时标注"无法获取一致预期"，不阻断主流程 |
| 评级词汇未覆盖 | `config.py` 评级词表可配置，用户可追加自定义词汇 |
| 历史快照累积过多 | `history/{symbol}.json` 保留最近 20 份，超出自动淘汰最旧 |
