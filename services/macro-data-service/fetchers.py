"""宏观数据获取函数集（基于 akshare）。

契约
----
每个 get_{indicator}_{country}() 遵循：
  - 入参：recent_periods(int=0, 0=全部) 等可选参数，均带默认值
  - 出参：List[dict]，每个 dict 符合 models.MacroRecord schema；按日期升序排序
  - 失败：内部捕获异常、记录 JSONL 溯源日志后返回 []（不抛出，保证批量不中断）

recent_periods 语义：>0 取最近 N 条（已按日期升序排序后 tail）；=0 取全部历史。
上层 main 默认传 1，即「每个指标只取最近一条」。

扩展性
------
新增指标只需：1) 写一个 get_xxx() 调用 _fetch_series(...)；2) 注册到 REGISTRY。

注意
----
akshare 各接口返回列名随版本变化，下方 *_cols 候选列表基于常见版本，
若某接口列名不符，按实际报错调整对应候选列表即可（_fetch_series 会自动回退到首个可转数值列）。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from typing import Any, Callable

import pandas as pd

from config.settings import settings
from core.logger import get_logger
from models import MacroRecord

log = get_logger("macro.fetchers")

PROV_LOG_NAME = "macrodata-fetch.log"


# --------------------------------------------------------------------------- #
# 基础设施：akshare 调用、重试、溯源日志、日期归一
# --------------------------------------------------------------------------- #

def _call_akshare(func_name: str, **kwargs):
    """懒加载 akshare 并调用指定函数（懒加载便于单测 mock，且不强制安装 akshare 即可 import 本模块）。"""
    import akshare as ak

    fn = getattr(ak, func_name, None)
    if fn is None:
        raise AttributeError(f"akshare 无此函数: {func_name}")
    return fn(**kwargs)


def _akshare_version() -> str:
    try:
        import akshare as ak

        return getattr(ak, "__version__", "unknown")
    except Exception:
        return "unknown"


def _pdr_version() -> str:
    try:
        import pandas_datareader as pdr

        return getattr(pdr, "__version__", "unknown")
    except Exception:
        return "unknown"


def _call_fred(series_id: str, start: str, end: str) -> pd.DataFrame:
    """懒加载 pandas-datareader，从 FRED 获取单序列。

    FRED 自 2018 年起强制要求 API key；从 settings.fred_api_key 读取（.env: FRED_API_KEY）。
    同时写入环境变量以兼容旧版 pandas-datareader 读取方式。
    """
    import os

    import pandas_datareader.data as web

    key = settings.fred_api_key
    if not key:
        raise RuntimeError(
            "未配置 FRED_API_KEY：请在 .env 中设置 FRED_API_KEY"
            "（免费申请：https://fred.stlouisfed.org）"
        )
    os.environ["FRED_API_KEY"] = key
    return web.DataReader(series_id, "fred", start=start, end=end, api_key=key)


def _log_provenance(
    func: str, source: str, records: int, status: str, error: str | None = None
) -> None:
    """以 JSON Lines 追加写入溯源日志 logs/macrodata-fetch.log。"""
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    entry: dict[str, Any] = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "func": func,
        "source": source,
        "records": records,
        "status": status,
    }
    if error:
        entry["error"] = error
    with (settings.logs_dir / PROV_LOG_NAME).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _with_retry(fn: Callable, retries: int, delay: float):
    """带重试的执行包装。retries=3 表示最多尝试 3 次。"""
    last_exc: Exception | None = None
    for i in range(max(1, retries)):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - 网络层异常需统一重试
            last_exc = e
            log.warning("第 %d/%d 次尝试失败: %s", i + 1, retries, e)
            if i < retries - 1:
                time.sleep(delay)
    assert last_exc is not None
    raise last_exc


_QUARTER_MAP = {
    "一": "03-31", "二": "06-30", "三": "09-30", "四": "12-31",
    "1": "03-31", "2": "06-30", "3": "09-30", "4": "12-31",
}


def _normalize_date(val: Any) -> str | None:
    """将多种日期格式统一为 YYYY-MM-DD。无法识别返回 None。"""
    if val is None:
        return None
    if isinstance(val, float) and pd.isna(val):
        return None
    if isinstance(val, (pd.Timestamp, datetime)):
        return val.strftime("%Y-%m-%d")
    s = str(val).strip()
    if not s or s.lower() == "nan":
        return None

    # 2024年7月 / 2024年07月份
    m = re.match(r"^(\d{4})年(\d{1,2})月", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-01"
    # 2024年二季度 / 2024年第2季度 / 2024年2季度
    m = re.match(r"^(\d{4})年第?([一二三四1-4])季度", s)
    if m:
        return f"{m.group(1)}-{_QUARTER_MAP[m.group(2)]}"
    # 2024Q2 / 2024q2
    m = re.match(r"^(\d{4})[qQ]([1-4])$", s)
    if m:
        return f"{m.group(1)}-{_QUARTER_MAP[m.group(2)]}"
    # 202407
    if re.match(r"^\d{6}$", s):
        return f"{s[:4]}-{s[4:6]}-01"
    # 2024-7 / 2024-07
    m = re.match(r"^(\d{4})-(\d{1,2})$", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-01"
    # 2024-07-15 ...（取前 10 位）
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    return None


def _pick(df: pd.DataFrame, candidates: list[str], exclude: set[str] | None = None) -> str | None:
    """按候选顺序选取第一个存在的列名。"""
    exclude = exclude or set()
    for c in candidates:
        if c in df.columns and c not in exclude:
            return c
    return None


def _first_numeric_col(df: pd.DataFrame, exclude: set[str]) -> str | None:
    """回退策略：第一个可转为数值的非排除列。"""
    for c in df.columns:
        if c in exclude:
            continue
        if pd.to_numeric(df[c], errors="coerce").notna().sum() > 0:
            return c
    return None


def _safe_extra(val: Any) -> Any:
    """将扩展列单元格转为可序列化值：NaN/None -> None，数值优先，其余转字符串。"""
    if val is None:
        return None
    if isinstance(val, float) and pd.isna(val):
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        s = str(val).strip()
        return s if s and s.lower() != "nan" else None


# --------------------------------------------------------------------------- #
# 通用获取+标准化流程
# --------------------------------------------------------------------------- #

def _fetch_series(
    *,
    indicator: str,
    name: str,
    country: str,
    frequency: str,
    unit: str,
    ak_func: str,
    date_cols: list[str],
    value_cols: list[str],
    func_name: str,
    recent_periods: int = 0,
    retries: int = 3,
    retry_delay: float = 1.0,
    source: str = "akshare",
    extra_cols: list[str] | None = None,
) -> list[dict]:
    """通用单序列获取流程：调用 akshare -> 选列 -> 日期归一 -> 按日期升序 -> 取最近N条 -> 构造 MacroRecord。

    extra_cols：需额外保留进 MacroRecord.extra 的列名候选（如 预测值/前值）。
    存在则收入 extra（NaN 跳过），便于追溯预测/前值等附属信息。
    """
    src_tag = f"{source}:{ak_func}"
    try:
        df = _with_retry(lambda: _call_akshare(ak_func), retries, retry_delay)
        if df is None or len(df) == 0:
            _log_provenance(func_name, src_tag, 0, "empty")
            return []

        date_col = _pick(df, date_cols) or df.columns[0]
        value_col = _pick(df, value_cols, exclude={date_col})
        if value_col is None:
            value_col = _first_numeric_col(df, exclude={date_col})
        if value_col is None:
            _log_provenance(func_name, src_tag, 0, "no_value_column")
            log.warning("%s 未找到可用数值列，df.columns=%s", func_name, list(df.columns))
            return []

        extra_present = [
            c for c in (extra_cols or [])
            if c in df.columns and c not in (date_col, value_col)
        ]
        keep_cols = [date_col, value_col] + extra_present
        sub = df[keep_cols].copy()
        sub["__date"] = sub[date_col].map(_normalize_date)
        sub["__value"] = pd.to_numeric(sub[value_col], errors="coerce")
        sub = sub.dropna(subset=["__date", "__value"])
        sub = sub.sort_values("__date")  # 升序，确保 tail 取到最新

        if recent_periods and recent_periods > 0:
            sub = sub.tail(recent_periods)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ver = _akshare_version()
        records = []
        for _, row in sub.iterrows():
            extra = {c: _safe_extra(row[c]) for c in extra_present} if extra_present else {}
            extra = {k: v for k, v in extra.items() if v is not None}
            records.append(
                MacroRecord(
                    indicator=indicator,
                    name=name,
                    country=country,
                    frequency=frequency,
                    date=str(row["__date"]),
                    value=float(row["__value"]),
                    unit=unit,
                    source=source,
                    source_url=f"{src_tag}@{ver}",
                    fetch_time=now,
                    extra=extra,
                ).to_dict()
            )
        _log_provenance(func_name, src_tag, len(records), "success")
        log.info("%s 获取成功 %d 条", func_name, len(records))
        return records
    except Exception as e:  # noqa: BLE001 - 单指标失败不应中断整体
        log.exception("%s 获取失败: %s", func_name, e)
        _log_provenance(func_name, src_tag, 0, "error", error=str(e))
        return []


def _fetch_fred_series(
    *,
    series_id: str,
    indicator: str,
    name: str,
    country: str,
    frequency: str,
    unit: str,
    func_name: str,
    recent_periods: int = 0,
    lookback_years: int = 10,
    retries: int = 3,
    retry_delay: float = 1.0,
) -> list[dict]:
    """通用 FRED 单序列获取流程：调用 FRED -> 取序列列 -> 日期归一 -> 升序 -> 取最近N条 -> 构造 MacroRecord。

    FRED 返回 DataFrame 索引为观测日期(DatetimeIndex)，列名为 series_id。
    lookback_years 控制拉取窗口（默认近10年，兼顾性能与画图所需历史；recent_periods=0 时仍受其约束）。
    """
    src_tag = f"FRED:{series_id}"
    try:
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=365 * lookback_years + 1)).strftime("%Y-%m-%d")
        df = _with_retry(lambda: _call_fred(series_id, start, end), retries, retry_delay)
        if df is None or len(df) == 0:
            _log_provenance(func_name, src_tag, 0, "empty")
            return []

        value_col = series_id if series_id in df.columns else df.columns[0]
        sub = df[[value_col]].copy()
        sub = sub.dropna()
        sub = sub.sort_index()  # FRED 索引为日期，升序确保 tail 取到最新

        if recent_periods and recent_periods > 0:
            sub = sub.tail(recent_periods)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ver = _pdr_version()
        records: list[dict] = []
        for ts, row in sub.iterrows():
            date_str = _normalize_date(ts)
            if not date_str:
                continue
            records.append(
                MacroRecord(
                    indicator=indicator,
                    name=name,
                    country=country,
                    frequency=frequency,
                    date=date_str,
                    value=float(row[value_col]),
                    unit=unit,
                    source="FRED",
                    source_url=f"{src_tag}@{ver}",
                    fetch_time=now,
                    extra={},
                ).to_dict()
            )
        _log_provenance(func_name, src_tag, len(records), "success")
        log.info("%s 获取成功 %d 条", func_name, len(records))
        return records
    except Exception as e:  # noqa: BLE001 - 单指标失败不应中断整体
        log.exception("%s 获取失败: %s", func_name, e)
        _log_provenance(func_name, src_tag, 0, "error", error=str(e))
        return []


# --------------------------------------------------------------------------- #
# 各指标函数（每指标一个，遵循统一契约）
# --------------------------------------------------------------------------- #

def get_cpi_cn(recent_periods: int = 0) -> list[dict]:
    """中国 CPI（居民消费价格指数，同比）。"""
    return _fetch_series(
        indicator="CPI", name="居民消费价格指数", country="CN", frequency="M",
        unit="%", ak_func="macro_china_cpi", func_name="get_cpi_cn",
        date_cols=["月份", "日期"], value_cols=["全国-同比增长", "同比增长", "同比"],
        recent_periods=recent_periods,
    )


def get_ppi_cn(recent_periods: int = 0) -> list[dict]:
    """中国 PPI（工业生产者出厂价格指数，同比）。"""
    return _fetch_series(
        indicator="PPI", name="工业生产者出厂价格指数", country="CN", frequency="M",
        unit="%", ak_func="macro_china_ppi", func_name="get_ppi_cn",
        date_cols=["月份", "日期"], value_cols=["当月同比增长", "同比增长", "当月同比", "同比"],
        recent_periods=recent_periods,
    )


def get_gdp_cn(recent_periods: int = 0) -> list[dict]:
    """中国 GDP（季度同比）。akshare 列：季度 / 国内生产总值-同比增长。"""
    return _fetch_series(
        indicator="GDP", name="国内生产总值", country="CN", frequency="Q",
        unit="%", ak_func="macro_china_gdp", func_name="get_gdp_cn",
        date_cols=["季度", "报告日", "日期"],
        value_cols=["国内生产总值-同比增长", "同比增长", "GDP同比增长"],
        recent_periods=recent_periods,
    )


def get_pmi_cn(recent_periods: int = 0) -> list[dict]:
    """中国官方制造业 PMI。"""
    return _fetch_series(
        indicator="PMI", name="制造业PMI", country="CN", frequency="M",
        unit="指数", ak_func="macro_china_pmi", func_name="get_pmi_cn",
        date_cols=["月份", "报告日", "日期"], value_cols=["制造业PMI", "官方制造业PMI", "PMI"],
        recent_periods=recent_periods,
    )


def get_m2_cn(recent_periods: int = 0) -> list[dict]:
    """中国 M2 货币供应量。"""
    return _fetch_series(
        indicator="M2", name="M2货币供应量", country="CN", frequency="M",
        unit="亿元", ak_func="macro_china_money_supply", func_name="get_m2_cn",
        date_cols=["月份", "日期"], value_cols=["货币和准货币(M2)-数量(亿元)", "M2数量", "M2"],
        recent_periods=recent_periods,
    )


def get_lpr_cn(recent_periods: int = 0) -> list[dict]:
    """中国 LPR（1年期/5年期），同一日期产出两条记录：LPR1Y / LPR5Y。"""
    func_name = "get_lpr_cn"
    src_tag = "akshare:macro_china_lpr"
    try:
        df = _with_retry(lambda: _call_akshare("macro_china_lpr"), 3, 1.0)
        if df is None or len(df) == 0:
            _log_provenance(func_name, src_tag, 0, "empty")
            return []

        date_col = _pick(df, ["报告日", "日期", "时间"]) or df.columns[0]
        terms = [
            ("LPR1Y", "1年期LPR", ["1年LPR", "1年期LPR", "LPR1Y", "1年"]),
            ("LPR5Y", "5年期LPR", ["5年LPR", "5年期LPR", "LPR5Y", "5年"]),
        ]
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ver = _akshare_version()
        records: list[dict] = []
        for ind, label, candidates in terms:
            col = _pick(df, candidates, exclude={date_col})
            if not col:
                continue
            tmp = df[[date_col, col]].copy()
            tmp["__date"] = tmp[date_col].map(_normalize_date)
            tmp["__value"] = pd.to_numeric(tmp[col], errors="coerce")
            tmp = tmp.dropna(subset=["__date", "__value"])
            tmp = tmp.sort_values("__date")
            if recent_periods and recent_periods > 0:
                tmp = tmp.tail(recent_periods)
            for _, row in tmp.iterrows():
                records.append(
                    MacroRecord(
                        indicator=ind, name=f"LPR-{label}", country="CN", frequency="M",
                        date=str(row["__date"]), value=float(row["__value"]), unit="%",
                        source="akshare", source_url=f"{src_tag}@{ver}",
                        fetch_time=now, extra={"term": ind[-2:]},
                    ).to_dict()
                )
        _log_provenance(func_name, src_tag, len(records), "success")
        log.info("%s 获取成功 %d 条", func_name, len(records))
        return records
    except Exception as e:  # noqa: BLE001
        log.exception("%s 获取失败: %s", func_name, e)
        _log_provenance(func_name, src_tag, 0, "error", error=str(e))
        return []


def get_social_financing_cn(recent_periods: int = 0) -> list[dict]:
    """社会融资规模增量。"""
    return _fetch_series(
        indicator="SF", name="社会融资规模增量", country="CN", frequency="M",
        unit="亿元", ak_func="macro_china_shrzgm", func_name="get_social_financing_cn",
        date_cols=["月份", "日期"], value_cols=["社会融资规模增量", "当月新增", "当月值", "数值"],
        recent_periods=recent_periods,
    )


def get_industrial_production_cn(recent_periods: int = 0) -> list[dict]:
    """工业增加值（同比）。"""
    return _fetch_series(
        indicator="IP", name="工业增加值", country="CN", frequency="M",
        unit="%", ak_func="macro_china_gyzjz", func_name="get_industrial_production_cn",
        date_cols=["月份", "日期"], value_cols=["同比增长", "工业增加值同比增长"],
        recent_periods=recent_periods,
    )


def get_unemployment_cn(recent_periods: int = 0) -> list[dict]:
    """城镇调查失业率。"""
    return _fetch_series(
        indicator="UE", name="城镇调查失业率", country="CN", frequency="M",
        unit="%", ak_func="macro_china_urban_unemployment", func_name="get_unemployment_cn",
        date_cols=["月份", "日期"], value_cols=["城镇调查失业率", "失业率", "数值"],
        recent_periods=recent_periods,
    )


# --------------------------------------------------------------------------- #
# 美国及商品指标（存入 macro_us_data 表）
# 列名基于 akshare 1.18.x 实测：phs 返回 ['时间','前值','现值','发布日期']；
#   cons_gold/silver 返回 ['商品','日期','总库存','增持/减持','总价值']。
# --------------------------------------------------------------------------- #

# 美国及商品指标键名集合：main 据此将其写入 macro_us_data 表。
US_INDICATORS: set[str] = {
    "usa_phs", "cons_gold", "cons_silver",
    # FRED（pandas-datareader）
    "fred_gdp", "fred_gdpc1", "fred_cpiaucsl", "fred_cpilfesl",
    "fred_ppiaco", "fred_unrate", "fred_payems", "fred_icsa",
    "fred_dgs6mo", "fred_dgs1", "fred_dgs10", "fred_gs10",
    "fred_dtwexbgs", "fred_dtwexm", "fred_dtwexo", "fred_rtwexbgs",
    # 贵金属现货（akshare SGE 上海金/银基准价）
    "sge_gold", "sge_silver",
}


def get_usa_phs(recent_periods: int = 0) -> list[dict]:
    """美国未决房屋销售月率（%）。akshare 列：时间(2026年07月) / 现值 / 前值 / 发布日期。

    最新一期通常仅有前值（现值为 NaN 待发布），dropna 后自动取最近一条已发布值。
    """
    return _fetch_series(
        indicator="USA_PHS", name="美国未决房屋销售月率", country="US", frequency="M",
        unit="%", ak_func="macro_usa_phs", func_name="get_usa_phs",
        date_cols=["时间", "发布日期", "日期"],
        value_cols=["现值", "前值"],
        extra_cols=["前值", "发布日期"],
        recent_periods=recent_periods,
    )


def get_cons_gold(recent_periods: int = 0) -> list[dict]:
    """黄金库存（COMEX，日度，吨）。akshare 列：日期 / 总库存 / 增持·减持 / 总价值。"""
    return _fetch_series(
        indicator="GOLD_INV", name="黄金库存", country="US", frequency="D",
        unit="吨", ak_func="macro_cons_gold", func_name="get_cons_gold",
        date_cols=["日期"], value_cols=["总库存"],
        extra_cols=["增持/减持", "总价值"],
        recent_periods=recent_periods,
    )


def get_cons_silver(recent_periods: int = 0) -> list[dict]:
    """白银库存（COMEX，日度，吨）。akshare 列：日期 / 总库存 / 增持·减持 / 总价值。"""
    return _fetch_series(
        indicator="SILVER_INV", name="白银库存", country="US", frequency="D",
        unit="吨", ak_func="macro_cons_silver", func_name="get_cons_silver",
        date_cols=["日期"], value_cols=["总库存"],
        extra_cols=["增持/减持", "总价值"],
        recent_periods=recent_periods,
    )


# --------------------------------------------------------------------------- #
# FRED 指标（pandas-datareader，存入 macro_us_data 表）
# 数据源：圣路易斯联储 FRED（https://fred.stlouisfed.org），需配置 FRED_API_KEY。
# indicator 直接采用 FRED series_id，便于与官方代码对齐。
# --------------------------------------------------------------------------- #

def get_fred_gdp(recent_periods: int = 0) -> list[dict]:
    """美国名义 GDP（季度，十亿美元）。FRED series: GDP。"""
    return _fetch_fred_series(
        series_id="GDP", indicator="GDP", name="名义GDP", country="US", frequency="Q",
        unit="十亿美元", func_name="get_fred_gdp",
        recent_periods=recent_periods,
    )


def get_fred_gdpc1(recent_periods: int = 0) -> list[dict]:
    """美国实际 GDP（季度，十亿美元，2017年价）。FRED series: GDPC1。"""
    return _fetch_fred_series(
        series_id="GDPC1", indicator="GDPC1", name="实际GDP", country="US", frequency="Q",
        unit="十亿美元", func_name="get_fred_gdpc1",
        recent_periods=recent_periods,
    )


def get_fred_cpiaucsl(recent_periods: int = 0) -> list[dict]:
    """美国消费者价格指数 CPI（月度，1982-84=100）。FRED series: CPIAUCSL。"""
    return _fetch_fred_series(
        series_id="CPIAUCSL", indicator="CPIAUCSL", name="消费者价格指数", country="US", frequency="M",
        unit="指数", func_name="get_fred_cpiaucsl",
        recent_periods=recent_periods,
    )


def get_fred_cpilfesl(recent_periods: int = 0) -> list[dict]:
    """美国核心 CPI（月度，1982-84=100）。FRED series: CPILFESL。"""
    return _fetch_fred_series(
        series_id="CPILFESL", indicator="CPILFESL", name="核心CPI", country="US", frequency="M",
        unit="指数", func_name="get_fred_cpilfesl",
        recent_periods=recent_periods,
    )


def get_fred_ppiaco(recent_periods: int = 0) -> list[dict]:
    """美国生产者价格指数 PPI（月度，1982=100）。FRED series: PPIACO。"""
    return _fetch_fred_series(
        series_id="PPIACO", indicator="PPIACO", name="生产者价格指数", country="US", frequency="M",
        unit="指数", func_name="get_fred_ppiaco",
        recent_periods=recent_periods,
    )


def get_fred_unrate(recent_periods: int = 0) -> list[dict]:
    """美国失业率（月度，%）。FRED series: UNRATE。"""
    return _fetch_fred_series(
        series_id="UNRATE", indicator="UNRATE", name="失业率", country="US", frequency="M",
        unit="%", func_name="get_fred_unrate",
        recent_periods=recent_periods,
    )


def get_fred_payems(recent_periods: int = 0) -> list[dict]:
    """美国非农就业人数（月度，千人）。FRED series: PAYEMS。"""
    return _fetch_fred_series(
        series_id="PAYEMS", indicator="PAYEMS", name="非农就业人数", country="US", frequency="M",
        unit="千人", func_name="get_fred_payems",
        recent_periods=recent_periods,
    )


def get_fred_icsa(recent_periods: int = 0) -> list[dict]:
    """美国初请失业金人数（周度，人）。FRED series: ICSA。"""
    return _fetch_fred_series(
        series_id="ICSA", indicator="ICSA", name="初请失业金人数", country="US", frequency="W",
        unit="人", func_name="get_fred_icsa",
        recent_periods=recent_periods,
    )


def get_fred_dgs6mo(recent_periods: int = 0) -> list[dict]:
    """美国6个月期国债收益率（日度，%）。FRED series: DGS6MO。"""
    return _fetch_fred_series(
        series_id="DGS6MO", indicator="DGS6MO", name="6个月期国债收益率", country="US", frequency="D",
        unit="%", func_name="get_fred_dgs6mo",
        recent_periods=recent_periods,
    )


def get_fred_dgs1(recent_periods: int = 0) -> list[dict]:
    """美国1年期国债收益率（日度，%）。FRED series: DGS1。"""
    return _fetch_fred_series(
        series_id="DGS1", indicator="DGS1", name="1年期国债收益率", country="US", frequency="D",
        unit="%", func_name="get_fred_dgs1",
        recent_periods=recent_periods,
    )


def get_fred_dgs10(recent_periods: int = 0) -> list[dict]:
    """美国10年期国债收益率（日度，%）。FRED series: DGS10。"""
    return _fetch_fred_series(
        series_id="DGS10", indicator="DGS10", name="10年期国债收益率", country="US", frequency="D",
        unit="%", func_name="get_fred_dgs10",
        recent_periods=recent_periods,
    )


def get_fred_gs10(recent_periods: int = 0) -> list[dict]:
    """美国10年期国债收益率（月度，%）。FRED series: GS10。

    与 DGS10（日度）口径一致、同为 10 年期国债恒定到期收益率，但为月频，
    适合与月频指标（如 CPIAUCSL/UNRATE）同图对比，避免日频与月频 X 轴点数悬殊错位。
    """
    return _fetch_fred_series(
        series_id="GS10", indicator="GS10", name="10年期国债收益率", country="US", frequency="M",
        unit="%", func_name="get_fred_gs10",
        recent_periods=recent_periods,
    )


def get_fred_dtwexbgs(recent_periods: int = 0) -> list[dict]:
    """名义广义美元指数（日度，指数 Jan 2006=100）。FRED series: DTWEXBGS。"""
    return _fetch_fred_series(
        series_id="DTWEXBGS", indicator="DTWEXBGS", name="名义广义美元指数", country="US", frequency="D",
        unit="指数", func_name="get_fred_dtwexbgs",
        recent_periods=recent_periods,
    )


def get_fred_dtwexm(recent_periods: int = 0) -> list[dict]:
    """名义主要货币美元指数（日度，指数 Mar 1973=100，已停止更新）。FRED series: DTWEXM。"""
    return _fetch_fred_series(
        series_id="DTWEXM", indicator="DTWEXM", name="名义主要货币美元指数(已停止)", country="US", frequency="D",
        unit="指数", func_name="get_fred_dtwexm",
        recent_periods=recent_periods,
    )


def get_fred_dtwexo(recent_periods: int = 0) -> list[dict]:
    """名义其他重要贸易伙伴美元指数（日度，指数 Jan 1997=100，已停止更新）。FRED series: DTWEXO。"""
    return _fetch_fred_series(
        series_id="DTWEXO", indicator="DTWEXO", name="名义其他重要贸易伙伴美元指数(已停止)", country="US", frequency="D",
        unit="指数", func_name="get_fred_dtwexo",
        recent_periods=recent_periods,
    )


def get_fred_rtwexbgs(recent_periods: int = 0) -> list[dict]:
    """实际广义美元指数（月度，指数 Jan 2006=100，通胀调整）。FRED series: RTWEXBGS。"""
    return _fetch_fred_series(
        series_id="RTWEXBGS", indicator="RTWEXBGS", name="实际广义美元指数", country="US", frequency="M",
        unit="指数", func_name="get_fred_rtwexbgs",
        recent_periods=recent_periods,
    )


# --------------------------------------------------------------------------- #
# 贵金属现货（上海黄金交易所 SGE 基准价，日度，CNY 计价）
# 说明：原 sina 伦敦金/银(hf_XAU/hf_XAG)实时接口与 FRED 伦敦金定盘价
#   (GOLDPMGBD228NLBM，已被 FRED API 永久下线) 均不可用，改用 akshare 的
#   SGE 上海金/银基准价。单位：黄金 元/克，白银 元/千克。
#   与 GOLD_INV/SILVER_INV(COMEX 库存，吨) 区分：本组为价格，彼组为库存量。
# --------------------------------------------------------------------------- #

def get_sge_gold(recent_periods: int = 0) -> list[dict]:
    """黄金现货（上海金基准价，日度，元/克）。akshare: spot_golden_benchmark_sge。

    取「晚盘价」(14:15 定盘) 作为当日基准；早盘价(10:15)亦返回但此处仅取晚盘。
    """
    return _fetch_series(
        indicator="GOLD", name="黄金现货(上海金基准价)", country="US", frequency="D",
        unit="元/克", ak_func="spot_golden_benchmark_sge", func_name="get_sge_gold",
        date_cols=["交易时间", "日期"], value_cols=["晚盘价", "早盘价"],
        recent_periods=recent_periods,
    )


def get_sge_silver(recent_periods: int = 0) -> list[dict]:
    """白银现货（上海银基准价，日度，元/千克）。akshare: spot_silver_benchmark_sge。

    取「晚盘价」作为当日基准；早盘价亦返回但此处仅取晚盘。
    """
    return _fetch_series(
        indicator="SILVER", name="白银现货(上海银基准价)", country="US", frequency="D",
        unit="元/千克", ak_func="spot_silver_benchmark_sge", func_name="get_sge_silver",
        date_cols=["交易时间", "日期"], value_cols=["晚盘价", "早盘价"],
        recent_periods=recent_periods,
    )


# 指标注册表：键名 <-> 获取函数。新增指标只需在此登记。
REGISTRY: dict[str, Callable[..., list[dict]]] = {
    # 中国宏观（macro_data 表）
    "cpi_cn": get_cpi_cn,
    "ppi_cn": get_ppi_cn,
    "gdp_cn": get_gdp_cn,
    "pmi_cn": get_pmi_cn,
    "m2_cn": get_m2_cn,
    "lpr_cn": get_lpr_cn,
    "social_financing_cn": get_social_financing_cn,
    "industrial_production_cn": get_industrial_production_cn,
    "unemployment_cn": get_unemployment_cn,
    # 美国及商品（macro_us_data 表，键名见 US_INDICATORS）
    "usa_phs": get_usa_phs,
    "cons_gold": get_cons_gold,
    "cons_silver": get_cons_silver,
    # FRED（pandas-datareader）
    "fred_gdp": get_fred_gdp,
    "fred_gdpc1": get_fred_gdpc1,
    "fred_cpiaucsl": get_fred_cpiaucsl,
    "fred_cpilfesl": get_fred_cpilfesl,
    "fred_ppiaco": get_fred_ppiaco,
    "fred_unrate": get_fred_unrate,
    "fred_payems": get_fred_payems,
    "fred_icsa": get_fred_icsa,
    "fred_dgs6mo": get_fred_dgs6mo,
    "fred_dgs1": get_fred_dgs1,
    "fred_dgs10": get_fred_dgs10,
    "fred_gs10": get_fred_gs10,
    "fred_dtwexbgs": get_fred_dtwexbgs,
    "fred_dtwexm": get_fred_dtwexm,
    "fred_dtwexo": get_fred_dtwexo,
    "fred_rtwexbgs": get_fred_rtwexbgs,
    # 贵金属现货（akshare SGE 上海金/银基准价）
    "sge_gold": get_sge_gold,
    "sge_silver": get_sge_silver,
}
