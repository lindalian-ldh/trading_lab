# bankuai-service 新增需求：主力资金动向 + 涨停连板梯队 实现计划

## 一、代码库研究结论

### 1.1 现有代码架构（bankuai-service）

现有系统采用分层架构，职责清晰：

| 模块 | 文件 | 职责 |
|---|---|---|
| 配置层 | [config.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/bankuai-service/config.py) | `ScanConfig` dataclass，所有可调参数集中；`PLATE_TYPE_MAP` 板块类型映射 |
| 数据层 | [data_loader.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/bankuai-service/data_loader.py) | 封装 zzshare SDK，提供 `fetch_plates_rank` / `fetch_plate_stocks` / `fetch_uplimit_stocks`；内置**限频**（request_interval）、**重试**（retry_times）、**缓存降级**（当日→历史回溯30天） |
| 排名模块 | [sector_ranker.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/bankuai-service/sector_ranker.py) | 模块 A：Top N 热门 + Bottom N 冷门板块筛选，合并去重 |
| 龙头识别 | [leader_selector.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/bankuai-service/leader_selector.py) | 模块 B：三级梯队（一级领涨龙/二级中军/三级补涨）；含 `build_uplimit_index` 涨停股池索引构建（字段名容错：`limit_times`/`continuous`/`lian_ban` 等） |
| 调度器 | [scanner.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/bankuai-service/scanner.py) | 主流程：板块排名 → 拉取涨停股池 → 逐板块识别梯队 → 汇总报告 |
| 报告层 | [reporter.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/bankuai-service/reporter.py) | 控制台卡片式输出（热门/冷门板块 + 各板块三级梯队） |
| CLI入口 | [main.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/bankuai-service/main.py) | argparse 解析参数，调用 `scan()`，保存 JSON 报告 + 打印 |
| 测试 | [test_bankuai.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/bankuai-service/test_bankuai.py) | 68+ 单测覆盖：config / sector_ranker / leader_selector（select_tiers/tier1评分/涨停索引/二级三级过滤）/ scanner（失败隔离）/ data_loader（缓存降级） |

**依赖**（[pyproject.toml](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/bankuai-service/pyproject.toml)）：`zzshare`、`pandas>=2.0`

### 1.2 接口可用性核查

