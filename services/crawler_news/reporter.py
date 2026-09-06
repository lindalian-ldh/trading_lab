"""crawler_news 报告层：基于已落盘 JSON Lines 生成新闻简报。

对应 ``project.md`` 阶段六任务：
    - ``generate_report(start_date, end_date, symbol, fmt)`` 主入口
    - 按 ``source_short`` 分组、按 ``publish_date`` 升序
    - 输出 Markdown / HTML 简报（数据源统计 + Top10 + Top5 股票 + 全部列表）
    - 可选：按 ``mentioned_codes`` 过滤与个股相关的新闻

输出路径（对齐 requirement.md §4.2 报告归档）：
    ``data/reports/news/{YYYY-MM}/{start_date}_{end_date}_{symbol|all}.{md|html}``
    其中 ``YYYY-MM`` 取自 ``end_date`` 的月份。

设计要点：
    - 复用 ``storage.load_jsonl`` 加载范围数据（含跨月扫描与 publish_date 过滤）
    - ``--symbol`` 过滤：仅保留 ``mentioned_codes`` 列表含目标代码的条目
    - 全部列表上限 200 条，超过则只列 Top10 与统计（避免文件过大）
    - 顶级 Top10 按 ``publish_date`` + ``publish_time`` 升序（日期为核心索引）
    - 被提及最多的 5 个股票代码：``mentioned_codes`` 字段 Counter 统计
    - HTML 渲染：复用 Markdown 渲染产物，经 ``markdown`` 库转 HTML 后
      套入基础 CSS 样式模板（含表格、列表、h1/h2 样式，便于直接浏览器打开）
"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import Optional

try:
    from config.settings import settings

    _REPORT_DIR: Path = settings.news_reports_dir
except Exception:
    _REPORT_DIR = (
        Path(__file__).resolve().parent.parent.parent
        / "data"
        / "reports"
        / "news"
    )

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


# 全部列表上限：超过则只输出统计与 Top10，避免文件过大
_FULL_LIST_LIMIT = 200


# ---------------------------------------------------------------------------
# 区域分类（国内 / 国外）
# ---------------------------------------------------------------------------

# 国外关键词表：标题或正文命中任一关键词即标记为"国外"。
# 覆盖：海外市场指数、国家/地区名、国际组织/人物、跨国公司、国际大宗商品
_FOREIGN_KEYWORDS: set[str] = {
    # --- 海外市场指数 ---
    "美股", "纳斯达克", "纽交所", "道琼斯", "标普", "日经", "恒生", "港股",
    "韩股", "伦敦金", "富时",
    # --- 国家 / 地区 ---
    "美国", "日本", "韩国", "朝鲜", "俄罗斯", "英国", "德国", "法国",
    "欧洲", "欧盟", "中东", "台湾", "香港", "印度", "越南", "泰国",
    "新加坡", "马来西亚", "菲律宾", "印尼", "澳大利亚", "加拿大",
    "巴西", "阿根廷", "南非", "埃及", "土耳其", "以色列", "伊朗",
    "乌克兰", "沙特", "阿联酋",
    # --- 国际组织 / 人物 ---
    "美联储", "白宫", "拜登", "特朗普", "哈里斯", "耶伦", "鲍威尔",
    "欧佩克", "OPEC", "北约", "WTO", "IMF", "世界银行",
    # --- 跨国公司 ---
    "特斯拉", "苹果", "谷歌", "微软", "亚马逊", "英伟达", "Meta",
    "OpenAI", "ChatGPT", "三星", "丰田", "索尼", "台积电", "高通",
    "英特尔", "AMD", "Netflix", "奈飞",
    # --- 国际大宗商品 ---
    "原油", "黄金", "白银", "天然气", "比特币", "以太坊",
    # --- 国际事件 / 议题 ---
    "中美", "贸易战", "关税", "制裁", "台海", "南海", "俄乌", "朝核",
    "伊核", "脱欧", "加息", "降息",
}


def classify_region(item: dict) -> str:
    """根据标题和正文关键词判定新闻区域归属。

    匹配逻辑：``title`` + ``content`` 拼接后检查是否包含任一国外关键词。
    命中则返回 ``"foreign"``，否则返回 ``"domestic"``。

    Args:
        item: 单条新闻 dict，至少含 ``title`` 字段

    Returns:
        ``"foreign"`` 或 ``"domestic"``
    """
    text = (item.get("title") or "") + (item.get("content") or "")
    for kw in _FOREIGN_KEYWORDS:
        if kw in text:
            return "foreign"
    return "domestic"


def compute_region_stats(items: list[dict]) -> list[dict]:
    """统计国内 / 国外新闻条数。

    Returns:
        list of ``{"region": "国内"|"国外", "count": int}``，固定顺序：国内在前
    """
    domestic = 0
    foreign = 0
    for it in items:
        if classify_region(it) == "foreign":
            foreign += 1
        else:
            domestic += 1
    return [
        {"region": "国内", "count": domestic},
        {"region": "国外", "count": foreign},
    ]


# ---------------------------------------------------------------------------
# 路径工具
# ---------------------------------------------------------------------------


def _build_report_path(
    start_date: str,
    end_date: str,
    symbol: Optional[str],
    fmt: str = "md",
    source: Optional[str] = None,
) -> Path:
    """构造报告输出路径。

    格式：``data/reports/news/{YYYY-MM}/{start}_{end}_{tag}.{md|html}``
    其中 ``YYYY-MM`` 取自 ``end_date`` 的月份；``tag`` 按
    ``source`` / ``symbol`` 是否存在拼接（均省略时归档为 ``all``，
    兼容老格式）：

        无过滤            → ``all``
        --source cls      → ``cls``
        --symbol 600519   → ``600519``
        --source cls --symbol 600519 → ``cls_600519``

    Args:
        start_date: 起始日期 YYYY-MM-DD
        end_date:   截止日期 YYYY-MM-DD
        symbol:     股票代码，None 时省略
        fmt:        输出格式（``md`` / ``html``），决定文件后缀
        source:     数据源 short 名（``cls`` / ``sina`` / ...），None 时省略
    """
    suffix = ".html" if fmt == "html" else ".md"
    month = end_date[:7]  # "2026-08"
    tag = "_".join(p for p in (source, symbol) if p) or "all"
    return _REPORT_DIR / month / f"{start_date}_{end_date}_{tag}{suffix}"


# ---------------------------------------------------------------------------
# 统计计算（纯函数，便于单测）
# ---------------------------------------------------------------------------


def compute_source_stats(items: list[dict]) -> list[dict]:
    """按 ``source_short`` 分组统计条数，按 source 名称升序。

    Returns:
        list of ``{"source": str, "count": int}``
    """
    counter: Counter[str] = Counter(
        it.get("source_short") or "unknown" for it in items
    )
    return [
        {"source": s, "count": c} for s, c in sorted(counter.items())
    ]


def pick_top_news(items: list[dict], n: int = 10) -> list[dict]:
    """选取 Top N 新闻，按 ``publish_date`` + ``publish_time`` 升序。

    项目约定：日期为核心索引，按发布时间升序排前 N 条。
    """
    sorted_items = sorted(
        items,
        key=lambda x: (
            x.get("publish_date") or "",
            x.get("publish_time") or "",
        ),
    )
    return sorted_items[:n]


def compute_top_codes(items: list[dict], n: int = 5) -> list[dict]:
    """统计被提及最多的 N 个股票代码。

    通过 ``mentioned_codes`` 字段 Counter 统计；
    ``mentioned_names`` 字段对齐索引回填 code→name 映射。

    Returns:
        list of ``{"code": str, "name": str, "count": int}``，按 count 降序
    """
    code_counter: Counter[str] = Counter()
    name_map: dict[str, str] = {}

    for it in items:
        codes = it.get("mentioned_codes") or []
        names = it.get("mentioned_names") or []
        for c in codes:
            code_counter[c] += 1
        # 收集 code→name 映射（按位置对齐；空 name 跳过）
        for i, c in enumerate(codes):
            if i < len(names) and names[i]:
                name_map[c] = names[i]

    top = code_counter.most_common(n)
    return [
        {"code": c, "name": name_map.get(c, ""), "count": cnt}
        for c, cnt in top
    ]


def filter_by_symbol(items: list[dict], symbol: Optional[str]) -> list[dict]:
    """按 ``--symbol`` 过滤：仅保留 ``mentioned_codes`` 列表含目标代码的条目。

    Args:
        items:  原始新闻列表
        symbol: 目标股票代码；None 或空字符串 = 不过滤

    Returns:
        过滤后的列表
    """
    if not symbol:
        return items
    return [
        it
        for it in items
        if symbol in (it.get("mentioned_codes") or [])
    ]


# ---------------------------------------------------------------------------
# Markdown 渲染
# ---------------------------------------------------------------------------


def _render_section_header(
    start_date: str,
    end_date: str,
    symbol: Optional[str],
    total: int,
    source: Optional[str] = None,
) -> list[str]:
    """渲染报告头部 + 元信息。"""
    sym_label = symbol or "全部"
    src_label = source or "全部"
    return [
        f"# 新闻简报 {start_date} ~ {end_date}",
        "",
        f"**数据源过滤**: {src_label}",
        f"**股票过滤**: {sym_label}",
        f"**总条数**: {total}",
        "",
    ]


def _render_source_stats(stats: list[dict]) -> list[str]:
    """渲染数据源统计表格。"""
    if not stats:
        return ["## 数据源统计", "", "_无数据_", ""]
    lines = [
        "## 数据源统计",
        "",
        "| 数据源 | 条数 |",
        "|--------|------|",
    ]
    for s in stats:
        lines.append(f"| {s['source']} | {s['count']} |")
    lines.append("")
    return lines


def _render_region_stats(stats: list[dict]) -> list[str]:
    """渲染国内 / 国外区域分布表格。"""
    if not stats:
        return []
    total = sum(s["count"] for s in stats)
    if total == 0:
        return []
    lines = [
        "## 区域分布（国内 / 国外）",
        "",
        "| 区域 | 条数 | 占比 |",
        "|------|------|------|",
    ]
    for s in stats:
        pct = f"{s['count'] / total * 100:.1f}%" if total else "0%"
        lines.append(f"| {s['region']} | {s['count']} | {pct} |")
    lines.append("")
    return lines


def _render_top_codes(top_codes: list[dict]) -> list[str]:
    """渲染被提及最多的 5 个股票代码表格。"""
    if not top_codes:
        return []
    return [
        "## 被提及最多的 5 个股票代码",
        "",
        "| 代码 | 名称 | 提及次数 |",
        "|------|------|----------|",
        *[
            f"| {c['code']} | {c['name']} | {c['count']} |"
            for c in top_codes
        ],
        "",
    ]


def _render_top_news(top_news: list[dict]) -> list[str]:
    """渲染 Top 10 新闻列表（每条标注 [国内]/[国外] 区域标签）。"""
    if not top_news:
        return []
    lines = ["## Top 10 新闻", ""]
    for i, it in enumerate(top_news, 1):
        src = it.get("source_short") or "?"
        d = it.get("publish_date") or ""
        t = it.get("publish_time") or ""
        title = it.get("title") or ""
        # 标题中的 | 转义，避免破坏 Markdown 表格（列表形式不严格需要，但保险）
        safe_title = title.replace("|", "\\|")
        region_tag = "[国外]" if classify_region(it) == "foreign" else "[国内]"
        lines.append(f"{i}. {region_tag} [{src}] {d} {t} | {safe_title}")
    lines.append("")
    return lines


def _render_full_list(items: list[dict]) -> list[str]:
    """渲染全部新闻列表（上限 ``_FULL_LIST_LIMIT`` 条），按国内/国外分组展示。"""
    if not items:
        return []
    if len(items) > _FULL_LIST_LIMIT:
        return [
            f"## 全部新闻列表（{len(items)} 条，超出 {_FULL_LIST_LIMIT} 上限，仅展示 Top 10）",
            "",
        ]

    domestic = [it for it in items if classify_region(it) == "domestic"]
    foreign = [it for it in items if classify_region(it) == "foreign"]
    total = len(items)
    lines = [f"## 全部新闻列表（{total} 条）", ""]

    # --- 国内 ---
    lines.append(f"### 国内新闻（{len(domestic)} 条）")
    lines.append("")
    for it in domestic:
        lines.append(_format_news_line(it))
    lines.append("")

    # --- 国外 ---
    lines.append(f"### 国外新闻（{len(foreign)} 条）")
    lines.append("")
    for it in foreign:
        lines.append(_format_news_line(it))
    lines.append("")

    return lines


def _format_news_line(it: dict) -> str:
    """格式化单条新闻为列表行：``- [src] date time | title``。"""
    src = it.get("source_short") or "?"
    d = it.get("publish_date") or ""
    t = it.get("publish_time") or ""
    title = (it.get("title") or "").replace("|", "\\|")
    return f"- [{src}] {d} {t} | {title}"


def render_markdown(
    start_date: str,
    end_date: str,
    symbol: Optional[str],
    items: list[dict],
    source_stats: list[dict],
    top_news: list[dict],
    top_codes: list[dict],
    source: Optional[str] = None,
) -> str:
    """渲染完整 Markdown 简报。

    各子段顺序：
        1. 头部（标题 + 数据源过滤 + 股票过滤 + 总条数）
        2. 数据源统计表
        3. 区域分布（国内 / 国外）表
        4. 被提及最多的 5 个股票代码表
        5. Top 10 新闻列表（每条标注 [国内]/[国外]）
        6. 全部新闻列表（>200 条时省略）
    """
    region_stats = compute_region_stats(items)
    lines: list[str] = []
    lines += _render_section_header(
        start_date, end_date, symbol, len(items), source=source
    )
    lines += _render_source_stats(source_stats)
    lines += _render_region_stats(region_stats)
    lines += _render_top_codes(top_codes)
    lines += _render_top_news(top_news)
    lines += _render_full_list(items)
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# HTML 渲染（方案 A：markdown 库 + 基础样式模板）
# ---------------------------------------------------------------------------


# 基础 CSS 样式：便于浏览器直接打开 .html 文件时有可读的排版
# 范围限定到 .news-report 容器内，避免污染其他页面
_HTML_STYLES = """
    body {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                     "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
        line-height: 1.6;
        max-width: 960px;
        margin: 24px auto;
        padding: 0 24px;
        color: #222;
    }
    h1 { border-bottom: 2px solid #e5e7eb; padding-bottom: 8px; }
    h2 { margin-top: 28px; color: #1f2937; }
    table {
        border-collapse: collapse;
        margin: 12px 0 20px 0;
        width: 100%;
    }
    th, td {
        border: 1px solid #d1d5db;
        padding: 6px 12px;
        text-align: left;
    }
    th { background: #f3f4f6; }
    ol, ul { padding-left: 24px; }
    li { margin: 4px 0; }
    .meta { color: #6b7280; }
    code { background: #f3f4f6; padding: 1px 4px; border-radius: 3px; }
"""


def _html_shell(title: str, body_html: str) -> str:
    """把 ``body_html`` 片段包进完整 HTML5 文档（charset UTF-8 + 基础样式）。"""
    # 简单但规范的模板：不引外链、不使用 JS，便于离线本地打开
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n'
        "<head>\n"
        '    <meta charset="UTF-8">\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f"    <title>{title}</title>\n"
        f'    <style type="text/css">{_HTML_STYLES}</style>\n'
        "</head>\n"
        "<body>\n"
        f"    <article class=\"news-report\">{body_html}</article>\n"
        "</body>\n"
        "</html>\n"
    )


def render_html(
    start_date: str,
    end_date: str,
    symbol: Optional[str],
    items: list[dict],
    source_stats: list[dict],
    top_news: list[dict],
    top_codes: list[dict],
    source: Optional[str] = None,
) -> str:
    """渲染完整 HTML 简报：先出 Markdown → 转 HTML → 套样式模板。

    依赖 ``markdown`` 库（tables 扩展使 ``| a | b |`` 语法能正确渲染为
    ``<table>``；fenced_code 扩展对本报告无作用但保留以提升通用性）。

    若 ``markdown`` 库不可用（缺失依赖），则降级为把 Markdown 原文包进
    ``<pre>`` 标签，仍产出合法 HTML 但不渲染样式。
    """
    md_text = render_markdown(
        start_date, end_date, symbol, items, source_stats, top_news, top_codes,
        source=source,
    )

    try:
        import markdown as _md

        body_fragment = _md.markdown(
            md_text, extensions=["tables", "fenced_code"]
        )
    except Exception:  # noqa: BLE001 - markdown 库可能缺失或解析异常
        # 降级：用 <pre> 保留原 Markdown 文本渲染
        import html as _html_std

        escaped = _html_std.escape(md_text)
        body_fragment = f"<pre style=\"white-space: pre-wrap;\">{escaped}</pre>"

    doc_title = f"新闻简报 {start_date} ~ {end_date}"
    return _html_shell(doc_title, body_fragment)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def generate_report(
    start_date: str,
    end_date: str,
    symbol: Optional[str] = None,
    fmt: str = "md",
    source: Optional[str] = None,
) -> dict:
    """生成新闻简报并落盘。

    流程：
        1. ``storage.load_jsonl(start_date, end_date, source=...)`` 加载范围数据
           （内部含跨月扫描与 ``publish_date`` 过滤；``source`` 在文件名层就过滤）
        2. 按 ``symbol`` 过滤 ``mentioned_codes`` 列表
        3. 计算数据源统计、Top10 新闻、Top5 股票代码
        4. 按 ``fmt`` 渲染为 Markdown 或 HTML
        5. 落盘到 ``data/reports/news/{YYYY-MM}/{start}_{end}_{tag}.{md|html}``，
           其中 ``tag`` 取自 ``source`` / ``symbol``（均省略时为 ``all``）

    Args:
        start_date: 起始日期（YYYY-MM-DD，含）
        end_date:   截止日期（YYYY-MM-DD，含）
        symbol:     可选股票代码过滤
        fmt:        输出格式：``md`` 或 ``html``
        source:     可选数据源过滤（``cls`` / ``sina`` / ``juchao`` / ...）

    Returns:
        dict 含 path / total_items / source_stats / top_codes 等字段
    """
    from storage import load_jsonl

    log.info("=" * 30 + " 开始生成新闻简报 " + "=" * 30)
    log.info(
        "范围: %s ~ %s | source: %s | symbol: %s | format: %s",
        start_date,
        end_date,
        source or "all",
        symbol or "all",
        fmt,
    )

    if fmt not in ("md", "html"):
        raise ValueError(f"不支持的格式: {fmt!r}（可选 'md' 或 'html'）")

    # 1. 加载数据（source 在文件名层就过滤，加速扫描）
    items = load_jsonl(start_date, end_date, source=source)
    log.info("加载 %d 条原始数据", len(items))

    # 2. 按 symbol 过滤
    if symbol:
        before = len(items)
        items = filter_by_symbol(items, symbol)
        log.info(
            "按 symbol=%s 过滤后剩余 %d 条（过滤前 %d）",
            symbol,
            len(items),
            before,
        )

    # 3. 计算统计
    source_stats = compute_source_stats(items)
    top_news = pick_top_news(items, n=10)
    top_codes = compute_top_codes(items, n=5)

    # 4. 按格式渲染
    if fmt == "html":
        content = render_html(
            start_date, end_date, symbol, items, source_stats, top_news, top_codes,
            source=source,
        )
    else:
        content = render_markdown(
            start_date, end_date, symbol, items, source_stats, top_news, top_codes,
            source=source,
        )

    # 5. 落盘
    out_path = _build_report_path(
        start_date, end_date, symbol, fmt=fmt, source=source
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")

    log.info("报告已保存: %s", out_path)
    log.info(
        "总条数: %d | 数据源数: %d | Top 代码: %s",
        len(items),
        len(source_stats),
        [c["code"] for c in top_codes],
    )
    log.info("=" * 30 + " 报告生成完成 " + "=" * 30)

    return {
        "path": str(out_path),
        "total_items": len(items),
        "source_stats": source_stats,
        "top_codes": top_codes,
        "top_news_count": len(top_news),
    }
