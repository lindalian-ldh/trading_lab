"""CSV 持久化：按月分文件追加写入。

路径派生（仿 sell_monitor/storage.py）：
    alarming_monitor/  →  services/  →  trading_lab/  →  data/alarming_signals/
    文件名: alarming_YYYY-MM.csv（按月分，追加模式，首行写表头）。

约定（与项目 memory 对齐）：
    - 交易/信号类记录走 CSV 按天/月分文件，便于复盘
    - 本服务无持仓状态，不建 SQLite 表
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from reporter import CSV_FIELDS, format_csv_row, LINKBAN_CSV_FIELDS, format_linkban_csv_row

logger = logging.getLogger(__name__)


def _atomic_write_csv(df: pd.DataFrame, path: Path) -> None:
    """原子写 CSV：先写同目录 .tmp，再 os.replace 覆盖目标。

    并发/中途崩溃不会产生半截文件；失败仅 warning，不阻断。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + '.tmp')
        df.to_csv(tmp_path, index=False, encoding='utf-8-sig')
        os.replace(tmp_path, path)
    except Exception as e:
        logger.warning("原子写 CSV 失败 %s: %s", path.name, e)
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass

# ── 路径派生 ──
_SERVICE_DIR = Path(__file__).resolve().parent
_TRADING_LAB_DIR = _SERVICE_DIR.parent.parent
_CSV_DIR = _TRADING_LAB_DIR / "data" / "alarming_signals"


def _linkban_csv_dir(cfg) -> Path:
    """派生连板 CSV 目录：cfg.LINKBAN_CSV_DIR 相对 trading_lab 根。"""
    p = getattr(cfg, 'LINKBAN_CSV_DIR', 'data/alarming_signals') or 'data/alarming_signals'
    pp = Path(str(p))
    if pp.is_absolute():
        return pp
    return _TRADING_LAB_DIR / pp


def log_alarm_signal(result: dict) -> Path:
    """把一次运行结果追加写入当月 CSV。返回文件路径。

    文件名: data/alarming_signals/alarming_YYYY-MM.csv
    不存在则创建并写表头；存在则按 ref_date 幂等去重后写回（同日多次运行只保留最新一条）。

    幂等性：按 ref_date 去重，同 ref_date 重复运行不会产生重复行。
    原子写：tmp + os.replace，并发/崩溃不产生坏文件。

    兼容性：当 CSV 现有表头是 reporter.CSV_FIELDS 的子集（例如新增 S6 列后旧文件
    缺 s6_net_leverage / s6_red），先把旧数据读出来，按新列顺序 + 空值重写，
    避免 DictWriter 把新列写到文件尾（位置错位导致列数≠表头数）。
    """
    _CSV_DIR.mkdir(parents=True, exist_ok=True)

    # 按 ref_date 的月份分文件（ref_date 缺失时用 run_time 月份）
    ref = result.get('ref_date') or result.get('run_time', '')[:10]
    try:
        month_key = datetime.strptime(ref[:7], "%Y-%m").strftime("%Y-%m")
    except Exception:
        month_key = datetime.now().strftime("%Y-%m")

    path = _CSV_DIR / f"alarming_{month_key}.csv"
    row = format_csv_row(result)

    # 读旧数据（含表头兼容处理）→ 按 ref_date 去重 → 追加新行 → 原子写回
    old_rows = []
    if path.exists():
        try:
            # 不用 pd.read_csv：旧表头比实际列数少时，pandas 会丢掉末列残差
            # （例如 17 列表头 + 每行 23 列 → 末 6 列被截断，无法对齐）。
            # 用纯 csv.reader 以最长行的列数重建。
            with path.open('r', newline='', encoding='utf-8-sig') as f:
                raw = list(csv.reader(f))
            if raw and len(raw) > 1:
                header_old = raw[0]
                max_cols = max(len(r) for r in raw)
                n_old = len(header_old)
                if n_old < max_cols or n_old < len(CSV_FIELDS):
                    for r in raw[1:]:
                        rr = (r + [''] * max_cols)[:max_cols]
                        map_dict = {}
                        for i, col_name in enumerate(CSV_FIELDS):
                            if col_name in header_old:
                                j = header_old.index(col_name)
                                map_dict[col_name] = rr[j] if j < len(rr) else ''
                            else:
                                map_dict[col_name] = ''
                        old_rows.append(map_dict)
                else:
                    # 表头与 CSV_FIELDS 一致 → 直接按列名映射
                    for r in raw[1:]:
                        map_dict = {col_name: (r[j] if j < len(r) else '')
                                    for j, col_name in enumerate(header_old)}
                        old_rows.append(map_dict)
        except Exception as e:
            logger.debug("alarming CSV 读取失败（按空表继续）: %s", e)
            old_rows = []

    # 按 ref_date 幂等去重：去掉同 ref_date 的旧行
    ref_str = str(ref)
    old_rows = [r for r in old_rows
                if str(r.get('ref_date', ''))[:10] != ref_str[:10]]

    old_rows.append(row)
    out_df = pd.DataFrame(old_rows, columns=CSV_FIELDS)
    _atomic_write_csv(out_df, path)
    logger.debug("预警信号已记录: %s (%s)", path, ref)
    return path


