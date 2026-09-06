"""数据源接口适配层：封装 AkShare 的 5 个财经资讯接口。

设计要点（对齐 requirement.md §3.1 接口调用约束）：
    - 限频保护：每个接口调用间隔 ≥ 2 秒
    - 超时保护：单个接口请求超时 300 秒
    - 字段容错：列名白名单匹配，缺失列填空值不报错
    - 失败降级：单个接口异常时记录日志并返回空列表，不阻断整体

标准化输出 dict 字段（对齐 requirement.md §2.1/2.2）：
    - news_id:        唯一标识（由 storage.py 计算，本模块不填）
    - source_url:     原文链接（接口返回的 url 列；缺失则空字符串）
    - source_channel: 来源中文名
    - source_short:   来源短标识
    - publish_date:   发布日期（YYYY-MM-DD）
    - publish_time:   发布时间（HH:MM:SS，可选）
    - title:          原文标题（一字不改）
    - content:        原文正文（一字不改；巨潮等无独立正文的接口留空）
    - mentioned_codes:  股票代码列表（接口自带 stock_code 列则直接填，否则空列表，
                       后续由 tagger.py 二次扫描 title/content 补全）
    - mentioned_names:  股票名称列表（同上）
"""

from __future__ import annotations

import logging
import re
import threading
import time
import traceback
from datetime import datetime
from typing import Optional

import pandas as pd

try:
    from .news_config import ALL_SOURCES, SOURCE_MAP, CrawlConfig, SourceConfig, crawl_config
except ImportError:
    from news_config import ALL_SOURCES, SOURCE_MAP, CrawlConfig, SourceConfig, crawl_config

# 懒加载 core.logger，避免与服务本地 config.py 命名冲突
try:
    from core.logger import get_logger
    log = get_logger(__name__)
except Exception:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 列名容错工具
# ---------------------------------------------------------------------------


def _resolve_column(df: pd.DataFrame, aliases: list[str]) -> Optional[str]:
    """在 DataFrame 中查找第一个匹配的列名。

    AkShare 不同版本可能返回不同列名（如 "发布日期" vs "日期"），
    本函数按 alias 列表顺序逐个尝试，返回第一个命中的列名；
    全部未命中返回 None。
    """
    cols = set(df.columns)
    for alias in aliases:
        if alias in cols:
            return alias
    return None


def _resolve_column_row(row: dict, aliases: list[str]) -> Optional[str]:
    """从一行 dict 中查找第一个匹配的列名（复用 _resolve_column 逻辑）。"""
    for alias in aliases:
        if alias in row:
            return alias
    return None


def _safe_str(val) -> str:
    """将单元格值转为字符串，NaN/NaT/None 转为空字符串。"""
    if val is None:
        return ""
    try:
        if isinstance(val, float) and pd.isna(val):
            return ""
        if pd.isna(val):
            return ""
    except (TypeError, ValueError):
        pass
    return str(val).strip()


def _parse_date(val) -> str:
    """将日期/时间单元格解析为 YYYY-MM-DD 格式字符串。

    支持的输入格式：
        - datetime 对象
        - "2026-08-18" / "2026-08-18 14:58:22"（字符串）
        - Timestamp / datetime64
    解析失败返回空字符串。
    """
    if val is None:
        return ""
    try:
        if isinstance(val, datetime):
            return val.strftime("%Y-%m-%d")
        if isinstance(val, pd.Timestamp):
            return val.strftime("%Y-%m-%d")
        s = str(val).strip()
        # 尝试直接截取前 10 位（覆盖 "2026-08-18 ..."）
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return s[:10]
        # 尝试 pandas 自动解析
        ts = pd.to_datetime(s, errors="coerce")
        if pd.notna(ts):
            return ts.strftime("%Y-%m-%d")
        return ""
    except Exception:
        return ""


def _parse_time(val) -> str:
    """从 publish_time 字段解析时间部分（HH:MM:SS），用于补全 publish_date。"""
    if val is None:
        return ""
    try:
        s = str(val).strip()
        # 提取 HH:MM:SS 部分
        m = re.search(r"(\d{1,2}:\d{2}:\d{2})", s)
        if m:
            return m.group(1)
        return s
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 标准化转换
# ---------------------------------------------------------------------------


