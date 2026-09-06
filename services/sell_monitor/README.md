# 动态止盈止损与网格卖出监控系统（卖出执行模块）

克服"处置效应"（赚点就跑，亏了死扛）。将"卖出"完全机械化，杜绝主观感觉。

## 核心理念：进场即锁定

是的，必须在买入成交的那一刻，就把所有规则"锁死"在系统里。

系统将自动接管后续的卖出决策，交易者只负责执行系统发出的"卖出信号"，不做任何主观判断。

---

## 三层卖出机制

| 层级 | 名称 | 作用 | 触发条件 |
|------|------|------|----------|
| **1** | 硬止损（保命线） | 防止黑天鹅巨亏 | 价格跌破初始止损价（默认 -2%） |
| **2** | 动态移动止盈（阶梯抬升） | 锁定利润，逐步上移止损线 | 保本 → 网格分批 → 移动回撤 |
| **3** | 时间止损（效率线） | 强制效率，不浪费时间 | 持仓超过 N 根K线且未达预期盈利 |

---

## 快速开始

### 1. 登记新持仓（开仓时执行）

```bash
# 登记 600550 在 2026-08-12 开仓 1000 股 @ 10.00
uv run services/sell_monitor/main.py \
    --symbol 600550 --date 2026-08-12 \
    --init --entry-price 10.00 --shares 1000

# 使用趋势型配置开仓（宽止损、大网格、长持仓）
uv run services/sell_monitor/main.py \
    --symbol 600550 --date 2026-08-12 \
    --init --entry-price 10.00 --shares 1000 --profile trend

# 开仓但不生成退出路线图
uv run services/sell_monitor/main.py \
    --symbol 600550 --date 2026-08-12 \
    --init --entry-price 10.00 --shares 1000 --no-chart
```

### 2. 每日监控（收盘后执行）

```bash
# 检查 600550 截至 2026-08-12 的卖出信号
uv run services/sell_monitor/main.py --symbol 600550 --date 2026-08-12
```

### 3. 持仓管理

```bash
# 列出所有未关闭持仓
uv run services/sell_monitor/main.py --list

# 强制关闭某持仓（手动平仓）
uv run services/sell_monitor/main.py --symbol 600550 --close
```

---

## 命令参数

| 参数 | 说明 |
|------|------|
| `--symbol` | 股票代码（6 位数字） |
| `--date` | 参考日期 YYYY-MM-DD，默认昨天（开仓时作为 entry_date，监控时作为 ref_date） |
| `--profile` | 卖出配置档名称：`default` / `conservative` / `trend`，默认 `default` |
| `--init` | 登记新持仓（开仓时使用） |
| `--entry-price` | 开仓均价（`--init` 时必填） |
| `--shares` | 开仓股数（`--init` 时必填） |
| `--list` | 列出所有未关闭持仓 |
| `--close` | 强制关闭持仓 |
| `--no-chart` | 开仓时不生成退出路线图 |
| `--verbose` / `-v` | 启用详细日志（INFO 级别） |
| `--help` / `-h` | 查看帮助 |

---

## 配置档一览

| 配置档 | 类名 | 适用场景 | 核心特点 |
|--------|------|---------|----------|
| `default` | ExitConfig | 日常使用 | 硬止损 2% / 3 层网格 5-10-15% / 20 天 / 8%激活-3%回撤 |
| `conservative` | ConservativeExitConfig | 震荡市/不确定行情 | 紧止损 1.5% / 快止盈 4-8-12% / 短持仓 15 天 |
| `trend` | TrendExitConfig | 单边/趋势行情 | 宽止损 2.5% / 大网格 8-15-25% / 长持仓 40 天 |

### 配置项详解（以 `default` 为例）

