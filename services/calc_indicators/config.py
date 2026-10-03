"""开仓筛查工具 - 配置中心。

所有参数统一在此定义，main.py 中不再出现硬编码数值。
修改此文件即可调整全部判定逻辑。

通过 get_config("profile_name") 选择不同的配置档。
"""

from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass
class TradingConfig:
    # ── 数据获取 ──
    # 240 根（约一年）：既是回踩/均线判定的需要，也是 `--index auto`
    # 做相关性选择的数据窗口 —— 固定 240 可保证每次运行取到同样的样本数，
    # 避免"今天选创业板指、明天选沪深300"这种因样本长度漂移导致的抖动。
    DATA_TRADING_DAYS: int = 240

    # ── 维度 A：结构 ──
    MA_PERIODS: Tuple[int, ...] = (5, 10, 20, 60)
    BREAKOUT_WINDOW: int = 20
    PULLBACK_THRESHOLD: float = 0.01  # 1% 偏离（长线回踩，兼容保留）

    # ── 维度 A 增强：短线回踩信号 ──
    USE_PULLBACK_ENHANCE: bool = True     # True=启用短线回踩增强逻辑，False=回退原 MA20 偏离判定
    PULLBACK_DEBUG: bool = True           # True=打印详细回踩排查日志到控制台
    PULLBACK_MA_TARGETS: List[int] = field(default_factory=lambda: [5, 10])
    PULLBACK_MAX_DEVIATION: float = 0.005        # 0.5%，回踩偏离度容忍阈值
    PULLBACK_REQUIRE_MA_UP: bool = True          # 必须均线方向向上
    PULLBACK_REQUIRE_SHRINK_VOLUME: bool = True   # 必须缩量
    PULLBACK_VOLUME_SHRINK_RATIO: float = 0.8    # 缩量阈值：当前量 < 5日均量 × 此系数
    PULLBACK_REQUIRE_BULLISH_CANDLE: bool = True # 要求阳线或锤子线
    PULLBACK_CONFIRM_BARS: int = 2               # 回踩确认窗口（最近 N 根 K 线）

    # ── 维度 A：回踩方向约束（修"对称偏离"语义错误）──
    # 问题：偏离度用 abs(收盘 − MA) / MA，把"均线**上方**0.5%"与"均线**下方**0.5%"
    #       当成同一件事。但回踩买入只应认**从上方回落触及均线**：
    #         · 价格在均线上方 1.5% 是"偏离/追高"，不是回踩买点（却会通过绝对偏离检查）
    #         · 价格在均线下方较远是"破位"，也不是回踩买点
    # 修法：① 要求最近 N 根内出现过"收盘在均线上方"（证明存在回落动作）；
    #       ② 当前收盘若在均线下方，跌幅不得超过允许比例，且均线必须向上（否则是破位）。
    PULLBACK_REQUIRE_TOUCH_FROM_ABOVE: bool = True   # True=必须有"从上方回落"的动作（推荐）
    PULLBACK_TOUCH_LOOKBACK: int = 5                 # 回看根数：最近 N 根内需有收盘 > 均线
    PULLBACK_ALLOW_BELOW_RATIO: float = 0.6          # 允许收在均线下方的比例（× MAX_DEVIATION）
                                                     # 0.6 × 0.5% = 最多允许 0.3% 的下方穿越

    # ── 维度 B：动量 ──
    MACD_FAST: int = 12
    MACD_SLOW: int = 26
    MACD_SIGNAL: int = 9
    RSI_WINDOW: int = 14
    RSI_OVERSOLD: float = 30.0
    RSI_OVERBOUGHT: float = 70.0
    VOLUME_MA_PERIOD: int = 5
    VOLUME_SURGE_RATIO: float = 1.5
    MOMENTUM_DEBUG: bool = True           # True=打印详细动量排查日志到控制台

    # ── 维度 B：信号分工（修"RSI超卖 单独放行"）──
    # 背景（P0 回测 5818 条信号实测，见 backtest_p0_report.md）：
    #   · RSI超卖 单独出现时 n=2849、10日 +0.46%、胜率 49.8% —— 是三者里最弱的
    #   · RSI超卖 与其他信号并存时 n=189、10日 +1.13%、胜率 53.4% —— 明显更好
    #   即：**RSI 超卖本身不构成买点，它是"跌得够深"的确认信息**。
    # 新逻辑：
    #   触发信号（决定 B 是否通过）= `MACD金叉/柱线翻红` 或 `成交量放大`
    #   RSI 超卖 = **确认信号**（不再单独放行），并把命中情况显示出来供人工参考
    #   RSI 超买 = 可选的**否决闸门**（防止半路追高）
    MOMENTUM_RSI_REQUIRE_CONFIRM: bool = True   # True=RSI超卖需 MACD/放量确认才计入通过
    MOMENTUM_RSI_VETO_OVERBOUGHT: bool = False  # True=RSI超买时直接否决 B（默认关，倾向刻意留着观察）
    # RSI 超卖是否仍参与"维度通过数/对比评分"（向后兼容开关，默认保持原行为）
    MOMENTUM_RSI_COUNTS_AS_SIGNAL: bool = True
    # ── B 的触发源白名单（2026-09-28 加入）──
    # 配对回测（300 只 × 2016~2026，737,697 行，日期级配对 + 分块 bootstrap）实测：
    #   全部 signal          相对当日无信号票 10 日增量 -0.574%  [-0.788, -0.358]  CI 不含 0
    #   仅「成交量放大」触发    -0.570%（单独就是这个量级）
    #   剔除 成交量放大+RSI超卖  +0.013%  [-0.258, +0.260]  CI 含 0
    # 即：**负增量几乎全部来自这两个子信号**；MACD 与 A 单独触发都测不出增量（也没有负增量）。
    # 三个独立时段（2016-19 / 2020-22 / 2023-26）结论一致。
    # 取值：任意子集组合，可选项 'MACD'（金叉/柱线翻红，二者数学恒等）、
    #       'RSI_OVERSOLD'、'VOLUME_SURGE'。留空列表 = 不允许任何触发源（B 永不通过，慎用）。
    # 注意：本开关**只改 B 是否通过**；各触发源命中与否仍照常写入 signals 并落进
    #       `b_macd_cross / b_rsi_oversold / b_vol_surge` 诊断列，便于事后对账。
    #       RSI超卖作为"确认信号"的角色不受影响（见 MOMENTUM_RSI_REQUIRE_CONFIRM）。
    # 用法：-o MOMENTUM_TRIGGERS=MACD      （只留 MACD）
    MOMENTUM_TRIGGERS: List[str] = field(
        default_factory=lambda: ["MACD", "RSI_OVERSOLD", "VOLUME_SURGE"])
    # RSI 极端超买线：70 是"偏高"，80 是"极端"。
    # 突破追高场景下 RSI>80 通常意味着"高潮量/情绪顶"，是风险信号而非买入信号。
    # 注意：它**不改 B 的通过逻辑**（B 仍由 MACD/放量触发），而是交给入口门控做否决，
    # 这样"B=动量触发"的语义不被污染，否决原因也能单独显示。
    MOMENTUM_RSI_EXTREME: float = 80.0

    # ── 维度 A：突破质量过滤（防"追高式突破"）──
    # 背景（实测 002119）：现价 29.61、前高 26.92 → 突破幅度 +9.99%，已不是"刚突破"。
    # 1) 收盘确认：要求**最近 BREAKOUT_CONFIRM_BARS 根内**发生过突破（而非盘中触及）
    # 2) 幅度上限：突破幅度 ≤ BREAKOUT_MAX_PCT，超过即视为追高
    BREAKOUT_REQUIRE_FRESH: bool = True      # True=要求突破发生在最近 N 根内
    BREAKOUT_CONFIRM_BARS: int = 3           # 突破需落在最近 N 根内（收盘价确认）
    BREAKOUT_MAX_PCT: float = 0.05           # 突破幅度上限（0.05 = 前高的 5%）；None/≥1 表示不限制

    # ── 入口门控（P0 核心：打通"市场状态 / RSI超买 / 赔率 / 生存性"四否决）──
    # 设计：不改 check_dimension_a/b/c 的通过语义（它们的返回契约被多个验证脚本依赖），
    # 而是在三维度之外新增一层独立门控，把"维度通过"与"综合放行"分开。
    ENTRY_GATE_ENABLED: bool = True             # 门控总开关（False=回到仅看 A/B/C）
    ENTRY_GATE_REGIME_ENABLED: bool = True      # 震荡市禁突破买入
    ENTRY_GATE_RSI_ENABLED: bool = True         # RSI 极端超买否决
    # 赔率否决：C 未通过（盈亏比不足）时否决。
    # 为什么要单独给开关（2026-09-29 加入）：
    #   原本这条规则的 "enabled" 硬编码为 True，导致一个**可测性**问题 ——
    #   C 不通过 ⇒ 必然追加 RR_RATIO 否决 ⇒ 门控失败 ⇒ 该样本被归入 vetoed。
    #   于是"C 到底有没有用"无法回答：**不存在「C❌ 但门控放行」的对照样本**。
    #   关掉本开关后，C❌ 且 A/B 通过的行会落进 below_bc 类，
    #   从而 `signal`(C✅) vs `below_bc`(C❌) 成为可在同一批数据上直接对比的对照。
    #   注意：关掉它**不会**关掉 C 的计算与展示，C 仍照常判定并写入 c_pass / rr_ratio。
    ENTRY_GATE_RR_RATIO_ENABLED: bool = True
    # 允许突破买入的市场状态白名单（其余状态一律禁突破）
    ENTRY_GATE_BREAKOUT_ALLOWED_REGIMES: List[str] = field(
        default_factory=lambda: ["TREND_UP", "SQUEEZE"])

    # ── 第 ④ 条否决：生存性 / 可交易性（2026-09-30 加入）──
    # 为什么加：300 只 × 2016-2026 的逐日配对检验显示，"落在区间内退市票上的信号"
    #   10 日相对收益 -3.564%（t=-8.05），贡献了全样本几乎全部的负向。而退市状态
    #   **事后**才知道，不能直接当规则；本规则只用**发信号当日可得**的日线派生量。
    #
    # 实测（10日、次日开盘入场，复现：python scripts/find_survival_filter.py）：
    #   不筛选         : -0.297%/10日 (t=-3.42, 显著负)
    #   加本否决后     : -0.056%/10日 (t=-0.61, 不显著)
    #   命中(被否决)组 : -0.746%/10日 (t=-4.27, 显著负)
    #   完美剔除退市票 : -0.100%/10日（事后方知、不可实现 —— 本规则反而更好）
    #
    # 关键定位：本规则**只降低伤害、不产生 alpha**（两侧期望都约等于 0）。
    #   代价是剔除约 21% 的信号；收益是避开"低价 + 低流动性"这一档的尾部损失。
    #   它属于风控层，不属于选股层 —— 不要指望它提高收益。
    ENTRY_GATE_SURVIVABILITY_ENABLED: bool = True
    MIN_ENTRY_PRICE: float = 3.0        # 元；收盘价低于此值 → 否决（低价/面值退市风险）
    MIN_ENTRY_AMOUNT: float = 2e7       # 元；20日均成交额低于此值 → 否决（流动性不足）

    # ── 维度 B 增强：大盘环境过滤 ──
    ENABLE_MARKET_FILTER: bool = False        # 默认关闭，保持向后兼容；短线建议开启
    MARKET_DEBUG: bool = True                 # True=打印详细大盘过滤排查日志到控制台
    BENCHMARK_INDEX: str = "sh000001"         # 基准指数（上证综指）；可改 'sz399001' / 'sh000300'
    BENCHMARK_MA_PERIOD: int = 20             # 大盘趋势判定均线周期
    BENCHMARK_MIN_CHANGE_PCT: float = -0.5    # 当日涨跌幅下限（-0.5 = 跌幅不超过 0.5%）
    BENCHMARK_FETCH_DAYS: int = 30            # 拉取指数的天数
    BENCHMARK_FETCH_TIMEOUT: int = 30         # 指数数据获取超时（秒）
                                              # ⚠️ baostock 新进程首次登录需 ~15s
                                              #    （会话已复用，见 market_filter._ensure_bs_login），
                                              #    取数本身仅 0.2~3s。原先默认 5s 会把 baostock 掐死，
                                              #    导致大盘过滤长期"取数失败 → 直接否决"。
    BENCHMARK_PREFER_SOURCE: str = "baostock" # 首选数据源：'baostock' / 'efinance'
                                              # 本机 efinance 走 eastmoney HTTPS 常被对端断开

    # ── 基准指数自动选择（benchmark.py）──
    # 实测：个股与任何指数相关性仅 0.15~0.38（ETF 是 0.80~0.94），
    # 且"按板块换基准"会选错一半（同为创业板股票，最优基准分布在上证/深证/创业板之间）。
    # 故默认仍是上证综指（候选里中位相关性最高），需要精确对齐时用 --index auto。
    BENCHMARK_AUTO_ENABLED: bool = False      # True=按过去 N 日收益率相关性自动选基准
    BENCHMARK_AUTO_LOOKBACK: int = 240        # 相关性回看根数（与 DATA_TRADING_DAYS 对齐，
                                              # 避免样本长度漂移导致选择抖动）

    # ── 维度 C：赔率 ──
    # 默认从 "swing"（近20日最低）改为 "near_low"（近端低点 + 距离上限）：
    # 实测 002119 用 swing 时止损距现价 −26.7%、盈亏比 0.34:1（几乎不可能靠胜率弥补），
    # 换 near_low 后止损距离降到 −8.0%、盈亏比 1.13:1 —— 短线突破交易不该用那么远的结构支撑。
    # swing 仍然保留，可用 -o STOP_MODE=swing 回到旧口径。
    # 默认保持 near_low —— 这是用户已用了一个多月的口径，属"当前体感基线"。
    # swing_atr（按波动率自适应）作为**可选**实验口径保留，用 -o STOP_MODE=swing_atr 试。
    # 实测结论：放宽止损能把被扫率从 82% 降到 30%，但盈亏比从 6~12 掉到 0.7~2.0，
    # 且各 RR 档的 MAE 几乎相同（−5.9~−7.0%）→ 放宽止损没有换来更好的风险特征。
    STOP_MODE: str = "near_low"
    PROFIT_MODE: str = "swing_enhanced"
    RR_WINDOW: int = 20
    # 盈亏比门槛：实测按盈亏比分档，2–3 档 10日均 −0.04%（最差，等于无效过滤），
    # 3–5 档 +0.35%、5–10 档 +0.36% 才转正 → 波段门槛提到 3.0。
    MIN_RR_RATIO: float = 3.0
    ATR_PERIOD: int = 14
    ATR_STOP_MULT: float = 1.5
    ATR_PROFIT_MULT: float = 3.0
    FIXED_STOP_PCT: float = 0.02
    FIXED_PROFIT_MULT: float = 2.0

    # ── 止损最小距离保护（防"退化止损"）──
    # 问题：swing 模式止损取「近 RR_WINDOW 日最低」。若标的是"刚创阶段新低又立刻反弹"
    #       （ETF 因走势平滑尤其常见），该低点会几乎等于现价 → 亏损空间趋近 0 →
    #       盈亏比虚高到 ∞（实测黄金ETF 0.06% → 137:1），仓位公式还会算出"满仓"。
    # 保护：止损至少离现价 STOP_MIN_DIST_ATR_MULT × ATR，且不少于 STOP_MIN_DIST_PCT × 现价。
    # 二者取更宽的那个（更保守）。仅在止损距离小于保护值时生效，正常情况完全不影响。
    # 想让保护更强（如 ETF 波段）可调大倍率，例如 -o STOP_MIN_DIST_ATR_MULT=1.0
    STOP_MIN_DIST_ENABLED: bool = True
    STOP_MIN_DIST_ATR_MULT: float = 0.5      # 以 ATR 为尺度：止损距离下限 = 0.5 × ATR
    STOP_MIN_DIST_PCT: float = 0.003         # 以价格为尺度：止损距离下限 = 0.3% × 现价（防 ATR 失效）

    # ── 近端结构止损（STOP_MODE="near_low"）──
    # 背景（实测 002119）：swing 用「近 20 日最低」= 21.69，距现价 29.61 有 −26.7%，
    # 同一时刻止盈只有 +9.1% → 盈亏比 0.34:1，几乎不可能靠胜率弥补。
    # 短线突破交易不该用那么远的结构支撑，故新增"近端"模式：
    #   止损 = max(近 NEAR_LOW_WINDOW 日最低, 现价 − NEAR_LOW_MAX_PCT × 现价)
    # 即"取离现价更近的那个"，同时用上限防止止损贴得过近（那会虚高盈亏比）。
    NEAR_LOW_WINDOW: int = 5                 # 近端低点回看根数
    NEAR_LOW_MAX_PCT: float = 0.08           # 近端止损距现价的上限（8%）

    # ── 波段(几天~两周)自适应止损：STOP_MODE="swing_atr" ──
    # 为什么不用固定百分比：实测平均 ATR%=3.61%，固定 8% 在高波动股上仅 2.2×ATR
    # → 47% 的信号会被正常波动扫掉；低波动股又拿到过宽的止损、盈亏比虚低。
    # 依据：MAE 中位 −6.2%、P25 −11.4%，覆盖 75% 信号需 ≈3.2×ATR。
    SWING_ATR_STOP_MULT: float = 3.0         # 止损 = min(近端结构位, 现价 − 3.0×ATR)
    SWING_ATR_STRUCT_WINDOW: int = 10        # 近端结构位回看根数
    SWING_ATR_MAX_PCT: float = 0.12          # 止损距现价上限（12%），防结构位过远
    SWING_ATR_STRUCT_SLACK_ATR: float = 1.0  # 结构位需比 ATR 止损更远出 N×ATR 才采用它
                                             # （否则结构位贴着现价，多半是噪声，改用 ATR 止损）

    # ── 波段止盈：移动止盈（在 --monitor 持仓跟踪里逐日生效）──
    SWING_TRAIL_ACTIVATE_ATR: float = 2.0    # 浮盈达 N×ATR 后启动移动止盈
    SWING_TRAIL_ATR_MULT: float = 2.0        # 移动止盈回撤宽度 = N×ATR

    # ── 波段时间止损 ──
    SWING_TIME_STOP_DAYS: int = 10           # 持有超过 N 日
    SWING_TIME_STOP_MIN_GAIN_ATR: float = 1.0  # 且浮盈未达 N×ATR → 平仓（套着不动就换手）

    # ── ETF 模式（etf.py）──
    # 开启后：① 基准指数切到该 ETF 的标的指数（维度B/维度E 都看它）
    #         ② 放宽 ETF 偏严的阈值（回踩偏离、缩量比、放量倍数、宽基 RSI 超卖线）
    # 默认关闭以免误伤个股；main.py 的 --etf 显式开启，--auto-etf 按代码前缀自动识别。
    AUTO_ETF_MODE: bool = False              # True=按代码前缀自动进入 ETF 模式（无需 --etf）
    ETF_MODE_ENABLED: bool = False           # 运行时由 CLI 置位（--etf / --auto-etf 命中）

    # ── 主题锚指数：ETF 模式下自动选"看哪张天气图" ──
    # 为什么需要：`etf._ETF_MAP` 里行业主题 ETF 的标的指数大多未收录（reason='sector'），
    # 原先一律兜底到 sh000001 —— 结果是**拿半导体 ETF 却看上证综指的市场状态**。
    # 实测（2026-10-01，baostock 各 2855 根 = 10.8 年，恐慌日买/持 20 日）：
    #   指数          全样本      A段2016-2020  B段2021-   两段同号
    #   sh000001    +2.17pp(p=.010)   +0.48        +4.16       ✅
    #   sz399001    +1.73pp(p=.085)   +0.77        +2.66       ✅
    #   sz399006    +1.07pp(p=.190)   +1.60        +1.13       ✅
    #   sh000300    +1.09pp(p=.160)   +0.44        +1.58       ✅
    #   ⇒ 四个都可用（方向在两种体制下一致）。**科创50 sh000688 baostock 无数据**，
    #     故科创/半导体类主题用创业板指(sz399006)代理 —— 半导体在创业板权重高。
    # 匹配方式：按 **ETF 名称关键词**（名称由 baostock query_stock_basic 取，本地缓存）。
    # 所以新上市的 ETF 只要名字含关键词就自动匹配，**不需要逐只登记**。
    ETF_THEME_ANCHOR_ENABLED: bool = True

    # 科创/半导体类主题用哪个锚：'cyb'=创业板指（默认） | 'kc50'=科创50
    #
    # 实测（2026-10-01，恐慌日买/持 h 日，同日对照）：
    #   持有期  创业板指            科创50
    #     h=3   −0.16pp(p=.645)   +1.26pp(p=.040) ✅  ← 短持有期科创50更优
    #     h=20  +0.97pp(p=.270)   +0.58pp(p=.335)     ← 默认持有期创业板指更优
    #   创业板指 2016-2020/2021- 两段同号（+1.60/+1.13）；科创50 基日 2019-12-31，
    #   2020 才有数据，**永远无法做双体制校验**，且其"A段"(仅2020)极不稳定。
    # ⇒ 结论：默认保持创业板指（历史长、两段同号、默认持有期更强）；
    #   若你改用**短持有期（约3日）**，用 kc50 更优。运行时切换：
    #       -o THEME_ANCHOR_KC=kc50      或      --kc50
    THEME_ANCHOR_KC: str = "cyb"

    # 精确代码覆盖：优先级最高，用于"关键词匹配不准"或想手工指定锚的场合。
    # 例：ETF_INDEX_OVERRIDE = {"512760": ("sh000300", "沪深300")}
    ETF_INDEX_OVERRIDE: dict = field(default_factory=dict)

    # (关键词元组, 指数代码, 指数名) —— **按顺序匹配，先命中先用**，故具体的放前面。
    THEME_INDEX_RULES: tuple = (
        # 半导体 / 芯片 / 科创 —— 锚由 THEME_ANCHOR_KC 决定（$KC 占位符）
        # 默认创业板指；-o THEME_ANCHOR_KC=kc50 或 --kc50 切到科创50
        (("半导体", "芯片", "集成电路", "科创", "消费电子", "面板", "光模块"),
         "$KC", "科创主题锚"),
        # TMT / 软件 / 云计算 —— 仍用创业板指（它们不是科创50 的主要权重）
        (("电子", "通信", "计算机", "软件", "云计算", "大数据",
          "人工智能", "TMT", "信创", "数字经济"),
         "sz399006", "创业板指"),
        # 新能源 / 电力设备 / 汽车
        (("新能源", "光伏", "储能", "电池", "电力设备", "电网", "风电", "碳中和",
          "新能车", "汽车", "锂电", "充电桩"),
         "sz399006", "创业板指"),
        # 医药 / 生物 / 医疗
        (("医药", "生物", "医疗", "创新药", "疫苗", "CXO"),
         "sz399006", "创业板指"),
        # 军工 / 高端制造 / 机械
        (("军工", "国防", "航空", "航天", "机械", "高端装备", "机器人"),
         "sz399001", "深证成指"),
        # 有色 / 煤炭 / 钢铁 / 化工 / 资源
        (("有色", "煤炭", "钢铁", "化工", "稀土", "资源", "矿业", "石油", "能源"),
         "sh000001", "上证综指"),
        # 公用事业 / 基建（**防御性**，与"电力设备"成长股不同，故单列且排在它之后）
        # 例：电力公用事业ETF 看上证综指；电网设备ETF 应命中上面的"电网"→创业板指
        (("公用事业", "电力", "水务", "燃气", "环保", "交通", "基建", "建筑", "港口"),
         "sh000001", "上证综指"),
        # 银行 / 券商 / 保险 / 红利 / 央企
        (("银行", "证券", "券商", "保险", "金融", "红利", "央企", "国企"),
         "sh000001", "上证综指"),
        # 消费 / 白酒 / 食品 / 家电 / 农业
        (("消费", "白酒", "食品", "饮料", "家电", "农业", "养殖", "旅游"),
         "sh000300", "沪深300"),
        # 商品类：黄金/白银/原油与 A 股低相关，任何 A 股指数都不是好锚。
        # 这里仍给 sh000001 以保证取数不失败，但 index_name 里**带上警示**，
        # 让输出能一眼看出"这个锚意义有限"（此前被静默兜底，看不出问题）。
        (("黄金", "白银", "贵金属", "有色金属", "原油", "商品", "豆粕", "能化"),
         "sh000001", "上证综指(⚠️商品类，与A股低相关，锚意义有限)"),
        # 宽基：按名称直接对应（放最后，避免"科创50"被前面的"科创"抢走）
        (("科创50", "科创板50"), "sh000688", "科创50(⚠️baostock 无数据，会失效)"),
        (("创业板", "创50", "创成长"), "sz399006", "创业板指"),
        (("深证", "深成"), "sz399001", "深证成指"),
        (("沪深300", "中证300"), "sh000300", "沪深300"),
        (("中证500",), "sh000905", "中证500"),
        (("中证1000", "中小"), "sz399005", "中小100"),
        (("上证", "上证50", "综指"), "sh000001", "上证综指"),
    )

    # ── 仓位管理 ──
    MAX_RISK_PER_TRADE: float = 0.01
    TOTAL_CAPITAL: float = 100_000

    # ── 维度 D：周线趋势背景（独立观察哨，只读模式） ──
    # 设计原则：不参与 A/B/C 总分计算，不触发自动止损/开仓
    # 数据层完全解耦：独立调用周线接口，失败时仅输出"数据暂不可用"，绝不影响 A/B/C
    ENABLE_WEEKLY_OBSERVER: bool = True            # 维度D总开关（即便关闭也不影响 A/B/C，仅跳过周线区块显示）
    WEEKLY_DEBUG: bool = True                       # True=打印详细周线排查日志到控制台（[D-周线观察] 前缀）
    WEEKLY_FETCH_BARS: int = 120                    # 拉取最近 N 根周 K 线（需求单要求 120 根）
    WEEKLY_FETCH_TIMEOUT: int = 8                   # 周线数据单源获取超时秒数
    WEEKLY_CACHE_ENABLED: bool = True               # 每日 1 次缓存（首次调用后当天复用，避免频繁请求被禁 IP）
    WEEKLY_MA20_PERIOD: int = 20                    # 周线 MA20 周期（趋势判定）
    WEEKLY_MA60_PERIOD: int = 60                    # 周线 MA60 周期（牛熊线）
    WEEKLY_MA_TREND_THRESHOLD: float = 0.001        # 周线 MA20 趋势判定阈值：环比变化幅度 < 0.1% 视为走平
    WEEKLY_RSI_PERIOD: int = 14                      # 周线 RSI 周期
    WEEKLY_RSI_OVERSOLD: float = 30.0               # 周线 RSI 超卖线
    WEEKLY_RSI_OVERBOUGHT: float = 70.0            # 周线 RSI 超买线
    WEEKLY_RSI_NEUTRAL_LOW: float = 40.0           # 评级A 多头趋势的 RSI 下限
    WEEKLY_RSI_NEUTRAL_HIGH: float = 60.0          # 评级A 多头趋势的 RSI 上限
    WEEKLY_MACD_FAST: int = 12                      # 周线 MACD 快线
    WEEKLY_MACD_SLOW: int = 26                      # 周线 MACD 慢线
    WEEKLY_MACD_SIGNAL: int = 9                     # 周线 MACD 信号线
    WEEKLY_HIST_SHRINK_BARS: int = 3                # 判定 MACD 柱状体"连续缩短"所需的最小连续根数

    # ── 维度 D：周线仓位折减（提示式，不改 calc_position 的股数计算）──
    # 背景（实测 002119）：周线评级 A（顺风），但价格距周 MA60 **+37.1%**，
    # 同时日线 RSI 84.5 极端超买 —— "周线顺风"不等于"日线可以追高"。
    # 故对"顺风但已远离均线"的情形给出仓位打折建议，交人工执行。
    WEEKLY_FAR_FROM_MA60_PCT: float = 30.0          # 价格距周 MA60 超过该百分比即视为"远离"
    WEEKLY_FAR_POSITION_DISCOUNT: float = 0.5       # 远离时的建议仓位折扣

    # ── 维度 E：市场状态雷达（regime detector，独立只读模块） ──
    # 设计原则：与维度D 同构——只打印"现在是什么市"，绝不修改任何阈值、不参与 A/B/C 判定。
    # 对账流程：先跑一段时间人工核对机器判定与你眼里的行情是否一致，再考虑接入自动改阈值。
    ENABLE_REGIME_DETECTOR: bool = True             # 维度E总开关（关闭仅跳过该区块，不影响 A/B/C/D）
    REGIME_DEBUG: bool = True                       # True=打印详细状态排查日志（[E-市场状态] 前缀）
    REGIME_FETCH_BARS: int = 250                    # 拉取指数日线根数（需覆盖 ATR/带宽分位的历史窗口）
    REGIME_FETCH_TIMEOUT: int = 45                  # 指数数据单源超时秒数
                                                    # ⚠️ baostock 的指数查询冷启动需 20~30s，
                                                    #    阈值设太小会把唯一可用的数据源掐死
                                                    #    （实测 250 根约 31s，故留足余量）
    REGIME_PREFER_SOURCE: str = "baostock"          # 首选数据源：'baostock' / 'efinance'
                                                    # 本机 efinance 走 eastmoney HTTPS 常被 TLS 断开，
                                                    # 先试死源会白等一个超时周期
    # 均线结构
    REGIME_MA_SHORT: int = 5                        # 短均线
    REGIME_MA_MID: int = 20                         # 中均线（震荡/趋势分界的核心）
    REGIME_MA_LONG: int = 60                        # 长均线（牛熊线）
    REGIME_MA20_SLOPE_LOOKBACK: int = 5             # 中轨斜率回看根数（约一周）
    REGIME_MA20_SLOPE_MIN_PCT: float = 0.0          # 斜率 > 该值(百分比)才视为"向上"
    # ADX（专治震荡市：ADX < 20 视为无趋势）
    REGIME_ADX_PERIOD: int = 14
    REGIME_ADX_TREND_MIN: float = 25.0              # ADX ≥ 该值才认定"有明确趋势"
    REGIME_CROSS_WINDOW: int = 20                   # 统计"价格穿越 MA中轨"的回看根数
    REGIME_CROSS_TREND_MAX: int = 3                 # 窗口内穿越次数 ≥ 该值 → 判定为"无趋势（区间震荡）"
    REGIME_PCTRANK_WINDOW: int = 120                # ATR/带宽分位的历史回看窗口（0 表示用全部拉取数据）    # ATR 波动分位
    REGIME_ATR_PERIOD: int = 14
    REGIME_PANIC_ATR_PCT: float = 0.85              # 通道①：ATR 分位 ≥ 该值（波动率已在高位）
    REGIME_PANIC_RET3_PCT: float = -3.0             # 通道①：近3日累计跌幅 ≤ 该值，且跌破 MA60
    REGIME_PANIC_RET3_ATR_MULT: float = 2.5         # 通道②：近3日累计跌幅 ≤ -(该倍数 × ATR%)
    REGIME_PANIC_RET3_FLOOR_PCT: float = -3.5       # 通道②的绝对下限：跌幅不到该值一律不算恐慌
                                                    # （防止"低波动环境里跌 2.5%"被 ATR 倍数放大成假恐慌）
                                                    # ⚠️ 对账期重点观察这两个参数，它们最容易误报/漏报
    # 布林带（波动收敛/扩张 + 震荡区间高低位）
    REGIME_BB_PERIOD: int = 20
    REGIME_BB_STD: float = 2.0
    REGIME_SQUEEZE_BW_PCT: float = 0.20             # 带宽分位 ≤ 该值 → 波动收敛（挤压候选）
    REGIME_SQUEEZE_CALIBER: float = 4.0             # 近20日振幅 / ATR ≤ 该值 → 价格被压缩在窄幅内（挤压确认）
    REGIME_RANGE_HIGH_POS: float = 0.80             # 带内位置 ≥ 该值 → 高位震荡
    REGIME_RANGE_LOW_POS: float = 0.20              # 带内位置 ≤ 该值 → 低位震荡


