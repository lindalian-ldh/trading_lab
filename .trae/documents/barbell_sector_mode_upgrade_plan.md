# 哑铃策略系统板块模式升级计划

## Context

现有 `services/barbell_system/` 已实现风格哑铃 MVP：固定资产池（中证红利/红利低波 + 中证2000增强/成长ETF），三个信号（股债利差、滚动相关性、拥挤度），打分卡权重判定，再平衡与失效预警，CSV 日志。所有可调参数集中在 `barbell_config.py`（注意：spec 写的 `config.py` 实为 `barbell_config.py`）。

本次升级在不重写核心逻辑、保留 `MODE="style"` 完全向后兼容的前提下，新增 `MODE="sector"`：动态板块筛选 + 板块ETF映射 + 参数覆盖 + 预警增强 + 信号验证回测。核心原则：复用 `signals.py`/`weights.py`/`rebalance.py` 的函数签名，通过 main.py 入口处的参数/资产池覆盖实现模式切换。

用户已确认的 4 个关键决策：
1. **回测**：仅做"信号验证回测"（验证哑铃信号是否有效、再平衡是否有超额），不做完整 T+1 涨跌停模拟
2. **配置文件**：沿用 `barbell_config.py` 文件名，不重命名
3. **失效预警**：新增独立 `alert.py` 模块，原 `rebalance.check_failure_alert` 不动，style 模式零回归
4. **板块数据源**：复用 `services/bankuai-service/` 的 zzshare 数据层与 percentile 辅助函数

## Design Principles

### 参数覆盖模式（Param Overlay Pattern）
为保持 `signals.py`/`weights.py`/`rebalance.py` 函数签名完全不变（spec 明确要求），采用"运行时覆盖"模式：
- `barbell_config.py` 顶层保留所有现有常量作为 style 默认值
- 新增 `MODE`、`STYLE_PARAMS`、`SECTOR_PARAMS` 三个常量
- `main.py` 启动时若 `MODE=="sector"`，将 `SECTOR_PARAMS` 中的字段写回 `cfg.SPREAD_FLOOR`、`cfg.CORRELATION_ALERT`、`cfg.REBALANCE_THRESHOLD` 等顶层变量
- signals.py/weights.py/rebalance.py 仍读 `cfg.SPREAD_FLOOR` 等，无感知

### 资产池覆盖模式
- `cfg.DEFENSIVE` / `cfg.OFFENSIVE` 改为模块级可变变量（原本就是）
- sector 模式下，`main.py` 调用 `screener.run_sectors()` 得到候选板块，调用 `sector_etf_map.pick_etf()` 选定 ETF 后写回 `cfg.DEFENSIVE` / `cfg.OFFENSIVE`
- style 模式不动 `cfg.DEFENSIVE_POOL`[0] 默认值，零回归

### 失效预警双轨
- `rebalance.check_failure_alert()` 原 3 条件保留，style 模式调用
- 新 `alert.py` 模块 `check_failure_alert_sector()` 含 8 条件（原 3 + 新 5），sector 模式调用
- 阈值全部从 `cfg.FAILURE_*` 读取，sector 模式同样由 main.py 覆盖

## Files to Modify

