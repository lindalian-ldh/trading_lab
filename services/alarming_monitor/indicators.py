"""7 个原子警戒信号 + ADX 趋势过滤器 + 板块轮动指标计算（纯函数，无 IO）。

每个 S* 函数接收标准化 DataFrame + AlarmConfig，返回 dict：
    {
        'name': str,             # 信号标识
        'label': str,            # 中文标签
        'red': bool,             # 是否亮红灯
        'value': float,          # 当前值
        'threshold': float,      # 触发阈值
        'detail': str,           # 人类可读明细
        'data_sufficient': bool, # 数据是否充足（不足时 red=False）
    }

数据不足时 red=False, data_sufficient=False（不报错，由调用方降级）。

ADX（平均趋向指标）不直接亮红灯，而是作为市场状态过滤器，
返回 market_state（trend / range / neutral）供聚合引擎动态调整 red_count 阈值。

板块轮动指标（RS-Ratio / RS-Momentum）用于 RRG 象限分类 + 5 维评分：
    calc_rs_ratio()      ETF/基准 比值的 WMA 平滑
    calc_rs_momentum()   RS-Ratio 的 N 日变化率
    calc_adx_etf()       ETF 自身的 ADX（与指数 ADX 复用同一算法）

talib 加速：cfg.USE_TALIB=True 且 talib 可用时优先用 talib 计算技术指标，
不可用时降级 numpy 自实现（_compute_adx 已验证可用）。
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from config import AlarmConfig


# ====================================================================
# talib 加速开关（启动时检测一次，模块级缓存）
# ====================================================================
try:
    import talib
    _HAS_TALIB = True
except ImportError:
    _HAS_TALIB = False


def _talib_available(cfg: AlarmConfig) -> bool:
    """是否启用 talib 加速（cfg.USE_TALIB=True 且 talib 已安装）。"""
    return getattr(cfg, 'USE_TALIB', False) and _HAS_TALIB


# ====================================================================
# 工具
# ====================================================================

def _ret_over(series: pd.Series, n: int) -> Optional[float]:
    """计算 n 日涨幅：series[-1] / series[-1-n] - 1。数据不足返回 None。"""
    if series is None or len(series) < n + 1:
        return None
    base = float(series.iloc[-1 - n])
    if base == 0:
        return None
    return float(series.iloc[-1]) / base - 1.0


def _insufficient(name: str, label: str, reason: str) -> dict:
    return {
        'name': name, 'label': label, 'red': False,
        'value': float('nan'), 'threshold': float('nan'),
        'detail': f'数据不足：{reason}', 'data_sufficient': False,
        'red_reason': '',
    }


# ====================================================================
# S1: 成交额/总市值 占比过高
# ====================================================================

def check_turnover_ratio(spot_df: Optional[pd.DataFrame], cfg: AlarmConfig) -> dict:
    """S1: 今日两市成交额合计 / 总市值合计 > 阈值。

    总市值优先取 spot 的 total_value 列；
    该列缺失（新浪指数 spot 源无总市值列）时，用 cfg.TOTAL_MARKET_CAP 常量兜底。
    """
    name, label = 'turnover_ratio_high', 'S1 成交额/总市值'
    th = cfg.TURNOVER_RATIO_THRESHOLD

    if spot_df is None or spot_df.empty:
        return _insufficient(name, label, '全市场快照为空')
    if 'amount' not in spot_df.columns:
        return _insufficient(name, label, '缺少成交额列')

    amt_sum = float(spot_df['amount'].sum())

    # 总市值：优先用 spot 数据列；缺失则用配置常量（新浪源兜底）
    used_config_cap = False
    if 'total_value' in spot_df.columns:
        cap_sum = float(spot_df['total_value'].sum())
        cap_src = '数据'
    else:
        cap_sum = float(cfg.TOTAL_MARKET_CAP)
        cap_src = f'配置常量{cap_sum/1e8:.0f}亿'
        used_config_cap = True

    if cap_sum <= 0:
        return _insufficient(name, label, '总市值为0')

    ratio = amt_sum / cap_sum
    red = ratio > th
    return {
        'name': name, 'label': label, 'red': red,
        'value': ratio, 'threshold': th,
        'detail': (f'成交额 {amt_sum/1e8:.0f}亿 / 总市值({cap_src})'
                   f' = {ratio*100:.2f}% {">阈值" if red else "≤阈值"} {th*100:.1f}%'
                   + ('〔新浪源·总市值用常量兜底〕' if used_config_cap else '')),
        'data_sufficient': True,
        'red_reason': f'占比{ratio*100:.2f}%超阈值{th*100:.1f}%' if red else '',
    }


# ====================================================================
# S2: 放量不涨
# ====================================================================

def check_volume_price_divergence(index_df: Optional[pd.DataFrame], cfg: AlarmConfig) -> dict:
    """S2: 近5日日均成交额 > 近20日日均成交额 × 乘数，且 指数5日涨幅 ≤ 0。

    口径：日均÷日均（修正量级错误）。
    """
    name, label = 'volume_price_divergence', 'S2 放量不涨'
    mult = cfg.VOLUME_HIGH_MULT
    flat = cfg.INDEX_FLAT_PCT

    if index_df is None or index_df.empty:
        return _insufficient(name, label, '指数日线为空')
    need = max(cfg.VOLUME_LOOKBACK_DAYS, 6) + 1
    if len(index_df) < need:
        return _insufficient(name, label, f'指数日线不足{need}行')

    if 'amount' not in index_df.columns or 'close' not in index_df.columns:
        return _insufficient(name, label, '缺少成交额/收盘价列')

    amt = index_df['amount']
    avg5 = float(amt.iloc[-5:].mean())
    avg20 = float(amt.iloc[-20:].mean()) if len(amt) >= 20 else float(amt.mean())
    if avg20 <= 0:
        return _insufficient(name, label, '20日均量为0')

    vol_ratio = avg5 / avg20
    idx_ret = _ret_over(index_df['close'], 5)
    if idx_ret is None:
        return _insufficient(name, label, '指数5日涨幅无法计算')

    high_volume = vol_ratio > mult
    flat_price = idx_ret <= flat
    red = high_volume and flat_price
    return {
        'name': name, 'label': label, 'red': red,
        'value': vol_ratio, 'threshold': mult,
        'detail': (f'5日均量/20日均量={vol_ratio:.2f} (阈值>{mult}) '
                   f'{"放量" if high_volume else "未放量"}；'
                   f'指数5日涨幅={idx_ret*100:.2f}% {"≤0滞涨" if flat_price else ">0上涨"}'),
        'data_sufficient': True,
        'red_reason': (f'量比{vol_ratio:.2f}放量·指数5日{idx_ret*100:+.2f}%滞涨'
                       if red else ''),
    }


# ====================================================================
# S3: 涨跌家数比 从高位快速回落
# ====================================================================

def check_ad_ratio(ad_ratio_series, cfg: AlarmConfig) -> dict:
    """S3: max(ad_ratio[-20:]) > AD_HIGH_REF 且 ad_ratio[-1] < AD_LOW_THRESHOLD。

    Args:
        ad_ratio_series: list/np/Series，每日涨跌家数比，升序，末值为今日。
    """
    name, label = 'ad_ratio_bearish', 'S3 涨跌家数比回落'
    high_ref = cfg.AD_HIGH_REF
    low_th = cfg.AD_LOW_THRESHOLD

    if ad_ratio_series is None or len(ad_ratio_series) == 0:
        return _insufficient(name, label, '涨跌家数序列为空')

    # 需≥5日历史以判定"曾高后回落"模式（仅今日1点无法判定）
    if len(ad_ratio_series) < 5:
        return _insufficient(name, label, f'仅{len(ad_ratio_series)}日历史，需≥5日')

    import math
    series = list(ad_ratio_series)[-20:]
    today = float(ad_ratio_series[-1])
    # inf（普涨跌停极端）视为很高
    past_max = max(series)
    inf_sentinel = float(getattr(cfg, 'INF_SENTINEL', 1e9))
    past_max_finite = past_max if not math.isinf(past_max) else inf_sentinel

    was_high = past_max_finite > high_ref
    now_low = today < low_th
    red = was_high and now_low
    return {
        'name': name, 'label': label, 'red': red,
        'value': today, 'threshold': low_th,
        'detail': (f'今日涨跌比={today:.2f} ({"<1.0已回落" if now_low else "≥1.0未回落"})；'
                   f'近20日最高={past_max_finite if math.isinf(past_max) else f"{past_max_finite:.2f}"} '
                   f'({">2.5曾沸腾" if was_high else "≤2.5未沸腾"})'),
        'data_sufficient': True,
        'red_reason': (f'今日{today:.2f}回落·20日最高{past_max_finite if math.isinf(past_max) else f"{past_max_finite:.2f}"}'
                       if red else ''),
    }


# ====================================================================
# S4: 高股息逆势走强
# ====================================================================

def check_dividend_strength(index_df: Optional[pd.DataFrame],
                            etf_prices: Optional[dict], cfg: AlarmConfig) -> dict:
    """S4: 沪深300近20日涨幅<0（市场跌）且 高股息篮子绝对正收益
    且 (高股息-成长) 超额 > DIVIDEND_SPREAD_PCT。"""
    name, label = 'dividend_strength', 'S4 高股息逆势走强'
    spread_th = cfg.DIVIDEND_SPREAD_PCT
    strong_th = cfg.DIVIDEND_STRONG_PCT
    n = cfg.LOOKBACK_DAYS

    if index_df is None or index_df.empty or 'close' not in index_df.columns:
        return _insufficient(name, label, '指数日线为空')
    if len(index_df) < n + 1:
        return _insufficient(name, label, f'指数日线不足{n+1}行')

    market_ret = _ret_over(index_df['close'], n)
    if market_ret is None:
        return _insufficient(name, label, '市场20日涨幅无法计算')

    div_ret = _basket_return(etf_prices, cfg.DIVIDEND_BASKET, n)
    gro_ret = _basket_return(etf_prices, cfg.GROWTH_BASKET, n)
    if div_ret is None or gro_ret is None:
        return _insufficient(name, label, 'ETF 篮子数据不足')

    spread = div_ret - gro_ret
    market_down = market_ret < 0
    div_strong = div_ret > strong_th
    spread_ok = spread > spread_th
    red = market_down and div_strong and spread_ok
    return {
        'name': name, 'label': label, 'red': red,
        'value': spread, 'threshold': spread_th,
        'detail': (f'市场20日={market_ret*100:.2f}% {"跌" if market_down else "涨"}；'
                   f'高股息={div_ret*100:.2f}% {"正收益" if div_strong else "非正"}；'
                   f'超额成长={spread*100:.2f}% (阈值>{spread_th*100:.0f}%)'),
        'data_sufficient': True,
        'red_reason': (f'市场{market_ret*100:+.2f}%下跌·高股息超额{spread*100:.2f}%'
                       if red else ''),
    }


# ====================================================================
# S5: 成长股破位
# ====================================================================

def check_growth_breakdown(etf_prices: Optional[dict], cfg: AlarmConfig) -> dict:
    """S5: 成长篮子近20日涨幅 < GROWTH_BREAK_PCT 或 跌破近20日收盘均线。"""
    name, label = 'growth_breakdown', 'S5 成长股破位'
    break_th = cfg.GROWTH_BREAK_PCT
    n = cfg.LOOKBACK_DAYS

    gro_ret = _basket_return(etf_prices, cfg.GROWTH_BASKET, n)
    if gro_ret is None:
        return _insufficient(name, label, '成长篮子数据不足')

    # 跌破近20日均线判定：篮子内今日均价 < 近20日均价均值
    below_ma = _basket_below_ma(etf_prices, cfg.GROWTH_BASKET, n)

    ret_break = gro_ret < break_th
    red = ret_break or below_ma
    reason = []
    if ret_break:
        reason.append(f'近20日{gro_ret*100:.2f}%<{break_th*100:.0f}%')
    if below_ma:
        reason.append('跌破20日均线')
    if not reason:
        reason.append('未破位')
    return {
        'name': name, 'label': label, 'red': red,
        'value': gro_ret, 'threshold': break_th,
        'detail': '；'.join(reason),
        'data_sufficient': True,
        'red_reason': '；'.join(reason) if red else '',
    }


# ====================================================================
# 篮子工具
# ====================================================================

def _basket_return(etf_prices: Optional[dict], basket: list, n: int) -> Optional[float]:
    """计算篮子内各 ETF 的 n 日涨幅均值。任一缺失或数据不足返回 None。"""
    if not etf_prices or not basket:
        return None
    rets = []
    for sym in basket:
        df = etf_prices.get(sym)
        if df is None or df.empty or 'close' not in df.columns:
            return None
        r = _ret_over(df['close'], n)
        if r is None:
            return None
        rets.append(r)
    return sum(rets) / len(rets) if rets else None


def _basket_below_ma(etf_prices: Optional[dict], basket: list, n: int) -> bool:
    """篮子今日均价 是否 < 近 n 日均价的均值（跌破均线）。"""
    if not etf_prices or not basket:
        return False
    today_prices, ma_prices = [], []
    for sym in basket:
        df = etf_prices.get(sym)
        if df is None or df.empty or 'close' not in df.columns:
            return False
        close = df['close']
        if len(close) < n + 1:
            return False
        today_prices.append(float(close.iloc[-1]))
        ma_prices.append(float(close.iloc[-n:].mean()))
    if not ma_prices:
        return False
    today_avg = sum(today_prices) / len(today_prices)
    ma_avg = sum(ma_prices) / len(ma_prices)
    return today_avg < ma_avg


# ====================================================================
# S6: 两融杠杆异常（过热 + 去杠杆）
# ====================================================================

def check_margin_leverage(margin_df: Optional[pd.DataFrame],
                          market_amount_today: Optional[float],
                          total_market_cap: float,
                          cfg: AlarmConfig) -> dict:
    """S6: 两融杠杆异常信号。

    红灯分支（cfg.MARGIN_RED_MODE 控制最终合并逻辑，默认 OR）：
      - 过热分支：任一命中即亮
          * 融资余额 5 日增速 > MARGIN_GROWTH_5D（快速加杠杆）
          * 融资余额 20 日增速 > MARGIN_GROWTH_20D
          * 融资买入额 / 两市成交额 > MARGIN_BUY_RATIO（杠杆交易过热）
          * (融资余额-融券余额) / 总市值 > MARGIN_NET_LEVERAGE（净多头杠杆过高）
      - 去杠杆分支：任一命中即亮
          * 融资余额 5 日回撤 < MARGIN_DELEV_5D（被动去杠杆，-2% 默认）
          * 融资余额 10 日回撤 < MARGIN_DELEV_10D

    Args:
        margin_df:          data_loader.fetch_margin_summary 返回
                            (列含 date, rzye, rzmre, rqye, rzrqye, 单位元)
        market_amount_today:S1 两市总成交额（元）；用于融资买入额/成交额 占比
        total_market_cap:   S1 用的总市值（元）
        cfg:                AlarmConfig
    """
    name, label = 'margin_leverage', 'S6 两融杠杆异常'

    # ---- 数据不足 ----
    if margin_df is None or margin_df.empty:
        return _insufficient(name, label, '两融汇总为空')
    req_cols = ['rzye']
    if not set(req_cols).issubset(margin_df.columns):
        return _insufficient(name, label, f'缺少列 {req_cols}')

    # ---- 整体 try/except：单源脏数据（NaN/inf/异常字段）不崩全流程，降级灰灯 ----
    try:
        rzye = pd.to_numeric(margin_df['rzye'], errors='coerce')
        rzye = rzye.dropna()
        if len(rzye) < 6:
            return _insufficient(name, label, f'融资余额有效行 {len(rzye)} < 6')

        rzye_vals = list(rzye.values.astype(float))
        last = rzye_vals[-1]
        if last <= 0 or not np.isfinite(last):
            return _insufficient(name, label, '最新融资余额<=0或非有限')

        # ---- 各增速计算（防除零 / NaN 基准）----
        def _ret(n: int) -> Optional[float]:
            if len(rzye_vals) < n + 1:
                return None
            base = rzye_vals[-1 - n]
            # 基准为 0 / NaN / inf → 放弃该周期增速，返回 None（降级为该子条件不参与判定）
            if base is None or not np.isfinite(float(base)) or float(base) == 0:
                return None
            return rzye_vals[-1] / float(base) - 1.0

        g5 = _ret(5)
        g20 = _ret(20)
        d5 = g5  # 去杠杆用同一指标（负值代表回撤）
        d10 = _ret(10)

        # ---- 融资买入额占比 ----
        buy_ratio: Optional[float] = None
        if market_amount_today and market_amount_today > 0 and 'rzmre' in margin_df.columns:
            rzmre_col = pd.to_numeric(margin_df['rzmre'], errors='coerce').dropna()
            if len(rzmre_col) > 0:
                buy_ratio = float(rzmre_col.iloc[-1]) / float(market_amount_today)

        # ---- 净杠杆水平 ----
        net_leverage: Optional[float] = None
        if 'rqye' in margin_df.columns and total_market_cap > 0:
            rqye_col = pd.to_numeric(margin_df['rqye'], errors='coerce').dropna()
            if len(rqye_col) > 0:
                rqye_last = float(rqye_col.iloc[-1])
                net = float(last) - rqye_last  # 融资余额 - 融券余额
                if net > 0:
                    net_leverage = net / float(total_market_cap)

        # ---- 过热/去杠杆分别亮 ----
        hot_flags = {}
        th_g5 = float(cfg.MARGIN_GROWTH_5D)
        th_g20 = float(cfg.MARGIN_GROWTH_20D)
        th_buy = float(cfg.MARGIN_BUY_RATIO)
        th_net = float(cfg.MARGIN_NET_LEVERAGE)
        th_d5 = float(cfg.MARGIN_DELEV_5D)
        th_d10 = float(cfg.MARGIN_DELEV_10D)

        if g5 is not None:
            hot_flags['5日增速'] = g5 > th_g5
        if g20 is not None:
            hot_flags['20日增速'] = g20 > th_g20
        if buy_ratio is not None:
            hot_flags['融资买入占比'] = buy_ratio > th_buy
        if net_leverage is not None:
            hot_flags['净多头杠杆'] = net_leverage > th_net

        delev_flags = {}
        if d5 is not None:
            delev_flags['5日回撤'] = d5 < th_d5
        if d10 is not None:
            delev_flags['10日回撤'] = d10 < th_d10

        hot_any = any(hot_flags.values()) if hot_flags else False
        delev_any = any(delev_flags.values()) if delev_flags else False

        mode = str(getattr(cfg, 'MARGIN_RED_MODE', 'OR')).upper()
        if mode == 'BOTH':
            red = hot_any and delev_any
        else:
            red = hot_any or delev_any

        # ---- 明细（人类可读） ----
        def _fmt_pct(x: Optional[float]) -> str:
            if x is None:
                return '—'
            return f'{x * 100:.2f}%'

        parts = [f"最新融资余额 {last / 1e12:.2f}万亿"]
        # 过热明细
        if g5 is not None:
            parts.append(f"5日融资增速 {_fmt_pct(g5)} {'超' if hot_flags.get('5日增速') else '≤'} {th_g5*100:.1f}%")
        if g20 is not None:
            parts.append(f"20日融资增速 {_fmt_pct(g20)} {'超' if hot_flags.get('20日增速') else '≤'} {th_g20*100:.1f}%")
        if buy_ratio is not None:
            parts.append(f"融资买入/两市 {buy_ratio*100:.2f}% {'超' if hot_flags.get('融资买入占比') else '≤'} {th_buy*100:.1f}%")
        if net_leverage is not None:
            parts.append(f"净多头杠杆 {net_leverage*100:.2f}% {'超' if hot_flags.get('净多头杠杆') else '≤'} {th_net*100:.2f}%")
        # 去杠杆明细
        if d5 is not None:
            parts.append(f"5日回撤 {_fmt_pct(d5)} {'破' if delev_flags.get('5日回撤') else '≥'} {th_d5*100:.1f}%")
        if d10 is not None:
            parts.append(f"10日回撤 {_fmt_pct(d10)} {'破' if delev_flags.get('10日回撤') else '≥'} {th_d10*100:.1f}%")
        # 合并模式
        parts.append(f"红逻辑 {mode}")
        detail = '；'.join(parts)

        # value：用 net_leverage 作为主数值便于后续统计（缺则用 g5，再缺用 0）
        if net_leverage is not None:
            value = float(net_leverage)
        elif g5 is not None:
            value = float(g5)
        else:
            value = float('nan')
        # threshold：对应主 value 的阈值
        if net_leverage is not None:
            threshold = th_net
        elif g5 is not None:
            threshold = th_g5
        else:
            threshold = float('nan')

        # 红灯子分类（简短原因，供 reporter 亮灯信号行展示）
        if bool(red):
            if hot_any and delev_any:
                red_reason = '过热+去杠杆共振'
            elif hot_any:
                # 过热分支命中，附关键数值
                hot_keys = [k for k, v in hot_flags.items() if v]
                red_reason = f'过热（{",".join(hot_keys)}）'
            elif delev_any:
                delev_keys = [k for k, v in delev_flags.items() if v]
                red_reason = f'去杠杆（{",".join(delev_keys)}）'
            else:
                red_reason = '异常'
        else:
            red_reason = ''

        return {
            'name': name, 'label': label, 'red': bool(red),
            'value': value, 'threshold': threshold,
            'detail': detail, 'data_sufficient': True,
            'red_reason': red_reason,
            # 扩展字段（给 reporter/storage 后续扩展）
            'margin_detail': {
                'rzye_yuan': last,
                'grow_5d': g5, 'grow_20d': g20,
                'buy_ratio': buy_ratio,
                'net_leverage': net_leverage,
                'delev_5d': d5, 'delev_10d': d10,
                'hot_any': hot_any,
                'delev_any': delev_any,
                'red_mode': mode,
            }
        }
    except Exception as e:
        # 单源脏数据（NaN/inf/字段异常）→ 降级灰灯，不崩全流程
        logger.warning("check_margin_leverage 异常降级灰灯: %s", e)
        return _insufficient(name, label, f'两融数据异常降级: {e}')


# ====================================================================
# S7: 大盘 KDJ 高位死叉（周线 / 月线）
# ====================================================================

def _resample_ohlc(df: pd.DataFrame, rule: str) -> Optional[pd.DataFrame]:
    """日线 → 周线/月线 OHV 聚合。

    rule: 'W'（周线，周一开盘周一为锚）或 'M'（月线，自然月）。
    要求 df 含 date 列（或可作 index）+ open/high/low/close/amount 列。
    聚合后列：date(期末交易日)、open(期初)、high(最高)、low(最低)、close(期末)、amount(总和)。
    """
    if df is None or df.empty:
        return None
    work = df.copy()
    # 确保有可解析的时间列
    if 'date' in work.columns:
        work['dt'] = pd.to_datetime(work['date'], errors='coerce')
    elif 'datetime' in work.columns:
        work['dt'] = pd.to_datetime(work['datetime'], errors='coerce')
    else:
        # index 是时间
        work['dt'] = pd.to_datetime(work.index, errors='coerce')
    work = work.dropna(subset=['dt'])
    if work.empty:
        return None
    work = work.set_index('dt')
    # pandas 新版不再支持 'M'/'Y'，需用 'ME'/'YE'；'W' 仍兼容
    safe_rule = rule
    if rule == 'M':
        safe_rule = 'ME'
    elif rule == 'Y':
        safe_rule = 'YE'
    agg = {}
    for col in ['open', 'high', 'low', 'close', 'amount']:
        if col in work.columns:
            agg[col] = ('first' if col == 'open' else
                       ('max' if col == 'high' else
                        ('min' if col == 'low' else
                         ('last' if col == 'close' else 'sum'))))
    if not agg:
        return None
    out = work.resample(safe_rule).agg(agg)
    # 丢弃全空区间（如周末/节假日形成的空周/空月）
    out = out.dropna(subset=['close'], how='all')
    if out.empty:
        return None
    out = out.reset_index()
    # 期末交易日作为该周期的 date 标签
    if 'dt' in out.columns:
        out = out.rename(columns={'dt': 'date'})
    return out


def _calc_kdj(high: pd.Series, low: pd.Series, close: pd.Series,
              n: int, k_smooth: int, d_smooth: int) -> 'tuple[np.ndarray, np.ndarray, np.ndarray]':
    """国内版 KDJ 计算（SMA(m, n) 平滑，初始 K=D=50）。

    RSV = (close - low_n.min) / (high_n.max - low_n.min) * 100
    K_t = (K_{t-1} * (k_smooth - 1) + RSV_t) / k_smooth
    D_t = (D_{t-1} * (d_smooth - 1) + K_t) / d_smooth
    J_t = 3*K_t - 2*D_t
    返回 (K, D, J) 一维 ndarray，与输入等长；前 n-1 个为 NaN。
    """
    hi = np.asarray(high, dtype=float)
    lo = np.asarray(low, dtype=float)
    cl = np.asarray(close, dtype=float)
    length = len(cl)
    rsv = np.full(length, np.nan)
    for i in range(n - 1, length):
        h_max = np.nanmax(hi[i - n + 1:i + 1])
        l_min = np.nanmin(lo[i - n + 1:i + 1])
        if h_max == l_min:
            rsv[i] = 50.0  # 区间无波动，中性
        else:
            rsv[i] = (cl[i] - l_min) / (h_max - l_min) * 100.0

    k = np.full(length, np.nan)
    d = np.full(length, np.nan)
    # 初始 K=D=50（国内惯例，首个有效 RSV 处用 50 作为前值）
    k_prev = 50.0
    d_prev = 50.0
    for i in range(length):
        if np.isnan(rsv[i]):
            continue
        k_cur = (k_prev * (k_smooth - 1) + rsv[i]) / k_smooth
        d_cur = (d_prev * (d_smooth - 1) + k_cur) / d_smooth
        k[i] = k_cur
        d[i] = d_cur
        k_prev = k_cur
        d_prev = d_cur
    j = 3 * k - 2 * d
    return k, d, j


def _find_recent_cross(k: np.ndarray, d: np.ndarray,
                      lookback: int) -> 'Optional[dict]':
    """在最近 lookback 个周期内寻找最新一次金叉或死叉（不要求高位）。

    死叉：k[i-1] >= d[i-1] 且 k[i] < d[i]
    金叉：k[i-1] <= d[i-1] 且 k[i] > d[i]
    返回：{idx, cross_type: 'death'/'gold', k_prev, d_prev, k_cur, d_cur}，无则 None。
    """
    n = len(k)
    if n < 2:
        return None
    start = max(1, n - lookback)
    for i in range(n - 1, start - 1, -1):
        if np.isnan(k[i]) or np.isnan(d[i]) or np.isnan(k[i - 1]) or np.isnan(d[i - 1]):
            continue
        death = (k[i - 1] >= d[i - 1]) and (k[i] < d[i])
        gold = (k[i - 1] <= d[i - 1]) and (k[i] > d[i])
        if death or gold:
            return {
                'idx': i,
                'cross_type': 'death' if death else 'gold',
                'k_prev': float(k[i - 1]), 'd_prev': float(d[i - 1]),
                'k_cur': float(k[i]), 'd_cur': float(d[i]),
            }
    return None


def _check_divergence(close_arr: 'np.ndarray', k_arr: 'np.ndarray',
                      lookback_bars: int = 60, diverg_type: str = 'bull') -> bool:
    """底背离 / 顶背离检测。

    底背离（bull）：股价创新低（当前 close < lookback 内 min_close），但 K 值未创新低。
    顶背离（bear）：股价创新高，而 K 值未创新高。
    依赖：序列有效行 ≥ lookback_bars；返回 bool，数据不足时返回 False。
    """
    import numpy as _np
    valid_idx = ~(_np.isnan(close_arr) | _np.isnan(k_arr))
    if int(valid_idx.sum()) < max(30, lookback_bars // 2):
        return False
    cl = _np.asarray(close_arr, dtype=float)
    kk = _np.asarray(k_arr, dtype=float)
    last = len(cl) - 1
    # 往左找最近有效点作为"当前"点（允许尾部有 NaN）
    while last >= 0 and (_np.isnan(cl[last]) or _np.isnan(kk[last])):
        last -= 1
    if last < 1:
        return False
    window_start = max(0, last - lookback_bars)
    sub_cl = cl[window_start:last + 1]
    sub_k = kk[window_start:last + 1]
    # 去掉 NaN 再比较
    mask = ~(_np.isnan(sub_cl) | _np.isnan(sub_k))
    if int(mask.sum()) < 5:
        return False
    sub_cl_c = sub_cl[mask]
    sub_k_c = sub_k[mask]
    cur_close = float(sub_cl_c[-1])
    cur_k = float(sub_k_c[-1])
    if diverg_type == 'bull':
        min_close_prev = float(_np.nanmin(sub_cl_c[:-1])) if len(sub_cl_c) > 1 else cur_close
        min_k_prev = float(_np.nanmin(sub_k_c[:-1])) if len(sub_k_c) > 1 else cur_k
        # 股价创新低但 K 未创新低
        if cur_close < min_close_prev and cur_k > min_k_prev:
            return True
        return False
    else:  # bear（当前场景暂未用 S7 顶背离展示，保留后续扩展）
        max_close_prev = float(_np.nanmax(sub_cl_c[:-1])) if len(sub_cl_c) > 1 else cur_close
        max_k_prev = float(_np.nanmax(sub_k_c[:-1])) if len(sub_k_c) > 1 else cur_k
        if cur_close > max_close_prev and cur_k < max_k_prev:
            return True
        return False


def check_kdj_divergence(index_df: Optional[pd.DataFrame], cfg: AlarmConfig) -> dict:
    """S7: 大盘周/月线 KDJ 高位死叉。

    逻辑：
        - 从 cfg.KDJ_INDEX（独立配置，默认 sh000300 沪深300）日线 resample 成周线/月线
          （若 KDJ_INDEX == BREADTH_INDEX，则 main._gather_data 直接复用主指数日线，不重复拉取；
           若不同如 sh000688 科创50，main 会单独拉 260 日窗口作为 index_df 传入）
        - 计算国内版 KDJ（RSV 9, K/D SMA(3,1) 平滑）
        - 在最近 KDJ_DEATH_CROSS_LOOKBACK 个周期内寻找高位死叉
          （K 下穿 D 且前一期 K > KDJ_OVERBOUGHT）
        - KDJ_RED_MODE=OR：周或月任一高位死叉 → S7 红
          KDJ_RED_MODE=AND：需周+月双周期共振才红
    数据不足（指数日线为空 / 周/月线 < n+2 行）→ 灰灯，不阻断。
    """
    name, label = 'kdj_death_cross', 'S7 大盘KDJ高位死叉'

    if index_df is None or index_df.empty:
        return _insufficient(name, label, '指数日线为空')
    need_cols = {'high', 'low', 'close'}
    if not need_cols.issubset(set(index_df.columns)):
        return _insufficient(name, label, f'缺列 {need_cols}')

    n = int(cfg.KDJ_RSV_PERIOD)
    k_smooth = int(cfg.KDJ_K_SMOOTH)
    d_smooth = int(cfg.KDJ_D_SMOOTH)
    overbought = float(cfg.KDJ_OVERBOUGHT)
    cross_lookback = int(cfg.KDJ_DEATH_CROSS_LOOKBACK)
    red_mode = str(getattr(cfg, 'KDJ_RED_MODE', 'OR')).upper()

    # 周/月线计算
    periods = []
    if bool(cfg.KDJ_USE_WEEKLY):
        periods.append(('周线', 'W'))
    if bool(cfg.KDJ_USE_MONTHLY):
        periods.append(('月线', 'M'))

    if not periods:
        return _insufficient(name, label, '周/月线均未启用')

    oversold = float(getattr(cfg, 'KDJ_OVERSOLD', 20.0))  # 超卖阈值（与 overbought=80 对称，J<0 额外标"极度超卖"）

    # 每个周期的完整状态包 {period_name: dict}
    period_states: 'dict[str, dict]' = {}
    # 红灯命中：仅"高位死叉"（即 cross_type=death 且 前一期 K>overbought）才进入 branch_hits
    branch_hits: 'dict[str, dict]' = {}
    # 保存周线 close + K 序列，用于底背离 & 历史分位（优先级：周线，若没有才用月线）
    weekly_k_valid = None
    weekly_close_valid = None

    for label_p, rule in periods:
        agg_df = _resample_ohlc(index_df, rule)
        if agg_df is None or len(agg_df) < n + 2:
            continue
        k_arr, d_arr, j_arr = _calc_kdj(agg_df['high'], agg_df['low'], agg_df['close'],
                                        n, k_smooth, d_smooth)
        # 过滤尾部 NaN（最近周期可能因数据不全为 NaN，取最后有效位置）
        valid_mask = ~(pd.isna(k_arr) | pd.isna(d_arr))
        if not valid_mask.any():
            continue
        last_valid = int(np.where(valid_mask)[0][-1])
        k_now = float(k_arr[last_valid])
        d_now = float(d_arr[last_valid])
        j_now = float(j_arr[last_valid]) if not np.isnan(j_arr[last_valid]) else float('nan')
        # 当前金叉/死叉状态（K<D 死叉 / K>D 金叉）
        cross_state = 'death' if k_now < d_now else 'gold'  # death / gold
        # 最近拐点叉：两个 lookback 分离 —— 用途不同、时间窗口不同，彻底避免混淆
        #   ① wide: 用于"趋势形态/方向/历史拐点展示"（宽松 24 周期，让 reporter 能显示"上次的叉发生过"）
        #   ② tight: 用于"高位死叉红信号"的有效期（严格 3 周期 cross_lookback=cfg.KDJ_DEATH_CROSS_LOOKBACK）
        #     → 交易实战中：高位死叉是"见顶那一刻"的一次性信号，K 已经从 80 跌到 20 就不能继续说"还在红灯"
        last_cross_wide = _find_recent_cross(k_arr, d_arr, lookback=max(cross_lookback, 24))
        last_cross_tight = _find_recent_cross(k_arr, d_arr, lookback=cross_lookback)

        # 四态趋势形态（按 k_now 的高/低 + 当前金叉/死叉组合）
        if k_now > overbought:
            zone = 'high'    # 高位
        elif k_now < oversold:
            zone = 'low'     # 低位
        else:
            zone = 'mid'     # 中性区间
        # 四态 + emoji（供 reporter 直接使用）
        state4 = ('高位死叉', '🔴') if (zone == 'high' and cross_state == 'death') else \
                 ('高位金叉', '🟠') if (zone == 'high' and cross_state == 'gold') else \
                 ('低位死叉', '🟡') if (zone == 'low' and cross_state == 'death') else \
                 ('低位金叉', '🟢') if (zone == 'low' and cross_state == 'gold') else \
                 ('死叉（中位）', '⬜') if cross_state == 'death' else \
                 ('金叉（中位）', '🟫')

        # 方向箭头：⬆️ 金叉区间(K>D)且 K 向上；⬇️ 死叉区间(K<D)且 K 向下；否则 ↔️
        # 用 2 周期 K 的斜率近似：若 last_valid 前 1 个有效点存在，比较斜率
        k_slope = 0.0
        prev_valid_idx = -1
        for j in range(last_valid - 1, -1, -1):
            if not (np.isnan(k_arr[j]) or np.isnan(d_arr[j])):
                prev_valid_idx = j
                break
        if prev_valid_idx >= 0:
            k_slope = k_now - float(k_arr[prev_valid_idx])
        if cross_state == 'gold' and k_slope >= -0.01:
            direction = 'up'   # ⬆️
        elif cross_state == 'death' and k_slope <= 0.01:
            direction = 'down'  # ⬇️
        else:
            direction = 'flat'  # ↔️

        # 周期 J 值定性标签（周线 J<0 极度超卖，月线同理）
        if np.isnan(j_now):
            j_tag = ''
        elif j_now < 0:
            j_tag = '极度超卖'
        elif j_now > 100:
            j_tag = '极度超买'
        elif k_now < oversold:
            j_tag = '超卖'
        elif k_now > overbought:
            j_tag = '超买'
        else:
            j_tag = '中性偏弱' if (cross_state == 'death' or k_now < 50) else '中性偏强'

        # 红灯命中（高位死叉）——**严格用 tight 窗口（近 KDJ_DEATH_CROSS_LOOKBACK 个周期）**
        #   条件：tight 窗口内发生了死叉，且死叉发生时的"前一期 K" > overbought（80 默认）
        #   ⚠️ 禁止用 wide 窗口判定红灯，否则 24 周前的一次高位死叉会让 S7 一直亮红灯，
        #      同时出现"K=15 低位死叉"却亮"高位死叉🔴"的逻辑矛盾。
        hi_red: 'Optional[dict]' = None
        if (last_cross_tight is not None
                and last_cross_tight['cross_type'] == 'death'
                and last_cross_tight['k_prev'] > overbought):
            hi_red = last_cross_tight
            branch_hits[label_p] = {
                'idx': last_cross_tight['idx'],
                'k_prev': last_cross_tight['k_prev'],
                'd_prev': last_cross_tight['d_prev'],
                'k_cur': last_cross_tight['k_cur'],
                'd_cur': last_cross_tight['d_cur'],
                'bars_ago': int(last_valid) - int(last_cross_tight['idx']),  # 距当前几周期
            }
        # wide 下的叉只作展示，不参与亮灯
        for_reporter_cross = last_cross_wide

        # 保存序列供底背离 & 分位数（周线优先）
        if label_p == '周线':
            close_v = agg_df['close'].values.astype(float)
            mask = ~(np.isnan(k_arr) | np.isnan(close_v))
            weekly_k_valid = k_arr[mask]
            weekly_close_valid = close_v[mask]

        period_states[label_p] = {
            'K': round(k_now, 2),
            'D': round(d_now, 2),
            'J': round(j_now, 2) if not np.isnan(j_now) else None,
            'j_tag': j_tag,
            'cross_state': cross_state,              # 'death' / 'gold'
            'cross_state_cn': '死叉' if cross_state == 'death' else '金叉',
            'zone': zone,                             # 'high' / 'mid' / 'low'
            'state4_cn': state4[0],                   # 四态中文
            'state4_emoji': state4[1],                # 四态图标
            'direction': direction,                   # 'up' / 'down' / 'flat'
            'last_cross_wide': for_reporter_cross,    # 展示用：宽 24 周期的最近叉
            'last_cross_tight': last_cross_tight,     # 触发用：紧 cross_lookback 窗口
            'high_death_hit': (hi_red is not None),
            'k_slope': round(k_slope, 2),
        }

    if not period_states:
        return _insufficient(name, label, '周/月线均不足有效数据')

    # ── 3. 周期共振分析 ──
    directions = {p: period_states[p]['direction'] for p in period_states}
    if len(directions) >= 2:
        dirs_list = list(directions.values())
        if all(d == 'up' for d in dirs_list):
            resonance = '多头共振'
        elif all(d == 'down' for d in dirs_list):
            resonance = '空头共振'
        else:
            resonance = '分歧'
    else:
        single_p = list(directions.keys())[0]
        resonance = f'{single_p}方向={directions[single_p]}'

    # ── 底背离（只看周线，近 60 周窗口）──
    bull_divergence = False
    if weekly_k_valid is not None and weekly_close_valid is not None and len(weekly_k_valid) > 30:
        bull_divergence = _check_divergence(weekly_close_valid, weekly_k_valid,
                                            lookback_bars=60, diverg_type='bull')

    # ── 近1年（约52周）周K 历史分位数 ──
    weekly_k_pctl: 'Optional[float]' = None
    if weekly_k_valid is not None and len(weekly_k_valid) >= 20:
        recent = weekly_k_valid[-52:] if len(weekly_k_valid) >= 52 else weekly_k_valid
        cur_v = float(recent[-1])
        pctl = float(np.mean(np.array(recent) <= cur_v)) * 100.0
        weekly_k_pctl = round(pctl, 1)

    # 红灯合并
    # AND：只对"实际成功计算出 KDJ 的周期"（period_states）要求全部命中高位死叉。
    #      注意不能用 periods（配置的周期列表）——若某周期数据不足未进 period_states，
    #      用 periods 会导致 AND 永久为 False（灰周期永久不红）。
    actual_periods = list(period_states.keys())
    if red_mode == 'AND':
        red = (len(actual_periods) > 0) and all(p in branch_hits for p in actual_periods)
    else:  # OR
        red = len(branch_hits) > 0

    # value / threshold：
    #   red=True 时优先用"命中周期"的最新 K（保证 value/文案周期与触发周期一致）；
    #   red=False 时用主周期（月线优先，否则周线）的最新 K。
    if red and branch_hits:
        # 取第一个命中周期（多周期命中时取月线优先，其次周线）
        hit_prio = ['月线', '周线']
        value_period = next((p for p in hit_prio if p in branch_hits),
                            next(iter(branch_hits)))
    else:
        value_period = ('月线' if '月线' in period_states
                        else ('周线' if '周线' in period_states else None))
    if value_period is not None and value_period in period_states:
        value = float(period_states[value_period]['K'])
    else:
        value = float('nan')
    threshold = float(overbought)

    # ── 4. 操作指引（定性 + 建议）──
    # 先拼周/月 K 级别的典型档位
    wk = period_states.get('周线')
    mk = period_states.get('月线')
    wk_k = float(wk['K']) if wk else None
    mk_k = float(mk['K']) if mk else None
    wk_j = (float(wk['J']) if (wk and wk.get('J') is not None) else None)

    # 定性（极度超跌 / 底部超卖 / 中性 / 高位风险 / 过热泡沫）
    extreme_oversold_k = float(getattr(cfg, 'KDJ_EXTREME_OVERSOLD_K', 15.0))
    if (wk_j is not None and wk_j < 0) or (wk_k is not None and wk_k < extreme_oversold_k):
        quality = '极度超跌'
    elif wk_k is not None and wk_k < oversold:
        quality = '底部超卖'
    elif (wk_k is not None and wk_k > overbought) or (mk_k is not None and mk_k > overbought):
        quality = '过热泡沫' if (wk_k is not None and wk_k > overbought + 10) else '高位风险'
    else:
        quality = '中性'

    # 操作建议（基于定性 + 周线金叉/死叉 + 共振）
    #
    # ⚠️  Bug4 修复："空头共振→任何反弹先减仓"的建议不能直接叠加在"极度超跌→不割肉"上，
    #      否则对小白是矛盾指令。原则：
    #      · 极度超跌（周J<0 / 周K<15）+ 空头共振：优先"不杀跌"，
    #        仅对重仓者提"反弹时可适度减杠杆"（不是清仓式减仓），明确空仓不追空。
    #      · 底部超卖（周K<20）+ 空头共振：持仓控半仓，不追空也不重仓抄底。
    #      · 中性/高位 + 共振：维持原"减仓/回踩买入"（这部分是原逻辑）。
    wk_cross = period_states['周线']['cross_state_cn'] if '周线' in period_states else ''
    mk_cross = period_states['月线']['cross_state_cn'] if '月线' in period_states else ''
    advice_lines = []
    if quality == '极度超跌':
        advice_lines.append('重仓被套：躺平不动，等周线金叉再加仓做T，不在超跌区杀跌')
        advice_lines.append('轻仓观望：等待放量阳线或底背离确认再进场，不追空')
        if bull_divergence:
            advice_lines.append('注意：当前出现底背离✅，衰竭性下跌末端概率增大，可分批建底仓')
        # 极度超跌 + 空头共振：保持"不杀跌"主基调 + 追加降杠杆小建议（非清仓）
        if resonance == '空头共振':
            advice_lines.append('⚠️ 虽处周月双空头共振，但当前已极度超跌，重仓被套者不在此位割肉，反弹时可降一点杠杆，空仓坚决不追空')
    elif quality == '底部超卖':
        advice_lines.append('持有筹码继续持有，空仓可小仓位试探（不超过半仓）')
        advice_lines.append('周线金叉后加仓')
        if resonance == '空头共振':
            advice_lines.append('⚠️ 周月双空头共振 + 底部超卖：持仓控制在半仓内，不追空也不重仓抄底')
    elif quality == '过热泡沫' or quality == '高位风险':
        advice_lines.append(f'减仓/止盈，K/D J 高位死叉确认时果断离场')
        advice_lines.append(f'D={wk["D"] if wk else (mk["D"] if mk else "—")} 可作为反弹阻力位参考')
    else:  # 中性
        advice_lines.append('仓位随 S1-S6 组合建议，KDJ 不提供方向性加减仓')
        advice_lines.append('关注周线"金叉/死叉"拐点，作为加减仓时机参考')

    # 共振建议（仅当 quality 没有单独写共振分支时追加；高位/中性保留原语义）
    if resonance == '空头共振':
        if quality in ('中性', '高位风险', '过热泡沫'):
            advice_lines.append('⚠️ 周月双空头共振，任何反弹先减仓')
    elif resonance == '多头共振':
        advice_lines.append('✅ 周月双多头共振，回踩都是买点')
    elif quality == '中性':
        # 中性 + 分歧：提示小仓位
        advice_lines.append(f'当前{resonance}，以"小仓位滚动"为主')

    # ── 一句话总结（给 reporter 末尾快速扫读）──
    summary_bits = []
    if quality == '极度超跌':
        summary_bits.append('短期砸出"超跌坑"（J值负）')
    elif quality == '底部超卖':
        summary_bits.append('短期处于超卖区间，抛压释放中')
    elif quality == '高位风险':
        summary_bits.append('周线处于高风险区（K>80），警惕回调')
    elif quality == '过热泡沫':
        summary_bits.append('KDJ 过热泡沫区间，追高风险极大')
    if '周线' in period_states and '月线' in period_states:
        if period_states['月线']['cross_state'] == 'death' and period_states['月线']['K'] < 50:
            summary_bits.append('但月线尚未走好，属于左侧磨底阶段')
        elif period_states['月线']['cross_state'] == 'gold' and period_states['月线']['K'] > 50:
            summary_bits.append('月线趋势已转多，叠加短中期')
    if quality in ('极度超跌', '底部超卖'):
        summary_bits.append('不追空但也不急着抄底，等待金叉确认')
    elif quality in ('高位风险', '过热泡沫'):
        summary_bits.append('逢高分批止盈，不要在高位死叉后硬扛')
    else:
        summary_bits.append('维持中性仓位，等待下一个拐点确认')
    one_liner_summary = '，'.join(summary_bits)

    # ── red_reason：子分类（沿用旧风格，保持亮灯信号行短格式不变）──
    if red:
        hit_names = list(branch_hits.keys())
        if len(hit_names) == 1:
            hp = hit_names[0]
            h = branch_hits[hp]
            red_reason = f'{hp}高位死叉（K {h["k_prev"]:.1f}→{h["k_cur"]:.1f}）'
        else:
            parts = []
            for hp in hit_names:
                h = branch_hits[hp]
                parts.append(f'{hp}K {h["k_prev"]:.1f}→{h["k_cur"]:.1f}')
            red_reason = '双周期高位死叉（' + '；'.join(parts) + '）'
    else:
        red_reason = ''

    # ── detail：沿用旧格式（保持 CSV/历史向后兼容），末尾再附加扩展结构即可
    parts = []
    for label_p, _rule in periods:
        if label_p in period_states:
            s = period_states[label_p]
            cross_cn = s['cross_state_cn']
            hit_mark = '〔高位死叉命中〕' if label_p in branch_hits else ''
            sK = s['K']
            sD = s['D']
            sJ = s.get('J')
            if sJ is None:
                j_fmt = '—'
            else:
                j_fmt = f'{sJ:.1f}'
            parts.append(f'{label_p} K={sK:.1f} D={sD:.1f} J={j_fmt}（{cross_cn} {s["state4_cn"]}{s["state4_emoji"]}）{hit_mark}')
        else:
            parts.append(f'{label_p} 数据不足')
    parts.append(f'红逻辑 {red_mode}（高位阈值 K>{overbought:.0f}）')
    detail = '；'.join(parts)

    # ── 5. 红逻辑触发单独行（真实触发条件：近 N 周期高位死叉，K 前值>overbought）──
    #   ⚠️ Bug3 根因：之前文案误写为"周K>80"，让用户以为判定条件是"当前K值>80"，
    #      实际正确条件是"近 KDJ_DEATH_CROSS_LOOKBACK 个周期内发生死叉，且死叉发生瞬间的前一期 K > 80"。
    #      修正后文案必须同时给出：阈值说明、是否触发、当前周K值（便于交叉验证）。
    wk_k_for_trigger = float(period_states['周线']['K']) if '周线' in period_states else (
        float(period_states['月线']['K']) if '月线' in period_states else float('nan'))
    # 真实条件描述（用于 reporter 渲染）
    cond_desc = f'近 {cross_lookback} 周期高位死叉（死叉发生时 K前值>{overbought:.0f}）'
    red_trigger_line = {
        'threshold_used': cond_desc,
        'triggered': red,
        'current_week_k': round(wk_k_for_trigger, 1) if wk_k_for_trigger == wk_k_for_trigger else None,
        'trigger_reason_hits': list(branch_hits.keys()) if branch_hits else [],
        # 关键：未触发时给出"为什么没触发"的解释（避免用户以为数学错误）
        'untriggered_reason': (
            f'当前周线K={wk_k_for_trigger:.1f} < {overbought:.0f}，且近 {cross_lookback} 周期内无高位死叉'
            if not red else ''
        ),
    }

    return {
        'name': name, 'label': label, 'red': bool(red),
        'value': value, 'threshold': threshold,
        'detail': detail, 'data_sufficient': True,
        'red_reason': red_reason,
        # 扩展字段（2026-09-04 增强，供 reporter 渲染 5 段展示区）
        'kdj_detail': {
            'periods': list(period_states.keys()),
            'period_states': period_states,                  # 核心数值 + 趋势四态
            'branch_hits': branch_hits,                       # 高位死叉命中（供亮灯信号行 & 红灯合并用）
            'k_now': {p: s['K'] for p, s in period_states.items()},
            'd_now': {p: s['D'] for p, s in period_states.items()},
            'j_now': {p: s.get('J') for p, s in period_states.items()},
            # 周期共振
            'resonance': resonance,
            'directions': directions,
            # 底背离
            'bull_divergence': bool(bull_divergence),
            # 近 1 年周 K 分位数
            'weekly_k_percentile': weekly_k_pctl,
            # 操作指引
            'quality': quality,
            'advice_lines': advice_lines,
            'one_liner_summary': one_liner_summary,
            # 红逻辑触发
            'red_trigger': red_trigger_line,
            'overbought': overbought,
            'oversold': oversold,
            'red_mode': red_mode,
        }
    }


# ====================================================================
# ADX 趋势过滤器（Wilder 平均趋向指标）
# ====================================================================
# ADX 仅衡量趋势强度（不关心涨跌方向），基于指数 high/low/close 计算。
# 用途：作为市场状态过滤器，动态调整聚合引擎的 red_count 阈值。
#   - ADX > STRONG_TREND_THRESHOLD(30) → 强趋势市，红灯阈值上调
#   - ADX < RANGE_LOW_THRESHOLD(22)    → 弱趋势/震荡市，红灯阈值下调
#   - 中间区                          → 方向不明，维持原判
#
# 算法（Wilder smoothing）：
#   1. TR  = max(high-low, |high-prev_close|, |low-prev_close|)
#   2. +DM = (high-prev_high) if >0 且 > (prev_low-low) else 0
#      -DM = (prev_low-low) if >0 且 > (high-prev_high) else 0
#   3. 平滑 TR/+DM/-DM（Wilder smoothing: S_t = S_{t-1} - S_{t-1}/n + X_t）
#   4. +DI = 100 * smoothed(+DM) / smoothed(TR)
#      -DI = 100 * smoothed(-DM) / smoothed(TR)
#   5. DX = 100 * |+DI - -DI| / (+DI + -DI)
#   6. ADX = Wilder smoothed DX over ADX_PERIOD

def check_adx(index_df: Optional[pd.DataFrame], cfg: AlarmConfig) -> dict:
    """计算 ADX 趋势强度指标，并判定市场状态。

    返回 dict（与 7 信号结构一致，但 red 恒为 False —— ADX 不直接报警，
    而是作为过滤器由 monitor.py 动态调整阈值）：
        - name: 'adx_trend'
        - value: ADX 值（float）
        - threshold: cfg.ADX_TREND_THRESHOLD
        - market_state: 'trend' / 'range' / 'neutral'
        - detail: 人类可读明细
        - data_sufficient: bool
    """
    name, label = 'adx_trend', 'ADX 市场状态'
    period = cfg.ADX_PERIOD

    if index_df is None or index_df.empty:
        return _insufficient(name, label, '指数日线为空')

    for col in ('high', 'low', 'close'):
        if col not in index_df.columns:
            return _insufficient(name, label, f'缺少 {col} 列')

    # ADX 至少需要 2*period 行（period 用于 DI 平滑，再 period 用于 DX→ADX 平滑）
    need = 2 * period + 5
    if len(index_df) < need:
        return _insufficient(name, label, f'指数日线不足{need}行')

    # talib 加速路径（cfg.USE_TALIB=True 且 talib 可用）
    adx_value = None
    if _talib_available(cfg):
        try:
            adx_arr = talib.ADX(
                index_df['high'].astype(float).values,
                index_df['low'].astype(float).values,
                index_df['close'].astype(float).values,
                timeperiod=period,
            )
            if adx_arr is not None and len(adx_arr) > 0:
                v = float(adx_arr[-1])
                if np.isfinite(v):
                    adx_value = v
        except Exception:
            pass  # 降级 numpy

    # numpy 自实现降级（_compute_adx 返回三元组，只取 adx）
    if adx_value is None:
        result = _compute_adx(
            index_df['high'].astype(float).values,
            index_df['low'].astype(float).values,
            index_df['close'].astype(float).values,
            period,
        )
        if result is not None:
            adx_value = result[0]  # (adx, plus_di, minus_di) 的第一项
    if adx_value is None or not np.isfinite(adx_value):
        return _insufficient(name, label, 'ADX 计算异常')

    # 市场状态分类
    if adx_value > cfg.ADX_STRONG_TREND_THRESHOLD:
        state = 'trend'
        state_label = '强趋势市'
        reason = f'ADX={adx_value:.1f} > {cfg.ADX_STRONG_TREND_THRESHOLD:.0f}'
    elif adx_value < cfg.ADX_RANGE_LOW_THRESHOLD:
        state = 'range'
        state_label = '弱趋势/震荡市'
        reason = f'ADX={adx_value:.1f} < {cfg.ADX_RANGE_LOW_THRESHOLD:.0f}'
    else:
        state = 'neutral'
        state_label = '方向不明'
        reason = (f'{cfg.ADX_RANGE_LOW_THRESHOLD:.0f} ≤ ADX={adx_value:.1f}'
                  f' ≤ {cfg.ADX_STRONG_TREND_THRESHOLD:.0f}')

    return {
        'name': name, 'label': label, 'red': False,
        'value': float(adx_value), 'threshold': cfg.ADX_TREND_THRESHOLD,
        'market_state': state,
        'detail': f'{state_label}（{reason}）',
        'data_sufficient': True,
    }


def _compute_adx(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                 period: int) -> Optional[tuple]:
    """Wilder ADX 计算（纯 numpy 实现，含 +DI/-DI 方向）。

    返回 (adx, plus_di, minus_di) 三元组，均为最新值；
    任一非有限时对应项为 None；整体失败返回 None。
    adx 高只代表"趋势强"，不辨方向；方向看 plus_di vs minus_di。
    """
    n = len(high)
    if n < 2 * period + 1:
        return None

    # 1. TR、+DM、-DM（每日值，第一日为 NaN）
    prev_close = close[:-1]
    tr = np.maximum.reduce([
        high[1:] - low[1:],
        np.abs(high[1:] - prev_close),
        np.abs(low[1:] - prev_close),
    ])
    up_move = high[1:] - high[:-1]
    down_move = low[:-1] - low[1:]
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    # 2. Wilder smoothing 平滑 TR/+DM/-DM
    atr = _wilder_smooth(tr, period)
    s_plus = _wilder_smooth(plus_dm, period)
    s_minus = _wilder_smooth(minus_dm, period)

    # 3. +DI / -DI
    with np.errstate(divide='ignore', invalid='ignore'):
        plus_di = 100.0 * s_plus / atr
        minus_di = 100.0 * s_minus / atr
        plus_di = np.where(np.isfinite(plus_di), plus_di, 0.0)
        minus_di = np.where(np.isfinite(minus_di), minus_di, 0.0)

    # 4. DX
    di_sum = plus_di + minus_di
    with np.errstate(divide='ignore', invalid='ignore'):
        dx = 100.0 * np.abs(plus_di - minus_di) / di_sum
        dx = np.where(np.isfinite(dx), dx, 0.0)

    # 5. ADX = Wilder smoothing of DX
    adx_arr = _wilder_smooth(dx, period)
    if adx_arr is None or len(adx_arr) == 0:
        return None
    adx_val = float(adx_arr[-1])
    pdi_val = float(plus_di[-1])
    mdi_val = float(minus_di[-1])
    return (
        adx_val if np.isfinite(adx_val) else None,
        pdi_val if np.isfinite(pdi_val) else None,
        mdi_val if np.isfinite(mdi_val) else None,
    )


def _wilder_smooth(arr: np.ndarray, period: int) -> np.ndarray:
    """Wilder smoothing（等价于 RMA / SMMA）。

    初始值 = 前 period 项的简单平均；
    后续值 = prev - prev/period + current。
    返回与 arr 等长的数组，前 period-1 项为 NaN。
    """
    n = len(arr)
    if n < period:
        return np.full(n, np.nan)
    out = np.full(n, np.nan)
    # 首个平滑值 = 前 period 项之和
    s = float(arr[:period].sum())
    out[period - 1] = s / period
    for i in range(period, n):
        s = s - s / period + float(arr[i])
        out[i] = s / period
    return out


# ====================================================================
# 板块轮动指标：RS-Ratio / RS-Momentum / 板块 ADX
# ====================================================================
# 用户 §3.2 RRG 象限图核心指标：
#   RS-Ratio = ETF/bench 比值的 WMA(20)
#   RS-Momentum = RS-Ratio 的 10 日变化率（%）
# talib 加速：cfg.USE_TALIB=True 时用 talib.WMA / talib.ROC，
# 不可用降级 pandas rolling 实现。

def calc_rs_ratio(etf_close: pd.Series, bench_close: pd.Series,
                  cfg: AlarmConfig) -> Optional[pd.Series]:
    """RS-Ratio：**归一化** ETF/基准 比值的加权移动平均 WMA。

    归一化修正（解决 ETF 价格 ~0.8 vs 指数 ~4500 的量级差异问题）：
        1. 先对两条序列各自归一化：设首行有效值 = 1（即 rel = close / close_first_valid）
        2. 再取 ratio = ETF_rel / BENCH_rel，此时量级在 1 附近
        3. 对 ratio 做 WMA(period) 平滑 → RS-Ratio

    标准 RRG（Julius de Kempenaer 原版）即基于"归一化相对强度"，
    典型输出在 0.8~1.2 之间，=1 代表同步于基准，>1 代表跑赢基准。

    Args:
        etf_close: ETF 收盘价序列（pd.Series，index 为日期或数字）
        bench_close: 基准（沪深300）收盘价序列，与 etf_close 等长且已对齐
        cfg: AlarmConfig（取 RS_RATIO_PERIOD + USE_TALIB）

    Returns:
        pd.Series（与输入等长，前 period-1 项为 NaN），失败返回 None。
        最后一行为最新 RS-Ratio。
    """
    period = cfg.RS_RATIO_PERIOD
    if etf_close is None or bench_close is None:
        return None
    if len(etf_close) < period + 1 or len(bench_close) < period + 1:
        return None
    if len(etf_close) != len(bench_close):
        return None

    # ---- 归一化：首行有效值=1，解决价格量级差异 ----
    etf_s = pd.to_numeric(etf_close.astype(float), errors='coerce')
    bch_s = pd.to_numeric(bench_close.astype(float), errors='coerce')
    # 取第一条非 NaN 的值作为基准（若前面因 ffill 占位存在 NaN 跳过）
    first_valid_etf = etf_s.first_valid_index()
    first_valid_bch = bch_s.first_valid_index()
    if first_valid_etf is None or first_valid_bch is None:
        return None
    # 若 index 为整数位置（非日期），取位置值直接除
    try:
        base_etf = float(etf_s.loc[first_valid_etf])
        base_bch = float(bch_s.loc[first_valid_bch])
    except Exception:
        # 兜底：取 iloc[0]（对齐后的序列首行应该是有效值）
        base_etf = float(etf_s.iloc[0])
        base_bch = float(bch_s.iloc[0])
    if base_etf <= 0 or base_bch <= 0:
        return None
    etf_rel = etf_s / base_etf
    bch_rel = bch_s / base_bch

    # 归一化比值：量级 ~1（跑赢基准则>1，跑输则<1）
    ratio = (etf_rel / bch_rel).astype(float)

    # talib 加速路径
    if _talib_available(cfg):
        try:
            arr = talib.WMA(ratio.values, timeperiod=period)
            return pd.Series(arr, index=ratio.index, name='rs_ratio')
        except Exception:
            pass  # 降级

    # numpy/pandas 降级实现：WMA = Σ(weight_i * x_i) / Σ(weight_i)
    weights = np.arange(1, period + 1, dtype=float)
    out = ratio.rolling(period).apply(
        lambda x: float((x * weights).sum() / weights.sum()), raw=True
    )
    out.name = 'rs_ratio'
    return out


def calc_rs_momentum(rs_ratio: pd.Series, cfg: AlarmConfig) -> Optional[pd.Series]:
    """RS-Momentum：RS-Ratio 的 N 日变化率 (%)。

    公式：RS_Mom = (RS_Ratio / RS_Ratio.shift(period) - 1) * 100

    Args:
        rs_ratio: calc_rs_ratio 的输出
        cfg: AlarmConfig（取 RS_MOMENTUM_PERIOD）

    Returns:
        pd.Series（与输入等长，前 period 项为 NaN），失败返回 None。
    """
    if rs_ratio is None or rs_ratio.empty:
        return None
    period = cfg.RS_MOMENTUM_PERIOD
    if len(rs_ratio) < period + 1:
        return None

    # talib 加速路径：talib.ROC = (real/prev - 1) * 100，与公式一致
    if _talib_available(cfg):
        try:
            arr = talib.ROC(rs_ratio.values, timeperiod=period)
            return pd.Series(arr, index=rs_ratio.index, name='rs_momentum')
        except Exception:
            pass

    # pandas 实现
    out = (rs_ratio / rs_ratio.shift(period) - 1) * 100
    out.name = 'rs_momentum'
    return out


def calc_adx_etf(etf_df: pd.DataFrame, cfg: AlarmConfig) -> Optional[dict]:
    """ETF 自身的 ADX + 方向 DI（含末2日，用于 +DI 上穿 -DI 判断）。

    与指数 ADX 复用同一 Wilder 算法，但返回 dict 含方向信息（+DI/-DI），
    用于在评分时辨别趋势方向——ADX 高且 +DI>-DI = 强上涨；
    ADX 高且 -DI>+DI = 强下跌（不该拿高分）。

    扩展返回昨日 +DI/-DI，用于超卖反弹三级确认条件中的"+DI 上穿 -DI"判断
    （今日 +DI > -DI 且昨日 +DI <= -DI）。

    Args:
        etf_df: ETF 日线 DataFrame，需含 high/low/close 列
        cfg: AlarmConfig（取 ADX_PERIOD + USE_TALIB）

    Returns:
        dict: {adx, plus_di, minus_di, direction,
               plus_di_prev, minus_di_prev, di_cross_up}
            - adx: 趋势强度（float，0~100，最新）
            - plus_di / minus_di: 最新 +DI / -DI（float）
            - direction: 'up' if plus_di > minus_di else 'down'
            - plus_di_prev / minus_di_prev: 昨日 +DI / -DI（float | None）
            - di_cross_up: 今日 +DI > -DI 且 昨日 +DI <= -DI（bool）
        失败返回 None。
    """
    if etf_df is None or etf_df.empty:
        return None
    for col in ('high', 'low', 'close'):
        if col not in etf_df.columns:
            return None
    period = cfg.ADX_PERIOD
    need = 2 * period + 5
    if len(etf_df) < need:
        return None

    # 过滤 NaN（ETF 上市晚于基准时首行可能为 NaN，导致 ADX/DI 计算返回 0.0）
    sub = etf_df[['high', 'low', 'close']].astype(float).dropna()
    if len(sub) < need:
        return None
    high = sub['high'].values
    low = sub['low'].values
    close = sub['close'].values

    adx_val = None
    pdi_val = None
    mdi_val = None
    pdi_prev = None
    mdi_prev = None

    # talib 加速路径：分别取 ADX / PLUS_DI / MINUS_DI 全数组
    if _talib_available(cfg):
        try:
            adx_arr = talib.ADX(high, low, close, timeperiod=period)
            if adx_arr is not None and len(adx_arr) > 0:
                v = float(adx_arr[-1])
                if np.isfinite(v):
                    adx_val = v
            pdi_arr = talib.PLUS_DI(high, low, close, timeperiod=period)
            if pdi_arr is not None and len(pdi_arr) > 0:
                v = float(pdi_arr[-1])
                if np.isfinite(v):
                    pdi_val = v
                if len(pdi_arr) >= 2:
                    vp = float(pdi_arr[-2])
                    if np.isfinite(vp):
                        pdi_prev = vp
            mdi_arr = talib.MINUS_DI(high, low, close, timeperiod=period)
            if mdi_arr is not None and len(mdi_arr) > 0:
                v = float(mdi_arr[-1])
                if np.isfinite(v):
                    mdi_val = v
                if len(mdi_arr) >= 2:
                    vp = float(mdi_arr[-2])
                    if np.isfinite(vp):
                        mdi_prev = vp
        except Exception:
            pass

    # numpy 降级路径（_compute_adx 返回三元组标量，无昨日值）
    if adx_val is None:
        result = _compute_adx(high, low, close, period)
        if result is None:
            return None
        adx_val, pdi_val, mdi_val = result

    if adx_val is None:
        return None
    direction = 'up' if (pdi_val or 0) > (mdi_val or 0) else 'down'
    # +DI 上穿 -DI：今日 +DI > -DI 且 昨日 +DI <= -DI
    di_cross_up = False
    if pdi_prev is not None and mdi_prev is not None:
        di_cross_up = ((pdi_val or 0) > (mdi_val or 0)) and (pdi_prev <= mdi_prev)
    # DI 差 = +DI - -DI（正=多方占优，负=空方占优）
    di_diff = (pdi_val or 0.0) - (mdi_val or 0.0)
    # ADX 斜率 = ADX[-1] - ADX[-N]（趋势强度增强/减弱）
    adx_slope = None
    slope_period = getattr(cfg, 'TREND_HEALTH_ADX_SLOPE_PERIOD', 5)
    # talib 路径：adx_arr 已在上方计算（如果有）
    adx_arr_for_slope = None
    if _talib_available(cfg):
        try:
            adx_arr_for_slope = talib.ADX(high, low, close, timeperiod=period)
        except Exception:
            adx_arr_for_slope = None
    if adx_arr_for_slope is not None and len(adx_arr_for_slope) > slope_period:
        v_today = float(adx_arr_for_slope[-1])
        v_prev = float(adx_arr_for_slope[-1 - slope_period])
        if np.isfinite(v_today) and np.isfinite(v_prev):
            adx_slope = v_today - v_prev
    # numpy 降级：无法高效取历史 ADX 序列，adx_slope 留 None（trend_health 会降级处理）
    return {
        'adx': adx_val,
        'plus_di': pdi_val,
        'minus_di': mdi_val,
        'direction': direction,
        'plus_di_prev': pdi_prev,
        'minus_di_prev': mdi_prev,
        'di_cross_up': di_cross_up,
        'di_diff': round(di_diff, 2),
        'adx_slope': round(adx_slope, 4) if adx_slope is not None else None,
    }


def calc_turnover_ratio(etf_df: pd.DataFrame) -> Optional[float]:
    """ETF 量能相对强度代理（近5日均成交额 / 近20日均成交额 = 放量倍数）。

    腾讯源 ETF 日线无流通市值，真实换手率不可得。改用"量能相对强度"作为
    资金关注度与拥挤度的代理：近5日成交额均值 ÷ 近20日成交额均值。

    典型区间：
        < 0.5  ：极缩量（资金关注度极低）
        0.5~1.0：缩量/平淡
        1.0    ：量能与 20 日平均持平
        1.0~2.0：温和放量（资金关注度上升）
        ≥ 2.0  ：显著放量（资金高度关注）
        ≥ 2.5  ：过度拥挤（量能过大，可能短期见顶）

    Args:
        etf_df: ETF 日线，需含 amount 列，且长度≥20行（不足则返回 None）

    Returns:
        量能相对强度（float，无量纲比值），失败返回 None。
    """
    if etf_df is None or etf_df.empty or 'amount' not in etf_df.columns:
        return None
    amt = etf_df['amount'].dropna()
    if len(amt) < 20:
        return None
    try:
        ma5 = float(amt.iloc[-5:].mean())
        ma20 = float(amt.iloc[-20:].mean())
    except (TypeError, ValueError):
        return None
    if ma20 <= 0:
        return None
    return ma5 / ma20


def calc_amount_percentile(etf_df: pd.DataFrame,
                           cfg: AlarmConfig) -> Optional[float]:
    """ETF 资金关注度代理：成交额历史分位（0~100）。

    腾讯源 ETF 无基金份额数据，真实换手率不可得；而 amount/(close*volume)
    是单位换算常数（close*volume ≈ amount），无横截面区分度。改用"成交额
    历史分位"作为资金关注度代理：今日成交额在过去 N 日（默认60日）序列中
    的百分位，反映"今日量能相对自身历史的热度"。

    相比旧的 MA5/MA20 量比，历史分位的优势：
    - 不受品种自身量级影响（银行ETF成交额天然低于芯片ETF，量比无法跨品种
      比较，但分位可以：银行ETF今日分位90% = 芯片ETF今日分位90% = 各自
      相对自身历史都处于放量状态）
    - 更直观：分位80% = 过去60日里只有20%的日子比今天更放量

    典型区间：
        < 20%  ：缩量（资金关注度低）
        ~50%   ：正常
        > 80%  ：放量（资金关注度上升）
        > 95%  ：极度放量（可能短期见顶）

    Args:
        etf_df: ETF 日线，需含 amount 列
        cfg: AlarmConfig（取 SCORE_CAP_ATTENTION_LOOKBACK）

    Returns:
        成交额历史分位（float，0~100），失败返回 None。
    """
    if etf_df is None or etf_df.empty or 'amount' not in etf_df.columns:
        return None
    amt = etf_df['amount'].astype(float).dropna()
    lookback = cfg.SCORE_CAP_ATTENTION_LOOKBACK
    if len(amt) < lookback:
        # 数据不足时用全部可用数据
        if len(amt) < 10:
            return None
        lookback = len(amt)
    today = float(amt.iloc[-1])
    hist = amt.iloc[-lookback:].values
    if today <= 0 or len(hist) == 0:
        return None
    # 分位 = 历史序列中 <= 今日值的比例 × 100
    percentile = float(np.sum(hist <= today) / len(hist) * 100.0)
    return round(percentile, 1)


def calc_amount_percentile_series(etf_df: pd.DataFrame,
                                  cfg: AlarmConfig,
                                  days: int = 5) -> Optional[list]:
    """返回末 N 日成交额历史分位序列（0~100）。

    用于超卖反弹二级条件"成交额分位从<20%回升到>50%"的判断：
    对末 N 日每一日，计算其在过去 lookback 日序列中的分位。
    返回列表按时间升序，末元素为今日。

    Args:
        etf_df: ETF 日线，需含 amount 列
        cfg: AlarmConfig（取 SCORE_CAP_ATTENTION_LOOKBACK）
        days: 返回末 N 日的分位

    Returns:
        list[float]，长度=days，按时间升序，末元素为今日；
        数据不足返回 None。
    """
    if etf_df is None or etf_df.empty or 'amount' not in etf_df.columns:
        return None
    amt = etf_df['amount'].astype(float).dropna()
    lookback = cfg.SCORE_CAP_ATTENTION_LOOKBACK
    need = lookback + days
    if len(amt) < need:
        if len(amt) < 10 + days:
            return None
        lookback = len(amt) - days
        if lookback < 5:
            return None
    result = []
    for i in range(days, 0, -1):
        # 取末 i 日作为"当日"，其过去 lookback 日作为历史
        end_idx = len(amt) - i + 1
        start_idx = max(0, end_idx - lookback)
        if end_idx <= start_idx:
            result.append(0.0)
            continue
        hist = amt.iloc[start_idx:end_idx].values
        if len(hist) == 0:
            result.append(0.0)
            continue
        val = float(hist[-1])
        if val <= 0:
            result.append(0.0)
            continue
        pct = float(np.sum(hist <= val) / len(hist) * 100.0)
        result.append(round(pct, 1))
    return result


def calc_volume_ratio(etf_df: pd.DataFrame,
                      period: int = 20) -> Optional[float]:
    """成交量比 = 今日成交量 / 过去 N 日均量。

    用途：判断今日大涨/大跌是否有量能配合。
        > 1.5 ：放量（资金关注度上升）
        0.8~1.5：正常
        < 0.8 ：缩量（无量上涨/下跌可靠性低）

    Args:
        etf_df: ETF 日线（需含 volume 列）
        period: 均量周期，默认 20

    Returns:
        float: 成交量比，失败返回 None
    """
    if etf_df is None or etf_df.empty:
        return None
    if 'volume' not in etf_df.columns:
        return None
    vol = etf_df['volume'].astype(float).dropna()
    if len(vol) < period + 1:
        return None
    last_vol = float(vol.iloc[-1])
    ma_vol = float(vol.iloc[-period - 1:-1].mean())  # 不含今日的过去 N 日均量
    if ma_vol <= 0 or not np.isfinite(ma_vol):
        return None
    return round(last_vol / ma_vol, 3)


def calc_multi_period_rs(etf_df: pd.DataFrame,
                         bench_df: pd.DataFrame,
                         periods: list = None) -> Optional[dict]:
    """多周期相对强度：ETF 收益 - 基准收益，避免单周期骗人。

    对每个周期 N：
        rs_N = (close[-1]/close[-N] - 1) - (bench_close[-1]/bench_close[-N] - 1)

    用途：多周期一致（5/20/60 日都跑赢）才是真正强于基准；
    单周期（如 5 日）跑赢可能只是短期反弹。

    Args:
        etf_df: ETF 日线（需含 close 列，已对齐基准）
        bench_df: 基准日线（需含 close 列，已对齐 ETF）
        periods: 周期列表，默认 [5, 20, 60]

    Returns:
        dict: {period: rs_value}，rs_value 单位为百分比；失败返回 None
    """
    if etf_df is None or bench_df is None or etf_df.empty or bench_df.empty:
        return None
    if 'close' not in etf_df.columns or 'close' not in bench_df.columns:
        return None
    if len(etf_df) != len(bench_df):
        return None
    if periods is None:
        periods = [5, 20, 60]
    etf_close = etf_df['close'].astype(float).dropna()
    bench_close = bench_df['close'].astype(float).dropna()
    # dropna 后长度可能不一致，取交集对齐
    min_len = min(len(etf_close), len(bench_close))
    if min_len <= 0:
        return None
    etf_arr = etf_close.iloc[-min_len:].values
    bench_arr = bench_close.iloc[-min_len:].values
    result = {}
    for p in periods:
        if min_len <= p:
            result[p] = None
            continue
        etf_ret = (etf_arr[-1] / etf_arr[-1 - p] - 1) * 100.0
        bench_ret = (bench_arr[-1] / bench_arr[-1 - p] - 1) * 100.0
        if np.isfinite(etf_ret) and np.isfinite(bench_ret):
            result[p] = round(etf_ret - bench_ret, 2)
        else:
            result[p] = None
    return result


def calc_bollinger_bandwidth(etf_df: pd.DataFrame,
                             period: int = 20,
                             nbdev: float = 2.0) -> Optional[pd.Series]:
    """布林带宽度 BBW = (上轨 - 下轨) / 中轨 × 100（百分比）。

    用途：波动率压缩检测。BBW 处于历史低分位 → 变盘前夜（盘整后可能突破）。

    Args:
        etf_df: ETF 日线（需含 close 列）
        period: 布林带周期，默认 20
        nbdev: 标准差倍数，默认 2.0

    Returns:
        pd.Series: BBW 序列（百分比），前 period-1 项为 NaN；失败返回 None
    """
    if etf_df is None or etf_df.empty or 'close' not in etf_df.columns:
        return None
    close = etf_df['close'].astype(float).dropna()
    if len(close) < period:
        return None
    mid = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    upper = mid + nbdev * std
    lower = mid - nbdev * std
    bbw = (upper - lower) / mid * 100.0
    # 对齐原始索引（dropna 后索引可能不连续）
    return pd.Series(bbw.values, index=close.index, name='bbw')


def calc_obv(etf_df: pd.DataFrame) -> Optional[pd.Series]:
    """OBV（On-Balance Volume，能量潮）。

    计算：
        OBV[t] = OBV[t-1] + volume[t]  if close[t] > close[t-1]
        OBV[t] = OBV[t-1] - volume[t]  if close[t] < close[t-1]
        OBV[t] = OBV[t-1]              if close[t] == close[t-1]

    用途：量价背离检测。价格创新低但 OBV 不创新低 → 底部背离（买盘承接）；
          价格创新高但 OBV 不创新高 → 顶部背离（卖盘涌出）。

    Args:
        etf_df: ETF 日线（需含 close + volume 列）

    Returns:
        pd.Series: OBV 序列（首项为 0）；失败返回 None
    """
    if etf_df is None or etf_df.empty:
        return None
    if 'close' not in etf_df.columns or 'volume' not in etf_df.columns:
        return None
    sub = etf_df[['close', 'volume']].astype(float).dropna()
    if len(sub) < 2:
        return None
    close = sub['close'].values
    volume = sub['volume'].values
    obv = np.zeros(len(close), dtype=float)
    for i in range(1, len(close)):
        if close[i] > close[i - 1]:
            obv[i] = obv[i - 1] + volume[i]
        elif close[i] < close[i - 1]:
            obv[i] = obv[i - 1] - volume[i]
        else:
            obv[i] = obv[i - 1]
    return pd.Series(obv, index=sub.index, name='obv')


def calc_ma_slope(etf_df: pd.DataFrame,
                  ma_period: int = 20,
                  slope_period: int = 5) -> Optional[dict]:
    """计算 MA 及斜率，返回 {ma_value, ma_slope, price_above_ma}。

    Args:
        etf_df: ETF 日线（需含 close 列）
        ma_period: 均线周期
        slope_period: 斜率回看天数（MA_today - MA_N天前）

    Returns:
        dict: {
            'ma_value': float,        # 今日 MA 值
            'ma_slope': float,        # MA 斜率（MA_today - MA_N天前）
            'price_above_ma': bool,   # 今日收盘价是否站上 MA
        }；失败返回 None
    """
    if etf_df is None or etf_df.empty or 'close' not in etf_df.columns:
        return None
    close = etf_df['close'].astype(float).dropna()
    if len(close) < ma_period + slope_period:
        return None
    ma = close.rolling(ma_period).mean()
    ma_today = float(ma.iloc[-1])
    ma_prev = float(ma.iloc[-1 - slope_period])
    price_today = float(close.iloc[-1])
    if not (np.isfinite(ma_today) and np.isfinite(ma_prev)):
        return None
    return {
        'ma_value': round(ma_today, 4),
        'ma_slope': round(ma_today - ma_prev, 4),
        'price_above_ma': price_today > ma_today,
    }


def calc_vwap(etf_df: pd.DataFrame,
              period: int = 20) -> Optional[float]:
    """日线 VWAP（成交量加权平均价）。

    计算：过去 N 日 Σ(典型价格 × 成交量) / Σ(成交量)
    典型价格 = (high + low + close) / 3

    用途：价格站上 VWAP → 多头占优；跌破 VWAP → 空头占优。

    Args:
        etf_df: ETF 日线（需含 high/low/close/volume 列）
        period: 累积周期，默认 20

    Returns:
        float: VWAP 值；失败返回 None
    """
    if etf_df is None or etf_df.empty:
        return None
    for col in ('high', 'low', 'close', 'volume'):
        if col not in etf_df.columns:
            return None
    sub = etf_df[['high', 'low', 'close', 'volume']].astype(float).dropna()
    if len(sub) < period:
        return None
    window = sub.iloc[-period:]
    typical = (window['high'] + window['low'] + window['close']) / 3.0
    vol = window['volume']
    total_pv = float((typical * vol).sum())
    total_vol = float(vol.sum())
    if total_vol <= 0 or not np.isfinite(total_vol):
        return None
    return round(total_pv / total_vol, 4)


def calc_volatility_ratio(etf_df: pd.DataFrame,
                          cfg: AlarmConfig) -> Optional[float]:
    """ETF 波动率代理：ATR(14) / close × 100（百分比，B6 改进）。

    用 ATR(14)（14日平均真实波幅）除以最新收盘价，得到"日均波动幅度占价格比例"。
    相比标准差波动率，ATR 对短期异常波动（跳空、长影线）更敏感，且直观反映
    "持有一天的平均风险敞口"。

    典型区间（ETF 品种经验值）：
        < 1.5%  ：低波动（稳健趋势，加仓风险收益比好）
        1.5~3.0%：中等波动（正常）
        > 3.0%  ：高波动（风险收益比恶化，加仓需谨慎/降级）

    用于操作参考的风险修正（不进入加权总分）：
        高波动 + 领涨主线 → 不加仓（持有而非加仓）
        高波动 + 退潮预警 → 减仓加速
        低波动 + 领涨主线 → 可持有/加仓（健康趋势）

    Args:
        etf_df: ETF 日线，需含 high/low/close 列
        cfg: AlarmConfig（取 VOLATILITY_PERIOD + USE_TALIB）

    Returns:
        波动率百分比（float，单位%），失败返回 None。
    """
    if etf_df is None or etf_df.empty:
        return None
    for col in ('high', 'low', 'close'):
        if col not in etf_df.columns:
            return None
    period = cfg.VOLATILITY_PERIOD
    need = period + 2  # ATR 需要 period+1 根 K 线做平滑
    if len(etf_df) < need:
        return None

    # 过滤 NaN（ETF 上市晚于基准时首行可能为 NaN，导致 ATR 计算返回 nan）
    sub = etf_df[['high', 'low', 'close']].astype(float).dropna()
    if len(sub) < need:
        return None
    high = sub['high'].values
    low = sub['low'].values
    close = sub['close'].values
    last_close = float(close[-1])
    if last_close <= 0:
        return None

    # talib 加速路径
    if _talib_available(cfg):
        try:
            atr_arr = talib.ATR(high, low, close, timeperiod=period)
            if atr_arr is not None and len(atr_arr) > 0:
                v = float(atr_arr[-1])
                if np.isfinite(v) and v > 0:
                    return v / last_close * 100.0
        except Exception:
            pass  # 降级 numpy

    # numpy 降级：Wilder 平滑 ATR
    # TR = max(high-low, |high-prev_close|, |low-prev_close|)
    if len(close) < 2:
        return None
    prev_close = close[:-1]
    tr_high = np.maximum(high[1:] - low[1:], np.abs(high[1:] - prev_close))
    tr = np.maximum(tr_high, np.abs(low[1:] - prev_close))
    if len(tr) < period:
        return None
    # Wilder 平滑：首个ATR = 简单平均；后续 ATR = (前ATR*(period-1) + TR) / period
    atr = float(tr[:period].mean())
    for i in range(period, len(tr)):
        atr = (atr * (period - 1) + float(tr[i])) / period
    if atr <= 0 or not np.isfinite(atr):
        return None
    return atr / last_close * 100.0


def calc_atr_pct_series(etf_df: pd.DataFrame,
                         cfg: AlarmConfig) -> Optional[pd.Series]:
    """返回 ATR(14)/close × 100 的完整序列（百分比），用于超卖历史回看。

    与 calc_volatility_ratio 算法一致（Wilder ATR），但返回全序列而非末值。
    用于 _classify_oversold_rebound 的时间约束逻辑：需要逐日判断是否超卖，
    从而确定"首次触发日"和"信号持续天数"。

    Args:
        etf_df: ETF 日线（需含 high/low/close 列，已 dropna）
        cfg: AlarmConfig（取 VOLATILITY_PERIOD + USE_TALIB）

    Returns:
        pd.Series: 与 etf_df 等长的 ATR_pct 序列（百分比），
                  前 period-1 项为 NaN（平滑预热期）。失败返回 None。
    """
    if etf_df is None or etf_df.empty:
        return None
    for col in ('high', 'low', 'close'):
        if col not in etf_df.columns:
            return None
    period = cfg.VOLATILITY_PERIOD
    need = period + 2
    # 过滤 NaN（与 calc_volatility_ratio 一致）
    sub = etf_df[['high', 'low', 'close']].astype(float).dropna()
    if len(sub) < need:
        return None

    high = sub['high'].values
    low = sub['low'].values
    close = sub['close'].values

    # talib 加速路径
    if _talib_available(cfg):
        try:
            atr_arr = talib.ATR(high, low, close, timeperiod=period)
            if atr_arr is not None and len(atr_arr) > 0:
                atr_pct = np.where(
                    (close > 0) & np.isfinite(atr_arr) & (atr_arr > 0),
                    atr_arr / close * 100.0, np.nan)
                return pd.Series(atr_pct, index=sub.index)
        except Exception:
            pass  # 降级 numpy

    # numpy 降级：Wilder 平滑 ATR（全序列）
    if len(close) < 2:
        return None
    prev_close = close[:-1]
    tr_high = np.maximum(high[1:] - low[1:], np.abs(high[1:] - prev_close))
    tr = np.maximum(tr_high, np.abs(low[1:] - prev_close))
    if len(tr) < period:
        return None
    # Wilder 平滑：全序列
    atr_arr = np.full(len(close), np.nan)
    s = float(tr[:period].sum())
    atr_arr[period] = s / period  # 第 period+1 个位置（0-indexed）
    for i in range(period, len(tr)):
        s = s - s / period + float(tr[i])
        atr_arr[i + 1] = s / period  # i+1 因为 tr 比 close 少 1
    atr_pct = np.where(
        (close > 0) & np.isfinite(atr_arr) & (atr_arr > 0),
        atr_arr / close * 100.0, np.nan)
    return pd.Series(atr_pct, index=sub.index)


# ====================================================================
# 持有层（长周期指标：ADX(56) / RS(60) / MA60/MA120 / ATR 止损）
# ====================================================================

def calc_long_adx(etf_df: pd.DataFrame,
                  period: int = 56,
                  slope_period: int = 5,
                  cfg: Optional[AlarmConfig] = None) -> Optional[dict]:
    """长周期 ADX（默认 56），用于持有层趋势延续判断。

    与 calc_adx_etf 算法一致（Wilder ADX + PLUS_DI/MINUS_DI），但允许自定义周期，
    返回精简 dict（持有层只需 ADX 数值、斜率、是否向上）。

    Args:
        etf_df: ETF 日线，需含 high/low/close
        period: ADX 周期（持有层默认 56）
        slope_period: ADX 斜率回看天数
        cfg: AlarmConfig（取 USE_TALIB；可缺省走 numpy 降级）

    Returns:
        dict: {adx, adx_slope, rising, plus_di, minus_di}
            - adx: 长周期 ADX 最新值（float）
            - adx_slope: ADX_today - ADX_N天前（正=增强，负=减弱）
            - rising: adx > 0 且 adx_slope > 0（趋势在增强）
            - plus_di / minus_di: 长周期 +DI / -DI（参考用，不做方向判断）
        失败返回 None。
    """
    if etf_df is None or etf_df.empty:
        return None
    for col in ('high', 'low', 'close'):
        if col not in etf_df.columns:
            return None
    need = 2 * period + 5
    sub = etf_df[['high', 'low', 'close']].astype(float).dropna()
    if len(sub) < need:
        return None
    high = sub['high'].values
    low = sub['low'].values
    close = sub['close'].values

    adx_val = None
    pdi_val = None
    mdi_val = None
    adx_arr_full = None

    if cfg is not None and _talib_available(cfg):
        try:
            adx_arr_full = talib.ADX(high, low, close, timeperiod=period)
            if adx_arr_full is not None and len(adx_arr_full) > 0:
                v = float(adx_arr_full[-1])
                if np.isfinite(v):
                    adx_val = v
            pdi_arr = talib.PLUS_DI(high, low, close, timeperiod=period)
            if pdi_arr is not None and len(pdi_arr) > 0:
                v = float(pdi_arr[-1])
                if np.isfinite(v):
                    pdi_val = v
            mdi_arr = talib.MINUS_DI(high, low, close, timeperiod=period)
            if mdi_arr is not None and len(mdi_arr) > 0:
                v = float(mdi_arr[-1])
                if np.isfinite(v):
                    mdi_val = v
        except Exception:
            adx_arr_full = None

    # numpy 降级：_compute_adx 返回三元组标量（无历史序列）
    if adx_val is None:
        result = _compute_adx(high, low, close, period)
        if result is None:
            return None
        adx_val, pdi_val, mdi_val = result

    if adx_val is None:
        return None

    # ADX 斜率：talib 路径有完整序列；numpy 路径无序列 → 留 None
    adx_slope = None
    if adx_arr_full is not None and len(adx_arr_full) > slope_period:
        v_today = float(adx_arr_full[-1])
        v_prev = float(adx_arr_full[-1 - slope_period])
        if np.isfinite(v_today) and np.isfinite(v_prev):
            adx_slope = v_today - v_prev

    rising = (adx_val > 0) and (adx_slope is not None and adx_slope > 0)

    return {
        'adx': round(float(adx_val), 2),
        'adx_slope': round(float(adx_slope), 4) if adx_slope is not None else None,
        'rising': bool(rising),
        'plus_di': round(float(pdi_val), 2) if pdi_val is not None else None,
        'minus_di': round(float(mdi_val), 2) if mdi_val is not None else None,
    }


def calc_long_rs_ratio(etf_close: pd.Series,
                       bench_close: pd.Series,
                       period: int = 60,
                       cfg: Optional[AlarmConfig] = None) -> Optional[float]:
    """长周期 RS-Ratio（默认 60 日 WMA 平滑），用于持有层判断长周期跑赢基准。

    与 calc_rs_ratio 算法一致（归一化 + WMA），但允许自定义周期，返回末值标量。

    Args:
        etf_close: ETF 收盘价序列
        bench_close: 基准收盘价序列（等长对齐）
        period: WMA 平滑周期（持有层默认 60）
        cfg: AlarmConfig（取 USE_TALIB；可缺省走 pandas 降级）

    Returns:
        float: 长周期 RS-Ratio 末值；失败返回 None。
    """
    if etf_close is None or bench_close is None:
        return None
    if len(etf_close) < period + 1 or len(bench_close) < period + 1:
        return None
    if len(etf_close) != len(bench_close):
        return None

    etf_s = pd.to_numeric(etf_close.astype(float), errors='coerce')
    bch_s = pd.to_numeric(bench_close.astype(float), errors='coerce')
    first_valid_etf = etf_s.first_valid_index()
    first_valid_bch = bch_s.first_valid_index()
    if first_valid_etf is None or first_valid_bch is None:
        return None
    try:
        base_etf = float(etf_s.loc[first_valid_etf])
        base_bch = float(bch_s.loc[first_valid_bch])
    except Exception:
        base_etf = float(etf_s.iloc[0])
        base_bch = float(bch_s.iloc[0])
    if base_etf <= 0 or base_bch <= 0:
        return None
    etf_rel = etf_s / base_etf
    bch_rel = bch_s / base_bch
    ratio = (etf_rel / bch_rel).astype(float)

    last_val = None
    if cfg is not None and _talib_available(cfg):
        try:
            arr = talib.WMA(ratio.values, timeperiod=period)
            if arr is not None and len(arr) > 0:
                v = float(arr[-1])
                if np.isfinite(v):
                    last_val = v
        except Exception:
            pass

    if last_val is None:
        weights = np.arange(1, period + 1, dtype=float)
        out = ratio.rolling(period).apply(
            lambda x: float((x * weights).sum() / weights.sum()), raw=True
        )
        v = float(out.iloc[-1])
        if not np.isfinite(v):
            return None
        last_val = v

    return round(last_val, 4)


def calc_atr_stop(etf_df: pd.DataFrame,
                   period: int = 14,
                   mult: float = 2.0,
                   ma_period: int = 20,
                   cfg: Optional[AlarmConfig] = None) -> Optional[dict]:
    """ATR 移动止损判断（持有层离场信号）。

    公式：stop_line = MA(ma_period) - mult × ATR(period)
          atr_stop_broken = close < stop_line

    用于持有层"跌破 ATR 止损 → 离场"规则，优先级最高（高于 hold_score 阈值）。

    Args:
        etf_df: ETF 日线，需含 high/low/close
        period: ATR 周期（默认 14）
        mult: ATR 倍数（默认 2.0）
        ma_period: MA 周期（默认 20）
        cfg: AlarmConfig（取 USE_TALIB；可缺省走 numpy 降级）

    Returns:
        dict: {
            'stop_line': float,           # 止损线值
            'ma_value': float,             # MA 值
            'atr_value': float,            # ATR 值
            'close_today': float,          # 今日收盘
            'atr_stop_broken': bool,       # 是否跌破止损线
        }；失败返回 None
    """
    if etf_df is None or etf_df.empty:
        return None
    for col in ('high', 'low', 'close'):
        if col not in etf_df.columns:
            return None
    sub = etf_df[['high', 'low', 'close']].astype(float).dropna()
    need = max(period + 2, ma_period)
    if len(sub) < need:
        return None
    high = sub['high'].values
    low = sub['low'].values
    close = sub['close'].values
    last_close = float(close[-1])
    if last_close <= 0:
        return None

    # ATR
    atr_val = None
    if cfg is not None and _talib_available(cfg):
        try:
            atr_arr = talib.ATR(high, low, close, timeperiod=period)
            if atr_arr is not None and len(atr_arr) > 0:
                v = float(atr_arr[-1])
                if np.isfinite(v) and v > 0:
                    atr_val = v
        except Exception:
            pass
    if atr_val is None:
        # numpy Wilder ATR 末值
        if len(close) < 2:
            return None
        prev_close = close[:-1]
        tr_high = np.maximum(high[1:] - low[1:], np.abs(high[1:] - prev_close))
        tr = np.maximum(tr_high, np.abs(low[1:] - prev_close))
        if len(tr) < period:
            return None
        atr = float(tr[:period].mean())
        for i in range(period, len(tr)):
            atr = (atr * (period - 1) + float(tr[i])) / period
        if atr <= 0 or not np.isfinite(atr):
            return None
        atr_val = atr

    # MA
    if len(close) < ma_period:
        return None
    ma_val = float(np.mean(close[-ma_period:]))
    stop_line = ma_val - mult * atr_val
    return {
        'stop_line': round(stop_line, 4),
        'ma_value': round(ma_val, 4),
        'atr_value': round(atr_val, 4),
        'close_today': round(last_close, 4),
        'atr_stop_broken': bool(last_close < stop_line),
    }

