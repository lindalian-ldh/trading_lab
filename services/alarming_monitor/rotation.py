"""板块轮动分析：数据对齐 + RRG 四象限 + 5 维评分。

三大核心函数（纯函数，无 IO）：
    align_to_benchmark()      时间轴全局对齐（基准指数交易日为锚，ffill）
    analyze_single_etf()      单 ETF 完整分析：RS-Ratio/RS-Momentum → 象限标签 + 5 维评分
    classify_rrg_quadrant()   RRG 四象限分类（领涨/轮动初期/滞后/退潮）

时间轴对齐规则（用户明确要求）：
    所有 ETF 序列以沪深300交易日索引为基准，向前填充（ffill），
    确保时间轴一致，避免因 ETF 停牌或节假日导致长度不齐。

RRG 四象限（按 RS-Ratio 与 RS-Momentum 组合）：
    象限 I  领涨主线：    RS-Ratio > 1.0 且 RS-Momentum > 0   （强强：强者恒强）
    象限 II 轮动初期：    RS-Ratio ≤ 1.0 且 RS-Momentum > 0   （弱强：由弱转强）
    象限 III 滞后回避：   RS-Ratio ≤ 1.0 且 RS-Momentum ≤ 0   （弱弱：持续弱势）
    象限 IV  退潮预警：   RS-Ratio > 1.0 且 RS-Momentum ≤ 0   （强弱：强转弱预警）
    边界保守归类（cfg.RRG_BOUNDARY_CONSERVATIVE=True）：
        RS-Ratio = 1.0  → 归为"≤1.0侧"（弱侧）
        RS-Momentum = 0 → 归为"≤0侧"（负动量侧）

5 维评分卡（每维原始分 → 归一化 [0, 5]，按 cfg.SCORE_WEIGHTS 加权求和）：
    维1 相对动量(25%)：  (RS-Ratio - 1) 分段映射 → [0, 5]（A2 改进）
                        分段：1.03→1分 / 1.10→3分 / 1.20→5分；跑输基准(<1.0)→0分
    维2 动量加速度(20%)：RS-Momentum 二阶差分 → [0, 5]，SCORE_MOM_ACC_FULL(5%) 即满分
    维3 ADX 趋势强度(20%)：ETF 自身 ADX(14) 分段映射 → [0, 5]，含 +DI/-DI 方向降权
                        分段：15→0 / 20→1 / 25→3 / 30→5；下跌趋势乘 ADX_DOWN_PENALTY(0.1)
    维4 资金关注度(20%)：换手率代理(amount/流通市值代理) → [0, 5]，SCORE_CAP_ATTENTION_FULL(5%) 即满分
    维5 拥挤度折扣(15%)：原始 5 分，若换手率 > CROWDING_TURNOVER_THRESHOLD 扣 CROWDING_PENALTY
    综合得分范围：[0, 5]

操作参考（A1 象限地板/天花板 + B5 大盘均线 + B6 波动率 + C7 回避分级）：
    操作参考不再仅由总分决定，而是"象限×大盘×波动率"三维矩阵：
        领涨主线🟢：地板=关注建仓（高分→持有/加仓）
        轮动初期🟡：地板=轻仓试错/观察不新增
        退潮预警🟠：天花板=持有（不许加仓），低分→减仓
        滞后回避🔴：强制清仓/减仓（C7 分级）
    大盘均线状态（基准收盘 vs MA20/MA60）：
        below（空头）：领涨主线→持有；轮动初期→观察不新增；退潮→减仓；滞后→清仓
        above（多头）：领涨主线→持有/加仓；轮动初期→轻仓试错；退潮→持有观察
        mixed（震荡）：领涨主线→持有；轮动初期→观察不新增；退潮→减仓
    波动率修正（ATR14/close，B6 风险刹车，不进加权总分）：
        高波动(>3%) + 领涨 → 不加仓（持有，风险收益比恶化）
        高波动(>3%) + 退潮 → 减仓加速
        低波动(<1.5%) + 领涨 → 可持有/加仓（健康趋势）
    C7 回避分级（"回避"拆成可执行档位）：
        清仓（破位）：滞后回避 + 空头/震荡
        减仓（趋势转弱）：退潮预警 + 空头/震荡
        观察不新增（震荡方向不明）：轮动初期 + 空头/震荡
        轻仓试错（轮动初期）：轮动初期 + 多头
    总分三档（>4持有/加仓、3~4关注、≤3回避）仅在象限标签缺失时作兜底。
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from config import AlarmConfig
from indicators import (
    calc_adx_etf, calc_amount_percentile, calc_amount_percentile_series,
    calc_atr_pct_series, calc_atr_stop, calc_bollinger_bandwidth,
    calc_long_adx, calc_long_rs_ratio, calc_ma_slope,
    calc_multi_period_rs, calc_obv, calc_rs_momentum, calc_rs_ratio,
    calc_turnover_ratio, calc_volatility_ratio, calc_volume_ratio, calc_vwap,
)


# ====================================================================
# 1. 时间轴全局对齐
# ====================================================================

def align_to_benchmark(bench_df: pd.DataFrame,
                       etf_dfs: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """以基准指数交易日为锚点，把所有 ETF DataFrame 对齐到同一时间轴。

    对齐规则：
        1. 取 bench_df['date'] 为统一交易日索引（沪深300交易日）
        2. 对每个 ETF：按 date 左连接到基准索引，缺失值 ffill（向前填充）
        3. 若 ETF 首行就缺失（上市晚于基准），保留 NaN（后续指标会自动判定数据不足）
        4. 输出 DataFrame 行数 = len(bench_df)，按 date 升序

    Args:
        bench_df: 基准指数日线（需含 date 列）
        etf_dfs: {symbol: df}，每个 df 需含 date + OHLCV 列

    Returns:
        {symbol: aligned_df}，aligned_df 与 bench_df 等行数，按 date 对齐
    """
    if bench_df is None or bench_df.empty or 'date' not in bench_df.columns:
        raise ValueError("align_to_benchmark: bench_df 为空或无 date 列")

    # 统一基准日期轴
    bench_dates = pd.DataFrame({'date': bench_df['date'].astype(str).values})
    aligned = {}

    for sym, df in etf_dfs.items():
        if df is None or df.empty:
            # ETF 完全无数据：输出全 NaN 等长占位，后续流程判定数据不足
            aligned[sym] = _empty_aligned(len(bench_dates))
            continue

        df = df.copy()
        df['date'] = df['date'].astype(str)
        # 左连接：保留基准所有日期
        merged = bench_dates.merge(df, on='date', how='left')
        # ffill 向前填充（停牌/节假日缺失值复用前一日）
        num_cols = [c for c in merged.columns if c != 'date']
        merged[num_cols] = merged[num_cols].ffill()
        aligned[sym] = merged.reset_index(drop=True)

    return aligned


def _empty_aligned(n_rows: int) -> pd.DataFrame:
    """输出 n_rows 行的空对齐占位（全 NaN）。"""
    return pd.DataFrame({
        'date': [''] * n_rows,
        'open': [np.nan] * n_rows,
        'high': [np.nan] * n_rows,
        'low': [np.nan] * n_rows,
        'close': [np.nan] * n_rows,
        'volume': [np.nan] * n_rows,
        'amount': [np.nan] * n_rows,
    })


# ====================================================================
# 2. RRG 四象限分类
# ====================================================================

def classify_rrg_quadrant(rs_ratio: float, rs_momentum: float,
                          cfg: AlarmConfig) -> tuple[str, str]:
    """RS-Ratio + RS-Momentum → 四象限标签（返回 label, emoji）。

    组合规则（含边界保守归类）：
        领涨主线 🟢    RS-Ratio > 1.0  AND  RS-Momentum > 0     （强强）
        轮动初期 🟡    RS-Ratio ≤ 1.0 AND  RS-Momentum > 0     （弱强，转强初期）
        滞后回避 🔴    RS-Ratio ≤ 1.0 AND  RS-Momentum ≤ 0    （弱弱）
        退潮预警 🟠    RS-Ratio > 1.0  AND  RS-Momentum ≤ 0    （强弱，盛极而衰）

    边界保守（cfg.RRG_BOUNDARY_CONSERVATIVE=True）：
        RS-Ratio = 1.0  → 视为 ≤ 1.0（归入弱侧）
        RS-Momentum = 0 → 视为 ≤ 0（归入负动量侧）
    """
    conservative = getattr(cfg, 'RRG_BOUNDARY_CONSERVATIVE', True)
    ratio_strong = cfg.RRG_RATIO_STRONG
    mom_strong = cfg.RRG_MOMENTUM_STRONG

    # 边界保守：= 阈值时归入弱势侧
    if conservative:
        ratio_ok = rs_ratio > ratio_strong
        mom_ok = rs_momentum > mom_strong
    else:
        ratio_ok = rs_ratio >= ratio_strong
        mom_ok = rs_momentum >= mom_strong

    if ratio_ok and mom_ok:
        return '领涨主线', '🟢'
    elif (not ratio_ok) and mom_ok:
        return '轮动初期', '🟡'
    elif (not ratio_ok) and (not mom_ok):
        return '滞后回避', '🔴'
    else:  # ratio_ok and (not mom_ok)
        return '退潮预警', '🟠'


# ====================================================================
# 3. 5 维评分卡
# ====================================================================

def _normalize(raw: float, full: float) -> float:
    """原始分 → 归一化 [0, 5]。clip 防溢出。"""
    if full <= 0 or not np.isfinite(raw):
        return 0.0
    ratio = abs(float(raw)) / float(full)
    return float(np.clip(ratio * 5.0, 0.0, 5.0))


def _normalize_rel_mom_piecewise(rs_ratio: float, cfg: AlarmConfig) -> float:
    """维1 相对动量分段归一化 → [0, 5]（A2 改进）。

    实战中 RS-Ratio 多在 0.9~1.15，原线性映射(超基准0.5即满分)会把
    "领涨主线"(RS≈1.05~1.15)压成 0.5~1.5 分的低分，导致评分与象限标签脱节。

    分段（cfg.SCORE_REL_MOM_BREAKPOINTS）：
        RS-Ratio = 1.0  → 0 分（同步基准）
        1.03      → 1 分（轻微跑赢）
        1.10      → 3 分（显著跑赢）
        1.20      → 5 分（满分，强势主线）
        > 1.20    → 5 分（封顶）
        < 1.0     → 0 分（跑输基准）

    分段间线性插值。
    """
    if rs_ratio is None or not np.isfinite(rs_ratio):
        return 0.0
    rel = float(rs_ratio) - 1.0
    if rel <= 0:
        return 0.0
    bps = cfg.SCORE_REL_MOM_BREAKPOINTS
    if not bps:
        return 0.0
    prev_x, prev_y = 0.0, 0.0
    for x, y in bps:
        if rel <= x:
            if x == prev_x:
                return float(np.clip(y, 0.0, 5.0))
            score = prev_y + (y - prev_y) * (rel - prev_x) / (x - prev_x)
            return float(np.clip(score, 0.0, 5.0))
        prev_x, prev_y = x, y
    return 5.0  # 超过最大断点 → 满分


def _score_adx_piecewise(adx: Optional[float],
                          plus_di: Optional[float],
                          minus_di: Optional[float],
                          cfg: AlarmConfig) -> float:
    """维3 ADX 强度分段映射 → [0, 5]（含方向降权）。

    分段（cfg.SCORE_ADX_BREAKPOINTS）：
        ADX ≤ 15  → 0 分（无趋势）
        ADX = 20  → 1 分（弱趋势启动）
        ADX = 25  → 3 分（趋势形成）
        ADX = 30  → 5 分（强趋势满分）
        ADX > 30  → 5 分（封顶）
        段间线性插值。

    方向降权（cfg.ADX_DOWN_PENALTY）：
        +DI > -DI（上涨趋势）→ 不降权，正常给分
        -DI > +DI（下跌趋势）→ 分数 × 降权系数（默认 0.1）
        DI 缺失 → 视为下跌，触发降权（保守处理）

    实战：ADX 高只代表"趋势强"不辨方向；强下跌也拿高分不合实战，
    降权后下跌趋势最多拿 0.5 分（ADX≥30 × 5 × 0.1）。
    """
    if adx is None or not np.isfinite(adx):
        return 0.0
    bps = cfg.SCORE_ADX_BREAKPOINTS
    if not bps:
        return 0.0
    adx_val = float(adx)
    prev_x, prev_y = 0.0, 0.0
    strength = 0.0
    for x, y in bps:
        if adx_val <= x:
            if x == prev_x:
                strength = float(np.clip(y, 0.0, 5.0))
                break
            strength = prev_y + (y - prev_y) * (adx_val - prev_x) / (x - prev_x)
            strength = float(np.clip(strength, 0.0, 5.0))
            break
        prev_x, prev_y = x, y
    else:
        # 超过最大断点 → 末位封顶
        strength = float(np.clip(bps[-1][1], 0.0, 5.0))
    # 方向降权
    pdi = plus_di if (plus_di is not None and np.isfinite(plus_di)) else 0.0
    mdi = minus_di if (minus_di is not None and np.isfinite(minus_di)) else 0.0
    if pdi > mdi:
        return strength
    return float(strength * cfg.ADX_DOWN_PENALTY)


def _classify_bench_state(bench_close: pd.Series,
                          cfg: AlarmConfig) -> tuple[str, str]:
    """大盘均线状态分类（B5：基准收盘价 vs MA20/MA60）。

    判定规则：
        above : close > MA20 且 close > MA60 → 多头格局（系统性机会）
        below : close < MA20 且 close < MA60 → 空头格局（系统性下跌，全面防守）
        mixed : 其他（震荡/过渡）
        unknown : 数据不足

    Returns:
        (state, reason)  state ∈ {'above','below','mixed','unknown'}
    """
    if bench_close is None or bench_close.empty:
        return 'unknown', '基准收盘序列为空'
    s = pd.to_numeric(bench_close, errors='coerce').dropna()
    ma_short_p = cfg.BENCH_MA_SHORT
    ma_long_p = cfg.BENCH_MA_LONG
    if len(s) < ma_long_p:
        return 'unknown', f'基准数据不足{ma_long_p}行（仅{len(s)}行）'
    ma_short = float(s.iloc[-ma_short_p:].mean())
    ma_long = float(s.iloc[-ma_long_p:].mean())
    last = float(s.iloc[-1])
    above_short = last > ma_short
    above_long = last > ma_long
    if above_short and above_long:
        return 'above', f'基准{last:.1f}>MA{ma_short_p}({ma_short:.1f})&MA{ma_long_p}({ma_long:.1f}) 多头格局'
    if (not above_short) and (not above_long):
        return 'below', f'基准{last:.1f}<MA{ma_short_p}({ma_short:.1f})&MA{ma_long_p}({ma_long:.1f}) 空头格局'
    return 'mixed', f'基准{last:.1f} 介于 MA{ma_short_p}({ma_short:.1f})/MA{ma_long_p}({ma_long:.1f}) 震荡'


def _classify_volatility(volatility: Optional[float],
                         cfg: AlarmConfig) -> tuple[str, str]:
    """波动率分级（B6：ATR14/close 百分比）。

    判定规则：
        high   : > VOLATILITY_HIGH_THRESHOLD(3.0%) → 高波动（加仓刹车）
        low    : < VOLATILITY_LOW_THRESHOLD(1.5%) → 低波动（健康趋势）
        medium : 中间
        unknown: 数据不足

    Returns:
        (level, reason)  level ∈ {'high','medium','low','unknown'}
    """
    if volatility is None or not np.isfinite(volatility):
        return 'unknown', '波动率数据不足'
    v = float(volatility)
    high_t = cfg.VOLATILITY_HIGH_THRESHOLD
    low_t = cfg.VOLATILITY_LOW_THRESHOLD
    if v > high_t:
        return 'high', f'ATR/close={v:.2f}%>{high_t:.1f}% 高波动'
    if v < low_t:
        return 'low', f'ATR/close={v:.2f}%<{low_t:.1f}% 低波动'
    return 'medium', f'ATR/close={v:.2f}% 中等波动'


def _resolve_action(quadrant: Optional[str], total_score: float,
                    bench_state: str, volatility: Optional[float],
                    cfg: AlarmConfig,
                    rebound_level: str = '无',
                    improvement_signal: bool = False) -> tuple:
    """生成双列操作参考（A1 象限地板/天花板 + B5 大盘均线 + B6 波动率 + C7 分级
    + 超卖反弹机会提示/降级保护 + 趋势改善信号降级保护）。

    返回 (with_position, without_position) 二元组：
        with_position:    有仓位者的动作（持有/减仓/清仓/观望）
        without_position: 无仓位者的动作（观望/可轻仓/可建仓/回避）

    超卖反弹修正（不覆盖象限地板/天花板，只做"机会提示"和"降级保护"）：
        大盘 below 时三级降一级（确认→候选，候选→观察），避免接飞刀。
        🟡轮动初期 + 候选 → 无仓：观望→可轻仓（二级信号可轻仓试）
        🟡轮动初期 + 确认 → 无仓：可轻仓→可建仓（需大盘非 below）

    趋势改善信号（improvement_signal）降级保护：
        ADX斜率转正 + DI+上穿DI- + RS动量加速度转正 → 预警改善。
        🟠退潮预警 + 改善信号 → 有仓：减仓→持有观察（不急于减仓，观察拐点是否成立）
        仅作降级保护，不改变象限地板/天花板，不预测拐点只减少误判。

    超卖反弹修正（续）：
        🟠退潮预警 + 反弹确认 + 大盘 above → 有仓：减仓→持有观察
        🔴滞后回避 + 任何 → 不变（超卖只是"别追空"，不买入）
        🟢领涨主线 + 任何 → 不变（超卖多为回调，等站回 MA20）

    quadrant 为 None 时（兜底，如单元测试直接调）：回退到总分三档。
    """
    # 兜底：象限缺失 → 总分三档
    if quadrant is None:
        if total_score > cfg.SCORE_STRONG_THRESHOLD:
            return ('持有', '可建仓')
        elif total_score >= cfg.SCORE_ATTENTION_THRESHOLD:
            return ('持有', '可轻仓')
        return ('减仓', '回避')

    # 波动率分级（B6 风险刹车信号）
    vol_level, _ = _classify_volatility(volatility, cfg)
    is_high_vol = (vol_level == 'high')
    is_low_vol = (vol_level == 'low')

    base_with = '减仓'
    base_without = '回避'

    # ---- 滞后回避：C7 分级（清仓 / 减仓）----
    if quadrant == '滞后回避':
        if bench_state in ('below', 'mixed'):
            base_with, base_without = '清仓', '回避'  # 破位 + 大盘弱 → 有仓清仓，无仓回避
        else:  # above/unknown：大盘强但品种破位 → 减仓
            base_with, base_without = '减仓', '回避'

    # ---- 退潮预警：天花板=持有，低分/空头→减仓 ----
    elif quadrant == '退潮预警':
        if bench_state in ('below', 'mixed'):
            base_with, base_without = '减仓', '观望'  # 趋势转弱 + 大盘弱
        elif is_high_vol:
            base_with, base_without = '减仓', '观望'  # 高波动加速减仓
        elif total_score > cfg.SCORE_STRONG_THRESHOLD:
            base_with, base_without = '持有', '观望'  # 高分但退潮，有仓持有，无仓不追
        else:
            base_with, base_without = '减仓', '观望'

    # ---- 轮动初期：C7 分级（观望 / 可轻仓）----
    elif quadrant == '轮动初期':
        if bench_state in ('below', 'mixed'):
            base_with, base_without = '观望', '观望'  # 方向不明，有仓不动，无仓不买
        elif is_high_vol:
            base_with, base_without = '观望', '观望'  # 多头但波动大，先观察
        elif total_score >= cfg.SCORE_ATTENTION_THRESHOLD:
            base_with, base_without = '持有', '可轻仓'  # 大盘顺风+由弱转强
        else:
            base_with, base_without = '观望', '观望'

    # ---- 领涨主线：地板=可建仓，高分→持有/加仓 ----
    elif quadrant == '领涨主线':
        if bench_state == 'below':
            base_with, base_without = '持有', '观望'  # 空头格局天花板=持有，无仓不新开
        elif bench_state == 'mixed':
            base_with, base_without = '持有', '可轻仓'  # 震荡不加仓，无仓可小仓试
        elif is_high_vol:
            base_with, base_without = '持有', '观望'  # 高波动刹车，不加仓，无仓先看
        elif is_low_vol and total_score > cfg.SCORE_STRONG_THRESHOLD:
            base_with, base_without = '持有', '可建仓'  # 低波动健康趋势
        elif total_score > cfg.SCORE_STRONG_THRESHOLD:
            base_with, base_without = '持有', '可建仓'
        else:
            base_with, base_without = '持有', '可轻仓'  # 地板：有仓持有，无仓可小仓试

    # ---- 超卖反弹修正（不覆盖地板/天花板，只做机会提示/降级保护）----
    # 大盘 below 时三级降一级（确认→候选，候选→观察），避免接飞刀
    eff_level = rebound_level
    if bench_state == 'below':
        if eff_level == '确认':
            eff_level = '候选'
        elif eff_level == '候选':
            eff_level = '观察'

    # 🟡轮动初期 + 超卖：候选→无仓可轻仓，确认→无仓可建仓（需非 below）
    if quadrant == '轮动初期':
        if eff_level == '候选' and base_without == '观望':
            base_without = '可轻仓'  # 二级信号可轻仓试
        elif eff_level == '确认' and bench_state != 'below':
            if base_without in ('观望', '可轻仓'):
                base_without = '可建仓'  # 三级信号+大盘非空头 → 可建仓
    # 🟠退潮预警 + 超卖确认 + 大盘 above：有仓减仓→持有观察
    elif quadrant == '退潮预警':
        if eff_level == '确认' and bench_state == 'above' and base_with == '减仓':
            base_with = '持有'  # 三级信号+大盘多头 → 减仓降为持有观察
    # 🔴滞后回避 + 超卖：不变（超卖只是"别追空"，不买入）
    # 🟢领涨主线 + 超卖：不变（超卖多为回调，等站回 MA20）

    # ---- 趋势改善信号降级保护（不预测拐点，只减少误判）----
    # ADX斜率转正 + DI+上穿DI- + RS动量加速度转正 → 预警改善
    # 🟠退潮预警 + 改善信号 → 有仓减仓→持有观察（不急于减仓，观察拐点是否成立）
    if improvement_signal and quadrant == '退潮预警' and base_with == '减仓':
        base_with = '持有'  # 降级保护：持有观察而非减仓

    return (base_with, base_without)


def calc_5d_score(etf_df: pd.DataFrame, rs_ratio_last: float,
                  rs_momentum_last: float, rs_momentum_series: pd.Series,
                  cfg: AlarmConfig,
                  quadrant: Optional[str] = None,
                  bench_state: str = 'unknown',
                  volatility: Optional[float] = None) -> dict:
    """5 维评分卡。返回各维得分 + 加权综合得分 + 操作参考。

    Args:
        etf_df: ETF 日线（含 high/low/close/amount）
        rs_ratio_last: 最新 RS-Ratio
        rs_momentum_last: 最新 RS-Momentum
        rs_momentum_series: RS-Momentum 序列（用于维2 二阶差分）
        cfg: AlarmConfig
        quadrant: RRG 象限标签（'领涨主线'/'轮动初期'/'退潮预警'/'滞后回避'），
                  None 时操作参考回退到总分三档兜底
        bench_state: 大盘均线状态（'above'/'below'/'mixed'/'unknown'），
                     用于操作参考的大盘格局修正
        volatility: ETF 波动率（ATR14/close 百分比），用于操作参考的风险刹车（B6）；
                    不进入加权总分，仅作为加仓决策的修正因子

    Returns:
        dict 结构：
        {
            'relative_momentum':     float,  # 维1 归一化分（分段映射）
            'momentum_acceleration': float,  # 维2 归一化分
            'adx_trend':             float,  # 维3 归一化分
            'capital_attention':     float,  # 维4 归一化分
            'crowding_penalty':      float,  # 维5 拥挤折扣后得分
            'crowding_penalty_applied': bool, # 是否触发拥挤度扣分
            'total_score':           float,  # 加权综合得分 [0, 5]
            'action_hint':           str,    # 操作参考（象限地板/天花板+大盘+波动率）
            'bench_state':           str,    # 大盘均线状态（便于报告展示）
            'volatility':            float | None,  # ATR14/close 百分比
            'volatility_level':      str,    # 波动率分级（high/medium/low/unknown）
        }
    """
    w = cfg.SCORE_WEIGHTS

    # ---- 维1 相对动量（25%）：分段归一化（A2）----
    # 旧线性映射 SCORE_REL_MOM_FULL 已废弃；改用 SCORE_REL_MOM_BREAKPOINTS 分段
    rel_mom_score = _normalize_rel_mom_piecewise(rs_ratio_last, cfg)

    # ---- 维2 动量加速度（20%）：RS-Momentum 二阶差分 + 横截面 z-score ----
    # 二阶差分 = mom[-1] - 2*mom[-2] + mom[-3]（动量的变化率）
    # 旧版用绝对阈值 5% 满分，但不同资产的二阶差分波动幅度不同（芯片天然比银行大），
    # 5% 对芯片可能常见、对银行可能罕见 → 系统性偏袒高波动品种。
    # 改用横截面 z-score 标准化：在批量层（apply_cross_sectional_mom_acc）对全篮子
    # 做标准差归一化，再映射到 [0, 5]。
    # 此处先保留原始值 + 初步分数（旧逻辑），横截面修正由后处理函数覆盖。
    mom_acc_raw = 0.0
    if rs_momentum_series is not None and len(rs_momentum_series) >= 3:
        vals = rs_momentum_series.dropna()
        if len(vals) >= 3:
            m1 = float(vals.iloc[-1])
            m2 = float(vals.iloc[-2])
            m3 = float(vals.iloc[-3])
            mom_acc_raw = m1 - 2 * m2 + m3
    # 初步分数：用旧绝对阈值（5% 满分）做兜底，横截面后处理会覆盖
    mom_acc_score = _normalize(mom_acc_raw, cfg.SCORE_MOM_ACC_FULL)
    if mom_acc_raw < 0:
        mom_acc_score = 0.0
    mom_acc_zscore = None  # 横截面后处理填充

    # ---- 维3 ADX 趋势强度（20%）：ETF 自身 ADX(14) + 方向降权 ----
    # ADX 只代表趋势强度不辨方向，强下跌也拿高分不合实战。
    # 改进：分段映射（ADX≤15→0 / 20→1 / 25→3 / 30→5）+ 方向降权（-DI>+DI 乘 0.1）
    # 下跌趋势最多 0.5 分，只奖励"有方向的上涨趋势"。
    adx_info = calc_adx_etf(etf_df, cfg)
    if adx_info is None or adx_info.get('adx') is None:
        adx_score = 0.0
        adx_raw = None
        adx_direction = None
    else:
        adx_raw = adx_info['adx']
        adx_direction = adx_info.get('direction', 'up')
        adx_score = _score_adx_piecewise(
            adx_raw, adx_info.get('plus_di'), adx_info.get('minus_di'), cfg)

    # ---- 维4 资金关注度（20%）：成交额历史分位 ----
    # 旧版用 MA5/MA20 量比（calc_turnover_ratio），对低换手品种（银行/豆粕）
    # 天然偏低且无法跨品种比较。改用成交额历史分位（calc_amount_percentile），
    # 反映"今日量能相对自身历史的热度"，不受品种量级影响。
    amount_pct = calc_amount_percentile(etf_df, cfg)
    if amount_pct is None:
        cap_att_score = 0.0
    else:
        # 分位分段映射 → [0, 5]：
        #   <20% → 1分（缩量）；50% → 3分（正常）；>80% → 5分（放量）
        low_t = cfg.SCORE_CAP_ATTENTION_LOW_PCT
        high_t = cfg.SCORE_CAP_ATTENTION_HIGH_PCT
        if amount_pct < low_t:
            # 0~20% → 0~1分线性
            cap_att_score = float(np.interp(amount_pct, [0, low_t], [0, 1]))
        elif amount_pct < 50.0:
            # 20%~50% → 1~3分线性
            cap_att_score = float(np.interp(amount_pct, [low_t, 50.0], [1, 3]))
        elif amount_pct < high_t:
            # 50%~80% → 3~5分线性
            cap_att_score = float(np.interp(amount_pct, [50.0, high_t], [3, 5]))
        else:
            cap_att_score = 5.0  # >80% 满分

    # ---- 维5 拥挤度折扣（15%）：只惩罚"涨过头 + 极度放量"，不惩罚"跌过头" ----
    # 旧版用 |close-MA20| 绝对值，下跌偏离（超卖）也会扣分——但超卖是机会而非拥挤。
    # 改为方向区分：
    #   上涨偏离（close > MA20 且 偏离>2×ATR）→ 扣分（拥挤/回调风险）
    #   下跌偏离（close < MA20 且 偏离>2×ATR）→ 不扣分，标记 oversold_flag（超卖观察）
    #   成交额分位 > 90%（极度放量）→ 扣分（拥挤）
    crowding_score = 5.0
    crowding_penalty_applied = False
    crowding_reason = ''
    oversold_flag = False       # 超卖标记（不进总分，仅观察）
    oversold_deviation = 0.0    # 超卖偏离幅度（MA20-close）/close×100

    if (volatility is not None and np.isfinite(volatility)
            and 'close' in etf_df.columns and len(etf_df) >= 20):
        close_series = etf_df['close'].astype(float).dropna()
        if len(close_series) >= 20:
            ma20_close = float(close_series.iloc[-20:].mean())
            last_close = float(close_series.iloc[-1])
            if last_close > 0 and ma20_close > 0:
                threshold_pct = cfg.CROWDING_PRICE_DEVIATION_ATR_MULT * volatility
                if last_close > ma20_close:
                    # 上涨偏离 → 拥挤扣分
                    price_dev_pct = (last_close - ma20_close) / last_close * 100.0
                    if price_dev_pct > threshold_pct:
                        crowding_score = max(0.0, 5.0 - cfg.CROWDING_PENALTY)
                        crowding_penalty_applied = True
                        crowding_reason = f'上涨偏离{price_dev_pct:.1f}%>{threshold_pct:.1f}%({cfg.CROWDING_PRICE_DEVIATION_ATR_MULT}×ATR)'
                else:
                    # 下跌偏离 → 超卖标记（不扣分）
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

    # ---- 加权综合得分 ----
    total = (
        rel_mom_score        * w.get('relative_momentum', 0.25)
        + mom_acc_score      * w.get('momentum_acceleration', 0.20)
        + adx_score          * w.get('adx_trend', 0.20)
        + cap_att_score      * w.get('capital_attention', 0.20)
        + crowding_score     * w.get('crowding_penalty', 0.15)
    )
    total = float(np.clip(total, 0.0, 5.0))

    # ---- 操作参考（双列：有仓位 / 无仓位）----
    action_with, action_without = _resolve_action(quadrant, total, bench_state, volatility, cfg)

    # 波动率分级（便于报告展示）
    vol_level, _ = _classify_volatility(volatility, cfg)

    return {
        'relative_momentum': round(rel_mom_score, 2),
        'momentum_acceleration': round(mom_acc_score, 2),
        'mom_acc_raw': round(mom_acc_raw, 4),
        'mom_acc_zscore': mom_acc_zscore,
        'mom_acc_cross_sectional': False,
        'adx_trend': round(adx_score, 2),
        'adx_raw': round(adx_raw, 1) if adx_raw is not None else None,
        'adx_direction': adx_direction,
        'capital_attention': round(cap_att_score, 2),
        'crowding_penalty': round(crowding_score, 2),
        'crowding_penalty_applied': crowding_penalty_applied,
        'crowding_reason': crowding_reason,
        'oversold_flag': oversold_flag,
        'oversold_deviation': round(oversold_deviation, 2),
        'rebound_level': '无',  # 默认值，由 analyze_single_etf 注入实际值
        'rebound_note': '',
        'amount_percentile': amount_pct,
        'volume_ratio': round(calc_turnover_ratio(etf_df), 3) if etf_df is not None else None,
        'total_score': round(total, 2),
        'action_with': action_with,
        'action_without': action_without,
        'action_hint': f'{action_with}/{action_without}',  # 兼容旧字段
        'bench_state': bench_state,
        'volatility': round(volatility, 2) if volatility is not None and np.isfinite(volatility) else None,
        'volatility_level': vol_level,
    }


def apply_cross_sectional_mom_acc(results: list, cfg: AlarmConfig) -> None:
    """横截面 z-score 标准化维2 动量加速度（原地修改 results）。

    解决"5% 绝对阈值对不同资产含义不同"问题：芯片的二阶差分天然比银行大，
    5% 阈值对芯片可能常见、对银行可能罕见 → 系统性偏袒高波动品种。

    横截面 z-score 做法：
        1. 收集全篮子所有数据充足 ETF 的 mom_acc_raw（二阶差分原始值）
        2. 计算 mean / std
        3. 每只 ETF 的 z-score = (raw - mean) / std
        4. 映射到 [0, 5]：score = clip(center + z * slope, 0, 5)
           z=0 → center（加速度处于横截面均值）
           z=+1 → center+slope（比均值高1个标准差，满分）
           z=-1 → center-slope（比均值低1个标准差，零分）
        5. 重算 total_score 和 action_hint

    兜底：若有效样本 < 3 或 std≈0（全相同），不做横截面修正，保留初步分数。

    Args:
        results: analyze_single_etf 返回的 list[dict]，原地修改
        cfg: AlarmConfig
    """
    # 1. 收集有效样本
    valid = []
    for r in results:
        if not r.get('data_sufficient'):
            continue
        s5 = r.get('score_5d')
        if s5 is None:
            continue
        raw = s5.get('mom_acc_raw')
        if raw is None or not np.isfinite(raw):
            continue
        valid.append((r, s5, float(raw)))

    if len(valid) < 3:
        return  # 样本不足，不做横截面

    raws = np.array([v[2] for v in valid])
    mean = float(np.mean(raws))
    std = float(np.std(raws))

    if std < 1e-8:
        return  # 全相同，不做

    center = cfg.SCORE_MOM_ACC_ZSCORE_CENTER
    slope = cfg.SCORE_MOM_ACC_ZSCORE_SLOPE
    w = cfg.SCORE_WEIGHTS

    for r, s5, raw in valid:
        z = (raw - mean) / std
        new_mom_acc = float(np.clip(center + z * slope, 0.0, 5.0))
        old_mom_acc = s5.get('momentum_acceleration', 0.0)
        delta = (new_mom_acc - old_mom_acc) * w.get('momentum_acceleration', 0.20)

        # 重算 total
        old_total = s5.get('total_score', 0.0)
        new_total = float(np.clip(old_total + delta, 0.0, 5.0))

        s5['momentum_acceleration'] = round(new_mom_acc, 2)
        s5['mom_acc_zscore'] = round(z, 2)
        s5['mom_acc_cross_sectional'] = True
        s5['total_score'] = round(new_total, 2)

        # 重算 action_hint（total 变了，操作参考可能变）
        quadrant = r.get('quadrant')
        bench_state = s5.get('bench_state', 'unknown')
        volatility = s5.get('volatility')  # 已是百分比格式（ATR/close*100）
        rebound_level = s5.get('rebound_level', '无')
        improvement_signal = s5.get('improvement_signal', False)
        new_with, new_without = _resolve_action(
            quadrant, new_total, bench_state, volatility, cfg,
            rebound_level=rebound_level,
            improvement_signal=improvement_signal,
        )
        s5['action_with'] = new_with
        s5['action_without'] = new_without
        s5['action_hint'] = f'{new_with}/{new_without}'


def _classify_oversold_rebound(
    etf_df: pd.DataFrame,
    rs_ratio_series: pd.Series,
    rs_momentum_series: pd.Series,
    adx_info: Optional[dict],
    amount_pct_series: Optional[list],
    quadrant_today: Optional[str],
    quadrant_prev: Optional[str],
    bench_state: str,
    cfg: AlarmConfig,
) -> tuple:
    """超卖反弹分级 + 时间约束（不进总分，仅作操作参考的机会提示/降级保护）。

    三级条件：
        一级（观察）：close < MA20 且 MA20 - close > 2×ATR
        二级（候选）：一级 + 任一
            - RS-Momentum > 0（今日）
            - 象限改善（昨日🔴→今日🟡，或其他改善）
            - 成交额分位从<20%回升到>50%
        三级（确认）：二级 + 任一
            - RS-Ratio 回升（今日>昨日）且 RS-Momentum > 0 连续 2 日
            - +DI 上穿 -DI（adx_info['di_cross_up']）
            - 成交额分位 > 60% 且价格站回 MA5

    时间约束（OVERSOLD_VALID_DAYS，默认 5 日）：
        - 超卖触发后 N 日内未出现 RS-Momentum > 0 → 自动失效，level 置"无"
        - 中途出现 RS-Momentum > 0 → 计时器归零（重新计数）
        - 窗口起点已超卖 → truncated=True，不自动过期（保守处理）
        - 过期后再次 RS-Momentum > 0 → 重新从观察/候选开始
        方案 B（无状态重算）：用当前数据窗口回算，可回测、可复现，无需跨日持久化。

    Returns:
        (rebound_level, rebound_note, oversold_days, oversold_expired, truncated)
        rebound_level: '无' / '观察' / '候选' / '确认'
        rebound_note: 触发原因简述
        oversold_days: 信号持续天数（从最近一次 RS-Momentum>0 或 streak 起点算）
        oversold_expired: 是否已过期（对外置"无"，内部保留此字段）
        truncated: 窗口起点已超卖（保守不过期）
    """
    # ---- 一级：判断今日是否超卖 + 历史超卖序列（用于时间约束） ----
    oversold = False
    ma20_close = None
    last_close = None
    oversold_days = 0
    oversold_expired = False
    truncated = False

    if etf_df is None or 'close' not in etf_df.columns or len(etf_df) < 20:
        return ('无', '', 0, False, False)

    close_series = etf_df['close'].astype(float).dropna()
    if len(close_series) < 20:
        return ('无', '', 0, False, False)

    ma20_close = float(close_series.iloc[-20:].mean())
    last_close = float(close_series.iloc[-1])
    volatility = calc_volatility_ratio(etf_df, cfg)
    if (last_close <= 0 or ma20_close <= 0 or volatility is None
            or not np.isfinite(volatility)):
        return ('无', '', 0, False, False)

    threshold_pct = cfg.OVERSOLD_ATR_MULT * volatility
    if last_close >= ma20_close:
        # 今日不超卖
        return ('无', '', 0, False, False)
    dev_pct = (ma20_close - last_close) / last_close * 100.0
    if dev_pct <= threshold_pct:
        # 偏离不足
        return ('无', '', 0, False, False)
    # 今日超卖
    oversold = True

    # ---- 时间约束：回算 streak + effective_days ----
    # 用 ATR_pct 全序列逐日判断超卖，找到当前 streak 起点
    atr_pct_series = calc_atr_pct_series(etf_df, cfg)
    # MA20 rolling 序列（与 close_series 等长，dropna 后）
    close_arr = close_series.values
    n = len(close_arr)
    # 构建 MA20 数组（不足 20 日处为 NaN）
    ma20_arr = np.full(n, np.nan)
    for i in range(19, n):
        ma20_arr[i] = float(np.mean(close_arr[i - 19:i + 1]))

    # 构建逐日超卖标志
    oversold_flags = np.zeros(n, dtype=bool)
    if atr_pct_series is not None:
        atr_arr = atr_pct_series.reindex(close_series.index).values
        for i in range(n):
            if (i >= 19 and np.isfinite(ma20_arr[i]) and ma20_arr[i] > 0
                    and close_arr[i] < ma20_arr[i]
                    and np.isfinite(atr_arr[i]) and atr_arr[i] > 0):
                dev_i = (ma20_arr[i] - close_arr[i]) / close_arr[i] * 100.0
                thr_i = cfg.OVERSOLD_ATR_MULT * atr_arr[i]
                if dev_i > thr_i:
                    oversold_flags[i] = True

    # 今日索引（close_series 末位）
    today_idx = n - 1
    # 回算 streak 起点：从今日往前找连续超卖
    streak_start = today_idx
    while streak_start > 0 and oversold_flags[streak_start - 1]:
        streak_start -= 1
    # truncated：streak 起点在窗口边界（无法判断更早是否已超卖）
    # MA20 需要 20 个点（index 19 起），ATR 需要 period+1 个点（index period 起）
    # 两者都可用之前，无法判断超卖 → streak 起点在此区域 = truncated
    warmup_end = max(19, cfg.VOLATILITY_PERIOD)
    truncated = (streak_start <= warmup_end)

    # effective_days：从最近一次 RS-Momentum > 0 之后计数
    # 若 streak 内无 RS-Momentum > 0，则从 streak 起点计数
    rs_mom_arr = None
    if rs_momentum_series is not None:
        rs_mom_clean = rs_momentum_series.dropna()
        if len(rs_mom_clean) > 0:
            # 对齐到 close_series 的索引（rs_mom 可能短于 close）
            rs_mom_arr = np.full(n, np.nan)
            mom_idx = 0
            for i in range(n):
                # 简化：rs_mom 末位对应 close 末位，往前对齐
                if i >= n - len(rs_mom_clean):
                    rs_mom_arr[i] = float(rs_mom_clean.iloc[mom_idx])
                    mom_idx += 1

    # 找 streak 内最近一次 rs_mom > 0
    last_positive_idx = -1  # -1 表示 streak 内无 rs_mom > 0
    if rs_mom_arr is not None:
        for i in range(streak_start, today_idx + 1):
            if np.isfinite(rs_mom_arr[i]) and rs_mom_arr[i] > 0:
                last_positive_idx = i

    if last_positive_idx >= 0:
        # 有 RS-Momentum > 0 → 计时器归零，从该日算起
        effective_days = today_idx - last_positive_idx
    else:
        # 无 RS-Momentum > 0 → 从 streak 起点算
        effective_days = today_idx - streak_start + 1
    oversold_days = effective_days

    # 过期判定：effective_days > OVERSOLD_VALID_DAYS 且非 truncated
    if not truncated and effective_days > cfg.OVERSOLD_VALID_DAYS:
        oversold_expired = True
        return ('无', f'超卖已失效（{effective_days}/{cfg.OVERSOLD_VALID_DAYS}日）',
                effective_days, True, truncated)

    # ---- 二级条件判断（今日） ----
    rs_mom_today = None
    rs_mom_prev = None
    if rs_momentum_series is not None and len(rs_momentum_series) >= 2:
        vals = rs_momentum_series.dropna()
        if len(vals) >= 2:
            rs_mom_today = float(vals.iloc[-1])
            rs_mom_prev = float(vals.iloc[-2])

    # 条件2a：RS-Momentum > 0（今日）
    mom_positive = rs_mom_today is not None and rs_mom_today > 0

    # 条件2b：象限改善（昨日 vs 今日）
    quadrant_improved = False
    if quadrant_prev and quadrant_today:
        rank = {'滞后回避': 0, '轮动初期': 1, '退潮预警': 1, '领涨主线': 2}
        prev_rank = rank.get(quadrant_prev, 0)
        today_rank = rank.get(quadrant_today, 0)
        quadrant_improved = today_rank > prev_rank

    # 条件2c：成交额分位从<20%回升到>50%
    amount_recovered = False
    if amount_pct_series is not None and len(amount_pct_series) >= 2:
        old_pct = amount_pct_series[0]
        new_pct = amount_pct_series[-1]
        amount_recovered = (old_pct < cfg.OVERSOLD_AMOUNT_LOW_PCT
                            and new_pct > cfg.OVERSOLD_AMOUNT_HIGH_PCT)

    is_level2 = mom_positive or quadrant_improved or amount_recovered

    # ---- 三级条件判断（今日） ----
    # 条件3a：RS-Ratio 回升 + RS-Momentum > 0 连续 2 日
    rs_ratio_recovered = False
    if (rs_ratio_series is not None and rs_momentum_series is not None
            and len(rs_ratio_series) >= 2 and len(rs_momentum_series) >= 2):
        rs_ratio_vals = rs_ratio_series.dropna()
        rs_mom_vals = rs_momentum_series.dropna()
        if len(rs_ratio_vals) >= 2 and len(rs_mom_vals) >= 2:
            rs_ratio_up = float(rs_ratio_vals.iloc[-1]) > float(rs_ratio_vals.iloc[-2])
            mom_pos_2d = (float(rs_mom_vals.iloc[-1]) > 0
                          and float(rs_mom_vals.iloc[-2]) > 0)
            rs_ratio_recovered = rs_ratio_up and mom_pos_2d

    # 条件3b：+DI 上穿 -DI
    di_cross_up = False
    if adx_info is not None:
        di_cross_up = bool(adx_info.get('di_cross_up', False))

    # 条件3c：成交额分位 > 60% 且价格站回 MA5
    amount_and_price = False
    if (etf_df is not None and 'close' in etf_df.columns
            and amount_pct_series is not None and len(amount_pct_series) >= 1):
        close_s = etf_df['close'].astype(float).dropna()
        if len(close_s) >= 5:
            ma5 = float(close_s.iloc[-5:].mean())
            if last_close is not None and last_close > ma5:
                today_pct = amount_pct_series[-1]
                if today_pct > cfg.REBOUND_AMOUNT_CONFIRM_PCT:
                    amount_and_price = True

    is_level3 = rs_ratio_recovered or di_cross_up or amount_and_price

    # ---- 组装 note ----
    note_parts = []
    if mom_positive:
        note_parts.append('RS动量转正')
    if quadrant_improved:
        note_parts.append(f'象限{quadrant_prev}→{quadrant_today}')
    if amount_recovered:
        note_parts.append('量能回升')

    if not is_level2:
        return ('观察', f'超卖观察（偏离{dev_pct:.1f}%，{effective_days}/{cfg.OVERSOLD_VALID_DAYS}日）',
                effective_days, False, truncated)
    elif is_level3:
        level3_parts = []
        if rs_ratio_recovered:
            level3_parts.append('RS-Ratio回升+动量连续2日正')
        if di_cross_up:
            level3_parts.append('+DI上穿-DI')
        if amount_and_price:
            level3_parts.append('量能>60%+站回MA5')
        note = f'超卖+{",".join(note_parts)}; 确认:{",".join(level3_parts)}'
        return ('确认', note, effective_days, False, truncated)
    else:
        note = f'超卖+{",".join(note_parts)}'
        return ('候选', note, effective_days, False, truncated)



# ====================================================================
# 3.5 趋势/RS 健康度（拐点预警辅助，不进总分）
# ====================================================================

def _compute_health(etf_df: pd.DataFrame,
                    bench_df: pd.DataFrame,
                    adx_info: Optional[dict],
                    rs_mom_series: pd.Series,
                    cfg: AlarmConfig) -> dict:
    """计算趋势健康分、RS 健康分及预警改善信号（不进总分，仅作拐点辅助）。

    新增字段（把 ADX、RS 从"死值"变成"动态信息"）：
        - adx_slope: ADX - ADX.shift(N)，趋势强度增强/减弱
        - di_diff: +DI - -DI，多空谁占优
        - rs_mom_accel: RS_Mom - RS_Mom.shift(N)，相对强度加速/减速
        - multi_period_rs: {5: rs, 20: rs, 60: rs}，多周期相对强度
        - volume_ratio: Volume / MA_N(Volume)，放量/缩量
        - trend_health: 0~5 趋势健康分
        - rs_health: 0~5 相对强度健康分
        - improvement_signal: ADX斜率转正 + DI+上穿DI- + RS动量加速度转正 → 预警改善

    Args:
        etf_df: ETF 日线（已对齐基准）
        bench_df: 基准日线（已对齐 ETF）
        adx_info: calc_adx_etf 返回值（含 adx_slope/di_diff/di_cross_up）
        rs_mom_series: RS-Momentum 序列
        cfg: AlarmConfig

    Returns:
        dict with the fields above
    """
    out = {
        'adx_slope': None,
        'di_diff': None,
        'di_cross_up': False,
        'rs_mom_accel': None,
        'multi_period_rs': None,
        'volume_ratio': None,
        'trend_health': 0.0,
        'rs_health': 0.0,
        'improvement_signal': False,
    }

    if adx_info is not None:
        out['adx_slope'] = adx_info.get('adx_slope')
        out['di_diff'] = adx_info.get('di_diff')
        out['di_cross_up'] = bool(adx_info.get('di_cross_up', False))

    # RS 动量加速度 = RS_Mom[-1] - RS_Mom[-N]
    if rs_mom_series is not None and len(rs_mom_series) >= 2:
        vals = rs_mom_series.dropna()
        n = cfg.RS_MOM_ACCEL_PERIOD
        if len(vals) > n:
            mom_today = float(vals.iloc[-1])
            mom_prev = float(vals.iloc[-1 - n])
            if np.isfinite(mom_today) and np.isfinite(mom_prev):
                out['rs_mom_accel'] = round(mom_today - mom_prev, 4)

    # 多周期相对强度
    mprs = calc_multi_period_rs(etf_df, bench_df, cfg.RS_MULTI_PERIODS)
    out['multi_period_rs'] = mprs

    # 成交量比
    out['volume_ratio'] = calc_volume_ratio(etf_df, cfg.VOLUME_RATIO_PERIOD)

    # ---- trend_health（0~5）----
    # 评分项（每项 +1，封顶 5）：
    #   ① ADX 斜率 > 0（趋势强度增强）
    #   ② di_diff > 0（+DI > -DI，多方占优）
    #   ③ di_cross_up（+DI 上穿 -DI，多方接管）
    #   ④ ADX > 25（有趋势存在）
    #   ⑤ direction == 'up'（上涨趋势）
    th = 0.0
    if out['adx_slope'] is not None and out['adx_slope'] > 0:
        th += 1.0
    if out['di_diff'] is not None and out['di_diff'] > 0:
        th += 1.0
    if out['di_cross_up']:
        th += 1.0
    if adx_info is not None and adx_info.get('adx') is not None and adx_info['adx'] > 25:
        th += 1.0
    if adx_info is not None and adx_info.get('direction') == 'up':
        th += 1.0
    out['trend_health'] = round(min(th, 5.0), 1)

    # ---- rs_health（0~5）----
    # 评分项（每项 +1，封顶 5）：
    #   ① rs_mom_accel > 0（RS 动量加速）
    #   ② rs_mom 最新值 > 0（RS 动量为正）
    #   ③ multi_period_rs[5] > 0（短期跑赢）
    #   ④ multi_period_rs[20] > 0（中期跑赢）
    #   ⑤ multi_period_rs[60] > 0（长期跑赢）
    rh = 0.0
    if out['rs_mom_accel'] is not None and out['rs_mom_accel'] > 0:
        rh += 1.0
    if rs_mom_series is not None and len(rs_mom_series) >= 1:
        v = rs_mom_series.dropna()
        if len(v) >= 1 and float(v.iloc[-1]) > 0:
            rh += 1.0
    if mprs is not None:
        for p in cfg.RS_MULTI_PERIODS:
            rv = mprs.get(p)
            if rv is not None and rv > 0:
                rh += 1.0
    out['rs_health'] = round(min(rh, 5.0), 1)

    # ---- improvement_signal（预警改善）----
    # ADX斜率转正 + DI+上穿DI- + RS动量加速度转正 → 三者同时满足
    cond_adx = out['adx_slope'] is not None and out['adx_slope'] > cfg.TREND_HEALTH_ADX_SLOPE_THRESHOLD
    cond_di = out['di_cross_up']
    cond_rs = out['rs_mom_accel'] is not None and out['rs_mom_accel'] > cfg.RS_MOM_ACCEL_THRESHOLD
    out['improvement_signal'] = bool(cond_adx and cond_di and cond_rs)

    return out


# ====================================================================
# 3.6 领先层（拐点提前嗅探，不进总分、不改操作矩阵）
# ====================================================================

def _compute_lead_score(etf_df: pd.DataFrame,
                        rs_mom_series: Optional[pd.Series],
                        cfg: AlarmConfig) -> dict:
    """计算领先预警分 lead_score（0~100），三类信号各 0~33 分。

    目标：提前嗅到可能的拐点。只用于观察名单，不直接交易。

    三类信号：
        ① 波动率压缩（0~33）：ATR_pct 历史分位 < LEAD_VOLATILITY_LOW_PCT
           或 BBW 历史分位 < LEAD_VOLATILITY_LOW_PCT → 变盘前夜
        ② 量价背离（0~33）：
           - 底部背离：价格创 window 新低，但 OBV 不创新低 → +33
           - 顶部背离：价格创 window 新高，但 OBV 不创新高 → +33
           （底部和顶部互斥，只取一个）
        ③ RS 动量背离（0~34）：
           - 价格创 window 新低，但 RS-Momentum 底部抬高（今日 RS-Mom > window 内最低 RS-Mom）→ +34

    Args:
        etf_df: ETF 日线（已对齐基准，含 high/low/close/volume）
        rs_mom_series: RS-Momentum 序列
        cfg: AlarmConfig

    Returns:
        dict: {
            'lead_score': 0~100,
            'vol_compression': bool,        # 波动率压缩触发
            'vol_pct': float|None,          # ATR_pct 历史分位
            'bbw_pct': float|None,          # BBW 历史分位
            'price_obv_divergence': str|None,  # 'bottom' / 'top' / None
            'rs_mom_divergence': bool,      # RS 动量背离触发
            'lead_note': str,               # 人类可读说明
        }
    """
    out = {
        'lead_score': 0,
        'vol_compression': False,
        'vol_pct': None,
        'bbw_pct': None,
        'price_obv_divergence': None,
        'rs_mom_divergence': False,
        'lead_note': '',
    }
    if etf_df is None or etf_df.empty:
        return out

    sub = etf_df[['close', 'high', 'low', 'volume']].astype(float).dropna()
    lookback = cfg.LEAD_LOOKBACK_DAYS
    low_pct = cfg.LEAD_VOLATILITY_LOW_PCT
    div_window = cfg.LEAD_DIVERGENCE_WINDOW

    # ---------- ① 波动率压缩 ----------
    vol_score = 0
    # ATR_pct 历史分位
    atr_pct_series = calc_atr_pct_series(etf_df, cfg)
    if atr_pct_series is not None:
        vals = atr_pct_series.dropna()
        if len(vals) >= 2:
            today_v = float(vals.iloc[-1])
            hist = vals.iloc[-lookback:] if len(vals) > lookback else vals
            vol_pct = float(np.sum(hist <= today_v) / len(hist) * 100.0)
            out['vol_pct'] = round(vol_pct, 1)
            if vol_pct < low_pct:
                vol_score += 17  # ATR 压缩
    # BBW 历史分位
    bbw_series = calc_bollinger_bandwidth(etf_df, cfg.LEAD_BOLLINGER_PERIOD, cfg.LEAD_BOLLINGER_STD)
    if bbw_series is not None:
        vals = bbw_series.dropna()
        if len(vals) >= 2:
            today_v = float(vals.iloc[-1])
            hist = vals.iloc[-lookback:] if len(vals) > lookback else vals
            bbw_pct = float(np.sum(hist <= today_v) / len(hist) * 100.0)
            out['bbw_pct'] = round(bbw_pct, 1)
            if bbw_pct < low_pct:
                vol_score += 16  # BBW 压缩
    out['vol_compression'] = vol_score > 0

    # ---------- ② 量价背离 ----------
    div_score = 0
    obv_series = calc_obv(etf_df)
    if obv_series is not None and len(obv_series) >= div_window + 1:
        obv_vals = obv_series.dropna()
        close_vals = sub['close']
        # 对齐索引
        common = obv_vals.index.intersection(close_vals.index)
        if len(common) >= div_window + 1:
            obv_aligned = obv_vals.loc[common]
            close_aligned = close_vals.loc[common]
            # 窗口内价格新低
            price_window = close_aligned.iloc[-(div_window + 1):]
            obv_window = obv_aligned.iloc[-(div_window + 1):]
            price_min_idx = price_window.idxmin()
            price_max_idx = price_window.idxmax()
            today_price = float(close_aligned.iloc[-1])
            today_obv = float(obv_aligned.iloc[-1])
            window_min_price = float(price_window.min())
            window_max_price = float(price_window.max())
            window_min_obv = float(obv_window.min())
            window_max_obv = float(obv_window.max())

            # 底部背离：今日价格创窗口新低，但 OBV 未创窗口新低
            if today_price <= window_min_price and today_obv > window_min_obv:
                out['price_obv_divergence'] = 'bottom'
                div_score = 33
            # 顶部背离：今日价格创窗口新高，但 OBV 未创窗口新高
            elif today_price >= window_max_price and today_obv < window_max_obv:
                out['price_obv_divergence'] = 'top'
                div_score = 33

    # ---------- ③ RS 动量背离 ----------
    rs_div_score = 0
    if rs_mom_series is not None and len(rs_mom_series) >= div_window + 1:
        mom_vals = rs_mom_series.dropna()
        if len(mom_vals) >= div_window + 1 and len(sub) >= div_window + 1:
            mom_window = mom_vals.iloc[-(div_window + 1):]
            today_mom = float(mom_window.iloc[-1])
            window_min_mom = float(mom_window.min())
            # 价格创窗口新低（用 close）但 RS-Momentum 底部抬高
            price_window_for_rs = sub['close'].iloc[-(div_window + 1):]
            today_price_rs = float(price_window_for_rs.iloc[-1])
            window_min_price_rs = float(price_window_for_rs.min())
            if today_price_rs <= window_min_price_rs and today_mom > window_min_mom:
                out['rs_mom_divergence'] = True
                rs_div_score = 34

    # ---------- 汇总 ----------
    total = vol_score + div_score + rs_div_score
    out['lead_score'] = int(round(min(total, 100)))

    # 生成 note
    notes = []
    if out['vol_compression']:
        parts = []
        if out['vol_pct'] is not None:
            parts.append(f"ATR分位{out['vol_pct']}%")
        if out['bbw_pct'] is not None:
            parts.append(f"BBW分位{out['bbw_pct']}%")
        notes.append("波动压缩(" + "/".join(parts) + ")")
    if out['price_obv_divergence'] == 'bottom':
        notes.append("OBV底背离")
    elif out['price_obv_divergence'] == 'top':
        notes.append("OBV顶背离")
    if out['rs_mom_divergence']:
        notes.append("RS动量底背离")
    out['lead_note'] = "；".join(notes) if notes else ""

    return out


# ====================================================================
# 3.7 同步层（拐点确认，过滤假反弹）
# ====================================================================

def _compute_confirm_score(etf_df: pd.DataFrame, cfg: AlarmConfig) -> dict:
    """计算同步确认分 confirm_score（0~100），5 个确认指标各 0~20 分。

    目标：领先信号出现后，等市场确认再行动。过滤假反弹。

    5 个确认指标（每项 0~20 分，合计 0~100）：
        ① MA20 确认（20）：价格站上 MA20（+10）且 MA20 斜率转正（+10）
        ② VWAP 确认（20）：价格站上 VWAP（+20）
        ③ 成交量确认（20）：今日量 > 20 日均量 × CONFIRM_VOLUME_MULT（+20）
        ④ 价格结构确认（20）：窗口内高低点抬高（N 字突破）（+20）
        ⑤ 板块宽度确认（20）：近 N 日上涨天数占比 > CONFIRM_BREADTH_THRESHOLD（+20）

    Args:
        etf_df: ETF 日线（已对齐基准，含 high/low/close/volume）
        cfg: AlarmConfig

    Returns:
        dict: {
            'confirm_score': 0~100,
            'ma_confirm': bool,           # MA 确认触发
            'price_above_ma20': bool,
            'ma20_slope_positive': bool,
            'vwap_confirm': bool,         # VWAP 确认触发
            'volume_confirm': bool,       # 成交量确认触发
            'price_structure_confirm': bool,  # 价格结构确认触发
            'breadth_confirm': bool,      # 板块宽度确认触发
            'breadth_ratio': float|None,  # 上涨天数占比
            'confirm_note': str,          # 人类可读说明
        }
    """
    out = {
        'confirm_score': 0,
        'ma_confirm': False,
        'price_above_ma20': False,
        'ma20_slope_positive': False,
        'vwap_confirm': False,
        'volume_confirm': False,
        'price_structure_confirm': False,
        'breadth_confirm': False,
        'breadth_ratio': None,
        'confirm_note': '',
    }
    if etf_df is None or etf_df.empty:
        return out

    score = 0

    # ---------- ① MA20 确认 ----------
    ma_info = calc_ma_slope(etf_df, cfg.CONFIRM_MA_SHORT, cfg.CONFIRM_MA_SLOPE_PERIOD)
    if ma_info is not None:
        out['price_above_ma20'] = ma_info['price_above_ma']
        out['ma20_slope_positive'] = ma_info['ma_slope'] > 0
        if out['price_above_ma20']:
            score += 10
        if out['ma20_slope_positive']:
            score += 10
        out['ma_confirm'] = out['price_above_ma20'] and out['ma20_slope_positive']

    # ---------- ② VWAP 确认 ----------
    vwap_val = calc_vwap(etf_df, cfg.CONFIRM_VWAP_PERIOD)
    if vwap_val is not None and 'close' in etf_df.columns:
        close_today = float(etf_df['close'].astype(float).dropna().iloc[-1])
        if close_today > vwap_val:
            out['vwap_confirm'] = True
            score += 20

    # ---------- ③ 成交量确认 ----------
    vol_ratio = calc_volume_ratio(etf_df, cfg.CONFIRM_VOLUME_MA)
    if vol_ratio is not None and vol_ratio > cfg.CONFIRM_VOLUME_MULT:
        out['volume_confirm'] = True
        score += 20

    # ---------- ④ 价格结构确认（高低点抬高，N 字突破）----------
    if 'high' in etf_df.columns and 'low' in etf_df.columns:
        sub = etf_df[['high', 'low']].astype(float).dropna()
        w = cfg.CONFIRM_PRICE_STRUCTURE_WINDOW
        if len(sub) >= w * 2:
            # 前半段 vs 后半段的高低点
            first_half = sub.iloc[-(w * 2):-w]
            second_half = sub.iloc[-w:]
            first_high = float(first_half['high'].max())
            first_low = float(first_half['low'].min())
            second_high = float(second_half['high'].max())
            second_low = float(second_half['low'].min())
            # 高低点都抬高 → N 字突破
            if second_high > first_high and second_low > first_low:
                out['price_structure_confirm'] = True
                score += 20

    # ---------- ⑤ 板块宽度确认（ETF 自身涨跌天数占比近似）----------
    if 'close' in etf_df.columns:
        close = etf_df['close'].astype(float).dropna()
        w = cfg.CONFIRM_BREADTH_WINDOW
        if len(close) >= w:
            window = close.iloc[-w:]
            # 上涨天数 = 今日收盘 > 昨日收盘
            diffs = window.diff().dropna()
            up_days = int((diffs > 0).sum())
            breadth_ratio = up_days / len(diffs) if len(diffs) > 0 else 0.0
            out['breadth_ratio'] = round(breadth_ratio, 2)
            if breadth_ratio > cfg.CONFIRM_BREADTH_THRESHOLD:
                out['breadth_confirm'] = True
                score += 20

    out['confirm_score'] = int(round(min(score, 100)))

    # 生成 note
    notes = []
    if out['ma_confirm']:
        notes.append("MA20站上+斜率正")
    if out['vwap_confirm']:
        notes.append("VWAP站上")
    if out['volume_confirm']:
        notes.append("放量1.5x")
    if out['price_structure_confirm']:
        notes.append("N字突破")
    if out['breadth_confirm']:
        notes.append(f"宽度{out['breadth_ratio']:.0%}")
    out['confirm_note'] = "；".join(notes) if notes else ""

    return out


# ====================================================================
# 3.8 持有层（趋势延续判断，加仓/减仓/移动止损）
# ====================================================================

def _compute_hold_score(etf_df: pd.DataFrame,
                        bench_df: pd.DataFrame,
                        cfg: AlarmConfig) -> dict:
    """计算持有分 hold_score（0~100），3 类长周期指标。

    目标：拐点确认后，拿得住趋势。只用于加仓/减仓/移动止损，**不用于预测拐点**。

    3 类指标（合计 0~100）：
        ① 长周期 ADX（34 分）：ADX(56) > HOLD_ADX_THRESHOLD 给 17 分；
           ADX 斜率向上（rising=True）给 17 分
        ② 长周期 RS-Ratio（33 分）：RS(60) > HOLD_RS_STRONG 给 33 分
        ③ 长周期均线（33 分）：站上 MA60 给 16 分；站上 MA120 给 17 分

    离场规则（优先级最高，独立于 hold_score 阈值）：
        跌破 ATR 止损（close < MA20 - HOLD_ATR_STOP_MULT × ATR(14)）→ hold_state='exit'

    持有/减仓规则：
        hold_score >= HOLD_SCORE_HIGH        → hold_state='hold'
        HOLD_SCORE_MED <= score < HIGH       → hold_state='reduce'
        score < HOLD_SCORE_MED               → hold_state='reduce'（加速减仓，仍标 reduce，
                                              hold_score 字段携带具体分值供上层进一步分级）

    Args:
        etf_df: ETF 日线（已对齐基准，含 high/low/close）
        bench_df: 基准指数日线（取 close 作为长周期 RS 分母）
        cfg: AlarmConfig

    Returns:
        dict: {
            'hold_score': 0~100,
            'hold_state': 'hold' / 'reduce' / 'exit',
            'long_adx': float|None,          # 长周期 ADX
            'long_adx_rising': bool,         # 长周期 ADX 斜率向上
            'long_rs': float|None,           # 长周期 RS-Ratio
            'above_ma60': bool,
            'above_ma120': bool,
            'atr_stop_broken': bool,         # 跌破 ATR 止损
            'stop_line': float|None,         # 止损线值
            'hold_note': str,                # 人类可读说明
        }
    """
    out = {
        'hold_score': 0,
        'hold_state': 'reduce',
        'long_adx': None,
        'long_adx_rising': False,
        'long_rs': None,
        'above_ma60': False,
        'above_ma120': False,
        'atr_stop_broken': False,
        'stop_line': None,
        'hold_note': '',
    }
    if etf_df is None or etf_df.empty or bench_df is None or bench_df.empty:
        return out

    score = 0

    # ---------- ① 长周期 ADX（34 分）----------
    long_adx_info = calc_long_adx(
        etf_df, period=cfg.HOLD_ADX_LONG_PERIOD,
        slope_period=cfg.HOLD_ADX_SLOPE_PERIOD, cfg=cfg,
    )
    if long_adx_info is not None:
        out['long_adx'] = long_adx_info['adx']
        out['long_adx_rising'] = bool(long_adx_info['rising'])
        if long_adx_info['adx'] is not None and long_adx_info['adx'] > cfg.HOLD_ADX_THRESHOLD:
            score += 17
        if long_adx_info['rising']:
            score += 17

    # ---------- ② 长周期 RS-Ratio（33 分）----------
    if 'close' in etf_df.columns and 'close' in bench_df.columns:
        long_rs = calc_long_rs_ratio(
            etf_df['close'], bench_df['close'],
            period=cfg.HOLD_RS_LONG_PERIOD, cfg=cfg,
        )
        if long_rs is not None:
            out['long_rs'] = long_rs
            if long_rs > cfg.HOLD_RS_STRONG:
                score += 33

    # ---------- ③ 长周期均线（MA60 + MA120，共 33 分）----------
    close = etf_df['close'].astype(float).dropna() if 'close' in etf_df.columns else None
    if close is not None and len(close) >= cfg.HOLD_MA_LONG:
        ma60 = float(close.iloc[-cfg.HOLD_MA_LONG:].mean())
        today_close = float(close.iloc[-1])
        out['above_ma60'] = today_close > ma60
        if out['above_ma60']:
            score += 16
    if close is not None and len(close) >= cfg.HOLD_MA_LONGER:
        ma120 = float(close.iloc[-cfg.HOLD_MA_LONGER:].mean())
        today_close = float(close.iloc[-1])
        out['above_ma120'] = today_close > ma120
        if out['above_ma120']:
            score += 17

    out['hold_score'] = int(round(min(score, 100)))

    # ---------- ATR 止损（离场信号，优先级最高）----------
    stop_info = calc_atr_stop(
        etf_df,
        period=cfg.HOLD_ATR_STOP_PERIOD,
        mult=cfg.HOLD_ATR_STOP_MULT,
        ma_period=cfg.CONFIRM_MA_SHORT,  # 复用 MA20 作基准
        cfg=cfg,
    )
    if stop_info is not None:
        out['stop_line'] = stop_info['stop_line']
        out['atr_stop_broken'] = bool(stop_info['atr_stop_broken'])

    # ---------- hold_state 决策 ----------
    if out['atr_stop_broken']:
        out['hold_state'] = 'exit'
    elif out['hold_score'] >= cfg.HOLD_SCORE_HIGH:
        out['hold_state'] = 'hold'
    else:
        out['hold_state'] = 'reduce'

    # ---------- note ----------
    notes = []
    if out['long_adx'] is not None:
        adx_str = f"ADX({cfg.HOLD_ADX_LONG_PERIOD})={out['long_adx']}"
        if out['long_adx'] > cfg.HOLD_ADX_THRESHOLD:
            adx_str += "✓"
        if out['long_adx_rising']:
            adx_str += "↑"
        notes.append(adx_str)
    if out['long_rs'] is not None:
        rs_str = f"RS({cfg.HOLD_RS_LONG_PERIOD})={out['long_rs']}"
        if out['long_rs'] > cfg.HOLD_RS_STRONG:
            rs_str += "✓"
        notes.append(rs_str)
    if out['above_ma60']:
        notes.append("站上MA60")
    if out['above_ma120']:
        notes.append("站上MA120")
    if out['atr_stop_broken']:
        notes.append(f"破ATR止损({out['stop_line']})")
    out['hold_note'] = "；".join(notes) if notes else ""

    return out


# ====================================================================
# 4. 单 ETF 完整分析（对齐后调用）
# ====================================================================

def analyze_single_etf(symbol: str, aligned_etf: pd.DataFrame,
                       aligned_bench: pd.DataFrame,
                       cfg: AlarmConfig) -> dict:
    """单只 ETF 完整轮动分析。

    Args:
        symbol: ETF 代码（6 位）
        aligned_etf: 已对齐到基准交易日的 ETF 日线（需含 close/high/low/amount/volume）
        aligned_bench: 已对齐的基准指数日线（取 close 作为 RS 分母）
        cfg: AlarmConfig

    Returns:
        dict 结构：
        {
            'symbol': str,
            'label': str,                   # 中文名（cfg.ROTATION_LABELS 映射）
            'close': float | None,          # 最新收盘价
            'adx': float | None,            # 最新 ETF ADX(14)
            'rs_ratio': float | None,       # 最新 RS-Ratio
            'rs_momentum': float | None,    # 最新 RS-Momentum (%)
            'quadrant': str | None,         # 象限标签
            'quadrant_emoji': str | None,   # 象限 emoji
            'score_5d': dict | None,        # 5 维评分结果
            'data_sufficient': bool,        # 数据是否充足
            'reason': str,                  # 数据不足原因（或空）
        }
    """
    result = {
        'symbol': symbol,
        'label': cfg.ROTATION_LABELS.get(symbol, symbol),
        'close': None,
        'adx': None,
        'rs_ratio': None,
        'rs_momentum': None,
        'quadrant': None,
        'quadrant_emoji': None,
        'score_5d': None,
        'data_sufficient': False,
        'reason': '',
    }

    # 基础数据检查
    if aligned_etf is None or aligned_etf.empty:
        result['reason'] = 'ETF 日线为空'
        return result
    if aligned_bench is None or aligned_bench.empty:
        result['reason'] = '基准日线为空'
        return result
    if len(aligned_etf) != len(aligned_bench):
        result['reason'] = f'ETF({len(aligned_etf)})与基准({len(aligned_bench)})长度不齐'
        return result

    need_rows = max(cfg.RS_RATIO_PERIOD + cfg.RS_MOMENTUM_PERIOD + 5,
                    2 * cfg.ADX_PERIOD + 10)
    if len(aligned_etf) < need_rows:
        result['reason'] = f'数据不足 {need_rows} 行（仅 {len(aligned_etf)} 行）'
        return result

    # 最新收盘价
    try:
        result['close'] = round(float(aligned_etf['close'].iloc[-1]), 4)
    except Exception:
        result['reason'] = '收盘价解析失败'
        return result

    # 计算 RS-Ratio / RS-Momentum
    rs_ratio = calc_rs_ratio(aligned_etf['close'], aligned_bench['close'], cfg)
    if rs_ratio is None or rs_ratio.empty:
        result['reason'] = 'RS-Ratio 计算失败'
        return result
    rs_ratio_last = float(rs_ratio.iloc[-1])
    if not np.isfinite(rs_ratio_last):
        result['reason'] = 'RS-Ratio 最新值异常（NaN/Inf）'
        return result
    result['rs_ratio'] = round(rs_ratio_last, 4)

    rs_mom = calc_rs_momentum(rs_ratio, cfg)
    if rs_mom is None or rs_mom.empty:
        result['reason'] = 'RS-Momentum 计算失败'
        return result
    rs_mom_last = float(rs_mom.iloc[-1])
    if not np.isfinite(rs_mom_last):
        result['reason'] = 'RS-Momentum 最新值异常'
        return result
    result['rs_momentum'] = round(rs_mom_last, 2)

    # ETF 自身 ADX（含方向 DI）
    adx_info = calc_adx_etf(aligned_etf, cfg)
    if adx_info is not None:
        result['adx'] = round(adx_info['adx'], 1)
        result['adx_direction'] = adx_info.get('direction')
        if adx_info.get('plus_di') is not None:
            result['plus_di'] = round(adx_info['plus_di'], 1)
        if adx_info.get('minus_di') is not None:
            result['minus_di'] = round(adx_info['minus_di'], 1)
    else:
        result['adx'] = None

    # RRG 象限
    quadrant, emoji = classify_rrg_quadrant(rs_ratio_last, rs_mom_last, cfg)
    result['quadrant'] = quadrant
    result['quadrant_emoji'] = emoji

    # 大盘均线状态（B5：基准收盘 vs MA20/MA60）
    bench_state, bench_reason = _classify_bench_state(aligned_bench['close'], cfg)
    result['bench_state'] = bench_state
    result['bench_state_reason'] = bench_reason

    # ETF 波动率（B6：ATR14/close 百分比，用于操作参考的风险刹车）
    volatility = calc_volatility_ratio(aligned_etf, cfg)
    result['volatility'] = round(volatility, 2) if volatility is not None else None

    # 5 维评分（传入象限标签 + 大盘状态 + 波动率，用于操作参考的多维修正）
    result['score_5d'] = calc_5d_score(
        aligned_etf, rs_ratio_last, rs_mom_last, rs_mom, cfg,
        quadrant=quadrant, bench_state=bench_state, volatility=volatility,
    )

    # ---- 超卖反弹分级（不进总分，注入 score_5d + 重算 action）----
    # 需要完整 Series 上下文（rs_ratio/rs_mom 历史 + adx_info 末2日 + amount 分位序列）
    # 在 analyze_single_etf 层算，不在 calc_5d_score 里算（保持单只纯函数可测试性）
    adx_info_full = calc_adx_etf(aligned_etf, cfg)  # 已扩展返回 di_cross_up/adx_slope/di_diff
    amount_pct_series = calc_amount_percentile_series(
        aligned_etf, cfg, days=cfg.REBOUND_LOOKBACK
    )
    # 昨日象限（用昨日 rs_ratio/rs_mom 重算）
    quadrant_prev = None
    if rs_ratio is not None and rs_mom is not None:
        if len(rs_ratio) >= 2 and len(rs_mom) >= 2:
            qr, _ = classify_rrg_quadrant(
                float(rs_ratio.iloc[-2]), float(rs_mom.iloc[-2]), cfg
            )
            quadrant_prev = qr
    rebound_level, rebound_note, oversold_days, oversold_expired, truncated = \
        _classify_oversold_rebound(
            aligned_etf, rs_ratio, rs_mom, adx_info_full, amount_pct_series,
            quadrant, quadrant_prev, bench_state, cfg,
        )

    # ---- 趋势/RS 健康度（拐点预警辅助，不进总分）----
    health = _compute_health(aligned_etf, aligned_bench, adx_info_full, rs_mom, cfg)
    improvement_signal = health['improvement_signal']

    s5 = result['score_5d']
    s5['oversold_flag'] = s5.get('oversold_flag', False) or (rebound_level != '无')
    s5['rebound_level'] = rebound_level
    s5['rebound_note'] = rebound_note
    s5['oversold_days'] = oversold_days
    s5['oversold_expired'] = oversold_expired
    s5['oversold_truncated'] = truncated
    # 趋势/RS 健康度字段注入
    s5['adx_slope'] = health['adx_slope']
    s5['di_diff'] = health['di_diff']
    s5['rs_mom_accel'] = health['rs_mom_accel']
    s5['multi_period_rs'] = health['multi_period_rs']
    s5['volume_ratio'] = health['volume_ratio']
    s5['trend_health'] = health['trend_health']
    s5['rs_health'] = health['rs_health']
    s5['improvement_signal'] = improvement_signal

    # ---- 领先层（拐点提前嗅探，不进总分、不改操作矩阵，仅观察名单）----
    lead = _compute_lead_score(aligned_etf, rs_mom, cfg)
    s5['lead_score'] = lead['lead_score']
    s5['vol_compression'] = lead['vol_compression']
    s5['vol_pct'] = lead['vol_pct']
    s5['bbw_pct'] = lead['bbw_pct']
    s5['price_obv_divergence'] = lead['price_obv_divergence']
    s5['rs_mom_divergence'] = lead['rs_mom_divergence']
    s5['lead_note'] = lead['lead_note']

    # ---- 同步层（拐点确认，过滤假反弹，不进总分、不改操作矩阵）----
    confirm = _compute_confirm_score(aligned_etf, cfg)
    s5['confirm_score'] = confirm['confirm_score']
    s5['ma_confirm'] = confirm['ma_confirm']
    s5['price_above_ma20'] = confirm['price_above_ma20']
    s5['ma20_slope_positive'] = confirm['ma20_slope_positive']
    s5['vwap_confirm'] = confirm['vwap_confirm']
    s5['volume_confirm'] = confirm['volume_confirm']
    s5['price_structure_confirm'] = confirm['price_structure_confirm']
    s5['breadth_confirm'] = confirm['breadth_confirm']
    s5['breadth_ratio'] = confirm['breadth_ratio']
    s5['confirm_note'] = confirm['confirm_note']
    # lead + confirm 耦合：拐点初步确认状态（不进总分、不改操作矩阵，仅观察名单标记）
    lead_high = s5['lead_score'] >= 50
    confirm_high = s5['confirm_score'] >= 60
    s5['turning_point_confirmed'] = lead_high and confirm_high  # 拐点初步确认
    s5['turning_point_watch'] = lead_high and not confirm_high   # 只观察不追

    # ---- 持有层（趋势延续判断，加仓/减仓/移动止损，不进总分、不用于预测拐点）----
    # 3 类长周期指标：ADX(56) / RS(60) / MA60+MA120，输出 hold_score 0~100。
    # 独立于 lead/confirm，只回答"已持仓后拿得住吗"，不回答"该不该进"。
    hold = _compute_hold_score(aligned_etf, aligned_bench, cfg)
    s5['hold_score'] = hold['hold_score']
    s5['hold_state'] = hold['hold_state']
    s5['long_adx'] = hold['long_adx']
    s5['long_adx_rising'] = hold['long_adx_rising']
    s5['long_rs'] = hold['long_rs']
    s5['above_ma60'] = hold['above_ma60']
    s5['above_ma120'] = hold['above_ma120']
    s5['atr_stop_broken'] = hold['atr_stop_broken']
    s5['stop_line'] = hold['stop_line']
    s5['hold_note'] = hold['hold_note']

    # 重算 action（叠加 rebound_level + improvement_signal 修正）
    new_with, new_without = _resolve_action(
        quadrant, s5['total_score'], bench_state, volatility, cfg,
        rebound_level=rebound_level,
        improvement_signal=improvement_signal,
    )
    s5['action_with'] = new_with
    s5['action_without'] = new_without
    s5['action_hint'] = f'{new_with}/{new_without}'

    result['data_sufficient'] = True
    return result
