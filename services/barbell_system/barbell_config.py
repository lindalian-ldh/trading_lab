# -*- coding: utf-8 -*-
"""哑铃策略核心参数与资产池。所有可调参数集中于此。

支持两种模式：
    MODE = "style"  : 风格哑铃（固定资产池：红利 + 成长 ETF）
    MODE = "sector" : 板块哑铃（动态板块筛选 + 板块 ETF 映射）

参数覆盖机制：main.py 启动时若 MODE == "sector"，将 SECTOR_PARAMS
字段写回顶层变量（SPREAD_FLOOR / REBALANCE_THRESHOLD 等），
signals/weights/rebalance 模块继续读 cfg.* 顶层变量，无感知切换。
"""

import os

# ===== 模式切换 =====
# 优先环境变量 BARBELL_MODE，便于命令行临时切换：BARBELL_MODE=sector ...
MODE = os.getenv("BARBELL_MODE", "style").strip().lower()
if MODE not in ("style", "sector"):
    MODE = "style"

# ====================================================================
# 风格哑铃参数（MODE="style" 使用；与升级前完全一致）
# ====================================================================
STYLE_PARAMS = {
    "base_weight": 0.50,
    "offensive_base_weight": 0.50,   # 进攻端标准配置权重（style=50/50）
    "rebalance_threshold": 0.10,
    "correlation_alert": 0.50,
    "spread_floor": 1.5,
    "spread_high": 2.5,
    "corr_low": 0.2,
    "zscore_lookback": 120,
    "zscore_window": 20,
    "zscore_low": 1.5,
    "zscore_high": 2.5,
    "corr_window": 60,
    "run_frequency": 14,
    "failure_corr": 0.5,
    "failure_spread": 1.5,
    "failure_down_days": 5,
    "failure_required": 2,
    "failure_reduce_pp": 0.20,
    "failure_wait_days": 10,
}

# ====================================================================
# 板块哑铃参数（MODE="sector" 时由 main.py 覆盖到顶层变量）
# ====================================================================
SECTOR_PARAMS = {
    # ---- 与 signals/weights/rebalance 同名字段（覆盖目标）----
    "base_weight": 0.60,            # 防御端 60%
    "offensive_base_weight": 0.40,  # 进攻端 40%（防御/进攻 60/40）
    "rebalance_threshold": 0.08,    # 8 个百分点（周度更敏感）
    "correlation_alert": 0.65,      # 板块间相关性天然更高
    "spread_floor": 2.0,            # 股债利差下限 2.0%
    "spread_high": 3.0,             # 利差上限（得分 +1）
    "corr_low": 0.2,
    "zscore_lookback": 120,
    "zscore_window": 20,
    "zscore_low": 1.5,
    "zscore_high": 2.0,             # 拥挤度上限更严
    "corr_window": 60,
    "run_frequency": 7,             # 每周检查
    "failure_corr": 0.65,
    "failure_spread": 2.0,
    "failure_down_days": 3,         # 连续 3 日下跌（更敏感）
    "failure_required": 2,
    "failure_reduce_pp": 0.20,
    "failure_wait_days": 10,

    # ---- 板块筛选阈值（screener.py 专用）----
    "defensive_div_yield_min": 3.0,            # 股息率 TTM > 3%
    "defensive_vol_lookback": 60,              # 60 日波动率窗口
    "defensive_vol_percentile_max": 30,        # 波动率分位 < 30%
    "defensive_val_pct_max": 50,               # 估值分位 < 50%
    "offensive_momentum_lookback": 250,        # 12 月动量 ≈ 250 交易日
    "offensive_momentum_top_pct": 20,          # 动量排名前 20%
    "offensive_turnover_up_days": 10,          # 成交额近 N 日上升
    "top_n_sectors": 3,                         # 每端取前 3
    "exclude_pct_low": 40,                      # 中间地带排除下限
    "exclude_pct_high": 60,                    # 中间地带排除上限

    # ---- ETF 流动性过滤（sector_etf_map.py 专用）----
    "etf_amount_min": 5000,                    # 日均成交额 > 5000 万
    "etf_size_min": 5,                         # 基金规模 > 5 亿
    "etf_track_err_max": 2.0,                  # 跟踪误差 < 2%

    # ---- 政策事件（手工输入；MVP 不接 API）----
    "policy_flag_offensive": [],               # e.g. ["半导体:美国出口管制升级"]

    # ---- 手动配置入口（非空时跳过 screener 动态筛选，直接使用这两只 ETF）----
    # 格式：{"sector": "板块名", "code": "6位ETF代码", "name": "ETF名称"}
    # 留 None 走动态筛选；填了字典就钉死两端，最稳定。
    # 示例（取消注释即可启用）：
    # "manual_defensive_etf": {"sector": "银行",     "code": "512800", "name": "银行 ETF"},
    # "manual_offensive_etf": {"sector": "半导体",   "code": "512480", "name": "半导体 ETF"},
    "manual_defensive_etf": None,
    "manual_offensive_etf": None,
}

# ====================================================================
# 顶层变量（style 默认值；sector 模式由 main.py 覆盖）
# 与升级前完全一致，保持 style 模式零回归
# ====================================================================

# ===== 基础权重 =====
BASE_WEIGHT = STYLE_PARAMS["base_weight"]               # 防御端标准配置权重
OFFENSIVE_BASE_WEIGHT = STYLE_PARAMS["offensive_base_weight"]  # 进攻端标准配置权重

