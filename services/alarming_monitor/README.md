# Alarming Monitor — A股多维度量化预警与板块轮动系统

个人版 A 股市场大跌预警监控 + 板块轮动分析一体化工具。整合 **7 大原子风险信号（S1\~S7）**、**ADX 市场状态动态过滤**、**RRG 板块轮动四象限 + 5 维评分卡** 三大核心模块，输出 Markdown 全景报告与 CSV 历史记录。

***

## 一、系统架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                           main.py (入口)                             │
│  参数解析 · 参考日自动决策 · 流程编排 · 落盘/打印                    │
└──────────────┬──────────────────────────┬───────────────────────────┘
               │                          │
      ┌────────▼────────┐       ┌─────────▼──────────┐
      │  模块一：预警    │       │  模块三：板块轮动   │
      │  S1~S7 + ADX    │       │  RRG + 5 维评分     │
      └────────┬────────┘       └─────────┬──────────┘
               │                          │
      ┌────────▼────────┐       ┌─────────▼──────────┐
      │  monitor.py     │       │  analysis.py       │
      │  信号聚合引擎    │       │  批量分析+排名Δ     │
      └────────┬────────┘       └─────────┬──────────┘
               │                          │
               │          ┌───────────────▼───────────────┐
               │          │     rotation.py               │
               │          │  时间轴对齐 / RRG象限 / 5维评分 │
               │          └───────────────┬───────────────┘
               │                          │
      ┌────────▼──────────────────────────▼──────────┐
      │            indicators.py (纯函数，无 IO)      │
      │  S1~S7 计算 / ADX(Wilder) / RS-Ratio / WMA   │
      └──────────────────────────┬───────────────────┘
                                 │
                      ┌──────────▼──────────┐
                      │   data_loader.py    │
                      │  腾讯主源+东财补今日 │
                      │  + akshare 两融汇总  │
                      │  (周/月线由indicators│
                      │   resample 聚合)   │
                      │  (cache.py 双层缓存)│
                      └──────────┬──────────┘
                                 │
        ┌────────────────────────┼────────────────────────┐
        │                        │                        │
