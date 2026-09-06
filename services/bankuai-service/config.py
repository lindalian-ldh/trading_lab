"""板块扫描配置中心。

所有可调参数集中于此，遵循"配置驱动"约定。plate_type 在 zzshare 后端为整数编码，
本模块提供中文名称 → 整数编码的映射，便于 CLI 传入 ``--plate-type 概念``。

zzshare 板块类型编码（来源：zzshare client.SHORTCUTS）：
    17 → 题材
    15 → 概念
    14 → 行业
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

# 板块类型：中文名 → zzshare 整数编码
PLATE_TYPE_MAP: dict[str, int] = {
    "概念": 15,
    "题材": 17,
    "行业": 14,
}

# 反向映射（编码 → 中文名），用于日志/报告展示
PLATE_TYPE_REVERSE: dict[int, str] = {v: k for k, v in PLATE_TYPE_MAP.items()}


def resolve_plate_type(value) -> int:
    """把 plate_type 解析为 zzshare 整数编码。

    Args:
        value: int（15/17/14）或 str（"概念"/"题材"/"行业"）

    Returns:
        zzshare plate_type 整数编码

    Raises:
        ValueError: 无法识别的板块类型
    """
    if isinstance(value, int):
        if value not in PLATE_TYPE_REVERSE:
            raise ValueError(f"未知板块类型编码: {value}，支持: {list(PLATE_TYPE_REVERSE)}")
        return value
    if isinstance(value, str):
        key = value.strip()
        if key not in PLATE_TYPE_MAP:
            raise ValueError(f"未知板块类型: {value}，支持: {list(PLATE_TYPE_MAP)}")
        return PLATE_TYPE_MAP[key]
    raise ValueError(f"plate_type 必须为 int 或 str，收到 {type(value)}")


@dataclass
class ScanConfig:
    """板块扫描与龙头梯队识别的全部可调参数。"""

    # ===== 板块排名 =====
    plate_type: int = 15              # 板块类型编码（15=概念）
    top_n: int = 10                   # 热门板块数量
    bottom_n: int = 10                # 冷门板块数量
    sectors_filter: str = ""         # 指定板块名称（逗号分隔），非空时仅扫描匹配的板块

    # ===== 龙头梯队 =====
    tier_size: int = 6                # 每个梯队最大数量
    tier_min: int = 3                 # 每个梯队最小数量（不足时从剩余池补足）
    stock_limit: int = 30             # 拉取成分股上限（传给 market_plate_stocks.limit）

    # ===== 一级龙头综合评分权重（加权求和，单位：分，总分 100）=====
    # 评分项：
    #   1) 涨幅相对强度（change - 板块均幅，min-max 归一化到 0-100）
    #   2) 量比异动（vol_ratio，min-max 归一化）
    #   3) 换手异动（turnover_ratio，min-max 归一化）
    #   4) 封板强度：涨停股池可用 → 涨停时间越早越高（9:30=100,15:00=0）；
    #                涨停数据缺失 → 降级用相对强度归一化（与分项1同源，作为封板时间代理）
    #   5) 板块内首个涨停：额外加分（first_limit_bonus）
    # 硬过滤条件不变：change > 板块均幅；不足 tier_min 时放宽为 change > 0
    w_change: float = 30.0            # 涨幅相对强度权重
    w_vol_ratio: float = 25.0         # 量比异动权重
    w_turnover: float = 20.0          # 换手异动权重
    w_seal: float = 25.0             # 封板强度权重（含降级）
    first_limit_bonus: float = 10.0   # 板块内首个涨停加分
    enable_uplimit_pool: bool = True  # 是否拉取涨停股池增强一级判定（False 时全程降级用相对强度）

    # ===== 接口控制 =====
    request_interval: float = 2.0     # 请求间隔(秒)，zzshare 免费版 30次/分钟
    timeout: int = 5                  # 单次请求超时(秒)
    retry_times: int = 2              # 失败重试次数（不含首次）
    overall_timeout: int = 90         # 整体扫描超时(秒)

    # ===== 鉴权 =====
    token: str = ""                   # zzshare token，留空则从环境变量 ZZSHARE_TOKEN 读取

    # ===== 涨停连板梯队（zzshare uplimit_stocks / uplimit_hot）=====
    enable_uplimit_ladder: bool = True   # 是否生成涨停连板梯队
    enable_uplimit_hot: bool = False     # 是否额外拉取 uplimit_hot 热门板块视图（需多一次请求）
    ladder_show_top_n: int = 5           # 每个连板组最多展示几只股
    ladder_min_limit: int = 1            # 最低展示的连板数（1=含首板，2=仅2板及以上）

    # ===== 派生（不序列化） =====
    cache_dir: Optional[str] = None   # JSON 原始缓存目录，None 时由 data_loader 决定

    @classmethod
    def from_env(cls, **overrides) -> "ScanConfig":
        """从环境变量构造，并允许调用方覆盖任意字段。"""
        token = os.environ.get("ZZSHARE_TOKEN", "")
        cfg = cls(token=token)
        for k, v in overrides.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg

    def plate_type_name(self) -> str:
        """返回板块类型中文名（用于报告展示）。"""
        return PLATE_TYPE_REVERSE.get(self.plate_type, str(self.plate_type))


# 兼容原始需求文档中的 CONFIG dict 形式（便于快速查阅对照）
DEFAULT_CONFIG_DICT: dict = {
    "plate_type": "概念",
    "top_n": 10,
    "bottom_n": 10,
    "tier_size": 6,
    "request_interval": 2,
    "timeout": 5,
    "retry_times": 2,
}
