"""功能二：游资席位偏好（龙虎榜分析）—— 纯函数层，零 IO。

输入：
    lhb_records_df: 龙虎榜记录 DataFrame（fetch_lhb_for_stock 返回）
                    列: date / stock_code / stock_name / seat_name / seat_type /
                        amount / youzi_icon / up_desc / up_reason
    kline_df:       个股日线 DataFrame
                    列: date / open / high / low / close / volume
    ref_date:       参考日 'YYYY-MM-DD'
    cfg:            StockCharConfig

输出 dict 结构（参考 gene.py 的 analyze_gene 输出约定）。

零 IO 约定：本模块不 import data_loader，不调用任何网络/磁盘。
"""

from __future__ import annotations

from typing import Optional

import pandas as pd


def _classify_seat(seat_name: str, youzi_icon: str, cfg) -> tuple:
    """席位分类。

    匹配 cfg.hot_money_seats 映射表（seat_name 包含匹配）→ 游资
    匹配 cfg.institution_seat_name → 机构
    youzi_icon 非空 → 游资（补充信号）
    其余 → 其他

    Returns:
        (seat_type, hot_money_name): seat_type='游资'/'机构'/'其他'，
        hot_money_name 为游资名称（如 '炒股养家'），非游资为 ''。
    """
    seat_name = str(seat_name or '').strip()
    youzi_icon = str(youzi_icon or '').strip()

    institution_name = getattr(cfg, 'institution_seat_name', '机构专用')
    if institution_name and institution_name in seat_name:
        return '机构', ''

    # 匹配游资映射表
    hot_money_seats = getattr(cfg, 'hot_money_seats', {}) or {}
    for hm_name, seat_patterns in hot_money_seats.items():
        for pattern in seat_patterns:
            if pattern and pattern in seat_name:
                return '游资', hm_name

    # youzi_icon 补充信号
    if youzi_icon and youzi_icon.lower() not in ('none', 'nan', 'null', ''):
        return '游资', youzi_icon

    return '其他', ''


def _get_next_day_pct(kline_df: pd.DataFrame, lhb_date: str) -> Optional[float]:
    """从日线数据取龙虎榜日期的下一交易日涨跌幅(%)。

    next_day_pct = (next_close - lhb_close) / lhb_close * 100
    """
    if kline_df is None or len(kline_df) == 0:
        return None
    if 'date' not in kline_df.columns or 'close' not in kline_df.columns:
        return None

    df = kline_df.copy()
    df['date'] = df['date'].astype(str).str[:10]
    df = df.sort_values('date').reset_index(drop=True)

    lhb_date = str(lhb_date)[:10]
    # 找到龙虎榜当日在 kline 中的位置
    idx_list = df.index[df['date'] == lhb_date].tolist()
    if not idx_list:
        # 龙虎榜当日不在 kline（可能 kline 未覆盖），尝试找最近的前一个交易日
        before = df[df['date'] <= lhb_date]
        if len(before) == 0:
            return None
        idx = before.index[-1]
    else:
        idx = idx_list[0]

    # 下一交易日
    if idx + 1 >= len(df):
        return None  # 龙虎榜日是最后一日，无次日数据
    lhb_close = pd.to_numeric(df.loc[idx, 'close'], errors='coerce')
    next_close = pd.to_numeric(df.loc[idx + 1, 'close'], errors='coerce')
    if pd.isna(lhb_close) or pd.isna(next_close) or lhb_close == 0:
        return None
    return round(float((next_close - lhb_close) / lhb_close * 100), 2)


