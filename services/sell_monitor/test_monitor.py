"""卖出监控系统测试：覆盖验收标准的 6 个场景 + 关键边界用例。

验收场景（来自 spec 第六节）：
    1. 开仓后价格直接暴跌至硬止损 → sell_all / 硬止损触发
    2. 价格涨到+2%（止损距离的1倍）→ 保本止损上移（hold，止损从-2% → 0%）
    3. 价格涨到+5.1% → 触发第1层网格，卖出30%，止损上移
    4. 价格涨到+15% → 触发第3层网格，卖出全部
    5. 持仓满20天，浮盈仅+0.5% → sell_all / 时间止损
    6. 价格涨到12%后回调至触发回撤>3% → sell_all / 移动回撤止损

补充测试：
    - 网格跳层：profit 直接跳到 +15% 时，应清仓（而非只触发第1层）
    - 多层累计：profit 跳到 +10% 时，应一次性触发第1+2层
    - 已关闭持仓：返回 hold
    - 空 df：返回 hold
"""

from __future__ import annotations

import sys
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
import pytest

# 把 sell_monitor 目录加入 sys.path，便于 `from config import ...`
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from config import ExitConfig  # noqa: E402
from position import Position  # noqa: E402
from monitor import check_exit_signals  # noqa: E402


# ====================================================================
# 测试夹具
# ====================================================================

@pytest.fixture
def default_config() -> ExitConfig:
    """标准 default 配置（与 spec 一致）。"""
    return ExitConfig()


@pytest.fixture
def entry_date_str() -> str:
    """开仓日：2026-07-15（周四）。"""
    return "2026-07-15"


@pytest.fixture
def base_position(entry_date_str, default_config) -> Position:
    """标准持仓：600550，10.00 元开仓 1000 股。"""
    return Position.create("600550", 10.00, 1000, entry_date_str, default_config)


def _make_df(entry_date: str, bars_after_entry: int,
             prices: list[tuple[float, float, float, float]] = None,
             end_date: str = None) -> pd.DataFrame:
    """构造测试用 K线 DataFrame。

    Args:
        entry_date: 开仓日 YYYY-MM-DD
        bars_after_entry: 开仓日之后的K线根数
        prices: 每根K线的 (open, high, low, close)；
                若为 None，则全部填充 (10, 10, 10, 10)
        end_date: 最后一根K线的日期；若为 None，则从 entry_date 起按"交易日"递增

    Returns:
        DataFrame，列：date(str) / open / high / low / close / volume
        行数 = 1（开仓日）+ bars_after_entry
    """
    rows = []
    # 开仓日当天的K线（不计入 bars_held）
    base_dt = datetime.strptime(entry_date, "%Y-%m-%d")
    rows.append({
        'date': entry_date,
        'open': 10.0, 'high': 10.0, 'low': 10.0, 'close': 10.0, 'volume': 1000,
    })
    # 后续 bars_after_entry 根K线（跳过周末，简化为每周5个交易日）
    cur_dt = base_dt + timedelta(days=1)
    cnt = 0
    while cnt < bars_after_entry:
        # 跳过周六/周日
        if cur_dt.weekday() < 5:
            if prices and cnt < len(prices):
                o, h, l, c = prices[cnt]
            else:
                o = h = l = c = 10.0
            rows.append({
                'date': cur_dt.strftime("%Y-%m-%d"),
                'open': o, 'high': h, 'low': l, 'close': c, 'volume': 1000,
            })
            cnt += 1
        cur_dt += timedelta(days=1)
    df = pd.DataFrame(rows)
    return df


# ====================================================================
# 验收场景 1：硬止损触发
# ====================================================================

def test_scenario_1_hard_stop_triggers(base_position, default_config):
    """开仓后价格直接暴跌至硬止损 → sell_all / 硬止损触发。

    开仓价 10.00，硬止损 = 10 × (1-0.02) = 9.80。
    构造一根 low=9.50（跌穿 9.80）的K线。
    """
    pos = base_position
    df = _make_df(pos.entry_date, bars_after_entry=1,
                  prices=[(10.0, 10.0, 9.50, 9.60)])  # low=9.50 < 9.80

    signal = check_exit_signals(pos, df, default_config)

    assert signal['action'] == 'sell_all'
    assert signal['reason'] == '硬止损触发'
    assert signal['sell_price'] == pytest.approx(9.80, abs=0.01)
    assert signal['sell_shares'] == 1000
    assert pos.sold_shares == 1000
    assert pos.remaining_shares == 0


