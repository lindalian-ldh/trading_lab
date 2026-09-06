#!/usr/bin/env python3
"""A股多维度量化预警与轮动分析系统（个人版）入口。

整合四大核心功能：
    模块一：7 大原子风险预警（S1-S7）
    模块二：ADX 市场状态过滤器（动态 red_count 阈值）
    模块三：板块轮动分析（RRG 四象限 + 5 维评分卡）
    模块四：连板龙头 & 大盘温度（最高板/梯队/晋级率/温度评分）

默认全流程：拉取数据 → 信号计算 → 轮动分析 → 连板分析 →
生成 Markdown 全景报告 → 控制台打印 → 写 CSV + Markdown 落盘。

用法示例：
    # 默认：预警 + ADX + 轮动 + 连板 全流程
    uv run services/alarming_monitor/main.py

    # 仅跑连板龙头（大盘温度） 模块
    uv run services/alarming_monitor/main.py --linkban-only

    # 跳过连板模块（保持老行为）
    uv run services/alarming_monitor/main.py --no-linkban

    # 仅跑板块轮动（跳过预警 + 跳过连板）
    uv run services/alarming_monitor/main.py --rotation-only

    # 跳过板块轮动（仅预警摘要 + 连板）
    uv run services/alarming_monitor/main.py --no-rotation

    # 指定基准日（默认昨日）
    uv run services/alarming_monitor/main.py --date 2026-08-19

    # 保守配置档（更严阈值，≥3 红即降仓）
    uv run services/alarming_monitor/main.py --profile conservative

    # 只跑部分预警指标
    uv run services/alarming_monitor/main.py --indicator turnover,dividend,growth

    # 禁用 ADX 过滤器（退化为静态阈值）
    uv run services/alarming_monitor/main.py --no-adx

    # 不写 CSV/Markdown 文件
    uv run services/alarming_monitor/main.py --no-csv --no-report

    # 查看近 3 个月预警历史
    uv run services/alarming_monitor/main.py --history

    # 静默（仅写文件，无控制台 Markdown）
    uv run services/alarming_monitor/main.py --quiet
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from config import get_alarm_config, list_profiles, AlarmConfig
from data_loader import (
    fetch_breadth_today, fetch_etf_daily, fetch_index_daily, fetch_market_amount,
    fetch_uplimit_stocks, fetch_uplimit_reason,
    fetch_margin_summary,
)
from indicators import (
    check_ad_ratio, check_adx, check_dividend_strength, check_growth_breakdown,
    check_turnover_ratio, check_volume_price_divergence,
    check_margin_leverage, check_kdj_divergence,
)
from linkban import analyze_linkban
from monitor import evaluate_signals
from reporter import (
    generate_markdown_report, format_linkban_report, print_summary,
    save_markdown_report,
)
from storage import (
    append_breadth_today, log_alarm_signal, log_linkban_signal,
    print_history, read_breadth_history,
    append_margin_row, read_margin_history,
)

logger = logging.getLogger(__name__)


# 指标名 → 信号 key（用于 --indicator 过滤）
_INDICATOR_KEYS = {
    'turnover': 's1', 'volume': 's2', 'ad': 's3',
    'dividend': 's4', 'growth': 's5',
    'margin': 's6',
    'kdj': 's7',
}


def _parse_args(argv):
    p = argparse.ArgumentParser(
        description="A股多维度量化预警与轮动分析系统（个人版）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""\
示例:
  默认 (全流程):  %(prog)s
  仅连板:        %(prog)s --linkban-only
  仅轮动:        %(prog)s --rotation-only
  仅预警:        %(prog)s --no-rotation --no-linkban
  指定日期:       %(prog)s --date 2026-08-19
  保守配置:       %(prog)s --profile conservative
  查看历史:       %(prog)s --history

可用配置档:
{list_profiles()}
""",
    )
    p.add_argument('--date', default=None,
                   help='参考日期 YYYY-MM-DD，默认**数据驱动**：指数收盘数据已更则今日，否则回退昨日')
    p.add_argument('--profile', default='default',
                   help='配置档: default / conservative / strict')
    p.add_argument('--indicator', default='all',
                   help='只跑部分预警信号，逗号分隔: turnover,volume,ad,dividend,growth,margin,kdj')
    # 模块开关（互斥：仅跑某单一模块）
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--rotation-only', action='store_true',
                      help='仅跑板块轮动（跳过预警 + 跳过连板）')
    mode.add_argument('--linkban-only', action='store_true',
                      help='仅跑连板龙头 & 大盘温度（跳过预警 + 跳过轮动）')
    # 各模块独立关闭开关
    p.add_argument('--no-rotation', action='store_true',
                   help='跳过板块轮动模块')
    p.add_argument('--no-linkban', action='store_true',
                   help='跳过连板龙头 & 大盘温度模块')
    p.add_argument('--no-margin', action='store_true',
                   help='跳过 S6 两融杠杆异常信号（仍跑其他预警信号）')
    p.add_argument('--no-kdj', action='store_true',
                   help='跳过 S7 大盘KDJ高位死叉信号（仍跑其他预警信号）')
    # 输出开关
    p.add_argument('--no-csv', action='store_true', help='不写预警/连板 CSV 历史记录')
    p.add_argument('--no-report', action='store_true', help='不写 Markdown 报告文件')
    p.add_argument('--no-adx', action='store_true',
                   help='禁用 ADX 趋势过滤器（退化为静态阈值 4 红）')
    p.add_argument('--history', action='store_true', help='查看近 N 个月预警历史记录')
    p.add_argument('--history-months', type=int, default=3,
                   help='--history 时显示近 N 个月（默认 3）')
    p.add_argument('--quiet', action='store_true', help='静默（仅写文件，不打印 Markdown）')
    p.add_argument('--verbose', '-v', action='store_true', help='详细日志（INFO 级别）')
    return p.parse_args(argv)


