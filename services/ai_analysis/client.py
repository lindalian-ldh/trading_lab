"""DeepSeek 视觉模型客户端（OpenAI 兼容多模态接口）。

调用 DeepSeek 官方 /v1/chat/completions 端点，将股票截图以 Base64 内联
data URL 形式发送给 deepseek-v4-flash-vision-exp 模型，结合用户输入的
盘中模拟盘口数据，返回结构化 JSON 评估结果。

设计要点：
- 离线/未配置时返回 Mock 结果（probability=0.5），便于 CLI 流程验证
- 网络异常重试 3 次（间隔 1s），HTTP 4xx/5xx 直接报错不重试
- response_format=json_object 强制 JSON 输出；解析时容错剥离 ```json 代码块
- 校验返回 dict 必需字段，缺失补默认值
"""
from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Any

from config.settings import settings
from core.logger import get_logger

log = get_logger("ai_analysis.client")

# 模型返回 JSON 必需字段及默认值
_REQUIRED_FIELDS: dict[str, Any] = {
    "conditions": {},
    "probability": 0.5,
    "hold_conditions": [],
    "main_reason": "",
    "risk_alert": "",
}


class DeepSeekVisionClient:
    """DeepSeek 视觉分析客户端。"""

    def __init__(self) -> None:
        self.enabled = settings.ai_enabled
        self.api_key = settings.ai_api_key
        self.api_url = settings.ai_api_url
        self.model = settings.ai_model

    # ------------------------------------------------------------------ #
    # 公开方法
    # ------------------------------------------------------------------ #

    def analyze_chart(
        self,
        image_path: Path,
        price_now: float,
        volume_ratio: float,
        vol_multiplier: float,
    ) -> dict:
        """分析股票截图，返回结构化评估 dict。

        参数
        ----
        image_path : PNG 图片完整路径
        price_now : 当前现价
        volume_ratio : 预估量比
        vol_multiplier : 预估全天成交量 / 昨日成交量

        返回
        ----
        dict : 含 conditions/probability/hold_conditions/main_reason/risk_alert 五字段
        """
        if not self.enabled or not self.api_key:
            log.warning(
                "AI 未启用或 API_KEY 为空（enabled=%s, key_empty=%s），返回 Mock 结果",
                self.enabled, not bool(self.api_key),
            )
            return self._mock_result(price_now, volume_ratio)

        # 延迟导入，避免未启用 AI 时强制依赖 requests
        import requests

        from prompt import build_prompt

        data_url = self._load_image_as_base64(image_path)
        prompt_text = build_prompt(price_now, volume_ratio, vol_multiplier)

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_text},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        log.info("调用 DeepSeek 视觉模型: model=%s image=%s", self.model, image_path.name)
        start = time.time()
        raw_content = self._post_with_retry(requests, payload, headers)
        elapsed = time.time() - start
        log.info("DeepSeek 响应耗时 %.2fs", elapsed)

        result = self._parse_content(raw_content)
        log.info(
            "分析完成: probability=%s passes_structure_A=%s",
            result.get("probability"),
            result.get("conditions", {}).get("passes_structure_A"),
        )
        return result

    # ------------------------------------------------------------------ #
    # 内部方法
    # ------------------------------------------------------------------ #

    @staticmethod
    def _load_image_as_base64(path: Path) -> str:
        """读取 PNG 文件，返回 data:image/png;base64,<b64> 形式的 data URL。"""
        data = path.read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:image/png;base64,{b64}"

    def _post_with_retry(self, requests_mod, payload: dict, headers: dict) -> str:
        """发送 POST 请求，网络异常重试 3 次（间隔 1s）。

        HTTP 4xx/5xx 直接抛 RuntimeError（鉴权/配额问题重试无意义）。
        返回 choices[0].message.content 字符串。
        """
        retries = 3
        delay = 1.0
        last_exc: Exception | None = None
        for i in range(retries):
            try:
                resp = requests_mod.post(
                    self.api_url, headers=headers, json=payload, timeout=120,
                )
                if resp.status_code >= 400:
                    raise RuntimeError(
                        f"DeepSeek API HTTP {resp.status_code}: {resp.text[:500]}"
                    )
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                return content
            except (RuntimeError, KeyError) as e:
                # HTTP 错误或响应结构异常：不重试
                raise
            except Exception as e:  # noqa: BLE001 - 网络层异常需统一重试
                last_exc = e
                log.warning("第 %d/%d 次请求失败: %s", i + 1, retries, e)
                if i < retries - 1:
                    time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _parse_content(content: str) -> dict:
        """解析模型返回的 content 字符串为 dict。

        容错处理：模型可能包裹 ```json ... ``` 代码块，剥离后 json.loads。
        缺失字段补默认值。
        """
        s = content.strip()
        # 剥离 ```json ... ``` 或 ``` ... ``` 包裹
        m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", s, re.DOTALL)
        if m:
            s = m.group(1).strip()
        try:
            result = json.loads(s)
        except json.JSONDecodeError as e:
            log.error("DeepSeek 返回非合法 JSON: %s", e)
            log.debug("原始内容: %s", content[:500])
            return dict(_REQUIRED_FIELDS)
        # 补缺失字段
        for key, default in _REQUIRED_FIELDS.items():
            if key not in result:
                result[key] = default
        return result

    @staticmethod
    def _mock_result(price_now: float, volume_ratio: float) -> dict:
        """Mock 结果：结构完整，probability=0.5，便于离线验证。"""
        return {
            "conditions": {
                "price_now": str(price_now),
                "volume_ratio": str(volume_ratio),
                "passes_structure_A": False,
            },
            "probability": 0.5,
            "hold_conditions": [
                "收盘价站稳 16.00 以上（有效突破 MA60）",
                "量比 > 1.5（放量突破）",
                "MACD 红柱放大且 RSI 进入 50-70 强势区",
            ],
            "main_reason": "[Mock] AI 未启用，返回默认中性概率 0.5 供 CLI 流程验证。",
            "risk_alert": "[Mock] 此为占位结果，未实际调用模型。",
        }
