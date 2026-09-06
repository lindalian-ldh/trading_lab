"""crawler_news 阶段六单元测试（reporter.py 报告层）。

覆盖（对齐 project.md 阶段六验收标准）：
    1. ``generate_report(2026-08-01, 2026-08-17)`` 输出
       ``data/reports/news/2026-08/2026-08-01_2026-08-17_all.md``
    2. 简报含每个数据源的条数 / Top10 新闻标题列表 / 被提及最多的 5 个股票代码
    3. ``--symbol`` 过滤：仅保留 ``mentioned_codes`` 含目标代码的条目
    4. ``compute_source_stats`` / ``pick_top_news`` / ``compute_top_codes`` 纯函数
    5. ``render_markdown`` 渲染各子段（头部 / 统计表 / Top10 / 全部列表）
    6. 空数据场景：仍能产出合法 Markdown
    7. > 200 条场景：全部列表自动省略，仅输出 Top10 + 统计

设计要点：
    - **完全隔离真实数据目录**：通过 ``isolated_report_dir`` + ``isolated_raw_dir``
      fixture 把 ``reporter._REPORT_DIR`` 与 ``storage._RAW_DIR`` 都重定向到
      ``tmp_path``，避免污染真实 ``data/`` 数据
    - 用 ``save_jsonl`` 注入合成数据后调用 ``generate_report``，端到端验证
    - 不访问网络

运行方式：
    cd trading_lab && uv run pytest services/crawler_news/test_reporter.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# 让测试可以 import crawler_news 模块
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SERVICE_DIR = Path(__file__).resolve().parent

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
if str(_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICE_DIR))

import reporter
from reporter import (
    compute_source_stats,
    pick_top_news,
    compute_top_codes,
    filter_by_symbol,
    render_markdown,
    render_html,
    generate_report,
    _build_report_path,
    classify_region,
    compute_region_stats,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_raw_dir(tmp_path, monkeypatch):
    """重定向 storage._RAW_DIR 到 tmp_path 子目录。"""
    import storage as storage_module

    new_dir = tmp_path / "raw" / "news"
    new_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage_module, "_RAW_DIR", new_dir)
    yield new_dir


@pytest.fixture
def isolated_report_dir(tmp_path, monkeypatch):
    """重定向 reporter._REPORT_DIR 到 tmp_path 子目录。"""
    new_dir = tmp_path / "reports" / "news"
    new_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(reporter, "_REPORT_DIR", new_dir)
    yield new_dir


def _make_item(
    title: str,
    publish_date: str,
    source_short: str,
    publish_time: str = "10:00:00",
    mentioned_codes: list[str] | None = None,
    mentioned_names: list[str] | None = None,
) -> dict:
    """构造标准化新闻 dict（已 tagger 标注后的形态）。"""
    return {
        "source_url": f"http://example.com/{title}",
        "source_channel": "测试源",
        "source_short": source_short,
        "publish_date": publish_date,
        "publish_time": publish_time,
        "title": title,
        "content": "",
        "mentioned_codes": mentioned_codes or [],
        "mentioned_names": mentioned_names or [],
    }


@pytest.fixture
def sample_items():
    """合成 5 条跨源跨日新闻。"""
    return [
        _make_item("贵州茅台涨停", "2026-08-01", "cls",
                   mentioned_codes=["600519"], mentioned_names=["贵州茅台"]),
        _make_item("平安银行发布年报", "2026-08-05", "sina",
                   mentioned_codes=["000001"], mentioned_names=["平安银行"]),
        _make_item("贵州茅台年报超预期", "2026-08-10", "cls",
                   mentioned_codes=["600519"], mentioned_names=["贵州茅台"]),
        _make_item("宁德时代新能源大涨", "2026-08-15", "em_global",
                   mentioned_codes=["300750"], mentioned_names=["宁德时代"]),
        _make_item("贵州茅台继续走强", "2026-08-17", "cls",
                   mentioned_codes=["600519"], mentioned_names=["贵州茅台"]),
    ]


# ---------------------------------------------------------------------------
# _build_report_path 路径构造
# ---------------------------------------------------------------------------


class TestBuildReportPath:
    def test_all_symbol_path(self, isolated_report_dir):
        """symbol=None 时归档为 _all.md。"""
        path = _build_report_path("2026-08-01", "2026-08-17", symbol=None)
        assert path == (
            isolated_report_dir / "2026-08" / "2026-08-01_2026-08-17_all.md"
        )

    def test_with_symbol_path(self, isolated_report_dir):
        path = _build_report_path("2026-08-01", "2026-08-17", symbol="600519")
        assert path == (
            isolated_report_dir / "2026-08" / "2026-08-01_2026-08-17_600519.md"
        )

    def test_cross_month_uses_end_date_month(self, isolated_report_dir):
        """跨月范围时，归档月份取自 end_date。"""
        path = _build_report_path("2026-07-15", "2026-08-17", symbol=None)
        assert path.parent.name == "2026-08"

    def test_html_format_suffix(self, isolated_report_dir):
        """fmt='html' 时后缀为 .html。"""
        path = _build_report_path("2026-08-01", "2026-08-17", symbol=None, fmt="html")
        assert path.suffix == ".html"
        assert path == (
            isolated_report_dir / "2026-08" / "2026-08-01_2026-08-17_all.html"
        )

    def test_html_format_with_symbol(self, isolated_report_dir):
        """fmt='html' + symbol 时文件命名正确。"""
        path = _build_report_path(
            "2026-08-01", "2026-08-17", symbol="600519", fmt="html"
        )
        assert "2026-08-01_2026-08-17_600519.html" in path.name
        assert path.parent.name == "2026-08"

    def test_source_only_in_path(self, isolated_report_dir):
        """指定 source、无 symbol 时文件名为 _{source}.md。"""
        path = _build_report_path(
            "2026-08-01", "2026-08-17", symbol=None, source="cls"
        )
        assert path.name == "2026-08-01_2026-08-17_cls.md"

    def test_source_and_symbol_in_path(self, isolated_report_dir):
        """同时指定 source + symbol 时文件名为 _{source}_{symbol}.html。"""
        path = _build_report_path(
            "2026-08-01", "2026-08-17", symbol="600519", fmt="html", source="cls"
        )
        assert path.name == "2026-08-01_2026-08-17_cls_600519.html"

    def test_no_source_no_symbol_falls_back_to_all(self, isolated_report_dir):
        """无 source 无 symbol 时仍归档为 _all.md（兼容老格式）。"""
        path = _build_report_path(
            "2026-08-01", "2026-08-17", symbol=None, source=None
        )
        assert path.name == "2026-08-01_2026-08-17_all.md"


# ---------------------------------------------------------------------------
# compute_source_stats
# ---------------------------------------------------------------------------


class TestComputeSourceStats:
    def test_empty_items(self):
        assert compute_source_stats([]) == []

    def test_single_source(self, sample_items):
        # sample_items 中 cls 出现 3 次
        stats = compute_source_stats(sample_items)
        sources = {s["source"]: s["count"] for s in stats}
        assert sources["cls"] == 3
        assert sources["sina"] == 1
        assert sources["em_global"] == 1

    def test_sorted_by_source_name(self, sample_items):
        stats = compute_source_stats(sample_items)
        names = [s["source"] for s in stats]
        assert names == sorted(names)

    def test_missing_source_short_treated_as_unknown(self):
        items = [{"title": "a"}, {"title": "b"}]
        stats = compute_source_stats(items)
        assert stats == [{"source": "unknown", "count": 2}]


# ---------------------------------------------------------------------------
# pick_top_news
# ---------------------------------------------------------------------------


class TestPickTopNews:
    def test_returns_n_items(self, sample_items):
        result = pick_top_news(sample_items, n=3)
        assert len(result) == 3

    def test_sorted_by_date_ascending(self, sample_items):
        result = pick_top_news(sample_items, n=10)
        dates = [it["publish_date"] for it in result]
        assert dates == sorted(dates)

    def test_sorted_by_date_then_time(self):
        """同一日期不同时间应按 publish_time 升序。"""
        items = [
            _make_item("晚", "2026-08-01", "cls", publish_time="22:00:00"),
            _make_item("早", "2026-08-01", "cls", publish_time="08:00:00"),
            _make_item("中", "2026-08-01", "cls", publish_time="12:00:00"),
        ]
        result = pick_top_news(items, n=3)
        assert [it["title"] for it in result] == ["早", "中", "晚"]

    def test_more_items_than_n(self):
        items = [
            _make_item(f"标题{i}", "2026-08-01", "cls") for i in range(20)
        ]
        assert len(pick_top_news(items, n=5)) == 5

    def test_n_zero_returns_empty(self, sample_items):
        assert pick_top_news(sample_items, n=0) == []


# ---------------------------------------------------------------------------
# compute_top_codes
# ---------------------------------------------------------------------------


class TestComputeTopCodes:
    def test_empty_items(self):
        assert compute_top_codes([]) == []

    def test_counts_code_mentions(self, sample_items):
        """600519 在 sample_items 出现 3 次。"""
        top = compute_top_codes(sample_items, n=5)
        codes = {t["code"]: t["count"] for t in top}
        assert codes["600519"] == 3
        assert codes["000001"] == 1
        assert codes["300750"] == 1

    def test_sorted_by_count_descending(self, sample_items):
        top = compute_top_codes(sample_items, n=5)
        counts = [t["count"] for t in top]
        assert counts == sorted(counts, reverse=True)

    def test_limits_to_n(self):
        items = [
            _make_item(
                f"新闻{i}",
                "2026-08-01",
                "cls",
                mentioned_codes=[f"{i:06d}"],
                mentioned_names=[f"股票{i}"],
            )
            for i in range(10)
        ]
        top = compute_top_codes(items, n=5)
        assert len(top) == 5

    def test_includes_name_mapping(self, sample_items):
        top = compute_top_codes(sample_items, n=5)
        by_code = {t["code"]: t["name"] for t in top}
        assert by_code["600519"] == "贵州茅台"
        assert by_code["000001"] == "平安银行"

    def test_missing_name_returns_empty_string(self):
        items = [
            _make_item(
                "a", "2026-08-01", "cls",
                mentioned_codes=["600519"], mentioned_names=[],
            )
        ]
        top = compute_top_codes(items, n=5)
        assert top[0]["code"] == "600519"
        assert top[0]["name"] == ""


# ---------------------------------------------------------------------------
# filter_by_symbol
# ---------------------------------------------------------------------------


class TestFilterBySymbol:
    def test_no_symbol_returns_all(self, sample_items):
        result = filter_by_symbol(sample_items, None)
        assert result == sample_items

    def test_empty_symbol_returns_all(self, sample_items):
        result = filter_by_symbol(sample_items, "")
        assert result == sample_items

    def test_filters_by_mentioned_codes(self, sample_items):
        result = filter_by_symbol(sample_items, "600519")
        # sample_items 中 3 条含 600519
        assert len(result) == 3
        for it in result:
            assert "600519" in it["mentioned_codes"]

    def test_no_match_returns_empty(self, sample_items):
        result = filter_by_symbol(sample_items, "999999")
        assert result == []

    def test_does_not_mutate_input(self, sample_items):
        original_len = len(sample_items)
        filter_by_symbol(sample_items, "600519")
        assert len(sample_items) == original_len


# ---------------------------------------------------------------------------
# render_markdown
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 区域分类（国内 / 国外）
# ---------------------------------------------------------------------------


class TestClassifyRegion:
    def test_foreign_by_market_keyword(self):
        """标题含"美股" → foreign。"""
        assert classify_region({"title": "美股存储板块盘前均跌超5%"}) == "foreign"

    def test_foreign_by_country_name(self):
        """标题含"美国" → foreign。"""
        assert classify_region({"title": "美国通胀数据低于预期"}) == "foreign"

    def test_foreign_by_company_name(self):
        """标题含"特斯拉" → foreign。"""
        assert classify_region({"title": "特斯拉股价大涨10%"}) == "foreign"

    def test_foreign_by_commodity(self):
        """标题含"黄金" → foreign。"""
        assert classify_region({"title": "黄金突破2500美元"}) == "foreign"

    def test_foreign_by_content_keyword(self):
        """正文含国外关键词 → foreign（标题不含）。"""
        item = {
            "title": "央行召开会议",
            "content": "会议讨论了美联储加息对国内市场的影响",
        }
        assert classify_region(item) == "foreign"

    def test_domestic_no_keyword(self):
        """标题和正文都不含国外关键词 → domestic。"""
        item = {
            "title": "贵州茅台涨停",
            "content": "白酒板块集体走强",
        }
        assert classify_region(item) == "domestic"

    def test_domestic_a_share_keyword(self):
        """含 A 股/沪深等国内关键词（不在国外表中）→ domestic。"""
        assert classify_region({"title": "沪深两市成交量突破万亿"}) == "domestic"

    def test_empty_title_domestic(self):
        """空标题 → domestic（不会误判）。"""
        assert classify_region({"title": ""}) == "domestic"
        assert classify_region({}) == "domestic"

    def test_japan_keyword_foreign(self):
        assert classify_region({"title": "日本央行维持利率不变"}) == "foreign"

    def test_hong_kong_keyword_foreign(self):
        assert classify_region({"title": "港股恒生指数下跌"}) == "foreign"


class TestComputeRegionStats:
    def test_mixed_items(self):
        """混合数据：2 domestic + 1 foreign。"""
        items = [
            {"title": "贵州茅台涨停"},
            {"title": "央行降准"},
            {"title": "美股三大指数收涨"},
        ]
        stats = compute_region_stats(items)
        assert stats == [
            {"region": "国内", "count": 2},
            {"region": "国外", "count": 1},
        ]

    def test_all_domestic(self):
        items = [{"title": "A股大涨"}, {"title": "白酒板块走强"}]
        stats = compute_region_stats(items)
        assert stats[0]["count"] == 2
        assert stats[1]["count"] == 0

    def test_all_foreign(self):
        items = [{"title": "美股涨"}, {"title": "欧洲央行加息"}]
        stats = compute_region_stats(items)
        assert stats[0]["count"] == 0
        assert stats[1]["count"] == 2

    def test_empty_items(self):
        stats = compute_region_stats([])
        assert stats == [
            {"region": "国内", "count": 0},
            {"region": "国外", "count": 0},
        ]


class TestRenderMarkdown:
    def test_basic_structure(self, sample_items):
        stats = compute_source_stats(sample_items)
        top_news = pick_top_news(sample_items, n=10)
        top_codes = compute_top_codes(sample_items, n=5)
        md = render_markdown(
            "2026-08-01", "2026-08-17", None,
            sample_items, stats, top_news, top_codes,
        )
        # 头部含日期范围
        assert "# 新闻简报 2026-08-01 ~ 2026-08-17" in md
        # 各子段标题都应存在
        assert "## 数据源统计" in md
        assert "## 区域分布（国内 / 国外）" in md
        assert "## 被提及最多的 5 个股票代码" in md
        assert "## Top 10 新闻" in md
        assert "## 全部新闻列表" in md

    def test_symbol_filter_label(self, sample_items):
        stats = compute_source_stats(sample_items)
        top_news = pick_top_news(sample_items, n=10)
        top_codes = compute_top_codes(sample_items, n=5)
        md = render_markdown(
            "2026-08-01", "2026-08-17", "600519",
            sample_items, stats, top_news, top_codes,
        )
        assert "**股票过滤**: 600519" in md

    def test_all_label_when_no_symbol(self, sample_items):
        stats = compute_source_stats(sample_items)
        md = render_markdown(
            "2026-08-01", "2026-08-17", None,
            sample_items, stats, [], [],
        )
        assert "**股票过滤**: 全部" in md

    def test_source_filter_label(self, sample_items):
        """指定 source 时头部应含 **数据源过滤**: cls。"""
        stats = compute_source_stats(sample_items)
        md = render_markdown(
            "2026-08-01", "2026-08-17", None,
            sample_items, stats, [], [],
            source="cls",
        )
        assert "**数据源过滤**: cls" in md
        # 未指定 source 时为"全部"
        md2 = render_markdown(
            "2026-08-01", "2026-08-17", None,
            sample_items, stats, [], [],
        )
        assert "**数据源过滤**: 全部" in md2

    def test_source_and_symbol_both_rendered(self, sample_items):
        """同时指定 source + symbol 时两个过滤标签都应渲染。"""
        stats = compute_source_stats(sample_items)
        md = render_markdown(
            "2026-08-01", "2026-08-17", "600519",
            sample_items, stats, [], [],
            source="cls",
        )
        assert "**数据源过滤**: cls" in md
        assert "**股票过滤**: 600519" in md

    def test_region_stats_table_rendered(self, sample_items):
        """Markdown 应含区域分布表，且国内/国外条数正确。"""
        stats = compute_source_stats(sample_items)
        top_news = pick_top_news(sample_items, n=10)
        top_codes = compute_top_codes(sample_items, n=5)
        md = render_markdown(
            "2026-08-01", "2026-08-17", None,
            sample_items, stats, top_news, top_codes,
        )
        assert "| 区域 | 条数 | 占比 |" in md
        assert "| 国内 |" in md
        assert "| 国外 |" in md
        # sample_items 无国外关键词 → 5 条全国内
        assert "| 国内 | 5 |" in md
        assert "| 国外 | 0 |" in md

    def test_top_news_has_region_tag(self, sample_items):
        """Top10 每条前应有 [国内] 或 [国外] 标签。"""
        stats = compute_source_stats(sample_items)
        top_news = pick_top_news(sample_items, n=10)
        md = render_markdown(
            "2026-08-01", "2026-08-17", None,
            sample_items, stats, top_news, [],
        )
        # sample_items 全部为 domestic
        assert "[国内]" in md
        # 不应出现 [国外]（sample_items 无国外关键词）
        assert "[国外]" not in md

    def test_foreign_news_tagged_in_top_news(self):
        """含国外关键词的新闻应在 Top10 中标注 [国外]。"""
        items = [
            {"title": "贵州茅台涨停", "source_short": "cls",
             "publish_date": "2026-08-01", "publish_time": "09:00:00",
             "mentioned_codes": [], "mentioned_names": []},
            {"title": "美股三大指数收涨", "source_short": "cls",
             "publish_date": "2026-08-01", "publish_time": "10:00:00",
             "mentioned_codes": [], "mentioned_names": []},
        ]
        top_news = pick_top_news(items, n=10)
        md = render_markdown(
            "2026-08-01", "2026-08-01", None,
            items, compute_source_stats(items), top_news, [],
        )
        assert "[国内] [cls]" in md   # 贵州茅台涨停
        assert "[国外] [cls]" in md   # 美股三大指数收涨

    def test_empty_items_still_renders(self):
        md = render_markdown(
            "2026-08-01", "2026-08-17", None,
            [], [], [], [],
        )
        assert "# 新闻简报" in md
        assert "## 数据源统计" in md
        assert "_无数据_" in md
        # 无数据时 Top 段省略
        assert "## 被提及最多的 5 个股票代码" not in md
        assert "## Top 10 新闻" not in md
        assert "## 全部新闻列表" not in md

    def test_top_news_includes_source_and_date(self, sample_items):
        top_news = pick_top_news(sample_items, n=10)
        stats = compute_source_stats(sample_items)
        top_codes = compute_top_codes(sample_items, n=5)
        md = render_markdown(
            "2026-08-01", "2026-08-17", None,
            sample_items, stats, top_news, top_codes,
        )
        # Top 10 第一条应含 [cls] 标签 + 日期 + 标题
        lines = md.split("\n")
        top_section_start = lines.index("## Top 10 新闻")
        first_news = lines[top_section_start + 2]  # 跳过空行
        assert first_news.startswith("1. [国内] [cls] 2026-08-01")
        assert "贵州茅台涨停" in first_news

    def test_top_codes_table_format(self, sample_items):
        top_codes = compute_top_codes(sample_items, n=5)
        stats = compute_source_stats(sample_items)
        md = render_markdown(
            "2026-08-01", "2026-08-17", None,
            sample_items, stats, [], top_codes,
        )
        # 表头
        assert "| 代码 | 名称 | 提及次数 |" in md
        # 600519 应出现 3 次
        assert "| 600519 | 贵州茅台 | 3 |" in md

    def test_full_list_limited_to_200(self):
        """超过 200 条时全部列表段省略条目，仅显示提示。"""
        items = [
            _make_item(f"标题{i}", "2026-08-01", "cls") for i in range(201)
        ]
        stats = compute_source_stats(items)
        top_news = pick_top_news(items, n=10)
        top_codes = compute_top_codes(items, n=5)
        md = render_markdown(
            "2026-08-01", "2026-08-17", None,
            items, stats, top_news, top_codes,
        )
        # 应含省略提示
        assert "## 全部新闻列表（201 条，超出 200 上限" in md
        # 不应含具体的 - [cls] 列表项
        assert "- [cls] 2026-08-01 10:00:00 | 标题0" not in md

    def test_full_list_rendered_when_within_limit(self, sample_items):
        """<= 200 条时全部列表段应渲染具体条目。"""
        stats = compute_source_stats(sample_items)
        md = render_markdown(
            "2026-08-01", "2026-08-17", None,
            sample_items, stats, [], [],
        )
        assert "- [cls] 2026-08-01 10:00:00 | 贵州茅台涨停" in md

    def test_full_list_split_by_region(self):
        """全部新闻列表应按国内/国外分组展示，含 ### 子标题。"""
        items = [
            {"title": "贵州茅台涨停", "source_short": "cls",
             "publish_date": "2026-08-01", "publish_time": "09:00:00",
             "mentioned_codes": [], "mentioned_names": []},
            {"title": "美股大涨", "source_short": "cls",
             "publish_date": "2026-08-01", "publish_time": "10:00:00",
             "mentioned_codes": [], "mentioned_names": []},
            {"title": "央行降准", "source_short": "sina",
             "publish_date": "2026-08-01", "publish_time": "11:00:00",
             "mentioned_codes": [], "mentioned_names": []},
        ]
        md = render_markdown(
            "2026-08-01", "2026-08-01", None,
            items, compute_source_stats(items), [], [],
        )
        # 应含国内/国外两个子段标题
        assert "### 国内新闻（2 条）" in md
        assert "### 国外新闻（1 条）" in md
        # 国内段应含贵州茅台涨停、央行降准
        domestic_section = md.split("### 国外新闻")[0]
        assert "贵州茅台涨停" in domestic_section
        assert "央行降准" in domestic_section
        # 国外段应含美股大涨
        foreign_section = md.split("### 国外新闻")[1]
        assert "美股大涨" in foreign_section

    def test_pipe_in_title_escaped(self):
        """标题中的 | 应被转义为 \\|，避免破坏 Markdown 表格。"""
        items = [_make_item("标题|含管道符", "2026-08-01", "cls")]
        stats = compute_source_stats(items)
        md = render_markdown(
            "2026-08-01", "2026-08-17", None,
            items, stats, [], [],
        )
        assert "\\|" in md
        assert "标题|含管道符" not in md  # 应已被转义

    def test_markdown_ends_with_newline(self, sample_items):
        stats = compute_source_stats(sample_items)
        md = render_markdown(
            "2026-08-01", "2026-08-17", None,
            sample_items, stats, [], [],
        )
        assert md.endswith("\n")