┌───────▼───────┐     ┌─────────▼────────┐    ┌───────────▼───────────┐
│  storage.py   │     │  reporter.py     │    │     notifier.py       │
│ CSV 按月分文件 │     │ 控制台/Markdown  │    │ 控制台(预留飞书/邮件) │
└───────────────┘     └──────────────────┘    └───────────────────────┘
```

### 核心文件职责

| 文件                                                                                                                               | 职责                                                                                                                                                                                                                                                       | <br /> |
| -------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :----- |
| [config.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/config.py)              | 配置中心：三档配置（default/conservative/strict）、所有阈值、ETF 篮子、指数代码、降仓位映射、RRG 参数、5 维评分权重                                                                                                                                                                             | <br /> |
| [main.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/main.py)                  | 入口：argparse 命令行解析、参考日自动决策（数据驱动）、全流程编排、落盘触发                                                                                                                                                                                                               | <br /> |
| [data\_loader.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/data_loader.py)   | 数据获取层：指数日线（S7 需 260 日窗口）、ETF 日线（腾讯主源→东财快照补今日→adata 回退）、两市总成交额（新浪 spot）、今日涨跌家数（legu）、全市场两融汇总（akshare SSE 区间+SZSE 日快照，单位统一为元）；全局 ETF 快照批量缓存（15\~20s 一次全量拉取复用）。周/月线聚合在 indicators 内 resample 完成                                                             | <br /> |
| [cache.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/cache.py)                | 双层缓存装饰器 `@cached`：历史层（>30 天前永久 CSV）+ 当日层（≤30 天 20h TTL）；含 freshness\_check（缓存最新日<今日强制重拉，最小重拉间隔防频繁触网）                                                                                                                                                     | <br /> |
| [indicators.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/indicators.py)      | 纯函数指标层：S1\~S7 7 大信号（S7 含日线→周/月线 resample + 国内版 KDJ 计算 + 高位死叉识别）、Wilder ADX（talib 加速+纯 numpy 降级）、RS-Ratio（归一化 WMA）、RS-Momentum（ROC）、ETF 换手率代理                                                                                                             | <br /> |
| [monitor.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/monitor.py)            | 信号聚合引擎：ADX 动态切换 red\_count 阈值、震荡市 S4/S5/S6 加权 ×1.5（S7 作为中长周期形态信号不参与加权）、按 POSITION\_ADVICE 降序匹配仓位建议                                                                                                                                                       | <br /> |
| [rotation.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/rotation.py)          | 板块轮动纯函数：`align_to_benchmark` 时间轴对齐（基准交易日锚定+ffill）、`classify_rrg_quadrant` RRG 四象限、`calc_5d_score` 5 维加权评分卡、`analyze_single_etf` 单 ETF 完整分析                                                                                                               | <br /> |
| [analysis.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/analysis.py)          | 批量轮动分析：`run_rotation_analysis` 逐 ETF 跑流程 + 按 5 维分排序 + 读取 `rotation_rank_last.json` 计算排名 Δ；`build_rotation_table_rows` 生成 Markdown 表格行                                                                                                                    | <br /> |
| [storage.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/storage.py)            | CSV 持久化：`alarming_YYYY-MM.csv` 预警历史按月追加；`breadth_YYYY-MM.csv` 涨跌家数自累积（legu 仅今日，CSV 重建 20 日序列）；`margin_YYYY-MM.csv` 两融汇总自累积；`linkban_YYYY-MM.csv` 连板记录。**幂等**：按 `ref_date`/`date` 去重，同交易日重复运行不产生重复行；**原子写**：所有写盘走 tmp + `os.replace`，并发/崩溃不产生坏文件。旧 CSV 表头缺列时按新表头重写兼容 | <br /> |
| [reporter.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/reporter.py)          | 输出层：`print_summary` 控制台红绿灯摘要卡片；`format_csv_row` 展平 25 字段 CSV 行（含 s6\_net\_leverage/s6\_red、s7\_k\_value/s7\_red；旧表头缺列时 storage 自动按新列重写兼容）；`generate_markdown_report` 全景 Markdown 报告（严格按模板，亮灯信号显示 red\_reason 子分类）；`save_markdown_report` 按 YYYY-MM 子目录落盘 | <br /> |
| [notifier.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/notifier.py)          | 通知抽象层：`BaseNotifier` + `ConsoleNotifier`（当前唯一实现）；`get_notifier('console')` 工厂，预留飞书 / 邮件 / 微信扩展点                                                                                                                                                          | <br /> |
| [test\_monitor.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/test_monitor.py) | 单元测试：构造 mock DataFrame 不触网覆盖 S1\~S7、ADX 计算、动态阈值、震荡市加权、时间轴对齐、RRG 四象限、5 维评分、Markdown 模板合规、storage 幂等去重与原子写                                                                                                                                                                  | <br /> |
| [test\_margin.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/test_margin.py)   | 单元测试：S6 两融杠杆异常专测（数据不足 / 过热分支 / 去杠杆分支 / OR/BOTH 合并模式 / CSV 归一化与表头兼容）                                                                                                                                                                                      | <br /> |

***

## 二、模块一：7 大原子预警信号（S1\~S7）

每个信号返回统一结构：`{name, label, red(bool), value, threshold, detail, data_sufficient, red_reason}`；`red_reason` 仅在红灯时填充简短子分类（如 S6 的"过热"/"去杠杆"、S7 的"周线高位死叉"），数据不足时 `red=False, data_sufficient=False`（灰灯，不阻断）。亮灯信号行展示格式：`SX（名称·red_reason）`。

### S1 成交额 / 总市值占比过高

- **逻辑**：`两市总成交额 / A 股总市值 > TURNOVER_RATIO_THRESHOLD`

- **默认阈值**：3.0%（`TURNOVER_RATIO_THRESHOLD=0.030`）

- **总市值来源**：新浪 spot 无总市值列时，用 `TOTAL_MARKET_CAP=1.05e14`（105 万亿）配置常量兜底

- **成交额来源**：新浪指数 spot 的 `sh000001 + sz399001` 成交额列求和（元）

### S2 放量不涨

- **逻辑**：`近5日日均成交额 > 近20日日均成交额 × VOLUME_HIGH_MULT` **且** `沪深300近5日涨幅 ≤ INDEX_FLAT_PCT`

- **默认阈值**：量比 ×1.3，涨幅 ≤0

- **口径修正**：日均 ÷ 日均（旧版"5 日总量 vs 20 日日均"存在量级差 ×5 会持续误灯）

### S3 涨跌家数比从高位快速回落

- **逻辑**：`max(ad_ratio[-20:]) > AD_HIGH_REF` **且** `ad_ratio[-1] < AD_LOW_THRESHOLD`

- **默认阈值**：20 日内曾 > 2.5，今日跌破 1.0

- **历史序列来源**：legu 仅提供今日快照，由 `storage.append_breadth_today + read_breadth_history` 自累积 CSV 重建近 20 日序列（升序，末值今日）

### S4 高股息逆势走强

- **逻辑**：`沪深300近20日涨幅 < 0`（市场跌）**且** `高股息篮子绝对正收益` **且** `(高股息篮子涨幅 - 成长篮子涨幅) > DIVIDEND_SPREAD_PCT`

- **默认阈值**：高股息绝对收益 > 0，超额成长 > 3%

- **高股息篮子**（2 只）：银行ETF(512800)、公用事业ETF(516070)

- **成长篮子**（9 只）：芯片 / 计算机 / 机器人 / 创新药 / AI / 半导体 / 卫星 / 航空航天 / 军工

### S5 成长股破位

- **逻辑**：`成长篮子近20日涨幅 < GROWTH_BREAK_PCT` **或** `成长篮子今日均价 < 近20日均价均值`（跌破 20 日均线）

- **默认阈值**：近 20 日涨幅 < -5%

### S6 两融杠杆异常（融资融券资金面信号）

通过跟踪全市场融资融券余额的变化与杠杆水平，识别资金面"快速加杠杆（过热）"或"被动去杠杆（踩踏前兆）"两种异常状态。S6 与 S1~~S5 相互正交：S1~~S5 看的是价格 / 量能 / 板块轮动等市场表现维度，S6 看的是**杠杆资金**这一风险偏好最高的资金群体——他们一进一退往往领先于指数拐点。

- **逻辑**：分**过热**与**去杠杆**两条分支，各自有 1\~4 个子条件；任一分支命中即亮本分支；最终由 `MARGIN_RED_MODE`（默认 `OR`）合并 → 任一方向亮即 S6 亮红灯

- **过热分支**（任一命中即亮，提示"杠杆资金过度涌入，回调风险积累"）

  - 融资余额 5 日增速 > `MARGIN_GROWTH_5D`（默认 3%，快速加杠杆）

  - 融资余额 20 日增速 > `MARGIN_GROWTH_20D`（默认 8%，中期杠杆抬升）

  - 融资买入额 / 两市成交额 > `MARGIN_BUY_RATIO`（默认 12%，杠杆交易过热）

  - (融资余额 - 融券余额) / 总市值 > `MARGIN_NET_LEVERAGE`（默认 1.8%，净多头杠杆过高）

- **去杠杆分支**（任一命中即亮，提示"杠杆资金被动撤离，可能引发踩踏"）

  - 融资余额 5 日回撤 < `MARGIN_DELEV_5D`（默认 -2%）

  - 融资余额 10 日回撤 < `MARGIN_DELEV_10D`（默认 -3.5%）

- **红灯合并模式**：`MARGIN_RED_MODE`

  - `OR`（默认）：过热 OR 去杠杆任一命中即亮红

  - `BOTH`：仅双向共振（过热 AND 去杠杆同时命中）才亮红，适合做"极端信号"口径

- **value 取值优先级**：`净多头杠杆水平 → 5 日增速 → NaN`；`threshold` 对应同一指标的阈值，便于 CSV/Markdown 一致展示

#### 数据来源与获取流程

全市场两融汇总由 `data_loader.fetch_margin_summary` 组合 akshare 的两个接口得到，**单位统一为元**：

| 数据片段               | 主接口                                                   | 单位       | 备注                                                                                    |
| ------------------ | ----------------------------------------------------- | -------- | ------------------------------------------------------------------------------------- |
| 上交所（SSE）两融区间汇总     | `akshare.stock_margin_sse(start_date, end_date)`      | 元（int）   | 一次拉取 25+ 自然日，含融资余额 / 融资买入额 / 融券余额等                                                    |
| 深交所（SZSE）当日快照      | `akshare.stock_margin_szse(date=YYYYMMDD)`            | 亿（float） | 拉失败时回退 `stock_margin_detail_szse` 明细汇总；周末 / 节假日 / 数据未出可能报 length mismatch，自动 fallback |
| 总市值（S6 净杠杆分母）      | 复用 S1 的 `cfg.TOTAL_MARKET_CAP`（默认 1.05e14 元 = 105 万亿） | 元        | 与 S1 一致，避免维护两套兜底常量                                                                    |
| 两市成交额（S6 融资买入占比分母） | 复用 S1 拉取的新浪 `sh000001 + sz399001` 成交额                 | 元        | 一次拉取双用                                                                                |

获取流程（`fetch_margin_summary(ref_date, lookback_days=25, prefer_csv_df)`）：

1. **优先 CSV 自累积**：`storage.read_margin_history(days=25)` 读近 3 个月的 `margin_YYYY-MM.csv`，若覆盖到最新日且行数足够，则免拉 SSE（最热路径，0 触网）
2. **CSV 不覆盖或行数不足 → 补拉 SSE 区间**：`ak.stock_margin_sse(start_date, end_date)`，按列重命名（中文→`rzye/rzmre/rqye/rqmcl/rzrqye/rqyl`），单位走 `_margin_rows_to_yuan` 归一化（启发式：最大绝对值 < 1e6 视为亿，×1e8 转元）
3. **SZSE 当日快照补加**：循环今日 / 昨日 / 前日 3 天（应对非交易日 / 数据未出），拉到即停；按 `date` 与 SSE 序列合并——同日则数值列相加，未匹配则追加一行
4. **CSV 落盘**：`storage.append_margin_row` 把最新一行写入 `data/alarming_signals/margin_YYYY-MM.csv`（同日去重、按月分文件、跨月拼接）
5. **返回**：升序 DataFrame，列含 `date, rzye, rzmre, rqye, rzrqye, rqmcl, rqyl`，单位全为元；任一关键源失败返回 None（S6 降级为灰灯）

#### 配置参数（`config.AlarmConfig` 第 8 节）

```python
MARGIN_LOOKBACK_DAYS: int = 25              # SSE 区间拉取回溯天数（>20 保证 20 日增速可用）
MARGIN_USE_HISTORY_CSV: bool = True         # 优先拼接 CSV 自累积的近 20 日
MARGIN_GROWTH_5D: float = 0.030             # 过热：融资 5 日增速 > 3%
MARGIN_GROWTH_20D: float = 0.080            # 过热：融资 20 日增速 > 8%
MARGIN_BUY_RATIO: float = 0.12              # 过热：融资买入额/两市成交额 > 12%
MARGIN_NET_LEVERAGE: float = 0.018          # 过热：净多头杠杆 > 1.8%
MARGIN_DELEV_5D: float = -0.020             # 去杠杆：5 日回撤 < -2%
MARGIN_DELEV_10D: float = -0.035            # 去杠杆：10 日回撤 < -3.5%
MARGIN_RED_MODE: str = "OR"                 # 合并模式: OR / BOTH
MARGIN_CSV_DIR: str = "data/alarming_signals"
```

#### 数据不足与降级

| 失败场景                        | 信号表现                                        | 不阻断整体流程 |
| --------------------------- | ------------------------------------------- | ------- |
| `margin_df` 为空 / 缺 `rzye` 列 | `red=False, data_sufficient=False`（灰灯）      | ✓       |
| `rzye` 有效行 < 6（无法算 5 日增速）   | 灰灯                                          | ✓       |
| SSE 拉取失败、SZSE 近 3 日均失败      | 退化为仅 SSE 序列；SSE 也失败则灰灯                      | ✓       |
| 总市值 / 两市成交额缺失               | `net_leverage` / `buy_ratio` 子条件跳过，不阻断其他子条件 | ✓       |

***

### S7 大盘 KDJ 高位死叉（周 / 月线中长周期形态信号）

通过沪深300日线**聚合到周线 / 月线**后计算国内版 KDJ 指标，识别"周线 K>80 时下穿 D"或"月线 K>80 时下穿 D"这种典型的**高位死叉**形态。S1\~S6 看的是当前日级别的量价/涨跌/板块/杠杆状态，S7 看的是**几周甚至几个月维度累积的超买顶部转折信号**——时间尺度更长，用来预警"趋势性大跌"而不是日内回调。

- **逻辑**：复用 `BREADTH_INDEX`（沪深300）日线数据 → `pandas.resample` 聚合周线('W')/月线('ME') → 国内版 KDJ 计算 → 在最近 `KDJ_DEATH_CROSS_LOOKBACK`（默认 3）个周期内扫描**高位死叉**（`K[i-1] ≥ D[i-1]` 且 `K[i] < D[i]` 且 `K[i-1] > KDJ_OVERBOUGHT`）→ 周/月两周期按 `KDJ_RED_MODE`（默认 `OR`）合并为最终红灯

- **默认阈值**（`config.AlarmConfig` 第 9 节）：

| 参数                                   | 默认值         | 含义                                      |
| ------------------------------------ | ----------- | --------------------------------------- |
| `KDJ_RSV_PERIOD`                     | 9           | RSV（未成熟随机值）计算周期                         |
| `KDJ_K_SMOOTH` / `KDJ_D_SMOOTH`      | 3 / 3       | K 和 D 的平滑周期（国内惯例 SMA(3,1)）              |
| `KDJ_OVERBOUGHT`                     | 80.0        | 高位阈值：死叉前一期 K > 80 才认定为"高位"死叉            |
| `KDJ_DEATH_CROSS_LOOKBACK`           | 3           | 死叉识别窗口（最近 N 个周期内发生过即算，避免"刚死叉那期数据未出就漏判"） |
| `KDJ_LOOKBACK_DAYS`                  | 260         | 日线拉取窗口（≥ 52 周 / 12 月，覆盖 1 年交易日足够）       |
| `KDJ_USE_WEEKLY` / `KDJ_USE_MONTHLY` | True / True | 是否启用周线 / 月线（任一关则仅用另一周期）                 |
| `KDJ_RED_MODE`                       | OR          | 合并模式：`OR`（任一周期高位死叉即红）/ `AND`（双周期共振才红）   |

- **国内版 KDJ 计算公式**（`_calc_kdj` 纯 numpy 自实现）：

```
RSV[i] = (close[i] - min(low[i-n+1..i])) / (max(high[i-n+1..i]) - min(low[i-n+1..i])) * 100
K[i] = (K[i-1] * (k_smooth - 1) + RSV[i]) / k_smooth   # 初始 K[0]=50
D[i] = (D[i-1] * (d_smooth - 1) + K[i])  / d_smooth   # 初始 D[0]=50
J[i] = 3*K[i] - 2*D[i]
```

- **高位死叉识别要点**：

  1. 只判定"最近 KDJ\_DEATH\_CROSS\_LOOKBACK（3）个周期"内是否发生过死叉——老历史的死叉不算（避免"2024 年牛市顶死叉但现在已是 2026 年"这种假阳性）
  2. 死叉必须**前一期 K 在高位**（`K[i-1] > 80`）——低位死叉（如 K 从 30 下穿 25）是"从弱走更弱"，不是高位反转，不亮红灯
  3. 周线/月线独立判定后按 `KDJ_RED_MODE` 合并：`OR` 任一中即红，`AND` 需双周期共振（更严苛的大顶信号）

- **red\_reason 子分类**（亮灯信号行展示）：

| 场景                               | red\_reason 示例                         |
| -------------------------------- | -------------------------------------- |
| 仅周线高位死叉                          | `周线高位死叉（K 94.9→84.9）`                  |
| 仅月线高位死叉                          | `月线高位死叉（K 92.1→79.8）`                  |
| 双周期共振（`KDJ_RED_MODE=AND` 才需同时命中） | `双周期高位死叉（周线K 94.9→84.9；月线K 92.1→79.8）` |

#### 实现流程（`indicators.check_kdj_divergence(index_df, cfg)`）

```
BREADTH_INDEX 日线（260 行，data_loader 拉取）
  └─→ _resample_ohlc(df, 'W')  ──周线 OHLCV
        └─→ _calc_kdj(n=9, k=3, d=3) ── 周 K/D/J 序列
              └─→ _find_recent_cross(周K, 周D, lookback=3, overbought=80)
  └─→ _resample_ohlc(df, 'ME') ──月线 OHLCV
        └─→ _calc_kdj(n=9, k=3, d=3) ── 月 K/D/J 序列
              └─→ _find_recent_cross(月K, 月D, lookback=3, overbought=80)
  └─→ 按 KDJ_RED_MODE 合并 → red=True/False + red_reason + detail + kdj_detail{branch_hits, k_now, mode}