# ====================================================================
# 默认参考日自动决策（方案A：数据驱动，零猜测）
# ====================================================================

def _resolve_default_ref_date(cfg: AlarmConfig) -> str:
    """方案A：数据驱动默认日期。

    规则：
        1. 先尝试拉取 BREADTH_INDEX（沪深300，兜底上证综指）近 3 天日线
        2. 若最新一行 date == 今日 → 指数收盘数据已更新 → 返回今日
        3. 否则（数据未更新 / 拉取失败 / 最新行是昨日 / 非交易日）→ 返回昨日
        4. 任何异常（网络/解析失败）→ 兜底昨日，永不抛错

    Returns:
        YYYY-MM-DD 字符串（选今日或昨日）
    """
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    try:
        # 仅拉 3 行，缓存命中时 < 0.01s，不影响性能
        idx_df = fetch_index_daily(cfg.BREADTH_INDEX, 3)
        if idx_df is None:
            idx_df = fetch_index_daily(cfg.BREADTH_INDEX_FALLBACK, 3)
        if idx_df is None or idx_df.empty:
            logger.debug("默认参考日：指数不可取，回退昨日 %s", yesterday)
            return yesterday
        latest_date = str(idx_df['date'].iloc[-1])
        if latest_date == today:
            logger.debug("默认参考日：指数已更新至今日 %s", today)
            return today
        logger.debug("默认参考日：指数最新=%s（未到今日），回退昨日 %s", latest_date, yesterday)
        return yesterday
    except Exception as e:
        logger.warning("默认参考日解析异常，回退昨日: %s", e)
        return yesterday


