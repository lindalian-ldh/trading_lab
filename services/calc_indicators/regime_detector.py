#!/usr/bin/env python3
"""维度 E - 市场状态雷达（regime detector，独立只读模块）。

模块性质：与维度D（周线观察哨）完全同构——**只读信息面板**。
不参与 A/B/C 任何判定，不修改任何阈值，不触发自动开仓/止损。
它的唯一职责是：把"现在是什么市"这件事，用可复算的客观事实摆在你眼前，
供人工对账一段时间后再决定是否让它自动改阈值（见 regime_plan.md）。

设计原则（与 A/B/C/D 解耦）：
    - 数据层：独立调用指数日线接口（复用 market_filter._fetch_index），失败只输出"暂不可用"
    - 逻辑层：不 import main.py，不修改任何 config 字段，绝不写回 config
    - 输出层：独立区块 + `🎯 [E-市场状态]` 前缀，与 A/B/C 的 ✅❌、D 的 🛰️ 严格区分

7 种市场状态（判定优先级：极端 → 无趋势 → 趋势 → 兜底）：

    PANIC_DOWN  恐慌急跌    ATR分位极高 + 跌破MA20 + 近3日累计大跌
    SQUEEZE     极缩量横盘  布林带宽处于历史极低位（变盘前夜）
    RANGE_HIGH  高位震荡    无趋势 + 价格在布林上轨区
    RANGE_LOW   低位震荡    无趋势 + 价格在布林下轨区
    RANGE_MID   中位震荡    无趋势 + 价格在中轨区
    TREND_UP    趋势上涨    均线多头排列 + ADX强 + MA20上行 + 站上MA60
    TREND_DOWN  趋势下跌    均线空头排列 + ADX强 + MA20下行 + 跌破MA60
    UNKNOWN     数据不足

用途（对账期）：每天跑一次，人工核对"机器说的状态"与"你眼里的状态"是否一致。
不一致时，优先怀疑阈值（ADX_TREND_MIN / BANDWIDTH_SQUEEZE_PCT 等），而不是怀疑行情。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from config import TradingConfig

# 复用 market_filter 的指数取数（多源 + 超时 + 输出压制），避免重复实现
from market_filter import _fetch_index

logger = logging.getLogger(__name__)

# 进程内"上次判定"记录：{(symbol_date): regime}
# 用途：对账期打印"较上次判定：变化 / 未变化"。
# 判定频率不高（通常每天 1 次），显式打印变化比静默缓存更有助于人工核对。
_LAST_REGIME: dict = {}

# 进程内判定结果缓存：{(BENCHMARK_INDEX, 判定日期): result}
# 为什么需要：市场状态现在有**两个消费方** —— 入口门控（决定是否否决）与展示区块。
# 无缓存时同一次运行会取两遍指数（baostock 冷启动 ~20s），运行时间直接翻倍。
# 约定与 weekly_observer 一致：只缓存**成功**结果，失败结果每次都重试。
_REGIME_CACHE: dict = {}


# ==================== 状态元数据 ====================

REGIME_META: dict = {
    "PANIC_DOWN": {
        "label": "恐慌急跌",
        "advice": "泥沙俱下，唯一任务是别接飞刀：空仓或极轻仓，等 ATR 回落 + 站回 MA20 再看。",
    },
    "TREND_UP": {
        "label": "趋势上涨",
        "advice": "单边向上，回调即机会：可用突破/浅回踩进场，忽略 RSI 超买，止盈用 ATR 追踪让利润奔跑。",
    },
    "TREND_DOWN": {
        "label": "趋势下跌",
        "advice": "反弹是逃命不是买点：禁止纯 RSI 超卖抄底，只允许'超卖+放量阳线+前低支撑'三重确认的反弹。",
    },
    "RANGE_HIGH": {
        "label": "高位震荡",
        "advice": "顶部区域，突破多为诱多：只减不增，个股入场需缩量回踩不破 + 周线D评级A。",
    },
    "RANGE_LOW": {
        "label": "低位震荡",
        "advice": "底部区域，适合区间低吸：等下轨/前低支撑 + RSI超卖 + 缩量止跌三重共振。",
    },
    "RANGE_MID": {
        "label": "中位震荡",
        "advice": "标准箱体，高抛低吸：禁止突破买入（假突破率最高），要求 RSI超卖与缩量同时成立。",
    },
    "SQUEEZE": {
        "label": "极缩量横盘",
        "advice": "变盘前夜，方向未定：不猜方向，等带宽扩张后按新状态重新判断。",
    },
    "UNKNOWN": {
        "label": "数据不足",
        "advice": "指数数据不足或接口异常，本次不做状态判断，A/B/C 照常参考。",
    },
}

# 状态 → 该状态下"主用哪类入场逻辑"（对账期只打印，不接管判定）
REGIME_ENTRY_HINT: dict = {
    "TREND_UP": "主用：突破 / 浅回踩（偏离阈值可放宽） | 禁用：RSI超买否决、突破必须放量",
    "TREND_DOWN": "主用：超卖反弹（须 AND 放量阳线 + 前低支撑） | 禁用：纯 RSI 超卖、突破买入",
    "RANGE_HIGH": "主用：只减不增 / 缩量回踩不破 | 禁用：追高、突破新高",
    "RANGE_LOW": "主用：均值回归（下轨/前低 + RSI超卖 + 缩量，三者 AND） | 禁用：突破买入",
    "RANGE_MID": "主用：均值回归（箱体下沿低吸） | 禁用：突破买入",
    "SQUEEZE": "主用：观察 | 禁用：任何方向性入场",
    "PANIC_DOWN": "主用：空仓 | 禁用：全部",
    "UNKNOWN": "—",
}


# ==================== 调试日志工具 ====================

def _debug(msg: str, enabled: bool = True) -> None:
    """打印调试日志到控制台（不受 Python logging 级别限制）。

    与 main.py（📋）/ market_filter.py（🌐）/ weekly_observer.py（🛰️）风格一致，
    使用 🎯 标记市场状态日志，便于在控制台中区分。
    """
    if enabled:
        print(f"  🎯 {msg}")


def _meta(key: str, field: str) -> str:
    return REGIME_META.get(key, REGIME_META["UNKNOWN"]).get(field, "")


# ==================== 指标计算 ====================

def _calc_atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Average True Range（与 main.py 同口径：TR 的简单移动平均）。"""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def _calc_adx(df: pd.DataFrame, period: int) -> pd.Series:
    """标准 Wilder ADX。

    步骤：+DM/-DM → Wilder 平滑 → +DI/-DI → DX → Wilder 平滑 → ADX。
    这是"专治震荡市"的经典指标：ADX < 20 表示无趋势（震荡）。
    """
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)

    atr = _calc_atr(df, period)
    # Wilder 平滑：alpha = 1/period
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan)

    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()

    return adx, plus_di, minus_di