@dataclass
class HighVolatilityConfig(TradingConfig):
    """急跌-反弹 高波动行情配置。

    针对市场波动放大、情绪极端的场景：
    - 维度A：放宽结构容忍度，允许偏离均线更远
    - 维度B：提高动量门槛，防止追高被套
    - 维度C：切换 ATR 动态追踪，止损更宽
    - 仓位：降低单笔风险，轻仓试错
    """

    # ── 维度 A：放宽结构容忍度 ──
    PULLBACK_THRESHOLD: float = 0.025   # 1% → 2.5%，允许偏离均线更远
    BREAKOUT_WINDOW: int = 10          # 20天 → 10天，只看最近的最强阻力支撑

    # ── 维度 B：提高动量过滤门槛 ──
    RSI_OVERSOLD: float = 25.0         # 30 → 25，要求跌得更透才考虑买入
    VOLUME_SURGE_RATIO: float = 1.8    # 1.5 → 1.8，要求更强的真金白银进场

    # ── 维度 C：切换 ATR 动态追踪 ──
    STOP_MODE: str = "atr"
    PROFIT_MODE: str = "atr"
    ATR_PERIOD: int = 14
    ATR_STOP_MULT: float = 2.0         # 1.5 → 2.0，止损放宽，防止被盘中毛刺扫掉
    ATR_PROFIT_MULT: float = 4.0       # 3.0 → 4.0，博取更大反弹空间

    # ── 仓位管理：降低单笔亏损上限 ──
    MAX_RISK_PER_TRADE: float = 0.008  # 1% → 0.8%，轻仓试错

    # ── 最小盈亏比：配合高波动稍微放宽 ──
    MIN_RR_RATIO: float = 1.8          # 2.0 → 1.8