# ---------------------------------------------------------------------------
# HTML 渲染（render_html：markdown 库 + 样式模板）
# ---------------------------------------------------------------------------


class TestRenderHtml:
    def test_html_doctype_and_structure(self, sample_items):
        """HTML 输出应为完整 HTML5 文档：DOCTYPE + html/head/body/article。"""
        stats = compute_source_stats(sample_items)
        top_news = pick_top_news(sample_items, n=10)
        top_codes = compute_top_codes(sample_items, n=5)
        html = render_html(
            "2026-08-01", "2026-08-17", None,
            sample_items, stats, top_news, top_codes,
        )
        assert html.startswith("<!DOCTYPE html>")
        assert "<html lang=\"zh-CN\">" in html
        assert "<head>" in html and "</head>" in html
        assert "<body>" in html and "</body>" in html
        assert "<article class=\"news-report\">" in html
        assert '<meta charset="UTF-8">' in html

    def test_html_title_contains_date_range(self, sample_items):
        """<title> 标签应含日期范围（便于浏览器标签页识别）。"""
        stats = compute_source_stats(sample_items)
        html = render_html(
            "2026-08-01", "2026-08-17", None,
            sample_items, stats, [], [],
        )
        assert "<title>新闻简报 2026-08-01 ~ 2026-08-17</title>" in html

    def test_html_tables_extension_renders_source_stats(self, sample_items):
        """tables 扩展应把 Markdown 表格转成 <table> 标签。"""
        stats = compute_source_stats(sample_items)
        top_codes = compute_top_codes(sample_items, n=5)
        html = render_html(
            "2026-08-01", "2026-08-17", None,
            sample_items, stats, [], top_codes,
        )
        # 数据源表格
        assert "<table>" in html
        assert "<th>数据源</th>" in html and "<th>条数</th>" in html
        assert "<td>cls</td>" in html and "<td>3</td>" in html
        # Top 代码表格
        assert "<th>代码</th>" in html
        assert "<td>600519</td>" in html and "<td>贵州茅台</td>" in html

    def test_html_top10_rendered_as_ordered_list(self, sample_items):
        """Top 10 新闻列表应转为 <ol>（Markdown 的 1./2./3. 列表）。"""
        top_news = pick_top_news(sample_items, n=10)
        stats = compute_source_stats(sample_items)
        html = render_html(
            "2026-08-01", "2026-08-17", None,
            sample_items, stats, top_news, [],
        )
        assert "<ol>" in html
        # Top10 标题包含贵州茅台涨停（Markdown 中首条）
        assert "贵州茅台涨停" in html

    def test_html_stylesheet_inline_in_head(self, sample_items):
        """基础 CSS 样式应内联到 <head> 中，便于离线本地打开。"""
        html = render_html(
            "2026-08-01", "2026-08-17", None,
            sample_items, [], [], [],
        )
        # CSS 关键字段（允许多行/缩进格式）
        assert "table {" in html
        assert "border-collapse: collapse;" in html
        assert "font-family: -apple-system" in html
        # <style> 标签位置正确：必须在 <head> 块内
        head_start = html.index("<head>")
        head_end = html.index("</head>")
        style_start = html.index('<style type="text/css">')
        assert head_start < style_start < head_end

    def test_empty_items_still_renders_valid_html(self):
        """空数据时仍应产出合法 HTML（含 DOCTYPE 与结构）。"""
        html = render_html(
            "2026-08-01", "2026-08-17", None,
            [], [], [], [],
        )
        assert html.startswith("<!DOCTYPE html>")
        assert "<html lang=\"zh-CN\">" in html
        assert "<body>" in html
        # 数据源统计段降级为 <em>无数据</em>（不是表格）
        assert "<em>无数据</em>" in html

    def test_html_headers_present(self, sample_items):
        """Markdown 的 h1/h2 段落标题应转为 <h1>/<h2>。"""
        stats = compute_source_stats(sample_items)
        top_news = pick_top_news(sample_items, n=10)
        top_codes = compute_top_codes(sample_items, n=5)
        html = render_html(
            "2026-08-01", "2026-08-17", None,
            sample_items, stats, top_news, top_codes,
        )
        assert "<h1>新闻简报 2026-08-01 ~ 2026-08-17</h1>" in html
        assert "<h2>数据源统计</h2>" in html
        assert "<h2>被提及最多的 5 个股票代码</h2>" in html
        assert "<h2>Top 10 新闻</h2>" in html


