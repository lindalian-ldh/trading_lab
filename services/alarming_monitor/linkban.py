"""连板龙头 & 大盘温度 纯函数分析层。

零 IO：
    - 输入：today_df/yesterday_df（data_loader.fetch_uplimit_stocks 输出的
      归一化 DataFrame，或 None/空）、ref_date（YYYY-MM-DD）、cfg
    - 输出：统一 dict（reporter.py / storage.py 直接消费）
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import pandas as pd

from config import AlarmConfig


# ====================================================================
# 内部工具
# ====================================================================

def _lookup_score(value, table: Dict, min_score_when_none: int = 10) -> int:
    """按配置表取档分（按阈值降序，首个 value >= threshold 命中）。

    典型用于：最高板分、连板总数分、晋级率分。
    value=None / NaN → 返回 min_score_when_none。
    """
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return int(min_score_when_none)
    for threshold in sorted(table.keys(), reverse=True):
        try:
            if value >= threshold:
                return int(table[threshold])
        except Exception:
            continue
    return int(min_score_when_none)


def _lookup_level(total_score: int, level_map: Dict[int, Tuple[str, str]]) -> Tuple[str, str, str]:
    """返回 (level_short, level_cn_suffix, action_advice)。

    level_map 的 key 为综合分阈值（降序首条命中），value = (level_short, level_cn_suffix 即中文描述)
    然后结合 cfg.TEMP_ADVICE_BY_LEVEL 查操作建议。
    """
    level_short = "❄️ 低温"
    level_suffix = "市场低迷"
    for threshold in sorted(level_map.keys(), reverse=True):
        if total_score >= int(threshold):
            level_short, level_suffix = level_map[threshold]
            break
    return level_short, level_suffix


def _normalize_code(code) -> str:
    """code 归一化为 6 位纯数字，便于今日/昨日对比。

    '000017.SZ' / 'SZ000017' / '000017' → '000017'
    空/None/纯非数字 → ''
    """
    if code is None:
        return ""
    s = str(code).strip().upper()
    if not s:
        return ""
    # 去掉常见交易所后缀
    if "." in s:
        s = s.split(".")[0]
    # 去掉前缀 SZ/SH/BJ
    for prefix in ("SZ", "SH", "BJ"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    # 只保留数字部分
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return ""
    return digits.zfill(6) if len(digits) <= 6 else digits


# ====================================================================
# 对外主入口
# ====================================================================

def analyze_linkban(today_df: Optional[pd.DataFrame],
                    yesterday_df: Optional[pd.DataFrame],
                    ref_date: str,
                    cfg: AlarmConfig,
                    reason_loader_fn=None) -> dict:
    """综合分析连板龙头与大盘温度。

    Args:
        today_df:     今日涨停股（归一化列），None/空 → 非交易日/数据不足
        yesterday_df: 昨日涨停股（归一化列），None/空 → 晋级率分母置 0
        ref_date:     'YYYY-MM-DD' 参考日
        cfg:          AlarmConfig
        reason_loader_fn(code, date_ymd='YYYYMMDD') -> Optional[str]
                      可选：回调查个股涨停原因（当 uplimit_stocks 里 limit_up_reason 为空时调用）。
                      传 None 表示不回查（龙头原因就用 df 里现成的）。

    Returns:
        dict（统一结构，见实施方案 Section 3）
    """
    # ---- 返回值骨架（data_sufficient 过程中会被改 True） ----
    result: dict = {
        'ref_date':       ref_date,
        'data_sufficient': False,
        'reason':         '',
        'total_limit_up': 0,
        'total_consecutive': 0,
        'max_board':      0,
        'tier_distribution': {},
        'tier_counts':    {},
        'dragon_head':    None,
        'jinji_rate':     None,
        'jinji_today_continued': 0,
        'jinji_yesterday_total': 0,
        'temp_score':     30,
        'temp_level_short': '❄️ 低温',
        'temp_level_suffix': '市场低迷',
        'temp_level':     '❄️ 低温（市场低迷）',
        'temp_advice':    '市场低迷，观望为主或控制仓位',
    }

    min_board = int(cfg.LINKBAN_MIN_BOARD_FOR_TIER or 2)

    # ---- 今日数据空？ ----
    if today_df is None or len(today_df) == 0:
        result['reason'] = '今日无涨停数据（非交易日或接口不可用）'
        # 温度分按最低档三项合计 10+10+10=30，与骨架一致
        _apply_level(result, 30, cfg)
        return result

    # 过滤 ST？
    df = today_df.copy()
    if cfg.LINKBAN_FILTER_ST:
        is_st = df['is_st'].map(lambda v: bool(v)) if 'is_st' in df.columns else pd.Series([False]*len(df))
        df = df[~is_st].reset_index(drop=True)

    if len(df) == 0:
        result['reason'] = '今日涨停股全为 ST 股（已过滤），无有效连板标的'
        _apply_level(result, 30, cfg)
        return result

    # 今日基础指标：涨停总数、连板股 df（continue_cnt >= min_board）
    total_limit_up = int(len(df))
    consec = df[df['continue_cnt'] >= min_board].reset_index(drop=True)
    total_consecutive = int(len(consec))
    max_board = int(df['continue_cnt'].max()) if 'continue_cnt' in df.columns else 0

    result['data_sufficient'] = True
    result['total_limit_up'] = total_limit_up
    result['total_consecutive'] = total_consecutive
    result['max_board'] = max_board

    # ---- 连板梯队（板数降序，每个板数内按 continue_cnt desc, seal_money desc, limit_up_time asc 排序） ----
    if total_consecutive > 0:
        # seal_money NaN 兜底 0
        seal_safe = pd.to_numeric(consec['seal_money'], errors='coerce').fillna(0)
        limit_time = consec['limit_up_time'].astype(str) if 'limit_up_time' in consec.columns else pd.Series(['']*len(consec))
        sort_df = pd.DataFrame({
            'code':         consec['code'].tolist(),
            'name':         consec['name'].tolist(),
            'continue_cnt': consec['continue_cnt'].astype(int).tolist(),
            'seal_money':   seal_safe.tolist(),
            'limit_up_time': limit_time.tolist(),
            'limit_up_reason': (consec['limit_up_reason'].tolist()
                                if 'limit_up_reason' in consec.columns
                                else ['']*len(consec)),
        })
        # 排序：板数 DESC, 封单 DESC, 首板时间 ASC
        # 空首板时间 '' 视为最末 → 转成 '99:99:99' 的排序键
        sort_df = sort_df.copy()
        _sort_time = sort_df['limit_up_time'].astype(str).map(
            lambda s: '99:99:99' if not s or s.strip() == '' or s.lower() == 'nan' else s
        )
        sort_df['_sort_time'] = _sort_time
        sort_df = sort_df.sort_values(
            by=['continue_cnt', 'seal_money', '_sort_time'],
            ascending=[False, False, True],
            na_position='last',
            kind='mergesort',
        ).reset_index(drop=True)
        sort_df = sort_df.drop(columns=['_sort_time'])

        # 分组到梯队 dict
        tier_distribution: Dict[int, List[dict]] = {}
        tier_counts: Dict[int, int] = {}
        for _, row in sort_df.iterrows():
            board = int(row['continue_cnt'])
            item = {
                'code': str(row['code']),
                'name': str(row['name']),
                'seal_money_yuan': float(row['seal_money']),
                'limit_up_time': str(row['limit_up_time']),
                'limit_up_reason_raw': str(row['limit_up_reason']),
            }
            tier_distribution.setdefault(board, []).append(item)
            tier_counts[board] = tier_counts.get(board, 0) + 1
        # key 降序：重建 dict
        tier_distribution = {k: tier_distribution[k] for k in sorted(tier_distribution.keys(), reverse=True)}
        tier_counts = {k: tier_counts[k] for k in sorted(tier_counts.keys(), reverse=True)}

        result['tier_distribution'] = tier_distribution
        result['tier_counts'] = tier_counts

        # ---- 龙头股（sort_df 第一行） ----
        first = sort_df.iloc[0]
        reason = str(first['limit_up_reason']).strip()
        if (not reason or reason.lower() in ('nan', 'none')) and reason_loader_fn is not None:
            try:
                ymd = ref_date.replace('-', '')
                r2 = reason_loader_fn(str(first['code']), ymd)
                if r2:
                    reason = str(r2).strip()
            except Exception:
                pass
        result['dragon_head'] = {
            'code':   str(first['code']),
            'name':   str(first['name']),
            'board_cnt': int(first['continue_cnt']),
            'limit_up_reason': reason if reason else '—',
            'seal_money_yuan': float(first['seal_money']),
        }

    # ---- 晋级率 ----
    jinji_rate: Optional[float] = None
    denominator = 0
    molecule = 0
    if yesterday_df is not None and len(yesterday_df) > 0:
        yd = yesterday_df
        # 过滤 ST（与今日口径一致）
        if cfg.LINKBAN_FILTER_ST and 'is_st' in yd.columns:
            yd = yd[~yd['is_st'].map(lambda v: bool(v))].reset_index(drop=True)
        if len(yd) > 0 and 'continue_cnt' in yd.columns:
            y_consec = yd[yd['continue_cnt'] >= min_board].reset_index(drop=True)
            denominator = int(len(y_consec))
            if denominator > 0:
                # 今日涨停 codes（continue_cnt >= 1 即可，不要求 >= 2）
                today_codes = {_normalize_code(c) for c in df['code'].tolist()}
                # 昨日连板 codes
                y_codes = {_normalize_code(c) for c in y_consec['code'].tolist()}
                molecule = len(today_codes & y_codes)
                jinji_rate = molecule / denominator

    result['jinji_yesterday_total'] = denominator
    result['jinji_today_continued'] = molecule
    result['jinji_rate'] = jinji_rate

    # ---- 温度评分 ----
    s_max_board = _lookup_score(max_board, cfg.TEMP_MAX_BOARD_SCORE,
                                min_score_when_none=cfg.TEMP_MAX_BOARD_SCORE.get(0, 10))
    s_total = _lookup_score(total_consecutive, cfg.TEMP_TOTAL_SCORE,
                            min_score_when_none=cfg.TEMP_TOTAL_SCORE.get(0, 10))
    s_jinji = _lookup_score(jinji_rate, cfg.TEMP_JINJI_SCORE,
                            min_score_when_none=cfg.TEMP_JINJI_SCORE.get(0.0, 10))
    total_score = int(s_max_board + s_total + s_jinji)
    total_score = int(max(0, min(100, total_score)))
    _apply_level(result, total_score, cfg)

    return result


def _apply_level(result: dict, total_score: int, cfg: AlarmConfig) -> None:
    """根据综合分 → level short/suffix/advice + 填充 result。"""
    level_short, level_suffix = _lookup_level(total_score, cfg.TEMP_LEVEL_MAP)
    advice = cfg.TEMP_ADVICE_BY_LEVEL.get(level_short,
                                           '观望为主或控制仓位')
    result['temp_score'] = int(total_score)
    result['temp_level_short'] = level_short
    result['temp_level_suffix'] = level_suffix
    result['temp_level'] = f'{level_short}（{level_suffix}）'
    result['temp_advice'] = advice
