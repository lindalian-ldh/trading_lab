"""宏观数据图表查询服务层（与采集模块解耦）。

仅依赖 macro_data 表（数据契约）与 core/config 基建，不导入采集模块的 fetcher。
所有边界处理（需求 7.1~7.5）均在本层完成，前端/渲染层只负责展示。

核心入口：query_chart(...)
返回统一结构 dict；output=json 直接序列化，output=html 交由 render.render_html()。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

from config.settings import settings
from core.logger import get_logger
from core.storage import execute_sql

log = get_logger("macro.charts")

# 错误码
ERR_DB_EMPTY = 1001        # 数据库为空（新系统首次运行）
ERR_INDICATOR_NOT_FOUND = 1002  # 指标不存在
ERR_DATE_FORMAT = 1003     # 日期格式错误

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 频率 -> pandas date_range freq（用于缺口检测）
FREQ_MAP = {"D": "D", "W": "W-SUN", "M": "MS", "Q": "QS", "Y": "YS"}

DOWNSAMPLE_THRESHOLD = 500
DOWNSAMPLE_TARGET = 200
MAX_COMPARE = 3


# --------------------------------------------------------------------------- #
# 数据加载（可被单测 mock）
# --------------------------------------------------------------------------- #

TABLE_CN = "macro_data"
TABLE_US = "macro_us_data"
# 表名白名单（FROM 子句不可参数化，故用白名单防 SQL 注入）
ALLOWED_TABLES = {TABLE_CN, TABLE_US}


def _load_records(indicator: str, country: str, table: str = TABLE_CN) -> list[dict]:
    """从指定表读取某指标+国家的全部记录，按 date 升序。

    table 取自 ALLOWED_TABLES 白名单（macro_data / macro_us_data）。
    """
    if table not in ALLOWED_TABLES:
        raise ValueError(f"非法表名: {table}")
    rows = execute_sql(
        f"SELECT indicator, name, country, frequency, date, value, unit, "
        f"source, source_url, fetch_time, extra "
        f"FROM {table} WHERE indicator=? AND country=? ORDER BY date",
        (indicator, country),
    )
    return [dict(r) for r in rows]


def _db_has_data(table: str = TABLE_CN) -> bool:
    """指定表是否存在且非空。"""
    if table not in ALLOWED_TABLES:
        return False
    try:
        rows = execute_sql(f"SELECT COUNT(*) AS c FROM {table}")
        return bool(rows) and rows[0]["c"] > 0
    except sqlite3.OperationalError:
        return False  # 表不存在


# --------------------------------------------------------------------------- #
# 日期工具
# --------------------------------------------------------------------------- #

def _valid_date(s: str) -> bool:
    return bool(s) and bool(DATE_RE.match(s))


def _parse_date(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# 缺口检测（需求 7.2：数据本身存在日期缺失）
# --------------------------------------------------------------------------- #

def _detect_gaps(dates: list[str], frequency: str) -> list[dict]:
    """检测时间序列中缺失的周期，返回缺口区间列表 [{start, end}]。

    start/end 为缺口前后的有效日期（用于渲染虚线连接 + 三角形标记）。
    """
    if len(dates) < 2 or frequency not in FREQ_MAP:
        return []
    ts = sorted(pd.to_datetime(dates))
    expected = pd.date_range(ts[0], ts[-1], freq=FREQ_MAP[frequency])
    present = set(ts)
    missing = [d for d in expected if d not in present]
    if not missing:
        return []

    # 把连续缺失分组成区间，取前后有效点
    gaps: list[dict] = []
    run = [missing[0]]
    for d in missing[1:]:
        if (d - run[-1]).days <= (expected[1] - expected[0]).days * 1.5:
            run.append(d)
        else:
            gaps.append(_gap_interval(ts, run))
            run = [d]
    gaps.append(_gap_interval(ts, run))
    return [g for g in gaps if g]


def _gap_interval(ts_sorted, run) -> dict | None:
    before = max((t for t in ts_sorted if t < run[0]), default=None)
    after = min((t for t in ts_sorted if t > run[-1]), default=None)
    if before is None or after is None:
        return None
    return {"start": before.strftime("%Y-%m-%d"), "end": after.strftime("%Y-%m-%d")}


# --------------------------------------------------------------------------- #
# 单序列处理
# --------------------------------------------------------------------------- #

def _build_series(indicator: str, country: str, start_date: str | None,
                  end_date: str | None, limit: int | None, table: str = TABLE_CN) -> dict:
    """处理单个指标，返回一个 series dict（已含全部边界信息）。"""
    records = _load_records(indicator, country, table)
    if not records:
        return {"indicator": indicator, "country": country, "not_found": True}

    # 清洗：剔除 value 为 NULL/NaN
    cleaned = []
    for r in records:
        v = r.get("value")
        if v is None or (isinstance(v, float) and np.isnan(v)):
            continue
        try:
            r["value"] = float(v)
        except (TypeError, ValueError):
            continue
        cleaned.append(r)
    cleaned.sort(key=lambda r: r["date"])

    if not cleaned:
        return {"indicator": indicator, "country": country, "not_found": True}

    name = cleaned[-1].get("name") or indicator
    unit = cleaned[-1].get("unit") or ""
    frequency = cleaned[-1].get("frequency") or "M"

    all_dates = [r["date"] for r in cleaned]
    earliest, latest = all_dates[0], all_dates[-1]
    today = date.today().isoformat()

    # 日期范围过滤（优先级高于 limit）
    date_filter_active = bool(start_date or end_date)
    sd = start_date or earliest
    ed = end_date or today
    # end_date 晚于今天 -> 截到今天
    if _parse_date(ed) and _parse_date(ed) > _parse_date(today):
        ed = today
        log.info("%s end_date 晚于今天，已截取为今天", indicator)
    # start_date 早于最早记录 -> 截到最早
    if _parse_date(sd) and _parse_date(sd) < _parse_date(earliest):
        sd = earliest
        log.info("%s start_date 早于最早记录，已自动调整为 %s", indicator, earliest)

    filtered = cleaned
    if date_filter_active:
        filtered = [r for r in cleaned if sd <= r["date"] <= ed]
    elif limit and limit > 0:
        filtered = cleaned[-limit:]

    warnings: list[str] = []
    if not filtered:
        return {
            "indicator": indicator, "name": name, "country": country, "unit": unit,
            "frequency": frequency, "dates": [], "values": [], "values_display": [],
            "fetch_times": [], "revision_status": [], "count": 0, "original_count": 0,
            "downsampled": False, "single_point": False, "few_samples": False,
            "no_fluctuation": False, "all_negative": False, "revised": False,
            "gaps": [], "frequency_changes": [], "warnings": ["所选时间段内无匹配记录，请调整筛选范围"],
        }

    # 缺口检测（在过滤后、降采样前的数据上）
    gaps = _detect_gaps([r["date"] for r in filtered], frequency)

    # 降采样（>500 期）
    original_count = len(filtered)
    downsampled = False
    if original_count > DOWNSAMPLE_THRESHOLD:
        idx = np.linspace(0, original_count - 1, DOWNSAMPLE_TARGET).round().astype(int)
        idx = sorted(set(idx))
        filtered = [filtered[i] for i in idx]
        downsampled = True
        warnings.append(f"已降采样显示，原始数据共 {original_count} 条")

    dates = [r["date"] for r in filtered]
    values = [r["value"] for r in filtered]
    values_display = [round(v, 2) for v in values]
    fetch_times = [r.get("fetch_time", "") for r in filtered]
    revision_status = [_parse_revision(r.get("extra")) for r in filtered]

    # 频率变更检测（7.4）
    freq_changes = _detect_frequency_changes(filtered)

    # 标志位
    n = len(values)
    single_point = n == 1
    few_samples = n == 2
    no_fluctuation = n > 0 and len(set(values)) == 1
    all_negative = n > 0 and all(v < 0 for v in values)
    revised = any(rs == "revised" for rs in revision_status)

    if single_point:
        warnings.append("当前仅1期数据，趋势尚不可见")
    if few_samples:
        warnings.append("样本量较少，趋势参考有限")
    if no_fluctuation:
        warnings.append("数据无波动，请核实源数据")

    return {
        "indicator": indicator, "name": name, "country": country, "unit": unit,
        "frequency": frequency, "dates": dates, "values": values,
        "values_display": values_display, "fetch_times": fetch_times,
        "revision_status": revision_status, "count": n, "original_count": original_count,
        "downsampled": downsampled, "single_point": single_point, "few_samples": few_samples,
        "no_fluctuation": no_fluctuation, "all_negative": all_negative, "revised": revised,
        "gaps": gaps, "frequency_changes": freq_changes, "warnings": warnings,
        "date_range": {"start": dates[0], "end": dates[-1]} if dates else {},
    }


def _parse_revision(extra: Any) -> str:
    """从 extra(JSON 字符串或 dict) 解析 revision_status。"""
    if not extra:
        return ""
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except (json.JSONDecodeError, TypeError):
            return ""
    if isinstance(extra, dict):
        return str(extra.get("revision_status", "") or "")
    return ""


def _detect_frequency_changes(records: list[dict]) -> list[dict]:
    """检测同一指标历史中频率是否发生变化（7.4）。"""
    changes: list[dict] = []
    prev = None
    for r in records:
        f = r.get("frequency")
        if prev is not None and f and f != prev:
            changes.append({"date": r["date"], "from": prev, "to": f})
        if f:
            prev = f
    return changes


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

def query_chart(
    indicator: str,
    country: str = "CN",
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int | None = None,
    chart_type: str = "line",
    extra: dict | None = None,
    table: str = TABLE_CN,
) -> dict:
    """图表查询主入口。indicator 可为逗号分隔（多指标对比，最多 3 个）。

    table 指定数据表：macro_data(中国,默认) / macro_us_data(美国及商品)。
    """
    indicators = [s.strip() for s in indicator.split(",") if s.strip()]
    if not indicators:
        return {"ok": False, "error_code": ERR_INDICATOR_NOT_FOUND,
                "message": "未提供指标", "series": []}
    if len(indicators) > MAX_COMPARE:
        return {"ok": False, "error_code": None,
                "message": f"最多支持 {MAX_COMPARE} 个指标同图对比，当前 {len(indicators)} 个，建议分开展示",
                "series": []}

    # 日期格式校验（7.2）
    for d in (start_date, end_date):
        if d and not _valid_date(d):
            return {"ok": False, "error_code": ERR_DATE_FORMAT,
                    "message": "日期格式需为 YYYY-MM-DD", "series": []}
    # start > end -> 交换（7.2）
    if start_date and end_date and start_date > end_date:
        start_date, end_date = end_date, start_date
        log.warning("start_date > end_date，已自动交换")

    # 数据库为空（7.1）
    if not _db_has_data(table):
        return {"ok": False, "error_code": ERR_DB_EMPTY,
                "message": "暂无数据，请先运行数据采集任务", "series": []}

    series: list[dict] = []
    not_found: list[str] = []
    for ind in indicators:
        s = _build_series(ind, country, start_date, end_date, limit, table=table)
        if s.get("not_found"):
            not_found.append(ind)
        else:
            series.append(s)

    # 指标不存在（7.1）
    if not series:
        return {"ok": False, "error_code": ERR_INDICATOR_NOT_FOUND,
                "message": f"该指标未收录，请检查名称是否正确：{', '.join(not_found)}",
                "series": []}

    warnings = [w for s in series for w in s.get("warnings", [])]
    if not_found:
        warnings.append(f"未找到指标(已忽略)：{', '.join(not_found)}")

    title_indicators = " / ".join(f"{s['name']}({s['indicator']})" for s in series)
    return {
        "ok": True,
        "error_code": None,
        "message": None,
        "chart_type": chart_type,
        "title": f"{title_indicators}  [{country}]",
        "series": series,
        "warnings": warnings,
        "compare": len(series) > 1,
    }
