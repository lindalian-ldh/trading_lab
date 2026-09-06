"""AI 预留客户端：默认 Mock 实现，返回 0.5。

只做占位，不引入 openai/anthropic 等重包，避免依赖膨胀。
真实调用以注释形式预留，便于后续启用。
"""
from __future__ import annotations

from config.settings import settings
from core.logger import get_logger

log = get_logger(__name__)


class AIClient:
    """轻量 AI 客户端占位。

    - AI_ENABLED=False：直接返回 0.5（Mock）。
    - AI_ENABLED=True：预留 requests.post 调用（注释形态，不报错）。
    """

    def __init__(self) -> None:
        self.enabled = settings.ai_enabled
        self.api_key = settings.ai_api_key
        self.api_url = settings.ai_api_url

    def analyze(self, text: str) -> float:
        """对文本进行情绪/评分分析，返回 [0,1] 分数。"""
        if not self.enabled:
            log.debug("AI 未启用，返回 Mock 分数 0.5")
            return 0.5

        # ---- 预留真实调用（暂不启用，避免依赖膨胀）----
        # import requests
        # resp = requests.post(
        #     self.api_url,
        #     headers={"Authorization": f"Bearer {self.api_key}"},
        #     json={"text": text},
        #     timeout=30,
        # )
        # resp.raise_for_status()
        # return float(resp.json().get("score", 0.5))
        log.warning("AI 已启用但真实调用尚未实现，仍返回 0.5")
        return 0.5
