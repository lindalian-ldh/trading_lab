# A股多维度量化预警与轮动分析系统（个人版）- 实施计划

## 0. 目标对齐（严格遵循用户需求）

每日盘后单条命令 `python main.py` 产出 Markdown 全景报告，整合：
- **模块一**：5 大原子预警（S1-S5）✓ 已完成
- **模块二**：ADX 市场环境过滤器 ✓ 已完成
- **模块三**：板块轮动（RRG 象限 + 5 维评分）⌒ 新增

**严格遵循用户提供的报告模板**（见 §6），不擅自加字段或改格式。

---

## 1. 架构（模块化单体）

```
services/alarming_monitor/
├── config.py           [扩] 新增轮动配置块（零硬编码）
├── cache.py            [新] 本地缓存层（断网降级）
├── data_loader.py      [扩] fetch_* 加 @cached
├── indicators.py       [扩] 新增 RS-Ratio / RS-Momentum / 板块 ADX
├── rotation.py         [新] RRG 象限分类 + 5 维评分
├── analysis.py         [新] 轮动汇总（排序 + Markdown 行）
├── reporter.py         [扩] generate_markdown_report 严格按用户模板
├── notifier.py         [新] 预留接口，默认 print
├── monitor.py          [不动] evaluate_signals 保持原样
├── storage.py          [不动] CSV 自累积保持原样
├── main.py             [扩] 整合三模块，单条命令出报告
└── test_monitor.py     [扩] 新增 4 个测试类
```

---

## 2. 技术约束与数据源