```

关键说明：

- **周/月线聚合在 indicators 内完成**，不在 data\_loader 做——数据层只拉原始日线（便于缓存复用），指标聚合放纯函数层，逻辑一致、易测

- `_resample_ohlc` 兼容 pandas 新版：把 `'M'` 自动转 `'ME'`、`'Y'`→`'YE'`；按期末日期作为周期标签

- `KDJ_LOOKBACK_DAYS=260` 已在 `main._gather_data` 与 `VOLUME_LOOKBACK_DAYS / ADX_PERIOD / LOOKBACK_DAYS` 取 max 作为 `fetch_index_daily` 实际窗口，**不会为 S7 单独再拉一次指数数据**

- `value` 字段用主周期（月线优先→周线）的最新 K 值，便于 CSV 横向比较；`threshold` 对应 `KDJ_OVERBOUGHT`

#### 数据不足与降级

| 失败场景                                          | 信号表现                                         | 不阻断整体流程 |
| --------------------------------------------- | -------------------------------------------- | ------- |
| `index_df` 为空 / 缺 `high/low/close`            | 灰灯                                           | ✓       |
| 日线 < n+2 行（聚合后 KDJ 无法有效收敛）                    | 灰灯（仅对应周期灰，不影响另一周期）                           | ✓       |
| `KDJ_USE_WEEKLY` 和 `KDJ_USE_MONTHLY` 均为 False | 灰灯（未启用任何周期）                                  | ✓       |
| 周/月线聚合尾部有空 NaN 周期（周末/节假日区间无交易）                | `resample` 后 `dropna(subset=['close'])` 自动丢弃 | ✓       |

***

## 三、模块二：ADX 市场状态过滤器（动态阈值）

Wilder 平均趋向指标（`ADX_PERIOD=14`）衡量趋势**强度**（不关心涨跌方向），基于沪深300的 high/low/close 计算。作为**市场状态过滤器**，不直接亮红灯，而是动态调整红灯计数阈值与权重。

| 市场状态            | ADX 区间        | 动态阈值                             | 说明                                                                                                                                                             |
| --------------- | ------------- | -------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 强趋势市 `trend`    | ADX > 30      | **5**（需全亮）                       | 情绪主导增量市场，轻度"放量不涨"会被后续资金消化                                                                                                                                      |
| 弱趋势/震荡市 `range` | ADX < 22      | **3**（S4/S5/S6 加权 ×1.5，S7 不参与加权） | 存量博弈，抽血效应 / 机构弃守 / 杠杆资金撤离更易引发踩踏；`effective_red_count = ceil(加权和)`。**S7 不纳入加权的原因**：它是周/月线级的中长周期形态信号（典型"大顶预警"），与 S4/S5/S6 这种"震荡市当日抽血/弃守/撤离"不在同一时间尺度，保持其独立强信号属性即可 |
| 过渡区 `neutral`   | 22 ≤ ADX ≤ 30 | **4**（维持原判）                      | 方向不明，严格口径                                                                                                                                                      |

- **talib 加速**：`USE_TALIB=True` 且 talib 可用时走 `talib.ADX`，否则降级纯 numpy `_compute_adx` Wilder 自实现（前 period 项简单平均初始化 + `S_t = S_{t-1} - S_{t-1}/n + X_t` 递推）

- **ADX 未取 / 数据不足**：退化为配置档自身 `REDUCE_THRESHOLD`（default=4, conservative=3, strict=4）

### 降仓位映射表（按 `effective_red_count` 降序首条匹配）

| red\_count ≥ | 仓位建议    | 说明                           |
| ------------ | ------- | ---------------------------- |
| 4            | 8成 → 5成 | 重仓降至半仓以下，7 信号中超过 3 个亮红灯      |
| 3            | 8成 → 6成 | 震荡市加权触发：S4/S5/S6 抽血效应 + 杠杆撤离 |
| 2            | 8成 → 7成 | 小幅降低或保持观察                    |
| < 2          | 维持当前仓位  | 未达预警门槛                       |

***

## 四、模块三：板块轮动分析（RRG 四象限 + 5 维评分卡）

### 4.1 时间轴全局对齐

`align_to_benchmark` 以沪深300交易日为**统一锚点**，所有 ETF 篮子成员按 `date` 左连接 + `ffill` 向前填充（停牌/节假日缺失复用前一日）。输出 ETF 行数严格 = len(bench\_df)，避免长度不齐导致指标错位。

### 4.2 板块篮子（19 只 ETF，可在 config 替换为个股）

防御（银行、公用事业）+ 成长（芯片、计算机、机器人、创新药、AI、半导体、卫星、航空航天、军工）+ 主题（电力、电网设备、能源、农业、豆粕、粮食、消费、医疗器械），共 19 只。

### 4.3 RRG 相对强度四象限

先对 ETF 收盘价与基准做**归一化**（各自首行有效值 = 1，解决 ETF 价格 \~0.8 vs 指数 \~4500 的量级差异问题），再取比值做 `WMA(20)` 得到 **RS-Ratio**（相对强度）；对 RS-Ratio 做 `ROC(10)` 得到 **RS-Momentum**（动量变化率 %）。

| 象限  | 组合条件                                 | 标签      | 含义           |
| --- | ------------------------------------ | ------- | ------------ |
| I   | RS-Ratio > 1.0 **且** RS-Momentum > 0 | 🟢 领涨主线 | 强强：强者恒强，优先持有 |
| II  | RS-Ratio ≤ 1.0 **且** RS-Momentum > 0 | 🟡 轮动初期 | 弱强：由弱转强，关注建仓 |
| III | RS-Ratio ≤ 1.0 **且** RS-Momentum ≤ 0 | 🔴 滞后回避 | 弱弱：持续弱势，回避   |
| IV  | RS-Ratio > 1.0 **且** RS-Momentum ≤ 0 | 🟠 退潮预警 | 强弱：盛极而衰，警惕   |

- **边界保守归类**（默认开启 `RRG_BOUNDARY_CONSERVATIVE=True`）：RS-Ratio=1.0 归入弱侧（≤1），RS-Momentum=0 归入负侧（≤0）

### 4.4 5 维评分卡（每维归一化 \[0, 5]，加权求和 → 综合 \[0, 5]）

| 维度          | 权重  | 指标                                     | 满分条件                      |
| ----------- | --- | -------------------------------------- | ------------------------- |
| 维1 相对动量     | 25% | RS-Ratio - 1                           | 超基准 0.5 即 5 分；RS<1 直接 0 分 |
| 维2 动量加速度    | 20% | RS-Momentum 二阶差分（mom₁ - 2·mom₂ + mom₃） | 二阶差分 5% 即 5 分；负值给 0       |
| 维3 ADX 趋势强度 | 20% | ETF 自身 Wilder ADX(14)                  | ADX=30 即 5 分              |
| 维4 资金关注度    | 20% | 换手率代理（amount / (close·volume)）         | 换手 5% 即 5 分               |
| 维5 拥挤度折扣    | 15% | 基础 5 分；换手 > 8% 扣 2 分                   | 不拥挤 5 分；拥挤 3 分            |

- **操作参考档**：综合分 > 4.0 → **持有/加仓**；3.0\~4.0 → **关注建仓**；≤ 3.0 → **回避**

- **排名 Δ**：读取 `data/cache/rotation_rank_last.json` 上次排名缓存，与今日 `total_score` 排名做差，输出 `+N / -N / 持平 / 新`（首次运行全部"新"）

***

## 五、多档配置档（default / conservative / strict / kc 科创综指）

通过 `--profile` 或 `get_alarm_config(name)` 切换，所有阈值集中在 config.py（含 KDJ 超卖/极度超卖/Inf 哨兵等派生常数），业务代码不硬编码阈值。未知档名直接报错并列出可用档。

| 配置档               | 红灯阈值              | 典型差异                                                                                                             | 适用场景                |
| ----------------- | ----------------- | ---------------------------------------------------------------------------------------------------------------- | ------------------- |
| `default`         | ≥4 红降仓            | 成交额 3% / 放量×1.3 / 涨跌比 2.5→1 / 超额 3% / 成长 -5% / 融资 5 日增速 3% / 净杠杆 1.8% / KDJ 高位阈值 80，主指数=沪深300(sh000300)          | 日常标准口径              |
| `conservative`    | ≥3 红降仓            | 成交额 2.5% / 放量×1.5 / 成长 -3% 即破位                                                                                   | 提前预警，防守型            |
| `strict`          | ≥4 红降仓（同 default） | 成交额 3.5% / 超额需 >5% / 成长 -8% 才算破位                                                                                 | 仅极端信号警报             |
| `kc` / `kechuang` | ≥4 红降仓（同 default） | 主指数=科创50(sh000688) + KDJ\_INDEX=科创50（复用日线，不重复拉取）；阈值继承 default 不变；想切"科创综指"把 `BREADTH_INDEX/KDJ_INDEX` 改成 sh000006 | 单独监控科创板，科创综指/科创50口径 |

> S6 两融阈值（`MARGIN_*`）、S7 KDJ 阈值（`KDJ_*`）目前未按配置档差异化，所有档共用同一组阈值；如需配置档级别差异化，可在 `ConservativeAlarmConfig` / `StrictAlarmConfig` / `KechuangAlarmConfig` 中覆写对应字段。

***

## 六、常用运行命令

> 项目使用 `uv` 管理依赖，`pyproject.toml` 位于同目录，`package=false`（作为单文件服务运行，不发布）。首次运行前执行 `uv sync` 安装 pandas / numpy / akshare / adata。

### 6.1 默认全流程（预警 + ADX + 板块轮动 → Markdown 全景报告 + CSV 落盘）

```bash
uv run services/alarming_monitor/main.py
```

- 参考日自动决策：沪深300已更新至今日→用今日；否则回退昨日（数据驱动，零猜测）

- 控制台打印完整 Markdown 全景报告

- 写入 `data/alarming_signals/alarming_YYYY-MM.csv`（预警历史按月追加）

- 写入 `data/reports/rotation/YYYY-MM/YYYY-MM-DD_rotation.md`（全景报告）

### 6.2 仅跑板块轮动（跳过预警模块）

```bash
uv run services/alarming_monitor/main.py --rotation-only
```

### 6.3 仅跑预警（跳过板块轮动，老行为摘要卡片）

```bash
uv run services/alarming_monitor/main.py --no-rotation
```

### 6.4 指定参考日（默认：数据驱动自动选今日/昨日）

```bash
uv run services/alarming_monitor/main.py --date 2026-08-19
```

### 6.5 切换配置档

```bash
uv run services/alarming_monitor/main.py --profile conservative   # 保守档
uv run services/alarming_monitor/main.py --profile strict         # 严格档
uv run services/alarming_monitor/main.py --profile default        # 标准档
uv run services/alarming_monitor/main.py --profile kc             # 科创综指档（主指数=sh000688 科创50，BREADTH_INDEX 和 KDJ_INDEX 同步切换）
uv run services/alarming_monitor/main.py --profile kechuang       # 科创综指档（长别名，等价于 kc）
```

### 6.6 只跑部分预警信号（逗号分隔，不跑的跳过）

可用 key：`turnover(S1)`, `volume(S2)`, `ad(S3)`, `dividend(S4)`, `growth(S5)`, `margin(S6)`, `kdj(S7)`

```bash
uv run services/alarming_monitor/main.py --indicator turnover,dividend,growth
uv run services/alarming_monitor/main.py --indicator margin        # 只跑 S6 两融杠杆异常
uv run services/alarming_monitor/main.py --indicator kdj           # 只跑 S7 大盘KDJ高位死叉
uv run services/alarming_monitor/main.py --indicator margin,kdj    # 只跑 S6+S7（资金面+中长周期形态）
```

### 6.7 跳过 S6 两融杠杆信号（仍跑其他 S1\~S5/S7）

```bash
uv run services/alarming_monitor/main.py --no-margin
```

适用场景：临时网络不稳 / akshare 两融接口抖动 / 只关心价格量能类信号时跳过 S6，避免拖慢主流程。

### 6.7b 跳过 S7 KDJ 信号（仍跑其他 S1\~S6）

```bash
uv run services/alarming_monitor/main.py --no-kdj
```

适用场景：快速跑短周期信号 / 只关心日级预警时跳过 S7 的日线拉长与周/月线聚合；或只想跑日级预警时减少一次 260 日拉取。

### 6.8 禁用 ADX 过滤器（退化为静态阈值 4 红）

```bash
uv run services/alarming_monitor/main.py --no-adx
```

### 6.9 不写文件（仅控制台输出，不落盘）

```bash
uv run services/alarming_monitor/main.py --no-csv --no-report
```

### 6.10 静默模式（只写文件，不打印控制台）

```bash
uv run services/alarming_monitor/main.py --quiet
```

### 6.11 查看历史预警记录（默认近 3 个月）

```bash
uv run services/alarming_monitor/main.py --history
uv run services/alarming_monitor/main.py --history --history-months 6   # 近 6 个月
```

### 6.12 详细日志（INFO 级别，调试数据拉取/缓存命中）

```bash
uv run services/alarming_monitor/main.py -v
```

### 6.13 强制禁用缓存（调试数据源，每次触网重拉）

```bash
NO_CACHE=1 uv run services/alarming_monitor/main.py
```

### 6.14 运行单元测试（全部 mock，不触网，\~10s）

```bash
cd services/alarming_monitor && uv run pytest test_monitor.py test_margin.py test_linkban.py -v
# 或项目根（全量）
uv run pytest services/alarming_monitor/
# S6 专测
uv run pytest services/alarming_monitor/test_margin.py -v
# 连板专测
uv run pytest services/alarming_monitor/test_linkban.py -v
```

***

## 七、完整命令行参数速查

```
uv run services/alarming_monitor/main.py [OPTIONS]

