# 交接总结 · Phase 2/3（ETF 择时增强 + 主题观察哨）

> **用途**：新开会话时把这**一份**贴进去即可恢复上下文。
> **最后更新**：2026-10-05
> **配套文档（按需查阅，不必先读）**：
> - 完整规格与全部完成记录：[`增强方案v2-板块轮动面板与ETF择时.md`](增强方案v2-板块轮动面板与ETF择时.md)
>   （含 **P0.5 冻结基准 v1**（12 条修订记录）+ 「P2.1~P2.4 完成记录」+「P3 完成记录」+「P0.4c」）
> - 新人上手 / 命令与读数：[`命令速查-Phase2与Phase3.md`](命令速查-Phase2与Phase3.md)
> - 旧文档 [`交接总结.md`](交接总结.md) 的**§二「已验证统计结论」仍然有效**，
>   但它的「下一步」两件事已被取代 —— **以本文件为准**

---

## 0. 系统状态总览（**交接用，权威**）

### 0.1 一句话状态

**Phase 0 / 1 / 2 / 3 与 P0.4c 全部完成。**
**结论：右侧择时（L1 结构破坏 / L2 均线转向 / RS 相对强度）未通过 P0.5 预注册判据
⇒ 不进入仓位逻辑，退化为「观察哨」（只显示、不决策）。**
**OOS 锁定段（2024-01-01 ~ 2026-09-30）至今未动用**，留给未来功效更好的假设。
**P0.4c 打通了「合成主题指数」能力**，解决了原先"3 个主题没有可用指数"的问题
（含你的主线「半导体设备」所在的半导体系，但那 4 个主题目前仍用宽泛代理）。

### 0.2 已完成（按层）

| 层 | 内容 | 关键产出 | 状态 |
|---|---|---|---|
| **P0.1 数据源** | 腾讯源指数/ETF/个股 + CSV 缓存 + 代码歧义保护 | [`core/marketdata_tx.py`](../core/marketdata_tx.py) | ✅ |
| **P0.1+ 企业行为** | 阈值**按代码推定**（主板 11% / 双创与 ETF 21% / 北交所 31%）+ 前复权 | [`scripts/audit_corporate_actions.py`](audit_corporate_actions.py) | ✅ 残余可疑 **0** |
| **P0.2 主题宇宙** | 10 主题（含题材）+ **12 观察项**（无题材）+ 2 个代理项 | [`core/theme_universe.py`](../core/theme_universe.py) | ✅ |
| **P0.3 龙虎榜** | 424 天回填 / 0 缺口 / Tier1+Tier2 | [`core/lhb_store.py`](../core/lhb_store.py) | ✅ |
| **P1 轮动面板** | 上榜率+持续性+买卖比超额，挂进 daily | [`core/rotation_panel.py`](../core/rotation_panel.py) | ✅ 口径 v1 冻结 |
| **P0.4a/b** | 主题指数可用性审计（≥10 年 + 与 ETF 相关性 ≥0.8） | [`scripts/audit_theme_index.py`](audit_theme_index.py) | ✅ **18 可用 / 0 需合成 / 4 受限** |
| **P0.5** | 预注册冻结基准 v1（含 12 条修订记录） | 方案文档 §五 | ✅ |
| **P2.1~P2.4** | L1 / L2 / RS 三层 + 频次 + 收益 + 边际贡献审计 | [`core/theme_timing.py`](../core/theme_timing.py)、[`core/signal_stats.py`](../core/signal_stats.py)、`scripts/audit_theme_{signals,returns}.py` | ✅ **全部未通过** |
| **P2.5~P2.7** | 不建仓位映射 / 仲裁表退化为一句话 / **不执行 OOS** | 方案文档 | ✅ 按 kill 判据 |
| **P3** | 观察哨 + 主题台账 + 「只显示不决策」契约 + 接进 daily | [`core/theme_sentinel.py`](../core/theme_sentinel.py)、[`scripts/theme_timing_report.py`](theme_timing_report.py) | ✅ daily 8 个任务 |
| **P0.4c** | **合成主题指数能力**（模块 + CLI + 2 条已上线合成指数） | [`core/synth_index.py`](../core/synth_index.py)、[`scripts/build_synth_index.py`](build_synth_index.py) | ✅ |

**P0.4c 的两条合成指数**（这是本轮最实的增量）：

| 主题 | 成分股 | 历史 | vs ETF 相关性 | 原方案 |
|---|---|---|---|---|
| **稀土** | 题材 801016 的 80 只 → 人工复核 **24 只** | 2855 根（**11.7 年**） | **0.941~0.943** | 有色代理只有 0.785 ❌ |
| **电网设备** | 题材 801346 的 500 只 → 人工复核 **39 只** | 2855 根（**11.7 年**） | **0.844~0.914** | 中证新能只有 0.686 ❌ |

### 0.3 关键数字（复跑命令见 §7）

```
观察宇宙        10 主题 + 12 观察项 = 22 项 | 不同价格序列 14 条
                共用序列：sh000039×4、sz399973×3（军工/卫星/航天）、sh000998×2、sh000688×2、sh000819×2
合成指数        2 条（synth:稀土 / synth:电网设备），均已入库（含成分股清单）
代理项          2 个（卫星→军工、航天→军工；复用被代理主题的指数）
P0.4b           ✅ 可用 18 / ❌ 需合成 0 / ⚠️ 受限 4（第三代半导体无 ETF、科创系 2 个 6.7y、机器人 6.6y）
P2.2 联合频次    L1 单层 4.8~5.0 次/年；**L1+L2 同日 AND 仅 0.00~0.88 次/年**（< kill 线 2）⇒ kill criterion 3
P2.3 收益审计    **通过的格数 = 0**（修掉"用错标的"的那一格之后）；4 条有效序列上 L1 的 t 仅 −0.42~+1.00
P2.7 OOS        **未动用**（2024-01-01~2026-09-30 仍锁定）
测试             pytest tests/ → 388 passed；pytest services/calc_indicators/ → 35 passed
门禁             verify_marketdata 0 / audit_corporate_actions 0 / 观察哨 --gate 0 / verify_regime 13/13
每日流程         run_all.py daily = 8 个任务（lhb → rotation_panel → **theme_sentinel** → …）
主题台账         data/observations/theme_signal_ledger.csv（目前只有 1 个交易日 / 22 行，**需逐日累积**）
```