def _gather_data(cfg: AlarmConfig, ref_date: str) -> dict:
    """获取全部所需数据。返回 dict，键：spot/index/etf_prices/breadth。

    数据源（实测后保留稳定源）：
        - spot: 合成单行 df，amount=两市总成交额(上证+深证 amount)，total_value=配置常量
        - index: 沪深300 日线（兜底上证综指），腾讯源回退链
        - etf_prices: ETF 篮子日线，腾讯源回退链
        - breadth: legu 今日涨跌家数 + storage CSV 自累积历史拼接
    """
    data = {}

    # 1. S1 成交额：新浪指数 spot 的两市成交额（上证综指+深证成指）→ 合成 spot_df
    market_amount = fetch_market_amount(cfg.TURNOVER_INDEX_SH,
                                        cfg.TURNOVER_INDEX_SZ)
    if market_amount is not None and market_amount > 0:
        # 合成单行 spot：amount=两市总成交额(元)，不含 total_value 列
        # → indicators 将用 cfg.TOTAL_MARKET_CAP 配置常量兜底分母
        data['spot'] = pd.DataFrame([{'amount': market_amount}])
    else:
        data['spot'] = None

    # 2. 指数日线（S2 放量不涨 + S4 逆势 + ADX 趋势过滤 + S7 周/月 KDJ）
    #    ADX 需 2*ADX_PERIOD+5 ≈ 33 行；S7 周KDJ需52周、月KDJ需12月 → ~260 日线
    idx_days = max(cfg.VOLUME_LOOKBACK_DAYS + 5,
                   2 * cfg.ADX_PERIOD + 10, cfg.LOOKBACK_DAYS + 5,
                   int(getattr(cfg, 'KDJ_LOOKBACK_DAYS', 260)))
    idx_df = fetch_index_daily(cfg.BREADTH_INDEX, idx_days)
    if idx_df is None:
        logger.warning("主指数 %s 不可取，兜底 %s",
                        cfg.BREADTH_INDEX, cfg.BREADTH_INDEX_FALLBACK)
        idx_df = fetch_index_daily(cfg.BREADTH_INDEX_FALLBACK, idx_days)
    data['index'] = idx_df

    # 2b. S7 KDJ 独立基准指数（与 BREADTH_INDEX 不同时单独拉取）
    #     相同则直接复用 data['index']，避免重复触网
    kdj_idx_symbol = str(getattr(cfg, 'KDJ_INDEX', cfg.BREADTH_INDEX) or cfg.BREADTH_INDEX)
    kdj_idx_symbol_fb = str(getattr(cfg, 'KDJ_INDEX_FALLBACK', cfg.BREADTH_INDEX_FALLBACK) or cfg.BREADTH_INDEX_FALLBACK)
    same_as_breadth = (kdj_idx_symbol.upper() == str(cfg.BREADTH_INDEX).upper())
    if same_as_breadth:
        data['kdj_index'] = idx_df  # 指针共享，零成本
    else:
        logger.debug("KDJ 指数 %s 与主指数 %s 不同，单独拉取", kdj_idx_symbol, cfg.BREADTH_INDEX)
        kdj_days = int(getattr(cfg, 'KDJ_LOOKBACK_DAYS', 260))
        kdj_idx_df = fetch_index_daily(kdj_idx_symbol, kdj_days)
        if kdj_idx_df is None:
            logger.warning("KDJ 专用指数 %s 不可取，兜底 %s", kdj_idx_symbol, kdj_idx_symbol_fb)
            kdj_idx_df = fetch_index_daily(kdj_idx_symbol_fb, kdj_days)
        if kdj_idx_df is None:
            logger.warning("KDJ 专用指数及兜底均不可取，回退复用 BREADTH_INDEX 日线")
            kdj_idx_df = idx_df   # 最后兜底：用主指数（哪怕和 KDJ 指数不同，也比空好）
        data['kdj_index'] = kdj_idx_df

    # 3. ETF 篮子日线（S4/S5）
    etf_prices = {}
    for sym in set(cfg.DIVIDEND_BASKET + cfg.GROWTH_BASKET):
        etf_prices[sym] = fetch_etf_daily(sym, cfg.LOOKBACK_DAYS + 5)
    data['etf_prices'] = etf_prices

    # 4. S3 涨跌家数：今日 legu + 历史 CSV 自累积拼接
    breadth_today = fetch_breadth_today()
    if breadth_today is not None and not breadth_today.empty:
        # try/except：legu 解析异常（字段缺失/类型脏）→ 当日数据跳过，S3 仅用历史 CSV 序列（降级灰灯）
        try:
            row = breadth_today.iloc[0]
            today_date = str(row.get('date', ref_date))
            up = int(row.get('up_count', 0))
            down = int(row.get('down_count', 0))
            flat = int(row.get('flat_count', 0))
            ratio = float(row.get('ad_ratio', up / down if down > 0 else float('inf')))
            # 当日追加到 breadth CSV（同日去重，保留最新）
            try:
                append_breadth_today(today_date, up, down, flat, ratio)
            except Exception as e:
                logger.warning("写入 breadth CSV 失败: %s", e)
        except Exception as e:
            logger.warning("legu 涨跌家数解析异常，跳过今日数据（S3 将基于历史 CSV 降级）: %s", e)

    # 读回近 N 日序列（含刚追加的今日）
    data['breadth'] = read_breadth_history(cfg.LOOKBACK_DAYS + 5)

    # 5. S6 两融：近 25+ 日自累积 CSV（今日 + SSE 历史 + SZSE 当日补加）
    #    cfg.MARGIN_USE_HISTORY_CSV=True 时：先读 CSV，仅缺时补拉区间；
    #    S6 默认在预警模块开时都取（--no-margin 只是 enabled 中排除 S6）。
    csv_days = int(cfg.MARGIN_LOOKBACK_DAYS) + 5
    try:
        prefer_csv = read_margin_history(days=csv_days, cfg=cfg) if cfg.MARGIN_USE_HISTORY_CSV else None
    except Exception as e:
        logger.warning("读两融历史 CSV 失败，退化为纯 akshare 拉取: %s", e)
        prefer_csv = None

    margin_df: Optional[pd.DataFrame] = None
    try:
        margin_df = fetch_margin_summary(ref_date,
                                         lookback_days=int(cfg.MARGIN_LOOKBACK_DAYS),
                                         prefer_csv_df=prefer_csv)
    except Exception as e:
        logger.warning("两融汇总取数失败: %s", e)
        margin_df = None

    # 写入 CSV 最新一行（同日去重）：让下次能直接复用 CSV
    if margin_df is not None and not margin_df.empty:
        try:
            last_row = margin_df.iloc[-1]
            def _nan(x):
                import numpy as _np
                try:
                    v = float(x)
                except Exception:
                    return _np.nan
                if v != v:
                    return _np.nan
                return v
            append_margin_row(
                date_str=str(last_row.get('date', ref_date)),
                rzye_yuan=_nan(last_row.get('rzye', float('nan'))),
                rzmre_yuan=_nan(last_row.get('rzmre', float('nan'))),
                rqye_yuan=_nan(last_row.get('rqye', float('nan'))),
                rzrqye_yuan=_nan(last_row.get('rzrqye', float('nan'))),
                rqmcl=_nan(last_row.get('rqmcl', float('nan'))),
                rqyl=_nan(last_row.get('rqyl', float('nan'))),
                cfg=cfg,
            )
        except Exception as e:
            logger.warning("写入两融 CSV 失败: %s", e)

    data['margin'] = margin_df
    # S1 两市总成交额（便于 S6 复用：S1 合成 spot 里只有 amount 没 ref_date）
    data['market_amount_today'] = market_amount

    return data