# ====================================================================
# 验收场景 2：保本止损上移
# ====================================================================

def test_scenario_2_break_even_stop_move(base_position, default_config):
    """价格涨到+2%（止损距离的1倍）→ 止损上移至成本价（保本）。

    开仓价 10.00，硬止损 9.80，BREAK_EVEN_TRIGGER=1.0，
    break_even_target_pct = 0.02 × 1.0 = 0.02 = +2%。
    当浮盈达到 +2%（现价 10.20）时，止损应从 9.80 上移到 10.00。
    动作应该是 hold（不卖出）。
    """
    pos = base_position
    # 一根 close=10.20 (+2%) 的K线
    df = _make_df(pos.entry_date, bars_after_entry=1,
                  prices=[(10.0, 10.20, 10.0, 10.20)])

    signal = check_exit_signals(pos, df, default_config)

    assert signal['action'] == 'hold'
    assert signal['reason'] == '保本止损上移'
    assert pos.stop_loss_price == pytest.approx(10.00, abs=0.001)
    assert pos.hard_stop_price == pytest.approx(9.80, abs=0.001)  # 硬止损不变
    # 没有卖出
    assert pos.sold_shares == 0


# ====================================================================
# 验收场景 3：触发第1层网格，卖出30%
# ====================================================================

def test_scenario_3_grid_level_1_partial_sell(base_position, default_config):
    """价格涨到+5.1% → 触发第1层网格，卖出30%，止损上移。

    默认 GRID_LEVELS[0] = (0.05, 0.30)。
    现价 10.51 (+5.1%) ≥ 5% → 触发第1层。
    卖出 = 1000 × 0.30 = 300 股，剩余 700 股。
    止损上移到 10.50 × (1-0.01) = 10.395，且不低于保本线 10.00。
    """
    pos = base_position
    df = _make_df(pos.entry_date, bars_after_entry=1,
                  prices=[(10.0, 10.51, 10.0, 10.51)])  # +5.1%

    signal = check_exit_signals(pos, df, default_config)

    assert signal['action'] == 'sell_partial'
    assert '网格' in signal['reason']
    assert signal['sell_shares'] == 300
    assert pos.sold_shares == 300
    assert pos.remaining_shares == 700
    assert pos.grid_level_index == 1
    # 止损上移：trigger_price=10.50, candidate=10.50*0.99=10.395
    assert pos.stop_loss_price == pytest.approx(10.395, abs=0.01)


# ====================================================================
# 验收场景 4：触发第3层网格，清仓
# ====================================================================

def test_scenario_4_grid_level_3_clear_all(base_position, default_config):
    """价格涨到+15% → 触发第3层网格，卖出全部。

    默认 GRID_LEVELS[2] = (0.15, 0.40)（最后一层）。
    现价 11.50 (+15%) ≥ 15% → 触发第3层（最后一层），清仓。
    sell_shares = 1000（全部）
    """
    pos = base_position
    df = _make_df(pos.entry_date, bars_after_entry=1,
                  prices=[(10.0, 11.50, 10.0, 11.50)])  # +15%

    signal = check_exit_signals(pos, df, default_config)

    assert signal['action'] == 'sell_all'
    assert '网格' in signal['reason']
    assert signal['sell_shares'] == 1000
    assert pos.sold_shares == 1000
    assert pos.remaining_shares == 0
    assert pos.grid_level_index == 3  # 三层全部触发完


# ====================================================================
# 验收场景 5：时间止损
# ====================================================================

def test_scenario_5_time_stop(base_position, default_config):
    """持仓满20天，浮盈仅+0.5% → sell_all / 时间止损。

    MAX_HOLDING_BARS=20，TIME_STOP_MIN_PROFIT_PCT=0.01。
    bars_held=20 且 profit=0.005 < 0.01 → 触发时间止损。
    """
    pos = base_position
    # 20 根K线，每根 close=10.05 (+0.5%)
    prices = [(10.0, 10.05, 10.0, 10.05) for _ in range(20)]
    df = _make_df(pos.entry_date, bars_after_entry=20, prices=prices)

    signal = check_exit_signals(pos, df, default_config)

    assert signal['action'] == 'sell_all'
    assert signal['reason'] == '时间止损'
    assert pos.bars_held == 20
    assert signal['sell_shares'] == 1000


