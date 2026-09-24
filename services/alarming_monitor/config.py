"""市场大跌预警监控系统 - 配置中心。

所有警戒阈值、ETF 篮子、指数代码、降仓位映射、ADX 趋势过滤参数统一在此定义，
main.py / indicators.py / monitor.py 不再出现硬编码数值。

通过 get_alarm_config("profile_name") 选择不同配置档：
    - default     ：标准口径（"超过3个"严格，7 信号中 ≥4 红才降仓）
    - conservative：提前预警（更严阈值，≥3 红即降仓）
    - strict      ：仅极端信号预警（更宽阈值）

继承关系：AlarmConfig 为基类；Conservative / Strict 为派生档。

ADX（平均趋向指标）作为市场状态过滤器：
    - 强趋势市（ADX > ADX_STRONG_TREND_THRESHOLD）：红灯阈值上调，需 5 个全亮才警报
    - 弱趋势/震荡市（ADX < ADX_RANGE_LOW_THRESHOLD）：红灯阈值下调，≥3 红即警报，
      且 S4/S5/S6 信号权重 ×1.5（抽血效应/机构弃守更易引发踩踏）
    - 方向不明（中间区）：维持原判，≥4 红才降仓
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class AlarmConfig:
    """大跌预警配置。

    7 个原子红灯信号（S1~S7）阈值 + ETF 篮子 + 指数代码 + 降仓位映射表。
    所有参数集中于此，便于一处调参。
    """

    # ===== S1: 成交额/总市值 占比过高 =====
    # 分母取总市值（含非流通股）
    # A 股总市值 ~100-110万亿 × 3% = 3-3.3万亿，接近历史峰值成交额 ~3万亿
    TURNOVER_RATIO_THRESHOLD: float = 0.030   # > 3.0% 亮红灯
    # 总市值兜底常量（元）。新浪指数 spot 无"总市值"列，
    # S1 用此常量作分母兜底。
    # A 股总市值 ~100-110万亿，建议每月按实际更新一次。
    TOTAL_MARKET_CAP: float = 1.05e14   # 105万亿

    # ===== S2: 放量不涨 =====
    # 口径：近5日日均成交额 > 近20日日均成交额 × VOLUME_HIGH_MULT
    #      且 指数（沪深300）近5日涨幅 ≤ INDEX_FLAT_PCT
    # 注意：日均÷日均 修正量级错误（旧版"5日总量 vs 20日日均"量级差5倍会持续误灯）
    VOLUME_HIGH_MULT: float = 1.3             # 乘数 1.3 要求量明显放大才灯
    INDEX_FLAT_PCT: float = 0.0               # 指数5日涨幅 ≤ 0 视为滞涨
    VOLUME_LOOKBACK_DAYS: int = 20            # 20日均量窗口

    # ===== S3: 涨跌家数比 从高位快速回落 =====
    # 逻辑：max(ad_ratio[-20:]) > AD_HIGH_REF 且 ad_ratio[-1] < AD_LOW_THRESHOLD
    AD_HIGH_REF: float = 2.5                  # 过去20日内日涨跌比曾超过2.5
    AD_LOW_THRESHOLD: float = 1.0            # 今日涨跌比跌破1.0

    # ===== S4: 高股息逆势走强 =====
    # 沪深300近20日涨幅 < 0（市场跌）且 高股息篮子绝对正收益
    # 且 (高股息篮子涨幅 - 成长篮子涨幅) > DIVIDEND_SPREAD_PCT
    DIVIDEND_STRONG_PCT: float = 0.0          # 高股息篮子绝对收益 > 0
    DIVIDEND_SPREAD_PCT: float = 0.03         # 高股息-成长超额 > 3%

    # ===== S5: 成长股破位 =====
    # 成长篮子近20日涨幅 < GROWTH_BREAK_PCT 或跌破近20日收盘均线
    GROWTH_BREAK_PCT: float = -0.05           # 近20日涨幅 < -5%

    # ===== 通用窗口 =====
    LOOKBACK_DAYS: int = 20                    # S2/S3/S4/S5 通用近20日

    # ===== ADX 趋势过滤器（Wilder 平均趋向指标）=====
    # ADX 仅衡量趋势强度，不关心涨跌方向。基于指数 high/low/close 计算，
    # 无需额外数据源。用于动态调整聚合引擎的 red_count 阈值。
    ADX_PERIOD: int = 14                        # ADX 计算周期（Wilder 原始周期）
    # 市场状态分界（参考经典阈值 + 用户校准值）
    ADX_TREND_THRESHOLD: float = 25.0          # > 25 为趋势市
    ADX_RANGE_THRESHOLD: float = 20.0          # < 20 为震荡市
    ADX_STRONG_TREND_THRESHOLD: float = 30.0   # > 30 为强趋势市（阈值上调）
    ADX_RANGE_LOW_THRESHOLD: float = 22.0      # < 22 为弱趋势/震荡市（阈值下调）
    # 动态 red_count 阈值（按 ADX 市场状态切换）
    # 强趋势市：情绪主导的增量市场，轻度"放量不涨"会被后续资金消化，需 ≥5 红才警报
    REDUCE_THRESHOLD_TREND: int = 5
    # 弱趋势/震荡市：存量博弈，S4/S5/S6 杀伤力极大，≥3 红即警报
    REDUCE_THRESHOLD_RANGE: int = 3
    # 方向不明（中间区）：维持原判，严格 ≥4 红才降仓
    REDUCE_THRESHOLD_NEUTRAL: int = 4
    # 震荡市 S4/S5/S6 信号权重乘数（抽血效应/机构弃守更易引发踩踏）
    RANGE_SIGNAL_WEIGHT: float = 1.5
    # 震荡市加权计数的信号 name（S4 高股息逆势 + S5 成长破位 + S6 两融杠杆）
    RANGE_WEIGHTED_SIGNALS: tuple = ('dividend_strength', 'growth_breakdown', 'margin_leverage')

    # ===== 板块代表（ETF 篮子）=====
    # 用户选"代表性个股/ETF 篮子"；ETF 抗个股噪声，代码可换为个股6位代码
    DIVIDEND_BASKET: List[str] = field(default_factory=lambda: [
        "512800",   # 银行ETF
        "516070",   # 公用事业ETF
    ])
    GROWTH_BASKET: List[str] = field(default_factory=lambda: [
        "159995",   # 芯片ETF
        "512720",   # 计算机ETF
        "562500",   # 机器人ETF华夏
        "159992",   # 创新药ETF银华
        "159819",   # 人工智能ETF易方达
        "512480",   # 半导体ETF国联安
        "159206",   # 卫星ETF永赢
        "159227",   # 航空航天ETF华夏
        "512660",   # 军工ETF国泰
    ])

    # ===== 指数代表 =====
    BREADTH_INDEX: str = "sh000300"           # 沪深300（S2 放量不涨 + S4 逆势判定）
    BREADTH_INDEX_FALLBACK: str = "sh000001"  # 上证综指（沪深300 不可取时兜底）
    # S1 两市总成交额：上证综指 + 深证成指 当日 amount 之和（全市场代表指数）
    TURNOVER_INDEX_SH: str = "sh000001"       # 上证综指（沪市全市场）
    TURNOVER_INDEX_SZ: str = "sz399001"       # 深证成指（深市全市场）

    # ===== 降仓位映射表（按 red_count 降序匹配首条）=====
    # (red_count_threshold, 仓位调整, 说明)
    # 震荡市 ADX 过滤器会把 S4/S5/S6 加权×1.5，effective_red_count 可能=3
    # 在 (4, "8→5") 与 (2, "8→7") 之间插入 (3, "8→6") 作为震荡市触发档
    POSITION_ADVICE: List[Tuple[int, str, str]] = field(default_factory=lambda: [
        (4, "8成 → 5成", "重仓降至半仓以下，7 信号中超过3个亮红灯"),
        (3, "8成 → 6成", "震荡市加权触发：S4/S5/S6 抽血效应"),
        (2, "8成 → 7成", "小幅降低或保持观察"),
    ])

    # ===== 聚合阈值 =====
    REDUCE_THRESHOLD: int = 4                 # 7信号中 ≥4 红 → 高风险（"超过3"严格口径）
    WARN_THRESHOLD: int = 2                   # ≥2 红 → 警示

    # ===== 模块三：板块轮动（RRG + 5维评分）=====
    # 基准指数：RS-Ratio 分母，所有时间序列以此对齐
    ROTATION_BENCHMARK: str = "sh000300"      # 沪深300
    # 板块篮子：复用 DIVIDEND_BASKET + GROWTH_BASKET + 防御/主题板块 + 宽基/行业ETF扩展
    ROTATION_BASKET: List[str] = field(default_factory=lambda: [
        "512800",   # 银行ETF
        "516070",   # 公用事业ETF
        "159995",   # 芯片ETF
        "512720",   # 计算机ETF
        "562500",   # 机器人ETF华夏
        "159992",   # 创新药ETF银华
        "159819",   # 人工智能ETF易方达
        "512480",   # 半导体ETF国联安
        "159206",   # 卫星ETF永赢
        "159227",   # 航空航天ETF华夏
        "512660",   # 军工ETF国泰
        "159611",   # 电力ETF广发
        "159326",   # 电网设备ETF华夏
        "159930",   # 能源ETF汇添富
        "562900",   # 农业ETF易方达
        "159985",   # 豆粕ETF华夏
        "159698",   # 粮食ETF鹏华
        "159928",   # 消费ETF汇添富
        "159883",   # 医疗器械ETF永赢
        # ---- 宽基指数ETF（510300 跟基准沪深300对比会恒为 1.0，可考虑排除）----
        "510050",   # 华夏上证50ETF（上证50，规模约1455亿，日均成交约19.6亿）
        # "510300",   # 华泰柏瑞沪深300ETF（沪深300，规模约3420亿，日均成交约38.5亿；与基准同指数）
        "510500",   # 南方中证500ETF（中证500，规模约968亿，日均成交约14.9亿）
        "159915",   # 易方达创业板ETF（创业板指，规模约844亿，日均成交约23.3亿）
        "588000",   # 华夏科创50ETF（科创50，规模约830亿，日均成交约34.8亿）
        # ---- 行业/主题ETF扩展 ----
        "512070",   # 易方达证券保险ETF（重仓中国平安/中信证券/东方财富，规模约145亿）
        "512880",   # 国泰中证全指证券公司ETF（券商ETF中规模最大，规模约308亿）
        "512690",   # 鹏华酒ETF（消费类ETF中流动性最强，规模约129亿）
        "159996",   # 国泰家电ETF（家电板块中流动性最好，近一周成交约9.3亿）
        "512400",   # 南方有色金属ETF（成交活跃，半日成交额可超10亿）
        "515220",   # 国泰煤炭ETF（煤炭板块中规模最大、流动性最好）
        "159870",   # 鹏华化工ETF（成交活跃，半日成交额可超10亿）
        "159869",   # 华夏游戏ETF（游戏板块中规模最大、成交最活跃）
        "515050",   # 华夏通信ETF（通信硬件端龙头，规模约183亿）
        "513180",   # 华夏恒生科技ETF（港股科技板块核心标的，规模大流动性好）
        "513120",   # 广发港股创新药ETF（规模约255亿，同类中流动性居前）
    ])
    # 板块中文名映射（Markdown 报告显示用）
    ROTATION_LABELS: dict = field(default_factory=lambda: {
        "512800": "银行ETF",
        "516070": "公用事业ETF",
        "159995": "芯片ETF",
        "512720": "计算机ETF",
        "562500": "机器人ETF",
        "159992": "创新药ETF",
        "159819": "人工智能ETF",
        "512480": "半导体ETF",
        "159206": "卫星ETF",
        "159227": "航空航天ETF",
        "512660": "军工ETF",
        "159611": "电力ETF",
        "159326": "电网设备ETF",
        "159930": "能源ETF",
        "562900": "农业ETF",
        "159985": "豆粕ETF",
        "159698": "粮食ETF",
        "159928": "消费ETF",
        "159883": "医疗器械ETF",
        # ---- 宽基指数ETF ----
        "510050": "上证50ETF",
        "510300": "沪深300ETF",
        "510500": "中证500ETF",
        "159915": "创业板ETF",
        "588000": "科创50ETF",
        # ---- 行业/主题ETF扩展 ----
        "512070": "证券保险ETF",
        "512880": "券商ETF",
        "512690": "酒ETF",
        "159996": "家电ETF",
        "512400": "有色金属ETF",
        "515220": "煤炭ETF",
        "159870": "化工ETF",
        "159869": "游戏ETF",
        "515050": "通信ETF",
        "513180": "恒生科技ETF",
        "513120": "港股创新药ETF",
    })

    # ---- RRG 象限参数 ----
    RS_RATIO_PERIOD: int = 20                  # RS-Ratio 平滑周期（WMA）
    RS_MOMENTUM_PERIOD: int = 10               # RS-Momentum 变化率周期
    RRG_RATIO_STRONG: float = 1.0             # RS-Ratio > 1.0 为相对强
    RRG_MOMENTUM_STRONG: float = 0.0           # RS-Momentum > 0 为正动量
    # 边界归类：RS-Ratio=1.0 或 RS-Momentum=0 时归入保守侧（弱势）
    RRG_BOUNDARY_CONSERVATIVE: bool = True

    # ---- 5维评分权重（合计 1.00）----
    SCORE_WEIGHTS: dict = field(default_factory=lambda: {
        "relative_momentum":     0.25,   # 相对动量（RS-Ratio - 1）
        "momentum_acceleration": 0.20,   # 动量加速度（RS-Momentum 二阶差分）
        "adx_trend":             0.20,   # ADX 趋势强度
        "capital_attention":     0.20,   # 资金关注度（量能相对强度：MA5额/MA20额）
        "crowding_penalty":      0.15,   # 拥挤度折扣（量能过度≥2.5倍扣2分）
    })
    # 5维评分归一化区间（每维原始分映射到 [0, 5]）
    # 维1 相对动量改用分段映射（A2）：实战 RS-Ratio 多在 0.9~1.15，
    # 线性映射到 1.5 满分会把"领涨"压成低分；分段：1.03→1分/1.10→3分/1.20→5分
    # 跑输基准(RS<1.0)→0分；超过最大断点→满分5.0
    SCORE_REL_MOM_BREAKPOINTS: List[Tuple[float, float]] = field(default_factory=lambda: [
        (0.03, 1.0),   # RS-Ratio=1.03 → 1分（轻微跑赢）
        (0.10, 3.0),   # RS-Ratio=1.10 → 3分（显著跑赢）
        (0.20, 5.0),   # RS-Ratio=1.20 → 5分（满分，强势主线）
    ])
    # 废弃兼容字段：维1 旧线性映射的满分点（RS-Ratio 超基准 0.5 即满分）。
    # 维1 已改用 SCORE_REL_MOM_BREAKPOINTS 分段映射，此字段仅供旧测试引用兼容，
    # 不再参与 calc_5d_score 计算。
    SCORE_REL_MOM_FULL: float = 0.5
    SCORE_ADX_FULL: float = 30.0             # [已废弃] 旧线性映射满分点，仅供旧测试引用兼容
    # 维3 ADX 强度分段映射（更符合 ADX 实战含义）：
    #   ADX ≤ 15 → 0 分（无趋势）
    #   15 < ADX ≤ 20 → 0~1 分（弱趋势启动）
    #   20 < ADX ≤ 25 → 1~3 分（趋势形成）
    #   25 < ADX ≤ 30 → 3~5 分（强趋势）
    #   ADX > 30 → 5 分（满分封顶）
    # 段间线性插值。配置为 (adx阈值, 该阈值对应分数) 列表，
    # 低于首个阈值 → 0 分；高于末个阈值 → 末个分数（封顶）。
    SCORE_ADX_BREAKPOINTS: List[Tuple[float, float]] = field(default_factory=lambda: [
        (15.0, 0.0),   # ADX=15 → 0分（无趋势）
        (20.0, 1.0),   # ADX=20 → 1分（弱趋势）
        (25.0, 3.0),   # ADX=25 → 3分（趋势形成）
        (30.0, 5.0),   # ADX=30 → 5分（强趋势，满分）
    ])
    # 维3 方向降权：ADX 只代表趋势强度，不辨方向；强下跌也拿高分不合实战。
    # +DI > -DI（上涨趋势）→ 不降权；-DI > +DI（下跌趋势）→ 乘此系数降权。
    # 默认 0.1：下跌强趋势最多拿 0.5 分（ADX≥30 × 5 × 0.1 = 0.5），避免虚高。
    ADX_DOWN_PENALTY: float = 0.1
    # 维2 动量加速度：横截面 z-score 标准化（解决"5% 绝对阈值对不同资产含义不同"问题）
    # 先算每只 ETF 的 RS-Momentum 二阶差分原始值，再在横截面（全篮子）做 z-score，
    # 最后映射到 [0, 5]：score = clip(center + z * slope, 0, 5)
    #   z = 0  → center 分（加速度处于横截面均值）
    #   z = +1 → center + slope 分（加速度比均值高1个标准差）
    #   z = -1 → center - slope 分（加速度比均值低1个标准差）
    # 默认 center=2.5, slope=2.5 → z=+1 满分5, z=-1 零分0
    SCORE_MOM_ACC_ZSCORE_CENTER: float = 2.5
    SCORE_MOM_ACC_ZSCORE_SLOPE: float = 2.5
    # 旧字段保留兼容（不再用于维2 计算的满分点，仅供旧测试引用）
    SCORE_MOM_ACC_FULL: float = 5.0

    # ===== 趋势/RS 健康度（拐点预警辅助，不进总分，仅作"预警改善"提示）=====
    # 目标：把 ADX、RS 从"死值"变成"动态信息"，减少拐点误判。
    TREND_HEALTH_ADX_SLOPE_PERIOD: int = 5        # ADX 斜率回看天数（ADX - ADX.shift(N)）
    RS_MOM_ACCEL_PERIOD: int = 5                  # RS 动量加速度回看天数（RS_Mom - RS_Mom.shift(N)）
    RS_MULTI_PERIODS: List[int] = field(default_factory=lambda: [5, 20, 60])  # 多周期相对强度
    VOLUME_RATIO_PERIOD: int = 20                 # 成交量比均线周期（Volume / MA_N(Volume)）
    # 预警改善信号触发条件（三者同时满足）：
    #   ① ADX 斜率 > TREND_HEALTH_ADX_SLOPE_THRESHOLD（趋势强度增强）
    #   ② +DI 上穿 -DI（di_cross_up=True）
    #   ③ RS 动量加速度 > RS_MOM_ACCEL_THRESHOLD（相对强度加速）
    TREND_HEALTH_ADX_SLOPE_THRESHOLD: float = 0.0
    RS_MOM_ACCEL_THRESHOLD: float = 0.0
    # 维4 资金关注度：成交额历史分位（今日成交额在过去N日的百分位）
    # 腾讯源 ETF 无基金份额数据，真实换手率不可得；amount/(close*volume) 是单位换算
    # 常数无区分度。改用成交额历史分位作为资金关注度代理（相对自身历史）：
    #   分位 < 20% → 低关注度（缩量，1分）
    #   分位 = 50% → 正常（3分）
    #   分位 > 80% → 高关注度（放量，5分）
    #   分位 > 95% → 极度放量（5分封顶，可能见顶）
    SCORE_CAP_ATTENTION_LOOKBACK: int = 60    # 历史分位回看天数
    SCORE_CAP_ATTENTION_LOW_PCT: float = 20.0  # 分位<此值 → 低关注度
    SCORE_CAP_ATTENTION_HIGH_PCT: float = 80.0  # 分位>此值 → 高关注度
    # 旧字段保留兼容（不再用于维4 计算，仅供旧测试引用）
    SCORE_CAP_ATTENTION_FULL: float = 2.0
    # 维5 拥挤度折扣：基础5分，任一条件命中扣分
    #   条件1：价格偏离20日均线 > CROWDING_PRICE_DEVIATION_ATR_MULT 倍 ATR（过度偏离均值，回调风险）
    #   条件2：成交额分位 > CROWDING_AMOUNT_PCT_HIGH（极度放量，可能短期见顶）
    CROWDING_PRICE_DEVIATION_ATR_MULT: float = 2.0  # 价格偏离20日均线>2倍ATR→扣分
    CROWDING_AMOUNT_PCT_HIGH: float = 90.0           # 成交额分位>90%→扣分
    CROWDING_PENALTY: float = 2.0             # 拥挤度过高扣除分数
    # 旧字段保留兼容（不再用于维5 计算）
    CROWDING_TURNOVER_THRESHOLD: float = 2.5
    # 超卖反弹观察系统（不进总分，仅作操作参考的机会提示/降级保护）
    # 一级（观察）：close < MA20 且 MA20 - close > OVERSOLD_ATR_MULT × ATR
    # 二级（候选）：一级 + 任一（RS-Momentum>0 / 象限改善 / 分位从<20%回升到>50%）
    # 三级（确认）：二级 + 任一（RS-Ratio回升+RS-Momentum>0连续2日 / +DI上穿-DI / 分位>60%且站回MA5）
    OVERSOLD_ATR_MULT: float = 2.0              # 超卖偏离阈值（MA20-close > N×ATR）
    OVERSOLD_AMOUNT_LOW_PCT: float = 20.0      # 缩量分位（二级条件之一）
    OVERSOLD_AMOUNT_HIGH_PCT: float = 50.0     # 回升分位（二级条件之一）
    REBOUND_AMOUNT_CONFIRM_PCT: float = 60.0   # 确认级量能分位
    REBOUND_LOOKBACK: int = 5                  # 反弹判断历史回看天数
    # 超卖信号时间约束（方案 B：无状态重算，可回测、可复现）
    # 超卖触发后 N 日内未出现 RS-Momentum > 0 → 自动失效，level 置"无"
    # 过期后若再次 RS-Momentum > 0 → 重新从观察/候选开始（计时器归零）
    # 窗口起点已超卖时保守处理（truncated=True），不自动过期
    OVERSOLD_VALID_DAYS: int = 5                # 超卖信号有效期（日）

    # ===== 领先层（拐点提前嗅探，不进总分、不改操作矩阵，仅用于观察名单）=====
    # 目标：提前嗅到可能的拐点。三类信号：
    #   ① 波动率压缩：ATR_pct / BBW 处于过去一年低分位 → 变盘前夜
    #   ② 量价背离：价格创新低但 OBV 不创新低（底部背离）/ 价格创新高但 OBV 不创新高（顶部背离）
    #   ③ RS 动量背离：价格新低但 RS-Momentum 底部抬高
    # 输出 lead_score: 0~100，每类信号 0~33 分，只用于观察名单，不直接交易。
    LEAD_LOOKBACK_DAYS: int = 252               # 历史分位回看窗口（约一年交易日）
    LEAD_VOLATILITY_LOW_PCT: float = 20.0       # 波动率低分位阈值（<此值算压缩）
    LEAD_DIVERGENCE_WINDOW: int = 20            # 背离对比窗口（创新高/新低的回看天数）
    LEAD_BOLLINGER_PERIOD: int = 20             # 布林带周期
    LEAD_BOLLINGER_STD: float = 2.0             # 布林带标准差倍数

    # ===== 同步层（拐点确认，过滤假反弹）=====
    # 目标：领先信号出现后，等市场确认再行动。5 个确认指标各 0~20 分，合计 0~100。
    #   ① MA20/MA60：价格站上 MA20 且 MA20 斜率转正
    #   ② VWAP：价格站上 VWAP（日线累积）
    #   ③ 成交量：今日量 > 20 日均量 × CONFIRM_VOLUME_MULT
    #   ④ 价格结构：高低点抬高（N 字突破）
    #   ⑤ 板块宽度：ETF 自身近 N 日上涨天数占比 > CONFIRM_BREADTH_THRESHOLD
    #     （注：原版要求成分股数据，此处用 ETF 自身涨跌天数近似）
    # 规则：lead_score 高 + confirm_score 高 → 拐点初步确认；
    #       lead_score 高 + confirm_score 低 → 只观察不追。
    CONFIRM_MA_SHORT: int = 20                 # 短期均线周期
    CONFIRM_MA_LONG: int = 60                  # 长期均线周期
    CONFIRM_MA_SLOPE_PERIOD: int = 5           # MA 斜率回看天数（MA_today - MA_N天前）
    CONFIRM_VWAP_PERIOD: int = 20              # VWAP 累积周期（日线）
    CONFIRM_VOLUME_MA: int = 20                # 成交量均线周期
    CONFIRM_VOLUME_MULT: float = 1.5           # 放量倍数阈值
    CONFIRM_PRICE_STRUCTURE_WINDOW: int = 10    # 价格结构回看窗口（高低点抬高）
    CONFIRM_BREADTH_WINDOW: int = 20           # 板块宽度回看窗口
    CONFIRM_BREADTH_THRESHOLD: float = 0.6     # 板块宽度阈值（上涨天数占比）

    # ===== 持有层（趋势延续判断，加仓/减仓/移动止损）=====
    # 目标：拐点确认后，拿得住趋势。只用于加仓/减仓/移动止损，不用于预测拐点。
    # 3 类长周期指标（合计 0~100）：
    #   ① 长周期 ADX（34 分）：ADX(56) > HOLD_ADX_THRESHOLD 给 17 分；ADX 斜率向上给 17 分
    #   ② 长周期 RS-Ratio（33 分）：RS(60) > HOLD_RS_STRONG 给 33 分
    #   ③ 长周期均线（33 分）：站上 MA60 给 16 分；站上 MA120 给 17 分
    # 规则：
    #   hold_score >= HOLD_SCORE_HIGH       → 持有
    #   HOLD_SCORE_MED <= hold_score < HIGH  → 减仓（趋势减弱）
    #   hold_score < HOLD_SCORE_MED         → 减仓加速
    #   跌破 ATR 止损（close < MA20 - HOLD_ATR_STOP_MULT × ATR(14)）→ 离场（最优先）
    HOLD_ADX_LONG_PERIOD: int = 56            # 长周期 ADX 周期（季线级趋势）
    HOLD_ADX_THRESHOLD: float = 20.0          # ADX > 20 视为长周期有趋势
    HOLD_ADX_SLOPE_PERIOD: int = 5            # ADX 斜率回看天数
    HOLD_RS_LONG_PERIOD: int = 60             # 长周期 RS-Ratio 平滑窗口
    HOLD_RS_STRONG: float = 1.0               # RS(60) > 1 视为长周期跑赢基准
    HOLD_MA_LONG: int = 60                    # 长周期均线 1（季线）
    HOLD_MA_LONGER: int = 120                  # 长周期均线 2（半年线）
    HOLD_ATR_STOP_PERIOD: int = 14            # ATR 止损计算周期
    HOLD_ATR_STOP_MULT: float = 2.0          # ATR 止损倍数（close < MA20 - 2×ATR → 离场）
    HOLD_SCORE_HIGH: int = 70                 # 持有阈值（≥70 持有）
    HOLD_SCORE_MED: int = 40                  # 减仓阈值（<70 减仓，<40 加速减仓）

    # 5维评分阈值（>4 强推荐，>3 关注，<=3 中性/回避）—— 仅在象限标签缺失时作兜底
    SCORE_STRONG_THRESHOLD: float = 4.0
    SCORE_ATTENTION_THRESHOLD: float = 3.0

    # ===== 大盘均线过滤（B5）=====
    # 基准指数收盘价 vs MA20/MA60，判定全局格局：
    #   above  : close > MA20 且 close > MA60 → 多头格局（系统性机会，领涨主线可加仓）
    #   below  : close < MA20 且 close < MA60 → 空头格局（系统性下跌，全面防守）
    #   mixed  : 其他（震荡/过渡，领涨主线持有但不加仓）
    BENCH_MA_SHORT: int = 20
    BENCH_MA_LONG: int = 60

    # ===== 波动率风险修正（B6）=====
    # ATR(14)/close × 100（百分比），不进入加权总分，仅作为操作参考的风险刹车：
    #   高波动 + 领涨 → 不加仓（持有而非加仓，风险收益比恶化）
    #   高波动 + 退潮 → 减仓加速
    #   低波动 + 领涨 → 可持有/加仓（健康趋势）
    VOLATILITY_PERIOD: int = 14               # ATR 周期（与 ADX 一致，便于复用 talib）
    VOLATILITY_HIGH_THRESHOLD: float = 3.0    # ATR/close > 3.0% → 高波动
    VOLATILITY_LOW_THRESHOLD: float = 1.5     # ATR/close < 1.5% → 低波动

    # ===== talib 加速 =====
    # True 优先用 talib 计算技术指标（ADX/WMA/MA），不可用时降级 numpy 自实现
    USE_TALIB: bool = True

    # ===== Local cache =====
    CACHE_DIR: str = "data/cache"              # cache root
    CACHE_TTL_HOURS: int = 20                  # TTL of the day cache (hour)
    CACHE_HISTORY_SPLIT_DAYS: int = 30         # History layer / day layer split (>30 days before permanent cache)

    # ===== Markdown report output =====
    REPORT_DIR: str = "data/reports/rotation"  # Markdown report output directory

    # ===== Section 7: 连板龙头 & 大盘温度 =====
    ZZSHARE_TOKEN: Optional[str] = None        # 未配时走匿名（30次/分钟）
    LINKBAN_FILTER_ST: bool = True             # 是否过滤 ST/*ST 股（连板梯队里剔除）
    LINKBAN_MIN_BOARD_FOR_TIER: int = 2        # 最小板数计入梯队（≥2板即连板梯队）

    # 温度评分：最高板 0-40 分（按板数降序，第一个 value >= key 命中对应分）
    TEMP_MAX_BOARD_SCORE: Dict[int, int] = field(default_factory=lambda: {
        7: 40,  # ≥7板 40分
        5: 30,  # ≥5板 30分
        3: 20,  # ≥3板 20分
        0: 10,  # <3板 10分（兜底）
    })
    # 温度评分：连板总数 0-30 分
    TEMP_TOTAL_SCORE: Dict[int, int] = field(default_factory=lambda: {
        20: 30,  # ≥20只 30分
        10: 20,  # ≥10只 20分
        0:  10,  # <10只 10分
    })
    # 温度评分：晋级率 0-30 分
    TEMP_JINJI_SCORE: Dict[float, int] = field(default_factory=lambda: {
        0.5: 30,  # ≥50% 30分
        0.3: 20,  # ≥30% 20分
        0.0: 10,  # <30% 10分
    })
    # 温度等级映射（综合分 → 等级文案+操作建议）
    #   key: 最低综合分阈值（按降序首条匹配）
    #   value: (等级文案不含后缀, 操作建议文案)
    #   level 最终格式由 reporter 加上括号中文后缀: "🔥 高温（市场极度活跃）"
    TEMP_LEVEL_MAP: Dict[int, Tuple[str, str]] = field(default_factory=lambda: {
        70: ("🔥 高温", "市场极度活跃"),
        50: ("🌤️ 常温", "市场正常"),
        0:  ("❄️ 低温", "市场低迷"),
    })
    # 温度等级后缀：建议文案前缀（与上面的后缀配合，和用户需求一致）
    TEMP_ADVICE_BY_LEVEL: Dict[str, str] = field(default_factory=lambda: {
        "🔥 高温": "可积极参与主线题材，注意高位分化风险",
        "🌤️ 常温": "市场正常，精选个股操作",
        "❄️ 低温": "市场低迷，观望为主或控制仓位",
    })

    # 连板 CSV 历史目录（与 alarming_*.csv 同目录）
    LINKBAN_CSV_DIR: str = "data/alarming_signals"

    # ===== Section 8: S6 两融杠杆异常 =====
    # 数据获取：akshare stock_margin_sse(区间汇总) + stock_margin_szse(日快照汇总 fallback 明细汇总)
    # SSE 单位是元；SZSE 日快照单位是亿 → 在 data_loader 内统一转成元
    MARGIN_LOOKBACK_DAYS: int = 25              # 历史拉取窗口（>20 保证 20 日增速可用）
    MARGIN_USE_HISTORY_CSV: bool = True         # 是否优先拼接 CSV 自累积的近 20 日（仅缺最新日时补拉）

    # --- 过热阈值（任一命中 → 过热分支亮） ---
    MARGIN_GROWTH_5D: float = 0.030             # 融资余额 5 日增速 > 3%（快速加杠杆）
    MARGIN_GROWTH_20D: float = 0.080            # 融资余额 20 日增速 > 8%
    MARGIN_BUY_RATIO: float = 0.12              # 融资买入额/两市成交额 > 12%（杠杆交易过热）
    MARGIN_NET_LEVERAGE: float = 0.018          # (融资余额-融券余额)/总市值 > 1.8%（净多头杠杆过高）

    # --- 去杠杆阈值（任一命中 → 去杠杆分支亮） ---
    MARGIN_DELEV_5D: float = -0.020             # 融资余额 5 日回撤 < -2%（被动去杠杆）
    MARGIN_DELEV_10D: float = -0.035            # 融资余额 10 日回撤 < -3.5%

    # --- 红灯逻辑：过热 OR 去杠杆 → S6 亮红 ---
    MARGIN_RED_MODE: str = "OR"                 # "OR"（任一方向亮）或 "BOTH"（仅双向共振）
    # 两融 CSV 自累积目录（与 breadth 同目录，按日期重建 20+ 日序列）
    MARGIN_CSV_DIR: str = "data/alarming_signals"

    # ===== Section 9: S7 大盘 KDJ 高位死叉 =====
    # 基准指数（独立于 BREADTH_INDEX，默认仍为沪深300，可改为科创综指等）
    #   常见参考: sh000300=沪深300 / sh000001=上证综指 / sz399001=深成指
    #             sh000688=科创50  / sz399006=创业板指 / sh000016=上证50 / sh000905=中证500
    KDJ_INDEX: str = "sh000300"                # KDJ 判定基准指数（默认沪深300）
    KDJ_INDEX_FALLBACK: str = "sh000001"       # KDJ 指数不可取时兜底（上证综指）
    # KDJ 计算说明：日线 → resample 周线/月线 → 国内版 KDJ 9,3,3
    #   国内 KDJ 平滑：SMA(m, n) = (前值*(n-1) + 当日值) / n，初始 K=D=50
    #   若 KDJ_INDEX == BREADTH_INDEX，则复用 data['index']，不重复触网；
    #   若不同（如 sh000688 科创50），main._gather_data 会单独拉一份 260 日窗口
    KDJ_RSV_PERIOD: int = 9              # RSV 周期（n 周期内最高最低）
    KDJ_K_SMOOTH: int = 3                # K 平滑周期
    KDJ_D_SMOOTH: int = 3                # D 平移周期
    KDJ_OVERBOUGHT: float = 80.0         # 高位阈值：死叉时 K > 80 视为高位死叉
    KDJ_OVERSOLD: float = 20.0           # 低位阈值（与 OVERBOUGHT 对称）
    KDJ_EXTREME_OVERSOLD_K: float = 15.0  # "极度超跌"档位的周K阈值（K<15 或 J<0）
    KDJ_DEATH_CROSS_LOOKBACK: int = 3    # 死叉识别窗口（最近 N 个周期内发生过死叉即算）
    KDJ_LOOKBACK_DAYS: int = 260         # 日线拉取窗口（≥12 个月线 / 52 周线）
    # 启用周期（True 即参与判定）
    KDJ_USE_WEEKLY: bool = True          # 周线 KDJ
    KDJ_USE_MONTHLY: bool = True         # 月线 KDJ
    # 红灯合并：OR（任一周期高位死叉即红）/ AND（双周期共振才红）
    KDJ_RED_MODE: str = "OR"

    # 通用数值哨兵
    INF_SENTINEL: float = 1e9            # math.isinf 时的占位值（防止 round 溢出）


@dataclass
class ConservativeAlarmConfig(AlarmConfig):
    """保守配置 - 提前预警：更严阈值，≥3 红即降仓。"""

    TURNOVER_RATIO_THRESHOLD: float = 0.025   # 2.5% 即灯
    VOLUME_HIGH_MULT: float = 1.5              # 更严的放量门槛
    GROWTH_BREAK_PCT: float = -0.03            # -3% 即破位
    REDUCE_THRESHOLD: int = 3                  # ≥3 红即降仓（"3个及以上"宽松口径）
    POSITION_ADVICE: List[Tuple[int, str, str]] = field(default_factory=lambda: [
        (3, "8成 → 5成", "保守口径：3 信号亮红灯即降仓"),
        (2, "8成 → 6成", "保守口径：2 信号亮红灯小幅降仓"),
    ])


@dataclass
class StrictAlarmConfig(AlarmConfig):
    """严格配置 - 仅极端信号预警：更宽阈值，仅 ≥4 红才降仓。"""

    TURNOVER_RATIO_THRESHOLD: float = 0.035   # 3.5% 才灯
    VOLUME_HIGH_MULT: float = 1.2            # 放宽放量门槛
    DIVIDEND_SPREAD_PCT: float = 0.05        # 超额需 >5%
    GROWTH_BREAK_PCT: float = -0.08           # -8% 才算破位


@dataclass
class KechuangAlarmConfig(AlarmConfig):
    """科创综指配置 - 以科创板（sh000688 科创50 或 sh000006 科创综指）为主指数，
    适用场景：单独监控科创板的量价/杠杆/中长周期形态风险。"""

    # 主指数（S2 放量不涨 + ADX 趋势过滤 + S4 逆势判定共用）——默认科创50
    #   sh000688  = 科创50       （行业公认核心科创宽基，akshare 数据源最稳定）
    #   sh000006  = 科创综指      （全部科创板股票综合，波动更宽）
    BREADTH_INDEX: str = "sh000688"
    BREADTH_INDEX_FALLBACK: str = "sh000001"  # 不可取时兜底上证综指

    # KDJ 独立指数（默认与主指数一致，使用"科创综指 sh000006"的用户可单独改此字段）
    #   若与 BREADTH_INDEX 相同，main._gather_data 直接复用日线，不重复拉取
    KDJ_INDEX: str = "sh000688"
    KDJ_INDEX_FALLBACK: str = "sh000001"

    # 宽基降仓阈值：继承 default（≥4红），整体与"标准档"保持一致
    REDUCE_THRESHOLD: int = 4


# ── 配置注册表 ──

_CONFIG_REGISTRY: dict = {
    "default": AlarmConfig,
    "conservative": ConservativeAlarmConfig,
    "strict": StrictAlarmConfig,
    "kc": KechuangAlarmConfig,
    "kechuang": KechuangAlarmConfig,
}

_PROFILE_DESCRIPTIONS: dict = {
    "default": "标准配置 - 7信号中≥4红才降仓（成交额/总市值3% / 放量×1.3 / 涨跌比2.5→1 / 逆势超额3% / 成长-5%）",
    "conservative": "保守配置 - 提前预警（阈值更严，≥3红即降仓）",
    "strict": "严格配置 - 仅极端信号预警（阈值更宽，≥4红才降仓）",
    "kc": "科创综指配置 - 以 sh000688 科创50 为主指数（同时作用于 S2/ADX/S4/S7 KDJ，BREADTH_INDEX=KDJ_INDEX=sh000688，降仓≥4红）",
    "kechuang": "科创综指配置（= --profile kc，长别名）",
}


def get_alarm_config(profile: str = "default") -> AlarmConfig:
    """按名称获取配置实例。"""
    cls = _CONFIG_REGISTRY.get(profile)
    if cls is None:
        available = ", ".join(_CONFIG_REGISTRY.keys())
        raise ValueError(f"未知配置档 '{profile}'，可用: {available}")
    return cls()


def list_profiles() -> str:
    """返回所有可用配置档的描述文本。"""
    lines = []
    for name, desc in _PROFILE_DESCRIPTIONS.items():
        cls = _CONFIG_REGISTRY[name]
        lines.append(f"  --profile {name:<14s} {desc}  ({cls.__name__})")
    return "\n".join(lines)