### 0.4 待完成（按优先级）

**A. 有明确价值、可直接做**

| # | 事项 | 为什么值得做 | 入口 |
|---|---|---|---|
| A1 | **给半导体系做合成指数** | 「半导体设备/材料/芯片/第三代半导体」现在仍用 `sh000039` **上证信息**（宽泛 IT 代理，0.841）—— **这是你主线唯一还是"代理"的地方**。P0.4c 流程已走通两遍，照做即可拿到语义正确的 0.9+ 指数 | `scripts/build_synth_index.py --plate 801490 --rank --ref-etf sh562590` |
| A2 | **恒生科技** | 唯一"评估后未加入"的方向。需要先给 `marketdata_tx` 加**港股通路**（新代码格式，打破"只收 sh/sz"前提）；且恒生科技指数只有 ~6 年 | 见 `core/theme_universe.py` 注释 |
| A3 | **北交所通路** | `920xxx` 腾讯源取不到（bj/sz/sh 全失败，已显式拒绝）⇒ 合成指数**丢了真实成分股**（西磁科技/九菱科技/奔朗新材等磁材公司） | 需换数据源或加第二条通路 |
| A4 | **台账前向验证** | `theme_signal_ledger.csv` 目前只有 1 天。**前向验证是这套系统唯一的出路**（回测已证明不可判定）；另外 `analyze_signal_ledger.py` 还没有按 `signal_family` 切片的支持 | 每日 `theme_timing_report.py --save`（已挂进 daily） |
| A5 | **新预注册假设（会动用 OOS）** | OOS 是**一次性资源**，现在锁着。可选项：① 跨主题**池化**（把 ~200 个片段合起来凑功效，`进化路径.md`：检出 +2pp 需 149 轮）；② "PANIC_DOWN 后右侧确认再加仓"的两阶段假设。**线索**：`sh000039` 上的 L2 是唯一过了四项中两项的（D=+4.10pp、placebo 0.030、两段同号，但只有 15 片段 / MDE 8.17pp） | 必须先写**冻结基准 v2**，再做 |

**B. 已知弱点（已记录、未解决，用的时候要记得）**

| # | 弱点 | 影响 |
|---|---|---|
| B1 | **RS「风格」列在 6 个主题上退化**（半导体系 4 个 + 云计算/消费电子，与创业板指相关 0.851/0.903） | 未换锚（那是改冻结项 §G1），只加了「大盘」显示列作补偿 |
| B2 | **电网设备的验证窗口只有 428~498 个交易日**（电网设备 ETF 全部 2024-09 后上市） | 相关性比稀土脆弱一倍多；审计已自动打「重叠 <500 日」告警 |
| B3 | **合成指数带残余幸存者偏差**（zzshare 不提供 `time_out`） | 已用 `SYNTH_START=2015` 控制，但**不可宣称无偏** |
| B4 | `audit_regime_lookahead.py` **未跑完**（>8 分钟，我中止了） | 这是**唯一没确认的既有自检**；它不 import `core.marketdata_tx`，与本轮改动无关 |
| B5 | 合成指数的成分股清单是**人工判断** | 换人复核可能得出不同名单；清单已入库（`data/synth/*.txt`）作为来源记录 |

**C. 明确不做**（方案文档第八章 + 本轮新增）

见 §9「不要做的事」。

### 0.5 下一个会话最该做的三件事

1. **先跑体检**（§7 的清单，全部 exit 0 才算环境正常）——尤其确认
   `data/cache/synth_*.csv` 与 `data/synth/*.txt` 在（它们已入库，缺了会直接导致测试失败）。
2. **若想验证你的主线** → 做 **A1（给半导体系做合成指数）**。这是当前性价比最高的一步：
   流程已在稀土/电网设备上走通两遍，产物能直接把 0.841 的宽泛代理换成语义正确的指数。
3. **若想动 OOS** → 先写**冻结基准 v2**（新的预注册），并且**只开一次**。
   不要在没写新预注册的情况下顺手把 2024 年之后的数据算进来。

---

## 1. 环境与硬约束（**新会话必读，否则会重新踩坑**）

| # | 约束 | 说明 |
|---|---|---|
| 1 | **沙箱里 `uv run` 会失败** | `Failed to initialize cache at ~/.cache/uv: Operation not permitted`。**一律用 `.venv/bin/python`**。用户自己的终端里 `uv run` 正常（`run_all.py` 的 cron 就是用 `uv run`） |
| 2 | **禁用东财** | `efinance` / akshare 的 `*_em` 系列实测反复 `ConnectionError`，且对 `000688` 有静默取错标的的隐患。**只用腾讯源 + zzshare + baostock** |
| 3 | **`fund_etf_hist_sina` 在本 venv 已损坏** | `py_mini_racer` `dlsym ... symbol not found` |
| 4 | **services 间 `config.py` 同名冲突** | 把某个服务目录加进 `sys.path` 会遮蔽 `config` 包，报 `No module named 'config.settings'; 'config' is not a package`。**新代码尽量放 `core/`**（本会话已因此把 `rotation_panel` 放 core 而非 alarming_monitor） |
| 5 | **测试要分服务跑** | `pytest tests/` 与 `pytest services/calc_indicators/` 分开；不要一条命令带多个服务目录 |
| 6 | **macOS 没有 `timeout` 命令** | 用 Python 自己的超时或 bash `sleep` |
| 7 | **`main.py --help` 里 `%` 要写 `%%`** | argparse 会对 help 做 `%` 格式化 |
| 8 | **看到 `\|t\| > 5` 先假定自己有 bug** | 优先核对"时间对齐"与"样本独立性"（见 §6 的两次前科） |
| 9 | **北交所（`920xxx`/`4xxxxx`/`8xxxxx`）腾讯源取不到** | 实测 bj/sz/sh 三种前缀全失败 ⇒ `normalize_tx_code` **已显式拒绝**，别再试着猜前缀 |
| 10 | **`synth:` 是合成指数的代码前缀** | `fetch_index_history("synth:稀土")` 读 `data/cache/synth_稀土_history.csv`。**该文件与 `data/synth/*.txt` 已入库**（缺了 `pytest tests/` 会失败） |
| 11 | **`audit_regime_lookahead.py` 很慢**（>8 分钟） | 别放进体检串行跑；它是唯一没确认的既有自检（见 §0.4 B4） |
| 12 | **沙箱里 `ps`/`find` 可能被拒** | 用 `pgrep -fl`、`glob` 工具替代 |

