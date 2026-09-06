"""信号聚合引擎：7 个原子信号 + ADX 趋势过滤器 → 风险等级 + 降仓位建议。

evaluate_signals(indicator_results, cfg, ref_date, adx_result) 返回结构化 dict，含：
    - signals: 7 信号明细列表
    - adx: ADX 过滤器结果 dict（含 market_state / value / detail）
    - red_count: 亮红灯数（震荡市下 S4/S5/S6 加权 ×1.5 向上取整）
    - effective_threshold: 实际生效的 red_count 阈值（按 ADX 市场状态动态切换）
    - market_state: trend / range / neutral
    - risk_level: high / warn / normal
    - position_advice: 仓位调整文案（如 "8成 → 5成"）
    - advice_desc: 建议说明
    - data_sufficient: 全部信号数据是否充足
    - run_time / ref_date: 时间戳

ADX 动态权重（用户校准方案）：
    - 强趋势市（ADX > 30）：红灯阈值上调，需 ≥5 红才警报
    - 弱趋势/震荡市（ADX < 22）：红灯阈值下调，≥3 红即警报；
      且 S4/S5/S6 信号权重 ×1.5（抽血效应/机构弃守更易引发踩踏）
    - 方向不明（22-30）：维持原判，≥4 红才降仓
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

from config import AlarmConfig


def evaluate_signals(indicator_results: list, cfg: AlarmConfig,
                      ref_date: str, adx_result: Optional[dict] = None) -> dict:
    """聚合 7 信号 + ADX 过滤器 → 风险等级 + 仓位建议。

    Args:
        indicator_results: 7 个信号 dict（由 indicators.check_* 产生）
        cfg: AlarmConfig 配置
        ref_date: 参考日期 YYYY-MM-DD
        adx_result: ADX 信号 dict（由 check_adx 产生），None 时退化为静态阈值

    Returns:
        结构化结果 dict。
    """
    # ---- ADX 市场状态 → 动态阈值 ----
    market_state = 'neutral'
    adx_value = float('nan')
    adx_detail = 'ADX 未计算（退化为中性口径）'
    adx_sufficient = False

    if adx_result is not None and adx_result.get('data_sufficient'):
        market_state = adx_result.get('market_state', 'neutral')
        adx_value = float(adx_result.get('value', float('nan')))
        adx_detail = adx_result.get('detail', '')
        adx_sufficient = True

    # 动态 red_count 阈值
    # 注意：ADX 未取/数据不足时退化为 cfg.REDUCE_THRESHOLD（兼容 Conservative/Strict 档）
    if market_state == 'trend':
        effective_threshold = cfg.REDUCE_THRESHOLD_TREND
        threshold_reason = (f'强趋势市 ADX={adx_value:.1f} → '
                            f'阈值上调至 {effective_threshold}（需全亮）')
    elif market_state == 'range':
        effective_threshold = cfg.REDUCE_THRESHOLD_RANGE
        threshold_reason = (f'弱趋势/震荡市 ADX={adx_value:.1f} → '
                            f'阈值下调至 {effective_threshold}（S4/S5/S6 加权×{cfg.RANGE_SIGNAL_WEIGHT}）')
    elif adx_sufficient:
        # ADX 数据充足但属中性区
        effective_threshold = cfg.REDUCE_THRESHOLD_NEUTRAL
        threshold_reason = (f'方向不明 ADX={adx_value:.1f} → '
                            f'维持原判阈值 {effective_threshold}')
    else:
        # ADX 未取/数据不足 → 退化为配置档原 REDUCE_THRESHOLD（兼容 Conservative=3 / Strict=4）
        effective_threshold = cfg.REDUCE_THRESHOLD
        threshold_reason = f'ADX 未取 → 配置档默认阈值 {effective_threshold}'

    # ---- 红灯计数（震荡市 S4/S5/S6 加权）----
    red_count = 0
    weighted_red_count = 0.0
    weighted_signals = cfg.RANGE_WEIGHTED_SIGNALS
    for s in indicator_results:
        if not s.get('red'):
            continue
        red_count += 1
        if market_state == 'range' and s.get('name') in weighted_signals:
            weighted_red_count += cfg.RANGE_SIGNAL_WEIGHT
        else:
            weighted_red_count += 1.0

    # 震荡市用加权计数向上取整作为有效红灯数
    if market_state == 'range':
        effective_red_count = math.ceil(weighted_red_count)
    else:
        effective_red_count = red_count

    # ---- 风险等级（基于 effective_red_count 与 effective_threshold）----
    if effective_red_count >= effective_threshold:
        risk_level = 'high'
    elif effective_red_count >= cfg.WARN_THRESHOLD:
        risk_level = 'warn'
    else:
        risk_level = 'normal'

    # ---- 降仓位建议 ----
    advice, advice_desc = _match_advice(effective_red_count, cfg, market_state)

    return {
        'signals': indicator_results,
        'adx': {
            'value': adx_value,
            'market_state': market_state,
            'detail': adx_detail,
            'data_sufficient': adx_sufficient,
        },
        'red_count': red_count,                     # 原始红灯数（不加权）
        'effective_red_count': effective_red_count, # 加权后用于判定的红灯数
        'effective_threshold': effective_threshold,
        'market_state': market_state,
        'threshold_reason': threshold_reason,
        'risk_level': risk_level,
        'position_advice': advice,
        'advice_desc': advice_desc,
        'data_sufficient': all(s.get('data_sufficient', False) for s in indicator_results),
        'run_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'ref_date': ref_date,
        'profile': type(cfg).__name__,
    }


def _match_advice(red_count: int, cfg: AlarmConfig,
                  market_state: str = 'neutral') -> tuple:
    """按 POSITION_ADVICE 表降序匹配首条 red_count >= threshold。

    POSITION_ADVICE 默认: [(4, "8→5"), (3, "8→6"), (2, "8→7")]
    表应已按 threshold 降序排列；此处做一次排序保险。
    无匹配时返回"维持当前仓位"。

    market_state 用于在 advice_desc 中附加 ADX 市场状态说明。
    若 desc 已含市场状态前缀（如"震荡市加权触发"）则不再叠加，避免重复。
    """
    table = sorted(cfg.POSITION_ADVICE, key=lambda x: x[0], reverse=True)
    state_prefix = {'range': '震荡市', 'trend': '强趋势市'}.get(market_state, '')
    for threshold, advice, desc in table:
        if red_count >= threshold:
            # 若 desc 已含市场状态关键字则不再叠加前缀
            if state_prefix and state_prefix not in desc:
                desc = f'{state_prefix}：{desc}'
            return advice, desc
    if state_prefix:
        return '维持当前仓位', f'{state_prefix}：{red_count}个红灯，未达预警门槛'
    return '维持当前仓位', f'{red_count}个红灯，未达预警门槛'
