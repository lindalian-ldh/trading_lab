"""通知推送预留接口（当前仅控制台实现，后续可扩展飞书/邮件/微信）。

统一接口：
    notifier = get_notifier("console")
    notifier.notify(markdown_text: str, title: Optional[str] = None)

扩展新渠道（示例思路，非必须实现）：
    - FeishuNotifier: 通过飞书机器人 webhook 推送
    - EmailNotifier: SMTP 发送 Markdown 邮件
    - WxPusherNotifier: 微信 PUSH
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ====================================================================
# 抽象基类
# ====================================================================

class BaseNotifier:
    """通知推送抽象基类。子类必须实现 send()。"""

    def notify(self, markdown_text: str, title: Optional[str] = None) -> bool:
        """推送一条 Markdown 消息。

        Args:
            markdown_text: Markdown 格式的消息正文
            title: 可选标题（部分渠道支持单独标题字段）

        Returns:
            bool: 推送是否成功
        """
        try:
            return self.send(markdown_text, title or '')
        except Exception as e:
            logger.exception("通知推送失败: %s", e)
            return False

    def send(self, markdown_text: str, title: str) -> bool:
        """子类实现具体推送逻辑。返回 True 成功。"""
        raise NotImplementedError


# ====================================================================
# 控制台 Notifier（当前唯一实现，默认使用）
# ====================================================================

class ConsoleNotifier(BaseNotifier):
    """控制台打印 Markdown（用于 main.py 输出完整报告到 stdout）。

    用法：
        n = ConsoleNotifier()
        n.notify("# 标题\n正文...")  # 直接 print，不写日志
    """

    def __init__(self, separator: bool = True):
        """
        Args:
            separator: 是否在消息前后打印分隔线
        """
        self.separator = separator

    def send(self, markdown_text: str, title: str) -> bool:
        if self.separator:
            print('\n' + '=' * 72)
        if title:
            print(f'📢 {title}')
            if self.separator:
                print('-' * 72)
        print(markdown_text)
        if self.separator:
            print('=' * 72 + '\n')
        return True


# ====================================================================
# 工厂函数
# ====================================================================

_NOTIFIER_REGISTRY = {
    'console': ConsoleNotifier,
}


def get_notifier(channel: str = 'console', **kwargs) -> BaseNotifier:
    """按渠道名获取 Notifier 实例。

    Args:
        channel: 渠道名，当前仅支持 'console'
        **kwargs: 透传给 Notifier 构造函数

    Returns:
        BaseNotifier 实例
    """
    cls = _NOTIFIER_REGISTRY.get(channel)
    if cls is None:
        available = ', '.join(_NOTIFIER_REGISTRY.keys())
        logger.warning("未知通知渠道 '%s'，回退到 console。可用: %s",
                        channel, available)
        cls = ConsoleNotifier
    return cls(**kwargs)