def _build_ad_ratio_series(breadth: Optional[pd.DataFrame]) -> 'list':
    """构造 S3 所需的日涨跌家数比序列（升序，末值为今日）。

    数据源：storage.read_breadth_history 返回的 CSV 自累积序列（含今日 legu）。
    序列不足 5 日时，S3 将判定数据不足（仅今日无法判定"曾高后回落"模式）。
    """
    if breadth is not None and not breadth.empty and 'ad_ratio' in breadth.columns:
        return list(breadth['ad_ratio'].astype(float))
    return []


def _compute_signals(data: dict, cfg: AlarmConfig,
                     enabled: set, use_adx: bool = True) -> tuple:
    """计算启用的信号，返回 (7 信号 dict 列表, ADX 信号 dict 或 None)。

    ADX 信号基于主指数 high/low/close 计算，不计入 5 红灯，
    作为过滤器由 monitor.evaluate_signals 动态调整 red_count 阈值。
    use_adx=False 或数据不足时返回 None（退化为静态阈值）。
    """
    results = []

    # S1
    if 's1' in enabled:
        results.append(check_turnover_ratio(data.get('spot'), cfg))
    # S2
    if 's2' in enabled:
        results.append(check_volume_price_divergence(data.get('index'), cfg))
    # S3
    if 's3' in enabled:
        ad_series = _build_ad_ratio_series(data.get('breadth'))
        results.append(check_ad_ratio(ad_series, cfg))
    # S4
    if 's4' in enabled:
        results.append(check_dividend_strength(data.get('index'),
                                                data.get('etf_prices'), cfg))
    # S5
    if 's5' in enabled:
        results.append(check_growth_breakdown(data.get('etf_prices'), cfg))
    # S6
    if 's6' in enabled:
        # 总市值：S1 用的 TOTAL_MARKET_CAP（无 spot 时的分母），这里也一致
        market_cap = float(getattr(cfg, 'TOTAL_MARKET_CAP', 0) or 0)
        results.append(check_margin_leverage(
            data.get('margin'),
            data.get('market_amount_today'),
            market_cap,
            cfg,
        ))
    # S7（优先用 KDJ_INDEX 专用日线 data['kdj_index']，缺失时兜底回退 data['index'] 共享主指数）
    if 's7' in enabled:
        results.append(check_kdj_divergence(
            data.get('kdj_index') if data.get('kdj_index') is not None else data.get('index'),
            cfg,
        ))

    # ADX 趋势过滤器（基于主指数 high/low/close）
    adx_result = None
    if use_adx:
        adx_result = check_adx(data.get('index'), cfg)

    return results, adx_result