def list_history(months: int = 3) -> Optional[pd.DataFrame]:
    """读取近 N 个月（含当月）CSV，合并返回 DataFrame。

    无任何文件时返回 None。
    """
    if not _CSV_DIR.exists():
        return None

    now = datetime.now()
    # 当月往前 months-1 个月，共 months 个月
    targets = []
    for i in range(months):
        y, m = now.year, now.month - i
        while m <= 0:
            m += 12
            y -= 1
        targets.append(f"{y:04d}-{m:02d}")

    files = [_CSV_DIR / f"alarming_{t}.csv" for t in targets]
    files = [f for f in files if f.exists()]
    if not files:
        return None

    dfs = [pd.read_csv(f, encoding='utf-8-sig') for f in files]
    df = pd.concat(dfs, ignore_index=True)
    if 'run_time' in df.columns:
        df = df.sort_values('run_time').reset_index(drop=True)
    return df


def print_history(months: int = 3) -> int:
    """打印近 N 个月历史汇总。返回退出码。"""
    df = list_history(months)
    if df is None or df.empty:
        print(f'（暂无历史记录，目录: {_CSV_DIR}）')
        return 0

    print(f'近 {months} 个月预警历史（{len(df)} 条）：')
    print('-' * 80)
    cols = [c for c in ['run_time', 'ref_date', 'risk_level', 'red_count',
                        'position_advice'] if c in df.columns]
    # 截取宽列避免终端换行
    print(df[cols].to_string(index=False))
    return 0


# ====================================================================
# S3 涨跌家数自累积（legu 仅今日，靠 CSV 重建近 N 日序列）
# ====================================================================

_BREADTH_FIELDS = ['date', 'up_count', 'down_count', 'flat_count', 'ad_ratio']


def _breadth_path(date_str: str) -> Path:
    """按月分文件：breadth_YYYY-MM.csv"""
    try:
        month_key = datetime.strptime(date_str[:7], "%Y-%m").strftime("%Y-%m")
    except Exception:
        month_key = datetime.now().strftime("%Y-%m")
    return _CSV_DIR / f"breadth_{month_key}.csv"