# ====================================================================
# 验收场景 6：移动回撤止损
# ====================================================================

def test_scenario_6_trailing_stop(base_position, default_config):
    """价格涨到+12%后回调至触发回撤>3% → sell_all / 移动回撤止损。

    TRAILING_ACTIVATE_PCT=0.08，TRAILING_REGRET_PCT=0.03。

    构造：highest=11.20 (+12%)，cur=10.80 (+8%)。
    profit(+8%) >= TRAILING_ACTIVATE_PCT(+8%) ✓
    regret = (11.20-10.80)/11.20 = 3.57% >= 3% ✓ → 触发移动回撤止损。

    注：用默认配置时，+5% 会先触发第1层网格（卖出30%，止损上移到 10.395），
    +10% 触发第2层（卖出剩余70%的30%=210股，止损上移到 10.89），
    +12% 时还在网格间隔内，highest=11.20。
    当 cur=10.80 时：
      - cur_low (假设=10.80) > stop_loss (10.89)? 10.80 < 10.89 → 当前止损被破！

    因此为干净地测移动回撤止损，使用一个禁用网格的配置，或调高 grid 阈值。
    这里用一个临时配置：ENABLE_GRID_EXIT=False 来隔离 trailing 逻辑。
    """
    config = ExitConfig(ENABLE_GRID_EXIT=False)
    pos = Position.create("600550", 10.00, 1000, "2026-07-15", config)

    # 第1根：达到 +12%（highest=11.20）
    # 第2根：从 11.20 回撤到 +8%（cur=10.80），regret=3.57%
    prices = [
        (10.0, 11.20, 10.0, 11.20),   # 第1根：最高 11.20
        (11.20, 11.20, 10.80, 10.80),  # 第2根：回撤到 10.80
    ]
    df = _make_df(pos.entry_date, bars_after_entry=2, prices=prices)

    signal = check_exit_signals(pos, df, config)

    assert signal['action'] == 'sell_all'
    assert signal['reason'] == '移动回撤止损'
    # 卖出价 = highest × (1 - 0.03) = 11.20 × 0.97 = 10.864
    assert signal['sell_price'] == pytest.approx(10.864, abs=0.01)


# ====================================================================
# 补充测试：网格跳层
# ====================================================================

def test_grid_skip_to_last_level_clears_all(base_position, default_config):
    """价格直接跳到 +15% 时，应一次清仓（而非只触发第1层）。

    验证跳层逻辑：grid_level_index 从 0 直接跳到 3，sell_all。
    """
    pos = base_position
    df = _make_df(pos.entry_date, bars_after_entry=1,
                  prices=[(10.0, 11.50, 10.0, 11.50)])  # +15%

    signal = check_exit_signals(pos, df, default_config)

    assert signal['action'] == 'sell_all'
    assert pos.grid_level_index == 3
    assert pos.remaining_shares == 0


def test_grid_skip_multiple_levels_partial(base_position, default_config):
    """价格跳到 +10% 时，应一次性触发第1+2层（卖出累计59%，剩余41%）。

    第1层：卖 30%，剩余 70%
    第2层：卖剩余 70% 的 30% = 21%，剩余 49%
    累计卖出 51%，剩余 49%（1000 股 → 490 股）
    """
    pos = base_position
    df = _make_df(pos.entry_date, bars_after_entry=1,
                  prices=[(10.0, 11.00, 10.0, 11.00)])  # +10%

    signal = check_exit_signals(pos, df, default_config)

    assert signal['action'] == 'sell_partial'
    assert pos.grid_level_index == 2  # 跳过第1层，直接到第2层
    # 累计卖出：1 - (1-0.3)*(1-0.3) = 1 - 0.49 = 0.51
    assert signal['sell_shares'] == 510  # 1000 × 0.51
    assert pos.remaining_shares == 490
    # 止损上移：trigger_price=11.00, candidate=11.00*0.99=10.89
    assert pos.stop_loss_price == pytest.approx(10.89, abs=0.01)


# ====================================================================
# 补充测试：边界用例
# ====================================================================

def test_closed_position_returns_hold(base_position, default_config):
    """已关闭的持仓：返回 hold / 无操作。"""
    pos = base_position
    pos.status = 'closed'
    pos.sold_shares = 1000

    df = _make_df(pos.entry_date, bars_after_entry=1)
    signal = check_exit_signals(pos, df, default_config)

    assert signal['action'] == 'hold'
    assert '已关闭' in signal['message']