---

## 2. 已完成并验证的（本会话产出）

### P0.1 · 腾讯行情源模块 ✅

| 文件 | 作用 |
|---|---|
| [`core/marketdata_tx.py`](../core/marketdata_tx.py) | 指数/ETF/个股统一取数 + CSV 缓存 + 显式失败 + **代码歧义保护** |
| [`scripts/verify_marketdata.py`](verify_marketdata.py) | 覆盖体检 + **退出码门禁** |
| [`tests/test_marketdata_tx.py`](../tests/test_marketdata_tx.py) | 51 个离线单测 |

- 缓存命名：`data/cache/index_{code}_tx_history.csv`（**不碰** baostock 的 `index_{code}_history.csv`，那是 `audit_regime_*.py` 的基线）
- **不做 6 位数字猜测**：`000001` 作指数是上证综指(sh)、作个股是平安银行(sz) ⇒ 判定不了抛 `MarketDataError`
- 指数接口无 volume 时**留 NaN 不填 0**
- 实测覆盖：宽基 9/9、主题指数 8/8、实盘 ETF 11/11、成分股抽样 6/6

### P0.2 · 主题宇宙定义表 ✅

| 文件 | 作用 |
|---|---|
| [`core/theme_universe.py`](../core/theme_universe.py) | 10 个主题（**主题 = 窄题材并集**）+ 宽度分级 + 审计 |
| [`scripts/list_themes.py`](list_themes.py) | 审计门禁 / 刷新 / ETF 对账 / 导出成分股 |
| [`tests/test_theme_universe.py`](../tests/test_theme_universe.py) | 36 个离线单测 |

- 审计：**10 个主题 / 过宽 4 个 / 锚不可用 0 个 / 无成分股 0 个**
- ⭐ **归一化把排序翻过来**（P0.2 存在的直接证据）：芯片 6466 股日(最多) 但上榜率 **1.34%**；
  **半导体设备 306 股日(最少) 但上榜率 30.54%(最高)**

### P0.3 · 龙虎榜累积层 ✅

| 文件 | 作用 |
|---|---|
| [`core/lhb_store.py`](../core/lhb_store.py) | 两级取数 + parquet 累积 + **断点续跑** + 题材 join |
| [`scripts/lhb_update.py`](lhb_update.py) | 增量/回填/体检/缺口/题材表 |
| [`tests/test_lhb_store.py`](../tests/test_lhb_store.py) | 59 个离线单测 |

**已回填**：**424 天**（2025-01-02 起，zzshare 历史上限）/ 30,437 个股日 / 431,537 题材明细 / 40,869 席位行 / **0 缺口**

**已挂进 [`run_all.py`](run_all.py) daily 场景第 1 位**（不改 crontab）：

```bash
uv run python scripts/lhb_update.py --tier 2 --recent 90 --end <昨天> --throttle 0.8
```

### P1 · 板块轮动资金确认板 ✅

| 文件 | 作用 |
|---|---|
| [`core/rotation_panel.py`](../core/rotation_panel.py) | 零 IO 聚合（口径冻结 v1）+ 状态标签 + ASCII 格式化 |
| [`scripts/rotation_panel.py`](rotation_panel.py) | 渲染 / `--save` / `--export` / `--backfill N` / `--json` |
| [`tests/test_rotation_panel.py`](../tests/test_rotation_panel.py) | 43 个离线单测 |

**定位硬约束**：**只做确认与证伪，不产生买入信号、不占仓位权重。**

---

## 3. 当前数据资产（Phase 2 会用到）

| 路径 | 内容 |
|---|---|
| `data/cache/index_{code}_tx_history.csv` | 腾讯源指数/ETF 历史（宽基 8726 根 → 主题指数 2009 起） |
| `data/lhb/lhb_stocks.parquet` | 龙虎榜个股日表（30,437 行，含 Tier2 毛额，60 天覆盖） |
| `data/lhb/lhb_concepts.parquet` | 个股↔题材明细（431,537 行） |
| `data/lhb/theme_table.parquet` | 题材表（520 个，plate_type=17） |
| `data/lhb/plate_constituents.parquet` | 14 个题材的成分股（**"上榜率"的分母**） |
| `data/lhb/theme_metrics.parquet` | 面板指标导出（2000 行 = 200 天 × 10 主题） |
| `data/cache/regime_timeline/*.json` | 逐日 regime 时间线（`audit_regime_*.py` 用，构建慢已缓存） |

---

> ⚠️ **以下为历史规格（已于 2026-10-05 全部执行完毕）——留作背景，不是待办。**
> 当前状态与待办请看 **§0**。

## 4. Phase 2 要做什么（**范围与坑都已在方案文档第五章定好**）

### 定位

把择时从"**只等 PANIC_DOWN**"升级为"**左侧（PANIC_DOWN）+ 右侧（结构/均线转向）**"的两侧择时阶梯。
**动作永远是调"仓位中枢"档位，不是一次性满仓，也不配紧止损。**