def append_breadth_today(date_str: str, up_count: int, down_count: int,
                         flat_count: int = 0, ad_ratio: Optional[float] = None
                         ) -> Path:
    """追加当日涨跌家数到 breadth CSV（按月分文件）。

    同日重复运行会先去重（保留最新一条），避免多次追加污染序列。
    ad_ratio 缺失时自动按 up/down 计算（down=0 时置 inf）。
    """
    _CSV_DIR.mkdir(parents=True, exist_ok=True)
    path = _breadth_path(date_str)

    # 计算或补全 ad_ratio
    if ad_ratio is None:
        import math
        ad_ratio = float(up_count / down_count) if down_count > 0 else float('inf')

    # 读旧 → 去重当日 → 追加新行 → 写回
    if path.exists():
        try:
            old = pd.read_csv(path, encoding='utf-8-sig')
        except Exception:
            old = pd.DataFrame(columns=_BREADTH_FIELDS)
    else:
        old = pd.DataFrame(columns=_BREADTH_FIELDS)

    # 去掉当日旧行（保留非当日的）
    if 'date' in old.columns:
        old = old[old['date'].astype(str) != date_str]

    new_row = pd.DataFrame([{
        'date': date_str, 'up_count': up_count, 'down_count': down_count,
        'flat_count': flat_count, 'ad_ratio': ad_ratio,
    }])
    out = pd.concat([old, new_row], ignore_index=True)
    out = out.sort_values('date').reset_index(drop=True)
    _atomic_write_csv(out, path)
    logger.debug("breadth 当日已写入: %s (%s)", path, date_str)
    return path


def read_breadth_history(days: int = 20) -> Optional[pd.DataFrame]:
    """读近 N 日（含当月及上月跨月文件）的 breadth 序列。

    返回 date/up_count/down_count/flat_count/ad_ratio，升序，末值最新。
    无文件或全空返回 None。
    """
    if not _CSV_DIR.exists():
        return None

    now = datetime.now()
    targets = []
    for i in range(2):  # 当月 + 上月，跨月时足够覆盖近 30 日
        y, m = now.year, now.month - i
        while m <= 0:
            m += 12
            y -= 1
        targets.append(f"{y:04d}-{m:02d}")

    files = [_CSV_DIR / f"breadth_{t}.csv" for t in targets]
    files = [f for f in files if f.exists()]
    if not files:
        return None

    dfs = []
    for f in files:
        try:
            d = pd.read_csv(f, encoding='utf-8-sig')
            dfs.append(d)
        except Exception:
            continue
    if not dfs:
        return None
    df = pd.concat(dfs, ignore_index=True)
    if 'date' not in df.columns or 'ad_ratio' not in df.columns:
        return None
    df['date'] = df['date'].astype(str)
    df = df.drop_duplicates(subset='date', keep='last')  # 跨月去重
    df = df.sort_values('date').reset_index(drop=True)
    df = df.tail(days).reset_index(drop=True)
    if df.empty:
        return None
    return df[['date', 'up_count', 'down_count', 'flat_count', 'ad_ratio']]


# ====================================================================
# S4 连板龙头 & 大盘温度 CSV 落盘
# ====================================================================