@dataclass
class ConservativeConfig(TradingConfig):
    """保守型配置：适用于震荡市或不确定行情。

    - 结构：要求更严格的突破确认
    - 动量：只在深度超卖时才出手
    - 赔率：要求更高的盈亏比
    - 仓位：更轻
    """

    PULLBACK_THRESHOLD: float = 0.008   # 0.8%，只接受非常贴近均线的回踩
    RSI_OVERSOLD: float = 28.0          # 28，只在较深超卖时考虑
    VOLUME_SURGE_RATIO: float = 2.0     # 2.0倍，要求放量更充分
    MIN_RR_RATIO: float = 2.5           # 2.5:1，要求更高的安全边际
    MAX_RISK_PER_TRADE: float = 0.005  # 0.5%，更保守的仓位


@dataclass
class TrendFollowingConfig(TradingConfig):
    """趋势跟随配置：适用于单边行情。

    - 结构：放宽突破窗口，捕捉更大级别趋势
    - 动量：对放量要求更严格
    - 赔率：止损用 swing（结构支撑），止盈用 ATR 追踪
    """

    BREAKOUT_WINDOW: int = 30           # 30天，看更大级别的突破
    PULLBACK_THRESHOLD: float = 0.015   # 1.5%，允许正常回踩
    VOLUME_SURGE_RATIO: float = 1.8     # 1.8倍，趋势需要放量确认
    STOP_MODE: str = "swing"
    PROFIT_MODE: str = "atr"            # 止盈用 ATR 追踪，让利润奔跑
    ATR_PROFIT_MULT: float = 5.0        # 5倍 ATR，追踪大趋势


