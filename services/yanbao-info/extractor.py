"""yanbao-info 信息提取层（基于关键词与正则规则）。

对齐用户"信息提取规则"表，每个字段独立函数 + 一个 extract_all 聚合入口:

    - extract_rating          评级（买入/增持/中性/减持/卖出）
    - extract_target_price    目标价
    - extract_earnings_forecast  营收/归母净利润年度预测
    - extract_core_logic      核心投资逻辑摘要
    - extract_catalysts       催化剂事件
    - extract_risks           风险提示
    - detect_first_coverage   是否首次覆盖
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import pandas as pd

try:
    from config import YanbaoConfig
except ImportError:
    from .config import YanbaoConfig

logger = logging.getLogger(__name__)


# ====================================================================
# 工具函数
# ====================================================================


def _clean_text(text: str) -> str:
    """清洗文本：去除多余空白，但保留段落结构。"""
    if not text:
        return ""
    # 合并连续空格为单个
    text = re.sub(r"[ \t]+", " ", text)
    # 合并连续换行为最多 2 个
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _find_section(text: str, headers: list, max_chars: int = 800) -> str:
    """从 text 中定位 header 关键词所在位置，返回其后 max_chars 字符的内容。

    Args:
        text: 全文
        headers: 候选标题关键词列表（如 ["投资要点", "核心逻辑"]）
        max_chars: 提取的最大字符数

    Returns:
        找到的段落文本；未找到返回空字符串
    """
    for header in headers:
        # 匹配 header 后接换行或冒号
        pattern = rf"{re.escape(header)}[\s:：]*\n*"
        m = re.search(pattern, text)
        if m:
            start = m.end()
            # 截取到下一个明显的小标题或 max_chars
            section = text[start:start + max_chars]
            # 在 section 内查找下一个标题标记（行首 + 关键词 + 冒号/换行）
            end_match = re.search(
                r"\n(投资建议|盈利预测|风险提示|催化剂|风险因素|财务预测|估值|结论|资料来源)[\s:：]",
                section,
            )
            if end_match:
                section = section[:end_match.start()]
            # 遇到表格标记（如 [Table_xxx] / [盈Ta利b预le测]）就截断
            table_match = re.search(r"\[[^\]]{5,}\]|\[.{2,30}?\].*?简\s*表", section)
            if table_match:
                section = section[:table_match.start()]
            return _clean_text(section)
    return ""


def _extract_list_items(section: str) -> list:
    """从段落文本中提取列表项。

    支持:
        - 数字编号: "1. xxx" / "1、 xxx" / "① xxx"
        - 项目符号: "• xxx" / "- xxx" / "* xxx"
        - 中文分号分隔: "xxx；yyy"
    """
    if not section:
        return []
    items = []

    # 数字编号
    numbered = re.findall(r"(?:^|\n)\s*(?:\d+[.、）]|①|②|③|④|⑤|⑥|⑦|⑧|⑨|⑩)\s*(.+?)(?=\n\s*(?:\d+[.、）]|[①-⑩])|$)",
                          section, re.DOTALL)
    if numbered:
        for it in numbered:
            it = it.strip()
            if it and len(it) > 2:
                items.append(it)

    if items:
        return items

    # 项目符号
    bulleted = re.findall(r"(?:^|\n)\s*[•\-\*]\s*(.+?)(?=\n\s*[•\-\*]|$)", section, re.DOTALL)
    if bulleted:
        for it in bulleted:
            it = it.strip()
            if it and len(it) > 2:
                items.append(it)
        return items

    # 中文分号分隔
    if "；" in section or ";" in section:
        # 先把分号间的换行替换为空格，避免跨行内容被截断
        cleaned = re.sub(r"\n(?!\s*(?:\d+[.、）]|[①-⑩]|[•\-\*]|风险| catalyst))", " ", section)
        parts = re.split(r"[；;]", cleaned)
        for p in parts:
            p = p.strip().lstrip("0123456789.、）")
            # 合并跨行残留的换行
            p = re.sub(r"\s+", " ", p).strip()
            if p and len(p) > 2:
                items.append(p)
        return items

    # 单段落，按句号切分
    sentences = re.split(r"[。\.]", section)
    for s in sentences:
        s = s.strip()
        if s and len(s) > 5:
            items.append(s)
    return items


# ====================================================================
# 1. 评级提取
# ====================================================================


def extract_rating(text: str, cfg: YanbaoConfig) -> str:
    """搜索"评级"/"投资评级"等关键词，提取附近的评级词汇。

    匹配评级词表（cfg.rating_*），返回标准化评级（买入/增持/中性/减持/卖出）。
    未找到返回空字符串。

    策略:
        1) 在评级关键词（"投资评级"/"评级"/"投资建议"/"本次评级"）前后 80 字符窗口内查找评级词
        2) 全文兜底：直接搜索评级词（按优先级顺序，取第一个命中）
    """
    if not text:
        return ""

    rating_groups = [
        ("买入", cfg.rating_buy),
        ("增持", cfg.rating_outperform),
        ("中性", cfg.rating_neutral),
        ("减持", cfg.rating_underperform),
        ("卖出", cfg.rating_sell),
    ]

    # 1. 在评级关键词前后窗口内查找
    rating_keywords = ["投资评级", "本次评级", "投资建议", "评级"]
    window_size = 80  # 关键词前后各 80 字符

    for kw in rating_keywords:
        for m in re.finditer(re.escape(kw), text):
            start = max(0, m.start() - window_size)
            end = min(len(text), m.end() + window_size)
            window = text[start:end]
            for canonical, words in rating_groups:
                for w in words:
                    if w in window:
                        logger.info("评级提取命中（关键词 %s 附近）: %s", kw, w)
                        return canonical

    # 2. 全文兜底：按优先级顺序匹配评级词
    # 用简单 in 匹配，避免词边界误判（如"强烈推荐"前后都是中文）
    for canonical, words in rating_groups:
        for w in words:
            if w in text:
                logger.info("评级提取命中（全文匹配）: %s", w)
                return canonical

    logger.warning("评级未提取到")
    return ""


# ====================================================================
# 2. 目标价提取
# ====================================================================


def extract_target_price(text: str) -> Optional[float]:
    """搜索"目标价"/"12个月目标价"等，提取后面的数字（如 25.60）。

    正则匹配"目标价"后 0-10 个非数字字符内的数字（支持小数与整数）。
    返回 float 或 None。
    """
    if not text:
        return None

    # 候选关键词，按优先级排序
    target_keywords = [
        "12个月目标价",
        "12 月目标价",
        "六个月目标价",
        "目标价位",
        "目标价",
        "目标股价",
    ]

    for kw in target_keywords:
        # 关键词后 0-15 个非数字字符内匹配整数（不限位数）+ 可选小数
        pattern = rf"{re.escape(kw)}[^\d]{{0,15}}(\d+(?:\.\d{{1,2}})?)"
        matches = re.findall(pattern, text)
        if matches:
            try:
                price = float(matches[-1])  # 取最后一次匹配（通常是结论部分）
                if 0 < price < 100000:
                    logger.info("目标价提取命中（关键词 %s）: %.2f", kw, price)
                    return price
            except ValueError:
                continue

    logger.warning("目标价未提取到")
    return None


# ====================================================================
# 3. 盈利预测提取（营收/归母净利润）
# ====================================================================


# 营收候选关键词（按优先级）
REVENUE_HEADERS = ["营业收入", "营业总收入", "营收", "总收入"]
# 归母净利润候选关键词
NET_PROFIT_HEADERS = ["归母净利润", "归属母公司净利润", "归属母公司股东的净利润",
                      "归母净利", "净利润", "股东净利润"]


def extract_earnings_forecast(tables: list, text: str) -> dict:
    """从财务表格中定位"营业收入"/"归母净利润"行，提取对应年份（如 2024E/2025E）数值。

    优先从表格提取（pdfplumber 已提取为 DataFrame），回退到正文正则匹配。

    返回:
        {
            "营收": {"2024E": "3200亿", "2025E": "3450亿"},
            "归母净利润": {"2024E": "1480亿", "2025E": "1650亿"},
        }
    """
    result = {"营收": {}, "归母净利润": {}}

    # 1. 优先从表格提取
    for df in tables:
        if df.empty:
            continue
        # 在所有单元格中查找指标行
        for _, row in df.iterrows():
            row_str = " ".join(str(v) for v in row.values if v is not None)
            # 匹配营收
            if any(h in row_str for h in REVENUE_HEADERS):
                year_vals = _extract_year_values_from_row(row, df)
                if year_vals:
                    for k, v in year_vals.items():
                        if k not in result["营收"]:
                            result["营收"][k] = v
            # 匹配归母净利润
            if any(h in row_str for h in NET_PROFIT_HEADERS):
                year_vals = _extract_year_values_from_row(row, df)
                if year_vals:
                    for k, v in year_vals.items():
                        if k not in result["归母净利润"]:
                            result["归母净利润"][k] = v

    # 2. 回退到正文正则匹配（如表格未提取到）
    if not result["营收"]:
        result["营收"] = _extract_year_values_from_text(text, REVENUE_HEADERS)
    if not result["归母净利润"]:
        result["归母净利润"] = _extract_year_values_from_text(text, NET_PROFIT_HEADERS)

    if result["营收"] or result["归母净利润"]:
        logger.info("盈利预测提取: 营收=%s, 归母净利润=%s",
                   result["营收"], result["归母净利润"])
    else:
        logger.warning("盈利预测未提取到")

    return result


def _extract_year_values_from_row(row, df) -> dict:
    """从 DataFrame 行中提取 {年份: 值} 映射。

    约定:
        - 列名含 4 位数字（如 2024 / 2024E / 2024A） → 视为年份列
        - 单元格值保留原始字符串（含单位如"亿"）
    """
    year_vals = {}
    for col in df.columns:
        col_str = str(col)
        # 匹配 4 位年份（含 E/A 后缀）
        m = re.search(r"(20\d{2})[EA]?", col_str)
        if m:
            year = m.group(1) + ("E" if "E" in col_str or "e" in col_str else "")
            val = row.get(col)
            if val is not None:
                val_str = str(val).strip()
                if val_str and val_str != "nan":
                    year_vals[year] = val_str
    return year_vals


def _extract_year_values_from_text(text: str, headers: list) -> dict:
    """从正文中按关键词+年份模式提取数值。

    匹配形如: "营业收入 2024E 3200亿"
    """
    year_vals = {}
    for h in headers:
        # 关键词后 200 字符窗口
        pattern = rf"{re.escape(h)}[^\n]{{0,200}}"
        for m in re.finditer(pattern, text):
            window = text[m.start():m.start() + 200]
            # 匹配 2024E/2025E + 数字 + 单位
            value_matches = re.findall(
                r"(20\d{2}[EA]?)\s*[：:\s]?\s*(\d+(?:\.\d+)?)\s*([万亿]?)",
                window,
            )
            for year, val, unit in value_matches:
                if year and val:
                    year_vals[year] = f"{val}{unit}"
                    break  # 每个关键词只取第一个匹配
        if year_vals:
            break
    return year_vals


# ====================================================================
# 4. 核心投资逻辑摘要
# ====================================================================


def extract_core_logic(text: str) -> str:
    """抓取"投资要点"/"核心逻辑"/"投资逻辑"标题下的首段文字（3-5 句）。

    截取首段前 500 字符并清洗。
    """
    if not text:
        return ""

    headers = [
        "投资要点",
        "核心逻辑",
        "投资逻辑",
        "核心观点",
        "主要逻辑",
        "投资建议",
        "报告摘要",
        "摘要",
    ]

    section = _find_section(text, headers, max_chars=500)
    if not section:
        return ""

    # 过滤分析师信息噪声（证书号/邮箱/姓名行）
    # 匹配 "王鸿行 S0630522050001" / "whxing@longone.com.cn" 等
    section = re.sub(r"[^\n]*S\d{8,}[^\n]*\n?", "", section)
    section = re.sub(r"[^\n]*@[\w.]+\.[a-z]+[^\n]*\n?", "", section)
    section = re.sub(r"\n{2,}", "\n", section).strip()
    if not section:
        return ""

    # 截取前 3-5 句
    sentences = re.split(r"[。\.]", section)
    summary = "。".join(s.strip() for s in sentences[:5] if s.strip())
    if summary and not summary.endswith("。"):
        summary += "。"

    if summary:
        logger.info("核心逻辑摘要提取成功 (%d 字符)", len(summary))
    else:
        logger.warning("核心逻辑摘要未提取到")
    return summary


# ====================================================================
# 5. 催化剂事件
# ====================================================================


def extract_catalysts(text: str) -> list:
    """查找"催化剂"/"近期驱动"/"即将落地"关键词，提取后面的事件描述列表。"""
    if not text:
        return []

    headers = ["催化剂", "近期催化", "近期驱动", "即将落地", "短期催化", "驱动因素"]
    section = _find_section(text, headers, max_chars=1000)
    if not section:
        return []

    items = _extract_list_items(section)
    if items:
        logger.info("催化剂事件提取 %d 条", len(items))
    else:
        logger.warning("催化剂事件未提取到")
    return items


# ====================================================================
# 6. 风险提示
# ====================================================================


def extract_risks(text: str) -> list:
    """定位"风险提示"/"风险因素"部分，提取列表文本。"""
    if not text:
        return []

    headers = ["风险提示", "风险因素", "风险", "主要风险", "潜在风险"]
    section = _find_section(text, headers, max_chars=1000)
    if not section:
        return []

    items = _extract_list_items(section)
    if items:
        logger.info("风险提示提取 %d 条", len(items))
    else:
        logger.warning("风险提示未提取到")
    return items


# ====================================================================
# 7. 首次覆盖检测
# ====================================================================


def detect_first_coverage(title: str, text: str) -> bool:
    """检查标题/正文是否出现"首次覆盖"/"首次深度"字眼。"""
    keywords = ["首次覆盖", "首次深度", "首次给予", "首次评级", "首次点评"]
    # 标题优先
    if title:
        for kw in keywords:
            if kw in title:
                logger.info("首次覆盖检测命中（标题）: %s", kw)
                return True
    # 正文兜底（取前 2000 字符避免误判）
    if text:
        head = text[:2000]
        for kw in keywords:
            if kw in head:
                logger.info("首次覆盖检测命中（正文）: %s", kw)
                return True
    return False


# ====================================================================
# 8. 公司基本情况
# ====================================================================


def extract_company_basics(text: str) -> dict:
    """从研报正文中提取公司基本情况（关键指标 + 主营业务描述）。

    提取指标（按关键词+数值模式，兼容不同券商格式）:
        - 收盘价 / 总股本 / 流通股本 / 资产负债率
        - 市净率 / 市盈率 / 净资产收益率
        - 12个月内最高/最低价

    主营业务描述:
        - 搜索"主营业务"/"公司主营"/"业务范围"附近的内容

    返回 dict，未提取到的字段不出现。
    """
    if not text:
        return {}

    basics: dict = {}

    # 1. 关键指标（正则提取，兼容多种空白与括号格式）
    patterns = {
        "收盘价": r"收盘价\s*[:：]?\s*([\d.]+)",
        "总股本(万股)": r"总股本[（(]万股[)）]\s*([\d,]+)",
        "流通股本(万股)": r"流通.*?股.*?万股[)）]\s*([\d,]+/\d+)",
        "资产负债率": r"资产负债率[（(]%[)）]\s*([\d.]+%?)",
        "市净率(PB)": r"(?:市净率|PB)[（(]倍[)）]\s*([\d.]+)",
        "市盈率(PE)": r"(?:市盈率|PE)[（(]倍[)）]\s*([\d.]+)",
        "净资产收益率(ROE)": r"净资产收益率(?:[（(]加权[)）])?\s*[:：]?\s*([\d.]+)",
        "12个月内最高/最低价": r"12个月内最高/最低价\s*([\d.]+/[\d.]+)",
    }

    for key, pat in patterns.items():
        m = re.search(pat, text)
        if m:
            val = m.group(1).strip()
            # 去除千分位逗号（保留小数点）；对含 "/" 的值分两部分分别处理
            if "," in val:
                if "/" in val:
                    parts = val.split("/")
                    val = "/".join(p.replace(",", "") for p in parts)
                else:
                    val = val.replace(",", "")
            basics[key] = val

    # 2. 主营业务描述
    business_keywords = ["主营业务", "公司主营", "业务范围", "核心业务"]
    for kw in business_keywords:
        # 关键词后 300 字符窗口
        pattern = rf"{re.escape(kw)}[：:\s]*\n?([^\n]{{5,300}})"
        m = re.search(pattern, text)
        if m:
            desc = m.group(1).strip()
            # 截取到第一个句号
            if "。" in desc:
                desc = desc.split("。")[0] + "。"
            if len(desc) > 10:
                basics["主营业务"] = desc
                break

    if basics:
        logger.info("公司基本情况提取: %d 个字段", len(basics))
    else:
        logger.warning("公司基本情况未提取到")

    return basics


# ====================================================================
# 9. 文章标题列表
# ====================================================================


def extract_section_titles(text: str) -> list:
    """提取研报中的章节标题/要点列表。

    支持三种格式:
        1. ➢ 符号开头的要点（东海证券等格式）：提取 ➢ 后的首句
        2. 数字编号: "1. xxx" / "1、 xxx" / "(1) xxx"
        3. 中文编号: "一、 xxx" / "二、 xxx"

    每个标题截取到第一个句号/冒号/换行，长度限制 50 字符。
    去重保序。
    """
    if not text:
        return []

    titles: list = []

    # 1. ➢ 符号开头的要点（东海证券等格式）
    for m in re.finditer(r"➢\s*([^\n]+)", text):
        line = m.group(1).strip()
        # 截取到第一个句号
        for sep in ["。", "：", ":"]:
            if sep in line:
                line = line.split(sep)[0] + sep
                break
        # 长度过滤：太长的是正文段落，太短的可能是噪声
        if 3 <= len(line) <= 50:
            titles.append(line)

    # 2. 数字编号: 1. / 1、 / 1) / (1) 开头（行首或换行后）
    for m in re.finditer(
        r"(?:^|\n)\s*((?:\d+[.、)）]|[(（]\d+[)）])\s*[：:]?\s*([^\n]{2,50}))",
        text,
    ):
        title = m.group(1).strip()
        # 过滤含句号的（正文段落）
        if "。" not in title and 3 <= len(title) <= 50:
            titles.append(title)

    # 3. 中文编号: 一、 / 二、 / 三、 开头
    for m in re.finditer(
        r"(?:^|\n)\s*([一二三四五六七八九十]+、\s*[^\n]{2,50})",
        text,
    ):
        title = m.group(1).strip()
        if "。" not in title and 3 <= len(title) <= 50:
            titles.append(title)

    # 去重保序 + 过滤噪声
    seen = set()
    unique = []
    # 尾部声明/邮箱/证书号过滤（标题含这些关键词即跳过）
    keyword_noise = ["评级说明", "分析师声明", "免责声明", "资质声明", "@", ".com", ".cn"]
    for t in titles:
        if t in seen:
            continue
        # 关键词噪声过滤
        if any(kw in t for kw in keyword_noise):
            continue
        if re.search(r"S\d{5,}", t):  # 分析师证书号
            continue
        # 去掉编号前缀后判断是否为纯数字/百分比
        content = re.sub(
            r"^(?:\d+[.、)）]|[一二三四五六七八九十]+[、）]|[(（]\d+[)）])\s*[：:]?\s*",
            "", t,
        ).strip()
        if re.match(r"^[\d.]+%?$", content):  # 纯数字/百分比（如 3.50 / 2.00%）
            continue
        seen.add(t)
        unique.append(t)

    if unique:
        logger.info("文章标题列表提取: %d 条", len(unique))
    else:
        logger.warning("文章标题列表未提取到")

    return unique


# ====================================================================
# 聚合入口
# ====================================================================


def extract_all(parsed: dict, cfg: YanbaoConfig, title: str = "") -> dict:
    """聚合提取入口。

    Args:
        parsed: pdf_parser.extract_text_and_tables 的返回
        cfg: 配置
        title: 研报标题（用于首次覆盖检测）

    Returns:
        完整提取结果 dict
    """
    status = parsed.get("parse_status", "")
    if status != "ok":
        logger.info("跳过提取（PDF 状态: %s）", status)
        return {
            "rating": "",
            "target_price": None,
            "earnings_forecast": {"营收": {}, "归母净利润": {}},
            "core_logic": "",
            "catalysts": [],
            "risks": [],
            "first_coverage": False,
            "company_basics": {},
            "section_titles": [],
        }

    text = parsed.get("full_text", "")
    tables = parsed.get("tables", [])

    return {
        "rating": extract_rating(text, cfg),
        "target_price": extract_target_price(text),
        "earnings_forecast": extract_earnings_forecast(tables, text),
        "core_logic": extract_core_logic(text),
        "catalysts": extract_catalysts(text),
        "risks": extract_risks(text),
        "first_coverage": detect_first_coverage(title, text),
        "company_basics": extract_company_basics(text),
        "section_titles": extract_section_titles(text),
    }