def log_linkban_signal(result: dict, cfg=None) -> Optional[Path]:
    """把一次连板分析结果追加写入当月 CSV。返回文件路径。

    文件名: data/alarming_signals/linkban_YYYY-MM.csv（按月分，追加模式，首行写表头）。
    同日重复运行会先去重（保留最新一条），避免多次追加。

    Args:
        result: analyze_linkban 返回的 dict（必须含 ref_date）
        cfg:    AlarmConfig，用于派生 LINKBAN_CSV_DIR；None 时用默认路径
    """
    if not result:
        return None
    ref = result.get('ref_date') or ''
    if not ref:
        logger.warning("log_linkban_signal 缺少 ref_date，跳过")
        return None

    # 目录
    csv_dir = _linkban_csv_dir(cfg) if cfg is not None else _CSV_DIR
    csv_dir.mkdir(parents=True, exist_ok=True)

    # 月份
    try:
        month_key = datetime.strptime(str(ref)[:7], "%Y-%m").strftime("%Y-%m")
    except Exception:
        month_key = datetime.now().strftime("%Y-%m")
    path = csv_dir / f"linkban_{month_key}.csv"

    # 补齐 run_time
    import time as _time
    if 'run_time' not in result or not result.get('run_time'):
        result = dict(result)
        result['run_time'] = _time.strftime('%Y-%m-%d %H:%M:%S')

    row = format_linkban_csv_row(result)

    # 读旧 → 去重当日 ref_date → 追加新行 → 写回
    if path.exists():
        try:
            old = pd.read_csv(path, encoding='utf-8-sig', dtype=str)
        except Exception:
            old = pd.DataFrame(columns=LINKBAN_CSV_FIELDS)
    else:
        old = pd.DataFrame(columns=LINKBAN_CSV_FIELDS)

    # 去掉当日旧行
    if 'ref_date' in old.columns:
        old = old[old['ref_date'].astype(str) != str(ref)]

    new_row = pd.DataFrame([{k: row.get(k, '') for k in LINKBAN_CSV_FIELDS}])
    out = pd.concat([old, new_row], ignore_index=True)
    out = out.sort_values('ref_date').reset_index(drop=True) if 'ref_date' in out.columns else out
    _atomic_write_csv(out, path)
    logger.debug("连板信号已记录: %s (%s)", path, ref)
    return path


def read_linkban_history(months: int = 3) -> Optional[pd.DataFrame]:
    """读取近 N 个月（含当月）连板 CSV，合并返回 DataFrame。无任何文件返回 None。"""
    targets = []
    now = datetime.now()
    for i in range(months):
        y, m = now.year, now.month - i
        while m <= 0:
            m += 12
            y -= 1
        targets.append(f"{y:04d}-{m:02d}")

    # 先用默认目录；未来若 cfg 可传，可再扩展参数
    files = [_CSV_DIR / f"linkban_{t}.csv" for t in targets]
    files = [f for f in files if f.exists()]
    if not files:
        return None
    dfs = []
    for f in files:
        try:
            d = pd.read_csv(f, encoding='utf-8-sig')
            dfs.append(d)
        except Exception:
            continue
    if not dfs:
        return None
    df = pd.concat(dfs, ignore_index=True)
    if 'ref_date' in df.columns:
        df = df.drop_duplicates(subset='ref_date', keep='last')
        df = df.sort_values('ref_date').reset_index(drop=True)
    return df


# ====================================================================
# S6 两融 CSV 自累积（与 breadth 同构：按日单行，重建近 25+ 日序列）
# ====================================================================

# margin CSV 行结构：每一行对应 fetch_margin_summary 返回的最新交易日单行
MARGIN_FIELDS = [
    'date',                # YYYY-MM-DD
    'rzye_yuan',           # 融资余额（元）
    'rzmre_yuan',          # 融资买入额（元）
    'rqye_yuan',           # 融券余额（元）
    'rzrqye_yuan',         # 融资融券余额（元）
    'rqmcl',               # 融券卖出量
    'rqyl',                # 融券余量
]


def _margin_csv_dir(cfg=None) -> Path:
    if cfg is not None:
        p = getattr(cfg, 'MARGIN_CSV_DIR', None) or 'data/alarming_signals'
    else:
        p = 'data/alarming_signals'
    pp = Path(str(p))
    return pp if pp.is_absolute() else _TRADING_LAB_DIR / pp


def _margin_path(date_str: str, cfg=None) -> Path:
    """两融 CSV 按月分：margin_YYYY-MM.csv"""
    try:
        month_key = datetime.strptime(str(date_str)[:7], "%Y-%m").strftime("%Y-%m")
    except Exception:
        month_key = datetime.now().strftime("%Y-%m")
    return _margin_csv_dir(cfg) / f"margin_{month_key}.csv"


