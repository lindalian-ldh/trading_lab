# bankuai-service 板块扫描与龙头梯队识别

基于 [zzshare](https://quant.zizizaizai.com) 免费接口，扫描 A 股板块热度排名，
并对热门/冷门板块识别**三级龙头梯队**（领涨龙 / 中军 / 补涨潜力）。
纯信号系统，跑完即退，无常驻循环。

## 一、快速开始

### 1. 配置 token

在 `trading_lab/.env` 中设置（token 在 [zzshare 个人中心](https://quant.zizizaizai.com/me/profile) 免费申请）：

```bash
ZZSHARE_TOKEN=your_zzshare_token_here
```

### 2. 运行扫描

```bash
cd trading_lab

# 扫描昨日（概念板块，默认 Top10 / Bottom10）
uv run services/bankuai-service/main.py

# 指定日期与板块类型（题材/行业）
uv run services/bankuai-service/main.py --date 2026-08-12 --plate-type 题材

# 自定义数量与梯队大小
uv run services/bankuai-service/main.py --top-n 5 --bottom-n 5 --tier-size 5

# 仅输出 JSON 报告（不打印控制台卡片）
uv run services/bankuai-service/main.py --quiet

# 详细日志
uv run services/bankuai-service/main.py -v
```

### CLI 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--date` | 昨天 | 查询日期 YYYY-MM-DD |
| `--plate-type` | 概念 | 板块类型：概念 / 题材 / 行业 |
| `--top-n` | 10 | 热门板块数量 |
| `--bottom-n` | 10 | 冷门板块数量 |
| `--tier-size` | 6 | 每个梯队最大数量 |
| `--out` | 自动 | JSON 报告输出路径 |
| `--quiet` | false | 不打印控制台卡片 |
| `--verbose` | false | 详细日志（INFO 级别） |

## 二、模块设计

```
bankuai-service/
├── config.py            # 配置中心：ScanConfig + plate_type 映射
├── data_loader.py       # zzshare 数据层：限频/重试/缓存降级
├── sector_ranker.py     # 模块 A：板块排名筛选（Top/Bottom N + 去重）
├── leader_selector.py   # 模块 B：三级龙头梯队识别（核心算法）
├── scanner.py           # 模块 C：主调度器（串联 A+B + 汇总）
├── reporter.py          # 控制台卡片输出
├── main.py              # CLI 入口（argparse）
└── test_bankuai.py      # 单元测试（31 例 + 2 网络集成）
```

### 模块 A：板块排名筛选（sector_ranker.py）

调用 `zzshare.plates_rank` 获取板块热度排名（按 score 降序），输出：
- **hot_sectors**：按 rank 升序取前 `top_n`（热度最高）
- **cold_sectors**：按 rank 降序取前 `bottom_n`（热度最低）
- **target_sectors**：合并去重后的待分析板块（以 plate_code 为 key）

### 模块 B：龙头梯队识别（leader_selector.py）

对每个目标板块调用 `zzshare.market_plate_stocks` 获取成分股人气排行，
按以下规则分三级（核心算法 `select_tiers` 为纯函数，便于单测）：

| 梯队 | 选取规则 | 数量 |
|---|---|---|
| 一级·领涨龙 | rank 升序，`change > 板块均幅`；不足 3 只放宽到 `change > 0` | 3-6 |
| 二级·中军 | 剔除一级后，`est_turnover` 降序，`change > 0`；不足 3 只放宽 | 3-6 |
| 三级·补涨 | 剔除一二级后，`turnover_rate` 降序，无涨跌幅要求 | 3-6 |

**补足机制**：任一梯队不足 `tier_min`(默认 3) 时，从"未被选入的剩余池"
按对应排序补足，保证每队尽量 ≥3 只（数据不足时按实际数量分配）。

### 模块 C：主调度器（scanner.py）

串联 A + B，逐板块识别梯队并汇总。**单板块失败不影响整体流程**：
失败板块记入 `summary.failed_sectors` 并继续下一个。

## 三、数据输入（zzshare 接口）

> ⚠️ 实际接口字段与原始需求文档有差异，已按 zzshare 0.4.9 实测调整。

### plates_rank（板块排名）

```python
api.plates_rank(plate_type=15, date1='2026-08-12', limit=50)
# plate_type: 15=概念, 17=题材, 14=行业
# 返回 list[dict]，按 score(热度) 降序
```

返回字段：`plate_code` / `plate_name` / `rate`(涨跌幅%) / `trade_money`(成交额,元) /
`score`(热度) / `time`

### market_plate_stocks（成分股排行）

```python
api.market_plate_stocks(plate_code='885852', date1='2026-08-12',
                        is_real=1, limit=30, plate_type=15)
```

返回字段：`stock_code` / `stock_name` / `rank`(板块内排名) /
`px_change_rate`(涨跌幅%) / `turnover_ratio`(换手率%) /
`circulation_value`(流通市值,元) / `vol_ratio`(量比) / `attention`(人气)

> **注意**：成分股接口**未直接返回成交额(turnover)字段**。
> 二级龙头排序所需成交额以 `est_turnover = circulation_value × turnover_rate / 100`
> 估算（仅用于相对比较，非精确成交额）。

## 四、数据输出

### JSON 报告

默认保存至 `data/reports/bankuai/bankuai_{date}_{plate_type}.json`：

```jsonc
{
  "scan_time": "2026-08-12 21:09:01",
  "date1": "2026-08-12",
  "plate_type": "概念",
  "hot_sectors": [
    {"name": "青蒿素", "code": "885852", "rank": 1, "change": 3.704,
     "turnover": 67.85, "score": 970.0, "time": "2026-08-12 15:01:03"}
  ],
  "cold_sectors": [ ... ],
  "sector_leaders": {
    "青蒿素": {
      "sector_name": "青蒿素",
      "plate_code": "885852",
      "avg_change": 3.70,
      "tier1": [{"code": "600721", "name": "百花医药", "rank": 3,
                 "change": 10.04, "turnover_rate": 34.43, ...}],
      "tier2": [ ... ],
      "tier3": [ ... ]
    }
  },
  "summary": {
    "total_sectors": 6, "hot_count": 3, "cold_count": 3,
    "success_count": 6, "failed_sectors": []
  }
}
```

### 原始缓存

每次成功调用原始响应落盘至 `data/raw/bankuai/{date1}/`：
- `plates_rank_{plate_type}.json`
- `stocks_{plate_type}_{plate_code}.json`

接口失败时自动降级读取缓存（当日 → 向前回溯 30 天）。

### 控制台卡片

```
================================================================
📊 板块扫描报告  2026-08-12 21:09:01  [概念]  日期 2026-08-12
================================================================
  目标板块: 6  成功: 6  失败: 无
----------------------------------------------------------------
🔥 热门板块 Top3
  #  1  青蒿素       涨幅   +3.70%  成交额     67.9亿  热度 970
  ...
================================================================
🎯 各板块龙头梯队
┌─ 青蒿素  (板块均幅 +3.70%)
│ 一级·领涨龙 (3 只):
│  1. 600721 百花医药      涨幅   +10.04%  换手  34.43%  rank 3
│  ...
└------------------------------------------------------------
```

## 五、配置参数

`config.py` 中的 `ScanConfig` dataclass（全部可调）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `plate_type` | 15 | 板块类型编码（15=概念/17=题材/14=行业） |
| `top_n` | 10 | 热门板块数量 |
| `bottom_n` | 10 | 冷门板块数量 |
| `tier_size` | 6 | 每个梯队最大数量 |
| `tier_min` | 3 | 每个梯队最小数量（不足时补足） |
| `stock_limit` | 30 | 拉取成分股上限 |
| `request_interval` | 2.0 | 请求间隔(秒)，zzshare 免费版 30次/分钟 |
| `timeout` | 5 | 单次请求超时(秒) |
| `retry_times` | 2 | 失败重试次数 |
| `overall_timeout` | 90 | 整体扫描超时(秒) |

## 六、限频与性能

- zzshare 免费版限制 **30 次/分钟**
- 单次完整扫描调用：1(板块排名) + 20(成分股) = **21 次**
- 每次调用后 `time.sleep(2)`，总耗时约 **42 秒**（实测 6 板块 16 秒）
- zzshare SDK 内置 429 自动退避重试；本模块额外加 2 次重试
- **运行时间 < 90 秒**，无 429 报错

## 七、运行测试

```bash
cd trading_lab

# 单元测试（无需网络，默认跳过集成测试）
uv run pytest services/bankuai-service/test_bankuai.py -v

# 网络集成测试（需配置 ZZSHARE_TOKEN）
ENABLE_NETWORK_TESTS=1 uv run pytest services/bankuai-service/test_bankuai.py -v -k Integration
```

全部 **31 个单元测试**通过（2 个网络集成测试默认跳过）。
覆盖：plate_type 解析、板块排名筛选、三级梯队核心算法（严格/放宽/补足/边界）、
失败隔离、缓存读写降级。

### 运行日志示例

```
21:10:09 [INFO] scanner: ===== 板块扫描开始 2026-08-12 [概念] =====
21:10:12 [INFO] sector_ranker: 板块排名筛选完成: 总数=50, 热门=1, 冷门=1, 去重后待分析=2
21:10:14 [INFO] leader_selector: 梯队识别完成 青蒿素(885852): 成分=10, 均幅=3.70%, T1=3 T2=3 T3=3
21:10:16 [INFO] leader_selector: 梯队识别完成 太空互联网(885822): 成分=11, 均幅=1.82%, T1=3 T2=3 T3=3
21:10:16 [INFO] scanner: ===== 板块扫描结束: 目标=2 成功=2 失败=0 =====
```

## 八、验收标准

| 标准 | 状态 |
|---|---|
| 正确获取 Top10 和 Bottom10 板块列表，与官网一致 | ✅ |
| 每个板块输出 3 级梯队，每队 3-6 只股票 | ✅ |
| 程序运行时间 < 90 秒 | ✅（实测 6 板块 16s，20 板块约 42s） |
| 无接口限频报错（429） | ✅（sleep(2) + SDK 退避） |
| 单板块获取失败不影响整体流程 | ✅（scanner 异常隔离 + 单测覆盖） |
| 输出数据结构完整、字段齐全 | ✅（JSON 报告 + 控制台卡片） |

## 九、Python API 调用

除 CLI 外，也可直接调用模块：

```python
from config import ScanConfig
from scanner import scan

cfg = ScanConfig.from_env()  # 从 ZZSHARE_TOKEN 环境变量读取
report = scan('2026-08-12', cfg)
print(report['summary'])
```
