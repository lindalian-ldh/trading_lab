# 维5 拆方向 + 超卖反弹观察系统 升级计划

## 摘要

将维5拥挤度从"绝对值偏离"改为"只惩罚上涨偏离+极度放量"，新增**超卖反弹观察系统**（oversold_flag + rebound_level 三级，不进总分），作为操作矩阵的"机会提示"与"降级保护"。输出表加2列（超卖/反弹），报告顶部摘要加超卖计数。

## 当前状态分析

### 维5（[rotation.py:461-488](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/rotation.py#L461-L488)）
当前用 `|close - MA20| / close > 2×ATR/close` **绝对值**判断拥挤——上涨偏离和下跌偏离都会扣分。问题：下跌偏离（超卖）不该被惩罚，这是机会而非拥挤。

### calc_adx_etf（[indicators.py:1370-1443](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/indicators.py#L1370-L1443)）
talib 路径已调用 `talib.PLUS_DI`/`talib.MINUS_DI` 取得完整数组，但只取末值丢弃。需扩展返回末2日（今日+昨日）以支持"+DI 上穿 -DI"判断。

### calc_amount_percentile（[indicators.py:1481-1526](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/indicators.py#L1481-L1526)）
只返回今日分位。需新增函数返回末5日分位序列，支持"分位从<20%回升到>50%"判断。

### _resolve_action（[rotation.py:269-353](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/rotation.py#L269-L353)）
当前双列决策矩阵已含象限×大盘×波动率三维。需加第4维：rebound_level 作为机会提示/降级保护，不覆盖地板/天花板。

### 表格（[reporter.py:431-481](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/reporter.py#L431-L481)）
当前9列（板块/收盘价/ADX/RS-Ratio/RS-Momentum/象限标签/5维评分/有仓位/无仓位）。加2列→11列：超卖（是/否）+ 反弹（无/观察/候选/确认）。

### summary（[analysis.py:159-178](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/analysis.py#L159-L178)）
当前含 leader/laggard/bench_state 计数。加 oversold_count/rebound_candidate_count/rebound_confirmed_count。

## 提议改动

### 1. config.py（[config.py:228-235](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/config.py#L228-L235)）

在维5配置后新增超卖反弹配置块：

```python
# 超卖反弹观察系统（不进总分，仅作操作参考的机会提示/降级保护）
# 一级（观察）：close < MA20 且 MA20 - close > OVERSOLD_ATR_MULT × ATR
# 二级（候选）：一级 + 任一（RS-Momentum>0 / 象限改善 / 分位从<20%回升到>50%）
# 三级（确认）：二级 + 任一（RS-Ratio回升+RS-Momentum>0连续2日 / +DI上穿-DI / 分位>60%且站回MA5）
OVERSOLD_ATR_MULT: float = 2.0              # 超卖偏离阈值（MA20-close > N×ATR）
OVERSOLD_AMOUNT_LOW_PCT: float = 20.0      # 缩量分位（二级条件之一）
OVERSOLD_AMOUNT_HIGH_PCT: float = 50.0     # 回升分位（二级条件之一）
REBOUND_AMOUNT_CONFIRM_PCT: float = 60.0   # 确认级量能分位
REBOUND_LOOKBACK: int = 5                  # 反弹判断历史回看天数（分位回升/RS-Momentum连续）
```

### 2. indicators.py

