"""crawler_news 存储层：JSON Lines 落盘 + 幂等去重 + 范围加载。

对应 ``project.md`` 阶段四任务：
    - ``compute_news_id(source, date, title)``  返回 ``{source}_{date}_{title_hash8}``
    - ``save_jsonl(items, source_short, date)`` 以 ``news_id`` 为主键幂等写入 JSONL
    - ``load_jsonl(start_date, end_date, source)`` 按日期范围加载历史新闻

设计要点（对齐 requirement.md §4.1 存储方案）：
    - 格式：JSON Lines（每行一条新闻），按 "源-日期" 归档
    - 目录：``data/raw/news/{YYYY-MM}/{source_short}_{date}.json``
    - 幂等：以 ``news_id`` 为主键，同日同源重复抓取时跳过已存在记录
    - 跨月加载：``load_jsonl`` 自动遍历 ``[start_date, end_date]`` 范围内的所有月份目录

依赖：
    - 项目根 ``config/settings.py`` 的 ``settings.news_raw_dir`` 派生路径
    - 项目根 ``core/logger.py`` 的 ``get_logger``
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

try:
    from config.settings import settings

    _RAW_DIR: Path = settings.news_raw_dir
except Exception:
    # 兜底：与 tagger.py 一致的项目根解析
    _RAW_DIR = (
        Path(__file__).resolve().parent.parent.parent / "data" / "raw" / "news"
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


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 文件名格式：{source_short}_{YYYY-MM-DD}.json
_FILENAME_RE = re.compile(r"^(?P<source>.+)_(?P<date>\d{4}-\d{2}-\d{2})$")

# 日期格式校验
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# news_id 计算
# ---------------------------------------------------------------------------


def compute_news_id(source: str, date: str, title: str) -> str:
    """生成新闻唯一标识。

    格式：``{source}_{date}_{title_hash8}``
        - source:      来源短标识（如 ``cls`` / ``sina``）
        - date:        发布日期 ``YYYY-MM-DD``
        - title_hash8: 标题 SHA1 前 8 位（用于幂等去重）

    Args:
        source: 数据源短标识
        date:   发布日期（YYYY-MM-DD）
        title:  原文标题（一字不改）

    Returns:
        news_id 字符串
    """
    title = title or ""
    # SHA1 取前 8 位作为标题哈希（对齐 requirement.md §2.1 约定）
    title_hash8 = hashlib.sha1(title.encode("utf-8")).hexdigest()[:8]
    return f"{source}_{date}_{title_hash8}"


# ---------------------------------------------------------------------------
# 路径工具
# ---------------------------------------------------------------------------


def _news_file_path(source_short: str, date_str: str) -> Path:
    """根据 source + date 构造 JSONL 文件路径。

    目录结构：``data/raw/news/{YYYY-MM}/{source_short}_{date}.json``
        如 ``data/raw/news/2026-08/cls_2026-08-18.json``

    Args:
        source_short: 数据源短标识
        date_str:     目标日期 ``YYYY-MM-DD``

    Raises:
        ValueError: date_str 不符合 ``YYYY-MM-DD`` 格式
    """
    if not _DATE_RE.match(date_str):
        raise ValueError(f"date 格式应为 YYYY-MM-DD，实际: {date_str!r}")
    year_month = date_str[:7]  # "2026-08"
    return _RAW_DIR / year_month / f"{source_short}_{date_str}.json"


def _ensure_dir(path: Path) -> None:
    """确保文件父目录存在。"""
    path.parent.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 幂等写入
# ---------------------------------------------------------------------------


def save_jsonl(items: list[dict], source_short: str, date: str) -> int:
    """将新闻列表幂等写入 JSONL 文件。

    幂等策略：以 ``news_id`` 为主键，已存在的 ``news_id`` 跳过（不重复追加）。
    若 item 缺少 ``news_id``，本函数会基于 ``source_short`` + item 的
    ``publish_date``（缺失则用参数 ``date`` 兜底）+ ``title`` 自动补算。

    Args:
        items:        标准化新闻 dict 列表（来自 sources.py + tagger.py）
        source_short: 数据源短标识（用于文件名）
        date:         目标日期 YYYY-MM-DD（用于文件名）

    Returns:
        新写入的条数（已存在的 ``news_id`` 不计入）
    """
    if not items:
        log.info("save_jsonl: items 为空，跳过写入 (%s_%s)", source_short, date)
        return 0

    file_path = _news_file_path(source_short, date)
    _ensure_dir(file_path)

    # 1. 读取已存在的 news_id 集合（用于幂等去重）
    existing_ids: set[str] = set()
    if file_path.exists():
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict) and "news_id" in obj:
                            existing_ids.add(obj["news_id"])
                    except json.JSONDecodeError:
                        log.warning(
                            "save_jsonl: 跳过无法解析的行: %s", line[:80]
                        )
                        continue
        except OSError as e:
            log.error("save_jsonl: 读取已有文件失败: %s", e)

    # 2. 过滤出新条目（去重 news_id；同一批 items 内也去重）
    new_items: list[dict] = []
    seen_in_batch: set[str] = set()
    skipped = 0
    for item in items:
        # 确保 news_id 存在；缺失则补算
        news_id = item.get("news_id") or compute_news_id(
            source=source_short,
            date=item.get("publish_date") or date,
            title=item.get("title", ""),
        )
        item["news_id"] = news_id

        if news_id in existing_ids or news_id in seen_in_batch:
            skipped += 1
            continue
        seen_in_batch.add(news_id)
        new_items.append(item)

    # 3. 追加写入（不覆盖已有内容）
    if new_items:
        with open(file_path, "a", encoding="utf-8") as f:
            for item in new_items:
                # ensure_ascii=False 保留中文原文；不缩进，每行一条
                line = json.dumps(
                    item, ensure_ascii=False, separators=(",", ":")
                )
                f.write(line + "\n")

    log.info(
        "save_jsonl: %s 写入 %d 条（跳过 %d 条已存在），文件: %s",
        source_short,
        len(new_items),
        skipped,
        file_path,
    )
    return len(new_items)


# ---------------------------------------------------------------------------
# 范围加载
# ---------------------------------------------------------------------------


def _iter_month_dirs(start_date: str, end_date: str) -> list[Path]:
    """枚举 ``[start_date, end_date]`` 范围内的所有月份目录。

    如 ``start_date=2026-07-15, end_date=2026-08-17`` →
    ``[data/raw/news/2026-07/, data/raw/news/2026-08/]``

    不存在的目录自动跳过。
    """
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    if end < start:
        return []

    months: list[Path] = []
    cur = date(start.year, start.month, 1)
    end_month = date(end.year, end.month, 1)
    while cur <= end_month:
        year_month = cur.strftime("%Y-%m")
        month_dir = _RAW_DIR / year_month
        if month_dir.exists() and month_dir.is_dir():
            months.append(month_dir)
        # 下一月
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)

    return months


def _parse_filename(name_stem: str) -> Optional[tuple[str, str]]:
    """解析 JSONL 文件名 → (source_short, date_str)。

    文件名格式：``{source_short}_{YYYY-MM-DD}``（不含扩展名）

    Returns:
        (source_short, date_str) 或 None（解析失败）
    """
    m = _FILENAME_RE.match(name_stem)
    if not m:
        return None
    return m.group("source"), m.group("date")


def _shift_month(d: date, delta: int) -> date:
    """将日期 ``d`` 月份增减 ``delta`` 个月，返回该月 1 号。

    正 delta 向后偏移；负 delta 向前偏移。用于扩展 ``load_jsonl`` 的月份
    扫描范围，覆盖"次日抓取前一日新闻"等跨月边界场景。
    """
    total = d.year * 12 + (d.month - 1) + delta
    year, month_idx = divmod(total, 12)
    return date(year, month_idx + 1, 1)


def load_jsonl(
    start_date: str,
    end_date: str,
    source: Optional[str] = None,
) -> list[dict]:
    """按日期范围加载历史新闻。

    实现要点：
        - 月份目录扫描范围在 ``[start_date, end_date]`` 基础上前后各扩 1 个月，
          覆盖"文件抓取日 = 月初/月末，但 ``publish_date`` 在邻月"的跨月场景
          （如 ``cls_2026-09-01.json`` 中含 ``publish_date=2026-08-31`` 的条目）
        - 文件名按 source 前缀过滤（加速扫描）
        - 读出的每条 item 再按 ``publish_date`` 与 ``source_short`` 字段二次过滤
          （``publish_date`` 是核心索引，决定是否在范围内）
        - 返回结果按 ``publish_date`` 升序排序；解析失败的 publish_date 排末尾

    Args:
        start_date: 起始日期（YYYY-MM-DD，含）
        end_date:   截止日期（YYYY-MM-DD，含）
        source:     可选，按数据源 source_short 过滤；None = 全部源

    Returns:
        新闻 dict 列表，按 ``publish_date`` 升序排序
    """
    if not start_date or not end_date:
        log.warning("load_jsonl: start_date/end_date 必填")
        return []

    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError as e:
        log.error("load_jsonl: 日期格式错误 %s", e)
        return []

    if end < start:
        log.warning("load_jsonl: end_date < start_date，返回空列表")
        return []

    # 月份目录扫描范围前后各扩 1 个月，覆盖跨月抓取场景
    scan_start = _shift_month(start, -1)
    scan_end = _shift_month(end, +1)
    scan_months = _iter_month_dirs(
        scan_start.strftime("%Y-%m-%d"), scan_end.strftime("%Y-%m-%d")
    )

    items: list[dict] = []
    scanned_files = 0

    for month_dir in scan_months:
        for json_file in sorted(month_dir.glob("*.json")):
            scanned_files += 1

            # 1. 解析文件名 → (source, date)
            parsed = _parse_filename(json_file.stem)
            if parsed is None:
                continue
            file_source, _file_date_str = parsed

            # 2. 文件名 source 过滤（加速扫描；不对 file_date 过滤，
            #    因为 file_date 是抓取日，可能与 publish_date 跨天/跨月）
            if source and file_source != source:
                continue

            # 3. 读取每行 item
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            log.warning(
                                "load_jsonl: 跳过无法解析的行: %s", line[:80]
                            )
                            continue
                        if not isinstance(obj, dict):
                            continue

                        # 4. 按 publish_date 过滤（核心索引）
                        pub = obj.get("publish_date", "")
                        if not pub:
                            continue
                        try:
                            pub_date = datetime.strptime(
                                pub, "%Y-%m-%d"
                            ).date()
                        except ValueError:
                            continue
                        if pub_date < start or pub_date > end:
                            continue

                        # 5. 二次过滤 source_short 字段
                        if source and obj.get("source_short") != source:
                            continue

                        items.append(obj)
            except OSError as e:
                log.error(
                    "load_jsonl: 读取文件失败 %s: %s", json_file, e
                )

    # 6. 按 publish_date 升序排序；解析失败的排末尾
    def _sort_key(item: dict) -> tuple[int, str]:
        pub = item.get("publish_date", "")
        try:
            datetime.strptime(pub, "%Y-%m-%d")
            return (0, pub)
        except ValueError:
            return (1, pub)

    items.sort(key=_sort_key)

    log.info(
        "load_jsonl: 范围 %s~%s（source=%s）扫描 %d 文件，返回 %d 条",
        start_date,
        end_date,
        source,
        scanned_files,
        len(items),
    )
    return items


# ---------------------------------------------------------------------------
# 工具函数（暴露给测试与外部调用）
# ---------------------------------------------------------------------------


def get_raw_dir() -> Path:
    """暴露 RAW_DIR 路径，便于测试。"""
    return _RAW_DIR


def count_jsonl(file_path: Path) -> int:
    """统计 JSONL 文件行数（用于幂等测试验证）。

    Args:
        file_path: JSONL 文件路径

    Returns:
        非空行数；文件不存在返回 0
    """
    if not file_path.exists():
        return 0
    count = 0
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                count += 1
    return count


def list_news_files(start_date: str, end_date: str) -> list[Path]:
    """列出 ``[start_date, end_date]`` 范围内的所有新闻 JSONL 文件路径。

    与 ``load_jsonl`` 使用同样的月份扫描与文件名过滤逻辑，但不读文件内容。
    可用于运维排查或测试校验。
    """
    if not start_date or not end_date:
        return []

    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        return []

    if end < start:
        return []

    files: list[Path] = []
    for month_dir in _iter_month_dirs(start_date, end_date):
        for json_file in sorted(month_dir.glob("*.json")):
            parsed = _parse_filename(json_file.stem)
            if parsed is None:
                continue
            file_source, file_date_str = parsed
            try:
                file_date = datetime.strptime(
                    file_date_str, "%Y-%m-%d"
                ).date()
            except ValueError:
                continue
            if file_date < start or file_date > end:
                continue
            files.append(json_file)

    return files


def clear_raw_dir(raw_dir: Optional[Path] = None) -> None:
    """清空 raw 目录下所有 JSONL 文件（仅测试使用）。

    Args:
        raw_dir: 指定清理目录；None = 使用默认 ``_RAW_DIR``
    """
    import shutil

    target = raw_dir or _RAW_DIR
    if not target.exists():
        return
    for month_dir in target.iterdir():
        if month_dir.is_dir():
            shutil.rmtree(month_dir, ignore_errors=True)
        elif month_dir.suffix == ".json":
            month_dir.unlink(missing_ok=True)
    log.info("clear_raw_dir: 已清空 %s", target)