```python
ExitConfig(
    # ===== 1. 硬止损 =====
    HARD_STOP_MODE: str = 'fixed_pct'       # 止损模式
    HARD_STOP_PCT: float = 0.02             # 初始硬止损 2%

    # ===== 2. 保本止损 =====
    BREAK_EVEN_TRIGGER: float = 1.0         # 浮盈达 1.0 倍止损距离时保本

    # ===== 3. 分批止盈（网格卖出） =====
    ENABLE_GRID_EXIT: bool = True
    GRID_LEVELS: List[Tuple[float, float]] = [
        (0.05, 0.30),   # +5% 卖剩余 30%
        (0.10, 0.30),   # +10% 卖剩余 30%
        (0.15, 0.40)    # +15% 清仓
    ]
    GRID_TRAILING_STOP: bool = True         # 网格卖出后止损上移
    GRID_TRAILING_BUFFER_PCT: float = 0.01  # 止损上移 = 触发价 × 0.99

    # ===== 4. 时间止损 =====
    MAX_HOLDING_BARS: int = 20              # 最多 20 个交易日
    TIME_STOP_MIN_PROFIT_PCT: float = 0.01  # 超时且盈利 < 1% 强制离场

    # ===== 5. 移动回撤止损 =====
    TRAILING_ACTIVATE_PCT: float = 0.08    # 浮盈 ≥ 8% 激活
    TRAILING_REGRET_PCT: float = 0.03      # 从最高点回撤 3% 卖出
)
```

---

## 核心逻辑详解

### 卖出信号判定优先级（高 → 低）

1. **硬止损触发** → `sell_all`（保命线，不可越过）
2. **当前止损价被跌破** → `sell_all`（保本/网格上移后的止损被破）
3. **移动回撤止损** → `sell_all`（趋势保护：从最高点回撤超阈值）
4. **时间止损** → `sell_all`（超时未达预期）
5. **网格分批止盈** → `sell_partial` / `sell_all`（阶梯抬升，支持跳层）
6. **保本止损上移** → `hold`（仅更新止损，无卖出）
7. 默认 → `hold`（继续持有）

### 网格卖出执行流程

```
进场后：系统在后台挂载 N 个虚拟"止盈哨兵"

价格触及 5% → sell_partial，卖出当前仓位的 30%
  卖出后：止损上移到 10.50 × 0.99 = 10.395（不低于保本线）

价格触及 10% → sell_partial，卖出剩余仓位的 30%（210 股）
  卖出后：止损上移到 11.00 × 0.99 = 10.89

价格触及 15% → sell_all，卖出剩余全部（清仓离场）
```

### 网格跳层处理

若一根K线内浮盈一次性穿过多个网格阈值（如从 +4% 直接跳到 +15%），系统按"跳到最高可触发的层级"处理：
- 最高可触发的层级是最后一层 → 清仓（`sell_all`）
- 否则按"累计剩余比例"计算卖出股数（`sell_partial`）

---

## 数据输入

| 数据 | 来源 | 格式 | 说明 |
|------|------|------|------|
| 股票代码 | 用户输入 | 6 位数字 | 如 `600550` |
| 开仓均价 | 用户输入 | float | 按此价格计算全部止盈止损位 |
| 开仓股数 | 用户输入 | int | 用于计算网格卖出数量 |
| 行情数据 | efinance (优先) / baostock (兜底) | DataFrame | 日线 K 线：date/open/high/low/close/volume |
| 配置参数 | ExitConfig | dataclass | 全部卖出规则，开仓时即锁定 |

---

## 数据输出

### 1. 控制台状态卡片（每日收盘后打印）

```
📦 持仓状态 [600550] 第 8/20 个交易日
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 开仓价: 10.00 | 现价: 11.20 | 浮盈: +12.0%
 硬止损: 9.80 (未触发) | 当前止损: 10.50 (已上移)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📌 网格卖出记录:
  [√] 第1层 (+5%): 已触发 @ 10.50
  [ ] 第2层 (+10%): 待触发 @ 11.00 (卖 30%)
  [ ] 第3层 (+15%): 待触发 @ 11.50 (清仓)
  累计卖出: 300/1000 股 (30.0%)，剩余 700 股
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⏰ 时间止损: 剩余 12 个交易日 (上限 20 根)
📐 移动回撤: 最高 11.50，回撤 2.6% < 3% (安全)
💡 建议: 网格止盈：第1层 (+5%) 触发，卖出 300 股；剩余 700 股，止损上移至 10.395
```

### 2. 退出路线图（开仓时自动生成）

- **路径**：`data/reports/exit-maps/exit_map_{symbol}_{date}.png`
- **内容**：画出现价、硬止损、保本线、各级网格止盈线
- **目的**：让交易者"看得见未来的每一步"，消除对未知的恐惧

### 3. 持仓实例持久化

- **路径**：`data/trading.db` → `positions` 表
- **主键**：`(symbol, entry_date)`
- **字段**：全部卖出规则锁定值 + 运行期追踪数据

