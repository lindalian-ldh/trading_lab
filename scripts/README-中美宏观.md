# 中美宏观核心组合 Dashboard 使用说明

本脚本一键刷新中美宏观数据并生成两个 dashboard HTML，方便一次性阅览核心指标组合。

## 快速开始

```bash
# 进入项目根目录
cd /Users/a801/Linda/Work/project/gupiao-assistant/trading_lab

# 一键执行（刷新数据 + 生成两个 HTML）
./scripts/refresh_macro_charts.sh

# 或者自定义取数期数（默认 60）
RECENT=120 ./scripts/refresh_macro_charts.sh
```

## 输出文件

```
data/reports/charts/
├── macro_dashboard_CN.html    # 中国宏观核心组合（5 组）
└── macro_dashboard_US.html    # 美国及全球资产核心组合（6 组）
```

浏览器打开：

```bash
open data/reports/charts/macro_dashboard_CN.html
open data/reports/charts/macro_dashboard_US.html
```

## 执行流程

脚本分为 4 步，任一步失败不中断后续步骤（图表会基于库内已存数据渲染）：

| 步骤 | 说明 | 涉及指标 |
|---|---|---|
| 1/4 | 刷新中国宏观数据（`macro_data` 表） | 8 个：CPI、PPI、GDP、PMI、M2、LPR1Y/LPR5Y、SF、UE |
| 2/4 | 刷新美国及商品数据（`macro_us_data` 表） | 10 个：UNRATE、PAYEMS、CPIAUCSL、GS10、DGS10、DGS6MO、DTWEXBGS、GOLD、SILVER、PPIACO |
| 3/4 | 生成中国 dashboard（5 组组合） | 见下方「中国组合」 |
| 4/4 | 生成美国 dashboard（6 组组合） | 见下方「美国组合」 |

## 指标组合清单

### 中国（5 组，基于 `macro_data` 表）

| # | 组合名称 | 指标A | 指标B | 宏观逻辑 |
|---|---|---|---|---|
| 1 | 增长周期 | PMI（制造业景气） | GDP（实际增长） | PMI 领先 GDP 约 1-2 季度；连续站上 50 预示 GDP 上行 |
| 2 | 通胀剪刀差 | CPI（下游消费） | PPI（上游生产） | 剪刀差(PPI-CPI) 反映利润分配；PPI 大涨 CPI 低迷→上游利润挤压 |
| 3 | 信用传导效率 | M2（货币供给） | SF（社融） | 同步扩张=宽货币→宽信用顺畅；M2 高增社融低迷=资金空转 |
| 4 | 政策利率与地产 | LPR5Y（长期利率） | SF（社融） | 5Y LPR 下调→房贷成本降→居民长贷回升（滞后 1-2 月） |
| 5 | 就业与消费复苏 | UE（城镇调查失业率） | CPI（居民消费） | 失业率下行→收入改善→核心 CPI 上行；若失业率降 CPI 低迷=预防性储蓄陷阱 |

### 美国及全球资产（6 组，基于 `macro_us_data` 表）

| # | 组合名称 | 指标A | 指标B | 宏观逻辑 |
|---|---|---|---|---|
| 1 | 劳动力市场核实 | UNRATE（失业率） | PAYEMS（非农就业） | 负相关；非农>20万+失业率降=就业强；双弱=衰退边缘 |
| 2 | 通胀-利率传导 | CPIAUCSL（CPI） | GS10（10Y国债月频） | CPI 超预期→10Y 收益率上行（加息预期）；CPI 下行但 10Y 不跌=衰退/财政风险 |
| 3 | 避险跷跷板 | DTWEXBGS（美元指数） | GOLD（黄金） | 强负相关；美元走强黄金承压；极端同涨=地缘避险事件 |
| 4 | 贵金属联动 | GOLD（黄金） | SILVER（白银） | 强正相关；工业复苏期白银弹性更大，避险期黄金更稳 |
| 5 | 生产端传导 | PPIACO（美国PPI） | CPIAUCSL（美国CPI） | 领先 1-2 个月；PPI 急升 CPI 不动=终端需求疲软 |
| 6 | 美债曲线斜率 | DGS6MO（6月短端） | DGS10（10年长端） | 利差(10Y-6M)：倒挂=衰退预警；转正陡峭化=复苏开启 |

## 数据库行为说明

### 是否重置？

**默认不重置**。脚本使用增量模式（`incremental`）：

- 数据库表使用 `INSERT OR IGNORE`，已存在的记录（主键：`indicator + date + country`）会被跳过
- 重复运行脚本是**幂等**的——只追加新记录，不改变历史数据
- 如需全量刷新（清空重拉），使用 `--reset` 标志

```bash
# 默认：增量追加（推荐日常使用）
./scripts/refresh_macro_charts.sh

# 全量刷新（清空后重拉，约需 2-5 分钟）
./scripts/refresh_macro_charts.sh --reset
```

> ⚠️ 注意：`--reset` 会删除 `macro_data` 和 `macro_us_data` 两表**全部历史数据**，之后按 `--recent 60` 只重拉最近 60 条。如果你的数据窗口需要更长，先设置 `RECENT` 再 `--reset`。

### `--recent` 参数

| 设置 | 含义 | 适用场景 |
|---|---|---|
| `RECENT=60`（默认） | 每个指标取最近 60 条记录 | 日常更新，覆盖最近 5 年数据 |
| `RECENT=0` | 取全部历史数据 | 首次建库或完整重建 |
| `RECENT=120` | 取最近 120 条 | 需要更长数据窗口时 |

```bash
# 首次建库：全量拉取历史
RECENT=0 ./scripts/refresh_macro_charts.sh --reset

# 日常更新：只拉最近 60 条（幂等快速）
./scripts/refresh_macro_charts.sh

# 扩展窗口：取最近 10 年数据
RECENT=120 ./scripts/refresh_macro_charts.sh --reset
```

## 前置条件

1. **uv 已安装**：项目使用 uv Workspace 管理依赖
2. **FRED API Key**：在 `.env` 文件中配置 `FRED_API_KEY=xxx`（[免费申请](https://fred.stlouisfed.org))
3. **网络访问**：需能访问 FRED（美国数据）和 akshare（中国数据）数据源

## 自定义组合

如需增删指标组合，编辑脚本顶部的变量：

```bash
# 中国指标键（macro_data 表）
CN_KEYS="cpi_cn,ppi_cn,gdp_cn,..."

# 美国指标键（macro_us_data 表）
US_KEYS="fred_unrate,fred_payems,..."

# 中国组合（; 分隔组，, 分隔同图）
CN_COMBOS="PMI,GDP;CPI,PPI;..."

# 美国组合
US_COMBOS="UNRATE,PAYEMS;..."
```

可用指标键名参见：`services/macro-data-service/README.md` 的「指标清单」章节。

## 故障排查

| 问题 | 解决方案 |
|---|---|
| `uv: command not found` | 确认 uv 在 PATH 中，或使用绝对路径 `/opt/homebrew/bin/uv` |
| 部分指标拉取失败 | 脚本容错，图表仍基于已存数据生成。查看日志 `logs/macrodata-fetch.log` |
| FRED 指标超时 | 检查网络连接；可能是 FRED 限流，稍后重试 |
| HTML 打不开 | 使用本地文件路径 `file:///` 打开，或用 `python -m http.server` 启个静态服务 |

## 相关文档

- [macro-data-service README](../services/macro-data-service/README.md) — 数据获取服务详细说明
- [macro-charts README](../services/macro-charts/README.md) — 图表生成服务详细说明
- [services README](./README.md) — 所有服务模块速查
