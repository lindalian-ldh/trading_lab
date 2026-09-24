# 哑铃策略配置与监控系统

A 股结构分化环境下的周级别战术再平衡工具。定期运行，输出防御端与进攻端的目标权重、再平衡建议和失效预警信号。

## 两种运行模式

| 模式 | 资产池 | 信号阈值 | 再平衡频率 | 失效预警 |
|---|---|---|---|---|
| **style**（默认）| 固定 ETF（红利 + 成长） | spread<1.5, corr>0.5, zscore>2.5 | 14 天 / 偏离 10pp | 3 条件 |
| **sector** | 动态板块筛选 + ETF 映射 | spread<2.0, corr>0.65, zscore>2.0 | 7 天 / 偏离 8pp | 7 条件 |

通过环境变量 `BARBELL_MODE` 或修改 `barbell_config.py` 顶层 `MODE` 切换。

## 设计原则

- 两端各留一个主池，信号不超过三个，规则简单到可以写在纸上
- 参数全部集中在 `barbell_config.py`（`STYLE_PARAMS` + `SECTOR_PARAMS`）
- 异常降级保证系统可运行
- 复用核心模块：`signals.py`/`weights.py`/`rebalance.py` 函数签名在两种模式下都不变

## 安装

依赖已纳入 trading_lab 根 `pyproject.toml`（baostock/efinance/akshare/pandas/numpy），`uv sync` 一次性装齐共享 `.venv`。sector 模式额外复用 `services/bankuai-service/` 的 zzshare 数据层（共享同一 `.venv`）。

## 运行

### 风格哑铃（默认）

```bash
cd /Users/a801/Linda/Work/project/gupiao-assistant/trading_lab
uv run python services/barbell_system/main.py
```

### 板块哑铃

```bash
BARBELL_MODE=sector uv run python services/barbell_system/main.py
```

### 信号验证回测

```bash
uv run python services/barbell_system/backtest.py style 2024-01-01
# 或 sector 模式
uv run python services/barbell_system/backtest.py sector 2024-01-01
```

## 资产池

### style 模式（固定）

| 端 | 默认 | 备选 |
|---|---|---|
| 防御端 | sh.515080 中证红利 ETF | sz.159549 红利低波 ETF |
| 进攻端 | sz.159552 中证 2000 增强 ETF | sz.159259 成长 ETF |

### sector 模式（动态 / 手动）

sector 模式有两种资产池解析路径，**手动配置优先**：

#### 路径 A：手动配置（推荐，最稳定）

在 `barbell_config.py` 的 `SECTOR_PARAMS` 里填这两个字段，运行时直接钉死两端 ETF，**跳过 screener 动态筛选**：

```python
SECTOR_PARAMS = {
    ...
    "manual_defensive_etf": {"sector": "银行",   "code": "512800", "name": "银行 ETF"},
    "manual_offensive_etf": {"sector": "半导体", "code": "512480", "name": "半导体 ETF"},
    ...
}
```

字段说明：
- `sector`：板块名（用于日志和报告展示，可填任意标识）
- `code`：6 位 ETF 代码（如 `512800`，前缀 sh/sz 自动判定）
- `name`：ETF 显示名

留 `None` 走路径 B。两端只要有一端为 `None`，自动降级到动态筛选。

#### 路径 B：动态筛选

`screener.py` 运行板块筛选，输出前 3 防御端 + 前 3 进攻端候选板块，再由 `sector_etf_map.py` 为每个板块映射流动性合格的 ETF。

- **防御端筛选因子**：60 日波动率分位 < 30%、估值分位 < 50%、股息率 TTM > 3%（缺失用中位数填充）
- **进攻端筛选因子**：12 月动量排名前 20%、近 10 日成交额上升
- **中间地带排除**：防御与进攻得分均在 40-60 分位的板块剔除
- **两端错开**：同一板块不会同时出现在防御端与进攻端（进攻端候选自动剔除防御端已选板块）
- **板块行情代理**：用该板块主 ETF 的日线（baostock 主源）替代 akshare 行业指数，规避 py_mini_racer 与 eastmoney 接口问题
- **ETF 流动性过滤**：日均成交额 > 5000 万、基金规模 > 5 亿、跟踪误差 < 2%；候选 ETF 不合格时顺延到下一板块候选
- **缓存**：当日结果写入 `data/cache/barbell_screener_YYYYMMDD.json`，板块 ETF 日线写入 `data/cache/barbell_etf_*_YYYYMMDD.csv`，同一天多次运行直接读缓存。如需强制重跑，删除对应文件即可。

#### 降级链

手动配置 → 动态筛选 → style 默认资产池（中证红利 + 中证 2000 增强）。任意一环失败自动降级，系统不崩溃。

## 参数对照表（barbell_config.py）