### 1. `services/barbell_system/barbell_config.py` (扩展)
- 保留所有现有常量不动
- 新增顶层常量：
  ```python
  MODE = "style"  # "style" | "sector"
  
  STYLE_PARAMS = {
      "base_weight": 0.50, "rebalance_threshold": 0.10,
      "correlation_alert": 0.50, "spread_floor": 1.5, "spread_high": 2.5,
      "corr_window": 60, "zscore_lookback": 120, "zscore_window": 20,
      "zscore_low": 1.5, "zscore_high": 2.5, "corr_low": 0.2,
      "run_frequency": 14, "failure_corr": 0.5, "failure_spread": 1.5,
      "failure_down_days": 5, "failure_required": 2, "failure_reduce_pp": 0.20,
      "failure_wait_days": 10,
  }
  
  SECTOR_PARAMS = {
      "base_weight": 0.60, "rebalance_threshold": 0.08,
      "correlation_alert": 0.65, "spread_floor": 2.0, "spread_high": 3.0,
      "corr_window": 60, "zscore_lookback": 120, "zscore_window": 20,
      "zscore_low": 1.5, "zscore_high": 2.0, "corr_low": 0.2,
      "run_frequency": 7, "failure_corr": 0.65, "failure_spread": 2.0,
      "failure_down_days": 3, "failure_required": 2, "failure_reduce_pp": 0.20,
      "failure_wait_days": 10,
      # 板块筛选阈值
      "defensive_div_yield_min": 3.0, "defensive_vol_lookback": 60,
      "defensive_vol_percentile_max": 30, "defensive_val_pct_max": 50,
      "offensive_momentum_lookback": 250, "offensive_momentum_top_pct": 20,
      "offensive_turnover_up_days": 10,
      "top_n_sectors": 3,  # 每端取前3
      # ETF 流动性
      "etf_amount_min": 5000, "etf_size_min": 5, "etf_track_err_max": 2.0,
      # 中间地带排除
      "exclude_pct_low": 40, "exclude_pct_high": 60,
      # 政策事件（手工输入）
      "policy_flag_offensive": [],
  }
  
  # 板块哑铃用的运行时占位（由 screener 动态填充）
  DEFENSIVE_SECTOR_POOL = []
  OFFENSIVE_SECTOR_POOL = []
  ```

### 2. `services/barbell_system/data_fetcher.py` (扩展)
- 保留所有现有函数签名不动
- 新增 `fetch_sector_etf_kline(etf_code, days=None)` —— 板块 ETF 行情，优先 efinance，baostock 备选（注意：spec 要求"板块ETF优先用 efinance"）
- 新增 `fetch_sector_dividend_yield(sector_name, constituents)` —— 板块加权股息率；优先 akshare 指数估值；失败降级用 `cfg.DIVIDEND_YIELD_FALLBACK`
- 新增 `compute_etf_liquidity(etf_code, lookback=20)` —— 返回 `{avg_amount, fund_size, has_track_err}` 用于 `sector_etf_map.py` 流动性过滤
- 新增 `fetch_sector_index_kline(sector_name, days=None)` —— 板块指数行情，用于 `screener.py` 计算动量/波动率

### 3. `services/barbell_system/signals.py` (不动)
- 函数签名完全不变；继续从 `cfg.SPREAD_FLOOR` 等读阈值
- main.py 在 sector 模式下提前覆盖 `cfg` 字段，signals.py 自动使用新阈值

### 4. `services/barbell_system/weights.py` (小改)
- `determine_weights(score)` 签名不变；继续用 `cfg.BASE_WEIGHT` 作为"标准配置"权重
- sector 模式下 main.py 覆盖 `cfg.BASE_WEIGHT = 0.60`（防御60/进攻40），其余阈值（≥2/0~1/≤-1 三档）保持不变
- 零代码改动 — 只需 main.py 一行覆盖

### 5. `services/barbell_system/rebalance.py` (不动)
- 原 `check_failure_alert` 完全不动，style 模式调用
- main.py 覆盖 `cfg.REBALANCE_THRESHOLD` 实现周度 8% 阈值
- sector 模式调用新 `alert.check_failure_alert_sector()`

### 6. `services/barbell_system/main.py` (重构)
核心改动：根据 `cfg.MODE` 分支。

```python
def run():
    if cfg.MODE == "sector":
        _apply_sector_params()  # 覆盖 cfg.* 字段
        _resolve_sector_pools()  # 调用 screener + sector_etf_map，写回 cfg.DEFENSIVE/OFFENSIVE
    # ... 后续流程不变，但失效预警按 MODE 分支：
    alert = (alert_mod.check_failure_alert_sector(...) 
             if cfg.MODE == "sector" 
             else rebalance.check_failure_alert(...))
    # CSV 日志新增字段：mode, defensive_sector, offensive_sector, screener_score, policy_flag
```

- `_apply_sector_params()` —— 将 SECTOR_PARAMS 字段写回 cfg 顶层变量
- `_resolve_sector_pools()` —— 调用 `screener.run_sectors()` → `sector_etf_map.pick_etf()` → 写回 `cfg.DEFENSIVE`/`cfg.OFFENSIVE`
- `_print_report()` —— 新增板块代码/筛选得分/政策标志字段
- `_write_log()` —— CSV 新增字段：`mode, defensive_sector, offensive_sector, screener_score, policy_flag`
- 新增 `_write_sector_report()` 子函数（或分支）输出板块筛选明细