> 依据：紧止损把恐慌择时的收益/风险比**砍掉 60%**（−2% 止损 0.157 vs 不止损 0.390）。

### 实施步骤（P2.1~P2.7，详见方案文档）

| 步 | 内容 |
|---|---|
| P2.1 | 拆成三个**可独立调用**的布尔函数：**L1 结构破坏** / **L2 均线转向** / **RS 相对强度确认**。**❌ 不实现"量能放大"条件** |
| P2.2 | **先做频次审计**（在谈收益之前）：2010-2026 每主题每层每组合的信号次数/年 |
| P2.3 | 收益审计：**复用 [`audit_regime_hold.py`](audit_regime_hold.py) 的方法论**（次日开盘入场、持 H 根、全历史预热的分段时间线、片段匹配重采样 placebo） |
| P2.4 | 边际贡献审计：L2 相对 L1？RS 相对 L1+L2？（**无增量者删掉**） |
| P2.5 | 仓位映射：防守档 / 中性档 / 进攻档 |
| P2.6 | **与 PANIC_DOWN 的仲裁表**（必须消除"同一波重复加仓"） |
| P2.7 | **OOS 锁定验证**（只看一次） |

### 已识别的 6 个坑（方案文档第五章「坑与明确处理方式」有完整版）

1. **L1 与 L2 不独立** ⇒ "三层同时满足"的联合频次可能只有 **0~2 次/年**。
   **"一年 3~5 次"是从 PANIC_DOWN 抄来的（创业板指 4.4 次/年），不能自动迁移。必须先测频次。**
2. **第三层"量能确认"已砍掉** —— 因为本仓库实测**放量阈值 1.5→3.0 单调恶化**（−0.86 → −7.35pp，全部显著为负）。虽然那是个股样本、ETF 未测，但**先验为负**。
3. **RS 有方向歧义**：主题与宽基同跌、主题跌得少 ⇒ RS 上升但价格仍下行 ⇒ 假阳性。
   **RS 必须与绝对价格条件绑定**（如 `close > MA20`）。
4. **统计功效**：检测 +2pp 需 **149 轮**（[`进化路径.md`](进化路径.md) 判据表）。ETF 自身历史太短（电网设备 431 根、科创半导体 363 根）⇒ **必须用长历史主题指数**（见 §5 P0.4）。
5. **数据源**：禁用东财；只用腾讯 + zzshare + baostock。
6. **"结构破坏"定义模糊** ⇒ **必须在 P0.5 冻结**。

### 验收判据

`|t| ≥ 1.96` **且** `|效应| ≥ MDE` **且** 两段同号（2016-2020 / 2021-2026）**且** placebo `p < 0.05`；
**并且** 联合频次 **≥ 3 次/年**。

### kill criteria（预注册，别到事后才想）

1. RS 无增量 → 删 RS，退化为 L1+L2
2. L2 无增量 → 只留 L1
3. 联合频次 < 2 次/年 → 不进入仓位逻辑，只做观察提示
4. 两段不同号 → 不采用（照 PANIC_DOWN 的先例标注体制依赖）
5. 合成主题指数与 ETF 相关性 < 0.8 → 该主题不参与结论

> **诚实预期**：最可能的结局是 **"L1 + L2 两层 + 仓位中枢调档"**，RS 可能被砍。
> **砍掉无增量的一层是这套方法论正常工作，不是失败。**

---

> ⚠️ **以下为历史规格（已于 2026-10-05 全部执行完毕）——留作背景，不是待办。**
> 当前状态与待办请看 **§0**。

## 5. Phase 2 的两个前置（**开工前完成**）

### 前置 A · P0.4 主题指数（**范围已缩减**）

**原计划"全部合成"范围过大。** 实测交易所已有现成主题指数可用：

```
国证半导体芯片 sz399363  4170 根(2009起)   中证TMT      sh000998  3479 根
有色金属      sh000819  3501 根           中证新能     sz399808  2681 根
中证医疗      sz399989  2681 根           中证白酒     sz399997  2663 根
中证环保      sh000827  3403 根           中证新能车   sz399976  2681 根
上证综指      sh000001  8726 根           科创50      sh000688  1636 根
```

⇒ **两步走**：
1. 先给每个主题**匹配现成指数**（`core/theme_universe.THEMES` 里已有 `index` 字段占位）；
2. **只对匹配不到的主题做合成**（用 `plates_stocks` 成分股 + `core.marketdata_tx` 的腾讯个股日线，按 `time_in` 还原成分）。
   **很可能是你的主线 `半导体设备`** —— 它正是既有文档里"baostock 没有精确行业指数"的那个缺口。

**验收**：每个主题 ≥10 年日线；**与对应 ETF 的日收益相关性 ≥0.8**（否则不采用该合成指数，降级为"只显示、不验证"）。

### 前置 B · P0.5 预注册（**草案已拟好，见下表；确认后即为冻结基准**）

> ⚠️ **一旦开始跑回测就不能再改。** 如确需修改，必须新开版本号并重算全部历史。

| 项 | 冻结草案（**待确认**） |
|---|---|
| **L1 结构破坏** | 分型摆动点确认窗口 **k=5**；"不再创新低"= 收盘价不破**前 20 日**最低收盘；"突破前一反弹高点"= **收盘价** > 最近一个已确认的摆动高点；**需等待 1 根 K 线确认** |
| **L2 均线转向** | **MA10 上穿 MA30**；MA30 斜率 = 近 **5** 根回归斜率 ≥ **0**（由负转平） |
| **RS 相对强度** | 比价 = 主题指数 / 宽基锚；窗口 **20 日**；"走强"= 比价 20 日变化 > 0 **且** 主题指数 `close > MA20`（**绑定绝对价格**，防坑 3） |
| **持有期** | H ∈ {**5, 10, 20, 40**} |
| **验收判据** | `\|t\| ≥ 1.96` 且 `\|效应\| ≥ MDE` 且两段同号 且 placebo `p < 0.05`；且联合频次 ≥ 3 次/年 |
| **OOS 锁定段** | **2024-01-01 ~ 2026-09-30**，只看一次 |
| **每主题的锚** | 见 `core/theme_universe.THEMES[*]['anchor']`（半导体系 `sz399006`、电网/黄金 `sh000001`、科创系 `sh000688`） |