# ====================================================================
# 板块轮动专用：长历史数据获取（RRG/ADX 需要更长序列）
# ====================================================================

def _gather_rotation_data(cfg: AlarmConfig) -> dict:
    """获取板块轮动所需长历史数据。

    返回 {
        'bench_df': 基准指数日线（足够长：max(RS+动量窗口, 2*ADX+余量)），
        'etf_full_dfs': {symbol: etf_df}，ROTATION_BASKET 全量 ETF 长历史，
    }

    任何缺失返回 None 对应键；后续 analyze_single_etf 会判定数据不足。
    """
    # RS-Ratio(20) + RS-Momentum(10) → 至少 30 行；再加 ffill 余量取 60+
    # ETF ADX(14) → 需 2*14+5=33 行；
    # 取 120 交易日（~ 半年）足够，又不会拉太长拖慢首跑。
    rot_days = max(
        cfg.RS_RATIO_PERIOD + cfg.RS_MOMENTUM_PERIOD + 30,
        2 * cfg.ADX_PERIOD + 30,
        120,
    )

    # 基准指数（沪深300，兜底上证综指）
    bench_df = fetch_index_daily(cfg.ROTATION_BENCHMARK, rot_days)
    if bench_df is None:
        logger.warning("轮动基准 %s 不可取，尝试 BREADTH_INDEX", cfg.ROTATION_BENCHMARK)
        bench_df = fetch_index_daily(cfg.BREADTH_INDEX, rot_days)
        if bench_df is None:
            logger.warning("轮动基准 BREADTH_INDEX 也不可取，尝试兜底")
            bench_df = fetch_index_daily(cfg.BREADTH_INDEX_FALLBACK, rot_days)

    # ROTATION_BASKET 全部 ETF 拉取
    etf_dfs = {}
    for sym in cfg.ROTATION_BASKET:
        etf_dfs[sym] = fetch_etf_daily(sym, rot_days)

    return {'bench_df': bench_df, 'etf_full_dfs': etf_dfs}


