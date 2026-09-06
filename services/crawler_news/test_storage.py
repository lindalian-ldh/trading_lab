"""crawler_news 阶段四单元测试。

覆盖（对齐 project.md 阶段四验收标准）：
    1. 同日同源重复抓取两次，文件行数不增加（幂等）
    2. ``load_jsonl("2026-08-01", "2026-08-17")`` 能跨月加载并返回按日期升序列表
    3. compute_news_id 格式正确（SHA1 前 8 位）
    4. save_jsonl 字段补算 / 批内去重 / 中文原文保留 / 目录自动创建
    5. load_jsonl 范围过滤 / source 过滤 / 跨月扫描 / 损坏行容错
    6. 边界场景：空 items / 非法日期 / end<start / 不存在的月份目录

设计要点：
    - **完全隔离真实数据目录**：通过 ``isolated_raw_dir`` fixture 把
      ``storage._RAW_DIR`` 重定向到 ``tmp_path`` 子目录，避免污染真实
      ``data/raw/news/`` 数据
    - 不访问网络，纯本地 JSONL 读写

运行方式：
    cd trading_lab && uv run pytest services/crawler_news/test_storage.py -v
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

# 让测试可以 import crawler_news 模块
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SERVICE_DIR = Path(__file__).resolve().parent

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
if str(_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICE_DIR))

import storage
from storage import (
    compute_news_id,
    save_jsonl,
    load_jsonl,
    count_jsonl,
    list_news_files,
    _news_file_path,
    _iter_month_dirs,
    _parse_filename,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_raw_dir(tmp_path, monkeypatch):
    """把 storage._RAW_DIR 重定向到临时目录，避免污染真实数据。

    每个 test 自动获得一个干净的临时 raw 目录；测试结束后 tmp_path 自动清理。
    """
    new_dir = tmp_path / "raw" / "news"
    new_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage, "_RAW_DIR", new_dir)
    yield new_dir


def _make_item(
    title: str,
    publish_date: str = "2026-08-18",
    source_short: str = "cls",
    content: str = "",
    source_url: str = "",
    mentioned_codes: list[str] | None = None,
    mentioned_names: list[str] | None = None,
    news_id: str | None = None,
) -> dict:
    """构造标准化新闻 dict（对齐 sources.py 输出）。"""
    item = {
        "source_url": source_url,
        "source_channel": "财联社" if source_short == "cls" else "测试源",
        "source_short": source_short,
        "publish_date": publish_date,
        "publish_time": "10:00:00",
        "title": title,
        "content": content,
        "mentioned_codes": mentioned_codes or [],
        "mentioned_names": mentioned_names or [],
    }
    if news_id is not None:
        item["news_id"] = news_id
    return item


# ---------------------------------------------------------------------------
# compute_news_id 单元测试
# ---------------------------------------------------------------------------


class TestComputeNewsId:
    def test_format_contains_three_parts(self):
        nid = compute_news_id("cls", "2026-08-18", "测试标题")
        parts = nid.split("_")
        assert len(parts) == 3
        assert parts[0] == "cls"
        assert parts[1] == "2026-08-18"
        assert len(parts[2]) == 8  # SHA1 前 8 位

    def test_same_input_produces_same_id(self):
        nid1 = compute_news_id("cls", "2026-08-18", "相同标题")
        nid2 = compute_news_id("cls", "2026-08-18", "相同标题")
        assert nid1 == nid2

    def test_different_title_produces_different_id(self):
        nid1 = compute_news_id("cls", "2026-08-18", "标题A")
        nid2 = compute_news_id("cls", "2026-08-18", "标题B")
        assert nid1 != nid2

    def test_different_source_produces_different_id(self):
        nid1 = compute_news_id("cls", "2026-08-18", "标题")
        nid2 = compute_news_id("sina", "2026-08-18", "标题")
        assert nid1 != nid2

    def test_different_date_produces_different_id(self):
        nid1 = compute_news_id("cls", "2026-08-17", "标题")
        nid2 = compute_news_id("cls", "2026-08-18", "标题")
        assert nid1 != nid2

    def test_empty_title_does_not_raise(self):
        nid = compute_news_id("cls", "2026-08-18", "")
        assert nid.startswith("cls_2026-08-18_")
        assert len(nid) == len("cls_2026-08-18_") + 8

    def test_none_title_does_not_raise(self):
        nid = compute_news_id("cls", "2026-08-18", None)  # type: ignore[arg-type]
        assert nid.startswith("cls_2026-08-18_")

    def test_chinese_title_hash_stable(self):
        """中文标题的 SHA1 应稳定，确保跨进程幂等。"""
        import hashlib

        expected_hash = hashlib.sha1("贵州茅台涨停".encode("utf-8")).hexdigest()[:8]
        nid = compute_news_id("cls", "2026-08-18", "贵州茅台涨停")
        assert nid == f"cls_2026-08-18_{expected_hash}"


# ---------------------------------------------------------------------------
# _news_file_path 路径构造
# ---------------------------------------------------------------------------


class TestNewsFilePath:
    def test_path_format(self, isolated_raw_dir):
        path = _news_file_path("cls", "2026-08-18")
        assert path == isolated_raw_dir / "2026-08" / "cls_2026-08-18.json"

    def test_path_for_different_source(self, isolated_raw_dir):
        path = _news_file_path("sina", "2026-07-31")
        assert path == isolated_raw_dir / "2026-07" / "sina_2026-07-31.json"

    def test_invalid_date_format_raises(self, isolated_raw_dir):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            _news_file_path("cls", "20260818")

    def test_invalid_date_format_raises_short(self, isolated_raw_dir):
        with pytest.raises(ValueError):
            _news_file_path("cls", "2026-08")


# ---------------------------------------------------------------------------
# _parse_filename
# ---------------------------------------------------------------------------


class TestParseFilename:
    def test_normal_filename(self):
        assert _parse_filename("cls_2026-08-18") == ("cls", "2026-08-18")

    def test_em_global_source_with_underscore(self):
        """em_global 含下划线，但 _FILENAME_RE 用非贪婪 source 段也能正确解析。"""
        # 注意：实际匹配中 source 段会贪婪匹配到第一个 _ 后内容
        # 因 _FILENAME_RE = ^(?P<source>.+)_(?P<date>\d{4}-\d{2}-\d{2})$
        # "em_global_2026-08-18" → source="em_global", date="2026-08-18"
        result = _parse_filename("em_global_2026-08-18")
        assert result == ("em_global", "2026-08-18")

    def test_no_underscore_returns_none(self):
        assert _parse_filename("cls20260818") is None

    def test_no_date_returns_none(self):
        assert _parse_filename("cls_20260818") is None  # 缺 - 分隔

    def test_empty_returns_none(self):
        assert _parse_filename("") is None


# ---------------------------------------------------------------------------
# _iter_month_dirs
# ---------------------------------------------------------------------------


class TestIterMonthDirs:
    def test_single_month(self, isolated_raw_dir):
        # 创建 2026-08 目录
        (isolated_raw_dir / "2026-08").mkdir()
        months = _iter_month_dirs("2026-08-01", "2026-08-17")
        assert len(months) == 1
        assert months[0].name == "2026-08"

    def test_cross_month(self, isolated_raw_dir):
        (isolated_raw_dir / "2026-07").mkdir()
        (isolated_raw_dir / "2026-08").mkdir()
        months = _iter_month_dirs("2026-07-15", "2026-08-17")
        assert [m.name for m in months] == ["2026-07", "2026-08"]

    def test_cross_year(self, isolated_raw_dir):
        (isolated_raw_dir / "2025-12").mkdir()
        (isolated_raw_dir / "2026-01").mkdir()
        months = _iter_month_dirs("2025-12-15", "2026-01-17")
        assert [m.name for m in months] == ["2025-12", "2026-01"]

    def test_nonexistent_months_skipped(self, isolated_raw_dir):
        # 2026-07 不存在，应被跳过
        (isolated_raw_dir / "2026-08").mkdir()
        months = _iter_month_dirs("2026-07-15", "2026-08-17")
        assert [m.name for m in months] == ["2026-08"]

    def test_end_before_start_returns_empty(self, isolated_raw_dir):
        months = _iter_month_dirs("2026-08-17", "2026-08-01")
        assert months == []

    def test_same_day(self, isolated_raw_dir):
        (isolated_raw_dir / "2026-08").mkdir()
        months = _iter_month_dirs("2026-08-17", "2026-08-17")
        assert [m.name for m in months] == ["2026-08"]


# ---------------------------------------------------------------------------
# save_jsonl 验收场景一：幂等写入
# ---------------------------------------------------------------------------


class TestSaveJsonlIdempotent:
    """验收场景：同日同源重复抓取两次，文件行数不增加。"""

    def test_save_twice_same_items_no_duplicate(self, isolated_raw_dir):
        items = [
            _make_item("贵州茅台涨停", publish_date="2026-08-18"),
            _make_item("平安银行发布年报", publish_date="2026-08-18"),
        ]

        # 第一次写入
        n1 = save_jsonl(items, "cls", "2026-08-18")
        assert n1 == 2

        file_path = isolated_raw_dir / "2026-08" / "cls_2026-08-18.json"
        assert file_path.exists()
        assert count_jsonl(file_path) == 2

        # 第二次写入同样的 items
        n2 = save_jsonl(items, "cls", "2026-08-18")
        assert n2 == 0  # 全部跳过
        # 文件行数不增加
        assert count_jsonl(file_path) == 2

    def test_partial_idempotent_only_new_written(self, isolated_raw_dir):
        """混合场景：旧条目 + 新条目，只追加新条目。"""
        old_items = [
            _make_item("旧标题1", publish_date="2026-08-18"),
            _make_item("旧标题2", publish_date="2026-08-18"),
        ]
        save_jsonl(old_items, "cls", "2026-08-18")

        # 第二次：包含 1 条旧的 + 1 条新的
        mixed = [
            _make_item("旧标题1", publish_date="2026-08-18"),  # 已存在
            _make_item("新标题3", publish_date="2026-08-18"),  # 新增
        ]
        n = save_jsonl(mixed, "cls", "2026-08-18")
        assert n == 1  # 仅新增 1 条

        file_path = isolated_raw_dir / "2026-08" / "cls_2026-08-18.json"
        assert count_jsonl(file_path) == 3  # 2 旧 + 1 新

    def test_batch_internal_dedup(self, isolated_raw_dir):
        """同一批 items 内含重复 news_id，应只写入一次。"""
        # 构造两个 news_id 相同的 item（同 source+date+title）
        items = [
            _make_item("相同标题", publish_date="2026-08-18"),
            _make_item("相同标题", publish_date="2026-08-18"),  # 重复
        ]
        n = save_jsonl(items, "cls", "2026-08-18")
        assert n == 1  # 批内去重

        file_path = isolated_raw_dir / "2026-08" / "cls_2026-08-18.json"
        assert count_jsonl(file_path) == 1


# ---------------------------------------------------------------------------
# save_jsonl 边界场景
# ---------------------------------------------------------------------------


class TestSaveJsonlEdgeCases:
    def test_empty_items_returns_zero(self, isolated_raw_dir):
        n = save_jsonl([], "cls", "2026-08-18")
        assert n == 0
        # 不应创建文件
        file_path = isolated_raw_dir / "2026-08" / "cls_2026-08-18.json"
        assert not file_path.exists()

    def test_creates_month_dir_if_missing(self, isolated_raw_dir):
        """目录不存在时应自动创建。"""
        items = [_make_item("测试", publish_date="2026-08-18")]
        # 此时 2026-08/ 目录尚不存在
        month_dir = isolated_raw_dir / "2026-08"
        assert not month_dir.exists()

        save_jsonl(items, "cls", "2026-08-18")

        assert month_dir.exists()
        assert (month_dir / "cls_2026-08-18.json").exists()

    def test_news_id_auto_computed_if_missing(self, isolated_raw_dir):
        """item 缺少 news_id 时应基于 source+publish_date+title 自动补算。"""
        items = [_make_item("测试标题", publish_date="2026-08-18")]
        # 不预设 news_id
        items[0].pop("news_id", None)

        save_jsonl(items, "cls", "2026-08-18")

        file_path = isolated_raw_dir / "2026-08" / "cls_2026-08-18.json"
        with open(file_path, "r", encoding="utf-8") as f:
            obj = json.loads(f.readline())
        expected_id = compute_news_id("cls", "2026-08-18", "测试标题")
        assert obj["news_id"] == expected_id

    def test_news_id_pre_computed_preserved(self, isolated_raw_dir):
        """item 自带 news_id 时应保留，不重新计算。"""
        items = [_make_item("测试", publish_date="2026-08-18", news_id="custom_id_123")]
        save_jsonl(items, "cls", "2026-08-18")

        file_path = isolated_raw_dir / "2026-08" / "cls_2026-08-18.json"
        with open(file_path, "r", encoding="utf-8") as f:
            obj = json.loads(f.readline())
        assert obj["news_id"] == "custom_id_123"

    def test_publish_date_missing_falls_back_to_date_param(self, isolated_raw_dir):
        """item 缺 publish_date 时，news_id 用参数 date 兜底。"""
        items = [_make_item("测试", publish_date="")]
        # 实际场景 publish_date 缺失罕见，此处验证兜底逻辑
        save_jsonl(items, "cls", "2026-08-18")

        file_path = isolated_raw_dir / "2026-08" / "cls_2026-08-18.json"
        with open(file_path, "r", encoding="utf-8") as f:
            obj = json.loads(f.readline())
        # news_id 应基于 date 参数 "2026-08-18" 计算
        expected_id = compute_news_id("cls", "2026-08-18", "测试")
        assert obj["news_id"] == expected_id

    def test_chinese_text_preserved_byte_level(self, isolated_raw_dir):
        """原文一字不改：中文标题/正文应原样落盘。"""
        title = "贵州茅台(600519)今日涨停10%"
        content = "贵州茅台发布2025年年报，净利润同比增长15%。原文保留测试。"
        items = [_make_item(title, content=content, publish_date="2026-08-18")]

        save_jsonl(items, "cls", "2026-08-18")

        file_path = isolated_raw_dir / "2026-08" / "cls_2026-08-18.json"
        with open(file_path, "r", encoding="utf-8") as f:
            obj = json.loads(f.readline())
        assert obj["title"] == title
        assert obj["content"] == content

    def test_each_item_one_line(self, isolated_raw_dir):
        """每行一条 JSON，无缩进，便于 diff 与追加。"""
        items = [
            _make_item("标题1", publish_date="2026-08-18"),
            _make_item("标题2", publish_date="2026-08-18"),
        ]
        save_jsonl(items, "cls", "2026-08-18")

        file_path = isolated_raw_dir / "2026-08" / "cls_2026-08-18.json"
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        # 两条 item = 两行
        assert len(lines) == 2
        # 每行以 \n 结尾，无多余空白
        for line in lines:
            assert line.endswith("\n")
            # 单行 JSON 不应包含换行符（json.dumps 默认无 indent）
            obj = json.loads(line)
            assert isinstance(obj, dict)

    def test_invalid_date_raises(self, isolated_raw_dir):
        """非法 date 格式应抛 ValueError。"""
        items = [_make_item("测试", publish_date="2026-08-18")]
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            save_jsonl(items, "cls", "20260818")

    def test_multiple_sources_separate_files(self, isolated_raw_dir):
        """不同 source 写入不同文件，互不影响。"""
        items_cls = [_make_item("财联新闻", source_short="cls")]
        items_sina = [_make_item("新浪新闻", source_short="sina")]

        save_jsonl(items_cls, "cls", "2026-08-18")
        save_jsonl(items_sina, "sina", "2026-08-18")

        cls_file = isolated_raw_dir / "2026-08" / "cls_2026-08-18.json"
        sina_file = isolated_raw_dir / "2026-08" / "sina_2026-08-18.json"
        assert cls_file.exists()
        assert sina_file.exists()
        assert count_jsonl(cls_file) == 1
        assert count_jsonl(sina_file) == 1


# ---------------------------------------------------------------------------
# load_jsonl 验收场景二：跨月加载 + 升序排序
# ---------------------------------------------------------------------------


class TestLoadJsonlCrossMonth:
    """验收场景：load_jsonl 跨月加载并返回按日期升序列表。"""

    def test_cross_month_load_sorted_ascending(self, isolated_raw_dir):
        """在 2026-07 与 2026-08 两个月都写入数据，load_jsonl 应跨月加载并升序排序。"""
        # 写入 2026-08 数据
        items_aug = [
            _make_item("8月17日新闻", publish_date="2026-08-17", source_short="cls"),
            _make_item("8月1日新闻", publish_date="2026-08-01", source_short="cls"),
        ]
        save_jsonl(items_aug, "cls", "2026-08-17")

        # 写入 2026-07 数据（注意文件名为 2026-07-31，但 publish_date 是 7 月某天）
        items_jul = [
            _make_item("7月15日新闻", publish_date="2026-07-15", source_short="cls"),
            _make_item("7月31日新闻", publish_date="2026-07-31", source_short="cls"),
        ]
        # 文件名用 2026-07-31 表示抓取日
        save_jsonl(items_jul, "cls", "2026-07-31")

        # 加载跨月范围
        result = load_jsonl("2026-07-01", "2026-08-31")
        # 应跨月加载 4 条，按 publish_date 升序
        assert len(result) == 4
        dates = [item["publish_date"] for item in result]
        assert dates == ["2026-07-15", "2026-07-31", "2026-08-01", "2026-08-17"]

    def test_load_single_month(self, isolated_raw_dir):
        """单月范围内的加载也应工作。"""
        items = [
            _make_item("早新闻", publish_date="2026-08-01", source_short="cls"),
            _make_item("晚新闻", publish_date="2026-08-17", source_short="cls"),
        ]
        save_jsonl(items, "cls", "2026-08-18")

        result = load_jsonl("2026-08-01", "2026-08-17")
        assert len(result) == 2
        assert result[0]["publish_date"] == "2026-08-01"
        assert result[1]["publish_date"] == "2026-08-17"

    def test_load_filtered_by_source(self, isolated_raw_dir):
        """source 参数过滤：只返回指定源的数据。"""
        save_jsonl(
            [_make_item("财联1", source_short="cls")], "cls", "2026-08-18"
        )
        save_jsonl(
            [_make_item("新浪1", source_short="sina")], "sina", "2026-08-18"
        )
        save_jsonl(
            [_make_item("财联2", source_short="cls")], "cls", "2026-08-18"
        )

        result = load_jsonl("2026-08-01", "2026-08-31", source="cls")
        assert len(result) == 2
        for item in result:
            assert item["source_short"] == "cls"

    def test_load_excludes_items_outside_date_range(self, isolated_raw_dir):
        """同一文件中含范围内与范围外的条目，只返回范围内条目。

        场景：文件抓取日 2026-08-18（次日抓取），含两条 item：
        - publish_date=2026-08-10（在查询范围 [2026-08-01, 2026-08-17] 内）
        - publish_date=2026-08-25（在范围外）
        load_jsonl 应只返回 1 条范围内的 item。
        """
        items = [
            _make_item("范围内", publish_date="2026-08-10", source_short="cls"),
            _make_item("范围外", publish_date="2026-08-25", source_short="cls"),
        ]
        save_jsonl(items, "cls", "2026-08-18")

        # 查询 2026-08-01 ~ 2026-08-17 范围
        result = load_jsonl("2026-08-01", "2026-08-17")
        assert len(result) == 1
        assert result[0]["publish_date"] == "2026-08-10"

    def test_load_returns_empty_for_empty_range(self, isolated_raw_dir):
        """目录为空时返回空列表。"""
        result = load_jsonl("2026-08-01", "2026-08-31")
        assert result == []

    def test_load_empty_when_end_before_start(self, isolated_raw_dir):
        """end_date < start_date 时返回空列表。"""
        save_jsonl(
            [_make_item("测试", publish_date="2026-08-18")], "cls", "2026-08-18"
        )
        result = load_jsonl("2026-08-31", "2026-08-01")
        assert result == []

    def test_load_invalid_date_returns_empty(self, isolated_raw_dir):
        """非法日期格式应返回空列表，不抛异常。"""
        result = load_jsonl("20260801", "2026-08-31")
        assert result == []

        result2 = load_jsonl("2026-08-01", "not-a-date")
        assert result2 == []

    def test_load_skips_corrupt_lines(self, isolated_raw_dir):
        """文件中存在无法解析的行时应跳过，不抛异常。"""
        # 手动构造一个含损坏行的 JSONL
        month_dir = isolated_raw_dir / "2026-08"
        month_dir.mkdir(parents=True)
        file_path = month_dir / "cls_2026-08-18.json"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(_make_item("正常标题", publish_date="2026-08-18")) + "\n")
            f.write("这不是JSON{坏掉了\n")  # 损坏行
            f.write(json.dumps(_make_item("另一条", publish_date="2026-08-18")) + "\n")

        result = load_jsonl("2026-08-01", "2026-08-31")
        assert len(result) == 2  # 仅正常 2 条

    def test_load_skips_non_dict_lines(self, isolated_raw_dir):
        """JSON 数组或标量行应被跳过。"""
        month_dir = isolated_raw_dir / "2026-08"
        month_dir.mkdir(parents=True)
        file_path = month_dir / "cls_2026-08-18.json"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(_make_item("正常", publish_date="2026-08-18")) + "\n")
            f.write(json.dumps(["list", "not", "dict"]) + "\n")
            f.write(json.dumps("string scalar") + "\n")

        result = load_jsonl("2026-08-01", "2026-08-31")
        assert len(result) == 1

    def test_load_publish_date_missing_skipped(self, isolated_raw_dir):
        """publish_date 缺失的条目应被跳过。"""
        month_dir = isolated_raw_dir / "2026-08"
        month_dir.mkdir(parents=True)
        file_path = month_dir / "cls_2026-08-18.json"
        with open(file_path, "w", encoding="utf-8") as f:
            # 写入一条无 publish_date 的 dict
            obj = _make_item("无日期", publish_date="2026-08-18")
            obj.pop("publish_date")
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

        result = load_jsonl("2026-08-01", "2026-08-31")
        assert result == []

    def test_load_invalid_publish_date_skipped(self, isolated_raw_dir):
        """publish_date 格式非法的条目应被跳过。"""
        month_dir = isolated_raw_dir / "2026-08"
        month_dir.mkdir(parents=True)
        file_path = month_dir / "cls_2026-08-18.json"
        with open(file_path, "w", encoding="utf-8") as f:
            obj = _make_item("非法日期", publish_date="2026-08-18")
            obj["publish_date"] = "not-a-date"
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

        result = load_jsonl("2026-08-01", "2026-08-31")
        assert result == []

    def test_load_skips_files_with_unparseable_name(self, isolated_raw_dir):
        """文件名不符合 {source}_{date} 格式的应被跳过。"""
        month_dir = isolated_raw_dir / "2026-08"
        month_dir.mkdir(parents=True)
        # 正常文件
        normal_file = month_dir / "cls_2026-08-18.json"
        with open(normal_file, "w", encoding="utf-8") as f:
            f.write(json.dumps(_make_item("正常", publish_date="2026-08-18")) + "\n")
        # 异常文件名（无日期分隔）
        bad_file = month_dir / "broken_filename.json"
        with open(bad_file, "w", encoding="utf-8") as f:
            f.write(json.dumps(_make_item("坏文件名", publish_date="2026-08-18")) + "\n")

        result = load_jsonl("2026-08-01", "2026-08-31")
        # 只加载正常文件
        assert len(result) == 1
        assert result[0]["title"] == "正常"

    def test_load_includes_both_boundary_days(self, isolated_raw_dir):
        """日期边界 [start_date, end_date] 应含两端。"""
        save_jsonl(
            [_make_item("起始日", publish_date="2026-08-01")], "cls", "2026-08-01"
        )
        save_jsonl(
            [_make_item("截止日", publish_date="2026-08-31")], "cls", "2026-08-31"
        )
        save_jsonl(
            [_make_item("中间", publish_date="2026-08-15")], "cls", "2026-08-15"
        )

        result = load_jsonl("2026-08-01", "2026-08-31")
        assert len(result) == 3
        assert result[0]["publish_date"] == "2026-08-01"
        assert result[2]["publish_date"] == "2026-08-31"


# ---------------------------------------------------------------------------
# list_news_files + count_jsonl
# ---------------------------------------------------------------------------


class TestListNewsFiles:
    def test_list_files_in_range(self, isolated_raw_dir):
        save_jsonl([_make_item("a", publish_date="2026-08-01")], "cls", "2026-08-01")
        save_jsonl([_make_item("b", publish_date="2026-08-17")], "cls", "2026-08-17")
        save_jsonl([_make_item("c", publish_date="2026-07-15")], "cls", "2026-07-15")

        files = list_news_files("2026-08-01", "2026-08-31")
        assert len(files) == 2
        for f in files:
            assert f.suffix == ".json"

    def test_list_files_filter_by_filename_date(self, isolated_raw_dir):
        """文件名 date 不在范围内的文件应被排除。"""
        save_jsonl([_make_item("a", publish_date="2026-08-01")], "cls", "2026-08-01")
        save_jsonl([_make_item("b", publish_date="2026-07-31")], "cls", "2026-07-31")

        files = list_news_files("2026-08-01", "2026-08-31")
        assert len(files) == 1
        assert "2026-08-01" in files[0].name


class TestCountJsonl:
    def test_existing_file(self, isolated_raw_dir, tmp_path):
        file_path = tmp_path / "test.jsonl"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write('{"a":1}\n{"b":2}\n{"c":3}\n')
        assert count_jsonl(file_path) == 3

    def test_nonexistent_file(self, tmp_path):
        assert count_jsonl(tmp_path / "no_such_file.json") == 0

    def test_skips_blank_lines(self, isolated_raw_dir, tmp_path):
        file_path = tmp_path / "test.jsonl"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write('{"a":1}\n\n{"b":2}\n   \n{"c":3}\n')
        assert count_jsonl(file_path) == 3


# ---------------------------------------------------------------------------
# 端到端：sources.py 输出 → tagger.py 标注 → save_jsonl 落盘
# ---------------------------------------------------------------------------


class TestEndToEndIntegration:
    """端到端验证 sources.py 输出 + tagger.py 标注后写入 storage 的链路。"""

    def test_sources_to_storage_with_tagging(self, isolated_raw_dir):
        """模拟 sources.py 输出的 dict 经 tagger 标注后写入 storage。"""
        from tagger import tag_news

        # 模拟 sources.py 产出的标准化 dict（巨潮接口示例）
        items = [
            {
                "source_url": "http://cninfo.example.com/1",
                "source_channel": "巨潮资讯",
                "source_short": "juchao",
                "publish_date": "2026-08-15",
                "publish_time": "10:00:00",
                "title": "董事会决议公告",
                "content": "",
                "mentioned_codes": ["000001"],
                "mentioned_names": ["平安银行"],
            },
            {
                "source_url": "http://cninfo.example.com/2",
                "source_channel": "巨潮资讯",
                "source_short": "juchao",
                "publish_date": "2026-08-15",
                "publish_time": "11:00:00",
                "title": "贵州茅台(600519)年报披露",
                "content": "贵州茅台2025年年报净利润同比增长15%。",
                "mentioned_codes": [],
                "mentioned_names": [],
            },
        ]
        # tagger 标注
        for item in items:
            tag_news(item)

        # 第二条应被 tagger 补全 mentioned_codes=["600519"]、names=["贵州茅台"]
        assert items[1]["mentioned_codes"] == ["600519"]
        assert items[1]["mentioned_names"] == ["贵州茅台"]

        # 写入 storage
        n = save_jsonl(items, "juchao", "2026-08-15")
        assert n == 2

        # 加载回来，字段应一致
        loaded = load_jsonl("2026-08-15", "2026-08-15", source="juchao")
        assert len(loaded) == 2
        # 验证第二条保留了 tagger 标注结果
        tagged_loaded = next(
            it for it in loaded if it["title"] == "贵州茅台(600519)年报披露"
        )
        assert tagged_loaded["mentioned_codes"] == ["600519"]
        assert tagged_loaded["mentioned_names"] == ["贵州茅台"]
        # 验证 news_id 已被 storage 自动补算
        assert "news_id" in tagged_loaded
        assert tagged_loaded["news_id"].startswith("juchao_2026-08-15_")