def analyze_hot_money(lhb_records_df: Optional[pd.DataFrame],
                       kline_df: Optional[pd.DataFrame],
                       ref_date: str,
                       cfg) -> dict:
    """分析单只股票的游资席位偏好。纯函数零 IO。

    逻辑：
    1. 席位分类：seat_name 匹配 cfg.hot_money_seats → 游资+名称；
       匹配 institution_seat_name → 机构；youzi_icon 非空 → 游资；其余 → 其他
    2. 统计 hot_money_buy_count（游资买入次数）、institutional_buy_count
    3. 次日涨跌：对每个 lhb date，从 kline_df 取下一交易日 pct_chg
    4. next_day_premium_count = 次日涨幅 ≥ premium_threshold 的次数
       next_day_dump_count = 次日跌幅 ≥ dump_threshold 的次数
    5. preference 判定：
       - hot_money_buy_count >= hot_money_min_buy_count
         AND next_day_premium_count >= 1 → '偏爱锁仓'
       - hot_money_buy_count >= hot_money_min_buy_count
         AND next_day_dump_count >= 1 → '一日游风险'
       - 否则 → '中性'

    Returns:
        dict，参考 gene.analyze_gene 输出结构。
    """
    # 空数据兜底
    if lhb_records_df is None or len(lhb_records_df) == 0:
        return {
            'ref_date': ref_date,
            'data_sufficient': False,
            'lhb_count': 0,
            'institutional_buy_count': 0,
            'hot_money_buy_count': 0,
            'hot_money_names': [],
            'next_day_premium_count': 0,
            'next_day_dump_count': 0,
            'preference': '中性',
            'preference_reason': '无龙虎榜记录',
            'hot_money_score': 0,
        }

    df = lhb_records_df.copy()
    if 'date' not in df.columns:
        return {
            'ref_date': ref_date,
            'data_sufficient': False,
            'lhb_count': 0,
            'institutional_buy_count': 0,
            'hot_money_buy_count': 0,
            'hot_money_names': [],
            'next_day_premium_count': 0,
            'next_day_dump_count': 0,
            'preference': '中性',
            'preference_reason': '龙虎榜数据缺 date 列',
            'hot_money_score': 0,
        }

    # 1. 席位分类
    seat_types = []
    hot_money_names = []
    for _, row in df.iterrows():
        seat_name = str(row.get('seat_name', ''))
        youzi_icon = str(row.get('youzi_icon', ''))
        seat_type, hm_name = _classify_seat(seat_name, youzi_icon, cfg)
        seat_types.append(seat_type)
        if hm_name and hm_name not in hot_money_names:
            hot_money_names.append(hm_name)
    df['classified_type'] = seat_types

    # 提取股票代码和名称
    code = str(df['stock_code'].iloc[0]) if 'stock_code' in df.columns else ''
    name = str(df['stock_name'].iloc[0]) if 'stock_name' in df.columns else ''

    # 2. 统计
    # 龙虎榜上榜次数（按 date 去重，一只股票一日算一次）
    lhb_dates = df['date'].astype(str).str[:10].unique().tolist()
    lhb_count = len(lhb_dates)

    # 只统计买入席位
    buy_df = df[df.get('seat_type', pd.Series(['buy'] * len(df))) == 'buy'] \
        if 'seat_type' in df.columns else df

    institutional_buy_count = int((buy_df['classified_type'] == '机构').sum()) \
        if 'classified_type' in buy_df.columns else 0
    hot_money_buy_count = int((buy_df['classified_type'] == '游资').sum()) \
        if 'classified_type' in buy_df.columns else 0

    # 3. 次日涨跌
    premium_threshold = getattr(cfg, 'premium_threshold', 3.0)
    dump_threshold = getattr(cfg, 'dump_threshold', -5.0)

    next_day_premium_count = 0
    next_day_dump_count = 0
    next_day_pcts = []

    for lhb_date in lhb_dates:
        pct = _get_next_day_pct(kline_df, lhb_date)
        if pct is None:
            continue
        next_day_pcts.append((lhb_date, pct))
        if pct >= premium_threshold:
            next_day_premium_count += 1
        if pct <= dump_threshold:
            next_day_dump_count += 1

    # 4. 偏好判定
    min_buy = getattr(cfg, 'hot_money_min_buy_count', 2)

    if hot_money_buy_count >= min_buy and next_day_premium_count >= 1:
        preference = '偏爱锁仓'
        # 详细原因
        hm_str = '/'.join(hot_money_names) if hot_money_names else '游资'
        # 找到溢价的次日
        premium_examples = [f'{d}次日+{p:.1f}%' for d, p in next_day_pcts if p >= premium_threshold]
        preference_reason = f'偏爱锁仓·游资{hm_str}{hot_money_buy_count}次买入·{"; ".join(premium_examples[:2])}'
    elif hot_money_buy_count >= min_buy and next_day_dump_count >= 1:
        preference = '一日游风险'
        hm_str = '/'.join(hot_money_names) if hot_money_names else '游资'
        dump_examples = [f'{d}次日{p:.1f}%' for d, p in next_day_pcts if p <= dump_threshold]
        preference_reason = f'一日游风险·游资{hm_str}{hot_money_buy_count}次买入后{"; ".join(dump_examples[:2])}'
    else:
        preference = '中性'
        reasons = []
        if hot_money_buy_count == 0:
            reasons.append('无游资买入')
        elif hot_money_buy_count < min_buy:
            reasons.append(f'游资买入{hot_money_buy_count}次<{min_buy}次阈值')
        if next_day_premium_count == 0 and next_day_dump_count == 0:
            reasons.append('次日涨跌不显著')
        preference_reason = '·'.join(reasons) if reasons else '数据不足'

    # 5. 评分（0-100）
    # 游资活跃度 50 分 + 次日溢价 50 分
    hm_active_score = min(hot_money_buy_count * 15, 50)
    premium_score = min(next_day_premium_count * 25, 50)
    dump_penalty = min(next_day_dump_count * 20, 50)
    hot_money_score = max(0, hm_active_score + premium_score - dump_penalty)

    return {
        'ref_date': ref_date,
        'code': code,
        'name': name,
        'data_sufficient': True,
        'lhb_count': lhb_count,
        'institutional_buy_count': institutional_buy_count,
        'hot_money_buy_count': hot_money_buy_count,
        'hot_money_names': hot_money_names,
        'next_day_premium_count': next_day_premium_count,
        'next_day_dump_count': next_day_dump_count,
        'next_day_pcts': next_day_pcts,
        'preference': preference,
        'preference_reason': preference_reason,
        'hot_money_score': hot_money_score,
    }