def _calc_bollinger(close: pd.Series, period: int, num_std: float):
    """布林带：返回 (中轨, 上轨, 下轨, 带宽)。

    带宽 = (上轨 − 下轨) / 中轨，用于衡量波动收敛/扩张。
    """
    mid = close.rolling(window=period).mean()
    std = close.rolling(window=period).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    bandwidth = (upper - lower) / mid.replace(0, np.nan)
    return mid, upper, lower, bandwidth


def _pct_rank(series: pd.Series, window: int = 0) -> Optional[float]:
    """当前值在最近 window 个有效值中的分位（0~1）。数据不足返回 None。

    用于把 ATR / 带宽这类绝对量转成"相对自身历史"的可比量，
    避免不同指数点位（3000 vs 30000）导致固定阈值失效。

    window > 0 时只看最近 window 个样本（默认 120：约半年的波动率环境），
    这样"极缩量横盘"是相对近半年而言，不会被更早的历史行情稀释。
    """
    s = series.dropna()
    if window and window > 0:
        s = s.tail(window)
    if len(s) < 20:
        return None
    cur = float(s.iloc[-1])
    return float((s <= cur).sum()) / len(s)


def _count_ma_crossings(close: pd.Series, ma: pd.Series, window: int) -> Optional[int]:
    """统计最近 window 根内"价格穿越 MA 中轨"的次数。

    这是 ADX 的补充意见：ADX 只看方向动量的大小，在"缓慢的宽幅来回摆动"里
    也可能读到 30+，但价格反复穿越中轨才是区间震荡的真正特征。
    返回 None 表示数据不足。
    """
    diff = (close - ma).tail(window)
    if len(diff) < window or diff.isna().all():
        return None
    sign = np.sign(diff.fillna(0.0).to_numpy())
    # 只统计符号变化次数（忽略 0 值造成的抖动：先去掉连续的 0）
    sign = sign[sign != 0]
    if len(sign) < 2:
        return 0
    return int((np.diff(sign) != 0).sum())


def _position_in_band(close: float, lower: float, upper: float) -> Optional[float]:
    """价格在布林带内的相对位置：0=下轨，0.5=中轨，1=上轨。带宽为 0 返回 None。"""
    if upper <= lower:
        return None
    return (close - lower) / (upper - lower)


# ==================== 状态判定 ====================

