# -*- coding: utf-8 -*-
"""权重判定模块：打分卡 → 市场状态 → 目标权重。

style 模式（BASE_WEIGHT=0.50, OFFENSIVE_BASE_WEIGHT=0.50）:
    总分 ≥ +2  → 标准配置  防御 50% / 进攻 50% / 现金 0%
    0 ~ +1     → 防御偏高  防御 60% / 进攻 40% / 现金 0%
    ≤ -1       → 降级模式  防御 40% / 进攻 30% / 现金 30%

sector 模式（BASE_WEIGHT=0.60, OFFENSIVE_BASE_WEIGHT=0.40）:
    总分 ≥ +2  → 标准配置  防御 60% / 进攻 40% / 现金 0%
    0 ~ +1     → 防御偏高  防御 70% / 进攻 30% / 现金 0% （防御更偏）
    ≤ -1       → 降级模式  防御 40% / 进攻 30% / 现金 30%

注：sector 模式下"防御偏高"档由 main.py 通过覆盖 HIGH_DEFENSIVE_WEIGHT 实现，
本函数只读 cfg.* 顶层变量，与 main.py 的参数覆盖模式一致。
"""

import barbell_config as cfg


def determine_weights(score):
    """根据综合总分返回目标权重字典。

    Returns:
        dict: {regime, defensive, offensive, cash}
    """
    if score is None:
        score = 0
    if score >= 2:
        # 标准配置：防御/进攻按各自基础权重
        return {
            "regime": "标准配置",
            "defensive": cfg.BASE_WEIGHT,
            "offensive": cfg.OFFENSIVE_BASE_WEIGHT,
            "cash": 0.00,
        }
    if score >= 0:
        # 防御偏高：在标准基础上防御 +10pp、进攻 -10pp
        return {
            "regime": "防御偏高",
            "defensive": cfg.BASE_WEIGHT + 0.10,
            "offensive": max(cfg.OFFENSIVE_BASE_WEIGHT - 0.10, 0.0),
            "cash": 0.00,
        }
    return {
        "regime": "降级模式",
        "defensive": 0.40,
        "offensive": 0.30,
        "cash": 0.30,
    }