# ====================================================================
# 连板模块专用：获取今日/昨日涨停数据
# ====================================================================

def _date_to_ymd(date_str: str) -> str:
    """'YYYY-MM-DD' -> 'YYYYMMDD'。"""
    return str(date_str).replace('-', '')


def _prev_trading_day_ymd(ref_date: str) -> str:
    """简单回退：ref_date 前一日。不精确但够用于晋级率（节假日/周末会返回空 df）。

    实际 fetch_uplimit_stocks(非交易日) 返回 None，晋级率分母自动为 0 即可。
    """
    from datetime import datetime, timedelta
    try:
        d = datetime.strptime(ref_date[:10], "%Y-%m-%d")
    except Exception:
        d = datetime.now()
    prev = d - timedelta(days=1)
    return prev.strftime("%Y%m%d")


def _gather_linkban_data(cfg: AlarmConfig, ref_date: str) -> dict:
    """获取连板模块所需：{today_df, yesterday_df}。任何字段可为 None。"""
    today_ymd = _date_to_ymd(ref_date)
    yesterday_ymd = _prev_trading_day_ymd(ref_date)
    try:
        today_df = fetch_uplimit_stocks(today_ymd)
    except Exception as e:
        logger.warning("拉取今日涨停失败 (%s): %s", today_ymd, e)
        today_df = None
    try:
        yesterday_df = fetch_uplimit_stocks(yesterday_ymd)
    except Exception as e:
        logger.warning("拉取昨日涨停失败 (%s): %s", yesterday_ymd, e)
        yesterday_df = None
    return {'today_df': today_df, 'yesterday_df': yesterday_df}


# ====================================================================
# 主流程：整合四模块
# ====================================================================