def test_empty_df_returns_hold(base_position, default_config):
    """空 df：返回 hold / 无行情数据。"""
    pos = base_position
    signal = check_exit_signals(pos, pd.DataFrame(), default_config)

    assert signal['action'] == 'hold'
    assert '无行情数据' in signal['message']


def test_hold_when_no_conditions_met(base_position, default_config):
    """没有任何触发条件时：返回 hold / 继续持有。"""
    pos = base_position
    # 现价 +1%（未达保本2%，未达网格5%，未达trailing 8%）
    df = _make_df(pos.entry_date, bars_after_entry=1,
                  prices=[(10.0, 10.10, 10.0, 10.10)])  # +1%

    signal = check_exit_signals(pos, df, default_config)

    assert signal['action'] == 'hold'
    assert signal['reason'] == '继续持有'
    assert pos.sold_shares == 0
    assert pos.stop_loss_price == pytest.approx(9.80, abs=0.001)  # 止损未动


def test_stop_loss_only_moves_up(base_position, default_config):
    """止损价只升不降：触发保本后，后续行情回落（但未跌破新止损）应保持止损不变。"""
    pos = base_position

    # 第1次：浮盈 +2% → 保本上移到 10.00
    df1 = _make_df(pos.entry_date, bars_after_entry=1,
                   prices=[(10.0, 10.20, 10.0, 10.20)])
    sig1 = check_exit_signals(pos, df1, default_config)
    assert pos.stop_loss_price == pytest.approx(10.00, abs=0.001)

    # 第2次：浮盈回落到 +0.5%，intraday low=10.02（高于新止损 10.00，不触发）
    # 此时无任何卖出条件，应返回 hold，且止损应保持在 10.00（不回退到 9.80）
    df2 = _make_df(pos.entry_date, bars_after_entry=2,
                   prices=[(10.0, 10.20, 10.0, 10.20),
                           (10.10, 10.15, 10.02, 10.05)])
    sig2 = check_exit_signals(pos, df2, default_config)
    assert sig2['action'] == 'hold'
    assert pos.stop_loss_price == pytest.approx(10.00, abs=0.001)


def test_consecutive_grid_levels(base_position, default_config):
    """连续触发网格：先+5%触发第1层，再+10%触发第2层，再+15%清仓。

    验证多根K线推进的网格逻辑。
    """
    pos = base_position

    # 第1根：+5% → 第1层触发，卖 300，剩余 700
    df1 = _make_df(pos.entry_date, bars_after_entry=1,
                   prices=[(10.0, 10.50, 10.0, 10.50)])
    sig1 = check_exit_signals(pos, df1, default_config)
    assert sig1['action'] == 'sell_partial'
    assert sig1['sell_shares'] == 300
    assert pos.remaining_shares == 700
    assert pos.grid_level_index == 1

    # 第2根：+10% → 第2层触发，卖 700×0.30=210，剩余 490
    df2 = _make_df(pos.entry_date, bars_after_entry=2,
                   prices=[(10.0, 10.50, 10.0, 10.50),
                           (10.50, 11.00, 10.50, 11.00)])
    sig2 = check_exit_signals(pos, df2, default_config)
    assert sig2['action'] == 'sell_partial'
    assert sig2['sell_shares'] == 210
    assert pos.remaining_shares == 490
    assert pos.grid_level_index == 2

    # 第3根：+15% → 第3层触发，清仓 490
    df3 = _make_df(pos.entry_date, bars_after_entry=3,
                   prices=[(10.0, 10.50, 10.0, 10.50),
                           (10.50, 11.00, 10.50, 11.00),
                           (11.00, 11.50, 11.00, 11.50)])
    sig3 = check_exit_signals(pos, df3, default_config)
    assert sig3['action'] == 'sell_all'
    assert sig3['sell_shares'] == 490
    assert pos.remaining_shares == 0
    assert pos.grid_level_index == 3


# ====================================================================
# 持仓对象单元测试
# ====================================================================

def test_position_create_locks_rules(default_config):
    """Position.create 应在开仓时锁定所有卖出规则。"""
    pos = Position.create("600550", 10.00, 1000, "2026-07-15", default_config)

    assert pos.entry_price == 10.00
    assert pos.shares == 1000
    assert pos.hard_stop_price == pytest.approx(9.80, abs=0.001)
    assert pos.stop_loss_price == pos.hard_stop_price  # 初始=硬止损
    assert pos.break_even_price == 10.00
    assert pos.grid_level_index == 0
    assert pos.sold_shares == 0
    assert pos.highest_price_since_entry == 10.00
    assert pos.bars_held == 0
    assert pos.status == "open"
    assert pos.is_closed is False


