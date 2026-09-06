"""crawler_news 阶段三单元测试。

覆盖（对齐 project.md 阶段三验收标准）：
    1. 三条核心验收场景：
       - "贵州茅台(600519)今日涨停" → mentioned_codes=["600519"]、mentioned_names=["贵州茅台"]
       - "平安银行发布年报" → 通过名称反查得到 000001
       - 同一新闻内代码多次提及只保留一次
    2. extract_codes：沪市/深市/科创板/创业板/北交所过滤/中文上下文/去重保序
    3. resolve_names：命中/未命中/空列表
    4. resolve_codes_by_name：命中/未命中/短名不误命中长名
    5. tag_news：与接口自带标注合并、title+content 拼接扫描、原地修改返回
    6. tag_news_batch：批量标注
    7. reload_mapping：测试场景切换映射表后强制重载
    8. 映射表缺失场景：返回空，不抛异常

设计要点：
    - 验收场景使用项目根 ``data/stock_mapping.json`` 真实映射（含 600519/000001）；
    - 边界场景通过 ``use_temp_mapping`` fixture 注入临时映射文件，隔离测试
      与真实映射表内容；
    - 全部不访问网络。

运行方式：
    cd trading_lab && uv run pytest services/crawler_news/test_tagger.py -v
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

import tagger
from tagger import (
    extract_codes,
    resolve_names,
    resolve_codes_by_name,
    tag_news,
    tag_news_batch,
    reload_mapping,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def use_temp_mapping(tmp_path, monkeypatch):
    """注入临时映射表文件，避免依赖真实 stock_mapping.json 的具体内容。

    用法：
        def test_xxx(use_temp_mapping):
            use_temp_mapping({"600519": "贵州茅台", ...})
            ...  # 此后调用 extract_codes / resolve_names 等均使用该临时映射
    """
    written_path_holder: dict[str, Path] = {}

    def _setup(mapping: dict[str, str]) -> Path:
        path = tmp_path / "stock_mapping.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False)
        written_path_holder["path"] = path
        # 强制重载，让 tagger 读入新映射
        reload_mapping()
        return path

    yield _setup

    # 测试结束后恢复真实映射，避免污染后续测试
    reload_mapping()


@pytest.fixture
def no_mapping_file(monkeypatch, tmp_path):
    """模拟映射表文件缺失场景。"""
    def _fake_resolve():
        return None
    monkeypatch.setattr(tagger, "_resolve_mapping_path", _fake_resolve)
    reload_mapping()
    yield
    reload_mapping()


# ---------------------------------------------------------------------------
# 验收场景一：贵州茅台(600519) 提取代码+反查名称
# ---------------------------------------------------------------------------


class TestAcceptanceMoutai:
    """验收场景：输入 "贵州茅台(600519)今日涨停"。"""

    def test_extract_code_from_parentheses(self):
        text = "贵州茅台(600519)今日涨停"
        codes = extract_codes(text)
        assert codes == ["600519"]

    def test_resolve_name_by_code(self):
        names = resolve_names(["600519"])
        assert names == ["贵州茅台"]

    def test_tag_news_full_flow(self):
        item = {
            "title": "贵州茅台(600519)今日涨停",
            "content": "",
            "source_short": "cls",
            "mentioned_codes": [],
            "mentioned_names": [],
        }
        result = tag_news(item)
        assert result["mentioned_codes"] == ["600519"]
        assert result["mentioned_names"] == ["贵州茅台"]


# ---------------------------------------------------------------------------
# 验收场景二：平安银行发布年报 → 通过名称反查得到 000001
# ---------------------------------------------------------------------------


class TestAcceptancePinganBank:
    """验收场景：输入 "平安银行发布年报"，无显式代码，靠名称反查。"""

    def test_no_code_in_text(self):
        text = "平安银行发布年报"
        codes = extract_codes(text)
        assert codes == []  # 正则无命中

    def test_resolve_code_by_name(self):
        codes = resolve_codes_by_name("平安银行发布年报")
        assert codes == ["000001"]

    def test_resolve_name_back(self):
        names = resolve_names(["000001"])
        assert names == ["平安银行"]

    def test_tag_news_full_flow(self):
        item = {
            "title": "平安银行发布年报",
            "content": "",
            "source_short": "cls",
            "mentioned_codes": [],
            "mentioned_names": [],
        }
        result = tag_news(item)
        assert result["mentioned_codes"] == ["000001"]
        assert result["mentioned_names"] == ["平安银行"]


# ---------------------------------------------------------------------------
# 验收场景三：同一新闻内代码多次提及只保留一次
# ---------------------------------------------------------------------------


class TestAcceptanceDedup:
    """验收场景：同一代码多次提及只保留一次。"""

    def test_extract_codes_dedup(self):
        text = "600519 600519 600519"
        assert extract_codes(text) == ["600519"]

    def test_extract_codes_dedup_preserves_first_order(self):
        text = "000001 600519 000001"
        assert extract_codes(text) == ["000001", "600519"]

    def test_tag_news_dedup_across_title_and_content(self):
        item = {
            "title": "贵州茅台(600519)业绩超预期",
            "content": "另外，600519 的销量数据也持续向好。再次提及 600519。",
            "source_short": "cls",
            "mentioned_codes": [],
            "mentioned_names": [],
        }
        result = tag_news(item)
        assert result["mentioned_codes"] == ["600519"]
        assert result["mentioned_names"] == ["贵州茅台"]


# ---------------------------------------------------------------------------
# extract_codes 单元测试
# ---------------------------------------------------------------------------


class TestExtractCodes:
    def test_empty_text(self):
        assert extract_codes("") == []

    def test_none_text(self):
        assert extract_codes(None) == []  # type: ignore[arg-type]

    def test_no_codes(self):
        assert extract_codes("今日无个股代码的新闻") == []

    def test_sh_main_board_600(self):
        assert extract_codes("关注 600000 走势") == ["600000"]

    def test_sh_kcb_688(self):
        assert extract_codes("科创板 688981 涨停") == ["688981"]

    def test_sz_main_000(self):
        assert extract_codes("深市 000002 跌停") == ["000002"]

    def test_sz_zxb_002(self):
        assert extract_codes("中小板 002230 公告") == ["002230"]

    def test_sz_cyb_300(self):
        assert extract_codes("创业板 300750 大涨") == ["300750"]

    def test_bj_8_ignored(self):
        """北交所（8 开头）不在标注范围。"""
        assert extract_codes("北交所 830832") == []

    def test_bj_4_ignored(self):
        """北交所（4 开头）不在标注范围。"""
        assert extract_codes("430047") == []

    def test_seven_digit_not_matched(self):
        """7 位数字串不应被匹配为股票代码。"""
        assert extract_codes("订单号 6005199") == []

    def test_five_digit_not_matched(self):
        """5 位数字串不应被匹配。"""
        assert extract_codes("编号 60051") == []

    def test_six_digit_within_longer_number_ignored(self):
        """长数字串中的 6 位子串不应被误匹配。"""
        assert extract_codes("123600519456") == []

    def test_chinese_context_no_word_boundary(self):
        """中文上下文中 \b 断言失效，但前后非数字断言仍能正确提取。"""
        assert extract_codes("贵州茅台600519今日涨停") == ["600519"]

    def test_parentheses_context(self):
        """括号内代码也能正确提取。"""
        assert extract_codes("标题(600519)尾") == ["600519"]

    def test_multiple_codes_preserve_order(self):
        text = "300750 与 600519 同步异动，000001 也参与"
        assert extract_codes(text) == ["300750", "600519", "000001"]

    def test_dedup_preserves_first_occurrence_order(self):
        text = "600519 000001 600519 000001"
        assert extract_codes(text) == ["600519", "000001"]


# ---------------------------------------------------------------------------
# resolve_names 单元测试
# ---------------------------------------------------------------------------


class TestResolveNames:
    def test_known_code(self):
        assert resolve_names(["600519"]) == ["贵州茅台"]

    def test_unknown_code_returns_empty(self):
        assert resolve_names(["999999"]) == [""]

    def test_mixed_known_and_unknown(self):
        """索引对齐：已知代码返回名称，未知代码返回空字符串。"""
        result = resolve_names(["600519", "999999", "000001"])
        assert result == ["贵州茅台", "", "平安银行"]

    def test_empty_list(self):
        assert resolve_names([]) == []


# ---------------------------------------------------------------------------
# resolve_codes_by_name 单元测试
# ---------------------------------------------------------------------------


class TestResolveCodesByName:
    def test_known_name(self):
        assert resolve_codes_by_name("贵州茅台业绩超预期") == ["600519"]

    def test_unknown_name_returns_empty(self):
        assert resolve_codes_by_name("不知名公司发布年报") == []

    def test_empty_text(self):
        assert resolve_codes_by_name("") == []

    def test_none_text(self):
        assert resolve_codes_by_name(None) == []  # type: ignore[arg-type]

    def test_multiple_names_preserve_dict_order(self):
        """同时命中多个名称时去重，顺序为映射表内长名优先。"""
        result = resolve_codes_by_name("贵州茅台与平安银行同步异动")
        assert set(result) == {"600519", "000001"}


# ---------------------------------------------------------------------------
# 短名不误命中长名（用临时映射表覆盖）
# ---------------------------------------------------------------------------


class TestShortNameNoFalseMatch:
    """短名不应误命中长名子串：如映射表同时含 "平安" 和 "平安银行"，
    文本 "平安银行" 应只匹配 "平安银行"，不匹配 "平安"。"""

    def test_longer_name_takes_priority(self, use_temp_mapping):
        use_temp_mapping({"000001": "平安银行", "999999": "平安"})
        # 文本含 "平安银行"，应只命中 "平安银行"（000001），不命中 "平安"（999999）
        result = resolve_codes_by_name("平安银行发布年报")
        assert result == ["000001"]
        assert "999999" not in result


# ---------------------------------------------------------------------------
# tag_news 单元测试
# ---------------------------------------------------------------------------


class TestTagNews:
    def test_basic_tagging(self):
        item = {
            "title": "贵州茅台(600519)涨停",
            "content": "",
            "mentioned_codes": [],
            "mentioned_names": [],
        }
        result = tag_news(item)
        assert result["mentioned_codes"] == ["600519"]
        assert result["mentioned_names"] == ["贵州茅台"]

    def test_in_place_modification(self):
        """tag_news 应原地修改并返回同一 dict。"""
        item = {"title": "T", "content": "C", "mentioned_codes": [], "mentioned_names": []}
        result = tag_news(item)
        assert result is item  # 同一对象引用

    def test_missing_mentioned_fields_initializes(self):
        """item 缺少 mentioned_codes/names 字段时也应正常初始化。"""
        item = {"title": "贵州茅台(600519)", "content": ""}
        result = tag_news(item)
        assert result["mentioned_codes"] == ["600519"]
        assert result["mentioned_names"] == ["贵州茅台"]

    def test_title_and_content_concatenated(self):
        """title 与 content 都应被扫描。"""
        item = {
            "title": "今日异动",
            "content": "贵州茅台(600519)涨停",
            "mentioned_codes": [],
            "mentioned_names": [],
        }
        result = tag_news(item)
        assert result["mentioned_codes"] == ["600519"]

    def test_preserves_interface_provided_codes(self):
        """巨潮接口自带 mentioned_codes/names（来自 代码/简称 列）应保留并合并。"""
        item = {
            "title": "董事会决议公告",
            "content": "",
            "source_short": "juchao",
            "mentioned_codes": ["000001"],
            "mentioned_names": ["平安银行"],
        }
        result = tag_news(item)
        # 接口自带的代码排在最前（接口自带 → 正则 → 名称反查 优先级）
        assert result["mentioned_codes"][0] == "000001"
        assert result["mentioned_names"][0] == "平安银行"

    def test_interface_codes_merge_with_extracted(self):
        """接口自带 + 正则提取 + 名称反查 三路合并去重。"""
        item = {
            "title": "贵州茅台(600519) 平安银行发布年报",
            "content": "",
            "source_short": "juchao",
            "mentioned_codes": ["000001"],  # 接口自带
            "mentioned_names": ["平安银行"],
        }
        result = tag_news(item)
        # 接口自带 000001 在前，正则命中 600519 在中
        assert result["mentioned_codes"] == ["000001", "600519"]
        # 名称列表：原有 "平安银行" 在前，反查得到 "贵州茅台"
        assert result["mentioned_names"] == ["平安银行", "贵州茅台"]

    def test_no_codes_no_names_returns_empty_lists(self):
        """无任何命中时，mentioned_* 应为空列表而非 None。"""
        item = {
            "title": "今日无个股相关新闻",
            "content": "宏观政策综述",
            "mentioned_codes": [],
            "mentioned_names": [],
        }
        result = tag_news(item)
        assert result["mentioned_codes"] == []
        assert result["mentioned_names"] == []

    def test_none_title_and_content_safe(self):
        """title/content 为 None 时不应抛异常。"""
        item = {
            "title": None,
            "content": None,
            "mentioned_codes": [],
            "mentioned_names": [],
        }
        result = tag_news(item)
        assert result["mentioned_codes"] == []
        assert result["mentioned_names"] == []

    def test_name_reverse_lookup_supplements_code(self):
        """正则无命中，但文本含映射表内名称 → 通过名称反查补全代码。"""
        item = {
            "title": "平安银行发布年报",
            "content": "",
            "mentioned_codes": [],
            "mentioned_names": [],
        }
        result = tag_news(item)
        assert result["mentioned_codes"] == ["000001"]
        assert result["mentioned_names"] == ["平安银行"]


# ---------------------------------------------------------------------------
# tag_news_batch 批量入口
# ---------------------------------------------------------------------------


class TestTagNewsBatch:
    def test_batch_tags_all_items(self):
        items = [
            {"title": "贵州茅台(600519)涨停", "content": "", "mentioned_codes": [], "mentioned_names": []},
            {"title": "平安银行发布年报", "content": "", "mentioned_codes": [], "mentioned_names": []},
            {"title": "宏观综述", "content": "无个股代码", "mentioned_codes": [], "mentioned_names": []},
        ]
        result = tag_news_batch(items)
        assert result is items
        assert items[0]["mentioned_codes"] == ["600519"]
        assert items[1]["mentioned_codes"] == ["000001"]
        assert items[2]["mentioned_codes"] == []

    def test_empty_batch(self):
        assert tag_news_batch([]) == []


# ---------------------------------------------------------------------------
# reload_mapping + 映射表缺失场景
# ---------------------------------------------------------------------------


class TestMappingMissing:
    """映射表缺失或为空时，标注流程不应阻断。"""

    def test_missing_mapping_no_names_resolved(self, no_mapping_file):
        """无映射表时 resolve_names 全部返回空字符串。"""
        assert resolve_names(["600519"]) == [""]

    def test_missing_mapping_no_codes_by_name(self, no_mapping_file):
        """无映射表时 resolve_codes_by_name 返回空列表。"""
        assert resolve_codes_by_name("贵州茅台涨停") == []

    def test_missing_mapping_extract_codes_still_works(self, no_mapping_file):
        """无映射表时正则提取仍可用。"""
        assert extract_codes("贵州茅台(600519)涨停") == ["600519"]

    def test_missing_mapping_tag_news_returns_codes_only(self, no_mapping_file):
        item = {
            "title": "贵州茅台(600519)涨停",
            "content": "",
            "mentioned_codes": [],
            "mentioned_names": [],
        }
        result = tag_news(item)
        # 代码仍能提取，名称无法反查 → 空列表
        assert result["mentioned_codes"] == ["600519"]
        assert result["mentioned_names"] == []


class TestReloadMapping:
    def test_reload_picks_up_new_entries(self, tmp_path, monkeypatch):
        """reload_mapping 后应读到新增条目。"""
        # 准备临时映射表
        path = tmp_path / "stock_mapping.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"600519": "贵州茅台"}, f, ensure_ascii=False)
        monkeypatch.setattr(tagger, "_MAPPING_PATH_CANDIDATES", (path,))
        reload_mapping()
        assert resolve_names(["600519"]) == ["贵州茅台"]

        # 追加新条目后 reload，应读到新条目
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {"600519": "贵州茅台", "000001": "平安银行"},
                f,
                ensure_ascii=False,
            )
        # reload 前缓存仍为旧
        assert resolve_names(["000001"]) == [""]
        reload_mapping()
        assert resolve_names(["000001"]) == ["平安银行"]

    @pytest.fixture(autouse=False)
    def restore_mapping_after(self, tmp_path, monkeypatch):
        """本类所有测试结束后恢复真实映射表缓存，避免污染后续测试。"""
        yield
        reload_mapping()


# ---------------------------------------------------------------------------
# 映射表元数据键忽略测试
# ---------------------------------------------------------------------------


class TestMappingMetadataKeys:
    """stock_mapping.json 中以 _ 开头的键应被忽略。"""

    def test_metadata_keys_ignored(self, use_temp_mapping):
        use_temp_mapping({
            "_comment": "这是说明文本",
            "_example": "这是示例",
            "_meta_version": "1.0",
            "600519": "贵州茅台",
            "000001": "平安银行",
        })
        # 元数据键不应被当作股票代码
        names = resolve_names(["600519", "000001"])
        assert names == ["贵州茅台", "平安银行"]
        # _comment 等不应作为名称反查的命中
        assert resolve_codes_by_name("这是说明文本") == []

    def test_non_sh_prefix_keys_ignored(self, use_temp_mapping):
        """非沪深前缀的键（如北交所 4/8 开头）应被加载时过滤。"""
        use_temp_mapping({
            "430047": "诺思兰德",  # 北交所，应被过滤
            "830832": "秉信包装",  # 北交所，应被过滤
            "600519": "贵州茅台",
        })
        # 北交所代码在加载阶段已过滤，反查不应命中
        assert resolve_names(["430047"]) == [""]
        assert resolve_names(["830832"]) == [""]
        assert resolve_names(["600519"]) == ["贵州茅台"]
        # 名称反查命中北交所名称也不应返回北交所代码
        result = resolve_codes_by_name("诺思兰德发布年报")
        assert result == []  # 因北交所键已被过滤