def _classify(facts: dict, config: TradingConfig) -> str:
    """依据事实字典判定 7 态之一。

    优先级（先判极端，再判无趋势，再判趋势，最后兜底）：
        1. 数据不足          → UNKNOWN
        2. 恐慌急跌          → PANIC_DOWN
        3. 无趋势（ADX弱 或 价格反复穿越中轨 或 带宽极低）
             ├ 带宽极低      → SQUEEZE
             └ 按带内位置    → RANGE_HIGH / RANGE_LOW / RANGE_MID
        4. 趋势（ADX强 + 未反复穿越 + 均线排列 + MA20斜率 + 站上/跌破MA60）
             ├ 多头        → TREND_UP
             └ 空头        → TREND_DOWN
        5. 兜底（ADX 中等且均线纠缠）→ 按带内位置归档 RANGE_*

    关于"双重否决"：趋势判定要求 ADX 与"穿越次数"两个条件同时成立。
    ADX 单独用会在'缓慢宽幅摆动'的区间里误报趋势（实测 ADX 可到 32），
    穿越次数是它的必要补充意见。
    """
    # —— 1. 数据不足 ——
    if facts.get("data_ok") is not True:
        return "UNKNOWN"

    adx = facts["adx"]
    pos = facts["pos_in_band"]
    bw_pct = facts["bandwidth_pct_rank"]
    atr_pct = facts["atr_pct_rank"]
    ret3 = facts["ret_3d_pct"]
    ma_align = facts["ma_align"]
    slope_up = facts["ma20_slope_up"]
    above_ma60 = facts["above_ma60"]
    crossings = facts.get("ma20_crossings")

    # —— 2. 恐慌急跌（最高优先级：任何其他状态在恐慌中都失效）——
    # 必须是"急跌 + 破位"两个条件同时成立，不只看跌得多快：
    #   ① 波动率已在高位 + 近3日累计大跌 + 跌破 MA60            → 系统性风险
    #   ② 近3日跌幅相对当前 ATR 异常大 + 跌破 MA20 且跌破 MA5   → 上升趋势中的突然破位急跌
    # 通道②同时受"ATR 倍数"和"绝对下限"双重约束：
    #   只用 ATR 倍数会在低波动环境把"跌 2.5%"放大成假恐慌（已实测踩过这个坑）。
    atr_pct_of_price = facts.get("atr_pct_of_price")
    below_ma_short = facts.get("below_ma_short") is True
    panic_vol = (atr_pct is not None and atr_pct >= config.REGIME_PANIC_ATR_PCT
                 and ret3 is not None and ret3 <= config.REGIME_PANIC_RET3_PCT
                 and above_ma60 is False)
    panic_crash = (ret3 is not None and atr_pct_of_price is not None
                   and ret3 <= config.REGIME_PANIC_RET3_FLOOR_PCT
                   and ret3 <= -config.REGIME_PANIC_RET3_ATR_MULT * atr_pct_of_price
                   and facts.get("below_ma_mid") is True
                   and below_ma_short)
    if panic_vol or panic_crash:
        return "PANIC_DOWN"

    # —— 3. 无趋势区 ——
    weak_adx = (adx is not None and adx < config.REGIME_ADX_TREND_MIN)
    choppy = (crossings is not None and crossings >= config.REGIME_CROSS_TREND_MAX)
    no_trend = weak_adx or choppy

    # 挤压（变盘前夜）：波动收敛 + 价格被压缩在窄幅内 + 确实没有趋势。
    # caliber（近20日振幅/ATR）是核心判据：它直接衡量"价格有没有在动"。
    # 必须叠加 no_trend 保护：单调趋势的价格序列标准差天然很小（带宽很低），
    # 若不加保护，任何干净的单边行情都会被误判成"极缩量横盘"（已实测踩过这个坑）。
    caliber = facts.get("caliber")
    squeeze = (bw_pct is not None and bw_pct <= config.REGIME_SQUEEZE_BW_PCT
               and caliber is not None and caliber <= config.REGIME_SQUEEZE_CALIBER
               and no_trend)

    if squeeze:
        return "SQUEEZE"

    if no_trend:
        if pos is None:
            return "RANGE_MID"
        if pos >= config.REGIME_RANGE_HIGH_POS:
            return "RANGE_HIGH"
        if pos <= config.REGIME_RANGE_LOW_POS:
            return "RANGE_LOW"
        return "RANGE_MID"

    # —— 4. 趋势区（ADX 强 且 未反复穿越中轨）——
    if (ma_align == "bull" and slope_up and above_ma60
            and adx is not None and adx >= config.REGIME_ADX_TREND_MIN):
        return "TREND_UP"
    if (ma_align == "bear" and not slope_up and above_ma60 is False
            and adx is not None and adx >= config.REGIME_ADX_TREND_MIN):
        return "TREND_DOWN"

    # —— 5. 兜底：ADX 中等 / 均线纠缠 / 排列与方向矛盾 ——
    if pos is None:
        return "RANGE_MID"
    if pos >= config.REGIME_RANGE_HIGH_POS:
        return "RANGE_HIGH"
    if pos <= config.REGIME_RANGE_LOW_POS:
        return "RANGE_LOW"
    return "RANGE_MID"


# ==================== 数据获取 ====================