| 参数 | style | sector | 说明 |
|---|---|---|---|
| base_weight | 0.50 | 0.60 | 防御端基础权重 |
| rebalance_threshold | 0.10 | 0.08 | 再平衡触发偏离（pp） |
| correlation_alert | 0.50 | 0.65 | 相关性警戒线 |
| spread_floor | 1.5 | 2.0 | 股债利差下限（%） |
| spread_high | 2.5 | 3.0 | 股债利差上限（%，得分 +1） |
| zscore_high | 2.5 | 2.0 | 拥挤度上限（更严） |
| corr_window | 60 | 60 | 滚动相关性窗口（交易日） |
| run_frequency | 14 | 7 | 建议运行频率（天） |
| failure_corr | 0.5 | 0.65 | 失效预警相关性阈值 |
| failure_spread | 1.5 | 2.0 | 失效预警利差阈值 |
| failure_down_days | 5 | 3 | 失效预警连续下跌天数 |

## 三信号规则

**信号一：股债利差**
- 计算：防御端股息率 − 10 年期国债收益率
- 打分：> `SPREAD_HIGH` → +1；< `SPREAD_FLOOR` → −1；中间 → 0

**信号二：两端滚动相关性**
- 计算：防御端与进攻端 `CORR_WINDOW` 日滚动收益率相关系数
- 打分：< `CORR_LOW`(0.2) → +1；> `CORRELATION_ALERT` → −1；中间 → 0

**信号三：进攻端拥挤度**
- 计算：进攻端 `ZSCORE_WINDOW` 日收益率相对于过去 `ZSCORE_LOOKBACK` 交易日的 Z-score
- 打分：< `ZSCORE_LOW`(1.5) → +1；> `ZSCORE_HIGH` → −1；中间 → 0

## 权重判定

| 总分 | 市场状态 | 目标权重（style） | 目标权重（sector） |
|---|---|---|---|
| ≥ +2 | 标准配置 | 防御 50% / 进攻 50% | 防御 60% / 进攻 40% |
| 0 ~ +1 | 防御偏高 | 防御 60% / 进攻 40% | 防御 60% / 进攻 40% |
| ≤ −1 | 降级模式 | 防御 40% / 进攻 30% / 现金 30% | 防御 40% / 进攻 30% / 现金 30% |

## 失效预警

### style 模式（3 条件，任意 2 条成立触发降级）

1. 两端 60 日相关性 > 0.5
2. 股债利差 < 1.5%
3. 两端同时连续 5 个交易日下跌

### sector 模式（7 条件，任意 2 条成立触发降级）

1. 两端 60 日相关性 > 0.65
2. 股债利差 < 2.0%
3. 两端同时连续 3 个交易日下跌
4. 两端同时跌破 20 日均线
5. 政策事件标志（进攻端板块有重大监管变化，手工输入）
6. 60 日相关性从 <0.3 骤升至 >0.6
7. 任意端单日跌幅 > 5%

预警操作：两端权重各削减 20 个百分点转现金/货币基金，等待至少 10 个交易日后重新评估。

## 再平衡

- style 模式每 14 天、sector 模式每周检查一次
- 若两端实际权重偏离目标超阈值（style=10pp / sector=8pp）触发
- 调仓原则：涨多减仓、跌多补仓
- 实际权重通过 `barbell_state.json` 记录的上次再平衡价格推算价格漂移得到

## 数据源与降级方案

| 数据 | 主源 | 备源 | 最终降级 |
|---|---|---|---|
| 风格 ETF 日线 | baostock | efinance | — |
| 板块 ETF 日线 | baostock | efinance | — |
| 板块指数历史 | akshare `stock_board_industry_hist_em` | — | 该板块行情走板块 ETF 代理 |
| 板块排名 | bankuai-service（zzshare `plates_rank`） | — | 中位数填充 |
| 10Y 国债收益率 | akshare `bond_china_yield` | efinance 债券数据 | 中证红利 ETF 价格分位数代理 |
| 防御端股息率 | akshare `stock_zh_index_value_csindex` | — | 固定 4.5% |
| ETF 流动性 | baostock 日线成交额（近 20 日均量） | efinance | 降级使用占位 ETF |

数据源顺序由 `cfg.SECTOR_ETF_SOURCE_ORDER` 控制（默认 `baostock,efinance`），
可用环境变量临时切换：`BARBELL_ETF_SOURCE=efinance,baostock`。

### 排障：efinance 报 `RemoteDisconnected` / `ProxyError`

症状：日志里刷 `urllib3.connectionpool: Retrying ... RemoteDisconnected('Remote end closed connection without response')`，
随后 `efinance sector_etf_xxx 获取异常: ... (Caused by ProxyError('Unable to connect to proxy', ...))`。