def _row_to_standard(row: dict, src: SourceConfig, today: str) -> dict:
    """将一行原始数据转换为标准化 dict。

    Args:
        row: 原始行 dict（列名 → 值）
        src: 数据源配置
        today: 当前日期（YYYY-MM-DD），用于补全缺失的日期字段

    Returns:
        标准化 dict，字段见模块 docstring
    """
    aliases = src.column_alias

    # 1. 标题
    title = ""
    if "title" in aliases:
        col = _resolve_column_row(row, aliases["title"])
        if col:
            title = _safe_str(row.get(col))

    # 2. 内容
    content = ""
    if "content" in aliases:
        col = _resolve_column_row(row, aliases["content"])
        if col:
            content = _safe_str(row.get(col))

    # 3. 发布日期
    publish_date = ""
    if "publish_date" in aliases:
        col = _resolve_column_row(row, aliases["publish_date"])
        if col:
            publish_date = _parse_date(row.get(col))

    # 4. 发布时间（可选）
    publish_time = ""
    if "publish_time" in aliases:
        col = _resolve_column_row(row, aliases["publish_time"])
        if col:
            publish_time = _parse_time(row.get(col))

    # 5. 原文链接（可选）
    source_url = ""
    if "url" in aliases:
        col = _resolve_column_row(row, aliases["url"])
        if col:
            source_url = _safe_str(row.get(col))

    # 6. 接口自带股票代码 / 名称（可选，如巨潮的 "代码"/"简称" 列）
    mentioned_codes: list[str] = []
    mentioned_names: list[str] = []
    if "stock_code" in aliases:
        col = _resolve_column_row(row, aliases["stock_code"])
        if col:
            code = _safe_str(row.get(col))
            if code:
                mentioned_codes.append(code)
    if "stock_name" in aliases:
        col = _resolve_column_row(row, aliases["stock_name"])
        if col:
            name = _safe_str(row.get(col))
            if name:
                mentioned_names.append(name)

    # 新浪特殊处理：从 content 中提取【】内标题
    if src.source_short == "sina":
        if not title and content:
            m = re.search(crawl_config.sina_title_pattern, content)
            if m:
                title = m.group(1)
            else:
                # 若无【】，取前 30 字作为标题
                title = content[:30] if content else ""
        # 新浪的时间列包含完整 datetime，需拆分 date + time
        if "time" in aliases:
            col = _resolve_column_row(row, aliases["time"])
            if col:
                time_str = _safe_str(row.get(col))
                if len(time_str) >= 10:
                    publish_date = time_str[:10]
                if len(time_str) >= 19:
                    publish_time = time_str[11:19]

    # 若日期仍为空，使用 today 兜底
    if not publish_date:
        publish_date = today

    return {
        "source_url": source_url,
        "source_channel": src.source_channel,
        "source_short": src.source_short,
        "publish_date": publish_date,
        "publish_time": publish_time,
        "title": title,
        "content": content,
        "mentioned_codes": mentioned_codes,
        "mentioned_names": mentioned_names,
    }


# ---------------------------------------------------------------------------
# 限频装饰器
# ---------------------------------------------------------------------------


_last_call_time: float = 0.0


def _rate_limit() -> None:
    """确保相邻两次接口调用间隔 ≥ rate_limit_sec。"""
    global _last_call_time
    elapsed = time.time() - _last_call_time
    if elapsed < crawl_config.rate_limit_sec:
        time.sleep(crawl_config.rate_limit_sec - elapsed)
    _last_call_time = time.time()


# ---------------------------------------------------------------------------
# 核心抓取函数
# ---------------------------------------------------------------------------


# akshare 1.18.83 的 stock_news_em 实现存在 bug：
# `temp_df["新闻内容"].str.replace(r"\u3000", "", regex=True)` 触发
# `ArrowInvalid: Invalid regular expression: invalid escape sequence: \u`。
# 此处捕获该异常并降级为空 DataFrame，避免阻断整体采集流程。
# markers 全部小写匹配（实际 akshare 错误信息用小写 "invalid escape sequence"）。
_EM_STOCK_BUG_MARKERS = ("invalid escape sequence", "arrowinvalid")