**若上述草案被修改，必须在方案文档里记一条修订记录（含日期与理由）。**

---

## 6. 本会话踩过的坑（可复用的教训）

| # | 坑 | 教训 |
|---|---|---|
| 1 | **`min_periods=1` 的滚动和会骗人** | `roll5` 用 `min_periods=1` 时，序列开头会把**递减的日频数据累加成"递增的滚动和"** ⇒ 面板误报 🔥。改成 `min_periods=w` |
| 2 | **排序口径会悄悄用错** | 面板最初按原始 `n_listed` 排序 ⇒ 宽题材永远靠前，**正是本板要避免的误导**。必须按归一化上榜率 |
| 3 | **NaN 不等于 None** | pandas 里 `None` 存进 DataFrame 会变 `NaN`，`x is None` 失效 ⇒ 显示 `nan`/`+nan`。一律先过 `_f()` 归一化 |
| 4 | **`plates_rank(17)` 硬上限 254 条** | 与 `limit` 无关；只覆盖当日有活跃度的题材 ⇒ **窄题材的"热度"列系统性不可用**（恒 `n/a`） |
| 5 | **`plates_list(18)` 不是 17 的超集** | 是**另一套分类**（漏掉 801001 芯片等高频题材，行覆盖仅 33.7%）。**题材宇宙必须用 `plate_type=17`** |
| 6 | **龙虎榜字段语义反直觉** | `buy_in` 是**净额**（`= buy_total − sell_total`）；`buy_group_icons` **只是"有标记的席位"**（万科A icon 买入 3.9 亿 vs 真实毛额 6.9 亿）⇒ **买卖比必须走 `lhb_detail`** |
| 7 | **买卖比的基线就是 0.50** | 上榜门槛要求买卖双方都够大 ⇒ 结构性对称。**绝对值无信息，必须看"主题 − 当日全市场"的超额**（全市场日均值时序 std 仅 0.0226） |
| 8 | **厂商日批约 18:00 落库，cron 是 16:30** | 会**把真实交易日记成 0 并永久跳过**（静默数据洞）⇒ 加了"尚未发布"护栏 + `--end 昨天` |
| 9 | **`--tier 2` 的 todo 判据写错会静默什么都不做** | 原来依据 `state['list']` ⇒ 对已回填 Tier1 的日期整体跳过。必须依据 `state['detail']` |
| 10 | **空结果也要记录**（但要分情况） | 节假日/无上榜要记（避免反复重试）；**当天未发布不能记**（否则永久封洞） |
| 11 | **服务目录加进 `sys.path` 会遮蔽 `config` 包** | 新代码尽量放 `core/` |
| 12 | **"我以为是"要先被数据检验** | 本会话 3 次靠实测推翻了原本会写进代码的假设（归一化排序、买卖比基线、题材宽度） |

---

## 7. 关键命令速查（2026-10-05 更新）

```bash
cd /Users/a801/Linda/Work/project/gupiao-assistant/trading_lab
# 沙箱里用 .venv/bin/python；你自己的终端可用 uv run python

# ================= 体检（全部 exit 0 才算环境正常）=================
.venv/bin/python scripts/verify_marketdata.py                    # 行情覆盖
.venv/bin/python scripts/audit_corporate_actions.py               # 企业行为门禁（残余可疑应为 0）
.venv/bin/python scripts/theme_timing_report.py --gate            # 观察哨（22 项，数据不可用则 exit 1）
.venv/bin/python scripts/list_themes.py                           # 主题宇宙审计
.venv/bin/python scripts/lhb_update.py --status                   # 龙虎榜累积 + 静默空洞
.venv/bin/python scripts/rotation_panel.py                        # 面板可渲染
.venv/bin/python services/calc_indicators/verify_regime.py        # 应 13/13
.venv/bin/python services/calc_indicators/verify_etf_mode.py --offline   # 应 28/28
.venv/bin/python services/calc_indicators/verify_entry_gate.py    # 应 24/27（3 项已知失败）

# ================= 测试 =================
.venv/bin/python -m pytest tests/ -q                              # 应 388 passed
.venv/bin/python -m pytest services/calc_indicators/ -q            # 应 35 passed

# ================= 每日 =================
.venv/bin/python scripts/theme_timing_report.py --date <交易日> --save   # 观察哨 + 主题台账
.venv/bin/python scripts/run_all.py --dry-run                     # daily 应为 8 个任务

# ================= 研究工具（不是健康门禁）=================
.venv/bin/python scripts/audit_theme_index.py                     # P0.4b 主题指数可用性（18/0/4）
.venv/bin/python scripts/audit_theme_index.py --gate              # 注意：--gate 当前 exit 1，那是研究结论
.venv/bin/python scripts/audit_theme_signals.py                   # P2.2 频次审计
.venv/bin/python scripts/audit_theme_returns.py --holds 5,10,20,40 # P2.3+P2.4 收益审计（1~3 分钟）
.venv/bin/python scripts/explore_theme_index_candidates.py        # 加主题/换指数前勘探候选

# ================= 合成指数（P0.4c）=================
# ① 出成分股复核清单（诊断用，**不许拿来筛成分股**）
.venv/bin/python scripts/build_synth_index.py --plate 801490 --name 半导体设备 \
    --rank --ref-etf sh562590
# ② 用人工复核过的清单合成 + 验证 + 落盘（落盘后代码即 synth:<name>）
.venv/bin/python scripts/build_synth_index.py --plate 801490 --name 半导体设备 \
    --include-file data/synth/半导体设备_members.txt --ignore-time-in \
    --validate sh562590,sh561980,sh512480 --save

# ================= 龙虎榜 / 面板 =================
.venv/bin/python scripts/lhb_update.py                          # 每日增量（Tier1）
.venv/bin/python scripts/lhb_update.py --tier 2 --recent 90      # 补最近 90 天毛额
.venv/bin/python scripts/rotation_panel.py --save                # 最新交易日 + 存快照
.venv/bin/python scripts/rotation_panel.py --date 2026-09-30     # 历史日期（可复现）

# ================= 旧审计模板 =================
.venv/bin/python scripts/audit_regime_hold.py                    # 持有期 + 多指数 + 分段
.venv/bin/python scripts/audit_regime_oos.py                     # 样本外分段
.venv/bin/python scripts/audit_regime_lookahead.py               # 无未来函数（**很慢，>8 分钟**）
```

