"""stock_character 单元测试。

参考 alarming_monitor/test_linkban.py 的纯函数测试模式：
    - analyze_gene：空数据/各种 max_boards/consecutive/amplitude/has_gene/just_starting
    - analyze_hot_money：席位分类/次日涨跌/preference 三分支
    - reporter：ASCII 格式 / CSV 行字段数

运行：
    uv run pytest services/stock_character/test_character.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from datetime import date, timedelta

import pandas as pd
import pytest

# 确保 services/stock_character 在 sys.path（直接运行 main 时注入）
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from config import StockCharConfig, get_stock_char_config
from gene import analyze_gene, filter_gene_candidates, _lookup_score, _calc_amplitude
from hot_money import analyze_hot_money, _classify_seat, _get_next_day_pct
from reporter import (
    format_gene_csv_row, format_hotmoney_csv_row,
    format_gene_scan_report, format_gene_single_report,
    format_hot_money_report, generate_markdown_report,
    GENE_CSV_FIELDS, HOTMONEY_CSV_FIELDS,
)


# ====================================================================
# 测试 fixtures
# ====================================================================

@pytest.fixture
def cfg():
    return StockCharConfig.from_env()


@pytest.fixture
def sample_history_df():
    """模拟涨停历史 DataFrame。"""
    return pd.DataFrame([
        {'date': '2026-08-28', 'code': '000017', 'name': '深中华A',
         'continue_cnt': 7, 'limit_up_reason': '黄金概念', 'seal_money': 10069100.0},
        {'date': '2026-08-27', 'code': '000017', 'name': '深中华A',
         'continue_cnt': 6, 'limit_up_reason': '黄金概念', 'seal_money': 45440700.0},
        {'date': '2026-08-26', 'code': '000017', 'name': '深中华A',
         'continue_cnt': 5, 'limit_up_reason': '黄金概念', 'seal_money': 30000000.0},
        {'date': '2026-06-15', 'code': '000017', 'name': '深中华A',
         'continue_cnt': 2, 'limit_up_reason': '锂电池', 'seal_money': 5000000.0},
        {'date': '2026-03-10', 'code': '000017', 'name': '深中华A',
         'continue_cnt': 3, 'limit_up_reason': '重组预期', 'seal_money': 8000000.0},
    ])


@pytest.fixture
def sample_kline_df():
    """模拟日线 DataFrame（含振幅数据）。"""
    base = date(2026, 8, 28)
    rows = []
    price = 10.0
    for i in range(200):
        d = (base - timedelta(days=200 - i)).isoformat()
        # 模拟高波动：high-low 差 0.6（6% 振幅）
        high = price + 0.3
        low = price - 0.3
        close = price
        rows.append({'date': d, 'open': price, 'high': high,
                     'low': low, 'close': close, 'volume': 1000000})
        price = close  # 简化：平盘
    return pd.DataFrame(rows)


@pytest.fixture
def sample_lhb_records():
    """模拟龙虎榜记录 DataFrame。"""
    return pd.DataFrame([
        {'date': '2026-08-28', 'stock_code': '000017', 'stock_name': '深中华A',
         'seat_name': '华泰证券股份有限公司深圳益田路证券营业部',
         'seat_type': 'buy', 'amount': 303459500.0, 'youzi_icon': '',
         'up_desc': '7连板', 'up_reason': '日涨幅偏离值达7%'},
        {'date': '2026-08-28', 'stock_code': '000017', 'stock_name': '深中华A',
         'seat_name': '机构专用', 'seat_type': 'sell',
         'amount': 405899300.0, 'youzi_icon': '',
         'up_desc': '7连板', 'up_reason': '日涨幅偏离值达7%'},
        {'date': '2026-08-27', 'stock_code': '000017', 'stock_name': '深中华A',
         'seat_name': '华泰证券股份有限公司深圳益田路证券营业部',
         'seat_type': 'buy', 'amount': 200000000.0, 'youzi_icon': '',
         'up_desc': '6连板', 'up_reason': '日涨幅偏离值达7%'},
    ])


# ====================================================================
# gene.py 测试
# ====================================================================

class TestLookupScore:
    def test_exact_match(self):
        assert _lookup_score(7, {7: 40, 5: 30, 3: 20}) == 40

    def test_between_keys(self):
        assert _lookup_score(6, {7: 40, 5: 30, 3: 20}) == 30

    def test_below_all(self):
        assert _lookup_score(1, {7: 40, 5: 30, 3: 20}) == 0

    def test_none_value(self):
        assert _lookup_score(None, {7: 40}) == 0

    def test_empty_table(self):
        assert _lookup_score(10, {}) == 0


class TestCalcAmplitude:
    def test_normal(self, sample_kline_df):
        amp = _calc_amplitude(sample_kline_df, 30)
        assert amp > 0
        # 模拟数据 high-low=0.6, pre_close=10 → 6%
        assert 5.0 <= amp <= 7.0

    def test_empty(self):
        assert _calc_amplitude(pd.DataFrame(), 30) == 0.0

    def test_single_row(self):
        df = pd.DataFrame([{'date': '2026-08-28', 'open': 10, 'high': 11,
                            'low': 9, 'close': 10, 'volume': 1000}])
        assert _calc_amplitude(df, 30) == 0.0  # 不足2行


class TestAnalyzeGene:
    def test_empty_history(self, sample_kline_df, cfg):
        """空数据兜底。"""
        result = analyze_gene(None, sample_kline_df, '2026-08-28', cfg)
        assert result['data_sufficient'] is False
        assert result['has_gene'] is False
        assert result['gene_score'] == 0
        assert '无涨停历史' in result['gene_reason']

    def test_empty_df(self, sample_kline_df, cfg):
        """空 DataFrame 兜底。"""
        result = analyze_gene(pd.DataFrame(), sample_kline_df, '2026-08-28', cfg)
        assert result['data_sufficient'] is False

    def test_max_boards_extraction(self, sample_history_df, sample_kline_df, cfg):
        """最高连板数提取。"""
        result = analyze_gene(sample_history_df, sample_kline_df, '2026-08-28', cfg)
        assert result['data_sufficient'] is True
        assert result['max_boards'] == 7
        assert result['limit_up_count'] == 5
        assert result['consecutive_2plus_count'] == 5  # 7,6,5,2,3 → 5条都≥2
        assert result['consecutive_3plus_count'] == 4  # 7,6,5,3 → 4条≥3

    def test_has_gene_true(self, sample_history_df, sample_kline_df, cfg):
        """具备妖股基因。"""
        result = analyze_gene(sample_history_df, sample_kline_df, '2026-08-28', cfg)
        assert result['has_gene'] is True
        assert result['gene_score'] > 0

    def test_just_starting(self, sample_history_df, sample_kline_df, cfg):
        """刚启动判定（最近涨停在30日内且未过热）。"""
        result = analyze_gene(sample_history_df, sample_kline_df, '2026-08-28', cfg)
        # 最近涨停 2026-08-28，距参考日0天 → recent_has_limitup=True
        # 近5日有7连板 ≥3 → recent_hot=True → just_starting=False
        assert result['just_starting'] is False  # 已过热

    def test_just_starting_true(self, cfg):
        """刚启动=True 的场景（近期有涨停但未过热）。"""
        history = pd.DataFrame([
            {'date': '2026-08-20', 'code': '000017', 'name': '深中华A',
             'continue_cnt': 2, 'limit_up_reason': '概念', 'seal_money': 1000000.0},
            {'date': '2026-03-10', 'code': '000017', 'name': '深中华A',
             'continue_cnt': 4, 'limit_up_reason': '重组', 'seal_money': 8000000.0},
        ])
        kline = pd.DataFrame([
            {'date': f'2026-08-{i:02d}', 'open': 10, 'high': 10.5,
             'low': 9.5, 'close': 10, 'volume': 1000}
            for i in range(1, 29)
        ])
        result = analyze_gene(history, kline, '2026-08-28', cfg)
        assert result['has_gene'] is True
        assert result['just_starting'] is True

    def test_has_gene_false_low_boards(self, cfg):
        """最高连板不足 → has_gene=False。"""
        history = pd.DataFrame([
            {'date': '2026-08-28', 'code': '000017', 'name': '深中华A',
             'continue_cnt': 1, 'limit_up_reason': '概念', 'seal_money': 1000000.0},
        ])
        kline = pd.DataFrame([
            {'date': '2026-08-01', 'open': 10, 'high': 10.5,
             'low': 9.5, 'close': 10, 'volume': 1000},
            {'date': '2026-08-28', 'open': 10, 'high': 10.5,
             'low': 9.5, 'close': 10, 'volume': 1000},
        ])
        result = analyze_gene(history, kline, '2026-08-28', cfg)
        assert result['has_gene'] is False
        assert '连板' in result['gene_reason']

    def test_outside_window(self, cfg):
        """涨停记录超出历史窗口 → data_sufficient=False。"""
        history = pd.DataFrame([
            {'date': '2024-01-01', 'code': '000017', 'name': '深中华A',
             'continue_cnt': 5, 'limit_up_reason': '概念', 'seal_money': 1000000.0},
        ])
        kline = pd.DataFrame([
            {'date': '2026-08-01', 'open': 10, 'high': 10.5,
             'low': 9.5, 'close': 10, 'volume': 1000},
            {'date': '2026-08-28', 'open': 10, 'high': 10.5,
             'low': 9.5, 'close': 10, 'volume': 1000},
        ])
        result = analyze_gene(history, kline, '2026-08-28', cfg)
        assert result['data_sufficient'] is False


class TestFilterGeneCandidates:
    def test_filter_and_sort(self):
        results = [
            {'has_gene': True, 'just_starting': True, 'data_sufficient': True,
             'gene_score': 80, 'code': '000017'},
            {'has_gene': True, 'just_starting': False, 'data_sufficient': True,
             'gene_score': 90, 'code': '600123'},
            {'has_gene': False, 'just_starting': True, 'data_sufficient': True,
             'gene_score': 60, 'code': '000456'},
            {'has_gene': True, 'just_starting': True, 'data_sufficient': True,
             'gene_score': 100, 'code': '000789'},
        ]
        candidates = filter_gene_candidates(results)
        assert len(candidates) == 2
        assert candidates[0]['code'] == '000789'  # score 100
        assert candidates[1]['code'] == '000017'  # score 80

    def test_empty(self):
        assert filter_gene_candidates([]) == []


# ====================================================================
# hot_money.py 测试
# ====================================================================

class TestClassifySeat:
    def test_hot_money_match(self, cfg):
        seat_type, hm_name = _classify_seat(
            '华泰证券股份有限公司深圳益田路证券营业部', '', cfg)
        assert seat_type == '游资'
        assert hm_name == '炒股养家'

    def test_institution(self, cfg):
        seat_type, hm_name = _classify_seat('机构专用', '', cfg)
        assert seat_type == '机构'
        assert hm_name == ''

    def test_youzi_icon(self, cfg):
        seat_type, hm_name = _classify_seat('某营业部', '量化打板', cfg)
        assert seat_type == '游资'
        assert hm_name == '量化打板'

    def test_other(self, cfg):
        seat_type, hm_name = _classify_seat('某小营业部', '', cfg)
        assert seat_type == '其他'
        assert hm_name == ''

    def test_empty_seat_name(self, cfg):
        seat_type, hm_name = _classify_seat('', '', cfg)
        assert seat_type == '其他'


class TestGetNextDayPct:
    def test_normal(self):
        kline = pd.DataFrame([
            {'date': '2026-08-27', 'close': 10.0},
            {'date': '2026-08-28', 'close': 11.0},
            {'date': '2026-08-29', 'close': 10.5},
        ])
        pct = _get_next_day_pct(kline, '2026-08-28')
        # (10.5 - 11.0) / 11.0 * 100 = -4.545...
        assert pct is not None
        assert round(pct, 1) == -4.5

    def test_no_next_day(self):
        kline = pd.DataFrame([
            {'date': '2026-08-27', 'close': 10.0},
            {'date': '2026-08-28', 'close': 11.0},
        ])
        pct = _get_next_day_pct(kline, '2026-08-28')
        assert pct is None  # 龙虎榜日是最后一日

    def test_date_not_in_kline(self):
        kline = pd.DataFrame([
            {'date': '2026-08-25', 'close': 10.0},
            {'date': '2026-08-26', 'close': 11.0},
        ])
        pct = _get_next_day_pct(kline, '2026-08-28')
        # 2026-08-28 不在 kline，找最近前一日 2026-08-26
        # next = 无 → None
        assert pct is None

    def test_empty_kline(self):
        assert _get_next_day_pct(pd.DataFrame(), '2026-08-28') is None


class TestAnalyzeHotMoney:
    def test_empty_records(self, sample_kline_df, cfg):
        """空龙虎榜记录兜底。"""
        result = analyze_hot_money(None, sample_kline_df, '2026-08-28', cfg)
        assert result['data_sufficient'] is False
        assert result['preference'] == '中性'

    def test_hot_money_buy_premium(self, sample_lhb_records, cfg):
        """游资买入 + 次日溢价 → 偏爱锁仓。"""
        # 构造次日涨的 kline
        kline = pd.DataFrame([
            {'date': '2026-08-27', 'close': 10.0},
            {'date': '2026-08-28', 'close': 10.0},
            {'date': '2026-08-29', 'close': 10.5},  # 次日 +5%
        ])
        result = analyze_hot_money(sample_lhb_records, kline, '2026-08-29', cfg)
        assert result['data_sufficient'] is True
        assert result['hot_money_buy_count'] >= 2
        assert result['preference'] == '偏爱锁仓'
        assert '锁仓' in result['preference_reason']

    def test_hot_money_buy_dump(self, sample_lhb_records, cfg):
        """游资买入 + 次日核按钮 → 一日游风险。"""
        kline = pd.DataFrame([
            {'date': '2026-08-26', 'close': 10.0},
            {'date': '2026-08-27', 'close': 10.0},
            {'date': '2026-08-28', 'close': 10.0},
            {'date': '2026-08-29', 'close': 9.0},  # 次日 -10%
        ])
        result = analyze_hot_money(sample_lhb_records, kline, '2026-08-29', cfg)
        assert result['data_sufficient'] is True
        assert result['preference'] == '一日游风险'

    def test_neutral_low_buy_count(self, cfg):
        """游资买入不足 → 中性。"""
        lhb = pd.DataFrame([
            {'date': '2026-08-28', 'stock_code': '000017', 'stock_name': '深中华A',
             'seat_name': '华泰证券股份有限公司深圳益田路证券营业部',
             'seat_type': 'buy', 'amount': 100000.0, 'youzi_icon': '',
             'up_desc': '', 'up_reason': ''},
        ])
        kline = pd.DataFrame([
            {'date': '2026-08-28', 'close': 10.0},
            {'date': '2026-08-29', 'close': 10.5},
        ])
        result = analyze_hot_money(lhb, kline, '2026-08-29', cfg)
        assert result['preference'] == '中性'
        # hot_money_buy_count=1 < min_buy=2
        assert result['hot_money_buy_count'] == 1

    def test_institutional_count(self, sample_lhb_records, sample_kline_df, cfg):
        """机构买入次数统计。"""
        result = analyze_hot_money(sample_lhb_records, sample_kline_df, '2026-08-28', cfg)
        # sample_lhb_records 有1条机构卖出（seat_type=sell）
        # 机构买入次数 = 机构在 buy 席位中的次数
        assert result['lhb_count'] >= 1


# ====================================================================
# reporter.py 测试
# ====================================================================

class TestReporter:
    def test_gene_csv_row_fields(self):
        result = {'ref_date': '2026-08-28', 'code': '000017', 'name': '深中华A',
                  'max_boards': 7, 'gene_score': 90}
        row = format_gene_csv_row(result)
        assert set(row.keys()) == set(GENE_CSV_FIELDS)
        assert row['code'] == '000017'

    def test_hotmoney_csv_row_fields(self):
        result = {'ref_date': '2026-08-28', 'code': '000017', 'name': '深中华A',
                  'preference': '偏爱锁仓', 'hot_money_names': ['炒股养家', '方新侠']}
        row = format_hotmoney_csv_row(result)
        assert set(row.keys()) == set(HOTMONEY_CSV_FIELDS)
        assert row['hot_money_names'] == '炒股养家/方新侠'

    def test_gene_scan_report_empty(self):
        report = format_gene_scan_report([], '2026-08-28')
        assert '无具备妖股基因' in report
        assert '2026-08-28' in report

    def test_gene_scan_report_with_data(self):
        results = [
            {'code': '000017', 'name': '深中华A', 'max_boards': 7,
             'consecutive_2plus_count': 4, 'avg_amplitude': 6.5,
             'gene_score': 90, 'gene_level': '🔥 妖股基因强',
             'gene_reason': '最高7连板'},
        ]
        report = format_gene_scan_report(results, '2026-08-28')
        assert '000017' in report
        assert '深中华A' in report
        assert '7' in report

    def test_gene_single_report(self):
        result = {'ref_date': '2026-08-28', 'code': '000017', 'name': '深中华A',
                  'data_sufficient': True, 'max_boards': 7, 'limit_up_count': 5,
                  'consecutive_2plus_count': 4, 'consecutive_3plus_count': 4,
                  'latest_limit_up_date': '2026-08-28', 'avg_amplitude': 6.5,
                  'high_volatility': True, 'has_gene': True, 'just_starting': False,
                  'gene_score': 90, 'gene_level': '🔥 妖股基因强',
                  'gene_reason': '最高7连板'}
        report = format_gene_single_report(result, '2026-08-28')
        assert '000017' in report
        assert '最高连板数: 7板' in report

    def test_hot_money_report(self):
        result = {'ref_date': '2026-08-28', 'code': '000017', 'name': '深中华A',
                  'data_sufficient': True, 'lhb_count': 2,
                  'institutional_buy_count': 0, 'hot_money_buy_count': 2,
                  'hot_money_names': ['炒股养家'],
                  'next_day_premium_count': 1, 'next_day_dump_count': 0,
                  'preference': '偏爱锁仓', 'preference_reason': '游资炒股养家2次买入',
                  'hot_money_score': 65,
                  'next_day_pcts': [('2026-08-28', 4.5)]}
        report = format_hot_money_report(result, '2026-08-28')
        assert '000017' in report
        assert '偏爱锁仓' in report

    def test_markdown_report(self):
        gene_results = [
            {'ref_date': '2026-08-28', 'code': '000017', 'name': '深中华A',
             'data_sufficient': True, 'max_boards': 7, 'limit_up_count': 5,
             'consecutive_2plus_count': 4, 'consecutive_3plus_count': 4,
             'latest_limit_up_date': '2026-08-28', 'avg_amplitude': 6.5,
             'high_volatility': True, 'has_gene': True, 'just_starting': True,
             'gene_score': 90, 'gene_level': '🔥 妖股基因强',
             'gene_reason': '最高7连板'}
        ]
        md = generate_markdown_report(gene_results, [], '2026-08-28', scan_mode=True)
        assert '# 股票特性分析报告' in md
        assert '连板基因' in md

    def test_markdown_empty(self):
        md = generate_markdown_report([], [], '2026-08-28')
        assert '无分析结果' in md


# ====================================================================
# config.py 测试
# ====================================================================

class TestConfig:
    def test_default_config(self):
        cfg = StockCharConfig.from_env()
        assert cfg.gene_min_max_boards == 2
        assert cfg.history_request_interval_seconds == 0.5
        assert cfg.premium_threshold == 3.0

    def test_conservative_profile(self):
        cfg = get_stock_char_config(profile='conservative')
        assert cfg.gene_min_max_boards == 3
        assert cfg.gene_min_amplitude == 6.0

    def test_strict_profile(self):
        cfg = get_stock_char_config(profile='strict')
        assert cfg.gene_min_max_boards == 4
        assert cfg.premium_threshold == 5.0

    def test_overrides(self):
        cfg = StockCharConfig.from_env(gene_min_max_boards=5)
        assert cfg.gene_min_max_boards == 5

    def test_hot_money_seats_default(self):
        cfg = StockCharConfig.from_env()
        assert '炒股养家' in cfg.hot_money_seats
        assert '方新侠' in cfg.hot_money_seats

    def test_csv_root_path(self):
        cfg = StockCharConfig.from_env()
        p = cfg.csv_root()
        assert 'stock_character' in str(p)

    def test_cache_root_path(self):
        cfg = StockCharConfig.from_env()
        p = cfg.cache_root()
        assert 'cache' in str(p)


# ====================================================================
# 集成测试（main.py 编排，mock 数据）
# ====================================================================

class TestMainOrchestration:
    def test_parse_args_codes(self):
        from main import _parse_args
        args = _parse_args(['--codes', '000017,600550'])
        assert args.codes == '000017,600550'
        assert args.gene_only is False
        assert args.hotmoney_only is False

    def test_parse_args_gene_only(self):
        from main import _parse_args
        args = _parse_args(['--gene-only', '--scan'])
        assert args.gene_only is True
        assert args.scan is True

    def test_determine_ref_date_default(self):
        from main import _determine_ref_date
        ref = _determine_ref_date(None)
        # 默认昨日
        from datetime import date, timedelta
        expected = (date.today() - timedelta(days=1)).isoformat()
        assert ref == expected

    def test_determine_ref_date_explicit(self):
        from main import _determine_ref_date
        ref = _determine_ref_date('2026-08-28')
        assert ref == '2026-08-28'