## New Files

### 7. `services/barbell_system/screener.py` (新增)
板块筛选模块。复用 `services/bankuai-service/data_loader.py` 与 `leader_selector._percentile` 辅助函数。

```python
def run_sectors(today=None):
    """主入口。返回:
    {
      "defensive": [{"name": "银行", "score": 0.85, "factors": {...}}, ...],
      "offensive": [{"name": "半导体", "score": 0.78, "factors": {...}}, ...],
      "excluded": [...],  # 中间地带
      "warnings": [...],  # 因子缺失警告
    }
    """
```

**复用 bankuai-service：**
```python
import sys; sys.path.insert(0, str(Path(__file__).parent.parent / "bankuai-service"))
import data_loader as bk_data  # zzshare 数据层
from leader_selector import _percentile  # 复用百分位工具
```

**防御端因子（MVP 实现可计算项）：**
- 股息率（TTM）> 3% — akshare 指数估值接口；缺失用中位数填
- 60日波动率 — 用 `fetch_sector_index_kline` 收益率 std；分位 < 30%
- 估值分位 < 50% — akshare PE/PB 历史分位
- ROE 稳定性 / 现金流比率 — 标注 `TODO 待财务数据接入`，用中位数填并打 warning

**进攻端因子（MVP）：**
- 12月动量排名前20% — 用 `fetch_sector_index_kline` 计算过去 250 日涨幅
- 成交额占比上升 — 近 10 日 vs 前 10 日
- 营收/净利润增速 / 研发占比 / 行业景气度 — 标注 TODO，中位数填充

**中间地带排除：** 防御得分与进攻得分均在 40%-60% 分位的板块直接剔除。

### 8. `services/barbell_system/sector_etf_map.py` (新增)
板块 → ETF 映射 + 流动性过滤。

```python
SECTOR_ETF_MAP = {
    "银行": "512800", "煤炭": "515220", "电力": "159611",
    "半导体": "512480", "人工智能": "515070", "机器人": "562500",
    "食品饮料": "515170", "医药": "512010", "新能源": "516160",
    "军工": "512660", "证券": "512880", "房地产": "512200",
    "钢铁": "515210", "有色": "512400", "化工": "159870",
    "汽车": "516110", "家电": "159996", "传媒": "512980",
    "计算机": "512720", "电子": "159997", "通信": "515880",
    "轻工": "159608", "农业": "159825", "环保": "512580",
    "机械": "159886", "建材": "159745", "建筑": "159749",
    # 兜底
}

def pick_etf(sector_name, cfg_params=None):
    """为板块选 1 只流动性合格 ETF。返回 {code, efinance_code, baostock_code, name, avg_amount, fund_size, fallback_reason}
    流动性过滤：日均成交额 > 5000万、基金规模 > 5亿、跟踪误差 < 2%
    无合格 ETF：返回 {"fallback": "field_fund", "name": sector_name, "reason": "..."}
    """

def filter_by_liquidity(etf_codes, cfg_params=None):
    """批量过滤，返回合格 ETF 列表"""
```

### 9. `services/barbell_system/alert.py` (新增)
sector 模式失效预警增强。8 条件，任意 2 条同时成立触发降级。

```python
def check_failure_alert_sector(corr_value, spread_value, def_df, off_df,
                                policy_flag=None, corr_history=None):
    """8 条件：
    [原3] 1. 60日相关性 > FAILURE_CORR_THRESHOLD
          2. 股债利差 < FAILURE_SPREAD_THRESHOLD
          3. 两端同时连续 FAILURE_DOWN_DAYS 日下跌
    [新5] 4. 两端同时跌破 20 日均线
          5. 政策事件标志 (policy_flag 中任意 True)
          6. 60日相关性从 <0.3 升至 >0.6 (corr_history 提供)
          7. 股债利差 < FAILURE_SPREAD_THRESHOLD * 1.0 (sector=2.0%) [与 #2 重复则合并]
          8. 两端同时连续 3 个交易日下跌 [sector 严格版]
    返回结构与 rebalance.check_failure_alert 一致
    """
```