> ⚠️ `audit_theme_index.py --gate` 与 `audit_theme_signals.py --gate` 当前返回 **1**，
> 那是把本轮研究结论编码成了门禁（0 需合成 / 联合频次不足），**不是环境坏了**。

## 8. 文件地图

```
core/
  marketdata_tx.py           腾讯源数据层（指数/ETF/个股 + 缓存 + 歧义保护
                             + **企业行为检测/前复权** + **按代码推定阈值** + synth: 转发）
  lhb_store.py               龙虎榜累积层（Tier1/Tier2 + 题材 + 题材热度 + 成分股）
  theme_universe.py          主题宇宙（**THEMES 10 主题** + **WATCH_ONLY 12 观察项** + all_observed()）
  rotation_panel.py          轮动面板零 IO 聚合（口径冻结 v1）
  theme_timing.py            Phase 2 三层：L1 结构破坏 / L2 均线转向 / RS 相对强度
                             （含 l1_state/l1_pending、摆动点、片段计数）
  signal_stats.py            P2.3 统计内核（前向收益 + **块长=H 块 bootstrap** + 片段匹配 placebo + MDE）
  theme_sentinel.py          Phase 3 观察哨（分区渲染 + RS 退化/历史不足/待确认 + 两列相对强度）
  synth_index.py             P0.4c 合成指数（等权逐日再平衡 + OHLC 合成 + synth: 加载）
scripts/
  # —— 门禁/体检 ——
  verify_marketdata.py       行情覆盖门禁
  audit_corporate_actions.py 企业行为门禁（exit 0 = 干净）
  theme_timing_report.py     观察哨 CLI（--date/--save/--json/--gate/--themes-only）
  list_themes.py             主题宇宙审计门禁
  lhb_update.py              龙虎榜 CLI
  rotation_panel.py          面板渲染 CLI
  run_all.py                 (M) daily 现为 **8 个任务**（加 lhb、rotation_panel、theme_sentinel）
  # —— 研究工具（失败即研究结论，不是环境故障）——
  audit_theme_index.py       P0.4b 指数可用性（≥10 年 + 相关性 ≥0.8）
  audit_theme_signals.py     P2.2 频次审计
  audit_theme_returns.py     P2.3 收益 + P2.4 边际贡献
  explore_theme_index_candidates.py  加主题/换指数前的候选勘探
  build_synth_index.py       P0.4c 合成指数 CLI（--rank / --include-file / --validate / --save）
tests/
  test_marketdata_tx.py      78（含企业行为/按代码阈值/北交所拒绝）
  test_lhb_store.py          59
  test_theme_universe.py     36+
  test_rotation_panel.py     43
  test_theme_timing.py       27（含无未来函数截断不变性、方向守卫、冻结参数守卫）
  test_signal_stats.py       14（含"块 bootstrap 必须比朴素 t 更保守"）
  test_theme_sentinel.py     28+（含数据不可用、两列相对强度、代理项一致性）
  test_synth_index.py        16（等权链式、时点还原、缺数据剔除、OHLC 合成）
data/
  cache/synth_稀土_history.csv、cache/synth_电网设备_history.csv   ← **已入库**
  synth/稀土_members.txt、synth/电网设备_members.txt                ← **已入库**（来源记录）
  observations/theme_signal_ledger.csv                             ← 主题级台账（逐日累积）
文档：
  增强方案v2-板块轮动面板与ETF择时.md   规格 + P0.5 冻结基准 v1 + 各阶段完成记录（最全）
  命令速查-Phase2与Phase3.md            新人上手 / 每条命令怎么读
  交接总结-Phase2.md                    **本文件**（§0 = 权威状态总览）
  交接总结.md                           旧文（§二统计结论仍有效）
  P0.5-预注册复核表.md / P0.4c-稀土成分股复核表.md / P0.4c-电网设备成分股复核表.md  复核过程记录
```

**git**：本轮（2026-10-04/05）产出 12 个提交，最新 `52e2cae`。
**已提交未 push**；remote = `https://github.com/lindalian-ldh/trading_lab.git`

---

## 9. 不要做的事（**继承自方案文档第八章 + 本会话新增**）

| # | 不要做 |
|---|---|
| 1 | **不要用东财**（任何接口） |
| 2 | **不要用 `fund_etf_hist_sina`**（本 venv 已损坏） |
| 3 | **不要依赖 `plates_rank_days` / `plates_rank_days_new`**（实测返回 None） |
| 4 | **不要用 `plates_trend` 取题材（801xxx）历史**（返回 0 条）；题材历史必须自己合成或用现成指数 |
| 5 | **不要把"上榜家数"直接横向比较**（成分股数从 6 到 1531） |
| 6 | **不要把面板升级成买入信号**（它没有经过验证） |
| 7 | **不要做"机构 vs 游资占比"**（游资识别依赖人工名单，不可靠） |
| 8 | **不要做"突破量能放大"确认**（放量阈值单调恶化） |
| 9 | **不要做需求 #3（个股择时）**（重新引入选股，且功率分析需 5~15 年才能验证） |
| 10 | **不要用紧止损配择时**（收益/风险比砍掉 60%） |
| 11 | **不要在验证前改冻结参数** |
| 12 | **不要忘记"同一波重复加仓"**（P2.6 仲裁表必须显式解决） |
| 13 | **不要因为砍掉 RS 或 L2 而觉得失败**（那是方法论正常工作） |
| 14 | **不要在 Phase 2 顺手改需求 #1 的面板口径**（v1 已冻结；要改就新开 v2 并重算历史） |

