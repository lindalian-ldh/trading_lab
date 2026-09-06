"""crawler_news 阶段五单元测试（main.py 主流程编排）。

覆盖（对齐 project.md 阶段五验收标准）：
    1. ``uv run main.py crawl --date 2026-08-17`` 成功产出 5 个 JSONL 文件
    2. 单接口失败时整体不中断，日志含失败接口清单
    3. 总耗时 < 60 秒（通过 mock 验证编排逻辑，真实耗时由 sources 限频保证）
    4. argparse 子命令解析（crawl / report 占位 / --date 默认昨天）
    5. ``--symbol`` 透传给支持个股过滤的接口
    6. ``--source`` 指定单一数据源
    7. 幂等：同日同源重复 crawl 两次，文件行数不增加
    8. 全部接口失败时 main() 返回 1

设计要点：
    - **完全隔离真实 akshare**：通过 ``monkeypatch`` 替换 ``sources.fetch_source``
      与 ``storage._RAW_DIR``，避免任何网络调用与真实数据污染
    - **隔离信号 alarm**：通过 ``monkeypatch`` 替换 ``signal.alarm``，
      防止 300s 计时器在测试结束后仍激活
    - 不访问网络

运行方式：
    cd trading_lab && uv run pytest services/crawler_news/test_main.py -v
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

# 让测试可以 import crawler_news 模块
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SERVICE_DIR = Path(__file__).resolve().parent

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
if str(_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICE_DIR))

import main as main_module
from main import build_parser, run_crawl, main as cli_main
from news_config import ALL_SOURCES, SOURCE_MAP
from storage import count_jsonl, _news_file_path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_raw_dir(tmp_path, monkeypatch):
    """重定向 storage._RAW_DIR 到临时目录，避免污染真实数据。"""
    import storage as storage_module

    new_dir = tmp_path / "raw" / "news"
    new_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage_module, "_RAW_DIR", new_dir)
    yield new_dir


@pytest.fixture
def stub_fetch_source(monkeypatch):
    """替换 sources.fetch_source 为可控的桩函数。

    用法：
        def test_xxx(stub_fetch_source):
            stub_fetch_source({
                "cls": [item1, item2],
                "sina": [item3],
                ...
            })
            # 此后 run_crawl 调用 cls/sina/... 时返回对应 items
    """
    state: dict = {"by_source": {}, "call_log": [], "raise_on": None}

    def _fake_fetch_source(src, target_date, symbol=None):
        state["call_log"].append(
            {
                "source_short": src.source_short,
                "target_date": target_date,
                "symbol": symbol,
            }
        )
        if state["raise_on"] and src.source_short in state["raise_on"]:
            raise RuntimeError(f"mock 失败: {src.source_short}")
        return list(state["by_source"].get(src.source_short, []))

    monkeypatch.setattr(main_module, "fetch_source", _fake_fetch_source)

    def _setup(by_source: dict, raise_on: set | None = None):
        state["by_source"] = by_source
        state["raise_on"] = raise_on or set()

    _setup.setup = _setup  # type: ignore[attr-defined]
    _setup.state = state  # type: ignore[attr-defined]
    return _setup


@pytest.fixture
def isolated_report_dir(tmp_path, monkeypatch):
    """重定向 reporter._REPORT_DIR 到临时目录，避免污染真实数据。"""
    import reporter as reporter_module

    new_dir = tmp_path / "reports" / "news"
    new_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(reporter_module, "_REPORT_DIR", new_dir)
    yield new_dir


@pytest.fixture
def disable_alarm(monkeypatch):
    """禁用 signal.alarm，避免测试结束后 300s 计时器仍激活。"""
    monkeypatch.setattr(main_module.signal, "alarm", lambda *_: None)
    yield


def _make_item(title: str, publish_date: str, source_short: str) -> dict:
    """构造标准化新闻 dict（对齐 sources.py 输出）。"""
    return {
        "source_url": f"http://example.com/{title}",
        "source_channel": "测试源",
        "source_short": source_short,
        "publish_date": publish_date,
        "publish_time": "10:00:00",
        "title": title,
        "content": "",
        "mentioned_codes": [],
        "mentioned_names": [],
    }


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------


class TestBuildParser:
    def test_crawl_default_date_is_yesterday(self):
        """未指定 --date 时默认昨天。"""
        args = build_parser().parse_args(["crawl"])
        expected = (date.today() - timedelta(days=1)).isoformat()
        assert args.date == expected

    def test_crawl_with_explicit_date(self):
        args = build_parser().parse_args(["crawl", "--date", "2026-08-17"])
        assert args.date == "2026-08-17"

    def test_crawl_with_symbol(self):
        args = build_parser().parse_args(
            ["crawl", "--symbol", "600519"]
        )
        assert args.symbol == "600519"

    def test_crawl_with_source(self):
        args = build_parser().parse_args(["crawl", "--source", "cls"])
        assert args.source == "cls"

    def test_crawl_invalid_source_rejected(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["crawl", "--source", "invalid"])

    def test_crawl_source_must_be_in_choices(self):
        """所有合法 source_short 都应被 --source 接受。"""
        for s in ["cls", "sina", "juchao", "em_global", "em_stock"]:
            args = build_parser().parse_args(["crawl", "--source", s])
            assert args.source == s

    def test_no_command_rejected(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_report_subcommand_placeholder(self):
        """report 子命令参数应可解析（阶段六实现具体逻辑）。"""
        args = build_parser().parse_args(
            [
                "report",
                "--start-date", "2026-08-01",
                "--end-date", "2026-08-17",
            ]
        )
        assert args.command == "report"
        assert args.start_date == "2026-08-01"
        assert args.end_date == "2026-08-17"

    def test_report_format_default_md(self):
        args = build_parser().parse_args(
            ["report", "--start-date", "2026-08-01", "--end-date", "2026-08-17"]
        )
        assert args.format == "md"

    def test_report_format_choices(self):
        for fmt in ["md", "html"]:
            args = build_parser().parse_args(
                [
                    "report",
                    "--start-date", "2026-08-01",
                    "--end-date", "2026-08-17",
                    "--format", fmt,
                ]
            )
            assert args.format == fmt

    def test_report_invalid_format_rejected(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(
                [
                    "report",
                    "--start-date", "2026-08-01",
                    "--end-date", "2026-08-17",
                    "--format", "pdf",
                ]
            )


# ---------------------------------------------------------------------------
# run_crawl 编排逻辑
# ---------------------------------------------------------------------------


class TestRunCrawlAllSources:
    """验收场景：crawl 全量产出 5 个 JSONL 文件。"""

    def test_crawl_all_sources_produces_5_files(
        self, isolated_raw_dir, stub_fetch_source
    ):
        """全量抓取 5 个数据源应产出 5 个独立 JSONL 文件。"""
        target_date = "2026-08-17"
        stub_fetch_source.setup(
            {
                "cls": [_make_item("财联1", target_date, "cls")],
                "sina": [_make_item("新浪1", target_date, "sina")],
                "juchao": [_make_item("巨潮1", target_date, "juchao")],
                "em_global": [_make_item("东财全球1", target_date, "em_global")],
                "em_stock": [_make_item("东财个股1", target_date, "em_stock")],
            }
        )

        result = run_crawl(target_date, symbol=None, source=None)

        # 验收：5 个 JSONL 文件
        files = list(isolated_raw_dir.rglob("*.json"))
        assert len(files) == 5
        # 每个文件以 source_short 命名前缀
        source_names = {f.stem.split("_")[0] for f in files}
        # 注意 em_global / em_stock 因含 _ 会分裂，用 stem 完整匹配
        actual_sources = {f.stem.rsplit("_", 1)[0] for f in files}
        assert actual_sources == {
            "cls", "sina", "juchao", "em_global", "em_stock"
        }

        # 汇总结果
        assert result["success_count"] == 5
        assert result["total_sources"] == 5
        assert result["total_items"] == 5
        assert result["total_written"] == 5
        assert result["failed_sources"] == []

    def test_crawl_returns_summary_dict_shape(
        self, isolated_raw_dir, stub_fetch_source
    ):
        """run_crawl 返回的 dict 应包含所有约定的汇总字段。"""
        stub_fetch_source.setup({"cls": []})
        result = run_crawl("2026-08-17", symbol=None, source="cls")
        expected_keys = {
            "target_date",
            "source",
            "symbol",
            "success_count",
            "total_sources",
            "total_items",
            "total_written",
            "failed_sources",
            "results",
            "duration_sec",
        }
        assert set(result.keys()) == expected_keys

    def test_crawl_idempotent_repeat_no_growth(
        self, isolated_raw_dir, stub_fetch_source
    ):
        """同日同源重复 crawl 两次，文件行数不增加（幂等验收）。"""
        target_date = "2026-08-17"
        stub_fetch_source.setup(
            {"cls": [_make_item("财联1", target_date, "cls")]}
        )

        # 第一次
        r1 = run_crawl(target_date, None, source="cls")
        assert r1["total_written"] == 1

        cls_file = _news_file_path("cls", target_date)
        assert count_jsonl(cls_file) == 1

        # 第二次：相同数据
        r2 = run_crawl(target_date, None, source="cls")
        assert r2["total_written"] == 0  # 幂等：全部跳过
        assert count_jsonl(cls_file) == 1  # 行数不增加


class TestRunCrawlSingleSource:
    def test_crawl_single_source_only_fetches_one(
        self, isolated_raw_dir, stub_fetch_source
    ):
        """--source cls 时只抓 cls，不调用其他接口。"""
        stub_fetch_source.setup({"cls": [_make_item("a", "2026-08-17", "cls")]})

        run_crawl("2026-08-17", None, source="cls")

        # 只 cls 被调用
        called_sources = {
            c["source_short"] for c in stub_fetch_source.state["call_log"]
        }
        assert called_sources == {"cls"}

    def test_crawl_single_source_produces_one_file(
        self, isolated_raw_dir, stub_fetch_source
    ):
        stub_fetch_source.setup(
            {"sina": [_make_item("s1", "2026-08-17", "sina")]}
        )
        run_crawl("2026-08-17", None, source="sina")
        files = list(isolated_raw_dir.rglob("*.json"))
        assert len(files) == 1
        assert files[0].name == "sina_2026-08-17.json"


class TestRunCrawlSymbolPassthrough:
    def test_symbol_passed_to_fetch_source(
        self, isolated_raw_dir, stub_fetch_source
    ):
        """--symbol 应透传给 fetch_source（不区分接口，由 sources.py 内部决定是否使用）。"""
        stub_fetch_source.setup(
            {"em_stock": [_make_item("个股新闻", "2026-08-17", "em_stock")]}
        )
        run_crawl("2026-08-17", symbol="600519", source="em_stock")

        calls = stub_fetch_source.state["call_log"]
        assert len(calls) == 1
        assert calls[0]["symbol"] == "600519"

    def test_symbol_none_when_not_specified(
        self, isolated_raw_dir, stub_fetch_source
    ):
        stub_fetch_source.setup({"cls": []})
        run_crawl("2026-08-17", symbol=None, source="cls")
        calls = stub_fetch_source.state["call_log"]
        assert calls[0]["symbol"] is None


# ---------------------------------------------------------------------------
# 单接口失败不阻断
# ---------------------------------------------------------------------------


class TestRunCrawlPartialFailure:
    """验收场景：单接口失败时整体不中断，日志含失败接口清单。"""

    def test_one_source_failure_does_not_block_others(
        self, isolated_raw_dir, stub_fetch_source
    ):
        """cls 抛异常，其他 4 个源仍应正常完成。"""
        target_date = "2026-08-17"
        stub_fetch_source.setup(
            {
                "cls": [],  # 不会被使用，因为 raise_on
                "sina": [_make_item("s1", target_date, "sina")],
                "juchao": [_make_item("j1", target_date, "juchao")],
                "em_global": [_make_item("eg1", target_date, "em_global")],
                "em_stock": [_make_item("es1", target_date, "em_stock")],
            },
            raise_on={"cls"},
        )

        result = run_crawl(target_date, None, source=None)

        # 4 个成功，1 个失败
        assert result["success_count"] == 4
        assert result["total_sources"] == 5
        assert result["failed_sources"] == ["cls"]
        # 失败的 cls 不应产出文件
        cls_file = _news_file_path("cls", target_date)
        assert not cls_file.exists()
        # 其他 4 个文件正常产出
        for src in ["sina", "juchao", "em_global", "em_stock"]:
            f = _news_file_path(src, target_date)
            assert f.exists(), f"{src} 文件应存在"

    def test_failed_source_recorded_in_results(
        self, isolated_raw_dir, stub_fetch_source
    ):
        stub_fetch_source.setup({"cls": []}, raise_on={"cls"})
        result = run_crawl("2026-08-17", None, source="cls")
        assert result["results"]["cls"]["status"] == "failed"
        assert "mock 失败" in result["results"]["cls"]["error"]

    def test_all_sources_failure_returns_empty_results(
        self, isolated_raw_dir, stub_fetch_source
    ):
        """全部源失败时汇总数据应为 0。"""
        stub_fetch_source.setup({}, raise_on={"cls"})
        result = run_crawl("2026-08-17", None, source="cls")
        assert result["success_count"] == 0
        assert result["total_items"] == 0
        assert result["failed_sources"] == ["cls"]


# ---------------------------------------------------------------------------
# main() 入口
# ---------------------------------------------------------------------------


class TestMain:
    def test_main_crawl_success_returns_zero(
        self, isolated_raw_dir, stub_fetch_source, disable_alarm
    ):
        stub_fetch_source.setup(
            {"cls": [_make_item("a", "2026-08-17", "cls")]}
        )
        exit_code = cli_main(["crawl", "--date", "2026-08-17", "--source", "cls"])
        assert exit_code == 0

    def test_main_crawl_all_failed_returns_one(
        self, isolated_raw_dir, stub_fetch_source, disable_alarm
    ):
        stub_fetch_source.setup({}, raise_on={"cls"})
        exit_code = cli_main(["crawl", "--date", "2026-08-17", "--source", "cls"])
        assert exit_code == 1

    def test_main_crawl_partial_failure_returns_zero(
        self, isolated_raw_dir, stub_fetch_source, disable_alarm
    ):
        """部分接口失败但有数据落盘时仍返回 0。"""
        target_date = "2026-08-17"
        stub_fetch_source.setup(
            {
                "cls": [],  # raise
                "sina": [_make_item("s1", target_date, "sina")],
                "juchao": [_make_item("j1", target_date, "juchao")],
                "em_global": [_make_item("eg1", target_date, "em_global")],
                "em_stock": [_make_item("es1", target_date, "em_stock")],
            },
            raise_on={"cls"},
        )
        exit_code = cli_main(["crawl", "--date", target_date])
        assert exit_code == 0  # 4/5 成功 → 0

    def test_main_report_success_returns_zero(
        self, isolated_raw_dir, isolated_report_dir, disable_alarm
    ):
        """阶段六：report 子命令成功生成报告应返回 0。"""
        from storage import save_jsonl

        target_date = "2026-08-17"
        save_jsonl(
            [_make_item("测试报告", target_date, "cls")], "cls", target_date
        )

        exit_code = cli_main(
            [
                "report",
                "--start-date", target_date,
                "--end-date", target_date,
            ]
        )
        assert exit_code == 0
        # 报告文件应存在
        expected = (
            isolated_report_dir / "2026-08"
            / f"{target_date}_{target_date}_all.md"
        )
        assert expected.exists()

    def test_main_report_with_symbol_filter(
        self, isolated_raw_dir, isolated_report_dir, disable_alarm
    ):
        """--symbol 过滤应在报告路径与内容中体现。"""
        from storage import save_jsonl

        target_date = "2026-08-17"
        items = [
            {
                "source_url": "http://example.com/1",
                "source_channel": "财联社",
                "source_short": "cls",
                "publish_date": target_date,
                "publish_time": "10:00:00",
                "title": "贵州茅台涨停",
                "content": "",
                "mentioned_codes": ["600519"],
                "mentioned_names": ["贵州茅台"],
            },
            {
                "source_url": "http://example.com/2",
                "source_channel": "财联社",
                "source_short": "cls",
                "publish_date": target_date,
                "publish_time": "11:00:00",
                "title": "平安银行公告",
                "content": "",
                "mentioned_codes": ["000001"],
                "mentioned_names": ["平安银行"],
            },
        ]
        save_jsonl(items, "cls", target_date)

        exit_code = cli_main(
            [
                "report",
                "--start-date", target_date,
                "--end-date", target_date,
                "--symbol", "600519",
            ]
        )
        assert exit_code == 0
        # 文件名含 symbol
        report_file = (
            isolated_report_dir / "2026-08"
            / f"{target_date}_{target_date}_600519.md"
        )
        assert report_file.exists()
        # 内容应只含 600519 相关的 1 条
        content = report_file.read_text(encoding="utf-8")
        assert "贵州茅台涨停" in content
        assert "平安银行公告" not in content

    def test_main_report_empty_storage_returns_zero(
        self, isolated_raw_dir, isolated_report_dir, disable_alarm
    ):
        """无数据时 report 子命令仍应返回 0（产出空报告）。"""
        exit_code = cli_main(
            [
                "report",
                "--start-date", "2026-08-01",
                "--end-date", "2026-08-17",
            ]
        )
        assert exit_code == 0
        # 报告文件仍应被创建
        assert (
            isolated_report_dir / "2026-08"
            / "2026-08-01_2026-08-17_all.md"
        ).exists()

    def test_main_report_invalid_format_returns_one(
        self, isolated_raw_dir, isolated_report_dir, disable_alarm
    ):
        """argparse 已在 choices 层拒绝非法 format，所以此处主要验证 choices 工作。"""
        with pytest.raises(SystemExit):
            cli_main(
                [
                    "report",
                    "--start-date", "2026-08-01",
                    "--end-date", "2026-08-17",
                    "--format", "pdf",
                ]
            )

    def test_main_no_command_exits_nonzero(self, disable_alarm):
        with pytest.raises(SystemExit):
            cli_main([])


# ---------------------------------------------------------------------------
# 端到端：sources→tagger→storage 完整链路（mock fetch_source）
# ---------------------------------------------------------------------------


class TestEndToEndMainFlow:
    """验证 main.py 编排的 sources→tagger→storage 完整链路。"""

    def test_full_pipeline_writes_tagged_items(
        self, isolated_raw_dir, stub_fetch_source
    ):
        """抓取未标注的 items → tagger 标注 → storage 落盘后字段应一致。"""
        target_date = "2026-08-17"
        # 构造未标注的 item（mentioned_codes/names 为空，靠 tagger 补全）
        raw_item = {
            "source_url": "http://example.com/1",
            "source_channel": "财联社",
            "source_short": "cls",
            "publish_date": target_date,
            "publish_time": "10:00:00",
            "title": "贵州茅台(600519)今日涨停",
            "content": "",
            "mentioned_codes": [],
            "mentioned_names": [],
        }
        stub_fetch_source.setup({"cls": [raw_item]})

        run_crawl(target_date, None, source="cls")

        # 验证文件中确实写入了 tagger 标注后的字段
        cls_file = _news_file_path("cls", target_date)
        import json

        with open(cls_file, "r", encoding="utf-8") as f:
            obj = json.loads(f.readline())
        # tagger 应补全 mentioned_codes=["600519"]、names=["贵州茅台"]
        assert obj["mentioned_codes"] == ["600519"]
        assert obj["mentioned_names"] == ["贵州茅台"]
        # storage 应自动补算 news_id
        assert "news_id" in obj
        assert obj["news_id"].startswith("cls_2026-08-17_")

    def test_full_pipeline_all_sources_separate_files(
        self, isolated_raw_dir, stub_fetch_source
    ):
        """全量抓取 5 个数据源，每个源独立文件，互不串数据。"""
        target_date = "2026-08-17"
        stub_fetch_source.setup(
            {
                "cls": [_make_item("财联新闻", target_date, "cls")],
                "sina": [_make_item("新浪新闻", target_date, "sina")],
                "juchao": [_make_item("巨潮公告", target_date, "juchao")],
                "em_global": [_make_item("东财全球", target_date, "em_global")],
                "em_stock": [_make_item("东财个股", target_date, "em_stock")],
            }
        )
        run_crawl(target_date, None, source=None)

        # 验证每个文件只含对应源的 1 条数据
        import json

        for src in ["cls", "sina", "juchao", "em_global", "em_stock"]:
            f = _news_file_path(src, target_date)
            assert f.exists(), f"{src} 文件应存在"
            with open(f, "r", encoding="utf-8") as fp:
                lines = fp.readlines()
            assert len(lines) == 1
            obj = json.loads(lines[0])
            assert obj["source_short"] == src


# ---------------------------------------------------------------------------
# 性能预算（编排逻辑层面）
# ---------------------------------------------------------------------------


class TestPerformanceBudget:
    """验收场景：总耗时 < 60 秒（mock 下应远低于此）。"""

    def test_crawl_duration_under_60s_with_mocks(
        self, isolated_raw_dir, stub_fetch_source
    ):
        """mock 下 run_crawl 应在毫秒级完成；真实耗时由 sources 限频保证。"""
        target_date = "2026-08-17"
        stub_fetch_source.setup(
            {
                "cls": [_make_item("c1", target_date, "cls")],
                "sina": [_make_item("s1", target_date, "sina")],
                "juchao": [_make_item("j1", target_date, "juchao")],
                "em_global": [_make_item("eg1", target_date, "em_global")],
                "em_stock": [_make_item("es1", target_date, "em_stock")],
            }
        )
        result = run_crawl(target_date, None, source=None)
        # mock 下应远低于 60s（实测通常 < 0.1s）
        assert result["duration_sec"] < 60