# ---------------------------------------------------------------------------
# generate_report 端到端
# ---------------------------------------------------------------------------


class TestGenerateReport:
    def test_creates_report_file_at_expected_path(
        self, isolated_raw_dir, isolated_report_dir, sample_items
    ):
        """验收：generate_report 应在 data/reports/news/{YYYY-MM}/ 下产出 .md 文件。"""
        from storage import save_jsonl

        # 1. 注入合成数据到 storage
        save_jsonl(sample_items, "cls", "2026-08-01")
        save_jsonl(sample_items[1:2], "sina", "2026-08-05")

        # 2. 生成报告
        result = generate_report("2026-08-01", "2026-08-17")

        expected_path = (
            isolated_report_dir / "2026-08"
            / "2026-08-01_2026-08-17_all.md"
        )
        assert Path(result["path"]) == expected_path
        assert expected_path.exists()

    def test_returns_summary_dict(self, isolated_raw_dir, isolated_report_dir, sample_items):
        from storage import save_jsonl

        save_jsonl(sample_items, "cls", "2026-08-01")

        result = generate_report("2026-08-01", "2026-08-17")
        assert "path" in result
        assert "total_items" in result
        assert "source_stats" in result
        assert "top_codes" in result
        assert "top_news_count" in result

    def test_total_items_matches_loaded(
        self, isolated_raw_dir, isolated_report_dir, sample_items
    ):
        from storage import save_jsonl

        save_jsonl(sample_items, "cls", "2026-08-01")

        result = generate_report("2026-08-01", "2026-08-17")
        # 5 条 sample_items 全部在范围内
        assert result["total_items"] == 5

    def test_symbol_filter_applied(
        self, isolated_raw_dir, isolated_report_dir, sample_items
    ):
        from storage import save_jsonl

        save_jsonl(sample_items, "cls", "2026-08-01")

        result = generate_report("2026-08-01", "2026-08-17", symbol="600519")
        # sample_items 中 3 条含 600519
        assert result["total_items"] == 3

    def test_symbol_in_output_filename(
        self, isolated_raw_dir, isolated_report_dir, sample_items
    ):
        from storage import save_jsonl

        save_jsonl(sample_items, "cls", "2026-08-01")

        result = generate_report(
            "2026-08-01", "2026-08-17", symbol="600519"
        )
        assert "2026-08-01_2026-08-17_600519.md" in result["path"]

    def test_empty_storage_produces_empty_report(
        self, isolated_raw_dir, isolated_report_dir
    ):
        """无数据时仍应产出合法 Markdown 文件。"""
        result = generate_report("2026-08-01", "2026-08-17")
        assert result["total_items"] == 0
        assert Path(result["path"]).exists()
        # 文件内容应含 "_无数据_" 提示
        content = Path(result["path"]).read_text(encoding="utf-8")
        assert "_无数据_" in content

    def test_invalid_format_raises(
        self, isolated_raw_dir, isolated_report_dir
    ):
        with pytest.raises(ValueError, match="不支持的格式"):
            generate_report("2026-08-01", "2026-08-17", fmt="pdf")

    def test_creates_month_dir_if_missing(
        self, isolated_raw_dir, isolated_report_dir, sample_items
    ):
        from storage import save_jsonl

        save_jsonl(sample_items, "cls", "2026-08-01")

        # 报告目录的 2026-08/ 子目录初始不存在
        month_dir = isolated_report_dir / "2026-08"
        assert not month_dir.exists()

        generate_report("2026-08-01", "2026-08-17")

        assert month_dir.exists()
        assert (month_dir / "2026-08-01_2026-08-17_all.md").exists()

    def test_report_content_contains_required_sections(
        self, isolated_raw_dir, isolated_report_dir, sample_items
    ):
        """验收：报告含数据源条数 + Top10 + Top5 股票。"""
        from storage import save_jsonl

        save_jsonl(sample_items, "cls", "2026-08-01")

        result = generate_report("2026-08-01", "2026-08-17")
        content = Path(result["path"]).read_text(encoding="utf-8")

        # 数据源统计：按各条数据本身的 source_short 统计
        # sample_items 中 cls=3, sina=1, em_global=1
        assert "| cls | 3 |" in content
        assert "| sina | 1 |" in content
        assert "| em_global | 1 |" in content
        # Top 10 新闻段
        assert "## Top 10 新闻" in content
        # Top 5 股票代码段
        assert "## 被提及最多的 5 个股票代码" in content
        assert "| 600519 | 贵州茅台 | 3 |" in content  # 600519 出现 3 次

    def test_generate_html_format_creates_html_file(
        self, isolated_raw_dir, isolated_report_dir, sample_items
    ):
        """fmt='html' 时 generate_report 应产出 .html 文件。"""
        from storage import save_jsonl

        save_jsonl(sample_items, "cls", "2026-08-01")

        result = generate_report("2026-08-01", "2026-08-17", fmt="html")
        assert result["path"].endswith(".html")

        expected_path = (
            isolated_report_dir / "2026-08"
            / "2026-08-01_2026-08-17_all.html"
        )
        assert Path(result["path"]) == expected_path
        assert expected_path.exists()

    def test_generate_html_content_is_valid_html(
        self, isolated_raw_dir, isolated_report_dir, sample_items
    ):
        """fmt='html' 的产出内容应为完整 HTML5 文档（含样式、表格、列表）。"""
        from storage import save_jsonl

        save_jsonl(sample_items, "cls", "2026-08-01")

        result = generate_report("2026-08-01", "2026-08-17", fmt="html")
        content = Path(result["path"]).read_text(encoding="utf-8")

        assert content.startswith("<!DOCTYPE html>")
        assert "<html lang=\"zh-CN\">" in content
        assert "<table>" in content          # 数据源统计表
        assert "<ol>" in content              # Top 10 列表
        assert "<h2>Top 10 新闻</h2>" in content
        # 600519 被提及次数对应的表格行（HTML td 形式）
        assert "<td>600519</td>" in content
        assert "贵州茅台" in content

    def test_html_and_md_have_equivalent_information(
        self, isolated_raw_dir, isolated_report_dir, sample_items
    ):
        """md 与 html 两种格式承载相同核心信息（数量、统计、条目一致）。"""
        from storage import save_jsonl

        save_jsonl(sample_items, "cls", "2026-08-01")

        md_result = generate_report("2026-08-01", "2026-08-17", fmt="md")
        html_result = generate_report("2026-08-01", "2026-08-17", fmt="html")

        # 返回的元信息一致（total_items / source_stats / top_codes 不因格式变）
        assert md_result["total_items"] == html_result["total_items"]
        assert md_result["source_stats"] == html_result["source_stats"]
        assert md_result["top_codes"] == html_result["top_codes"]
        # md 路径与 html 路径除后缀外其他相同
        md_p = Path(md_result["path"])
        html_p = Path(html_result["path"])
        assert md_p.stem == html_p.stem
        assert md_p.parent == html_p.parent

    def test_source_filter_loads_only_that_source(
        self, isolated_raw_dir, isolated_report_dir, sample_items
    ):
        """generate_report(source='cls') 仅加载 source_short=='cls' 的条目。

        load_jsonl 既按文件名 source 过滤（加速扫描），又对每条 item 的
        ``source_short`` 字段二次过滤——即便 cls 文件中混入 sina/em_global
        条目，source='cls' 也只会返回 source_short=='cls' 的 3 条。
        """
        from storage import save_jsonl

        # 把混合 source_short 的 5 条整体写入 cls 文件
        save_jsonl(sample_items, "cls", "2026-08-01")
        # sina 文件单独写 1 条
        save_jsonl(
            [_make_item("新浪新闻", "2026-08-02", "sina")],
            "sina", "2026-08-02",
        )

        # 不过滤 source：加载 cls 文件 5 条 + sina 文件 1 条 = 6 条
        all_result = generate_report("2026-08-01", "2026-08-17")
        assert all_result["total_items"] == 6

        # 过滤 source=cls：按文件名 + source_short 二次过滤 → 3 条
        cls_result = generate_report("2026-08-01", "2026-08-17", source="cls")
        assert cls_result["total_items"] == 3
        # 路径应含 _cls 标识
        assert "_cls" in cls_result["path"]
        # source_stats 只含 cls（sina/em_global 已被 source_short 二次过滤掉）
        assert [s["source"] for s in cls_result["source_stats"]] == ["cls"]
        assert cls_result["source_stats"][0]["count"] == 3

    def test_source_filter_html_path_and_content(
        self, isolated_raw_dir, isolated_report_dir, sample_items
    ):
        """generate_report(source='cls', fmt='html') 产出 _cls.html，内容含 source 标签。"""
        from storage import save_jsonl

        save_jsonl(sample_items, "cls", "2026-08-01")

        result = generate_report(
            "2026-08-01", "2026-08-17", fmt="html", source="cls"
        )
        assert result["path"].endswith("_cls.html")

        content = Path(result["path"]).read_text(encoding="utf-8")
        assert content.startswith("<!DOCTYPE html>")
        # HTML 头部应含数据源过滤标签（markdown 库把 **x** 转为 <strong>x</strong>）
        assert "<strong>数据源过滤</strong>: cls" in content


