"""功能一：连板基因识别（股性分析）—— 纯函数层，零 IO。

输入：
    history_df: stock_uplimit_reason_history 归一化后的 DataFrame
                列: date / code / name / continue_cnt / limit_up_reason / seal_money
    kline_df:   个股日线 DataFrame
                列: date / open / high / low / close / volume
    ref_date:   参考日 'YYYY-MM-DD'
    cfg:        StockCharConfig

输出 dict 结构（参考 linkban.py 的 analyze_linkban 输出约定）。

零 IO 约定：本模块不 import data_loader，不调用任何网络/磁盘。
所有数据由 main.py 拉取后传入。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd


def _lookup_score(value, table: dict) -> int:
    """按 key 降序查表，首个 value >= key 命中对应分值。

    仿 alarming_monitor/linkban.py 的 _lookup_score 实现。
    """
    if value is None:
        return 0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0
    for k in sorted(table.keys(), reverse=True):
        if v >= k:
            return table[k]
    return 0


def _filter_by_window(df: pd.DataFrame, ref_date: str, days: int) -> pd.DataFrame:
    """过滤 df 到 [ref_date - days, ref_date] 窗口内。

    ref_date 格式 YYYY-MM-DD。df 必须有 'date' 列（YYYY-MM-DD 字符串）。
    """
    if df is None or len(df) == 0:
        return pd.DataFrame()
    if 'date' not in df.columns:
        return pd.DataFrame()
    try:
        ref_dt = datetime.strptime(ref_date[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return df
    cutoff = (ref_dt - timedelta(days=days)).strftime("%Y-%m-%d")
    mask = df['date'].astype(str).str[:10] >= cutoff
    return df[mask].reset_index(drop=True)


def _calc_amplitude(kline_df: pd.DataFrame, days: int) -> float:
    """计算近 N 个交易日的日振幅均值(%)。

    日振幅 = (high - low) / 前一交易日close * 100。
    需要前一日 close，所以实际用到 days+1 行。
    """
    if kline_df is None or len(kline_df) < 2:
        return 0.0
    df = kline_df.tail(days + 1).reset_index(drop=True).copy()
    if len(df) < 2:
        return 0.0
    # 前收盘
    df['pre_close'] = df['close'].shift(1)
    df = df.dropna(subset=['pre_close'])
    if len(df) == 0:
        return 0.0
    # 振幅 = (high - low) / pre_close * 100
    import numpy as np
    df['amplitude'] = np.where(
        df['pre_close'] > 0,
        (df['high'] - df['low']) / df['pre_close'] * 100,
        0.0,
    )
    # 取近 days 行的振幅均值
    return round(float(df['amplitude'].tail(days).mean()), 2)


def analyze_gene(history_df: Optional[pd.DataFrame],
                 kline_df: Optional[pd.DataFrame],
                 ref_date: str,
                 cfg) -> dict:
    """分析单只股票的连板基因。纯函数零 IO。

    逻辑：
    1. 按 gene_history_days 过滤 history_df
    2. 提取 max_boards = max(continue_cnt)
    3. 统计 consecutive_2plus_count / consecutive_3plus_count
    4. latest_limit_up_date = history_df.date.max()
    5. 日振幅：kline_df 近 kline_recent_days 的 (high-low)/pre_close*100 均值
    6. has_gene = max_boards >= gene_min_max_boards
                 AND consecutive_2plus_count >= gene_min_consecutive_2plus
                 AND avg_amplitude >= gene_min_amplitude
    7. just_starting = latest_limit_up_date 在 gene_recent_limitup_days 内
                       AND 近5日无 ≥ gene_current_hot_threshold 连板记录
    8. gene_score 0-100：max_boards(40) + consecutive(30) + amplitude(30) 查表加和

    Returns:
        dict，参考 linkban.analyze_linkban 输出结构。
    """
    # 空数据兜底
    if history_df is None or len(history_df) == 0:
        return {
            'ref_date': ref_date,
            'data_sufficient': False,
            'max_boards': 0,
            'limit_up_count': 0,
            'consecutive_2plus_count': 0,
            'consecutive_3plus_count': 0,
            'latest_limit_up_date': '',
            'avg_amplitude': 0.0,
            'high_volatility': False,
            'has_gene': False,
            'just_starting': False,
            'gene_score': 0,
            'gene_level': '❄️ 妖股基因弱（连板特征不明显）',
            'gene_reason': '无涨停历史数据',
        }

    # 1. 按历史窗口过滤
    history_window = getattr(cfg, 'gene_history_days', 365)
    recent_df = _filter_by_window(history_df, ref_date, history_window)

    if len(recent_df) == 0:
        return {
            'ref_date': ref_date,
            'data_sufficient': False,
            'max_boards': 0,
            'limit_up_count': 0,
            'consecutive_2plus_count': 0,
            'consecutive_3plus_count': 0,
            'latest_limit_up_date': '',
            'avg_amplitude': _calc_amplitude(kline_df, getattr(cfg, 'kline_recent_days', 180)),
            'high_volatility': False,
            'has_gene': False,
            'just_starting': False,
            'gene_score': 0,
            'gene_level': '❄️ 妖股基因弱（连板特征不明显）',
            'gene_reason': f'近{history_window}日无涨停记录',
        }

    # 2. 提取核心指标
    continue_col = recent_df['continue_cnt'] if 'continue_cnt' in recent_df.columns else pd.Series([0])
    max_boards = int(continue_col.max()) if len(continue_col) > 0 else 0
    limit_up_count = len(recent_df)
    consecutive_2plus = int((continue_col >= 2).sum())
    consecutive_3plus = int((continue_col >= 3).sum())
    latest_limit_up_date = str(recent_df['date'].max())[:10]

    # 提取股票代码和名称（从最后一行）
    code = str(recent_df['code'].iloc[-1]) if 'code' in recent_df.columns else ''
    name = str(recent_df['name'].iloc[-1]) if 'name' in recent_df.columns else ''

    # 3. 日振幅
    kline_days = getattr(cfg, 'kline_recent_days', 180)
    avg_amplitude = _calc_amplitude(kline_df, kline_days)

    # 4. 阈值判定
    min_max_boards = getattr(cfg, 'gene_min_max_boards', 2)
    min_consecutive = getattr(cfg, 'gene_min_consecutive_2plus', 1)
    min_amplitude = getattr(cfg, 'gene_min_amplitude', 5.0)

    high_volatility = avg_amplitude >= min_amplitude

    has_gene = (max_boards >= min_max_boards
                and consecutive_2plus >= min_consecutive
                and avg_amplitude >= min_amplitude)

    # 5. "刚启动"判定
    recent_limitup_days = getattr(cfg, 'gene_recent_limitup_days', 30)
    current_hot_threshold = getattr(cfg, 'gene_current_hot_threshold', 3)

    # 最近涨停日期是否在窗口内
    try:
        ref_dt = datetime.strptime(ref_date[:10], "%Y-%m-%d")
        latest_dt = datetime.strptime(latest_limit_up_date[:10], "%Y-%m-%d")
        days_since_latest = (ref_dt - latest_dt).days
    except (ValueError, TypeError):
        days_since_latest = 9999

    recent_has_limitup = days_since_latest <= recent_limitup_days

    # 近5日是否有 ≥ current_hot_threshold 连板（已过热）
    recent_5d = _filter_by_window(recent_df, ref_date, 5)
    recent_hot = False
    if len(recent_5d) > 0 and 'continue_cnt' in recent_5d.columns:
        recent_hot = int((recent_5d['continue_cnt'] >= current_hot_threshold).sum()) > 0

    just_starting = recent_has_limitup and not recent_hot

    # 6. 基因评分
    max_board_score = _lookup_score(max_boards, getattr(cfg, 'gene_max_board_score', {}))
    consecutive_score = _lookup_score(consecutive_2plus,
                                      getattr(cfg, 'gene_consecutive_score', {}))
    amplitude_score = _lookup_score(avg_amplitude,
                                    getattr(cfg, 'gene_amplitude_score', {}))
    gene_score = max_board_score + consecutive_score + amplitude_score

    # 7. 等级
    level_map = getattr(cfg, 'gene_level_map', {})
    gene_level = '❄️ 妖股基因弱（连板特征不明显）'
    for k in sorted(level_map.keys(), reverse=True):
        if gene_score >= k:
            gene_level = level_map[k]
            break

    # 8. 原因描述
    reasons = []
    if has_gene:
        reasons.append(f'最高{max_boards}连板')
        reasons.append(f'近{history_window}日2连板+{consecutive_2plus}次')
        reasons.append(f'日振幅{avg_amplitude:.1f}%')
        if just_starting:
            reasons.append(f'近期涨停（{latest_limit_up_date}）未过热')
        else:
            reasons.append('当前形态未启动或已过热')
    else:
        if max_boards < min_max_boards:
            reasons.append(f'最高{max_boards}连板<阈值{min_max_boards}')
        if consecutive_2plus < min_consecutive:
            reasons.append(f'2连板+{consecutive_2plus}次<阈值{min_consecutive}')
        if avg_amplitude < min_amplitude:
            reasons.append(f'日振幅{avg_amplitude:.1f}%<{min_amplitude}%')

    return {
        'ref_date': ref_date,
        'code': code,
        'name': name,
        'data_sufficient': True,
        'max_boards': max_boards,
        'limit_up_count': limit_up_count,
        'consecutive_2plus_count': consecutive_2plus,
        'consecutive_3plus_count': consecutive_3plus,
        'latest_limit_up_date': latest_limit_up_date,
        'avg_amplitude': avg_amplitude,
        'high_volatility': high_volatility,
        'has_gene': has_gene,
        'just_starting': just_starting,
        'gene_score': gene_score,
        'gene_level': gene_level,
        'gene_reason': '·'.join(reasons),
    }


def filter_gene_candidates(results: list) -> list:
    """从 analyze_gene 结果列表中筛选"具备妖股基因且刚启动"的标的。

    按 gene_score 降序排列。
    """
    candidates = [r for r in results
                  if r.get('has_gene') and r.get('just_starting')
                  and r.get('data_sufficient')]
    candidates.sort(key=lambda r: r.get('gene_score', 0), reverse=True)
    return candidates
