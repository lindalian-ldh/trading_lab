"""单元测试占位。

运行：uv run pytest
"""
from core.ai_client import AIClient
from core.storage import _safe_symbol


def test_ai_mock_returns_half():
    """AI 未启用时应返回 Mock 分数 0.5。"""
    assert AIClient().analyze("any text") == 0.5


def test_safe_symbol():
    """交易对符号应被转换为安全的目录名。"""
    assert _safe_symbol("BTC/USDT") == "BTC_USDT"
    assert _safe_symbol("ETH/USDT:USDC") == "ETH_USDT_USDC"