def _is_em_stock_bug(exc: Exception) -> bool:
    """判断异常是否为 akshare stock_news_em 的已知 bug（大小写不敏感）。"""
    msg = str(exc).lower()
    return any(marker in msg for marker in _EM_STOCK_BUG_MARKERS)


def _call_akshare(func_name: str, **kwargs) -> pd.DataFrame:
    """动态调用 AkShare 接口，限频 + 硬超时 + 异常捕获。

    实现要点：
        - 限频：``_rate_limit()`` 保证调用间隔 ≥ ``RATE_LIMIT_SEC``
        - 硬超时：通过 daemon 线程 + ``join(timeout)`` 强制单次调用不超过
          ``crawl_config.request_timeout_sec``（默认 300s）；超时则返回空
          DataFrame，主流程不被卡死。daemon 线程在进程退出时自动结束。
        - 异常捕获：akshare 抛出的任何异常（含网络错误）都返回空 DataFrame

    Args:
        func_name: AkShare 函数名（如 ``stock_info_global_cls``）
        **kwargs: 透传给 AkShare 接口的参数

    Returns:
        DataFrame，异常/超时返回空 DataFrame
    """
    _rate_limit()
    arg_repr = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
    log.info("调用 AkShare 接口: %s(%s)", func_name, arg_repr)

    try:
        import akshare as ak

        func = getattr(ak, func_name, None)
        if func is None:
            log.error("akshare 无此函数: %s", func_name)
            return pd.DataFrame()

        # daemon 线程 + join(timeout) 强制硬超时
        # （akshare 内部 requests 无统一超时，需外部强制）
        result_holder: list = [None]
        exception_holder: list = [None]

        def _worker() -> None:
            try:
                result_holder[0] = func(**kwargs)
            except Exception as e:  # noqa: BLE001 - 需捕获所有异常
                exception_holder[0] = e

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join(timeout=crawl_config.request_timeout_sec)

        if t.is_alive():
            # 超时：daemon 线程仍在后台运行，主流程继续
            log.error(
                "接口 %s 超时(>%ds)，返回空结果（后台线程仍在运行，进程退出时自动清理）",
                func_name,
                crawl_config.request_timeout_sec,
            )
            return pd.DataFrame()

        # 重新抛出 worker 捕获的异常（交由下方统一处理）
        if exception_holder[0] is not None:
            raise exception_holder[0]

        result = result_holder[0]

        if result is None:
            log.warning("接口 %s 返回 None，视为空结果", func_name)
            return pd.DataFrame()

        if not isinstance(result, pd.DataFrame):
            log.warning("接口 %s 返回类型异常: %s，转为 DataFrame", func_name, type(result))
            result = pd.DataFrame(result)

        log.info("接口 %s 返回 %d 条记录", func_name, len(result))
        return result

    except Exception as e:
        if _is_em_stock_bug(e):
            log.warning(
                "接口 %s 触发 akshare 已知 bug（\\u 转义异常），降级为空结果。"
                "详见 news_config.py SOURCE_EM_STOCK 注释。",
                func_name,
            )
        else:
            log.error("接口 %s 调用失败: %s\n%s", func_name, e, traceback.format_exc())
        return pd.DataFrame()


def _build_akshare_kwargs(
    src: SourceConfig, target_date: str, symbol: Optional[str]
) -> dict:
    """根据 SourceConfig 元数据构造 AkShare 接口调用参数。

    - ``needs_date_range``：巨潮接口需 start_date/end_date（YYYYMMDD 格式）
    - ``needs_symbol``：个股接口需 symbol；巨潮支持空 symbol（全市场）
    """
    kwargs: dict = {}

    if src.needs_date_range:
        # target_date "2026-08-18" → "20260818"
        compact = target_date.replace("-", "")
        kwargs["start_date"] = compact
        kwargs["end_date"] = compact
        # 巨潮 market 默认沪深京
        if src.source_short == "juchao":
            kwargs["market"] = crawl_config.juchao_market

    if src.needs_symbol:
        # stock_news_em 必须 symbol，否则默认 603777
        # 巨潮若调用方提供 symbol 则按 symbol 过滤，否则留空（全市场）
        if src.source_short == "em_stock":
            kwargs["symbol"] = symbol or "600519"
        elif src.source_short == "juchao":
            kwargs["symbol"] = symbol or ""

    return kwargs


