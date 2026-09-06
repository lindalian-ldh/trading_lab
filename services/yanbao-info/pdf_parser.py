"""yanbao-info PDF 解析层（pdfplumber 封装）。

职责:
    - 用 pdfplumber 逐页提取全文文本 + 表格
    - 扫描版 PDF 检测（文本字符数 < pdf_text_min_chars）
    - 最多解析 pdf_max_pages 页（防超长报告耗时）

返回结构:
    {
        "full_text": str,
        "tables": list[pd.DataFrame],
        "page_count": int,
        "parse_status": "ok" | "scanned_pdf" | "parse_failed",
        "char_count": int,
    }
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import pandas as pd

try:
    from config import YanbaoConfig
except ImportError:
    from .config import YanbaoConfig

logger = logging.getLogger(__name__)


def extract_text_and_tables(pdf_path: Path, cfg: YanbaoConfig) -> dict:
    """用 pdfplumber 提取全文文本 + 所有表格。

    Args:
        pdf_path: PDF 文件路径
        cfg: 配置（使用 pdf_max_pages / pdf_text_min_chars）

    Returns:
        dict, 字段见模块 docstring
    """
    if not pdf_path.exists():
        logger.error("PDF 文件不存在: %s", pdf_path)
        return _empty_result("parse_failed")

    try:
        import pdfplumber
    except ImportError:
        logger.error("pdfplumber 未安装，请执行 uv sync")
        return _empty_result("parse_failed")

    full_text_parts: list[str] = []
    tables: list[pd.DataFrame] = []
    page_count = 0
    parse_status = "ok"

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            total_pages = len(pdf.pages)
            pages_to_parse = min(total_pages, cfg.pdf_max_pages)
            logger.info(
                "PDF 解析开始: %s (共 %d 页，解析前 %d 页)",
                pdf_path.name,
                total_pages,
                pages_to_parse,
            )

            for i, page in enumerate(pdf.pages[:pages_to_parse]):
                page_count = i + 1
                # 文本提取
                try:
                    text = page.extract_text() or ""
                except Exception as e:
                    logger.warning("第 %d 页文本提取失败: %s", page_count, e)
                    text = ""
                full_text_parts.append(text)

                # 表格提取
                try:
                    page_tables = page.extract_tables()
                    if page_tables:
                        for tbl in page_tables:
                            if tbl and len(tbl) > 1:
                                # 第一行作为表头
                                df = pd.DataFrame(tbl[1:], columns=tbl[0])
                                tables.append(df)
                except Exception as e:
                    logger.warning("第 %d 页表格提取失败: %s", page_count, e)

    except Exception as e:
        logger.error("PDF 解析异常: %s", e)
        return _empty_result("parse_failed")

    full_text = "\n\n".join(full_text_parts)
    char_count = len(full_text)

    # 扫描版检测：文本字符数过少
    if char_count < cfg.pdf_text_min_chars:
        parse_status = "scanned_pdf"
        logger.warning(
            "PDF 文本字符数 %d < %d，判定为扫描版 PDF（跳过提取）",
            char_count,
            cfg.pdf_text_min_chars,
        )

    logger.info(
        "PDF 解析完成: 状态=%s, 页数=%d, 字符=%d, 表格=%d",
        parse_status,
        page_count,
        char_count,
        len(tables),
    )

    return {
        "full_text": full_text,
        "tables": tables,
        "page_count": page_count,
        "parse_status": parse_status,
        "char_count": char_count,
    }


def _empty_result(status: str) -> dict:
    """构造空结果 dict（用于解析失败场景）。"""
    return {
        "full_text": "",
        "tables": [],
        "page_count": 0,
        "parse_status": status,
        "char_count": 0,
    }