def _fetch_index_data(config: TradingConfig) -> Optional[pd.DataFrame]:
    """获取基准指数日线。复用 market_filter._fetch_index（多源 + 超时 + 降级）。

    默认让 baostock 优先（见 REGIME_PREFER_SOURCE）：本机 efinance 的 eastmoney
    HTTPS 通道常被对端断开，先试它只会白等一个超时周期。

    独立异常捕获：任何异常都返回 None，绝不向 A/B/C 抛。
    """
    try:
        return _fetch_index(
            config.BENCHMARK_INDEX,
            config.REGIME_FETCH_BARS,
            config.REGIME_FETCH_TIMEOUT,
            dbg=False,   # 本模块自己有 dbg 输出，避免与 market_filter 日志重复刷屏
            prefer=getattr(config, "REGIME_PREFER_SOURCE", "efinance"),
        )
    except Exception as e:  # pragma: no cover - 容灾兜底
        logger.info("[E-市场状态] 指数数据获取异常: %s", e)
        return None


# ==================== 主入口 ====================

def detect_regime(config: TradingConfig) -> dict:
    """维度E - 市场状态雷达主入口（只读，绝不修改 config 任何字段）。

    Args:
        config: TradingConfig 实例，读取以下字段（均为只读）：
            ENABLE_REGIME_DETECTOR / REGIME_DEBUG / REGIME_FETCH_BARS /
            REGIME_FETCH_TIMEOUT / BENCHMARK_INDEX /
            REGIME_MA_* / REGIME_MA20_SLOPE_LOOKBACK / REGIME_ADX_* /
            REGIME_ATR_* / REGIME_BB_* / REGIME_SQUEEZE_BW_PCT /
            REGIME_PANIC_* / REGIME_RANGE_*_POS

    Returns:
        dict: {
            'available': bool,      # 是否成功判定（False 时 regime='UNKNOWN'）
            'regime': str,          # 7 态之一 / 'UNKNOWN'
            'label': str,           # 中文标签
            'advice': str,          # 人工参考建议
            'entry_hint': str,      # 该状态下建议主用/禁用的入场逻辑（对账期只打印）
            'facts': dict,          # 全部客观事实（可复算）
            'bars_used': int,
            'date': str,            # 判定所用最新交易日
            'error': str | None,
        }
    """
    dbg = config.REGIME_DEBUG

    # —— 前置开关 ——
    if not config.ENABLE_REGIME_DETECTOR:
        return {
            "available": False, "regime": "UNKNOWN", "label": _meta("UNKNOWN", "label"),
            "advice": "维度E已关闭 (ENABLE_REGIME_DETECTOR=False)",
            "entry_hint": "—", "facts": {}, "bars_used": 0, "date": "", "error": "disabled",
            "benchmark": config.BENCHMARK_INDEX,
        }

    if dbg:
        _debug("══════ [E-市场状态] 状态雷达排查 ══════", True)
        _debug(f"基准指数: {config.BENCHMARK_INDEX}  |  拉取根数: {config.REGIME_FETCH_BARS}  |  超时: {config.REGIME_FETCH_TIMEOUT}s", True)
        _debug(f"首选数据源: {getattr(config, 'REGIME_PREFER_SOURCE', 'efinance')}", True)
        _debug(f"均线: MA{config.REGIME_MA_SHORT}/{config.REGIME_MA_MID}/{config.REGIME_MA_LONG}"
               f"  |  ADX周期: {config.REGIME_ADX_PERIOD}  趋势线: {config.REGIME_ADX_TREND_MIN}", True)
        _debug(f"布林: {config.REGIME_BB_PERIOD}日/{config.REGIME_BB_STD}σ  挤压分位: {config.REGIME_SQUEEZE_BW_PCT}", True)
        _debug(f"震荡分区: 上轨区≥{config.REGIME_RANGE_HIGH_POS}  下轨区≤{config.REGIME_RANGE_LOW_POS}", True)
        _debug(f"恐慌线: ATR分位≥{config.REGIME_PANIC_ATR_PCT} 且 近3日≤{config.REGIME_PANIC_RET3_PCT}%", True)

    # —— 进程内缓存（同一基准 + 同一天只算一次）——
    # 市场状态有两个消费方：入口门控 与 展示区块。命中缓存可避免重复取指数。
    cache_key = (config.BENCHMARK_INDEX, datetime.now().strftime("%Y-%m-%d"))
    if cache_key in _REGIME_CACHE:
        if dbg:
            _debug(f"命中进程内缓存（{cache_key[0]} @ {cache_key[1]}），跳过指数取数", True)
        cached = dict(_REGIME_CACHE[cache_key])
        cached["from_cache"] = True
        return cached

    # —— 数据获取 ——
    df = _fetch_index_data(config)

    min_len = config.REGIME_MA_LONG + 10
    if df is None or df.empty:
        msg = f"指数 {config.BENCHMARK_INDEX} 数据获取失败，本次不做状态判断"
        if dbg:
            _debug(f"❌ {msg}", True)
            _debug("══════ [E-市场状态] 排查结束 ❌ 数据暂不可用 ══════", True)
        logger.info("[E-市场状态] %s", msg)
        return {
            "available": False, "regime": "UNKNOWN", "label": _meta("UNKNOWN", "label"),
            "advice": _meta("UNKNOWN", "advice"), "entry_hint": "—",
            "facts": {}, "bars_used": 0, "date": "", "error": msg, "benchmark": config.BENCHMARK_INDEX,
        }

    if len(df) < min_len:
        msg = f"指数数据不足：仅 {len(df)} 根，需 ≥ {min_len}"
        if dbg:
            _debug(f"❌ {msg}", True)
            _debug("══════ [E-市场状态] 排查结束 ❌ 数据不足 ══════", True)
        logger.info("[E-市场状态] %s", msg)
        return {
            "available": False, "regime": "UNKNOWN", "label": _meta("UNKNOWN", "label"),
            "advice": _meta("UNKNOWN", "advice"), "entry_hint": "—",
            "facts": {}, "bars_used": len(df), "date": "", "error": msg, "benchmark": config.BENCHMARK_INDEX,
        }

    if dbg:
        _debug(f"✅ 数据充足: {len(df)} 根 (需 ≥ {min_len})", True)
        _debug(f"   日期范围: {df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}", True)

    # —— 事实计算 ——
    close = df["close"]
    cur_price = float(close.iloc[-1])

    ma_short = close.rolling(config.REGIME_MA_SHORT).mean()
    ma_mid = close.rolling(config.REGIME_MA_MID).mean()
    ma_long = close.rolling(config.REGIME_MA_LONG).mean()

    ma_s = float(ma_short.iloc[-1])
    ma_m = float(ma_mid.iloc[-1])
    ma_l = float(ma_long.iloc[-1])

    # MA20 斜率：对比 N 根之前的 MA20（默认 5 根，约一周）
    lb = config.REGIME_MA20_SLOPE_LOOKBACK
    ma_mid_prev = float(ma_mid.iloc[-(lb + 1)]) if len(ma_mid) > lb and not pd.isna(ma_mid.iloc[-(lb + 1)]) else float("nan")
    ma20_slope_pct = ((ma_m / ma_mid_prev - 1) * 100) if ma_mid_prev and ma_mid_prev > 0 else 0.0
    slope_up = ma20_slope_pct > config.REGIME_MA20_SLOPE_MIN_PCT

    above_ma60 = cur_price >= ma_l
    below_ma_mid = cur_price < ma_m
    below_ma_short = cur_price < ma_s

    if ma_s > ma_m > ma_l:
        ma_align = "bull"
    elif ma_s < ma_m < ma_l:
        ma_align = "bear"
    else:
        ma_align = "mixed"

    adx_s, plus_di_s, minus_di_s = _calc_adx(df, config.REGIME_ADX_PERIOD)
    adx = float(adx_s.iloc[-1]) if not pd.isna(adx_s.iloc[-1]) else None
    plus_di = float(plus_di_s.iloc[-1]) if not pd.isna(plus_di_s.iloc[-1]) else None
    minus_di = float(minus_di_s.iloc[-1]) if not pd.isna(minus_di_s.iloc[-1]) else None

    atr_s = _calc_atr(df, config.REGIME_ATR_PERIOD)
    atr = float(atr_s.iloc[-1]) if not pd.isna(atr_s.iloc[-1]) else None
    atr_pct_rank = _pct_rank(atr_s, config.REGIME_PCTRANK_WINDOW)
    # ATR 占价格百分比：用于把"急跌幅度"标准化为波动率倍数（恐慌判定通道②）
    atr_pct_of_price = (atr / cur_price * 100) if (atr is not None and cur_price > 0) else None

    bb_mid, bb_up, bb_low, bw = _calc_bollinger(close, config.REGIME_BB_PERIOD, config.REGIME_BB_STD)
    bw_pct_rank = _pct_rank(bw, config.REGIME_PCTRANK_WINDOW)
    bb_upper = float(bb_up.iloc[-1])
    bb_lower = float(bb_low.iloc[-1])
    bb_middle = float(bb_mid.iloc[-1])
    bandwidth = float(bw.iloc[-1])
    pos_in_band = _position_in_band(cur_price, bb_lower, bb_upper)

    # ADX 的第二意见：价格反复穿越 MA中轨 = 区间震荡（防止"缓慢宽幅摆动"被误判为趋势）
    cw = config.REGIME_CROSS_WINDOW
    ma20_crossings = _count_ma_crossings(close, ma_mid, cw)

    # 口径（caliber）：近 20 日振幅 / ATR，衡量"价格是否被压缩在窄幅内"。
    # ≤ 4 表示近 20 日的整个波动区间还不到 4 个 ATR → 典型的缩量横盘。
    caliber = None
    if atr is not None and atr > 0 and len(df) >= 20:
        recent_high = float(df["high"].tail(config.REGIME_BB_PERIOD).max())
        recent_low = float(df["low"].tail(config.REGIME_BB_PERIOD).min())
        caliber = (recent_high - recent_low) / atr

    # 近 3 日累计涨跌幅
    ret_3d_pct = ((cur_price / float(close.iloc[-4]) - 1) * 100) if len(close) >= 4 else None
    # 当日涨跌幅（仅供人工参考，不参与状态判定）
    ret_1d_pct = ((cur_price / float(close.iloc[-2]) - 1) * 100) if len(close) >= 2 else None

    facts = {
        "data_ok": True,
        "date": df["date"].iloc[-1].date().isoformat(),
        "price": cur_price,
        "ma_short": ma_s, "ma_mid": ma_m, "ma_long": ma_l,
        "ma_periods": (config.REGIME_MA_SHORT, config.REGIME_MA_MID, config.REGIME_MA_LONG),
        "ma_align": ma_align,
        "ma20_slope_pct": ma20_slope_pct,
        "ma20_slope_lookback": lb,
        "ma20_slope_up": slope_up,
        "above_ma60": above_ma60,
        "below_ma_mid": below_ma_mid,
        "below_ma_short": below_ma_short,
        "adx": adx, "plus_di": plus_di, "minus_di": minus_di,
        "adx_period": config.REGIME_ADX_PERIOD,
        "ma20_crossings": ma20_crossings,
        "cross_window": cw,
        "cross_trend_max": config.REGIME_CROSS_TREND_MAX,
        "caliber": caliber,
        "squeeze_caliber": config.REGIME_SQUEEZE_CALIBER,
        "atr": atr, "atr_pct_rank": atr_pct_rank, "atr_period": config.REGIME_ATR_PERIOD,
        "atr_pct_of_price": atr_pct_of_price,
        "bb_mid": bb_middle, "bb_upper": bb_upper, "bb_lower": bb_lower,
        "bandwidth": bandwidth, "bandwidth_pct_rank": bw_pct_rank,
        "bb_period": config.REGIME_BB_PERIOD, "bb_std": config.REGIME_BB_STD,
        "pos_in_band": pos_in_band,
        "ret_1d_pct": ret_1d_pct, "ret_3d_pct": ret_3d_pct,
    }

    if dbg:
        _debug("─── 客观事实 ───", True)
        _debug(f"  最新收盘: {cur_price:.2f}  ({facts['date']})", True)
        _debug(f"  MA{config.REGIME_MA_SHORT}/{config.REGIME_MA_MID}/{config.REGIME_MA_LONG}: "
               f"{ma_s:.2f} / {ma_m:.2f} / {ma_l:.2f}  → 排列={ma_align}", True)
        _debug(f"  MA{config.REGIME_MA_MID} 斜率({lb}根): {ma20_slope_pct:+.3f}%  → {'向上' if slope_up else '未向上'}", True)
        _debug(f"  站上 MA{config.REGIME_MA_LONG}: {above_ma60}", True)
        _debug(f"  ADX({config.REGIME_ADX_PERIOD}): {adx:.2f}" if adx is not None else "  ADX: 数据不足", True)
        _debug(f"  +DI/-DI: {plus_di:.2f} / {minus_di:.2f}" if (plus_di is not None and minus_di is not None) else "  +DI/-DI: 数据不足", True)
        _debug(f"  近{cw}根穿越MA中轨次数: {ma20_crossings}" +
               (f"（≥{config.REGIME_CROSS_TREND_MAX} 视为区间震荡）" if ma20_crossings is not None else ""), True)
        _debug(f"  ATR({config.REGIME_ATR_PERIOD}): {atr:.3f}" if atr is not None else "  ATR: 数据不足", True)
        _debug(f"  ATR 分位: {atr_pct_rank:.0%}" if atr_pct_rank is not None else "  ATR 分位: 数据不足", True)
        _debug(f"  布林带 {config.REGIME_BB_PERIOD}日: 上轨 {bb_upper:.2f} / 中轨 {bb_middle:.2f} / 下轨 {bb_lower:.2f}", True)
        _debug(f"  带宽: {bandwidth:.4f}  带宽分位: " + (f"{bw_pct_rank:.0%}" if bw_pct_rank is not None else "数据不足"), True)
        _debug(f"  口径(近{config.REGIME_BB_PERIOD}日振幅/ATR): {caliber:.2f}" +
               (f"（≤{config.REGIME_SQUEEZE_CALIBER} 才算窄幅压缩）" if caliber is not None else ""), True)
        _debug(f"  带内位置: {pos_in_band:.2f} (0=下轨 0.5=中轨 1=上轨)" if pos_in_band is not None else "  带内位置: 数据不足", True)
        _debug(f"  近1日: {ret_1d_pct:+.2f}%  近3日: {ret_3d_pct:+.2f}%" if (ret_1d_pct is not None and ret_3d_pct is not None) else "  涨跌幅: 数据不足", True)

    # —— 状态判定 ——
    regime = _classify(facts, config)
    label = _meta(regime, "label")
    advice = _meta(regime, "advice")
    entry_hint = REGIME_ENTRY_HINT.get(regime, "—")

    # —— 与上次判定对比（对账期用：状态切换才是需要人工复核的时刻）——
    compare_key = f"{config.BENCHMARK_INDEX}_{facts['date']}"
    prev = _LAST_REGIME.get(compare_key)
    if prev is None:
        change = f"首次判定（{facts['date']}）"
    elif prev == regime:
        change = f"未变化（仍为 {regime}）"
    else:
        change = f"⚠️ 状态切换: {prev} → {regime}"
    _LAST_REGIME[compare_key] = regime

    if dbg:
        _debug("─── 状态判定 ───", True)
        _debug(f"  🎯 市场状态: {regime}（{label}）", True)
        _debug(f"  较上次判定: {change}", True)
        _debug(f"  建议: {advice}", True)
        _debug("══════ [E-市场状态] 排查结束 ══════", True)

    logger.info("[E-市场状态] %s 状态=%s (%s) ADX=%.2f 带宽分位=%s",
                config.BENCHMARK_INDEX, regime, label,
                adx if adx is not None else -1,
                f"{bw_pct_rank:.0%}" if bw_pct_rank is not None else "-")

    result = {
        "available": True, "regime": regime, "label": label,
        "advice": advice, "entry_hint": entry_hint, "change": change,
        "facts": facts, "bars_used": len(df), "date": facts["date"], "error": None,
        "benchmark": config.BENCHMARK_INDEX, "from_cache": False,
    }
    # 只缓存成功结果（失败/数据不足的路径直接 return，不写缓存 → 下次仍会重试）
    _REGIME_CACHE[cache_key] = dict(result)
    return result