数据选择:
  --date YYYY-MM-DD        参考日期（默认：数据驱动自动选今日/昨日）
  --profile NAME           配置档: default / conservative / strict（默认: default）
  --indicator LIST         只跑部分信号: turnover,volume,ad,dividend,growth,margin,kdj
                            逗号分隔（默认: all）

模块开关（互斥二选一）:
  --rotation-only          仅跑板块轮动，跳过预警模块
  --no-rotation            跳过板块轮动，仅输出预警摘要卡片

输出开关:
  --no-csv                 不写 alarming_YYYY-MM.csv 预警历史
  --no-report              不写 Markdown 全景报告文件
  --no-adx                 禁用 ADX 动态阈值过滤器，退化静态阈值
  --no-margin              跳过 S6 两融杠杆异常信号（仍跑 S1~S5/S7）
  --no-kdj                 跳过 S7 大盘KDJ高位死叉信号（仍跑 S1~S6）
  --quiet, -q              静默，仅写文件，不打印控制台
  --verbose, -v            详细日志（INFO 级）

历史查询:
  --history                打印近 N 个月预警历史汇总表
  --history-months N       --history 显示月数（默认: 3）
```

***

## 八、数据源策略

| 数据               | 主源                                                       | 回退 / 补充                           | 说明                                                                                                          |
| ---------------- | -------------------------------------------------------- | --------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| 指数日线（沪深300/上证综指） | 腾讯 `stock_zh_index_daily_tx`                             | adata `stock.market.get_market`   | OHLCV + 成交额，ADX 需 high/low/close；S7 拉 260 日窗口，由 indicators resample 周线('W')/月线('ME')，再计算国内版 KDJ 9,3,3 找高位死叉 |
| ETF 日线（前复权）      | 腾讯 `stock_zh_a_hist_tx` (adjust=qfq)                     | adata → 东财 `fund_etf_spot_em` 补今日 | 腾讯源不含今日时，用东财全量快照（全局 30s 缓存，一次拉取 19 只复用）拼接                                                                   |
| 两市总成交额（S1 分子）    | 新浪 `stock_zh_index_spot_sina`                            | —                                 | `sh000001 + sz399001` 成交额列求和（元）                                                                             |
| 今日涨跌家数（S3 今日）    | legu `stock_market_activity_legu`                        | —                                 | 仅今日；历史靠 storage CSV 自累积                                                                                     |
| 全市场两融汇总（S6）      | akshare `stock_margin_sse`（区间）+ `stock_margin_szse`（日快照） | `stock_margin_detail_szse`（明细汇总）  | SSE 单位为元，SZSE 单位为亿 → data\_loader 统一转元；CSV 自累积优先拼接（最热路径 0 触网），仅缺最新日才补拉 SSE                                  |
| 大盘周/月 KDJ（S7）    | —（纯指标层，无外部数据）                                            | —                                 | **复用指数日线**做 resample，不再重复触网；国内版 KDJ 纯 numpy 自实现，兼容 pandas 新版                                                |

- **akshare stdout/stderr 噪声**：`_suppress_output` 上下文 fd 级压制（dup2 /dev/null），不污染 Markdown 输出

- **腾讯 symbol 归一化**：指数代码带 sh/sz 前缀（如 `sh000300`）必须原样保留（00 开头虽属深市规则，但指数归沪市），不能误判为 sz；6 位裸 ETF 代码按开头 2 位推断交易所（51/50/56/58→sh，15/16→sz）

***

## 九、缓存与离线降级

### 9.1 双层缓存（`@cached` 装饰器）

- **历史层**：`{key}_history.csv` → >30 天前数据，永久缓存，不触网

- **当日层**：`{key}_latest.csv` → ≤30 天数据，TTL 20h（`CACHE_TTL_HOURS=20`）

- **合并**：history + latest 按 date 去重，keep=last

- **freshness\_check（默认开启）**：即使 TTL 未过期，若 `缓存最新日 < 今日` 也视为过期强制重拉（解决"数据源更新前拉取到旧数据并写入缓存"问题）；`min_refetch_minutes=10` 防止连续运行频繁触网

- **缓存目录**：`trading_lab/data/cache/`（相对项目根）

### 9.2 离线 / 拉取失败降级

拉取失败时优先用旧 latest 兜底（仅缺最新日）→ 仍失败用仅历史缓存 → 仍失败返回 None，对应信号标记灰灯 `data_sufficient=False`。整体流程永不因单源失败阻断。

***

## 十、输出文件路径

```
trading_lab/
├── data/
│   ├── cache/                               # 双层缓存（9.1）
│   │   ├── index_sh000300_history.csv       # 沪深300历史永久层
│   │   ├── index_sh000300_latest.csv        # 沪深300当日层（TTL 20h）
│   │   ├── etf_512800_history.csv           # 银行ETF历史
│   │   ├── etf_512800_latest.csv
│   │   └── rotation_rank_last.json          # 板块轮动上次排名缓存
│   ├── alarming_signals/                    # CSV 历史（storage.py）
│   │   ├── alarming_2026-08.csv             # 预警记录按月分（25 字段，含 s6_net_leverage/s6_red、s7_k_value/s7_red；旧表头缺列时 storage 自动兼容重写）
│   │   ├── breadth_2026-08.csv              # 涨跌家数自累积按月分
│   │   └── margin_2026-08.csv               # 两融汇总自累积按月分（rzye_yuan 等元单位列）
│   └── reports/rotation/
│       └── 2026-08/
│           └── 2026-08-19_rotation.md       # 全景 Markdown 报告
```

***

## 十一、扩展点

1. **通知渠道扩展**：在 `notifier.py` 新增 `FeishuNotifier`（飞书机器人 webhook）/ `EmailNotifier`（SMTP）/ `WxPusherNotifier`（微信），继承 `BaseNotifier` 实现 `send()`，并在 `_NOTIFIER_REGISTRY` 注册即可通过 `get_notifier('feishu')` 调用。
2. **新增 ETF / 板块**：编辑 `config.py` 的 `ROTATION_BASKET`（代码）与 `ROTATION_LABELS`（中文名映射），无需改其他代码。
3. **新增信号**：在 `indicators.py` 新增 `check_xxx(df, cfg) -> dict`（遵循统一结构 `{name,label,red,value,threshold,detail,data_sufficient,red_reason}`），在 `main._INDICATOR_KEYS` 增加别名映射，在 `reporter._SIGNAL_MAP` / `CSV_FIELDS` 增加列映射，在 `config.AlarmConfig` 增加阈值字段即可（S6 / S7 即按此流程接入）。
4. **新配置档**：在 `config.py` 新增 `class XxxAlarmConfig(AlarmConfig)` 覆写字段，并在 `_CONFIG_REGISTRY` / `_PROFILE_DESCRIPTIONS` 注册，命令行直接 `--profile xxx` 可用。

三档操作参考
操作参考	触发条件	综合分区间	指导意义
持有/加仓	综合分 > 4.0	(4.0, 5.0]	强势主线：RS-Ratio 远超基准 + 动量加速 + ADX 强趋势 + 资金持续流入 + 不拥挤。5 维几乎全优，可坚定持有甚至加仓
关注建仓	综合分 ≥ 3.0	\[3.0, 4.0]	温和偏强：部分维度优秀但尚有短板（如 ADX 不够强或动量在衰减），可纳入观察池、择机建仓
回避	综合分 < 3.0	\[0, 3.0)	偏弱或不明朗：多数维度得分低，跑输基准或趋势不明，不适合介入

设计意图
这套评分卡不是给你"明天买什么"的短线择时，而是板块相对强弱的中期筛选工具：

回避 ≠ 一定会跌，而是"相对沪深300没有超额收益 + 趋势不够强"，中期持有大概率跑输指数
持有/加仓意味着多维度共振走强，属于"强者恒强"的主线品种
三档的本质是用 5 个正交维度（动量/加速度/趋势/资金/拥挤度）做交叉验证，避免单看涨跌幅被假突破误导