### 2.1 核心库
- **pandas / numpy** ✓ 已用
- **talib**：用户明确要求用于 ADX 及均线计算。但 macOS 安装需先 `brew install ta-lib`，环境当前未装。
  - **方案**：indicators.py 顶部 `try: import talib; HAS_TALIB=True except: HAS_TALIB=False`，
    优先 talib，降级 numpy 自实现（[_compute_adx](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/indicators.py#L380-L423) 已验证可用）。
  - config.py 新增 `USE_TALIB: bool = True`（默认尝试），运行时 log 实际使用的实现。

### 2.2 数据源（零 Key 优先，复用现有）
- 腾讯源 `stock_zh_index_daily_tx` / `stock_zh_a_hist_tx` → 指数/ETF 日线 ✓
- 新浪 `stock_zh_index_spot_sina` → 两市成交额 ✓
- legu `stock_market_activity_legu` → 涨跌家数 ✓
- adata → 二级回退 ✓

### 2.3 防封：本地缓存
- 路径 `data/cache/`，双层缓存（见 §4）
- 历史数据（>30 天前）永久缓存，避免每日重复请求

---

## 3. 模块三：板块轮动详细方案

### 3.1 板块篮子（用户指定）

```python
# config.py
DIVIDEND_BASKET: list = field(default_factory=lambda: ['512800', '516070'])  # 银行/公用事业
GROWTH_BASKET: list = field(default_factory=lambda: ['159995', '512720'])   # 芯片/计算机
ROTATION_BASKET: list = field(default_factory=lambda: DIVIDEND_BASKET + GROWTH_BASKET)
ROTATION_LABELS: dict = field(default_factory=lambda: {
    '512800': '银行ETF', '516070': '公用事业ETF',
    '159995': '芯片ETF', '512720': '计算机ETF',
})
ROTATION_BENCHMARK: str = 'sh000300'  # 沪深300为基准
```

> 实施前用 `fetch_etf_daily` 实测 4 个代码，若 512720 不可取则替换为同类计算机 ETF（如 515050 / 512760）。

### 3.2 RRG 象限（RS-Ratio + RS-Momentum）

**RS-Ratio**（相对强度水平，20 日平滑）：
```python
# indicators.py
def calc_rs_ratio(etf_close: pd.Series, bench_close: pd.Series, period: int = 20) -> pd.Series:
    """ETF/bench 比值的加权移动平均 WMA。
    WMA 权重线性递减：最近一日权重=period，最远一日=1。
    """
    ratio = etf_close / bench_close
    weights = np.arange(1, period + 1)
    return ratio.rolling(period).apply(lambda x: (x * weights).sum() / weights.sum(), raw=True)
```

**RS-Momentum**（相对强度动量，10 日变化率）：
```python
def calc_rs_momentum(rs_ratio: pd.Series, period: int = 10) -> pd.Series:
    """RS-Ratio 的 N 日变化率 (%)。"""
    return (rs_ratio / rs_ratio.shift(period) - 1) * 100
```

**四象限分类**（严格按用户定义）：

| 象限 | RS-Ratio | RS-Momentum | 含义 | 操作参考 |
|------|----------|-------------|------|----------|
| 领涨主线 | >1.0 | >0 | 强势+加速 | 持有 |
| 轮动初期 | <1.0 | >0 | 弱势+转强 | 关注建仓 |
| 滞后回避 | <1.0 | <0 | 弱势+减速 | 回避 |
| 退潮预警 | >1.0 | <0 | 强势+衰退 | 减仓 |

边界（=1.0 或 =0）默认归入"过渡"或保守归入弱势侧（实施时确认）。

### 3.3 5 维评分卡（权重严格 25/20/20/20/15）

| 维度 | 权重 | 公式 | 范围 |
|------|------|------|------|
| 相对动量 | 25% | `min(max((RS_Ratio-1)*10, 0), 5)` 归一化 | 0-5 |
| 动量加速度 | 20% | RS-Momentum 二阶差分 `RS_Mom.diff().diff()` 归一化 | 0-5 |
| ADX 趋势强度 | 20% | `min(ADX/30*5, 5)`（ADX=30 满分） | 0-5 |
| 资金关注度 | 20% | `min(成交额/流通市值*100, 5)` | 0-5 |
| 拥挤度折扣 | 15% | 拥挤度>CROWDING_THRESHOLD 则 -2，否则 0 | -2~0 |

**综合得分** = Σ(维度 × 权重)，范围约 [0, 5]，>4 强推荐。

> 拥挤度数据源：ETF 自身的 `amount / 流通市值`。
> ETF 流通市值：腾讯源日线无此列，用 `close * volume` 代理（量级近似，用于横向比较）。

### 3.4 时间轴对齐（严格遵循用户约束）

> "所有数据以沪深300交易日索引为基准，向前填充（ffill），确保时间轴一致。"

```python
# rotation.py
def align_to_benchmark(etf_df: pd.DataFrame, bench_df: pd.DataFrame) -> pd.DataFrame:
    """以 bench_df.date 为基准，reindex + ffill。"""
    bench_dates = sorted(bench_df['date'].unique())
    etf_indexed = etf_df.set_index('date')
    aligned = etf_indexed.reindex(bench_dates).ffill().reset_index().rename(columns={'index': 'date'})
    # 缺失首日（bench 有 ETF 无）→ dropna
    aligned = aligned.dropna(subset=['close'])
    return aligned
```

---

## 4. 本地缓存机制

### 4.1 缓存策略（双层）

[cache.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/cache.py) 装饰器：

```python
def cached(key_fn, ttl_days=1, history_split=30):
    """双层缓存装饰器。
    - 历史层（>history_split 天前）：永久缓存到 {key}_history.csv
    - 当日层（≤history_split 天）：每日刷新 {key}_latest.csv，TTL=ttl_days
    """
```

**命中规则**：
1. 缓存未过期 → 直接读 CSV，不触网
2. 缓存过期 → 触网拉取，成功覆盖；失败用旧缓存 + warning
3. 无缓存 → 触网拉取，成功写缓存；失败返回 None（降级为灰灯）

**断网降级**：网络失败优先用最新缓存，仅缺最新日数据，signals 数据不足时按现有逻辑灰灯。

### 4.2 缓存目录结构

```
data/cache/
├── index_sh000300_history.csv   # 永久缓存
├── index_sh000300_latest.csv    # 当日刷新
├── etf_512800_history.csv
├── etf_512800_latest.csv
└── ...
```

### 4.3 改造现有 fetch_*

[data_loader.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/data_loader.py)：
- `fetch_index_daily` → `@cached(key=lambda *a: f"index_{a[0]}")`
- `fetch_etf_daily` → `@cached(key=lambda *a: f"etf_{a[0]}")`
- `fetch_market_amount` / `fetch_breadth_today` → 不缓存（实时）

---

## 5. main.py 整合（每日执行逻辑）

严格按用户 §4 流程：

```python
def main():
    cfg = get_alarm_config(args.profile)
    ref_date = args.date

    # 1. 拉取数据（带缓存）：沪深300 + ETF + 总市值 + 涨跌家数
    data = _gather_data(cfg, ref_date)           # 已有 + 加缓存

    # 2. 统一对齐：沪深300交易日索引 + ffill
    aligned = align_all_to_benchmark(data, cfg)   # 新增

    # 3. 计算基础指标：ADX / RS-Ratio / RS-Momentum / 涨跌比 20 日均值
    signals, adx = _compute_signals(aligned, cfg)  # 已有
    rotation_metrics = compute_rotation_metrics(aligned, cfg)  # 新增

    # 4. 触发预警：S1-S5 + ADX 动态警报等级
    result = evaluate_signals(signals, cfg, ref_date, adx)  # 已有

    # 5. 板块轮动：象限标签 + 5 维评分排序
    rotation_result = analyze_rotation(rotation_metrics, cfg)  # 新增

    # 6. 生成 Markdown 报告
    report = generate_markdown_report(result, rotation_result, ref_date)  # 新增

    # 7. 推送（控制台 + 预留 notifier）
    notifier.send(report)
    save_report(report, ref_date)
```

**CLI**：保持现有 `--date / --profile / --no-csv`，**不加新参数**（默认即全流程）。
新增 `--no-cache`（禁用缓存强制重拉）用于调试。

---

## 6. Markdown 报告（严格按用户模板）

[reporter.py:generate_markdown_report](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/reporter.py) 输出：

```markdown
### 📊 2026-08-20 市场全景分析报告

#### 🚨 综合警报等级：**中风险**（ADX=24.5，过渡区，红灯计数: 3/5）
- **亮灯信号**：S2（放量不涨）, S3（涨跌比恶化）, S4（高股息逆势）

#### 🔄 板块轮动全景表
| 板块 | 收盘价 | ADX | RS-Ratio | RS-Momentum | 象限标签 | 5维评分 | 操作参考 |
|------|--------|-----|----------|-------------|----------|---------|----------|
| 银行ETF | 1.052 | 32.1 | 1.12 | +2.3% | 领涨主线 | 4.2 | 持有 |
| 芯片ETF | 0.987 | 26.5 | 0.98 | +1.8% | 轮动初期 | 4.8 | 关注建仓 |
| 计算机ETF | 0.921 | 18.2 | 0.95 | -0.5% | 滞后回避 | 2.1 | 回避 |

#### 📈 仓位建议
- 综合：8成 → 6成
```

**警报等级映射**（用户要求 高/中/低 三档）：
- 高风险：`effective_red_count >= effective_threshold` → **高风险**
- 警示：`effective_red_count >= WARN_THRESHOLD (2)` → **中风险**
- 正常：< 2 → **低风险**

**落盘**：同时写 `data/reports/rotation/{YYYY-MM-DD}_market_report.md`

---

## 7. notifier 接口（预留）

[notifier.py](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/notifier.py)：

```python
class Notifier:
    """通知接口（预留扩展）。默认仅 print。"""
    def send(self, content: str, **kwargs) -> None:
        print(content)

# 未来扩展点（不在本期实现）：
# class WebhookNotifier(Notifier): ...
# class EmailNotifier(Notifier): ...
# class LarkNotifier(Notifier): ...
```

main.py 通过 `notifier = ConsoleNotifier()` 注入，未来可换实现。

---

## 8. 测试计划

新增 4 个测试类，约 15 个用例：

### TestRotationIndicators
- RS-Ratio 公式正确（构造 ETF 涨/bench 平的序列 → RS>1）
- RS-Momentum 10 日变化率
- 板块 ADX 复用 _compute_adx（不同 ETF 应得到不同值）

### TestRRGQuadrant
- 四象限边界（>1/+1, <1/+1, <1/-1, >1/-1）
- 边界值（=1.0, =0）的归类

### Test5DScore
- 5 维单项计算
- 权重求和 25+20+20+20+15=100%
- 拥挤度过高触发 -2 折扣

### TestCache
- 缓存命中（mock 网络函数，二次调用不触网）
- TTL 过期重拉
- 断网降级（无网络用旧缓存）

### TestAlignment
- ETF 停牌日 ffill 正确
- 基准缺失时降级

---

## 9. 实施步骤（按依赖顺序）

| 步骤 | 文件 | 内容 | 预计改动 |
|------|------|------|---------|
| 1 | config.py | 新增轮动配置块 + USE_TALIB 开关 | +25 行 |
| 2 | cache.py | 缓存装饰器 + 双层存储 | +120 行 |
| 3 | data_loader.py | 给 fetch_* 加 @cached | +15 行 |
| 4 | indicators.py | RS-Ratio / RS-Momentum + talib 加速路径 | +80 行 |
| 5 | rotation.py | RRG 象限 + 5 维评分 + 对齐 | +180 行 |
| 6 | analysis.py | 轮动汇总 + Markdown 表格行 | +60 行 |
| 7 | notifier.py | ConsoleNotifier | +20 行 |
| 8 | reporter.py | generate_markdown_report | +90 行 |
| 9 | main.py | 整合三模块 + 默认全流程 | +40 行 |
| 10 | test_monitor.py | 4 个测试类 | +200 行 |
| 11 | 实测 + 全量测试 | 验收 | - |

---

## 10. 验收标准（严格对齐用户 §6）

- [x] S1 使用总市值（非流通市值）— 已用 `TOTAL_MARKET_CAP` 兜底
- [x] S2 正确使用日均量（非总量）— 已修正量级口径
- [x] 所有阈值在 config.py — 已实现，新增模块同样遵循
- [ ] `python main.py` 5 秒内完成全流程（命中缓存时秒级）
- [ ] 断网时用本地缓存运行（仅缺最新日数据）
- [ ] 输出 Markdown 报告含综合警报等级 + 板块轮动全景表（严格按模板）
- [ ] 全量测试通过（原有 48 + 新增 ~15 = 63+）

---

## 11. 风险与降级

| 风险 | 应对 |
|------|------|
| talib 不可用 | 顶部 try/except，降级 numpy 自实现 |
| ETF 代码 512720 不可取 | 实测后替换为同类（515050/512760） |
| 腾讯源不稳定 | 已有 adata 二级回退 + 缓存降级 |
| ETF 停牌缺数据 | ffill 前值填充；首日缺失 dropna |
| 5 维某维度数据缺失 | 该维度计 0 分 + warning，不阻断整体 |
| 5 秒性能不达标 | 缓存命中时秒级；首次拉取并发可优化 |