@dataclass
class AggressiveShortConfig(TradingConfig):
    """激进短线：精准踩 MA5，允许均线走平，严抓买点。

    适合日内/超短线操作，信号触发少但胜率高。
    """

    USE_PULLBACK_ENHANCE: bool = True
    PULLBACK_MA_TARGETS: List[int] = field(default_factory=lambda: [5])
    PULLBACK_MAX_DEVIATION: float = 0.003        # 0.3% 极严
    PULLBACK_REQUIRE_MA_UP: bool = False          # 允许走平
    PULLBACK_REQUIRE_SHRINK_VOLUME: bool = True
    PULLBACK_VOLUME_SHRINK_RATIO: float = 0.7    # 0.7 倍量，极度缩量
    PULLBACK_REQUIRE_BULLISH_CANDLE: bool = True
    PULLBACK_CONFIRM_BARS: int = 1               # 只看当天


@dataclass
class SteadyShortConfig(TradingConfig):
    """稳健短线：MA5/MA10 双均线确认，要求趋势向上。

    适合波段操作，信号稍多但可靠性高。
    """

    USE_PULLBACK_ENHANCE: bool = True
    PULLBACK_MA_TARGETS: List[int] = field(default_factory=lambda: [5, 10])
    PULLBACK_MAX_DEVIATION: float = 0.008        # 0.8% 稍宽
    PULLBACK_REQUIRE_MA_UP: bool = True          # 必须向上
    PULLBACK_REQUIRE_SHRINK_VOLUME: bool = True
    PULLBACK_VOLUME_SHRINK_RATIO: float = 0.6    # 0.6 倍量，极度缩量
    PULLBACK_REQUIRE_BULLISH_CANDLE: bool = True
    PULLBACK_CONFIRM_BARS: int = 2