def _run_once(args, cfg: AlarmConfig) -> int:
    """单次全流程运行。返回退出码。

    模块开关优先级：
      --linkban-only   → 仅模块四
      --rotation-only  → 仅模块三
      否则：
          预警（S1-S7+ADX）= 默认开
          轮动            = not --no-rotation
          连板            = not --no-linkban

    步骤：
      1. 预警模块（可选）
      2. 板块轮动模块（可选）
      3. 连板龙头 & 大盘温度（可选）
      4. 生成 Markdown 全景报告（按启用模块拼接）
      5. 控制台打印（notifier / ASCII 报告 / print_summary）
      6. 落盘：预警 CSV + 连板 CSV + Markdown 报告（各可选）
    """
    t0 = time.time()

    # 模块开关判定
    if getattr(args, 'linkban_only', False):
        run_alarms = False
        run_rotation = False
        run_linkban = True
    elif getattr(args, 'rotation_only', False):
        run_alarms = False
        run_rotation = True
        run_linkban = False
    else:
        run_alarms = True
        run_rotation = not getattr(args, 'no_rotation', False)
        run_linkban = not getattr(args, 'no_linkban', False)

    # --------- 数据层 ---------
    data = _gather_data(cfg, args.date) if run_alarms else None
    rot_data = _gather_rotation_data(cfg) if run_rotation else None
    lb_data = _gather_linkban_data(cfg, args.date) if run_linkban else None

    # --------- 模块一：预警（S1-S7 + ADX）---------
    alarm_result = None
    all_insufficient = False
    if run_alarms:
        enabled = set(_INDICATOR_KEYS.values()) if args.indicator == 'all' \
            else {_INDICATOR_KEYS[k.strip()] for k in args.indicator.split(',')
                  if k.strip() in _INDICATOR_KEYS}
        # --no-margin：不跑 S6 两融（即便 indicator=all 或显式 margin）
        if getattr(args, 'no_margin', False):
            enabled.discard('s6')
        # --no-kdj：不跑 S7 KDJ（即便 indicator=all 或显式 kdj）
        if getattr(args, 'no_kdj', False):
            enabled.discard('s7')
        signals, adx_result = _compute_signals(data, cfg, enabled,
                                               use_adx=not args.no_adx)

        all_insufficient = bool(signals and all(
            not s.get('data_sufficient', False) for s in signals))
        alarm_result = evaluate_signals(signals, cfg, args.date, adx_result)

        # 非静默 + 无其他模块 → 打印轻量预警摘要
        if not args.quiet and not run_rotation and not run_linkban:
            print_summary(alarm_result)

    # --------- 模块三：板块轮动（RRG + 5 维评分）---------
    rotation_result = None
    if run_rotation:
        from analysis import run_rotation_analysis
        if rot_data and rot_data.get('bench_df') is not None:
            rotation_result = run_rotation_analysis(
                rot_data['bench_df'],
                rot_data.get('etf_full_dfs', {}),
                cfg,
            )
        else:
            logger.warning("轮动模块数据不足（基准指数不可取），跳过")
            rotation_result = {
                'results': [], 'rank_delta_map': {}, 'last_rank': None,
                'summary': {'total_basket': len(cfg.ROTATION_BASKET), 'data_ok': 0,
                            'leader_count': 0, 'laggard_count': 0},
                'analyzed_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            }

    # --------- 模块四：连板龙头 & 大盘温度 ---------
    linkban_result = None
    if run_linkban:
        lb_today = (lb_data or {}).get('today_df')
        lb_yesterday = (lb_data or {}).get('yesterday_df')
        linkban_result = analyze_linkban(
            lb_today, lb_yesterday, args.date, cfg,
            reason_loader_fn=fetch_uplimit_reason,
        )

        # 非静默 + 仅连板模式 → 打印 ASCII 报告
        if not args.quiet and not run_alarms and not run_rotation:
            report_ascii = format_linkban_report(linkban_result)
            from notifier import get_notifier
            n = get_notifier('console')
            n.notify(report_ascii, title=f'{args.date} 连板龙头 & 大盘温度')

    # --------- 退出码判定：所有启用模块都数据不足才抛 2 ---------
    any_data_ok = False
    if run_alarms and not all_insufficient:
        any_data_ok = True
    if run_rotation and rotation_result is not None and bool(
            (rotation_result.get('summary') or {}).get('data_ok', 0) > 0
            or rotation_result.get('results')):
        any_data_ok = True
    if run_linkban and linkban_result is not None and linkban_result.get('data_sufficient'):
        any_data_ok = True

    any_module_enabled = run_alarms or run_rotation or run_linkban
    if any_module_enabled and not any_data_ok:
        print('❌ 所有启用模块数据获取失败，无法生成有效报告')
        if not args.quiet:
            if alarm_result:
                for s in alarm_result.get('signals', []) or []:
                    print(f'  {s.get("label")}: {s.get("detail")}')
            if linkban_result and not linkban_result.get('data_sufficient'):
                print(f'  连板模块: {linkban_result.get("reason") or "无涨停数据（非交易日/接口不可用）"}')
        return 2

    # --------- Markdown 全景报告（按启用模块拼接）---------
    md_text = None
    # 连板+其他模式：扩展 generate_markdown_report 加入 linkban_result
    if run_alarms and run_rotation and alarm_result and rotation_result:
        md_text = generate_markdown_report(alarm_result, rotation_result,
                                           linkban_result=linkban_result)
    elif run_rotation and rotation_result:
        dummy_alarm = {
            'ref_date': args.date,
            'risk_level': 'normal',
            'red_count': 0,
            'signals': [],
            'adx': {'value': None, 'detail': ''},
            'market_state': 'neutral',
            'position_advice': '—',
            'advice_desc': '仅轮动/连板模式（跳过预警模块）',
        }
        md_text = generate_markdown_report(dummy_alarm, rotation_result,
                                           linkban_result=linkban_result)
    elif run_alarms and run_linkban and alarm_result and linkban_result:
        # 有预警 + 有连板但无轮动：复用 generate_markdown_report 需要 rotation_result
        # 这里用 dummy_rotation
        dummy_rotation = {
            'results': [], 'rank_delta_map': {}, 'last_rank': None,
            'summary': {'total_basket': 0, 'data_ok': 0,
                        'leader_count': 0, 'laggard_count': 0},
            'analyzed_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        md_text = generate_markdown_report(alarm_result, dummy_rotation,
                                           linkban_result=linkban_result)
    elif run_linkban and linkban_result:
        # 仅连板模式：ASCII 报告已在上方打印，md_text 这里也拼一份便于落盘
        md_text = (f"### 🔥 {args.date} 连板龙头 & 大盘温度\n\n"
                   f"```text\n{format_linkban_report(linkban_result).rstrip()}\n```\n")

    # --------- 控制台输出（非静默）---------
    if not args.quiet:
        if md_text and (run_alarms or run_rotation) and not (
                getattr(args, 'linkban_only', False)):
            from notifier import get_notifier
            n = get_notifier('console')
            n.notify(md_text, title=f'{args.date} 市场全景分析')
        elif alarm_result and (not run_rotation and not run_linkban):
            # 纯预警模式已在上方打印过
            pass
        # 性能统计
        dt = time.time() - t0
        print(f'⏱  总耗时: {dt:.2f}s')

    # --------- 落盘 ---------
    # 1. 预警 CSV
    if run_alarms and not args.no_csv and alarm_result:
        try:
            path = log_alarm_signal(alarm_result)
            if not args.quiet:
                print(f'📝 预警信号已记录: {path}')
        except Exception as e:
            logger.warning("写预警 CSV 失败: %s", e)
    # 2. 连板 CSV
    if run_linkban and not args.no_csv and linkban_result:
        try:
            path = log_linkban_signal(linkban_result, cfg)
            if not args.quiet and path:
                print(f'🔥 连板温度已记录: {path}')
        except Exception as e:
            logger.warning("写连板 CSV 失败: %s", e)
    # 3. Markdown 报告
    if md_text and not args.no_report:
        try:
            path = save_markdown_report(md_text, cfg, args.date)
            if not args.quiet:
                print(f'📊 Markdown 报告已保存: {path}')
        except Exception as e:
            logger.warning("写 Markdown 报告失败: %s", e)

    return 0


def main() -> int:
    args = _parse_args(sys.argv[1:])

    # 日志
    if args.verbose:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                            datefmt="%H:%M:%S")
    else:
        root = logging.getLogger()
        if not root.handlers:
            _h = logging.StreamHandler()
            _h.setLevel(logging.WARNING)
            root.addHandler(_h)
            root.setLevel(logging.WARNING)

    # --history
    if args.history:
        return print_history(args.history_months)

    # 加载配置
    try:
        cfg = get_alarm_config(args.profile)
    except ValueError as e:
        print(f'❌ {e}')
        print(f'可用配置档:\n{list_profiles()}')
        return 1

    # 校验 --indicator
    if args.indicator != 'all':
        unknown = [k.strip() for k in args.indicator.split(',')
                   if k.strip() and k.strip() not in _INDICATOR_KEYS]
        if unknown:
            print(f'❌ 未知指标: {unknown}')
            print(f'可用: {", ".join(_INDICATOR_KEYS.keys())}')
            return 1

    # 参考日决策（方案A：数据驱动）
    user_specified_date = args.date is not None
    if not user_specified_date:
        args.date = _resolve_default_ref_date(cfg)
        if not args.quiet:
            today = date.today().isoformat()
            if args.date == today:
                print(f'🗓  自动选择参考日: {args.date}（指数已更新至今日）')
            else:
                print(f'🗓  自动选择参考日: {args.date}（指数最新数据尚未更新，回退昨日）')
    else:
        if not args.quiet:
            print(f'🗓  用户指定参考日: {args.date}')

    try:
        return _run_once(args, cfg)
    except Exception as e:
        logger.exception("执行失败: %s", e)
        print(f'❌ 执行失败: {e}')
        return 1


if __name__ == "__main__":
    sys.exit(main())
