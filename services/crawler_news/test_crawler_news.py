"""crawler_news 阶段七跨层集成测试。

覆盖（对齐 project.md 阶段七验收标准）：
    1. 单接口字段映射正确性（mock _call_akshare 注入合成 DataFrame，
       经 sources._row_to_standard 标准化后字段正确）
    2. 股票标注命中与去重（crawl 全流程跑完后 mentioned_codes/names 生效）
    3. 幂等写入（重复 run_crawl 不膨胀 JSONL 行数）
    4. 报告渲染正确性（generate_report 产出含数据源统计 + Top10 + Top5）
    5. 失败降级不阻断（部分源抛异常时 run_crawl 不中断、report 仍可产出）

设计要点：
    - **mock ``sources._call_akshare``**：按 ``func_name`` 返回对应列名的合成
      DataFrame，复用 sources 层真实的字段映射逻辑（已在 test_sources.py
      单测过，此处验证其在主流程串联中的端到端行为）
    - **隔离数据目录**：``isolated_raw_dir`` + ``isolated_report_dir`` 把
      ``storage._RAW_DIR`` 与 ``reporter._REPORT_DIR`` 重定向到 ``tmp_path``
    - **不访问网络**：所有 akshare 调用被 mock 拦截
    - 跨层串联：``main.run_crawl`` → ``sources.fetch_source`` →
      ``tagger.tag_news_batch`` → ``storage.save_jsonl`` →
      ``reporter.generate_report``

运行方式：
    cd trading_lab && uv run pytest services/crawler_news/test_crawler_news.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

# 让测试可以 import crawler_news 模块
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SERVICE_DIR = Path(__file__).resolve().parent

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
if str(_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICE_DIR))

import sources as sources_module
import storage as storage_module
import reporter as reporter_module
import main as main_module
from main import run_crawl, main
from news_config import ALL_SOURCES, SOURCE_MAP

TARGET_DATE = "2026-08-17"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_raw_dir(tmp_path, monkeypatch):
    """重定向 storage._RAW_DIR 到 tmp_path 子目录。"""
    new_dir = tmp_path / "raw" / "news"
    new_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage_module, "_RAW_DIR", new_dir)
    yield new_dir


@pytest.fixture
def isolated_report_dir(tmp_path, monkeypatch):
    """重定向 reporter._REPORT_DIR 到 tmp_path 子目录。"""
    new_dir = tmp_path / "reports" / "news"
    new_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(reporter_module, "_REPORT_DIR", new_dir)
    yield new_dir


def _df_cls() -> pd.DataFrame:
    """财联社：['标题', '内容', '发布日期', '发布时间']"""
    return pd.DataFrame(
        {
            "标题": ["贵州茅台(600519)涨停", "央行发布新规"],
            "内容": [
                "财联社8月17日电，贵州茅台(600519)今日涨停。",
                "财联社8月17日电，央行今日发布新规。",
            ],
            "发布日期": ["2026-08-17", "2026-08-17"],
            "发布时间": ["10:00:00", "11:00:00"],
        }
    )


def _df_sina() -> pd.DataFrame:
    """新浪：['时间', '内容']"""
    return pd.DataFrame(
        {
            "时间": ["2026-08-17 10:30:00", "2026-08-17 11:30:00"],
            "内容": [
                "【平安银行(000001)发布年报】净利润同比增长10%。",
                "【宁德时代(300750)新能源大涨】锂电池板块走强。",
            ],
        }
    )


def _df_juchao() -> pd.DataFrame:
    """巨潮：['代码', '简称', '公告标题', '公告时间', '公告链接']"""
    return pd.DataFrame(
        {
            "代码": ["600519", "000001"],
            "简称": ["贵州茅台", "平安银行"],
            "公告标题": ["贵州茅台2025年年度报告", "平安银行关于董事会决议的公告"],
            "公告时间": ["2026-08-17 09:00:00", "2026-08-17 12:00:00"],
            "公告链接": [
                "http://www.cninfo.com.cn/announcement1.html",
                "http://www.cninfo.com.cn/announcement2.html",
            ],
        }
    )


def _df_em_global() -> pd.DataFrame:
    """东财泛资讯：['标题', '摘要', '发布时间', '链接']"""
    return pd.DataFrame(
        {
            "标题": ["贵州茅台股价创新高", "新能源汽车板块异动"],
            "摘要": [
                "【贵州茅台(600519)股价创新高】白酒龙头走强...",
                "【宁德时代(300750)带领新能源板块异动】锂电池需求旺盛...",
            ],
            "发布时间": ["2026-08-17 14:00:00", "2026-08-17 15:00:00"],
            "链接": [
                "https://finance.eastmoney.com/a/202608171.html",
                "https://finance.eastmoney.com/a/202608172.html",
            ],
        }
    )


def _df_em_stock() -> pd.DataFrame:
    """东财个股：['关键词', '新闻标题', '新闻内容', '发布时间', '文章来源', '新闻链接']"""
    return pd.DataFrame(
        {
            "关键词": ["600519", "600519"],
            "新闻标题": ["贵州茅台年报超预期", "茅台机构调研纪要"],
            "新闻内容": [
                "贵州茅台(600519)年报净利润超预期，券商上调评级。",
                "近期多家机构调研贵州茅台(600519)，关注新品发售计划。",
            ],
            "发布时间": ["2026-08-17 13:00:00", "2026-08-17 16:00:00"],
            "文章来源": ["证券时报", "中国证券报"],
            "新闻链接": [
                "https://finance.eastmoney.com/a/stock1.html",
                "https://finance.eastmoney.com/a/stock2.html",
            ],
        }
    )


def _akshare_side_effect(func_name: str, **kwargs) -> pd.DataFrame:
    """根据 func_name 返回对应合成 DataFrame。"""
    mapping = {
        "stock_info_global_cls": _df_cls,
        "stock_info_global_sina": _df_sina,
        "stock_zh_a_disclosure_report_cninfo": _df_juchao,
        "stock_info_global_em": _df_em_global,
        "stock_news_em": _df_em_stock,
    }
    factory = mapping.get(func_name)
    if factory is None:
        return pd.DataFrame()
    return factory()


def _akshare_side_effect_with_cls_failure(func_name: str, **kwargs) -> pd.DataFrame:
    """cls 抛异常，其余源正常返回。"""
    if func_name == "stock_info_global_cls":
        raise Exception("cls network down")
    return _akshare_side_effect(func_name, **kwargs)


def _akshare_side_effect_all_failure(func_name: str, **kwargs) -> pd.DataFrame:
    """所有源都抛异常。"""
    raise Exception(f"{func_name} network down")


# ---------------------------------------------------------------------------
# 1. 全流程端到端：crawl → tagger → storage → report
# ---------------------------------------------------------------------------


class TestFullPipelineCrawlToReport:
    """端到端：5 源抓取 → 标注 → 落盘 → 报告生成。"""

    def test_crawl_produces_five_jsonl_files(
        self, isolated_raw_dir, isolated_report_dir
    ):
        """crawl 全源抓取后应产出 5 个 JSONL 文件（每源一个）。"""
        with patch("sources._call_akshare", side_effect=_akshare_side_effect):
            result = run_crawl(TARGET_DATE, symbol=None, source=None)

        # 5 个源全部成功
        assert result["success_count"] == 5
        assert result["total_sources"] == 5
        assert result["failed_sources"] == []
        # 总条数 = 2+2+2+2+2 = 10
        assert result["total_items"] == 10
        assert result["total_written"] == 10

        # 校验 5 个 JSONL 文件落盘
        month_dir = isolated_raw_dir / "2026-08"
        for src in ALL_SOURCES:
            f = month_dir / f"{src.source_short}_{TARGET_DATE}.json"
            assert f.exists(), f"缺少文件: {f}"

    def test_tagger_annotation_takes_effect(self, isolated_raw_dir, isolated_report_dir):
        """crawl 后 tagger 应在落盘数据中写入 mentioned_codes/names。"""
        with patch("sources._call_akshare", side_effect=_akshare_side_effect):
            run_crawl(TARGET_DATE, symbol=None, source=None)

        # 读 cls 文件，校验 600519 被标注
        cls_file = isolated_raw_dir / "2026-08" / f"cls_{TARGET_DATE}.json"
        items = [json.loads(line) for line in cls_file.read_text().splitlines() if line]
        moutai_item = next(it for it in items if "600519" in it["title"])
        assert "600519" in moutai_item["mentioned_codes"]
        assert "贵州茅台" in moutai_item["mentioned_names"]

        # 巨潮源应自带代码标注（接口返回 代码/简称 列）
        juchao_file = isolated_raw_dir / "2026-08" / f"juchao_{TARGET_DATE}.json"
        juchao_items = [
            json.loads(line) for line in juchao_file.read_text().splitlines() if line
        ]
        assert any("600519" in it["mentioned_codes"] for it in juchao_items)
        assert any("000001" in it["mentioned_codes"] for it in juchao_items)

    def test_idempotent_write_no_bloat(self, isolated_raw_dir, isolated_report_dir):
        """重复 run_crawl 两次，JSONL 行数不增加（幂等）。"""
        with patch("sources._call_akshare", side_effect=_akshare_side_effect):
            first = run_crawl(TARGET_DATE, symbol=None, source=None)
            second = run_crawl(TARGET_DATE, symbol=None, source=None)

        # 第一次写入 10 条，第二次因 news_id 幂等全部跳过
        assert first["total_written"] == 10
        assert second["total_written"] == 0

        # 行数不增加
        cls_file = isolated_raw_dir / "2026-08" / f"cls_{TARGET_DATE}.json"
        first_lines = len(
            [ln for ln in cls_file.read_text().splitlines() if ln]
        )
        # 重新读取（第二次后）
        second_lines = len(
            [ln for ln in cls_file.read_text().splitlines() if ln]
        )
        assert first_lines == second_lines == 2

    def test_report_contains_required_sections(
        self, isolated_raw_dir, isolated_report_dir
    ):
        """crawl 后 generate_report 产出含数据源统计 + Top10 + Top5。"""
        with patch("sources._call_akshare", side_effect=_akshare_side_effect):
            run_crawl(TARGET_DATE, symbol=None, source=None)

        result = main_module.run_report(
            "2026-08-17", "2026-08-17", symbol=None, fmt="md"
        )
        content = Path(result["path"]).read_text(encoding="utf-8")

        # 数据源统计段（5 个源各 2 条）
        assert "## 数据源统计" in content
        for src in ALL_SOURCES:
            assert f"| {src.source_short} | 2 |" in content
        # Top 10 新闻段
        assert "## Top 10 新闻" in content
        # Top 5 股票代码段（600519 被提及最多：cls×1 + juchao×1 + em_global×1 + em_stock×2 = 5）
        assert "## 被提及最多的 5 个股票代码" in content
        assert "| 600519 | 贵州茅台 |" in content
        # 总条数
        assert result["total_items"] == 10


# ---------------------------------------------------------------------------
# 2. 失败降级不阻断
# ---------------------------------------------------------------------------


class TestFailureDegradation:
    """部分源失败时主流程不中断，report 仍可产出。"""

    def test_partial_failure_does_not_block(
        self, isolated_raw_dir, isolated_report_dir
    ):
        """cls 抛异常时，其余 4 源仍正常落盘，failed_sources 含 cls。"""
        with patch(
            "sources._call_akshare",
            side_effect=_akshare_side_effect_with_cls_failure,
        ):
            result = run_crawl(TARGET_DATE, symbol=None, source=None)

        # 4 个源成功，cls 失败
        assert result["success_count"] == 4
        assert result["failed_sources"] == ["cls"]
        # 仍有数据落盘（8 条）
        assert result["total_items"] == 8
        assert result["total_written"] == 8

        # cls 文件不存在（失败未落盘），其余 4 个文件存在
        month_dir = isolated_raw_dir / "2026-08"
        assert not (month_dir / f"cls_{TARGET_DATE}.json").exists()
        for src_short in ["sina", "juchao", "em_global", "em_stock"]:
            assert (month_dir / f"{src_short}_{TARGET_DATE}.json").exists()

    def test_report_still_produces_after_partial_failure(
        self, isolated_raw_dir, isolated_report_dir
    ):
        """部分源失败后，report 仍能基于已落盘数据产出。"""
        with patch(
            "sources._call_akshare",
            side_effect=_akshare_side_effect_with_cls_failure,
        ):
            run_crawl(TARGET_DATE, symbol=None, source=None)

        result = main_module.run_report(
            "2026-08-17", "2026-08-17", symbol=None, fmt="md"
        )
        content = Path(result["path"]).read_text(encoding="utf-8")

        # 报告含 4 个数据源（cls 缺失）
        assert "## 数据源统计" in content
        assert "| cls |" not in content  # cls 失败无数据
        for src_short in ["sina", "juchao", "em_global", "em_stock"]:
            assert f"| {src_short} | 2 |" in content
        assert result["total_items"] == 8


# ---------------------------------------------------------------------------
# 3. report symbol 过滤端到端
# ---------------------------------------------------------------------------


class TestReportSymbolFilterE2E:
    """generate_report(symbol=...) 端到端过滤。"""

    def test_symbol_filter_only_returns_matched_items(
        self, isolated_raw_dir, isolated_report_dir
    ):
        """report --symbol 600519 仅返回 mentioned_codes 含 600519 的条目。"""
        with patch("sources._call_akshare", side_effect=_akshare_side_effect):
            run_crawl(TARGET_DATE, symbol=None, source=None)

        # 全量 vs 过滤
        all_result = main_module.run_report(
            "2026-08-17", "2026-08-17", symbol=None, fmt="md"
        )
        filtered = main_module.run_report(
            "2026-08-17", "2026-08-17", symbol="600519", fmt="md"
        )

        assert all_result["total_items"] == 10
        # 600519 出现在：cls×1 + juchao×1 + em_global×1 + em_stock×2 = 5
        assert filtered["total_items"] == 5
        assert filtered["total_items"] < all_result["total_items"]

        # 报告文件名含 symbol
        assert "600519" in filtered["path"]


# ---------------------------------------------------------------------------
# 4. CLI 入口集成测试
# ---------------------------------------------------------------------------


class TestCLIEntryPoint:
    """通过 main(argv) 验证 CLI 子命令退出码。"""

    def test_cli_crawl_exit_zero(
        self, isolated_raw_dir, isolated_report_dir
    ):
        """main(['crawl', '--date', ...]) 正常返回 0。"""
        with patch("sources._call_akshare", side_effect=_akshare_side_effect):
            code = main(["crawl", "--date", TARGET_DATE])
        assert code == 0

    def test_cli_crawl_single_source_exit_zero(
        self, isolated_raw_dir, isolated_report_dir
    ):
        """main(['crawl', '--source', 'cls', ...]) 单源模式返回 0。"""
        with patch("sources._call_akshare", side_effect=_akshare_side_effect):
            code = main(["crawl", "--date", TARGET_DATE, "--source", "cls"])
        assert code == 0
        # 仅 cls 文件落盘
        month_dir = isolated_raw_dir / "2026-08"
        assert (month_dir / f"cls_{TARGET_DATE}.json").exists()
        assert not (month_dir / f"sina_{TARGET_DATE}.json").exists()

    def test_cli_crawl_with_symbol_exit_zero(
        self, isolated_raw_dir, isolated_report_dir
    ):
        """main(['crawl', '--symbol', '600519', ...]) 透传 symbol 返回 0。"""
        with patch("sources._call_akshare", side_effect=_akshare_side_effect):
            code = main(["crawl", "--date", TARGET_DATE, "--symbol", "600519"])
        assert code == 0

    def test_cli_report_exit_zero(
        self, isolated_raw_dir, isolated_report_dir
    ):
        """main(['report', ...]) 基于已落盘数据返回 0。"""
        # 先 crawl 落盘
        with patch("sources._call_akshare", side_effect=_akshare_side_effect):
            run_crawl(TARGET_DATE, symbol=None, source=None)
        # 再 report
        code = main(
            ["report", "--start-date", TARGET_DATE, "--end-date", TARGET_DATE]
        )
        assert code == 0
        # 校验产出文件
        report_file = (
            isolated_report_dir
            / "2026-08"
            / f"{TARGET_DATE}_{TARGET_DATE}_all.md"
        )
        assert report_file.exists()

    def test_cli_report_html_format_exit_zero(
        self, isolated_raw_dir, isolated_report_dir
    ):
        """main(['report', '--format', 'html', ...]) 应正常返回 0，并产出 .html。"""
        # 先 crawl 落盘
        with patch("sources._call_akshare", side_effect=_akshare_side_effect):
            run_crawl(TARGET_DATE, symbol=None, source=None)
        # 再 report --format html
        code = main(
            [
                "report",
                "--start-date", TARGET_DATE,
                "--end-date", TARGET_DATE,
                "--format", "html",
            ]
        )
        assert code == 0
        # 校验 .html 文件存在，且内容是合法 HTML5 文档
        html_file = (
            isolated_report_dir
            / "2026-08"
            / f"{TARGET_DATE}_{TARGET_DATE}_all.html"
        )
        assert html_file.exists()
        content = html_file.read_text(encoding="utf-8")
        assert content.startswith("<!DOCTYPE html>")
        assert "<html lang=\"zh-CN\">" in content
        assert "<table>" in content

    def test_cli_report_invalid_format_exit_two(
        self, isolated_raw_dir, isolated_report_dir
    ):
        """argparse choices 对未知 --format 返回 SystemExit(2)。"""
        # 数据落盘与否不影响（parse_args 层就会失败）
        with patch("sources._call_akshare", side_effect=_akshare_side_effect):
            run_crawl(TARGET_DATE, symbol=None, source=None)

        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "report",
                    "--start-date", TARGET_DATE,
                    "--end-date", TARGET_DATE,
                    "--format", "pdf",
                ]
            )
        assert exc_info.value.code == 2

    def test_cli_report_source_filter_exit_zero(
        self, isolated_raw_dir, isolated_report_dir
    ):
        """main(['report', '--source', 'cls', ...]) 仅产出 cls 源的 .html。"""
        # 先 crawl 全量落盘（5 个源各 1 个文件）
        with patch("sources._call_akshare", side_effect=_akshare_side_effect):
            run_crawl(TARGET_DATE, symbol=None, source=None)

        # report --source cls --format html
        code = main(
            [
                "report",
                "--start-date", TARGET_DATE,
                "--end-date", TARGET_DATE,
                "--source", "cls",
                "--format", "html",
            ]
        )
        assert code == 0
        # 文件名应含 _cls.html
        html_file = (
            isolated_report_dir
            / "2026-08"
            / f"{TARGET_DATE}_{TARGET_DATE}_cls.html"
        )
        assert html_file.exists()
        content = html_file.read_text(encoding="utf-8")
        assert content.startswith("<!DOCTYPE html>")
        # HTML 头部应含数据源过滤标签
        assert "<strong>数据源过滤</strong>: cls" in content

    def test_cli_crawl_all_failed_exit_one(
        self, isolated_raw_dir, isolated_report_dir
    ):
        """全部源失败时 main(['crawl', ...]) 返回 1。"""
        with patch(
            "sources._call_akshare",
            side_effect=_akshare_side_effect_all_failure,
        ):
            code = main(["crawl", "--date", TARGET_DATE])
        assert code == 1

    def test_cli_all_crawl_and_report_exit_zero(
        self, isolated_raw_dir, isolated_report_dir
    ):
        """main(['all', ...]) 一键 crawl → report，返回 0 并产出 .html。"""
        with patch("sources._call_akshare", side_effect=_akshare_side_effect):
            code = main(
                [
                    "all",
                    "--date", TARGET_DATE,
                    "--format", "html",
                ]
            )
        assert code == 0
        # crawl 落盘的 JSONL 存在
        jsonl = isolated_raw_dir / "2026-08" / f"cls_{TARGET_DATE}.json"
        assert jsonl.exists()
        # report 产出的 HTML 存在（start=end=TARGET_DATE）
        html = (
            isolated_report_dir / "2026-08"
            / f"{TARGET_DATE}_{TARGET_DATE}_all.html"
        )
        assert html.exists()
        content = html.read_text(encoding="utf-8")
        assert content.startswith("<!DOCTYPE html>")

    def test_cli_all_single_source_html(
        self, isolated_raw_dir, isolated_report_dir
    ):
        """main(['all', '--source', 'cls', ...]) 单源一键产出 _cls.html。"""
        with patch("sources._call_akshare", side_effect=_akshare_side_effect):
            code = main(
                [
                    "all",
                    "--date", TARGET_DATE,
                    "--source", "cls",
                    "--format", "html",
                ]
            )
        assert code == 0
        html = (
            isolated_report_dir / "2026-08"
            / f"{TARGET_DATE}_{TARGET_DATE}_cls.html"
        )
        assert html.exists()
        content = html.read_text(encoding="utf-8")
        assert "<strong>数据源过滤</strong>: cls" in content

    def test_cli_report_failure_exit_one(
        self, isolated_raw_dir, isolated_report_dir
    ):
        """report 内部异常时 main 返回 1（覆盖 report 异常退出分支）。"""
        with patch("sources._call_akshare", side_effect=_akshare_side_effect):
            run_crawl(TARGET_DATE, symbol=None, source=None)
        # mock generate_report 抛异常，触发 main 的 except 分支返回 1
        with patch("main.generate_report", side_effect=Exception("render fail")):
            code = main(
                [
                    "report",
                    "--start-date",
                    TARGET_DATE,
                    "--end-date",
                    TARGET_DATE,
                ]
            )
        assert code == 1