辅助函数复用 `rebalance._both_consecutive_down`（直接 import 调用，不重复实现）。

### 10. `services/barbell_system/backtest.py` (新增 — MVP 信号验证)
spec 要求仅做信号验证（非完整 T+1 模拟）。

```python
def run_backtest(mode="style", start_date=None, end_date=None):
    """滚动回测哑铃信号 + 再平衡决策，输出：
    - 累计收益曲线 vs 等权基准
    - 三信号 IC（信息系数）
    - 再平衡触发次数与超额收益
    - CSV: data/barbell_backtest_{mode}_{start}_{end}.csv
    不模拟 T+1/涨跌停/成本 — 仅信号有效性验证。
    """
```

### 11. `services/barbell_system/README.md` (更新)
新增"板块哑铃模式"章节：MODE 切换、STYLE_PARAMS/SECTOR_PARAMS 对照表、新增模块说明、运行命令、回测用法。

### 12. `services/barbell_system/pyproject.toml` (更新)
如有可选依赖（akshare 已在根 pyproject 中），仅更新文档字符串。实际依赖仍由根 `pyproject.toml` 管理。

## Implementation Order

1. **`barbell_config.py`** — 加 MODE/STYLE_PARAMS/SECTOR_PARAMS（不改原常量）
2. **`sector_etf_map.py`** — 板块→ETF 映射表 + `pick_etf` + `filter_by_liquidity`
3. **`screener.py`** — 板块筛选（复用 bankuai-service）
4. **`data_fetcher.py` 扩展** — 板块 ETF/指数/股息率/流动性
5. **`alert.py`** — sector 8 条件预警
6. **`backtest.py`** — 信号验证 MVP
7. **`main.py` 重构** — MODE 分支、参数覆盖、资产池覆盖、日志扩展
8. **`README.md`** — 文档更新
9. **运行验证** — style 模式回归 + sector 模式端到端

## Verification

### Style 模式回归测试
```bash
cd /Users/a801/Linda/Work/project/gupiao-assistant/trading_lab
uv run python services/barbell_system/main.py
# 预期：与升级前输出完全一致（CSV 字段新增的 mode/sector 字段为空或 "style"）
# 比对升级前最后一次运行日志 data/barbell_log_YYYYMMDD.csv
```

### Sector 模式端到端
```bash
# 临时修改 barbell_config.py 中 MODE = "sector"（或环境变量 BARBELL_MODE=sector）
BARBELL_MODE=sector uv run python services/barbell_system/main.py
```

预期输出：
- 控制台打印板块筛选明细（每端前 3 板块 + 得分 + 候选 ETF）
- 三信号按 SECTOR_PARAMS 阈值（spread_floor=2.0, corr_alert=0.65, zscore_high=2.0）计算
- 再平衡阈值 8%（周度）
- 失效预警检查 8 条件
- CSV 日志新增字段：mode=sector, defensive_sector=银行, offensive_sector=半导体, screener_score=0.85, policy_flag=

### 信号验证回测
```bash
uv run python -c "from services.barbell_system.backtest import run_backtest; run_backtest(mode='style', start_date='2024-01-01')"
# 输出 data/barbell_backtest_style_20240101_20260917.csv
```

### 异常降级验证
- 断网情况下 sector 模式应能：板块筛选用上次缓存或降级为中位数填充并 warning；ETF 流动性失败用 fallback 标记；系统不崩溃。

## Hard Constraints (per project memory)

- 所有数据源免费、不需付费 API key
- 全程 < 5s 运行（`python main.py`）— 板块筛选须带本地缓存
- 异常降级保证可运行，不崩溃
- CSV 列扩展自动兼容
- baostock 主源、efinance 备源（板块 ETF 反过来：efinance 主、baostock 备）
- 复用 bankuai-service 数据层，不重复造轮子
- signals.py/weights.py/rebalance.py 函数签名零改动

## Out of Scope

- 完整 T+1/涨跌停/交易成本回测模拟（用户已确认本次只做信号验证）
- ROE 稳定性 / 现金流比率 / 研发占比 因子的真实数据接入（MVP 用中位数填，标注 TODO）
- 政策事件 API 自动抓取（MVP 留 `policy_flag` 字段，人工输入）
- 高频交易逻辑（系统定位周级别战术再平衡）