通过 zzshare 源码 [client.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/.venv/lib/python3.11/site-packages/zzshare/client.py#L14-L382) SHORTCUTS 表确认：

| 需求接口 | zzshare 是否提供 | 说明 |
|---|---|---|
| `uplimit_hot` | ✅ 是 | `open/review/uplimit/hot`，参数 `date1`, `board` |
| `uplimit_stocks` | ✅ 是（已在用） | `open/review/uplimit/stocks/{date1}`，参数 `date1` |
| `stock_sector_fund_flow_rank` | ❌ 否 | zzshare SHORTCUTS 无此接口；**akshare** 中有同名接口 `stock_sector_fund_flow_rank(indicator="今日")`（来自 grep 确认） |

**决策**：主力资金动向使用 **akshare**（免费无需 token），涨停连板梯队使用现有 **zzshare**（保持一致性，复用已有 retry/cache 机制）。

---

## 二、待修改的文件与模块

### 2.1 文件清单

| 文件 | 改动类型 | 说明 |
|---|---|---|
| `config.py` | 编辑 | ScanConfig 新增 4 个参数（fund_flow 显示条数、连板显示上限、akshare 请求超时、是否启用 akshare） |
| `data_loader.py` | 编辑 | 新增 3 个 fetch 函数：`fetch_sector_fund_flow_rank`（akshare，带限频+重试+缓存）、`fetch_uplimit_hot`（zzshare，复用现有模式）；扩展 `_to_float` / 字段容错 keys |
| `scanner.py` | 编辑 | `scan()` 新增两大块并行（或串行）获取：`fund_flow_data`、`uplimit_ladder_data`，挂到报告 dict 顶层 |
| `reporter.py` | 编辑 | 新增两个输出区块：**💰 主力资金动向**（流入/流出双栏，含关键个股）、**🔥 涨停连板梯队**（按连板数倒序，3板→2板→首板） |
| `main.py` | 编辑 | CLI 新增 `--no-fund-flow`、`--no-uplimit-ladder` 开关控制是否跳过；JSON 报告含新增字段 |
| `pyproject.toml` | 编辑 | 新增 `akshare` 依赖 |
| `test_bankuai.py` | 编辑 | 新增单测：主力资金方向判断、关键个股解析、连板分组、失败隔离、akshare mock |

### 2.2 数据结构设计（报告 dict 扩展）

现有报告结构保留，新增顶层 key：

```python
report = {
    # --- 原有字段（保持不变）---
    "scan_time": ..., "date1": ..., "plate_type": ...,
    "hot_sectors": [...], "cold_sectors": [...],
    "sector_leaders": {...}, "summary": {...},

    # === 新增字段 ===
    # 需求一：主力资金动向
    "fund_flow": {
        "inflow": [               # 主力净流入（按净额降序）
            {
                "direction": "净流入",
                "sector": "半导体",          # 板块名称
                "net_amount": 12.34,          # 主力净流入净额（亿元）
                "change_pct": 3.45,           # 板块涨跌幅%
                "key_stock": {                # 主力净流入最大股
                    "name": "中芯国际",
                    "code": "688981",
                    "net_amount": 2.56        # 亿元
                }
            },
            # ...
        ],
        "outflow": [              # 主力净流出（按净额绝对值降序）
            {
                "direction": "净流出",
                "sector": "白酒",
                "net_amount": -8.76,
                "change_pct": -2.10,
                "key_stock": {
                    "name": "贵州茅台",
                    "code": "600519",
                    "net_amount": -3.21
                }
            },
            # ...
        ],
        "meta": {"source": "akshare_stock_sector_fund_flow_rank", "indicator": "今日"}
    },

    # 需求二：涨停连板梯队
    "uplimit_ladder": {
        "ladders": [              # 按连板数倒序
            {
                "limit_times": 5,            # 连板数
                "label": "5板",
                "stocks": [
                    {"code": "000001", "name": "平安银行", "change_pct": 10.01,
                     "seal_time": "09:35:00", "reason": "金融科技"},
                    # ...
                ]
            },
            {"limit_times": 3, "label": "3板", "stocks": [...]},
            {"limit_times": 2, "label": "2板", "stocks": [...]},
            {"limit_times": 1, "label": "首板", "stocks": [...]}
        ],
        "uplimit_hot_sectors": [   # 可选：uplimit_hot 热门板块（若接口成功）
            {"name": "AI", "uplimit_count": 12, "continuous_head": "XXX 3板"}
        ],
        "meta": {"uplimit_stocks_count": 45, "source": "zzshare_uplimit_stocks"}
    }
}
```

---

## 三、修改步骤与实现细节

### 步骤 1：config.py — 新增配置参数

在 `ScanConfig` dataclass 中追加（与现有风格一致，全部提供默认值，遵循"配置驱动"约定）：

```python
# ===== 主力资金动向 =====
fund_flow_top_n: int = 10              # 展示流入/流出各多少个板块
fund_flow_indicator: str = "今日"      # akshare indicator: "今日"/"3日排行"/"5日排行"/"10日排行"/"20日排行"
fund_flow_timeout: int = 10            # akshare 单次请求超时(秒)
enable_fund_flow: bool = True          # 是否拉取主力资金动向（CLI 开关对应）

# ===== 涨停连板梯队 =====
ladder_show_top_n: int = 5              # 每个连板组最多展示几只股
ladder_min_limit: int = 1               # 最低展示的连板数（1=含首板，2=仅2板及以上）
enable_uplimit_ladder: bool = True      # 是否生成连板梯队
enable_uplimit_hot: bool = False        # 是否额外拉取 uplimit_hot（需更多请求）
```

**风险点**：akshare `stock_sector_fund_flow_rank` 返回列名可能因版本变动 → 字段容错 keys 设计。

### 步骤 2：data_loader.py — 新增 3 个接口获取函数

#### A. `fetch_sector_fund_flow_rank(date1, cfg)` — akshare 主力资金流向排名

**设计要点**：
- 调用方式：`import akshare as ak; ak.stock_sector_fund_flow_rank(indicator=cfg.fund_flow_indicator)`
- 返回是 `pd.DataFrame`（akshare 约定），需转 list[dict] 并标准化列名
- **已知列名容错**（多版本兼容，实测后补充）：
  - 板块名称：`"名称"`, `"板块名称"`, `"板块"`, `"sector"`
  - 主力净流入-净额：`"主力净流入-净额"`, `"主力净流入"`, `"主力净买额"`, `"main_net_inflow"`
  - 主力净流入最大股：`"主力净流入最大股"`, `"最大净流入股"`, `"key_stock"`
  - 涨跌幅：`"涨跌幅"`, `"今日涨跌幅"`, `"change_pct"`
- **金额单位统一**：akshare 返回可能是"元"或"亿元"，需通过数值范围判断（>1e6 视为元 → /1e8 转亿）
- **关键个股解析**：最大股字段可能是 `"中芯国际(688981) 2.56亿"` 或 `"中芯国际 2.56亿"` 或 dict，正则容错提取 code/name/amount
- **缓存机制**：复用 `_save_cache` / `_load_latest_cache`，文件名 `fund_flow_{indicator}.json`
- **限频**：akshare 免费版请求间隔 1s，复用 `_throttle`
- **方向判定纯函数**：抽出 `_classify_direction(net_amount) -> "净流入"/"净流出"`，便于单测

#### B. `fetch_uplimit_hot(date1, cfg)` — zzshare 涨停热门板块（可选）

- 完全复用现有 `fetch_uplimit_stocks` 的模式：`_call_with_retry` + `_throttle` + 缓存降级
- 接口：`api.uplimit_hot(date1=date1, board=cfg.plate_type)`
- 文件名：`uplimit_hot_{cfg.plate_type}.json`
- 用途：补充展示热门板块涨停数和最高连板股；`enable_uplimit_hot=False` 默认跳过（避免多一次请求）

#### C. 扩展 uplimit_stocks 字段提取

现有 `_UPLIMIT_CONT_KEYS` 需补充 `limit_times`：
```python
_UPLIMIT_CONT_KEYS = (
    "limit_times", "continuous", "limit_count", "board_count",
    "lian_ban", "continuous_days", "continue_count", "ct",
)
```
新增涨停原因提取 keys：
```python
_UPLIMIT_REASON_KEYS = ("reason", "limit_reason", "zt_reason", "reason_for", "cause")
```
新增代码/名称提取容错（akshare 和 zzshare 格式差异）：复用现有 `_extract_code` 逻辑。

### 步骤 3：scanner.py — 主调度集成

在 `scan()` 函数内，**板块梯队识别之后**新增两大块（串行，保持简单；每块 try/except 隔离，失败不影响主报告）：

```python
# ---- 新增：主力资金动向 ----
fund_flow_data: dict = {"inflow": [], "outflow": [], "meta": {"error": None}}
if cfg.enable_fund_flow:
    try:
        fund_flow_raw = fetch_sector_fund_flow_rank(date1, cfg)
        fund_flow_data = _process_fund_flow(fund_flow_raw, cfg)  # 纯函数：标准化+分流
    except Exception as e:
        logger.warning("主力资金动向获取失败: %s", e)
        fund_flow_data["meta"]["error"] = str(e)

# ---- 新增：涨停连板梯队 ----
uplimit_ladder_data: dict = {"ladders": [], "uplimit_hot_sectors": [], "meta": {"error": None}}
if cfg.enable_uplimit_ladder:
    try:
        # 复用已拉取的 uplimit_raw（若 scanner 中已有），否则重拉
        stocks_raw = uplimit_raw if (cfg.enable_uplimit_pool and uplimit_raw) else fetch_uplimit_stocks(date1, cfg)
        uplimit_ladder_data = _build_uplimit_ladder(stocks_raw, cfg)  # 纯函数：按 limit_times 分组
        # 可选：uplimit_hot
        if cfg.enable_uplimit_hot:
            uplimit_ladder_data["uplimit_hot_sectors"] = _process_uplimit_hot(fetch_uplimit_hot(date1, cfg))
    except Exception as e:
        logger.warning("涨停连板梯队获取失败: %s", e)
        uplimit_ladder_data["meta"]["error"] = str(e)
```

最后把 `fund_flow` 和 `uplimit_ladder` 两个 dict 挂到 `return` 的 report 中。

**失败隔离设计**（与现有 `identify_leaders` 模式一致）：
- 任一新增模块异常 → 记录 warning，对应 `meta.error` 填字符串，返回空 list，不影响主流程 success_count

### 步骤 4：reporter.py — 新增两个输出区块

#### 💰 主力资金动向区块

```
--------------------------------------------------------------
💰 主力资金动向  [来源: akshare 今日]
--------------------------------------------------------------
📈 主力净流入 Top 5:
  #1  半导体         +12.34亿  涨幅 +3.45%  关键股: 中芯国际 +2.56亿
  #2  算力           + 9.87亿  涨幅 +4.12%  关键股: 寒武纪 +1.89亿
  ...

📉 主力净流出 Top 5:
  #1  白酒           - 8.76亿  涨幅 -2.10%  关键股: 贵州茅台 -3.21亿
  #2  医药商业       - 6.54亿  涨幅 -1.85%  关键股: 药明康德 -2.10亿
  ...
```

金额格式化：`_fmt_amount(v: float) -> str`（亿元，带 +/-，右对齐 8 位，千分位可选）。

#### 🔥 涨停连板梯队区块

```
--------------------------------------------------------------
🔥 涨停连板梯队  [共 45 只涨停]
--------------------------------------------------------------
🏆 5板 (1 只):
    1. 000001 平安银行  +10.01%  封板 09:35  金融科技
     ...
🥇 3板 (3 只):
    1. 000xxx XXXXXX   +10.03%  封板 09:42  AI算力
    ...
🥈 2板 (8 只):
    1. ...
🥉 首板 (33 只, 仅展示前 5):
    1. ...
```

设计：
- 连板数 >= 5 用 🏆，3-4 用 🥇，2 用 🥈，1 用 🥉
- 首板超过 `ladder_show_top_n` 时标注"仅展示前 N"
- 封板时间缺省显示 `"--:--"`，原因缺省省略

### 步骤 5：main.py — CLI 开关 & 报告

新增 argparse 参数（与现有风格一致，均有默认值）：
```python
p.add_argument("--no-fund-flow", action="store_true", help="跳过主力资金动向")
p.add_argument("--no-uplimit-ladder", action="store_true", help="跳过涨停连板梯队")
p.add_argument("--enable-uplimit-hot", action="store_true", help="额外拉取涨停热门板块板块视图(默认关闭)")
p.add_argument("--fund-flow-indicator", default="今日",
               help="主力资金周期: 今日/3日排行/5日排行/10日排行/20日排行，默认今日")
p.add_argument("--fund-flow-top-n", type=int, default=10, help="主力资金展示板块数，默认 10")
```

映射到 `ScanConfig.from_env(...)` 的 override。

### 步骤 6：pyproject.toml — 新增 akshare 依赖

```toml
dependencies = [
    "zzshare",
    "akshare>=1.12",
    "pandas>=2.0",
]
```

### 步骤 7：test_bankuai.py — 新增单测

**新增测试类 7 个**（纯函数优先，mock 网络层）：

| 测试类 | 用例 |
|---|---|
| `TestFundFlowDirection` | `_classify_direction`：正数→净流入，负数→净流出，0→净流入（或未分类），None→默认 |
| `TestFundFlowNormalize` | akshare 多版列名容错（`名称`/`主力净流入-净额`/旧版列名）；金额元→亿转换；关键个股字符串解析正则（中芯国际(688981) 2.56亿 → 正确三元组）；缺字段降级 |
| `TestFundFlowSplit` | `_process_fund_flow`：按净额正负分流，各自排序取 top_n，金额为 0 归哪一侧，空 DataFrame → 空 list |
| `TestUplimitLadderGrouping` | `_build_uplimit_ladder`：按 limit_times 分组，组内按 seal_time 升序，limit_times 缺省→1，空列表→空 ladders，含首板分组 |
| `TestUplimitLadderLimitTimesKeys` | `_extract_continuous` 新增 `limit_times` 字段：多种 key 都能解析，数值类型容错 |
| `TestScannerNewModules` | monkeypatch `fetch_sector_fund_flow_rank` 抛异常 → report.fund_flow.meta.error 非空但主流程成功；monkeypatch 返回有效数据 → inflow/outflow 非空；连板梯队同理 |
| `TestIntegrationAkshare`（`@pytest.mark.skipif` 需 ENABLE_NETWORK_TESTS=1） | 真实调用 akshare `stock_sector_fund_flow_rank` 验证列名解析有效性，打印实际列名便于回归 |

---

## 四、潜在依赖与注意事项

### 4.1 akshare 版本兼容性 ⚠️
- **风险**：akshare 接口 `stock_sector_fund_flow_rank` 历史上多次变更列名（CHANGELOG 确认：0.7.52 / 0.8.87+ fix）
- **应对**：在 data_loader 中设计 4~6 组别名映射（见步骤 2.A），并在集成测试中打印实际列名；若实测发现额外别名，立即补入
- **降级**：全部列名匹配失败时，返回空列表 + warning，不中断主流程

### 4.2 请求量与限频
- **现有请求数**（概念 Top10 + Bottom10，共 ~20 板块）：1(plates_rank) + 1(uplimit_stocks) + 20(plate_stocks) = **22 次 zzshare**
- **新增请求数**：1(akshare fund_flow) + 0/1(zzshare uplimit_hot 可选) = **1~2 次**
- **总计 ~23-24 次**，zzshare 免费版 30次/分钟 上限安全
- akshare 侧：单请求，无压力

### 4.3 日期格式一致性
- zzshare `uplimit_stocks(date1)` 支持 `YYYY-MM-DD`（现有 data_loader 已验证）
- akshare `stock_sector_fund_flow_rank` **不接受 date 参数**（实时接口，indicator 控制周期），`date1` 仅用于缓存归档 key
- **风险点**：非交易时段运行，akshare 返回上一交易日数据 → 应与 `date1` 比对，若缓存已存在且对应同一交易日则跳过请求（可选优化，首版简化：直接拉取即可）

### 4.4 金额单位歧义
- akshare `stock_sector_fund_flow_rank` 的"主力净流入-净额"列可能是元或亿元（不同 indicator 不同）
- **判定启发式**：若板块级金额绝对值 > 1e6 → 判定为"元"→ /1e8 转亿；否则已是"亿元"
- 个股级同理：绝对值 > 1e5 → 元 → /1e8；否则已是亿
- 在集成测试中打印实际量级验证判定

### 4.5 连板数字段名差异
- zzshare `uplimit_stocks` 可能返回 `limit_times` 或 `continuous` 或 `lian_ban`
- **应对**：扩展现有 `_UPLIMIT_CONT_KEYS`（leader_selector.py L137-L140），已在步骤 2.C 设计

---

## 五、风险处理

| 风险 | 概率 | 影响 | 处理策略 |
|---|---|---|---|
| akshare fund_flow 接口列名变更 | 中 | 高（需求一无数据） | 多别名 key 列表 + 集成测试打印列名；全部匹配失败→空降级不中断 |
| akshare 请求超时/被限频 | 低 | 中 | 复用现有 `_call_with_retry` 包装 + akshare 内部请求间隔；失败→缓存降级 |
| zzshare uplimit_stocks 字段缺 `limit_times` | 中 | 中（分组全部落为首板） | `_extract_continuous` 回退：用 `seal_time` 是否缺失 + 历史缓存辅助判断，缺省→1 |
| 新增代码引入回归 | 低 | 高 | 原 68 个单测全部必须通过；新增模块以纯函数为主，mock 层隔离 |
| 整体执行超 cfg.overall_timeout（90s） | 低 | 中 | 新增请求最多 2 个（默认仅 1 个）；`scanner.scan()` 外层已含超时控制，可单独为 fund_flow / ladder 设内部时限；必要时 CLI 可 `--no-fund-flow` 跳过 |
| akshare 与现有依赖冲突 | 低 | 高 | `akshare>=1.12` 与 `pandas>=2.0` 兼容（akshare 官方要求 pandas）；在虚拟环境 `uv pip install akshare` 验证 |

---

## 六、验证方案

### 6.1 单测（离线）
```bash
cd trading_lab
uv run pytest services/bankuai-service/test_bankuai.py -v
```
期望：所有原有 + 新增用例通过（目标 > 75 通过）。

### 6.2 集成测试（在线，可选）
```bash
ENABLE_NETWORK_TESTS=1 uv run pytest services/bankuai-service/test_bankuai.py -v -k "Integration"
```
期望：akshare fund_flow 接口可用、列名解析正确；zzshare uplimit_hot 返回结构验证。

### 6.3 端到端手动验证
```bash
cd trading_lab
uv run services/bankuai-service/main.py --date 2026-08-21
```
控制台检查：
- 看到 "💰 主力资金动向" 区块：有 inflow/outflow 各 Top 10，金额非空，关键个股显示名称+金额
- 看到 "🔥 涨停连板梯队" 区块：按连板数倒序分组，首板数量最多，个股含代码+名称+涨幅
- JSON 报告路径含 `fund_flow` 和 `uplimit_ladder` 字段
- 原"热门板块/冷门板块/龙头梯队"区块不受影响

### 6.4 失败场景验证
```bash
# 断网或 token 无效时
ENABLE_NETWORK_TESTS=1 uv run services/bankuai-service/main.py --date 2026-08-21
```
期望：主报告产出，`fund_flow.meta.error`/`uplimit_ladder.meta.error` 有值，原板块梯队部分按缓存降级正常输出，退出码为 0（若板块梯队成功>0）。

---

## 七、CLI 用法（新增）

```bash
# 默认：全量跑（板块扫描 + 龙头梯队 + 主力资金 + 连板梯队）
uv run services/bankuai-service/main.py

# 跳过主力资金动向
uv run services/bankuai-service/main.py --no-fund-flow

# 跳过涨停连板梯队
uv run services/bankuai-service/main.py --no-uplimit-ladder

# 自定义主力资金周期和条数
uv run services/bankuai-service/main.py --fund-flow-indicator "5日排行" --fund-flow-top-n 5

# 启用 uplimit_hot 热门板块视图（多一次 zzshare 请求）
uv run services/bankuai-service/main.py --enable-uplimit-hot
```