def test_position_remaining_shares(default_config):
    """remaining_shares 计算正确。"""
    pos = Position.create("600550", 10.00, 1000, "2026-07-15", default_config)
    assert pos.remaining_shares == 1000

    pos.sold_shares = 300
    assert pos.remaining_shares == 700

    pos.sold_shares = 1000
    assert pos.remaining_shares == 0
    assert pos.is_closed is True


def test_position_to_dict_round_trip(default_config):
    """to_dict / from_dict 往返一致。"""
    pos1 = Position.create("600550", 10.00, 1000, "2026-07-15", default_config)
    pos1.sold_shares = 300
    pos1.grid_level_index = 1
    pos1.stop_loss_price = 10.40
    pos1.highest_price_since_entry = 10.60
    pos1.bars_held = 5

    d = pos1.to_dict()
    pos2 = Position.from_dict(d)

    assert pos2.symbol == pos1.symbol
    assert pos2.entry_price == pos1.entry_price
    assert pos2.shares == pos1.shares
    assert pos2.entry_date == pos1.entry_date
    assert pos2.hard_stop_price == pos1.hard_stop_price
    assert pos2.stop_loss_price == pos1.stop_loss_price
    assert pos2.break_even_price == pos1.break_even_price
    assert pos2.grid_level_index == pos1.grid_level_index
    assert pos2.sold_shares == pos1.sold_shares
    assert pos2.highest_price_since_entry == pos1.highest_price_since_entry
    assert pos2.bars_held == pos1.bars_held
    assert pos2.status == pos1.status


# ====================================================================
# 配置档测试
# ====================================================================

def test_exit_config_profiles_load():
    """三个配置档都能加载且参数有差异。"""
    from config import get_exit_config

    default = get_exit_config("default")
    conservative = get_exit_config("conservative")
    trend = get_exit_config("trend")

    # 三个配置档的 HARD_STOP_PCT 应有差异
    assert default.HARD_STOP_PCT == 0.02
    assert conservative.HARD_STOP_PCT == 0.015
    assert trend.HARD_STOP_PCT == 0.025

    # 三个配置档的 MAX_HOLDING_BARS 应有差异
    assert default.MAX_HOLDING_BARS == 20
    assert conservative.MAX_HOLDING_BARS == 15
    assert trend.MAX_HOLDING_BARS == 40

    # 网格层级数量
    assert len(default.GRID_LEVELS) == 3
    assert len(conservative.GRID_LEVELS) == 3
    assert len(trend.GRID_LEVELS) == 3

    # 未知配置档应抛 ValueError
    with pytest.raises(ValueError):
        get_exit_config("nonexistent_profile")


# ====================================================================
# data_loader 集成测试（需网络，默认跳过）
# ====================================================================

import os as _os
_NET_TEST_ENABLED = _os.environ.get('ENABLE_NETWORK_TESTS', '0') == '1'


@pytest.mark.skipif(
    not _NET_TEST_ENABLED,
    reason='需要网络连接，设置 ENABLE_NETWORK_TESTS=1 启用'
)
class TestDataLoaderIntegration:
    """data_loader 集成测试：验证 efinance / baostock 数据源可用性。

    运行方式：ENABLE_NETWORK_TESTS=1 uv run pytest -v -k "TestDataLoaderIntegration"
    """

    def test_fetch_klines_efinance(self):
        """测试 efinance 获取 K 线数据。"""
        from data_loader import fetch_klines
        df = fetch_klines('600550', '2026-07-01', '2026-08-12')
        assert df is not None
        assert len(df) > 0
        assert 'close' in df.columns
        assert 'high' in df.columns
        assert 'low' in df.columns
        assert 'open' in df.columns
        # 最后一行应该是最新日期
        assert df['date'].iloc[-1] >= '2026-08-10'


def test_normalize_code_invalid():
    """测试无效股票代码的错误处理（纯函数，无需网络）。"""
    from data_loader import _normalize_code
    # 无效代码应该在 _normalize_code 中抛 ValueError
    with pytest.raises(ValueError):
        _normalize_code('00001')  # 5 位代码
    with pytest.raises(ValueError):
        _normalize_code('abcdef')  # 非数字代码
