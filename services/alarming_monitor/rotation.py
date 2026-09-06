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
    维1 相对动量(25%)：  (RS-Ratio - 1) 映射 → [0, 5]，超基准 SCORE_REL_MOM_FULL(0.5) 即满分
    维2 动量加速度(20%)：RS-Momentum 二阶差分 → [0, 5]，SCORE_MOM_ACC_FULL(5%) 即满分
    维3 ADX 趋势强度(20%)：ETF 自身 ADX(14) → [0, 5]，SCORE_ADX_FULL(30) 即满分
    维4 资金关注度(20%)：换手率代理(amount/流通市值代理) → [0, 5]，SCORE_CAP_ATTENTION_FULL(5%) 即满分
    维5 拥挤度折扣(15%)：原始 5 分，若换手率 > CROWDING_TURNOVER_THRESHOLD 扣 CROWDING_PENALTY
    综合得分范围：[0, 5]
        > 4.0 → 强推荐（持有/加仓）
        3.0~4.0 → 关注建仓
        ≤ 3.0 → 中性/回避
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from config import AlarmConfig
from indicators import (
    calc_adx_etf, calc_rs_momentum, calc_rs_ratio, calc_turnover_ratio,
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


def calc_5d_score(etf_df: pd.DataFrame, rs_ratio_last: float,
                  rs_momentum_last: float, rs_momentum_series: pd.Series,
                  cfg: AlarmConfig) -> dict:
    """5 维评分卡。返回各维得分 + 加权综合得分。

    Returns:
        dict 结构：
        {
            'relative_momentum':     float,  # 维1 归一化分
            'momentum_acceleration': float,  # 维2 归一化分
            'adx_trend':             float,  # 维3 归一化分
            'capital_attention':     float,  # 维4 归一化分
            'crowding_penalty':      float,  # 维5 拥挤折扣后得分
            'crowding_penalty_applied': bool, # 是否触发拥挤度扣分
            'total_score':           float,  # 加权综合得分 [0, 5]
            'action_hint':           str,    # 操作参考（强推荐/关注/回避）
        }
    """
    w = cfg.SCORE_WEIGHTS

    # ---- 维1 相对动量（25%）：(RS-Ratio - 1) 归一化 ----
    rel_mom_raw = float(rs_ratio_last) - 1.0
    rel_mom_score = _normalize(rel_mom_raw, cfg.SCORE_REL_MOM_FULL)
    # RS-Ratio 低于基准时为负，归一化取 abs 后需反向修正：<1 时直接给 0 分
    if rel_mom_raw < 0:
        rel_mom_score = 0.0

    # ---- 维2 动量加速度（20%）：RS-Momentum 二阶差分 ----
    # 二阶差分 = mom[-1] - 2*mom[-2] + mom[-3]（动量的变化率）
    mom_acc_raw = 0.0
    if rs_momentum_series is not None and len(rs_momentum_series) >= 3:
        vals = rs_momentum_series.dropna()
        if len(vals) >= 3:
            m1 = float(vals.iloc[-1])
            m2 = float(vals.iloc[-2])
            m3 = float(vals.iloc[-3])
            mom_acc_raw = m1 - 2 * m2 + m3
    mom_acc_score = _normalize(mom_acc_raw, cfg.SCORE_MOM_ACC_FULL)
    # 负加速度（动量在衰减）不给负分，给 0
    if mom_acc_raw < 0:
        mom_acc_score = 0.0

    # ---- 维3 ADX 趋势强度（20%）：ETF 自身 ADX(14) ----
    adx_raw = calc_adx_etf(etf_df, cfg)
    if adx_raw is None:
        adx_score = 0.0
    else:
        adx_score = _normalize(adx_raw, cfg.SCORE_ADX_FULL)

    # ---- 维4 资金关注度（20%）：换手率代理 ----
    turnover_raw = calc_turnover_ratio(etf_df)
    if turnover_raw is None:
        cap_att_score = 0.0
    else:
        cap_att_score = _normalize(turnover_raw, cfg.SCORE_CAP_ATTENTION_FULL)

    # ---- 维5 拥挤度折扣（15%）：基础 5 分，换手过高扣分 ----
    crowding_score = 5.0
    crowding_penalty_applied = False
    if turnover_raw is not None and turnover_raw > cfg.CROWDING_TURNOVER_THRESHOLD:
        crowding_score = max(0.0, 5.0 - cfg.CROWDING_PENALTY)
        crowding_penalty_applied = True

    # ---- 加权综合得分 ----
    total = (
        rel_mom_score        * w.get('relative_momentum', 0.25)
        + mom_acc_score      * w.get('momentum_acceleration', 0.20)
        + adx_score          * w.get('adx_trend', 0.20)
        + cap_att_score      * w.get('capital_attention', 0.20)
        + crowding_score     * w.get('crowding_penalty', 0.15)
    )
    total = float(np.clip(total, 0.0, 5.0))

    # ---- 操作参考 ----
    if total > cfg.SCORE_STRONG_THRESHOLD:
        action = '持有/加仓'
    elif total >= cfg.SCORE_ATTENTION_THRESHOLD:
        action = '关注建仓'
    else:
        action = '回避'

    return {
        'relative_momentum': round(rel_mom_score, 2),
        'momentum_acceleration': round(mom_acc_score, 2),
        'adx_trend': round(adx_score, 2),
        'capital_attention': round(cap_att_score, 2),
        'crowding_penalty': round(crowding_score, 2),
        'crowding_penalty_applied': crowding_penalty_applied,
        'volume_ratio': round(turnover_raw, 3) if turnover_raw is not None else None,
        'total_score': round(total, 2),
        'action_hint': action,
    }


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

    # ETF 自身 ADX
    result['adx'] = calc_adx_etf(aligned_etf, cfg)
    if result['adx'] is not None:
        result['adx'] = round(result['adx'], 1)

    # RRG 象限
    quadrant, emoji = classify_rrg_quadrant(rs_ratio_last, rs_mom_last, cfg)
    result['quadrant'] = quadrant
    result['quadrant_emoji'] = emoji

    # 5 维评分
    result['score_5d'] = calc_5d_score(aligned_etf, rs_ratio_last, rs_mom_last, rs_mom, cfg)

    result['data_sufficient'] = True
    return result