@dataclass
class ShortTermConfig(TradingConfig):
    """超短线配置：强制大盘过滤。

    吃情绪溢价，大盘暴跌泥沙俱下，必须看大盘做个股。
    """

    ENABLE_MARKET_FILTER: bool = True
    BENCHMARK_INDEX: str = "sh000001"
    BENCHMARK_MA_PERIOD: int = 20
    BENCHMARK_MIN_CHANGE_PCT: float = -0.5


@dataclass
class UltraShortConfig(TradingConfig):
    """超短线止盈止损配置：紧止损 + 短期压力位止盈。

    - 止损：入场价 - 1.0×ATR（紧止损，超短线快速认错）
    - 止盈：10日最高价 + 2×ATR（以10日最高点为压力基准，上方再博取2倍ATR波动率空间）
    - 适合日内/超短线，与 short_term 大盘过滤叠加使用效果更佳
    """

    STOP_MODE: str = "atr"
    ATR_STOP_MULT: float = 1.0            # 1.0倍ATR，紧止损
    PROFIT_MODE: str = "swing_ultra"      # 10日最高 + N×ATR
    RR_WINDOW: int = 10                   # 10日最高点为压力基准
    ATR_PROFIT_MULT: float = 2.0          # 压力位上方2倍ATR
    ATR_PERIOD: int = 14