原因（本机实测确认）：

1. efinance 的 `get_quote_history` 走 eastmoney 的 `/api/qt/stock/kline/get`。
   该接口在部分网络下会被服务端**直接断连**（TLS 握手正常、请求发出后返回空响应；
   同主机的 `/api/qt/stock/trends2/get`、`/api/qt/stock/fflow/daykline/get` 均正常）。
2. 本机 macOS 系统代理指向本地代理（127.0.0.1:31180/31181），requests 会自动复用该代理，
   代理同样连不通 → 报 `ProxyError`。
3. efinance 的 urllib3 适配器 `max_retries=5`，所以每次失败刷 5 行重试日志；
   sector 模式要拉 31 个板块，逐个重试会刷屏并让运行卡住。
4. `screener.py` 在此之前还调用了 baostock 备源，但当时 **baostock 尚未登录**
   （`main.py` 的登录排在板块筛选之后），备源直接返回 `10001001 you don't login`，
   于是一个板块都拿不到数据；`compute_etf_liquidity` 同理失败 → 所有 ETF 都被判为
   "流动性不足" → `main.py` 整体退回 style 默认资产池（sector 模式实际失效）。

已做的修复：

| 位置 | 修复 |
|---|---|
| `data_fetcher.fetch_sector_etf_kline` | 数据源改为 baostock 主、efinance 备（`cfg.SECTOR_ETF_SOURCE_ORDER`） |
| `data_fetcher.ensure_baostock_login` | 懒登录，screener / 流动性过滤等提前调用数据接口也能自愈 |
| `data_fetcher._prepare_efinance` | efinance 会话加固：`trust_env=False` 忽略系统代理、关闭自动重试、超时 5/10s |
| `data_fetcher.disable_efinance` | 连接类失败一次即熔断，后续板块不再重试同一坏源 |
| `data_fetcher._load/_save_etf_cache` | 板块 ETF 日线当日缓存（`data/cache/barbell_etf_*`，可用 `BARBELL_ETF_CACHE=0` 关闭） |
| `data_fetcher.compute_etf_liquidity` | 改用同一套日线源算均量，eastmoney 不可用时流动性过滤不再全灭 |
| `data_fetcher._dividend_akshare` | 进程内缓存，避免 31 个板块重复请求中证指数估值 |
| `main.py` | baostock 登录提前到板块筛选之前 |
| `main.py` / `screener.py` | 两端板块强制错开；单个板块 ETF 不合格时顺延到下一候选，而不是整体退回 style 池 |

## 输出

### 控制台报告
数据区间、模式与板块筛选明细、三信号值与打分、综合得分、市场状态与目标权重、当前隐含权重与再平衡建议、失效预警检查。

### CSV 日志
写入 `data/barbell_log_YYYYMMDD.csv`

字段（sector 模式含后 4 个）：
```
date, mode, spread, correlation, zscore, score, regime,
target_defensive, target_offensive, target_cash,
rebalance_triggered, alert_info,
defensive_sector, offensive_sector, screener_score, policy_flag
```

### 回测 CSV
- `data/barbell_backtest_{mode}_{start}_{today}.csv` — 收益曲线与每日信号
- `data/barbell_backtest_ic_{mode}_{start}_{today}.csv` — 三信号 IC 表

### 状态文件
`data/barbell_state.json` — 上次再平衡状态

### 运行日志
`logs/{date}.log` — 由 `core.logger` 自动记录运行日志

## 项目结构

```
services/barbell_system/
├── barbell_config.py     # 模式切换 + STYLE_PARAMS/SECTOR_PARAMS + 资产池
├── data_fetcher.py       # baostock/efinance/akshare 数据获取与降级（含板块扩展）
├── signals.py            # 三信号计算（两种模式共用）
├── weights.py            # 打分卡与权重判定（两种模式共用）
├── rebalance.py          # 再平衡 + style 模式失效预警
├── alert.py              # sector 模式 8 条件失效预警
├── screener.py           # 板块筛选（复用 bankuai-service 数据层）
├── sector_etf_map.py     # 板块→ETF 映射 + 流动性过滤
├── backtest.py           # 信号验证回测 MVP
├── main.py               # 主流程入口（按 MODE 分支）
├── README.md
└── pyproject.toml       # 服务依赖面文档（实际依赖在根 pyproject.toml）
```

## 与 bankuai-service 的复用关系

sector 模式的 `screener.py` 通过 `sys.path` 插入 `services/bankuai-service/` 复用：
- `data_loader.fetch_plates_rank` — 行业板块排名（plate_type=14）
- `leader_selector._percentile` — 线性插值百分位工具

不会重复造轮子。若 bankuai-service 不可用，screener 退化为仅 akshare 数据源并打 warning。