---

## 10. 一句话交接

> **需求 #1 已上线**：一块"仪表盘"，用龙虎榜归一化上榜率 + 持续性 + 买卖比超额，
> 让你每天 10 秒形成板块轮动印象。**它只显示，不决策。**
>
> **需求 #2（Phase 2）做完了，结论是"不采用"**：把择时从"只等 PANIC_DOWN"扩成
> "左侧 + 右侧"的仓位调档阶梯 —— 这条路**没走通**。三层（L1 结构破坏 / L2 均线转向 /
> RS 相对强度）在 4 条有效价格序列上**全部未通过**预注册判据；联合层更被
> **kill criterion 3** 判出局（0.38~0.88 次/年 < 2）。唯一那一格"通过"是
> **用错标的**（中证新能冒充电网设备）的产物。
>
> **但这不是失败，是预注册方法在正常工作**：它在**没烧掉 OOS 段**的情况下就否掉了假设。
> 更准确的措辞是 —— **"不可判定"多于"已证伪"**（MDE 3~4.5pp，而目标效应量级是 +2pp；
> `进化路径.md`：检出 +2pp 需 **149 轮**，我们每条序列只有 19~48 个片段）。
>
> **本轮真正的增量有两块**：① **数据层**（发现腾讯 ETF 日线未复权，修好企业行为检测，
> 把 0.990 的相关性从 0.666 里救回来）；② **P0.4c 合成主题指数能力**（稀土 0.941~0.943、
> 电网设备 0.844~0.914，历史 11.7 年）—— 它把"没有可用指数"这个结构性障碍解决了。
>
> **下一步最值钱的一步**是 §0.4 的 **A1：给半导体系做合成指数**（那是你主线唯一还是"代理"的地方）。

---

## 11. 2026-10-04 会话产出（Phase 2 主体）

### 11.1 两个前置

| 步 | 产出 | 关键数字 |
|---|---|---|
| **P0.4a** | [`core/marketdata_tx.py`](../core/marketdata_tx.py) 加**企业行为检测与前复权**（`detect_corporate_actions` / `adjust_corporate_actions`，equity 默认 `adjust=True`；**缓存仍存原始未复权价**）+ [`scripts/audit_corporate_actions.py`](audit_corporate_actions.py) 门禁 + 12 个新单测 | 腾讯 ETF 日线**未复权**：512480 在 2021-03-29 单日 −48.90%（份额折算）。不修则把 **0.990 看成 0.666**（512480 vs 512760） |
| **P0.4b** | [`scripts/audit_theme_index.py`](audit_theme_index.py) | **只有 4/10 主题的现成指数达标**；**3 个需合成**（半导体设备 0.715 / 半导体设备材料 0.773 / 电网设备 0.686）；科创系 6.7y（你已决定接受） |
| **P0.5** | [`scripts/P0.5-预注册复核表.md`](P0.5-预注册复核表.md) → 方案文档**「P0.5 冻结基准 v1」** | 原草案有 **10 处**缺陷（含两处会致命：**假 OOS**、**H 未指定主判据**），已全部修正并记修订记录 |

### 11.2 Phase 2 主体

| 文件 | 作用 |
|---|---|
| [`core/theme_timing.py`](../core/theme_timing.py) | L1 / L2 / RS 三层（严格按冻结基准）+ 摆动点 + 同日 AND 组合 + 片段计数 |
| [`core/signal_stats.py`](../core/signal_stats.py) | 前向收益 + **块长=H 的移动块 bootstrap** + 片段匹配 placebo + MDE |
| [`scripts/audit_theme_signals.py`](audit_theme_signals.py) | P2.2 频次审计 |
| [`scripts/audit_theme_returns.py`](audit_theme_returns.py) | P2.3 收益审计（+ P2.4 边际贡献） |
| `logs/phase2_p22_freq.json`、`logs/phase2_p23_returns.log/.json` | 可复跑的数字存档 |
| `tests/test_theme_timing.py`(27) + `tests/test_signal_stats.py`(14) | 离线单测 |

### 11.3 三条最重要的实测结论

1. **联合频次坍缩（坑 1 证实且更狠）**：L1 单层 4.8~5.0 次/年，但 **L1+L2 同日 AND 只有 0.38~0.88 次/年**
   ⇒ 每个主题都低于 kill 线 2 次/年 ⇒ **联合层不得进入仓位逻辑**。
2. **10 个主题只有 5 条不同价格序列**（`sz399363` 被 4 个主题共用）⇒ 多重检验按 5 算，不是 10。
3. **收益审计：4 条有效序列上 L1 全部未通过**（D 从 −1.19pp 到 +1.09pp，t 都 < 1.0，
   MDE 3.1~4.5pp）；L2 全部未通过且 4/5 两段不同号；唯一"通过"的两格来自
   `sz399808`（中证新能），而它与电网设备 ETF 相关性只有 **0.686 < 0.8**
   ⇒ 按 **kill criterion 5 该主题不参与结论**。

> **注意措辞**：这是 **"不可判定" 多于 "已证伪"** —— MDE 3~4.5pp 而目标效应量级是 +2pp
> （[`进化路径.md`](进化路径.md)：检出 +2pp 需 **149 轮**，我们只有 **19~40 个片段/序列**）。
> 样本功效天生不足，这本身就是要写进结论的事实。

### 11.4 新增的"不要做"