@dataclass
class SwingConfig(TradingConfig):
    """波段配置：放宽大盘涨跌幅限制。

    可忍受短期回调，但防股灾。
    """

    ENABLE_MARKET_FILTER: bool = True
    BENCHMARK_INDEX: str = "sh000001"
    BENCHMARK_MA_PERIOD: int = 20
    BENCHMARK_MIN_CHANGE_PCT: float = -1.0       # 允许 1% 跌幅


@dataclass
class LongTermConfig(TradingConfig):
    """长线价投配置：关闭大盘过滤。

    看内在价值，跌了反而便宜。
    """

    ENABLE_MARKET_FILTER: bool = False


# ── 配置注册表 ──

_CONFIG_REGISTRY: dict = {
    "default": TradingConfig,
    "high_vol": HighVolatilityConfig,
    "conservative": ConservativeConfig,
    "trend": TrendFollowingConfig,
    "aggressive_short": AggressiveShortConfig,
    "steady_short": SteadyShortConfig,
    "short_term": ShortTermConfig,
    "ultra_short": UltraShortConfig,
    "swing": SwingConfig,
    "long_term": LongTermConfig,
}

_PROFILE_DESCRIPTIONS: dict = {
    "default": "标准配置 - 日常使用",
    "high_vol": "高波动配置 - 急跌反弹行情",
    "conservative": "保守配置 - 震荡市/不确定行情",
    "trend": "趋势跟随配置 - 单边行情",
    "aggressive_short": "激进短线 - 精准踩MA5",
    "steady_short": "稳健短线 - MA5/MA10双均线确认",
    "short_term": "超短线 - 强制大盘过滤",
    "ultra_short": "超短线止盈止损 - 紧止损+10日高压基准",
    "swing": "波段 - 放宽大盘涨跌幅限制",
    "long_term": "长线价投 - 关闭大盘过滤",
}