### 4. 卖出信号日志（CSV）

- **路径**：`data/sell_signals/sell_signals_YYYY-MM-DD.csv`（按天分文件）
- **字段**：

| 字段 | 说明 |
|------|------|
| `timestamp` | 记录时间 |
| `symbol` | 股票代码 |
| `action` | `hold` / `sell_partial` / `sell_all` |
| `reason` | 触发原因 |
| `sell_price` | 建议卖出价 |
| `sell_shares` | 建议卖出股数 |
| `new_stop_price` | 更新后的止损价 |
| `cur_price` | 当前收盘价 |
| `profit_pct` | 当前浮盈比例(%) |
| `remaining_shares` | 剩余持仓股数 |
| `bars_held` | 已持有K线根数 |
| `highest_price` | 持仓以来最高价 |
| `message` | 交易员提示信息 |

---

## 文件结构

```
sell_monitor/
├── __init__.py          # 模块说明
├── pyproject.toml       # 子服务依赖面
├── config.py            # ExitConfig 配置中心
├── position.py          # Position 持仓对象（进场即锁定）
├── monitor.py           # check_exit_signals 核心引擎
├── storage.py           # SQLite 持久化 + CSV 日志
├── reporter.py          # 控制台状态输出
├── chart.py             # 退出路线图可视化
├── data_loader.py       # K线数据获取（efinance/baostock）
├── main.py              # argparse 入口
└── test_monitor.py      # 单元测试（17 个用例）
```

---

## 验收标准（测试覆盖）

| 测试场景 | 预期输出 | 测试状态 |
|----------|----------|----------|
| 暴跌至硬止损 | `sell_all`，理由：硬止损触发 | ✅ |
| 涨到 +2%（保本触发） | `hold`，止损从 -2% 上移到 0% | ✅ |
| 涨到 +5.1% | `sell_partial`，卖 30%，止损上移 | ✅ |
| 涨到 +15% | `sell_all`，清仓 | ✅ |
| 持仓 20 天 + 盈利 0.5% | `sell_all`，理由：时间止损 | ✅ |
| 涨到 12% 后回撤 >3% | `sell_all`，理由：移动回撤止损 | ✅ |
| 网格跳层（+15% 直接触发） | `sell_all`，非仅触发第1层 | ✅ |
| 网格多层累计（+10% 跳层） | `sell_partial`，卖 51%（累计） | ✅ |
| 止损只升不降 | 保本后回落仍保持止损价 | ✅ |
| 连续网格触发 | 第1→第2→第3层依次触发 | ✅ |
| 已关闭持仓 | `hold`，无操作 | ✅ |
| 空行情数据 | `hold`，无操作 | ✅ |

---

## 约束条件

1. **纯信号系统**：该工具不直接下单，只输出卖出信号（`action` 和 `sell_price`），由交易员手动确认或在第二阶段对接券商 API。
2. **网格卖出比例计算**：卖出比例基于"当前剩余仓位"而非初始仓位（避免清仓过早）。
3. **日志完整**：每次触发卖出信号，必须记录时间、价格、触发原因、剩余仓位，生成 CSV 便于复盘。
4. **无全局安装**：仅依赖项目 `uv sync` 安装的共享虚拟环境。
5. **批处理模式**：每次运行即退出，不使用 `while True` 持续监听。
6. **HARD_STOP_MODE 限制**：CLI `--init` 目前仅支持 `fixed_pct` 模式。`atr` 和 `swing` 模式已在 `config.py` 和 `position.py` 中实现（`Position.create_with_atr()` / `Position.create_with_swing()`），但需要额外的数据输入（ATR 值或前低价），将在后续版本暴露为 CLI 参数。

---

## 已知限制

- **HARD_STOP_MODE**：`atr` 和 `swing` 模式的工厂方法已实现但未在 CLI 中暴露。若需使用，可通过 Python API 直接调用：
  ```python
  from position import Position
  from config import ExitConfig

  config = ExitConfig(HARD_STOP_MODE='atr')
  pos = Position.create_with_atr('600550', 10.00, 1000, '2026-08-12', config, atr=0.15)
  ```

---

## 运行测试

```bash
cd trading_lab && uv run pytest services/sell_monitor/test_monitor.py -v
```

全部 18 个测试用例应通过（其中 1 个集成测试需网络，默认跳过）。
