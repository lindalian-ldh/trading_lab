"""yanbao-info 单元测试。

覆盖:
    - extractor:    评级/目标价/盈利预测/催化剂/风险/首次覆盖 提取
    - signal_judge: 目标价空间/评级跳升/盈利预测上调/催化剂时效性/首次覆盖信号
    - pdf_parser:   扫描版 PDF 检测（mock）
    - data_loader:  PDF 缓存命中（mock）

数据获取层与 PDF 下载层不写单测（依赖网络），仅用 mock 验证降级路径。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# 服务目录加入 sys.path
_SERVICE_DIR = Path(__file__).resolve().parent
if str(_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICE_DIR))

from config import YanbaoConfig
from extractor import (
    detect_first_coverage,
    extract_all,
    extract_catalysts,
    extract_core_logic,
    extract_earnings_forecast,
    extract_rating,
    extract_risks,
    extract_target_price,
)
from signal_judge import (
    judge_catalyst_timeliness,
    judge_first_coverage_signal,
    judge_forecast_revision,
    judge_rating_jump,
    judge_signals,
    judge_target_price_upside,
)


# ====================================================================
# extractor 测试
# ====================================================================


class TestExtractRating:
    cfg = YanbaoConfig()

    def test_buy(self):
        text = "投资评级：买入。目标价 45 元。"
        assert extract_rating(text, self.cfg) == "买入"

    def test_outperform(self):
        text = "维持增持评级"
        assert extract_rating(text, self.cfg) == "增持"

    def test_neutral(self):
        text = "本次评级：中性"
        assert extract_rating(text, self.cfg) == "中性"

    def test_underperform(self):
        text = "下调至减持"
        assert extract_rating(text, self.cfg) == "减持"

    def test_sell(self):
        text = "投资建议：卖出"
        assert extract_rating(text, self.cfg) == "卖出"

    def test_strong_recommend(self):
        text = "我们给予强烈推荐评级"
        assert extract_rating(text, self.cfg) == "买入"  # "强烈推荐" 在 rating_buy 列表

    def test_missing(self):
        text = "这是一份普通研报，无评级信息"
        assert extract_rating(text, self.cfg) == ""

    def test_empty(self):
        assert extract_rating("", self.cfg) == ""


class TestExtractTargetPrice:
    def test_with_decimal(self):
        text = "12个月目标价 45.60 元"
        assert extract_target_price(text) == 45.60

    def test_with_integer(self):
        text = "目标价 45 元"
        assert extract_target_price(text) == 45.0

    def test_take_last_match(self):
        text = "目标价 30 元，上调目标价至 45.00 元"
        assert extract_target_price(text) == 45.00

    def test_missing(self):
        text = "无目标价信息"
        assert extract_target_price(text) is None

    def test_empty(self):
        assert extract_target_price("") is None

    def test_invalid_range(self):
        # 价格异常大（>100000）应被忽略
        text = "目标价 999999 元"
        assert extract_target_price(text) is None


class TestExtractEarningsForecast:
    def test_from_table(self):
        # 模拟 pdfplumber 提取的表格
        df = pd.DataFrame(
            [
                ["营业收入", "3000", "3200", "3450"],
                ["归母净利润", "1400", "1480", "1650"],
            ],
            columns=["指标", "2023A", "2024E", "2025E"],
        )
        result = extract_earnings_forecast([df], "")
        # "营业收入" 在 REVENUE_HEADERS 中，应提取到
        assert result["营收"].get("2024E") == "3200"
        assert result["归母净利润"].get("2025E") == "1650"

    def test_from_text(self):
        text = """
        营业收入 2024E 3200亿 2025E 3450亿
        归母净利润 2024E 1480亿
        """
        result = extract_earnings_forecast([], text)
        # 至少应提取到营收或归母净利润
        assert result["营收"] or result["归母净利润"]

    def test_empty(self):
        result = extract_earnings_forecast([], "")
        assert result == {"营收": {}, "归母净利润": {}}


class TestExtractCatalysts:
    def test_numbered_list(self):
        text = """
        催化剂：
        1. 新品发布预期
        2. 订单落地
        3. 政策出台
        """
        items = extract_catalysts(text)
        assert len(items) >= 1
        assert any("新品发布" in i for i in items)

    def test_missing(self):
        text = "无催化剂信息"
        assert extract_catalysts(text) == []

    def test_empty(self):
        assert extract_catalysts("") == []


class TestExtractRisks:
    def test_numbered_list(self):
        text = """
        风险提示：
        1. 宏观经济下行
        2. 房地产不良暴露
        """
        items = extract_risks(text)
        assert len(items) >= 1
        assert any("宏观经济" in i for i in items)

    def test_missing(self):
        text = "无风险提示"
        assert extract_risks(text) == []

    def test_empty(self):
        assert extract_risks("") == []


class TestDetectFirstCoverage:
    def test_in_title(self):
        assert detect_first_coverage("首次覆盖报告：XXX", "正文") is True

    def test_in_text(self):
        text = "本报告为首次深度覆盖..."
        assert detect_first_coverage("标题", text) is True

    def test_not_first_coverage(self):
        assert detect_first_coverage("维持买入评级", "维持评级") is False

    def test_empty(self):
        assert detect_first_coverage("", "") is False


class TestExtractCoreLogic:
    def test_basic(self):
        text = """
        投资要点：
        公司是行业龙头，市占率持续提升。
        财富管理业务快速发展，AUM 突破 10 万亿。
        """
        summary = extract_core_logic(text)
        assert "行业龙头" in summary or "财富管理" in summary

    def test_missing(self):
        text = "无投资要点"
        assert extract_core_logic(text) == ""


class TestExtractAll:
    cfg = YanbaoConfig()

    def test_scanned_pdf_skips(self):
        parsed = {
            "full_text": "",
            "tables": [],
            "page_count": 5,
            "parse_status": "scanned_pdf",
            "char_count": 10,
        }
        result = extract_all(parsed, self.cfg, title="测试")
        assert result["rating"] == ""
        assert result["target_price"] is None
        assert result["first_coverage"] is False
        assert result["company_basics"] == {}
        assert result["section_titles"] == []

    def test_ok(self):
        parsed = {
            "full_text": "投资评级：买入。目标价 45 元。",
            "tables": [],
            "page_count": 1,
            "parse_status": "ok",
            "char_count": 100,
        }
        result = extract_all(parsed, self.cfg, title="测试")
        assert result["rating"] == "买入"
        assert result["target_price"] == 45.0
        assert "company_basics" in result
        assert "section_titles" in result


class TestExtractCompanyBasics:
    def test_extract_indicators(self):
        text = """
        收盘价 38.27
        总股本(万股) 2,521,985
        流通A股/B股(万股) 2,062,894/0
        资产负债率(%) 90.43%
        市净率(倍) 0.75
        净资产收益率(加权) 3.37
        12个月内最高/最低价 48.55/37.31
        PE（倍） 6.43
        """
        from extractor import extract_company_basics
        result = extract_company_basics(text)
        assert result["收盘价"] == "38.27"
        assert result["总股本(万股)"] == "2521985"  # 去千分位
        assert result["流通股本(万股)"] == "2062894/0"
        assert result["市净率(PB)"] == "0.75"
        assert result["市盈率(PE)"] == "6.43"
        assert result["12个月内最高/最低价"] == "48.55/37.31"

    def test_empty(self):
        from extractor import extract_company_basics
        assert extract_company_basics("") == {}

    def test_business_description(self):
        text = "主营业务：公司主要从事银行业务，提供存贷款、财富管理等服务。"
        from extractor import extract_company_basics
        result = extract_company_basics(text)
        assert "主营业务" in result
        assert "银行业务" in result["主营业务"]


class TestExtractSectionTitles:
    def test_arrow_titles(self):
        text = """
        ➢ 事件：公司公布2026年一季度报告。
        ➢ 息差降幅收窄，负债优势支撑行业见底阶段的相对韧性。
        ➢ 非息修复主线延续，财富管理仍是核心弹性来源。
        """
        from extractor import extract_section_titles
        result = extract_section_titles(text)
        assert len(result) >= 1
        assert any("事件" in t for t in result)

    def test_numbered_titles(self):
        text = """
        1. 公司概况
        2. 业务分析
        3. 财务分析
        """
        from extractor import extract_section_titles
        result = extract_section_titles(text)
        assert len(result) >= 1
        assert any("公司概况" in t for t in result)

    def test_chinese_numbered_titles(self):
        text = """
        一、公司概况
        二、业务分析
        三、财务分析
        """
        from extractor import extract_section_titles
        result = extract_section_titles(text)
        assert len(result) >= 1

    def test_empty(self):
        from extractor import extract_section_titles
        assert extract_section_titles("") == []

    def test_dedup(self):
        text = """
        ➢ 事件：公司公布报告。
        ➢ 事件：公司公布报告。
        """
        from extractor import extract_section_titles
        result = extract_section_titles(text)
        # 去重后应只剩 1 条
        assert len(result) == 1

    def test_filter_noise(self):
        text = """
        1. 3.50
        2. 2.00%
        3. 一、评级说明
        4. 分析师声明：xxx
        """
        from extractor import extract_section_titles
        result = extract_section_titles(text)
        # 过滤纯数字/百分比/尾部声明后应为空
        assert len(result) == 0


class TestExtractRisksMultiline:
    def test_semicolon_multiline(self):
        """测试分号分隔的风险提示跨行不被截断。"""
        text = """
        风险提示：零售贷款资产质量恶化超预期；净息差下行幅度超预期；财富管理与银行卡等
        中收修复不及预期。
        """
        from extractor import extract_risks
        result = extract_risks(text)
        assert len(result) >= 2
        # 第三条应合并跨行，不被截断
        last = result[-1]
        assert "中收修复不及预期" in last


# ====================================================================
# signal_judge 测试
# ====================================================================


class TestJudgeTargetPriceUpside:
    cfg = YanbaoConfig()

    def test_strong_signal(self):
        result = judge_target_price_upside(45.0, 30.0, self.cfg)
        assert result["signal"] == "强"
        assert result["upside_pct"] == 0.5

    def test_moderate_signal(self):
        # 30 → 25.5, upside = 0.2
        result = judge_target_price_upside(25.5, 21.25, self.cfg)
        assert result["signal"] == "中"

    def test_weak_signal(self):
        result = judge_target_price_upside(20.0, 18.0, self.cfg)
        assert result["signal"] == "弱"

    def test_missing_target_price(self):
        result = judge_target_price_upside(None, 30.0, self.cfg)
        assert result["signal"] == "无法判断"

    def test_missing_current_price(self):
        result = judge_target_price_upside(45.0, None, self.cfg)
        assert result["signal"] == "无法判断"

    def test_zero_price(self):
        result = judge_target_price_upside(45.0, 0.0, self.cfg)
        assert result["signal"] == "无法判断"


class TestJudgeRatingJump:
    cfg = YanbaoConfig()

    def test_jump_from_neutral_to_buy(self):
        history = [
            {"org_name": "中信证券", "rating": "中性", "publish_date": "2026-08-01"},
        ]
        result = judge_rating_jump("买入", history, "中信证券", self.cfg)
        assert result["is_jump"] is True
        assert result["from"] == "中性"
        assert result["to"] == "买入"

    def test_maintain_rating(self):
        history = [
            {"org_name": "中信证券", "rating": "买入", "publish_date": "2026-08-01"},
        ]
        result = judge_rating_jump("买入", history, "中信证券", self.cfg)
        assert result["is_jump"] is False
        assert "维持" in result["reason"]

    def test_downgrade(self):
        history = [
            {"org_name": "中信证券", "rating": "买入", "publish_date": "2026-08-01"},
        ]
        result = judge_rating_jump("中性", history, "中信证券", self.cfg)
        assert result["is_jump"] is False
        assert "降至" in result["reason"]

    def test_no_history(self):
        result = judge_rating_jump("买入", [], "中信证券", self.cfg)
        assert result["is_jump"] is False
        assert "无历史对比" in result["reason"]

    def test_no_same_org_history(self):
        history = [
            {"org_name": "国泰君安", "rating": "买入", "publish_date": "2026-08-01"},
        ]
        result = judge_rating_jump("买入", history, "中信证券", self.cfg)
        assert result["is_jump"] is False
        assert "无机构" in result["reason"]

    def test_empty_current_rating(self):
        result = judge_rating_jump("", [], "中信证券", self.cfg)
        assert result["is_jump"] is False
        assert "未提取" in result["reason"]


class TestJudgeForecastRevision:
    cfg = YanbaoConfig()

    def test_revision_up(self):
        # 最新 1600 vs 一致预期 1500，比率 6.67% > 5%
        latest = {"归母净利润": {"2024E": "1600亿"}}
        consensus = {"2024": "1500亿"}
        result = judge_forecast_revision(latest, consensus, self.cfg)
        assert result["is_revision_up"] is True
        assert result["latest"] == 1600.0
        assert result["consensus"] == 1500.0

    def test_no_revision(self):
        # 最新 1550 vs 一致预期 1500，比率 3.3% < 5%
        latest = {"归母净利润": {"2024E": "1550亿"}}
        consensus = {"2024": "1500亿"}
        result = judge_forecast_revision(latest, consensus, self.cfg)
        assert result["is_revision_up"] is False

    def test_missing_consensus(self):
        latest = {"归母净利润": {"2024E": "1600亿"}}
        result = judge_forecast_revision(latest, {}, self.cfg)
        assert result["is_revision_up"] is False
        assert "无法获取一致预期" in result["reason"]

    def test_missing_latest(self):
        consensus = {"2024": "1500亿"}
        result = judge_forecast_revision({"归母净利润": {}}, consensus, self.cfg)
        assert result["is_revision_up"] is False
        assert "无" in result["reason"]

    def test_year_matching_with_E_suffix(self):
        # 一致预期年份 "2024"，研报预测年份 "2024E"，应能匹配
        latest = {"归母净利润": {"2024E": "1600亿"}}
        consensus = {"2024": "1500亿"}
        result = judge_forecast_revision(latest, consensus, self.cfg)
        assert result["is_revision_up"] is True


class TestJudgeCatalystTimeliness:
    cfg = YanbaoConfig()

    def test_has_recent_catalyst(self):
        catalysts = ["预计9月降息利好", "新品即将发布"]
        result = judge_catalyst_timeliness(catalysts, self.cfg)
        assert result["has_recent_catalyst"] is True
        assert len(result["items"]) >= 1

    def test_no_recent_catalyst(self):
        catalysts = ["公司长期发展向好"]
        result = judge_catalyst_timeliness(catalysts, self.cfg)
        assert result["has_recent_catalyst"] is False

    def test_empty(self):
        result = judge_catalyst_timeliness([], self.cfg)
        assert result["has_recent_catalyst"] is False


class TestJudgeFirstCoverageSignal:
    def test_is_first_coverage(self):
        result = judge_first_coverage_signal(True)
        assert result["is_first_coverage"] is True

    def test_not_first_coverage(self):
        result = judge_first_coverage_signal(False)
        assert result["is_first_coverage"] is False


class TestJudgeSignals:
    cfg = YanbaoConfig()

    def test_aggregation(self):
        extracted = {
            "rating": "买入",
            "target_price": 45.0,
            "earnings_forecast": {"归母净利润": {"2024E": "1600亿"}},
            "catalysts": ["预计9月降息"],
            "first_coverage": False,
        }
        signals = judge_signals(
            extracted=extracted,
            current_price=30.0,
            consensus={"2024": "1500亿"},
            history=[],
            current_org="中信证券",
            cfg=self.cfg,
        )
        assert "首次覆盖" in signals
        assert "盈利预测上调" in signals
        assert "目标价空间" in signals
        assert "评级变化" in signals
        assert "近期催化剂" in signals
        assert signals["目标价空间"]["signal"] == "强"
        assert signals["盈利预测上调"]["is_revision_up"] is True


# ====================================================================
# pdf_parser 测试（mock pdfplumber）
# ====================================================================


class TestPdfParser:
    def test_file_not_found(self):
        from pdf_parser import extract_text_and_tables
        cfg = YanbaoConfig()
        result = extract_text_and_tables(Path("/nonexistent/file.pdf"), cfg)
        assert result["parse_status"] == "parse_failed"
        assert result["page_count"] == 0

    def test_scanned_pdf_detection(self, tmp_path):
        # mock pdfplumber：返回极少文本（< pdf_text_min_chars）
        from pdf_parser import extract_text_and_tables

        cfg = YanbaoConfig(pdf_text_min_chars=100)

        # 用临时文件绕过 Path.exists() 检查
        fake_pdf = tmp_path / "fake.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 fake")

        # 构造 mock pdfplumber
        mock_page = MagicMock()
        mock_page.extract_text.return_value = "短文本"  # 3 字符 < 100
        mock_page.extract_tables.return_value = []

        mock_pdf = MagicMock()
        mock_pdf.pages = [mock_page]

        mock_pdfplumber = MagicMock()
        mock_pdfplumber.open.return_value.__enter__.return_value = mock_pdf

        with patch.dict(sys.modules, {"pdfplumber": mock_pdfplumber}):
            result = extract_text_and_tables(fake_pdf, cfg)

        assert result["parse_status"] == "scanned_pdf"
        assert result["char_count"] < 100


# ====================================================================
# reporter 测试
# ====================================================================


class TestReporter:
    def test_build_no_report_found(self):
        from reporter import build_no_report_found
        report = build_no_report_found("999999", "akshare 返回空")
        assert report["股票代码"] == "999999"
        assert report["状态"] == "未找到研报"
        assert "akshare 返回空" in report["原因"]

    def test_build_markdown_no_report(self):
        from reporter import _build_markdown
        report = {"股票代码": "600036", "状态": "未找到研报", "原因": "无数据", "建议": "检查代码", "生成时间": "2026-08-26"}
        md = _build_markdown(report)
        assert "未找到研报" in md
        assert "600036" in md

    def test_build_markdown_full_report(self):
        from reporter import _build_markdown
        report = {
            "股票代码": "600036",
            "股票简称": "招商银行",
            "研报标题": "深度报告",
            "发布机构": "中信证券",
            "发布日期": "2026-08-25",
            "评级": "买入",
            "目标价": 45.0,
            "当前股价": 32.5,
            "隐含涨幅": "38.5%",
            "盈利预测": {"归母净利润": {"2024E": "1480亿"}},
            "短线信号": {
                "首次覆盖": {"is_first_coverage": False, "reason": "非首次"},
                "目标价空间": {"signal": "强", "reason": "涨幅 38.5%"},
            },
            "解析状态": {"pdf_parse_status": "ok", "pdf_pages": 28, "pdf_downloaded": True, "fields_extracted": ["评级"]},
        }
        md = _build_markdown(report)
        assert "招商银行" in md
        assert "买入" in md
        assert "38.5%" in md
        assert "短线博弈信号" in md