**改动 A：扩展 `calc_adx_etf` 返回末2日 +DI/-DI**（[indicators.py:1370-1443](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/indicators.py#L1370-L1443)）

talib 路径已取 `pdi_arr`/`mdi_arr` 全数组，numpy 降级路径 `_compute_adx` 现返回 `(adx, plus_di, minus_di)` 标量。改：

- talib 路径：返回 `pdi_arr[-2:]` 和 `mdi_arr[-2:]`（末2日数组）
- numpy 降级路径：扩展 `_compute_adx` 返回 `(adx_arr[-2:], plus_di_arr[-2:], minus_di_arr[-2:])`，或新增 `_compute_adx_series` 函数

返回 dict 加字段：
```python
{
    'adx': float,                    # 最新
    'plus_di': float,                # 最新（兼容现有）
    'minus_di': float,               # 最新（兼容现有）
    'direction': 'up'/'down',        # 最新方向
    'plus_di_prev': float | None,    # 昨日 +DI
    'minus_di_prev': float | None,   # 昨日 -DI
    'di_cross_up': bool,             # 今日+DI>-DI 且 昨日+DI<=-DI（上穿）
}
```

**改动 B：新增 `calc_amount_percentile_series`**（在 [indicators.py:1526](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/indicators.py#L1526) 后）

返回末 N 日（默认5日）的成交额分位序列，用于"分位从<20%回升到>50%"判断：

```python
def calc_amount_percentile_series(etf_df, cfg, days=5) -> Optional[list[float]]:
    """返回末 N 日成交额历史分位（0~100），失败返回 None。"""
    # 滚动计算：对末 N 日每一日，取其过去 lookback 日的分位
```

### 3. rotation.py

**改动 A：维5 拆方向**（[rotation.py:461-488](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/rotation.py#L461-L488)）

```python
# 维5 拥挤度：只惩罚"涨过头 + 极度放量"，不惩罚"跌过头"（跌过头→超卖观察）
crowding_score = 5.0
crowding_penalty_applied = False
crowding_reason = ''
oversold_flag = False  # 新增：超卖标记（不进总分，仅观察）
oversold_deviation = 0.0  # 超卖偏离幅度（MA20-close）/close×100

if (volatility is not None and np.isfinite(volatility)
        and 'close' in etf_df.columns and len(etf_df) >= 20):
    close_series = etf_df['close'].astype(float).dropna()
    if len(close_series) >= 20:
        ma20_close = float(close_series.iloc[-20:].mean())
        last_close = float(close_series.iloc[-1])
        if last_close > 0 and ma20_close > 0:
            threshold_pct = cfg.CROWDING_PRICE_DEVIATION_ATR_MULT * volatility
            # 上涨偏离 → 拥挤扣分
            if last_close > ma20_close:
                price_dev_pct = (last_close - ma20_close) / last_close * 100.0
                if price_dev_pct > threshold_pct:
                    crowding_score = max(0.0, 5.0 - cfg.CROWDING_PENALTY)
                    crowding_penalty_applied = True
                    crowding_reason = f'上涨偏离{price_dev_pct:.1f}%>{threshold_pct:.1f}%'
            # 下跌偏离 → 超卖标记（不扣分）
            else:
                price_dev_pct = (ma20_close - last_close) / last_close * 100.0
                if price_dev_pct > threshold_pct:
                    oversold_flag = True
                    oversold_deviation = price_dev_pct

# 条件2：成交额分位 > 90%（极度放量，拥挤）
if not crowding_penalty_applied and amount_pct is not None:
    if amount_pct > cfg.CROWDING_AMOUNT_PCT_HIGH:
        crowding_score = max(0.0, 5.0 - cfg.CROWDING_PENALTY)
        crowding_penalty_applied = True
        crowding_reason = f'成交额分位{amount_pct:.0f}%>{cfg.CROWDING_AMOUNT_PCT_HIGH:.0f}%'
```

calc_5d_score 返回值加 `oversold_flag`、`oversold_deviation`（仅标记，不进 total）。

**改动 B：新增 `_classify_oversold_rebound` 函数**（在 [rotation.py:597](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/rotation.py#L597) 后，apply_cross_sectional_mom_acc 之前）

```python
def _classify_oversold_rebound(
    etf_df, rs_ratio_series, rs_momentum_series,
    adx_info, amount_pct_series, quadrant_today, quadrant_prev,
    bench_state, cfg
) -> tuple[str, str]:
    """超卖反弹分级（不进总分，仅作操作参考提示）。
    
    返回 (rebound_level, rebound_note)：
        rebound_level: '无' / '观察' / '候选' / '确认'
        rebound_note: 触发原因简述
    """
    # 一级：超卖观察
    #   close < MA20 且 MA20 - close > 2×ATR
    #   用 calc_5d_score 已算的 oversold_flag 传入，或此处重算
    
    # 二级：超卖反弹候选（一级 + 任一）
    #   - RS-Momentum > 0（今日）
    #   - 象限改善（quadrant_prev=🔴且 quadrant_today=🟡，或其他改善）
    #   - 成交额分位从<20%回升到>50%（用 amount_pct_series[0]<20 且 amount_pct_series[-1]>50）
    
    # 三级：超卖反弹确认（二级 + 任一）
    #   - RS-Ratio 回升（今日>昨日）且 RS-Momentum > 0 连续 2 日
    #   - +DI 上穿 -DI（adx_info['di_cross_up']）
    #   - 成交额分位 > 60% 且价格站回 MA5（close > MA5）
```

象限改善判断（昨日→今日）：
- 🔴滞后回避 → 🟡轮动初期（RS-Momentum 转正）
- 🔴滞后回避 → 🟠退潮预警（RS-Ratio 突破1.0）
- 🟡轮动初期 → 🟢领涨主线（RS-Ratio 突破1.0）
- 🟠退潮预警 → 🟢领涨主线（RS-Momentum 转正）
- 🔴 → 🟢 也算改善

昨日象限用 `classify_rrg_quadrant(rs_ratio_series.iloc[-2], rs_momentum_series.iloc[-2], cfg)` 计算（在 analyze_single_etf 中算）。

**改动 C：`_resolve_action` 加 rebound_level 参数**（[rotation.py:269-353](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/rotation.py#L269-L353)）

签名改为：
```python
def _resolve_action(quadrant, total_score, bench_state, volatility, cfg,
                    rebound_level='无') -> tuple:
```

在现有矩阵基础上，叠加 rebound_level 修正（不覆盖地板/天花板）：

| 象限 | rebound_level | 修正 |
|------|--------------|------|
| 🟢领涨主线 | 任何 | 不变（有仓持有，无仓等站回MA20） |
| 🟡轮动初期 | 候选 | 无仓：观望→可轻仓（原本 below 也是观望，二级信号可轻仓试） |
| 🟡轮动初期 | 确认 | 无仓：可轻仓→可建仓（需 bench_state != 'below'） |
| 🟠退潮预警 | 确认 | 有仓：减仓→持有观察（需 bench_state == 'above'） |
| 🔴滞后回避 | 任何 | 不变（清仓/回避，超卖只是"别追空"） |

**关键原则**：大盘 below 时，三级信号降一级处理（确认→候选，候选→观察），避免接飞刀。

**改动 D：`calc_5d_score` 调用链**（[rotation.py:500-524](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/rotation.py#L500-L524)）

`calc_5d_score` 已接收 etf_df、rs_momentum_series、quadrant、bench_state、volatility。但缺 rs_ratio_series（只传了 rs_ratio_last）和 adx_info（内部重算）。

方案：在 `analyze_single_etf` 中算好 rebound_level，注入到 score_5d dict，不修改 calc_5d_score 签名（保持单只 ETF 纯函数的可测试性）。

```python
# analyze_single_etf 中（[rotation.py:723-728](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/rotation.py#L723-L728)）：
result['score_5d'] = calc_5d_score(...)

# 新增：超卖反弹分级（在 analyze_single_etf 里，用完整 Series 上下文）
adx_info_full = calc_adx_etf(aligned_etf, cfg)  # 已扩展返回 di_cross_up
amount_pct_series = calc_amount_percentile_series(aligned_etf, cfg, days=5)
quadrant_prev = None
if len(rs_ratio) >= 2 and len(rs_mom) >= 2:
    quadrant_prev, _ = classify_rrg_quadrant(
        float(rs_ratio.iloc[-2]), float(rs_mom.iloc[-2]), cfg
    )
rebound_level, rebound_note = _classify_oversold_rebound(
    aligned_etf, rs_ratio, rs_mom, adx_info_full, amount_pct_series,
    quadrant, quadrant_prev, bench_state, cfg
)
# 注入到 score_5d
result['score_5d']['oversold_flag'] = ...
result['score_5d']['rebound_level'] = rebound_level
result['score_5d']['rebound_note'] = rebound_note
# 重算 action（叠加 rebound 修正）
new_with, new_without = _resolve_action(
    quadrant, result['score_5d']['total_score'], bench_state, volatility, cfg,
    rebound_level=rebound_level
)
result['score_5d']['action_with'] = new_with
result['score_5d']['action_without'] = new_without
result['score_5d']['action_hint'] = f'{new_with}/{new_without}'
```

**改动 E：`apply_cross_sectional_mom_acc` 适配**（[rotation.py:595-602](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/rotation.py#L595-L602)）

横截面后处理重算 action 时也要传 rebound_level：
```python
rebound_level = s5.get('rebound_level', '无')
new_with, new_without = _resolve_action(
    quadrant, new_total, bench_state, volatility, cfg,
    rebound_level=rebound_level
)
```

### 4. analysis.py

**改动 A：summary 加超卖计数**（[analysis.py:159-178](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/analysis.py#L159-L178)）

```python
oversold_count = sum(1 for r in results
    if r.get('data_sufficient') and r.get('score_5d', {}).get('oversold_flag'))
rebound_candidate = sum(1 for r in results
    if r.get('data_sufficient') and r.get('score_5d', {}).get('rebound_level') in ('候选', '确认'))
rebound_confirmed = sum(1 for r in results
    if r.get('data_sufficient') and r.get('score_5d', {}).get('rebound_level') == '确认')
summary.update({
    'oversold_count': oversold_count,
    'rebound_candidate_count': rebound_candidate,
    'rebound_confirmed_count': rebound_confirmed,
})
```

**改动 B：build_rrg_summary 加超卖摘要**（[analysis.py:245-268](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/analysis.py#L245-L268)）

在摘要末尾加：
```python
oversold = summary.get('oversold_count', 0)
rebound_c = summary.get('rebound_candidate_count', 0)
if oversold > 0:
    oversold_part = f' · 超卖观察 {oversold} 只'
    if rebound_c > 0:
        oversold_part += f'（反弹候选 {rebound_c} 只）'
    # 末尾追加
```

**改动 C：build_rotation_table_rows 加2列**（[analysis.py:188-239](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/analysis.py#L188-L239)）

表头加 `| 超卖 | 反弹 |`，行加：
```python
oversold_s = '是' if s5.get('oversold_flag') else '否'
rebound_s = s5.get('rebound_level', '无')
# 数据不足：'—'
```

### 5. reporter.py

**改动 A：表头加2列**（[reporter.py:431-432](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/reporter.py#L431-L432)）

```python
lines.append('| 板块 | 收盘价 | ADX | RS-Ratio | RS-Momentum | 象限标签 | 5维评分 | 有仓位 | 无仓位 | 超卖 | 反弹 |')
lines.append('|------|--------|-----|----------|-------------|----------|---------|----------|----------|------|------|')
```

**改动 B：行输出加2列**（[reporter.py:437-481](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/reporter.py#L437-L481)）

数据不足行：`action_with_s = '—'`、`action_without_s = '—'`、`oversold_s = '—'`、`rebound_s = '—'`

正常行：
```python
oversold_s = '是' if s5.get('oversold_flag') else '否'
rebound_s = s5.get('rebound_level', '无')
# 评分列加反弹注释后缀
if rebound_s != '无':
    score_s += f' [{rebound_s}]'
```

行末：`... | {action_with_s} | {action_without_s} | {oversold_s} | {rebound_s} |`

**改动 C：模板注释同步**（[reporter.py:181-185](file:///Users/a801/Linda/Work/project/gupiao-assistant/trading_lab/services/alarming_monitor/reporter.py#L181-L185)）

### 6. README.md

更新 4.4 维5 描述（拆方向），新增 4.6 节"超卖反弹观察系统"，含：
- 三级条件表
- 与操作矩阵的耦合规则
- 设计意图（不进总分、不覆盖地板/天花板、大盘 below 降级）
- 已知陷阱（超卖可更超卖、ATR 放大、不替代止损、时间约束待加）

### 7. test_monitor.py

- 场景1/2 的 score_5d 断言加 `oversold_flag`/`rebound_level` 字段
- reporter 测试的表头断言改 11 列
- mock 数据加 `oversold_flag`/`rebound_level`/`rebound_note`

## 假设与决策

1. **超卖判断复用维5已算的 ma20_close/volatility**：避免重复计算，但需从 calc_5d_score 暴露 oversold_flag。或者在 _classify_oversold_rebound 里重算（更独立）。决策：calc_5d_score 算 oversold_flag（已有数据），analyze_single_etf 算 rebound_level（需更多上下文）。

2. **象限改善判断**：用昨日 rs_ratio/rs_mom 重算昨日象限，对比今日。决策：在 analyze_single_etf 里算（有完整 Series），不传给 calc_5d_score。

3. **+DI 上穿 -DI**：扩展 calc_adx_etf 返回 `plus_di_prev`/`minus_di_prev`/`di_cross_up`。talib 路径取 `pdi_arr[-2]`/`mdi_arr[-2]`；numpy 降级路径需扩展 `_compute_adx` 返回末2日。

4. **成交额分位序列**：新增 `calc_amount_percentile_series`，滚动计算末5日每日的历史分位。

5. **时间约束（3-5日失效）暂不实现**：用户提到作为"陷阱"警告，但"最小改法"不含此项。作为 TODO 在 README 注明。

6. **大盘 below 时三级降一级**：在 _resolve_action 的 rebound 修正逻辑里实现：`if bench_state == 'below' and rebound_level == '确认': rebound_level_effective = '候选'`。

## 验证步骤

1. 跑 `pytest test_monitor.py -o pythonpath=` 全过（71+测试）
2. 跑 `uv run services/alarming_monitor/main.py --profile kc --rotation-only --no-report` 实跑
3. 检查输出表有11列，超卖/反弹字段正确
4. 检查报告顶部摘要含"超卖观察 N 只"
5. 验证：滞后回避+超卖 仍是 清仓/回避（不变）；轮动初期+超卖候选 无仓变可轻仓
