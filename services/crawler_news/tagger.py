"""股票代码标注模块：正则提取 + 本地映射表反查双重策略。

对应 ``project.md`` 阶段三任务：
    - ``extract_codes(text)``        正则提取 6 位代码，按沪深规则校验
    - ``resolve_names(codes)``       本地映射表反查名称
    - ``resolve_codes_by_name(text)`` 名称反查代码补全
    - ``tag_news(item)``             对 title+content 执行标注，去重写入 mentioned_codes/names

设计要点（对齐 requirement.md §3.2 股票标注策略）：
    - 优先级：正则直接命中的代码优先；未命中代码但命中本地映射表名称时，
      通过名称反查补全。
    - 去重：同一新闻内同一代码/名称多次提及只保留一次，保留首次出现顺序。
    - 容错：映射表缺失或解析失败时返回空，不阻断采集流程。

依赖：
    - 项目根 ``data/stock_mapping.json``（扁平 ``code→name`` 结构，
      支持可选 ``_comment``/``_example`` 元数据键自动忽略）
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

try:
    from .news_config import crawl_config
except ImportError:
    from news_config import crawl_config

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
# 沪深代码前缀校验规则
# ---------------------------------------------------------------------------
#
# 对齐 project.md 阶段三：6 开头上交所、0/3 开头深交所。
# 北交所（4/8 开头）不在 requirement 标注范围内，故不收集。
# 完整覆盖范围：
#   - 600/601/603/605：上交所主板
#   - 688/689：科创板
#   - 000/001：深交所主板
#   - 002/003：中小板
#   - 300/301：创业板
SH_PREFIXES: tuple[str, ...] = ("6", "0", "3")

# 元数据键前缀：stock_mapping.json 中以 _ 开头的键视为文档说明，不当作股票映射
_METADATA_KEY_PREFIX = "_"


# ---------------------------------------------------------------------------
# 映射表缓存（懒加载，进程内复用）
# ---------------------------------------------------------------------------


_MAPPING_PATH_CANDIDATES: tuple[Path, ...] = (
    # 1. 项目根 data/stock_mapping.json（tagger.py 位于 services/crawler_news/）
    Path(__file__).resolve().parent.parent.parent / "data" / "stock_mapping.json",
    # 2. 兜底：环境变量 DATA_DIR 指定的路径
)

_CODE_TO_NAME: Optional[dict[str, str]] = None
_NAME_TO_CODE: Optional[dict[str, str]] = None


def _resolve_mapping_path() -> Optional[Path]:
    """返回首个存在的映射表文件路径；找不到返回 None。"""
    for p in _MAPPING_PATH_CANDIDATES:
        try:
            if p.exists() and p.is_file():
                return p
        except OSError:
            continue
    # 兜底：通过环境变量 DATA_DIR 寻找
    import os

    data_dir = os.environ.get("DATA_DIR")
    if data_dir:
        candidate = Path(data_dir) / "stock_mapping.json"
        if candidate.exists():
            return candidate
    return None


def _load_mapping() -> tuple[dict[str, str], dict[str, str]]:
    """加载 ``data/stock_mapping.json``，返回 (code→name, name→code) 双向映射。

    支持的 JSON 结构：
        1. 扁平 dict：``{"600519": "贵州茅台", ...}``
        2. 含元数据键：``{"_comment": "...", "_example": "...", "600519": "贵州茅台"}``
           → 以 ``_`` 开头的键自动忽略

    Returns:
        (code_to_name, name_to_code) 双向映射 dict；
        文件缺失或解析失败时返回空 dict，不抛异常。
    """
    global _CODE_TO_NAME, _NAME_TO_CODE
    if _CODE_TO_NAME is not None and _NAME_TO_CODE is not None:
        return _CODE_TO_NAME, _NAME_TO_CODE

    code_to_name: dict[str, str] = {}
    name_to_code: dict[str, str] = {}

    path = _resolve_mapping_path()
    if path is None:
        log.warning("未找到 stock_mapping.json，标注仅依赖正则提取，名称反查不可用")
        _CODE_TO_NAME = code_to_name
        _NAME_TO_CODE = name_to_code
        return code_to_name, name_to_code

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        if isinstance(raw, dict):
            for key, value in raw.items():
                # 跳过 _comment / _example 等元数据键
                if not isinstance(key, str) or key.startswith(_METADATA_KEY_PREFIX):
                    continue
                code = str(key).strip()
                name = str(value).strip() if value else ""
                # 跳过非 6 位代码或非沪深前缀的键（防御性，避免脏数据干扰）
                if not (len(code) == 6 and code.isdigit()):
                    continue
                if not code.startswith(SH_PREFIXES):
                    continue
                if code and name:
                    code_to_name[code] = name
                    name_to_code[name] = code
        else:
            log.warning("stock_mapping.json 顶层不是 dict，已忽略全部条目")

        log.info(
            "stock_mapping.json 加载完成: %d 条映射（%s）",
            len(code_to_name),
            path,
        )
    except json.JSONDecodeError as e:
        log.error("stock_mapping.json JSON 解析失败: %s", e)
    except OSError as e:
        log.error("stock_mapping.json 读取失败: %s", e)

    _CODE_TO_NAME = code_to_name
    _NAME_TO_CODE = name_to_code
    return code_to_name, name_to_code


def reload_mapping() -> None:
    """强制重新加载映射表（测试场景切换 mock 文件时使用）。"""
    global _CODE_TO_NAME, _NAME_TO_CODE
    _CODE_TO_NAME = None
    _NAME_TO_CODE = None
    _load_mapping()


# ---------------------------------------------------------------------------
# 正则提取
# ---------------------------------------------------------------------------


def extract_codes(text: str) -> list[str]:
    """从文本中正则提取 6 位股票代码，按沪深规则校验。

    实现要点：
        - 使用 ``(?<!\\d)(\\d{6})(?!\\d)`` 替代 ``\\b`` 断言，避免在中文上下文中
          ``\\b`` 不生效导致漏匹配（``\\b`` 依赖 ``\\w``，CJK 字符不属于 ``\\w``）。
        - 命中后按前缀校验：6 开头上交所、0/3 开头深交所；
          北交所（4/8 开头）不在标注范围，自动过滤。
        - 去重并保留首次出现顺序。

    Args:
        text: 待扫描文本（None / 空字符串返回空列表）

    Returns:
        去重后的代码列表
    """
    if not text:
        return []

    # crawl_config.code_pattern 在 news_config.py 中默认为 r"\b([036]\d{5})\b"
    # 但为兼容中文上下文（\b 对 CJK 无效），此处改用前后非数字断言
    pattern = r"(?<!\d)(\d{6})(?!\d)"
    matches = re.findall(pattern, str(text))

    seen: set[str] = set()
    codes: list[str] = []
    for m in matches:
        if m in seen:
            continue
        if not m.startswith(SH_PREFIXES):
            continue
        seen.add(m)
        codes.append(m)

    return codes


# ---------------------------------------------------------------------------
# 名称反查
# ---------------------------------------------------------------------------


def resolve_names(codes: list[str]) -> list[str]:
    """根据代码列表反查股票名称。

    Args:
        codes: 已提取的代码列表

    Returns:
        名称列表，与 ``codes`` 一一对应；映射表未命中时对应位置为空字符串
        （不剔除，保持索引对齐，便于调用方关联 code↔name）
    """
    if not codes:
        return []

    code_to_name, _ = _load_mapping()
    return [code_to_name.get(c, "") for c in codes]


def resolve_codes_by_name(text: str) -> list[str]:
    """扫描文本中是否含映射表内的股票名称，命中则反查代码补全。

    实现要点：
        - 按名称长度降序匹配，避免短名称误命中长名称子串
          （如 ``平安`` 不应误命中 ``平安银行``，故先匹配 ``平安银行``）
        - 同一文本中多次命中同一名称只保留一次代码

    Args:
        text: 待扫描文本（None / 空字符串返回空列表）

    Returns:
        去重后的代码列表（保留出现顺序）
    """
    if not text:
        return []

    _, name_to_code = _load_mapping()
    if not name_to_code:
        return []

    text_str = str(text)

    seen: set[str] = set()
    codes: list[str] = []
    # 按名称长度降序匹配，避免短名误命中长名子串
    for name in sorted(name_to_code.keys(), key=len, reverse=True):
        if name in text_str:
            code = name_to_code[name]
            if code not in seen:
                seen.add(code)
                codes.append(code)

    return codes


# ---------------------------------------------------------------------------
# 单条新闻标注
# ---------------------------------------------------------------------------


def tag_news(item: dict) -> dict:
    """对单条新闻执行股票代码标注，写入 ``mentioned_codes`` / ``mentioned_names``。

    流程（对齐 requirement.md §3.2）：
        1. 合并 ``title`` + ``content`` 为待扫描全文
        2. 正则提取代码（``extract_codes``）
        3. 名称反查补全代码（``resolve_codes_by_name``）
        4. 与接口自带的 ``mentioned_codes``/``mentioned_names``（如巨潮的
           ``代码``/``简称`` 列）合并，去重保留首次出现顺序
        5. 反查所有代码对应名称，合并去重

    Args:
        item: ``sources.py`` 产出的标准化 dict，原地修改并返回

    Returns:
        同一 dict，``mentioned_codes`` / ``mentioned_names`` 已填充/合并
    """
    title = str(item.get("title", "") or "")
    content = str(item.get("content", "") or "")
    # title 与 content 都可能为空（如巨潮无 content）；用换行连接避免首尾粘连
    full_text = "\n".join(s for s in (title, content) if s)

    # 1. 正则提取
    extracted = extract_codes(full_text)

    # 2. 名称反查
    by_name = resolve_codes_by_name(full_text)

    # 3. 合并去重：接口自带 → 正则命中 → 名称反查
    existing_codes: list[str] = [c for c in (item.get("mentioned_codes") or []) if c]
    existing_names: list[str] = [n for n in (item.get("mentioned_names") or []) if n]

    all_codes: list[str] = []
    seen_codes: set[str] = set()

    for c in existing_codes:
        if c not in seen_codes:
            seen_codes.add(c)
            all_codes.append(c)
    for c in extracted:
        if c not in seen_codes:
            seen_codes.add(c)
            all_codes.append(c)
    for c in by_name:
        if c not in seen_codes:
            seen_codes.add(c)
            all_codes.append(c)

    # 4. 反查所有代码对应名称
    resolved_names = resolve_names(all_codes)

    # 5. 合并 names：原有 names → 反查 names，去重
    all_names: list[str] = []
    seen_names: set[str] = set()
    for n in existing_names:
        if n not in seen_names:
            seen_names.add(n)
            all_names.append(n)
    for n in resolved_names:
        if n and n not in seen_names:
            seen_names.add(n)
            all_names.append(n)

    item["mentioned_codes"] = all_codes
    item["mentioned_names"] = all_names
    return item


def tag_news_batch(items: list[dict]) -> list[dict]:
    """批量标注新闻列表（便利入口）。

    Args:
        items: ``sources.py`` 产出的标准化 dict 列表

    Returns:
        同一列表（原地修改），每条 dict 的 ``mentioned_*`` 已填充
    """
    for item in items:
        tag_news(item)
    return items
