# 交接总结 · Phase 2（ETF 择时增强）

> **用途**：新开会话做 Phase 2 时，把这一份贴进去即可恢复上下文。
> **最后更新**：2026-10-04
> **配套文档**（Phase 2 的完整规格，**必读**）：
> [`增强方案v2-板块轮动面板与ETF择时.md`](增强方案v2-板块轮动面板与ETF择时.md) 的
> **第五章 Phase 2** + **P0.5 冻结清单** + **P0.5-A 面板口径** + **第八章 不要做的事**
> 旧文档 [`交接总结.md`](交接总结.md) 仍然有效（已验证的统计结论都在里面），
> 但它"下一步"那两件事已被本方案取代，**以本文件为准**。

---

## 0. 一句话状态

**需求 #1（板块轮动面板）已上线并挂进每日流程；Phase 0 的 P0.1~P0.3 与 Phase 1 全部完成并验证。**
**接下来做 Phase 2（需求 #2：ETF 择时增强），开工前有两件前置：P0.5 预注册（草案已在下文 §5）与 P0.4 主题指数（范围已缩减）。**

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

## 7. 关键命令速查

```bash
cd /Users/a801/Linda/Work/project/gupiao-assistant/trading_lab

# —— 体检（5 个门禁，全部 exit 0 才算健康）——
.venv/bin/python scripts/verify_marketdata.py            # 行情覆盖
.venv/bin/python scripts/list_themes.py                  # 主题宇宙审计
.venv/bin/python scripts/lhb_update.py --status          # 龙虎榜累积 + 静默空洞
.venv/bin/python scripts/rotation_panel.py               # 面板可渲染
.venv/bin/python services/calc_indicators/verify_regime.py   # 13/13

# —— 测试 ——
.venv/bin/python -m pytest tests/ -q                     # 274 passed
.venv/bin/python -m pytest services/calc_indicators/ -q   # 35 passed

# —— 龙虎榜 ——
.venv/bin/python scripts/lhb_update.py                   # 每日增量（Tier1）
.venv/bin/python scripts/lhb_update.py --tier 2 --recent 90   # 补最近 90 天毛额
.venv/bin/python scripts/lhb_update.py --backfill        # 全量回填（可断点续跑）

# —— 面板 ——
.venv/bin/python scripts/rotation_panel.py --save        # 最新交易日 + 存快照
.venv/bin/python scripts/rotation_panel.py --date 2026-09-30   # 历史日期（可复现）
.venv/bin/python scripts/rotation_panel.py --backfill 20       # 回看最近 20 天
.venv/bin/python scripts/rotation_panel.py --json             # 机器可读

# —— 全流程（dry-run 先看）——
.venv/bin/python scripts/run_all.py --dry-run            # daily = 7 个任务

# —— Phase 2 会复用的审计模板 ——
.venv/bin/python scripts/audit_regime_hold.py            # 持有期 + 多指数 + 分段
.venv/bin/python scripts/audit_regime_robustness.py --index sh000001   # 阈值敏感 + placebo
.venv/bin/python scripts/audit_regime_oos.py             # 样本外分段
.venv/bin/python scripts/audit_regime_lookahead.py       # 无未来函数（应通过）
```

---

## 8. 文件地图（本会话新增）

```
core/
  marketdata_tx.py           腾讯源数据层（指数/ETF/个股 + 缓存 + 歧义保护）
  lhb_store.py               龙虎榜累积层（Tier1/Tier2 + 题材 + 题材热度 + 成分股）
  theme_universe.py          主题宇宙定义（10 主题 = 窄题材并集）
  rotation_panel.py          轮动面板零 IO 聚合（口径冻结 v1）
scripts/
  verify_marketdata.py       行情覆盖门禁
  lhb_update.py              龙虎榜 CLI
  list_themes.py             主题宇宙审计门禁
  rotation_panel.py          面板渲染 CLI
  run_all.py                 (M) daily/full 加 lhb → rotation_panel
  增强方案v2-....md           方案 + 冻结口径 + 完成记录（**Phase 2 的规格**）
tests/
  test_marketdata_tx.py      51
  test_lhb_store.py          59
  test_theme_universe.py     36
  test_rotation_panel.py     43
```

**git**：本会话产出两个提交
`8b29a7f` 腾讯源 + 移除东财 ｜ `db02555` 龙虎榜/主题/面板
（**已提交未 push**；remote = `https://github.com/lindalian-ldh/trading_lab.git`）

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
> **需求 #2（Phase 2）是真正的增强**：把择时从"只等 PANIC_DOWN"扩成"左侧 + 右侧"的仓位调档阶梯。
> 已知数据底座够用（腾讯源给了 10~20 年主题指数历史），
> **最大的风险不是技术，而是多重检验**（三层 × 阈值 × 持有期 × 主题）
> ⇒ **先把 P0.5 冻结，再跑任何回测。**
>
> **最可能的结局是 L1+L2 两层 + 仓位中枢调档，RS 被砍。那不是失败。**
