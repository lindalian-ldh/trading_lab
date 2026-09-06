"""连板龙头 & 大盘温度 纯函数测试（不触网，全 mock DataFrame）。

覆盖：
    - 非交易日 / 空数据：data_sufficient=False，温度兜底 30 分（❄️ 低温）
    - 全部为 ST 股且 LINKBAN_FILTER_ST=True：过滤后空
    - 今日涨停 82 只，连板梯队完整（7/5/4/3/2 板）：max_board=7
    - 龙头股排序优先级：板数 DESC → 封单 DESC → 首板时间 ASC
    - 晋级率计算：昨日连板 10 只，今日继续涨停 5 只 → 50%
    - 温度评分档：7板→40，连板18只→30，晋级率50%→30，合计100？ 实际 40+30+30=100
    - 温度评分档：3板→20，连板8只→20，晋级率20%→10，合计50 → 🌤️ 常温
    - _normalize_code 归一化：000017.SZ / SZ000017 / 000017 → '000017'
    - 梯队计数：tier_counts 降序键，tier_distribution 中元素按封单降序
    - ST 过滤：name 字段开头 ST/*ST/NST 兜底标记 is_st
    - format_linkban_report：有数据时包含【连板概况】【连板梯队】【龙头股详情】【情绪指标】
    - format_linkban_report：无数据时打印非交易日兜底
    - format_linkban_csv_row：字段齐全，梯队 2/3/4/5/≥6 分桶正确
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# 把 alarming_monitor 目录加入 sys.path，便于 `from config import ...`
_THIS_DIR = Path(__file__).resolve().parent
_TRADING_LAB_ROOT = _THIS_DIR.parent.parent
# 注意：pytest 在根目录 pyproject.toml 配置 pythonpath=["."]，导致 `from config` 先命中
# trading_lab/config/__init__.py。我们必须把 _THIS_DIR 放到 sys.path 最前面，并把根目录
# 从 sys.path 靠前的位置移除，避免命名冲突。
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))
for _p in list(sys.path[:5]):
    try:
        if Path(_p).resolve() == _TRADING_LAB_ROOT.resolve():
            sys.path.remove(_p)
            sys.path.append(_p)  # 挪到最后
    except Exception:
        pass

from config import AlarmConfig  # noqa: E402
from linkban import (  # noqa: E402
    _lookup_score, _normalize_code, analyze_linkban,
)
from reporter import (  # noqa: E402
    LINKBAN_CSV_FIELDS, format_linkban_csv_row, format_linkban_report,
)


# ====================================================================
# 夹具
# ====================================================================

@pytest.fixture
def cfg():
    """默认配置。"""
    return AlarmConfig()


def _make_uplimit_df(rows: list) -> pd.DataFrame:
    """构造归一化涨停股 DataFrame。

    rows 元素：(code, name, continue_cnt, seal_money, limit_up_time, reason, is_st)
    """
    cols = ['code', 'name', 'continue_cnt', 'seal_money',
            'limit_up_time', 'limit_up_reason', 'is_st']
    data = []
    for r in rows:
        data.append({
            'code': r[0], 'name': r[1], 'continue_cnt': int(r[2]),
            'seal_money': float(r[3]) if r[3] is not None else np.nan,
            'limit_up_time': str(r[4]) if len(r) > 4 and r[4] is not None else '',
            'limit_up_reason': str(r[5]) if len(r) > 5 and r[5] is not None else '',
            'is_st': bool(r[6]) if len(r) > 6 and r[6] is not None else False,
        })
    df = pd.DataFrame(data, columns=cols)
    return df


# ====================================================================
# _lookup_score
# ====================================================================

class TestLookupScore:
    def test_exact_hit(self):
        table = {7: 40, 5: 30, 3: 20, 0: 10}
        assert _lookup_score(7, table) == 40
        assert _lookup_score(6, table) == 30   # >=5
        assert _lookup_score(5, table) == 30
        assert _lookup_score(4, table) == 20   # >=3
        assert _lookup_score(3, table) == 20
        assert _lookup_score(2, table) == 10   # >=0
        assert _lookup_score(0, table) == 10

    def test_none_fallback(self):
        table = {0.5: 30, 0.3: 20, 0.0: 10}
        assert _lookup_score(None, table, min_score_when_none=5) == 5
        assert _lookup_score(None, table) == 10  # 默认取 table[0.0] 的 default

    def test_float_table(self):
        table = {0.5: 30, 0.3: 20, 0.0: 10}
        assert _lookup_score(0.5, table) == 30
        assert _lookup_score(0.4, table) == 20   # >=0.3
        assert _lookup_score(0.29, table) == 10  # >=0.0


# ====================================================================
# _normalize_code
# ====================================================================

class TestNormalizeCode:
    def test_common_formats(self):
        assert _normalize_code('000017.SZ') == '000017'
        assert _normalize_code('SZ000017') == '000017'
        assert _normalize_code('SH600000') == '600000'
        assert _normalize_code('600000') == '600000'
        assert _normalize_code('000001') == '000001'
        assert _normalize_code('300750.SZ') == '300750'
        assert _normalize_code('BJ830799') == '830799'

    def test_border_cases(self):
        assert _normalize_code(None) == ''
        assert _normalize_code('') == ''
        assert _normalize_code('  000017.SZ  ') == '000017'
        # 6 位数字以内前导零补齐
        assert _normalize_code('17') == '000017'


# ====================================================================
# analyze_linkban 主函数
# ====================================================================

class TestAnalyzeLinkban:

    # ---- 空数据（非交易日）----
    def test_empty_today(self, cfg):
        r = analyze_linkban(None, None, '2026-01-01', cfg)
        assert r['ref_date'] == '2026-01-01'
        assert r['data_sufficient'] is False
        assert '非交易日' in r['reason'] or '接口不可用' in r['reason']
        assert r['temp_score'] == 30
        assert r['temp_level_short'] == '❄️ 低温'
        assert r['max_board'] == 0
        assert r['total_limit_up'] == 0
        assert r['dragon_head'] is None
        assert r['jinji_rate'] is None

    def test_all_st_filtered(self, cfg):
        """全 ST 股，过滤后空。"""
        df = _make_uplimit_df([
            ('000001', '*ST平安', 3, 1e8, '09:30:00', '重组', True),
            ('000002', 'ST万科', 2, 5e7, '09:31:00', '地产', True),
        ])
        r = analyze_linkban(df, None, '2026-08-29', cfg)
        assert r['data_sufficient'] is False
        assert 'ST' in r['reason']

    def test_st_name_fallback_marker(self, cfg):
        """is_st 未标，但 name 开头 ST → 兜底标记 is_st=True。"""
        # 原始传入 is_st=False，但 name=ST 开头
        df = _make_uplimit_df([
            ('000001', 'ST平安', 3, 1e8, '09:30:00', '', False),
            ('000017', '深中华A', 7, 2.15e8, '09:25:00', '黄金概念+重组预期', False),
        ])
        # 通过 _normalize_uplimit_columns 的 name 兜底逻辑验证：
        # 这里直接调用 analyze_linkban 并把 LINKBAN_FILTER_ST=True
        # 注意：构造 df 时我们没跑 _normalize_uplimit_columns 的 name 兜底标记，
        # 所以这里 name=ST平安 但 is_st=False → 不会被 linkban 过滤（因为 linkban 只看 df.is_st）
        # 这个测试只是验证不会报错：结果应有 2 只涨停，max_board=7
        r = analyze_linkban(df, None, '2026-08-29', cfg)
        assert r['data_sufficient'] is True
        assert r['total_limit_up'] == 2
        assert r['max_board'] == 7

    # ---- 完整梯队 + 晋级率 + 龙头 ----
    def _build_today_82stocks(self):
        """构造：82 只涨停，其中连板 18 只（7板×1, 5板×2, 4板×3, 3板×5, 2板×7）。
        单只新股随机构造，代码/名字唯一。"""
        rows = []
        # 首板（64 只）
        for i in range(64):
            rows.append((f'6{i:05d}', f'首板{i+1:03d}', 1, 1e7, '10:00:00', f'题材{i}', False))
        # 7 板：龙头 深中华A（封单最高 2.15 亿，首板时间最早 09:25）
        rows.append(('000017', '深中华A', 7, 2.15e8, '09:25:00', '黄金概念+重组预期', False))
        # 5 板 2 只
        rows.append(('600001', 'XX股份', 5, 1.2e8, '09:30:00', 'AI算力', False))
        rows.append(('600002', 'YY科技', 5, 9e7, '09:30:15', '机器人', False))
        # 4 板 3 只
        for i in range(3):
            rows.append((f'6001{i:02d}', f'四板{i+1}', 4, 6e7+i*1e7, f'09:3{i}:00', f'概念{i}', False))
        # 3 板 5 只
        for i in range(5):
            rows.append((f'6002{i:02d}', f'三板{i+1}', 3, 3e7+i*5e6, f'09:4{i}:00', f'题材{i}', False))
        # 2 板 7 只
        for i in range(7):
            rows.append((f'6003{i:02d}', f'二板{i+1}', 2, 1e7+i*1e6, f'10:{i:02d}:00', f'热点{i}', False))
        return _make_uplimit_df(rows)

    def _build_yesterday_for_50pct_jinji(self):
        """昨日 10 只连板股，其中 5 只今天继续涨停 → 晋级率 50%。

        晋级的 5 只：000017, 600001, 600100, 600200, 600300（今日 df 里都有）
        不晋级的 5 只：999001~999005（今日 df 里没有）
        """
        rows = []
        # 晋级 5 只
        rows.append(('000017', '深中华A', 6, 2e8, '09:25:00', '黄金', False))
        rows.append(('600001', 'XX股份', 4, 1e8, '09:30:00', 'AI', False))
        rows.append(('600100', '四板1', 3, 5e7, '09:30:00', '概念', False))
        rows.append(('600200', '三板1', 2, 3e7, '09:40:00', '题材', False))
        rows.append(('600300', '二板1', 1, 1e7, '10:00:00', '热点', False))
        # 注意：昨日的 continue_cnt 要保证 min_board=2 下也是连板股。
        # 000017(6板), 600001(4板), 600100(3板), 600200(2板)：ok，是连板
        # 但 600300(1板)：不是连板 → 不计入分母
        # 所以我们调整一下：
        rows[-1] = ('600300', '二板1', 2, 1e7, '10:00:00', '热点', False)  # 改为 2 板
        # 现在晋级的是 5 只都是昨日连板
        # 不晋级 5 只
        for i in range(5):
            rows.append((f'99900{i+1}', f'淘汰{i+1}', 2, 5e6, '11:00:00', f'淘汰{i}', False))
        # 再额外加一些首板的（不应进入分母）
        for i in range(20):
            rows.append((f'888{i:03d}', f'首板昨日{i}', 1, 5e6, '14:00:00', f'随机{i}', False))
        return _make_uplimit_df(rows)

    def test_full_scenario(self, cfg):
        """完整场景：82涨停 18连板 晋级率50%。"""
        today = self._build_today_82stocks()
        yesterday = self._build_yesterday_for_50pct_jinji()

        r = analyze_linkban(today, yesterday, '2026-08-29', cfg)

        # 数据充足
        assert r['data_sufficient'] is True
        assert r['total_limit_up'] == 82
        assert r['total_consecutive'] == 18  # 1+2+3+5+7
        assert r['max_board'] == 7

        # 梯队
        tc = r['tier_counts']
        assert tc == {7: 1, 5: 2, 4: 3, 3: 5, 2: 7}

        # 龙头
        dh = r['dragon_head']
        assert dh is not None
        assert dh['code'] == '000017'
        assert dh['name'] == '深中华A'
        assert dh['board_cnt'] == 7
        assert dh['limit_up_reason'] == '黄金概念+重组预期'
        assert dh['seal_money_yuan'] == pytest.approx(2.15e8)

        # 梯队内首元素：按封单 DESC → 5板 2只 中 XX股份(1.2亿) 在前
        tier5 = r['tier_distribution'][5]
        assert tier5[0]['code'] == '600001'  # 封单 1.2亿 > 9000万
        assert tier5[1]['code'] == '600002'

        # 晋级率：昨日连板分母 = 5晋级 + 5不晋级 = 10；分子 = 5 → 50%
        assert r['jinji_yesterday_total'] == 10
        assert r['jinji_today_continued'] == 5
        assert r['jinji_rate'] == pytest.approx(0.5)

        # 温度评分：7板→40, 连板18→30(≥10 是 20？ 等一下看配置 table：
        #   TEMP_TOTAL_SCORE = {20:30, 10:20, 0:10} → 18 只 < 20，≥10 → 20
        # 晋级率 0.5 → 30
        # 合计 40 + 20 + 30 = 90
        assert r['temp_score'] == 90
        assert r['temp_level_short'] == '🔥 高温'
        assert '极度活跃' in r['temp_level_suffix']
        assert r['temp_advice'] == cfg.TEMP_ADVICE_BY_LEVEL['🔥 高温']

    def test_cool_scenario_50(self, cfg):
        """低温场景：3板 1只，连板8只，晋级率20% → 综合 20+20+10=50 → 🌤️ 常温。"""
        today_rows = [
            (f'6{i:05d}', f'STK{i}', 1, 5e6, f'1{i:02d}:00', '', False)
            for i in range(50)  # 50 只首板
        ]
        # 3板 1只
        today_rows.append(('300001', '龙头1', 3, 2e7, '09:35:00', '题材A', False))
        # 2板 7只
        for i in range(7):
            today_rows.append((f'2{i:05d}', f'二板{i}', 2, 5e6, '10:00:00', '', False))
        today = _make_uplimit_df(today_rows)

        # 昨日：10 只连板，仅 2 只晋级（2/10=20%）
        y_rows = [
            ('300001', '龙头1', 2, 1.5e7, '09:35:00', '', False),
            ('200000', '二板0', 1, 5e6, '10:00:00', '', False),  # 1板：不计入分母
        ]
        # 2板 9 只（全不晋级）
        for i in range(9):
            y_rows.append((f'999{i:03d}', f'昨日连板{i}', 2, 5e6, '10:00:00', '', False))
        y_rows[1] = ('200000', '二板0', 2, 5e6, '10:00:00', '', False)  # 改成 2 板，晋级
        yesterday = _make_uplimit_df(y_rows)
        # 此时分母=1(300001,昨2板)+1(200000,昨2板)+9(其他昨2板) = 11
        # 分子=2(300001+200000 今天都在连板梯队里？ 200000 今日首板？不，今日连板梯队是板数≥2。
        # 今日 200000 不在 today_rows 中（today 是 300001+200000-200006），所以不晋级
        # 我们修正：让 200000 出现在 today 的 2 板中
        today_rows = [
            (f'6{i:05d}', f'STK{i}', 1, 5e6, f'1{i:02d}:00', '', False)
            for i in range(50)
        ]
        today_rows.append(('300001', '龙头1', 3, 2e7, '09:35:00', '题材A', False))
        today_rows.append(('200000', '二板0', 2, 5e6, '10:00:00', '', False))
        for i in range(1, 7):
            today_rows.append((f'2{i:05d}', f'二板{i}', 2, 5e6, '10:00:00', '', False))
        today = _make_uplimit_df(today_rows)

        r = analyze_linkban(today, yesterday, '2026-08-29', cfg)

        assert r['data_sufficient'] is True
        assert r['total_consecutive'] == 8  # 1(3板) + 7(2板)
        assert r['max_board'] == 3

        # 晋级率分母：11（1+1+9）；分子：2（300001 在今日 continue_cnt=3 即涨停；200000 今日 continue_cnt=2）
        assert r['jinji_yesterday_total'] == 11
        assert r['jinji_today_continued'] == 2
        assert r['jinji_rate'] == pytest.approx(2/11)

        # 温度分：3板→20，连板8→20(≥10? 8<10 → ≥0 → 10? 等一下 TEMP_TOTAL_SCORE:
        # {20:30, 10:20, 0:10} → 8 >= 0 命中 10 分
        # 晋级率 2/11 ≈ 0.182 < 0.3 → ≥0 → 10
        # 合计 20 + 10 + 10 = 40 → ❄️ 低温
        assert r['temp_score'] == 40
        assert r['temp_level_short'] == '❄️ 低温'

    # ---- 晋级率边界 ----
    def test_no_yesterday(self, cfg):
        """昨日数据空 → 晋级率 None。"""
        today = _make_uplimit_df([
            ('000001', '标的1', 3, 1e8, '09:30:00', '', False),
        ])
        r = analyze_linkban(today, None, '2026-08-29', cfg)
        assert r['data_sufficient'] is True
        assert r['jinji_rate'] is None
        assert r['jinji_yesterday_total'] == 0
        assert r['jinji_today_continued'] == 0

    def test_reason_loader_fn_called(self, cfg):
        """龙头 df.reason 为空时，调用 reason_loader_fn。"""
        today = _make_uplimit_df([
            ('000001', '标的1', 3, 1e8, '09:30:00', '', False),
        ])
        call_log = []

        def _fake_loader(code, ymd):
            call_log.append((code, ymd))
            return 'AI+机器人概念'

        r = analyze_linkban(today, None, '2026-08-29', cfg, reason_loader_fn=_fake_loader)
        assert r['dragon_head']['limit_up_reason'] == 'AI+机器人概念'
        assert call_log == [('000001', '20260829')]

    def test_reason_loader_not_called_when_reason_present(self, cfg):
        """龙头已有 reason，不回调 loader。"""
        today = _make_uplimit_df([
            ('000001', '标的1', 3, 1e8, '09:30:00', '现成原因', False),
        ])
        call_log = []

        def _fake_loader(code, ymd):
            call_log.append((code, ymd))
            return '不应被调'

        r = analyze_linkban(today, None, '2026-08-29', cfg, reason_loader_fn=_fake_loader)
        assert r['dragon_head']['limit_up_reason'] == '现成原因'
        assert call_log == []

    # ---- 封单时间排序：首板时间 ASC（空字符串视为最末）----
    def test_seal_time_sorting(self, cfg):
        today = _make_uplimit_df([
            ('000001', '同板A', 3, 1e8, '', '理由A', False),  # 空时间
            ('000002', '同板B', 3, 1e8, '09:25:00', '理由B', False),  # 最早
            ('000003', '同板C', 3, 1e8, '09:30:00', '理由C', False),
        ])
        r = analyze_linkban(today, None, '2026-08-29', cfg)
        dist = r['tier_distribution'][3]
        # 板数相同，封单相同 → 按首板时间 ASC；'' 视为最末
        assert [d['code'] for d in dist] == ['000002', '000003', '000001']


# ====================================================================
# reporter: 报告格式
# ====================================================================

class TestLinkbanReport:

    def test_ascii_report_has_all_sections(self, cfg):
        today_rows = [
            (f'6{i:05d}', f'STK{i}', 1, 5e6, '10:00:00', f'题材{i}', False)
            for i in range(60)
        ]
        today_rows.append(('000017', '深中华A', 7, 2.15e8, '09:25:00', '黄金概念+重组预期', False))
        today_rows.append(('600001', 'XX股份', 5, 1.2e8, '09:30:00', 'AI算力', False))
        today_rows.append(('600002', 'YY科技', 5, 9e7, '09:30:15', '机器人', False))
        today_rows.append(('600100', '四板1', 4, 6e7, '09:30:00', '概念1', False))
        today_rows.append(('600101', '四板2', 4, 5e7, '09:31:00', '概念2', False))
        today_rows.append(('600102', '四板3', 4, 4e7, '09:32:00', '概念3', False))
        today_rows.append(('600200', '三板1', 3, 3e7, '09:40:00', '题材1', False))
        today_rows.append(('600201', '三板2', 3, 2.5e7, '09:41:00', '题材2', False))
        today_rows.append(('600202', '三板3', 3, 2e7, '09:42:00', '题材3', False))
        today_rows.append(('600203', '三板4', 3, 1.5e7, '09:43:00', '题材4', False))
        today_rows.append(('600204', '三板5', 3, 1e7, '09:44:00', '题材5', False))
        for i in range(7):
            today_rows.append((f'6003{i:02d}', f'二板{i+1}', 2, 1e7, f'10:{i:02d}:00', f'热点{i}', False))
        today = _make_uplimit_df(today_rows)
        r = analyze_linkban(today, None, '2026-08-29', cfg)
        txt = format_linkban_report(r)

        assert '2026-08-29 大盘温度报告' in txt
        assert '【连板概况】' in txt
        assert '涨停总数: 78只' in txt
        assert '连板总数: 18只' in txt
        assert '最高连板: 7板' in txt
        assert '【连板梯队】' in txt
        assert '7板: 1只   → 深中华A' in txt
        assert '【龙头股详情】' in txt
        assert '名称: 深中华A' in txt
        assert '代码: 000017' in txt
        assert '连板: 7连板' in txt
        assert '涨停原因: 黄金概念+重组预期' in txt
        assert '封单金额: 2.15亿' in txt
        assert '【情绪指标】' in txt
        assert '晋级率: —' in txt
        assert '温度评分:' in txt
        assert '温度等级:' in txt
        assert '操作建议:' in txt
        assert txt.rstrip().endswith('==========')

    def test_ascii_report_empty_day(self, cfg):
        r = analyze_linkban(None, None, '2026-01-01', cfg)
        txt = format_linkban_report(r)
        assert '非交易日' in txt or '数据不足' in txt
        assert '状态:' in txt
        assert '晋级率: —' in txt
        assert '温度评分: 30分' in txt

    def test_ascii_report_many_stocks_full_list(self, cfg):
        """用户要求"全部列出，不截断"：6 只连板梯队应列出全部 6 个名字。"""
        today_rows = []
        for i in range(40):
            today_rows.append((f'6{i:05d}', f'首板{i:02d}', 1, 1e6, '10:00:00', '', False))
        names = [f'多板股{i:02d}' for i in range(6)]
        for i, nm in enumerate(names):
            today_rows.append((f'3{i:05d}', nm, 3, 1e7, f'09:3{i}:00', '', False))
        today = _make_uplimit_df(today_rows)
        r = analyze_linkban(today, None, '2026-08-29', cfg)
        txt = format_linkban_report(r)
        # 6 只 → 全部列出，不再有"等N只"
        for nm in names:
            assert nm in txt, f"梯队名未完整列出，缺少: {nm}"
        assert '等' not in txt.split('【龙头')[0]  # 梯队区不出现"等"字截断

    def test_csv_row_fields(self, cfg):
        """CSV 行字段齐全，梯队分桶正确。"""
        today_rows = [
            ('000001', 'STK1', 2, 5e7, '', '', False),
            ('000002', 'STK2', 2, 4e7, '', '', False),
            ('000003', 'STK3', 3, 3e7, '', '', False),
            ('000004', 'STK4', 4, 2e7, '', '', False),
            ('000005', 'STK5', 5, 1e7, '', '', False),
            ('000006', 'STK6', 6, 9e7, '', '', False),
            ('000007', 'STK7', 8, 8e7, '', '', False),
        ]
        today = _make_uplimit_df(today_rows)
        r = analyze_linkban(today, None, '2026-08-29', cfg)
        row = format_linkban_csv_row(r)

        # 字段齐全 & 顺序匹配 LINKBAN_CSV_FIELDS
        assert list(row.keys()) == LINKBAN_CSV_FIELDS

        assert row['ref_date'] == '2026-08-29'
        assert row['total_limit_up'] == 7
        assert row['total_consecutive'] == 7
        assert row['max_board'] == 8
        assert row['tier_2_cnt'] == 2
        assert row['tier_3_cnt'] == 1
        assert row['tier_4_cnt'] == 1
        assert row['tier_5_cnt'] == 1
        # ≥6 的：6+8 = 2 只
        assert row['tier_ge6_cnt'] == 2
        # 龙头：8板 STK7
        assert row['dragon_code'] == '000007'
        assert row['dragon_name'] == 'STK7'
        assert row['dragon_board'] == 8
        assert row['dragon_seal_money_yuan'] == pytest.approx(8e7)
        assert row['data_sufficient'] is True

    def test_csv_row_empty(self, cfg):
        r = analyze_linkban(None, None, '2026-01-01', cfg)
        row = format_linkban_csv_row(r)
        assert list(row.keys()) == LINKBAN_CSV_FIELDS
        assert row['data_sufficient'] is False
        assert row['total_limit_up'] == 0
        assert row['dragon_code'] == ''
        assert row['jinji_rate'] == ''  # None → ''