def get_config(profile: str = "default") -> TradingConfig:
    """按名称获取配置实例。

    Args:
        profile: 配置档名称，可选值见 list_profiles()

    Returns:
        对应配置的实例

    Raises:
        ValueError: 未知的 profile 名称
    """
    cls = _CONFIG_REGISTRY.get(profile)
    if cls is None:
        available = ", ".join(_CONFIG_REGISTRY.keys())
        raise ValueError(f"未知配置档 '{profile}'，可用: {available}")
    return cls()


def merge_profiles(profile_names: list) -> tuple:
    """按顺序叠加多个配置档，返回 (合并后的实例, 字段来源字典)。

    规则：
        - 第一个 profile 作为基底
        - 后续每个 profile 只覆盖"**它自己显式定义过的字段**"（即：在配置类 __dict__ 中
          直接出现的字段；继承自父类的默认值不参与覆盖，避免误覆盖）
        - 叠加顺序是从左到右（后面的覆盖前面的同名显式字段）
        - 返回的第二个值是 `dict[field_name, source]`，用于 --show-config 时标注来源

    Args:
        profile_names: 配置档名称列表，例如 ['aggressive_short', 'long_term']
                       空列表会自动兜底 default。

    Returns:
        (merged_config: TradingConfig, field_sources: dict[str, str])

    Raises:
        ValueError: 列表中包含未知的 profile 名称
    """
    from dataclasses import fields as _dc_fields

    if not profile_names:
        profile_names = ["default"]

    merged = get_config(profile_names[0])
    # 每字段来源：初始默认第一个 profile
    field_sources = {f.name: profile_names[0] for f in _dc_fields(TradingConfig)}

    for p_name in profile_names[1:]:
        p_cls = _CONFIG_REGISTRY.get(p_name)
        if p_cls is None:
            available = ", ".join(_CONFIG_REGISTRY.keys())
            raise ValueError(f"未知配置档 '{p_name}'，可用: {available}")
        p_instance = p_cls()

        # 只覆盖该"子类**显式声明**"的字段（class.__dict__ 中存在的 dataclass field）
        explicit_in_subclass = set()
        for f in _dc_fields(p_cls):
            # 通过类层面检查该属性是在本类直接定义还是从父类继承
            if f.name in p_cls.__dict__:
                explicit_in_subclass.add(f.name)

        for f_name in explicit_in_subclass:
            new_val = getattr(p_instance, f_name)
            setattr(merged, f_name, new_val)
            field_sources[f_name] = p_name

    return merged, field_sources


