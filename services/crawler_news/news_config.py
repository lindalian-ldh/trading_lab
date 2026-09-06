"""crawler_news 专属配置。

路径相关配置复用 ``config.settings`` 的 ``settings.news_raw_dir`` 等属性；
本模块只管数据源清单、限频参数、列名映射、正则规则等新闻采集业务参数。

注意：模块命名为 ``news_config`` 而非 ``config``，避免与项目根 ``config/`` 包
命名冲突导致 ``core.logger`` 导入失败。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# 数据源注册表：每个接口的元数据 + 列名白名单 + 标准化映射
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceConfig:
    """单个数据源的元数据与列名映射。

    字段容错约定（见 requirement.md §3.1）：
        AkShare 返回列名可能漂移，故用 ``column_alias`` 做多名匹配，
        取第一个命中的列；所有必需列缺失时返回空值，不抛异常。

    Attributes:
        source_short:   短标识，用于文件名（如 ``cls`` / ``sina``）。
        source_channel: 来源中文名，写入输出 dict 的 ``source_channel``。
        ak_func_name:   AkShare 接口函数名（字符串，用于动态调用与日志）。
        priority:       优先级数字，越小越高。
        column_alias:   标准化字段 → AkShare 可能列名列表的映射。
                        必需字段：title, content(可空), publish_date
                        可选字段：url / stock_code / stock_name
                        - ``url``：原文链接列，缺失则 ``source_url`` 留空
                        - ``stock_code`` / ``stock_name``：接口自带股票代码/名称
                          列（如巨潮的 ``代码``/``简称``），命中时直接写入
                          ``mentioned_codes``/``mentioned_names`` 跳过 tagger
        needs_date_range: 接口是否需要 start_date/end_date 形参（如巨潮）。
        needs_symbol:    接口是否需要 symbol 形参（如 stock_news_em）。
    """

    source_short: str
    source_channel: str
    ak_func_name: str
    priority: int
    column_alias: dict[str, list[str]]
    needs_date_range: bool = False
    needs_symbol: bool = False


# 财联社：stock_info_global_cls(symbol='全部') → columns: ['标题', '内容', '发布日期', '发布时间']
SOURCE_CLS = SourceConfig(
    source_short="cls",
    source_channel="财联社",
    ak_func_name="stock_info_global_cls",
    priority=1,
    column_alias={
        "title": ["标题"],
        "content": ["内容"],
        "publish_date": ["发布日期"],
        "publish_time": ["发布时间"],
    },
)

# 新浪财经-全球财经快讯：stock_info_global_sina() → columns: ['时间', '内容']
#
# 说明：requirement.md / project.md 原计划使用 ``stock_zh_a_news()`` 抓取 A 股个股
# 动态新闻，但该接口在 akshare 1.18.83 中已移除（dir(ak) 中不存在）。
# 退而求其次使用 ``stock_info_global_sina()``（新浪全球财经快讯），覆盖面与财联社
# 接近，title 仍从 content 的【】中提取。如未来 akshare 恢复 stock_zh_a_news，
# 可直接替换 ak_func_name 并调整 column_alias。
SOURCE_SINA = SourceConfig(
    source_short="sina",
    source_channel="新浪财经",
    ak_func_name="stock_info_global_sina",
    priority=1,
    column_alias={
        "time": ["时间"],
        "content": ["内容"],
    },
)

# 东方财富泛资讯：stock_info_global_em() → columns: ['标题', '摘要', '发布时间', '链接']
# 注意：content 实际列名为 "摘要" 而非 "内容"；publish_date 列名为 "发布时间"
# (含日期+时间)，URL 列为 "链接"。
SOURCE_EM_GLOBAL = SourceConfig(
    source_short="em_global",
    source_channel="东方财富",
    ak_func_name="stock_info_global_em",
    priority=2,
    column_alias={
        "title": ["标题"],
        "content": ["摘要", "内容"],
        "publish_date": ["发布时间", "发布日期"],
        "url": ["链接", "新闻链接"],
    },
)

# 东方财富个股新闻：stock_news_em(symbol) → columns:
# ['关键词', '新闻标题', '新闻内容', '发布时间', '文章来源', '新闻链接']
#
# 注意：akshare 1.18.83 内置实现存在 bug（`str.replace(r"\u3000", "", regex=True)`
# 触发 `ArrowInvalid: Invalid regular expression: invalid escape sequence: \u`），
# sources.py 中通过 ``_call_akshare_em_stock_safe`` 包装函数降级为空列表，不阻断整体。
SOURCE_EM_STOCK = SourceConfig(
    source_short="em_stock",
    source_channel="东方财富",
    ak_func_name="stock_news_em",
    priority=2,
    column_alias={
        "title": ["新闻标题", "标题"],
        "content": ["新闻内容", "内容"],
        "publish_date": ["发布时间", "发布日期", "时间"],
        "url": ["新闻链接", "链接"],
    },
    needs_symbol=True,
)

# 巨潮资讯：stock_zh_a_disclosure_report_cninfo(symbol, market, start_date, end_date)
#   → columns: ['代码', '简称', '公告标题', '公告时间', '公告链接']
# 说明：
#   - 巨潮无独立正文列，正文留空，仅保留 ``公告标题`` 作为 title。
#   - ``代码`` / ``简称`` 列直接提供该条公告关联的股票，sources.py 会自动写入
#     ``mentioned_codes`` / ``mentioned_names``，跳过 tagger 二次标注。
#   - 接口需要 start_date/end_date (YYYYMMDD) 形参；fetch_source 调用时按 target_date
#     构造 [target_date, target_date] 单日窗口。
SOURCE_JUCHAO = SourceConfig(
    source_short="juchao",
    source_channel="巨潮资讯",
    ak_func_name="stock_zh_a_disclosure_report_cninfo",
    priority=1,
    column_alias={
        "title": ["公告标题", "标题"],
        "publish_date": ["公告时间", "公告日期", "发布日期"],
        "url": ["公告链接", "链接"],
        "stock_code": ["代码"],
        "stock_name": ["简称"],
    },
    needs_date_range=True,
    needs_symbol=True,
)

# 全部可用数据源（按优先级排序）
ALL_SOURCES: list[SourceConfig] = sorted(
    [SOURCE_CLS, SOURCE_SINA, SOURCE_JUCHAO, SOURCE_EM_GLOBAL, SOURCE_EM_STOCK],
    key=lambda s: s.priority,
)

# source_short → SourceConfig 快速索引
SOURCE_MAP: dict[str, SourceConfig] = {s.source_short: s for s in ALL_SOURCES}


# ---------------------------------------------------------------------------
# 采集运行时参数
# ---------------------------------------------------------------------------


@dataclass
class CrawlConfig:
    """新闻采集运行时可调参数。"""

    # 限频保护：每个接口调用间隔 ≥ 2 秒
    rate_limit_sec: float = 2.0

    # 单接口请求超时 300 秒
    request_timeout_sec: int = 300

    # 单接口最大返回条数（防接口异常拉回过多数据）
    max_items_per_source: int = 200

    # 股票代码提取正则：6 位数字，覆盖沪市(6/5开头)、深市(0/3开头)、北交所(4/8开头)
    # 沪深校验在 tagger.py 中处理
    code_pattern: str = r"\b([036]\d{5})\b"

    # 新浪标题提取正则：从【...】中提取标题
    sina_title_pattern: str = r"【([^】]+)】"

    # 默认采集日期（None 时为昨天）
    default_date: Optional[str] = None

    # 输出格式
    output_format: str = "jsonl"

    # 巨潮 market 参数（沪深京包含沪深北交易所）
    juchao_market: str = "沪深京"


# 全局单例
crawl_config = CrawlConfig()