# ===== 再平衡 =====
REBALANCE_THRESHOLD = STYLE_PARAMS["rebalance_threshold"]
RUN_FREQUENCY = STYLE_PARAMS["run_frequency"]

# ===== 信号阈值 =====
CORRELATION_ALERT = STYLE_PARAMS["correlation_alert"]
SPREAD_FLOOR = STYLE_PARAMS["spread_floor"]
SPREAD_HIGH = STYLE_PARAMS["spread_high"]
CORR_LOW = STYLE_PARAMS["corr_low"]
ZSCORE_LOOKBACK = STYLE_PARAMS["zscore_lookback"]
ZSCORE_WINDOW = STYLE_PARAMS["zscore_window"]
ZSCORE_LOW = STYLE_PARAMS["zscore_low"]
ZSCORE_HIGH = STYLE_PARAMS["zscore_high"]
CORR_WINDOW = STYLE_PARAMS["corr_window"]

# ===== 失效预警 =====
FAILURE_CORR_THRESHOLD = STYLE_PARAMS["failure_corr"]
FAILURE_SPREAD_THRESHOLD = STYLE_PARAMS["failure_spread"]
FAILURE_DOWN_DAYS = STYLE_PARAMS["failure_down_days"]
FAILURE_CONDITIONS_REQUIRED = STYLE_PARAMS["failure_required"]
FAILURE_REDUCE_PP = STYLE_PARAMS["failure_reduce_pp"]
FAILURE_WAIT_DAYS = STYLE_PARAMS["failure_wait_days"]

# ===== 降级常量 =====
DIVIDEND_YIELD_FALLBACK = 4.5     # 股息率不可用时的固定近似值（%）
DATA_FETCH_DAYS = 400             # 拉取交易日数量（满足 120+60 窗口留余量）

# ===== 板块 ETF 日线数据源（sector 模式 screener / 流动性过滤用）=====
# 背景：efinance 走 eastmoney 的 /api/qt/stock/kline/get，该接口在部分网络/本机代理
# 环境下会被服务端直接断连（RemoteDisconnected → ProxyError），且 urllib3 会自动重试 5 次，
# 导致 31 个板块逐个失败、日志刷屏、运行卡住；而 baostock 同一批 ETF 数据完全可用。
# 因此默认 baostock 优先，efinance 作为备源。
# 覆盖方式：BARBELL_ETF_SOURCE=efinance,baostock
SECTOR_ETF_SOURCE_ORDER = [
    s.strip().lower()
    for s in os.getenv("BARBELL_ETF_SOURCE", "baostock,efinance").split(",")
    if s.strip()
]

# 板块 ETF 日线当日本地缓存（同一交易日重复运行直接读缓存，避免反复打接口）
ETF_KLINE_CACHE = os.getenv("BARBELL_ETF_CACHE", "1").strip().lower() not in ("0", "false", "no")

# ===== 资产池（style 模式：固定资产池）=====
DEFENSIVE_POOL = [
    {"code": "sh.515080", "name": "中证红利 ETF", "baostock_code": "sh.515080",
     "efinance_code": "515080"},
    {"code": "sz.159549", "name": "红利低波 ETF", "baostock_code": "sz.159549",
     "efinance_code": "159549"},
]

OFFENSIVE_POOL = [
    {"code": "sz.159552", "name": "中证 2000 增强 ETF", "baostock_code": "sz.159552",
     "efinance_code": "159552"},
    {"code": "sz.159259", "name": "成长 ETF", "baostock_code": "sz.159259",
     "efinance_code": "159259"},
]

# 当前选用的两端资产（默认取池中第一个；sector 模式由 main.py 动态覆盖）
DEFENSIVE = DEFENSIVE_POOL[0]
OFFENSIVE = OFFENSIVE_POOL[0]

# 板块哑铃用的运行时占位（由 screener 动态填充）
DEFENSIVE_SECTOR_POOL = []
OFFENSIVE_SECTOR_POOL = []
DEFENSIVE_SECTOR_NAME = ""   # 实际选用的防御端板块名（ETF 合格后确定，报告/日志用）
OFFENSIVE_SECTOR_NAME = ""   # 实际选用的进攻端板块名
SCREENER_RESULT = {}      # 完整筛选结果（含 excluded / warnings），供日志引用
POLICY_FLAG = []           # 当前运行使用的政策事件标志

# ===== 状态与日志文件名（路径由 config.settings 派生）=====
STATE_FILE = "barbell_state.json"
LOG_PREFIX = "barbell_log_"


# ====================================================================
# 板块筛选与 ETF 映射的板块代码表（screener.py 与 sector_etf_map.py 共享）
# 申万一级行业（共 31 个），名称与 zzshare plates_rank(plate_type=14) 返回对齐
# ====================================================================
SW_L1_SECTORS = [
    "煤炭", "石油石化", "有色金属", "钢铁", "基础化工",
    "建筑材料", "建筑装饰", "机械设备", "电力设备", "国防军工",
    "汽车", "家用电器", "轻工制造", "农林牧渔", "食品饮料",
    "纺织服饰", "医药生物", "电子", "通信", "计算机",
    "传媒", "银行", "非银金融", "房地产", "交通运输",
    "商贸零售", "社会服务", "公用事业", "环保", "综合",
    "美容护理",
]