def _parse_bool(s: str) -> bool:
    """宽松解析 bool 字符串。支持 true/1/yes/y/on 及反义词，大小写不敏感。"""
    v = s.strip().lower()
    if v in {"true", "1", "yes", "y", "on"}:
        return True
    if v in {"false", "0", "no", "n", "off"}:
        return False
    raise ValueError(f"无法解析为 bool: {s!r} (true/false/1/0/yes/no)")


def _parse_list_type(tp, s: str):
    """把逗号分隔字符串解析成 List[T]。支持 '[a,b]' 或直接 'a,b'。"""
    import typing as _typing
    origin = getattr(tp, "__origin__", None)
    args = getattr(tp, "__args__", ())
    if origin is list and args:
        inner_type = args[0]
    else:
        # 兜底：当作 str 列表
        inner_type = str
    stripped = s.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        stripped = stripped[1:-1]
    if not stripped:
        return []
    parts = [p.strip() for p in stripped.split(",") if p.strip() != ""]
    result = []
    for p in parts:
        if inner_type is int:
            result.append(int(p))
        elif inner_type is float:
            result.append(float(p))
        elif inner_type is bool:
            result.append(_parse_bool(p))
        else:
            # 去掉两端引号（若是字面量）
            if (p.startswith("'") and p.endswith("'")) or (p.startswith('"') and p.endswith('"')):
                p = p[1:-1]
            result.append(p)
    return result


def _parse_tuple_type(tp, s: str):
    """类似 list，但结果是 tuple。"""
    return tuple(_parse_list_type(tp, s))


def apply_overrides(
    config: TradingConfig,
    overrides: list,
    existing_sources: dict | None = None,
) -> dict:
    """逐个应用 key=value 覆盖到 config 实例，返回更新后的来源字典。

    每个 override 是 "KEY=VALUE" 字符串。
    按 TradingConfig 中对应字段的声明类型进行安全解析：

        - bool    : true/1/yes / false/0/no
        - int     : 十进制整数
        - float   : 十进制浮点
        - List[T] : 逗号分隔（如 '[5,10]' 或 '5,10'），按内部 T 解析
        - Tuple[T]: 同上，返回 tuple
        - str     : 原样（自动去掉两端引号）

    Args:
        config: 待修改的配置实例（会原地 modify）
        overrides: ["KEY=VAL", ...]
        existing_sources: 传入来自 merge_profiles() 的来源字典，会被更新为 "override:KEY=VAL"

    Returns:
        更新后的来源字典（与 existing_sources 是同一对象引用，方便链式调用）

    Raises:
        ValueError: KEY 不存在 / VALUE 类型解析失败
    """
    import typing as _typing
    from dataclasses import fields as _dc_fields

    if existing_sources is None:
        existing_sources = {f.name: "default" for f in _dc_fields(TradingConfig)}

    field_map = {f.name: f for f in _dc_fields(TradingConfig)}

    for idx, raw in enumerate(overrides):
        if "=" not in raw:
            raise ValueError(
                f"第 {idx+1} 个 override {raw!r} 格式错误，应为 KEY=VALUE（例如 ENABLE_MARKET_FILTER=false）")
        key, val_str = raw.split("=", 1)
        key = key.strip()
        val_str = val_str.strip()
        if key not in field_map:
            available = ", ".join(field_map.keys())
            raise ValueError(f"未知字段 {key!r}，可用字段: {available}")

        f = field_map[key]
        tp = f.type

        # 针对各种字段类型解析
        if tp is bool or (getattr(tp, "__origin__", None) is None and isinstance(tp, type) and issubclass(tp, bool)):
            value = _parse_bool(val_str)  # type: ignore[assignment]
        elif tp is int or (isinstance(tp, type) and issubclass(tp, int)):
            value = int(val_str, 0)  # 支持 0x / 0b 前缀
        elif tp is float or (isinstance(tp, type) and issubclass(tp, float)):
            value = float(val_str)
        elif tp is str or (isinstance(tp, type) and issubclass(tp, str)):
            if (val_str.startswith("'") and val_str.endswith("'")) or (val_str.startswith('"') and val_str.endswith('"')):
                value = val_str[1:-1]
            else:
                value = val_str
        elif getattr(tp, "__origin__", None) is list:
            value = _parse_list_type(tp, val_str)
        elif getattr(tp, "__origin__", None) is tuple:
            value = _parse_tuple_type(tp, val_str)
        else:
            # 兜底：尝试字符串直接赋，失败再报类型不支持
            try:
                value = val_str  # type: ignore[assignment]
                # 试一下类型是否兼容，这里只做启发式
                # 直接 setattr，让 dataclass 的自然类型在使用阶段暴露问题
            except Exception as e:  # pragma: no cover
                raise ValueError(f"字段 {key} 类型 {tp!r} 暂不支持从命令行 override: {e}") from e

        setattr(config, key, value)
        existing_sources[key] = f"override:{raw}"

    return existing_sources


def list_profiles() -> str:
    """返回所有可用配置档的描述文本。"""
    lines = []
    for name, desc in _PROFILE_DESCRIPTIONS.items():
        cls = _CONFIG_REGISTRY[name]
        lines.append(f"  --profile {name:<14s} {desc}  ({cls.__name__})")
    return "\n".join(lines)