# ---------------------------------------------------------------------------
# 跨月报告
# ---------------------------------------------------------------------------


class TestCrossMonthReport:
    def test_cross_month_loads_data_from_both_months(
        self, isolated_raw_dir, isolated_report_dir, sample_items
    ):
        """跨月报告：在 2026-07 与 2026-08 都注入数据，generate_report 应跨月加载。"""
        from storage import save_jsonl

        # 7 月数据
        jul_item = _make_item("7月新闻", "2026-07-15", "cls",
                              mentioned_codes=["600519"],
                              mentioned_names=["贵州茅台"])
        save_jsonl([jul_item], "cls", "2026-07-15")

        # 8 月数据
        aug_item = _make_item("8月新闻", "2026-08-01", "cls",
                              mentioned_codes=["000001"],
                              mentioned_names=["平安银行"])
        save_jsonl([aug_item], "cls", "2026-08-01")

        result = generate_report("2026-07-01", "2026-08-31")
        assert result["total_items"] == 2

        content = Path(result["path"]).read_text(encoding="utf-8")
        # 两条新闻都应在 Top 10 列表中
        assert "7月新闻" in content
        assert "8月新闻" in content

    def test_cross_month_report_path_uses_end_date_month(
        self, isolated_raw_dir, isolated_report_dir, sample_items
    ):
        """跨月报告归档月份取自 end_date（2026-08）。"""
        from storage import save_jsonl

        save_jsonl(sample_items, "cls", "2026-08-01")

        result = generate_report("2026-07-15", "2026-08-17")
        assert "/2026-08/" in result["path"]
        assert "2026-07-15_2026-08-17_all.md" in result["path"]