def fetch_source(
    src: SourceConfig, target_date: str, symbol: Optional[str] = None
) -> list[dict]:
    """抓取单个数据源并转换为标准化 dict 列表。

    Args:
        src:         数据源配置
        target_date: 目标日期（YYYY-MM-DD），用于过滤与兜底
        symbol:      股票代码（仅个股相关接口使用）

    Returns:
        标准化 dict 列表，异常时返回空列表
    """
    # 1. 构造调用参数
    kwargs = _build_akshare_kwargs(src, target_date, symbol)

    # 2. 调用 AkShare 接口
    df = _call_akshare(src.ak_func_name, **kwargs)

    if df.empty:
        log.warning("数据源 %s 返回空数据", src.source_short)
        return []

    # 3. 取前 max_items_per_source 条
    df = df.head(crawl_config.max_items_per_source)

    # 4. 逐行标准化
    items: list[dict] = []
    for _, row in df.iterrows():
        row_dict = row.to_dict()
        try:
            item = _row_to_standard(row_dict, src, target_date)
            # 跳过标题和内容都为空的行
            if item["title"] or item["content"]:
                items.append(item)
        except Exception as e:
            log.warning("行数据转换失败: %s", e)
            continue

    log.info("数据源 %s 转换完成: %d 条", src.source_short, len(items))
    return items


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------


def fetch_cls(target_date: str) -> list[dict]:
    """抓取财联社快讯。"""
    return fetch_source(SOURCE_MAP["cls"], target_date)


def fetch_sina(target_date: str) -> list[dict]:
    """抓取新浪财经快讯。"""
    return fetch_source(SOURCE_MAP["sina"], target_date)


def fetch_juchao(target_date: str, symbol: Optional[str] = None) -> list[dict]:
    """抓取巨潮资讯公告。

    Args:
        target_date: 目标日期（YYYY-MM-DD）
        symbol:      股票代码；为空时抓取全市场当日公告（数据量较大）
    """
    return fetch_source(SOURCE_MAP["juchao"], target_date, symbol=symbol)


def fetch_em_global(target_date: str) -> list[dict]:
    """抓取东方财富泛财经资讯。"""
    return fetch_source(SOURCE_MAP["em_global"], target_date)


def fetch_em_stock(target_date: str, symbol: str) -> list[dict]:
    """抓取东方财富个股新闻。

    Note:
        akshare 1.18.83 的 stock_news_em 存在已知 bug（见 news_config 注释），
        触发时返回空列表，不抛异常。
    """
    return fetch_source(SOURCE_MAP["em_stock"], target_date, symbol=symbol)


def fetch_all(
    target_date: str,
    symbol: Optional[str] = None,
    source: Optional[str] = None,
) -> dict[str, list[dict]]:
    """抓取全部（或指定）数据源。

    Args:
        target_date: 目标日期
        symbol:      股票代码（透传给 em_stock / juchao 等支持个股过滤的接口）
        source:      指定单一数据源 short 标识（None = 全部）

    Returns:
        {source_short: [dict, ...]} 映射
    """
    results: dict[str, list[dict]] = {}

    sources_to_fetch = (
        [SOURCE_MAP[source]] if source and source in SOURCE_MAP else ALL_SOURCES
    )

    for src in sources_to_fetch:
        try:
            items = fetch_source(src, target_date, symbol=symbol)
            results[src.source_short] = items
        except Exception as e:
            log.error("数据源 %s 抓取异常: %s", src.source_short, e)
            results[src.source_short] = []

    total = sum(len(v) for v in results.values())
    log.info("全部数据源抓取完成: 共 %d 条", total)
    return results