def append_margin_row(date_str: str,
                      rzye_yuan: float,
                      rzmre_yuan: float = float('nan'),
                      rqye_yuan: float = float('nan'),
                      rzrqye_yuan: float = float('nan'),
                      rqmcl: float = float('nan'),
                      rqyl: float = float('nan'),
                      cfg=None) -> Path:
    """把两融汇总单行追加到 margin CSV。

    与 breadth 完全同构：同日重复运行先去重，保留最新一条。
    单位全为元（rqmcl/rqyl 保留原始股数口径）。
    """
    d = _margin_csv_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    path = _margin_path(date_str, cfg)

    # 读旧 → 去重当日 → 追加新行 → 写回
    if path.exists():
        try:
            old = pd.read_csv(path, encoding='utf-8-sig')
        except Exception:
            old = pd.DataFrame(columns=MARGIN_FIELDS)
    else:
        old = pd.DataFrame(columns=MARGIN_FIELDS)

    if 'date' in old.columns:
        old = old[old['date'].astype(str) != str(date_str)]

    new_row = pd.DataFrame([{
        'date': date_str,
        'rzye_yuan': float(rzye_yuan),
        'rzmre_yuan': float(rzmre_yuan),
        'rqye_yuan': float(rqye_yuan),
        'rzrqye_yuan': float(rzrqye_yuan),
        'rqmcl': float(rqmcl),
        'rqyl': float(rqyl),
    }])
    out = pd.concat([old, new_row], ignore_index=True)
    out = out.sort_values('date').reset_index(drop=True)
    _atomic_write_csv(out, path)
    logger.debug("两融当日已写入: %s (%s)", path, date_str)
    return path


def read_margin_history(days: int = 25, cfg=None) -> Optional[pd.DataFrame]:
    """读近 days 日（跨月覆盖）的两融 CSV 序列。

    返回列: date / rzye / rzmre / rqye / rzrqye / rqmcl / rqyl（单位统一为元）。
    升序，末值最新。注意列名与 fetch_margin_summary 完全一致，便于 main 层
    用 prefer_csv_df 直接喂给 fetch_margin_summary。
    无文件或全空返回 None。
    """
    base = _margin_csv_dir(cfg)
    if not base.exists():
        return None
    now = datetime.now()
    targets = []
    # 当月 + 往前 2 个月，跨月足够覆盖 25+ 个交易日（自然日 3*31=93）
    for i in range(3):
        y, m = now.year, now.month - i
        while m <= 0:
            m += 12
            y -= 1
        targets.append(f"{y:04d}-{m:02d}")
    files = [base / f"margin_{t}.csv" for t in targets]
    files = [f for f in files if f.exists()]
    if not files:
        return None
    dfs = []
    for f in files:
        try:
            d = pd.read_csv(f, encoding='utf-8-sig')
            dfs.append(d)
        except Exception:
            continue
    if not dfs:
        return None
    df = pd.concat(dfs, ignore_index=True)
    if 'date' not in df.columns or 'rzye_yuan' not in df.columns:
        return None
    df['date'] = df['date'].astype(str).str[:10]
    df = df.drop_duplicates(subset='date', keep='last')
    df = df.sort_values('date').reset_index(drop=True)
    df = df.tail(days).reset_index(drop=True)
    if df.empty:
        return None
    # 列名映射：MARGIN_FIELDS（_yuan 后缀）→ fetch_margin_summary 统一列名（无 _yuan）
    rename_map = {
        'rzye_yuan':   'rzye',
        'rzmre_yuan':  'rzmre',
        'rqye_yuan':   'rqye',
        'rzrqye_yuan': 'rzrqye',
    }
    for k, v in rename_map.items():
        if k in df.columns:
            df[v] = pd.to_numeric(df[k], errors='coerce')
    for keep in ['rqmcl', 'rqyl']:
        if keep in df.columns:
            df[keep] = pd.to_numeric(df[keep], errors='coerce')
    want = ['date', 'rzye', 'rzmre', 'rqye', 'rzrqye', 'rqmcl', 'rqyl']
    df = df[[c for c in want if c in df.columns]].reset_index(drop=True)
    return df