| # | 不要做 |
|---|---|
| 15 | **不要用未复权的腾讯 ETF 价算收益/相关性**（必须走 `fetch_equity_history` 的默认 `adjust=True`） |
| 16 | **不要用日级朴素 t 判定重叠 H 日窗口的显著性**（必须用块长=H 的块 bootstrap） |
| 17 | **不要把 10 个主题当成 10 个独立复现**（只有 5 条价格序列） |
| 18 | **不要在样本内不通过时打开 OOS**（OOS 是**一次性**资源，留着给功效更好的假设） |
| 19 | **不要为了救频次把"同日 AND"放宽成"N 日内共现"**（那是改冻结项） |
| 20 | **不要用未复权的腾讯 ETF/个股价算收益或相关性**（`fetch_equity_history` 默认 `adjust=True`，别绕过） |
| 21 | **不要用日级朴素 t 判定重叠 H 日窗口的显著性**（必须用 `core/signal_stats` 的块长=H 块 bootstrap） |
| 22 | **不要把 22 个观察项当成 22 次独立观测**（只有 **14 条**不同价格序列） |
| 23 | **不要按"与 ETF 的相关性"筛合成指数的成分股**（会让 ≥0.8 的验证变成循环论证） |
| 24 | **不要在样本内不通过时打开 OOS**（一次性资源，留给功效更好的假设） |
| 25 | **不要把 `RS 大盘` 那列塞进冻结组合或判据**（它是事后加的显示列，进了就是"看到数据后加假设"） |
| 26 | **不要给北交所代码猜 sh/sz 前缀**（腾讯源取不到，已显式拒绝；猜了会静默取错标的） |
| 27 | **不要删 `data/cache/synth_*.csv` 或 `data/synth/*.txt` 而不重新生成**（它们已入库；缺了测试会失败） |

---

## 12. 2026-10-05 会话产出（P0.4c + 观察项扩展 + 观察哨打磨）

### 12.1 P0.4c：合成主题指数能力（**本轮最大增量**）

| 步 | 产出 |
|---|---|
| ① | 企业行为阈值由固定 22% 改为**按代码推定**（`price_limit`：主板 10% / 科创创业 20% / 基金 20% / 北交所 30%，+1pp；前 10 根豁免）⇒ 门禁「残余可疑」**3 → 0**，`sz002371` 从 21.95% 修到 10.02% |
| ② | [`core/synth_index.py`](../core/synth_index.py)（等权逐日再平衡 + **OHLC 一起合成** + `synth:` 加载）+ [`scripts/build_synth_index.py`](build_synth_index.py) + 16 单测 |
| ③ | 稀土：题材 801016 的 80 只 → 人工复核 **24 只**；电网设备：题材 801346 的 500 只 → **39 只** |
| ④ | 合成指数 **2855 根（2015 起，11.7 年）**；稀土 0.941~0.943、电网设备 0.844~0.914 ⇒ **P0.4b「需合成」归零** |

**两个口径陷阱（已记录）**：
1. **`time_in` 常是批量分类日期**（稀土 24 只里 13 只完全相同 2018-09-20）⇒ 严格按它过滤只剩 8 年历史；
   `--ignore-time-in` 拿到 11.7 年且相关性几乎不变 ⇒ **采用忽略口径**。
2. **合成指数必须真合成 OHLC** —— L1 的摆动点用 `high`/`low` 判极值，只给 close 会让摆动点退化成"收盘价极值"。

### 12.2 观察项从 0 扩到 12 个

`WATCH_ONLY`：红利、电力、有色金属、煤炭、银行、军工、创新药、机器人、证券、稀土、卫星、航天。
两套机制：**代理项**（`proxy_of`，卫星/航天复用军工的指数 —— 不各挑一个，否则把"重复"伪装成"独立覆盖"）
与 **合成指数项**（`synth:`）。**已评估但未加入**：恒生科技（港股，不在 sh/sz 通道）。

### 12.3 观察哨打磨（都由用户提问引出）

| 改动 | 原因 |
|---|---|
| RS 列退化时显示 `⚠️✅`/`⚠️·` | 原来只印一个 `⚠️`，把"亮没亮"盖住了 |
| 「结构已破」只认最近 20 个交易日 | 原判据"历史上曾转多"几乎是常量，所有行都印同一句 |
| 新增 `⏳ 待下一根确认` 状态 | 条件今天已满足但按 §① 要等确认（如 2026-09-30 的创新药） |
| **新增「大盘」列**（固定沪深300） | 风格锚在 6 个主题上退化（0.851/0.903）⇒ 加一列互补视角，**不动冻结参数** |
| 历史 <10 年 / 重叠 <500 日 告警 | 机器人/科创系、电网设备 |
| 列宽放宽、表头加图例 | `synth:电网设备` 是 11 字符，原来被挤爆 |

### 12.4 修掉的坑（都已加测试）

1. `ev.iloc[N:]` 砍的是"事件列表前 N 条"而不是"前 N 根内的事件" ⇒ 几乎全部事件被丢弃（门禁一度报 12 个 ❌）
2. 上市豁免把短序列整段豁免掉 ⇒ 加 `≥250 根` 护栏
3. `build_index_from_ohlc` 的 `level` 忘了按 `min_members` 掩码 ⇒ 成分股不足的日子给出 1000 点
4. `build_synth_index` 里 `for raw, tx in kept` 解包顺序反了 ⇒ `--rank` 永远输出空表
5. 北交所 `920xxx` 会被猜成 `sh920xxx`（取错标的）⇒ 显式拒绝
6. 状态标签块跑在"大盘"计算之前 ⇒ 双强却显示"风格强，大盘弱"
7. **`data/cache/synth_*.csv` 与 `data/synth/*.txt` 原本被 gitignore** ⇒ 新克隆会**测试失败**；已放行入库

**git**：`b947895`…`52e2cae` 共 12 个提交（**已提交未 push**）。