def print_regime_result(result: dict) -> None:
    """打印维度E结果到控制台（独立区块，与 A/B/C/D 输出严格分隔）。"""
    print("【维度E - 市场状态雷达（只读，不参与 A/B/C 判定）】")
    if not result.get("available"):
        err = result.get("error") or "数据暂不可用"
        if err == "disabled":
            print("  ⏭️  维度E已关闭，跳过市场状态判断")
            print()
            return
        # —— 取数失败 / 数据不足：这是**危险**分支，必须显式警示 ——
        # 为什么加（2026-09-30）：择时结论（PANIC_DOWN 加仓）是本项目唯一经检验
        # 有效的正面发现；取数失败时该信号是"未知"，而不是"非恐慌"。原来只有一行
        # "⚠️ 市场状态暂不可用"，在长输出里极易被忽略，用户可能把"未知"当成"安全"。
        # 不改退出码：维度E 的契约是"只读、不参与 A/B/C 判定"，
        # 改了会让 scan_leaders_chart.sh 把正常筛查运行判为失败。
        print(f"  ⚠️  市场状态暂不可用：{err}")
        print()
        print("  ╔════════════════════════════════════════════════════════════════╗")
        print("  ║  ⛔ 今日「择时判断」不可用 —— 请勿据此做任何仓位决策            ║")
        print("  ╚════════════════════════════════════════════════════════════════╝")
        print("     · 7 状态判定需要指数日线，本次取数失败或数据不足")
        print("     · 择时信号（PANIC_DOWN 加仓）今天 = **未知**，不等于「非恐慌」")
        print("     · 门控里 REGIME_BREAKOUT / SURVIVABILITY 若显示「已跳过」，同理")
        print("     · 处理：① 稍后重跑本命令   ② 检查网络 / baostock 可用性")
        print("     · 记台账时今日 regime 列为空；请不要把今日样本当作有效的择时观测")
        print()
        # 同时写 stderr：scan_leaders_chart.sh 等脚本会捕获 stdout 做解析，
        # 只有 stderr 才能保证这条警告在终端里一定被看到。
        import sys as _sys
        print(f"[regime-unavailable] 市场状态不可用：{err} —— 今日勿做择时决策",
              file=_sys.stderr)
        return

    f = result.get("facts", {})
    regime = result.get("regime", "UNKNOWN")
    label = result.get("label", "")

    # 标明基准指数：ETF 模式下它会被切换（如 sh000300），不标出来用户无法判断
    # "为什么同一只 ETF 的市场状态与上次不同"，也无法对账。
    idx = result.get("benchmark", "-")
    idx_name = _benchmark_cn(idx)
    print(f"  📅 判定日期: {f.get('date', '-')}  |  指数根数: {result.get('bars_used', 0)}"
          f"  |  基准: {idx}{('（' + idx_name + '）') if idx_name else ''}")

    # ① 均线结构
    align_cn = {"bull": "多头排列", "bear": "空头排列", "mixed": "纠缠"}.get(f.get("ma_align"), "-")
    ma_p = f.get("ma_periods", ("-", "-", "-"))
    print(f"  ① 均线结构: {align_cn}  "
          f"(MA{ma_p[0]} {f.get('ma_short', 0):.2f} / "
          f"MA{ma_p[1]} {f.get('ma_mid', 0):.2f} / "
          f"MA{ma_p[2]} {f.get('ma_long', 0):.2f})")

    slope = f.get("ma20_slope_pct")
    slope_cn = "向上" if f.get("ma20_slope_up") else "未向上"
    print(f"  ② MA中轨斜率({f.get('ma20_slope_lookback', '-')}根): {slope:+.3f}% → {slope_cn}"
          if slope is not None else "  ② MA中轨斜率: 数据不足")

    adx = f.get("adx")
    adx_cn = "无趋势（震荡）" if (adx is not None and adx < 20) else ("趋势明确" if adx is not None and adx >= 25 else "趋势偏弱")
    print(f"  ③ ADX({f.get('adx_period', '-')}): {adx:.2f}（{adx_cn}）  "
          f"+DI {f.get('plus_di', 0):.2f} / -DI {f.get('minus_di', 0):.2f}"
          if adx is not None else "  ③ ADX: 数据不足")

    cr = f.get("ma20_crossings")
    print(f"  ③b 近{f.get('cross_window', '-')}根穿越MA中轨: {cr} 次"
          + (f"（≥{f.get('cross_trend_max', '-')} 视为区间震荡）" if cr is not None else "")
          if cr is not None else "  ③b 穿越次数: 数据不足")

    atr_rank = f.get("atr_pct_rank")
    print(f"  ④ ATR 波动分位: {atr_rank:.0%}（{_rank_cn(atr_rank)}）"
          if atr_rank is not None else "  ④ ATR 波动分位: 数据不足")

    bw_rank = f.get("bandwidth_pct_rank")
    print(f"  ⑤ 布林带宽分位: {bw_rank:.0%}（{_rank_cn(bw_rank)}）"
          f"  带宽 {f.get('bandwidth', 0):.4f}"
          if bw_rank is not None else "  ⑤ 布林带宽分位: 数据不足")

    cal = f.get("caliber")
    print(f"  ⑤b 口径(近{f.get('bb_period', 20)}日振幅/ATR): {cal:.2f}"
          + (f"（≤{f.get('squeeze_caliber', '-')} = 窄幅压缩）" if cal is not None else "")
          if cal is not None else "  ⑤b 口径: 数据不足")

    pos = f.get("pos_in_band")
    pos_cn = _pos_cn(pos)
    print(f"  ⑥ 带内位置: {pos:.2f}（{pos_cn}）" if pos is not None else "  ⑥ 带内位置: 数据不足")

    r3 = f.get("ret_3d_pct")
    r1 = f.get("ret_1d_pct")
    print(f"  ⑦ 近1日 {r1:+.2f}%  |  近3日 {r3:+.2f}%" if (r1 is not None and r3 is not None) else "  ⑦ 涨跌幅: 数据不足")

    print()
    print(f"  🎯 市场状态: {regime}（{label}）")
    if result.get("change"):
        print(f"  🔁 较上次判定: {result['change']}")
    print(f"  💡 该状态建议的入场逻辑: {result.get('entry_hint', '—')}")
    print(f"  ⚠️  注意: 本区块为只读信息，不改变 A/B/C 任何判定结果。")
    print()


def _benchmark_cn(index_code: str) -> str:
    """常见指数代码 → 中文名（ETF 模式下基准会被切换，输出需要标清楚是哪个）。"""
    return {
        "sh000001": "上证综指", "sz399001": "深证成指", "sz399006": "创业板指",
        "sh000300": "沪深300", "sh000905": "中证500", "sh000852": "中证1000",
        "sh000016": "上证50", "sh000010": "上证180", "sh000015": "上证红利",
        "sh000922": "中证红利", "sz399673": "创业板50", "sh000688": "科创50",
    }.get(index_code, "")


def _rank_cn(r: Optional[float]) -> str:
    if r is None:
        return "-"
    if r >= 0.9:
        return "极高"
    if r >= 0.7:
        return "偏高"
    if r <= 0.1:
        return "极低"
    if r <= 0.3:
        return "偏低"
    return "中位"


def _pos_cn(pos: Optional[float]) -> str:
    if pos is None:
        return "-"
    if pos >= 0.8:
        return "贴近上轨（高位）"
    if pos <= 0.2:
        return "贴近下轨（低位）"
    return "中轨附近"
