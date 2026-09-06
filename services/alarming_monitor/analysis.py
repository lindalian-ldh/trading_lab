"""板块轮动汇总：批量分析 ROTATION_BASKET + 排名变化 + Markdown 表格行。

核心函数：
    run_rotation_analysis()    一键跑完整板块轮动（对齐→逐ETF分析→排序→排名差）
    build_rotation_rows()      从结果生成 Markdown 表格行（给 reporter.py 用）

排名变化（rank_delta）：
    从 ROTATION_RANK_CACHE 文件读取上次排名，与今日 total_score 排名比较，
    输出 ΔN（正数=名次上升，负数=名次下降，"新"=首次进入排名）。
    缓存文件不存在时，全部标记为"新"。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Optional

from config import AlarmConfig
from rotation import align_to_benchmark, analyze_single_etf

logger = logging.getLogger(__name__)


def _project_root() -> str:
    """项目根目录（与 cache.py 保持一致）。"""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def _rank_cache_path(cfg: AlarmConfig) -> str:
    """上次轮动排名缓存文件路径。"""
    return os.path.join(_project_root(), cfg.CACHE_DIR, 'rotation_rank_last.json')


def _read_last_rank(cfg: AlarmConfig) -> Optional[dict[str, int]]:
    """读取上次排名缓存 → {symbol: rank}。失败返回 None。"""
    path = _rank_cache_path(cfg)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return {str(k): int(v) for k, v in data.items()}
    except Exception as e:
        logger.debug("读取上次排名缓存失败（首次运行正常）: %s", e)
        return None


def _write_last_rank(cfg: AlarmConfig, ranked: list[dict]) -> None:
    """写入今日排名缓存（按 total_score 降序）。原子写：tmp + os.replace。"""
    path = _rank_cache_path(cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rank_dict = {}
    for i, r in enumerate(ranked):
        if r.get('data_sufficient') and r.get('score_5d') is not None:
            rank_dict[r['symbol']] = i + 1  # 排名从 1 开始
    tmp_path = path + '.tmp'
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(rank_dict, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception as e:
        logger.warning("写入排名缓存失败: %s", e)
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except Exception:
            pass


def _compute_rank_delta(symbol: str, current_rank: int,
                        last_rank: Optional[dict]) -> str:
    """计算排名变化字符串。

    Args:
        symbol: ETF 代码
        current_rank: 今日排名（从 1 开始）
        last_rank: 上次排名缓存 {symbol: rank}，可能为 None

    Returns:
        "+N"（名次上升 N 位）/ "-N"（下降 N 位）/ "持平" / "新"（首次）
    """
    if last_rank is None or symbol not in last_rank:
        return '新'
    delta = last_rank[symbol] - current_rank  # 排名数字越小越好
    if delta == 0:
        return '持平'
    sign = '+' if delta > 0 else ''
    return f'{sign}{delta}'


# ====================================================================
# 1. 主入口：一键轮动分析
# ====================================================================

def run_rotation_analysis(bench_df, etf_full_dfs: dict,
                          cfg: AlarmConfig) -> dict:
    """运行完整板块轮动分析流程。

    Args:
        bench_df: 基准指数日线（沪深300，含 date/close/high/low）
        etf_full_dfs: {symbol: etf_df}，未对齐的原始 ETF 日线（含完整历史）
        cfg: AlarmConfig

    Returns:
        dict 结构：
        {
            'results': list[dict],       # analyze_single_etf 列表，按 total_score 降序
            'rank_delta_map': dict,      # {symbol: rank_delta_str}
            'last_rank': dict | None,    # 上次排名缓存
            'summary': {                 # 汇总统计
                'total_basket': int,     # 篮子总数
                'data_ok': int,          # 数据充足数
                'leader_quadrant': str,  # 领涨象限的板块数
                'laggard_quadrant': str, # 滞后象限的板块数
            }
        }
    """
    # 1. 数据对齐：以基准交易日为锚，所有 ETF ffill 对齐
    aligned_etfs = align_to_benchmark(bench_df, etf_full_dfs)

    # 2. 逐 ETF 分析
    results = []
    for sym in cfg.ROTATION_BASKET:
        aligned_etf = aligned_etfs.get(sym)
        r = analyze_single_etf(sym, aligned_etf, bench_df, cfg)
        results.append(r)

    # 3. 按 total_score 降序排序（数据不足的放最后）
    def _sort_key(r):
        s5 = r.get('score_5d') or {}
        score = s5.get('total_score', -1.0)
        # 数据充足且有得分的优先；同分按 symbol 稳定排序
        return (0 if r.get('data_sufficient') else 1,
                -float(score if score is not None else -1.0),
                r.get('symbol', ''))

    results.sort(key=_sort_key)

    # 4. 排名变化（读取上次缓存 + 计算 delta + 写今日缓存）
    last_rank = _read_last_rank(cfg)
    rank_delta_map = {}
    for i, r in enumerate(results):
        if r.get('data_sufficient') and r.get('score_5d') is not None:
            rank_delta_map[r['symbol']] = _compute_rank_delta(r['symbol'], i + 1, last_rank)
        else:
            rank_delta_map[r['symbol']] = '—'
    _write_last_rank(cfg, results)

    # 5. 汇总统计
    data_ok = sum(1 for r in results if r.get('data_sufficient'))
    leader = sum(1 for r in results if r.get('quadrant') == '领涨主线')
    laggard = sum(1 for r in results if r.get('quadrant') == '滞后回避')
    summary = {
        'total_basket': len(cfg.ROTATION_BASKET),
        'data_ok': data_ok,
        'leader_count': leader,
        'laggard_count': laggard,
    }

    return {
        'results': results,
        'rank_delta_map': rank_delta_map,
        'last_rank': last_rank,
        'summary': summary,
        'analyzed_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


# ====================================================================
# 2. Markdown 表格行（给 reporter.py 用）
# ====================================================================

def build_rotation_table_rows(rotation_result: dict) -> list[str]:
    """从轮动分析结果生成 Markdown 表格行（不含表头）。

    列顺序严格按用户模板：
    | 板块 | 收盘价 | ADX | RS-Ratio | RS-Momentum | 象限标签 | 5维评分 | 排名Δ | 操作参考 |

    数据不足行：5维评分、排名Δ 列显示"—"，象限显示"数据不足"。
    """
    rows = []
    results = rotation_result.get('results', [])
    rank_delta_map = rotation_result.get('rank_delta_map', {})

    for r in results:
        label = r.get('label', r.get('symbol', ''))
        sym = r.get('symbol', '')

        if not r.get('data_sufficient'):
            close_s = '—'
            adx_s = '—'
            ratio_s = '—'
            mom_s = '—'
            quad_s = f'数据不足（{r.get("reason", "未知原因")}）'
            score_s = '—'
            rank_s = '—'
            action_s = '—'
        else:
            close_s = f"{r['close']:.3f}" if r.get('close') is not None else '—'
            adx_s = f"{r['adx']:.1f}" if r.get('adx') is not None else '—'
            ratio_s = f"{r['rs_ratio']:.2f}" if r.get('rs_ratio') is not None else '—'
            mom_raw = r.get('rs_momentum')
            if mom_raw is None:
                mom_s = '—'
            else:
                sign = '+' if mom_raw >= 0 else ''
                mom_s = f'{sign}{mom_raw:.1f}%'
            emoji = r.get('quadrant_emoji', '')
            quad_s = f'{emoji} {r.get("quadrant", "")}'
            s5 = r.get('score_5d') or {}
            score_s = f"{s5.get('total_score', '—'):.1f}" if s5.get('total_score') is not None else '—'
            if s5.get('crowding_penalty_applied'):
                score_s += ' ⚠️'
            rank_s = rank_delta_map.get(sym, '—')
            action_s = s5.get('action_hint', '—')

        row = f'| {label} | {close_s} | {adx_s} | {ratio_s} | {mom_s} | {quad_s} | {score_s} | {rank_s} | {action_s} |'
        rows.append(row)

    return rows


def build_rrg_summary(rotation_result: dict) -> str:
    """RRG 象限一句话摘要（给 reporter 顶部用）。"""
    summary = rotation_result.get('summary', {}) or {}
    total = summary.get('total_basket', 0)
    ok = summary.get('data_ok', 0)
    leader = summary.get('leader_count', 0)
    laggard = summary.get('laggard_count', 0)
    return (f'板块篮子 {total} 只 · 数据充足 {ok} 只 · '
            f'领涨主线 🟢{leader} 只 · 滞后回避 🔴{laggard} 只')
