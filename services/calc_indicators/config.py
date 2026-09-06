"""开仓筛查工具 - 配置中心。

所有参数统一在此定义，main.py 中不再出现硬编码数值。
修改此文件即可调整全部判定逻辑。

通过 get_config("profile_name") 选择不同的配置档。
"""

from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass
class TradingConfig:
    # ── 数据获取 ──
    DATA_TRADING_DAYS: int = 120

    # ── 维度 A：结构 ──
    MA_PERIODS: Tuple[int, ...] = (5, 10, 20, 60)
    BREAKOUT_WINDOW: int = 20
    PULLBACK_THRESHOLD: float = 0.01  # 1% 偏离（长线回踩，兼容保留）

    # ── 维度 A 增强：短线回踩信号 ──
    USE_PULLBACK_ENHANCE: bool = True     # True=启用短线回踩增强逻辑，False=回退原 MA20 偏离判定
    PULLBACK_DEBUG: bool = True           # True=打印详细回踩排查日志到控制台
    PULLBACK_MA_TARGETS: List[int] = field(default_factory=lambda: [5, 10])
    PULLBACK_MAX_DEVIATION: float = 0.005        # 0.5%，回踩偏离度容忍阈值
    PULLBACK_REQUIRE_MA_UP: bool = True          # 必须均线方向向上
    PULLBACK_REQUIRE_SHRINK_VOLUME: bool = True   # 必须缩量
    PULLBACK_VOLUME_SHRINK_RATIO: float = 0.8    # 缩量阈值：当前量 < 5日均量 × 此系数
    PULLBACK_REQUIRE_BULLISH_CANDLE: bool = True # 要求阳线或锤子线
    PULLBACK_CONFIRM_BARS: int = 2               # 回踩确认窗口（最近 N 根 K 线）

    # ── 维度 B：动量 ──
    MACD_FAST: int = 12
    MACD_SLOW: int = 26
    MACD_SIGNAL: int = 9
    RSI_WINDOW: int = 14
    RSI_OVERSOLD: float = 30.0
    RSI_OVERBOUGHT: float = 70.0
    VOLUME_MA_PERIOD: int = 5
    VOLUME_SURGE_RATIO: float = 1.5
    MOMENTUM_DEBUG: bool = True           # True=打印详细动量排查日志到控制台

    # ── 维度 B 增强：大盘环境过滤 ──
    ENABLE_MARKET_FILTER: bool = False        # 默认关闭，保持向后兼容；短线建议开启
    MARKET_DEBUG: bool = True                 # True=打印详细大盘过滤排查日志到控制台
    BENCHMARK_INDEX: str = "sh000001"         # 基准指数（上证综指）；可改 'sz399001' / 'sh000300'
    BENCHMARK_MA_PERIOD: int = 20             # 大盘趋势判定均线周期
    BENCHMARK_MIN_CHANGE_PCT: float = -0.5    # 当日涨跌幅下限（-0.5 = 跌幅不超过 0.5%）
    BENCHMARK_FETCH_DAYS: int = 30            # 拉取指数的天数
    BENCHMARK_FETCH_TIMEOUT: int = 5          # 指数数据获取超时（秒）

    # ── 维度 C：赔率 ──
    STOP_MODE: str = "swing"
    PROFIT_MODE: str = "swing_enhanced"
    RR_WINDOW: int = 20
    MIN_RR_RATIO: float = 2.0
    ATR_PERIOD: int = 14
    ATR_STOP_MULT: float = 1.5
    ATR_PROFIT_MULT: float = 3.0
    FIXED_STOP_PCT: float = 0.02
    FIXED_PROFIT_MULT: float = 2.0

    # ── 仓位管理 ──
    MAX_RISK_PER_TRADE: float = 0.01
    TOTAL_CAPITAL: float = 100_000

    # ── 维度 D：周线趋势背景（独立观察哨，只读模式） ──
    # 设计原则：不参与 A/B/C 总分计算，不触发自动止损/开仓
    # 数据层完全解耦：独立调用周线接口，失败时仅输出"数据暂不可用"，绝不影响 A/B/C
    ENABLE_WEEKLY_OBSERVER: bool = True            # 维度D总开关（即便关闭也不影响 A/B/C，仅跳过周线区块显示）
    WEEKLY_DEBUG: bool = True                       # True=打印详细周线排查日志到控制台（[D-周线观察] 前缀）
    WEEKLY_FETCH_BARS: int = 120                    # 拉取最近 N 根周 K 线（需求单要求 120 根）
    WEEKLY_FETCH_TIMEOUT: int = 8                   # 周线数据单源获取超时秒数
    WEEKLY_CACHE_ENABLED: bool = True               # 每日 1 次缓存（首次调用后当天复用，避免频繁请求被禁 IP）
    WEEKLY_MA20_PERIOD: int = 20                    # 周线 MA20 周期（趋势判定）
    WEEKLY_MA60_PERIOD: int = 60                    # 周线 MA60 周期（牛熊线）
    WEEKLY_MA_TREND_THRESHOLD: float = 0.001        # 周线 MA20 趋势判定阈值：环比变化幅度 < 0.1% 视为走平
    WEEKLY_RSI_PERIOD: int = 14                      # 周线 RSI 周期
    WEEKLY_RSI_OVERSOLD: float = 30.0               # 周线 RSI 超卖线
    WEEKLY_RSI_OVERBOUGHT: float = 70.0            # 周线 RSI 超买线
    WEEKLY_RSI_NEUTRAL_LOW: float = 40.0           # 评级A 多头趋势的 RSI 下限
    WEEKLY_RSI_NEUTRAL_HIGH: float = 60.0          # 评级A 多头趋势的 RSI 上限
    WEEKLY_MACD_FAST: int = 12                      # 周线 MACD 快线
    WEEKLY_MACD_SLOW: int = 26                      # 周线 MACD 慢线
    WEEKLY_MACD_SIGNAL: int = 9                     # 周线 MACD 信号线
    WEEKLY_HIST_SHRINK_BARS: int = 3                # 判定 MACD 柱状体"连续缩短"所需的最小连续根数


@dataclass
class HighVolatilityConfig(TradingConfig):
    """急跌-反弹 高波动行情配置。

    针对市场波动放大、情绪极端的场景：
    - 维度A：放宽结构容忍度，允许偏离均线更远
    - 维度B：提高动量门槛，防止追高被套
    - 维度C：切换 ATR 动态追踪，止损更宽
    - 仓位：降低单笔风险，轻仓试错
    """

    # ── 维度 A：放宽结构容忍度 ──
    PULLBACK_THRESHOLD: float = 0.025   # 1% → 2.5%，允许偏离均线更远
    BREAKOUT_WINDOW: int = 10          # 20天 → 10天，只看最近的最强阻力支撑

    # ── 维度 B：提高动量过滤门槛 ──
    RSI_OVERSOLD: float = 25.0         # 30 → 25，要求跌得更透才考虑买入
    VOLUME_SURGE_RATIO: float = 1.8    # 1.5 → 1.8，要求更强的真金白银进场

    # ── 维度 C：切换 ATR 动态追踪 ──
    STOP_MODE: str = "atr"
    PROFIT_MODE: str = "atr"
    ATR_PERIOD: int = 14
    ATR_STOP_MULT: float = 2.0         # 1.5 → 2.0，止损放宽，防止被盘中毛刺扫掉
    ATR_PROFIT_MULT: float = 4.0       # 3.0 → 4.0，博取更大反弹空间

    # ── 仓位管理：降低单笔亏损上限 ──
    MAX_RISK_PER_TRADE: float = 0.008  # 1% → 0.8%，轻仓试错

    # ── 最小盈亏比：配合高波动稍微放宽 ──
    MIN_RR_RATIO: float = 1.8          # 2.0 → 1.8


@dataclass
class ConservativeConfig(TradingConfig):
    """保守型配置：适用于震荡市或不确定行情。

    - 结构：要求更严格的突破确认
    - 动量：只在深度超卖时才出手
    - 赔率：要求更高的盈亏比
    - 仓位：更轻
    """

    PULLBACK_THRESHOLD: float = 0.008   # 0.8%，只接受非常贴近均线的回踩
    RSI_OVERSOLD: float = 28.0          # 28，只在较深超卖时考虑
    VOLUME_SURGE_RATIO: float = 2.0     # 2.0倍，要求放量更充分
    MIN_RR_RATIO: float = 2.5           # 2.5:1，要求更高的安全边际
    MAX_RISK_PER_TRADE: float = 0.005  # 0.5%，更保守的仓位


@dataclass
class TrendFollowingConfig(TradingConfig):
    """趋势跟随配置：适用于单边行情。

    - 结构：放宽突破窗口，捕捉更大级别趋势
    - 动量：对放量要求更严格
    - 赔率：止损用 swing（结构支撑），止盈用 ATR 追踪
    """

    BREAKOUT_WINDOW: int = 30           # 30天，看更大级别的突破
    PULLBACK_THRESHOLD: float = 0.015   # 1.5%，允许正常回踩
    VOLUME_SURGE_RATIO: float = 1.8     # 1.8倍，趋势需要放量确认
    STOP_MODE: str = "swing"
    PROFIT_MODE: str = "atr"            # 止盈用 ATR 追踪，让利润奔跑
    ATR_PROFIT_MULT: float = 5.0        # 5倍 ATR，追踪大趋势


@dataclass
class AggressiveShortConfig(TradingConfig):
    """激进短线：精准踩 MA5，允许均线走平，严抓买点。

    适合日内/超短线操作，信号触发少但胜率高。
    """

    USE_PULLBACK_ENHANCE: bool = True
    PULLBACK_MA_TARGETS: List[int] = field(default_factory=lambda: [5])
    PULLBACK_MAX_DEVIATION: float = 0.003        # 0.3% 极严
    PULLBACK_REQUIRE_MA_UP: bool = False          # 允许走平
    PULLBACK_REQUIRE_SHRINK_VOLUME: bool = True
    PULLBACK_VOLUME_SHRINK_RATIO: float = 0.7    # 0.7 倍量，极度缩量
    PULLBACK_REQUIRE_BULLISH_CANDLE: bool = True
    PULLBACK_CONFIRM_BARS: int = 1               # 只看当天


@dataclass
class SteadyShortConfig(TradingConfig):
    """稳健短线：MA5/MA10 双均线确认，要求趋势向上。

    适合波段操作，信号稍多但可靠性高。
    """

    USE_PULLBACK_ENHANCE: bool = True
    PULLBACK_MA_TARGETS: List[int] = field(default_factory=lambda: [5, 10])
    PULLBACK_MAX_DEVIATION: float = 0.008        # 0.8% 稍宽
    PULLBACK_REQUIRE_MA_UP: bool = True          # 必须向上
    PULLBACK_REQUIRE_SHRINK_VOLUME: bool = True
    PULLBACK_VOLUME_SHRINK_RATIO: float = 0.6    # 0.6 倍量，极度缩量
    PULLBACK_REQUIRE_BULLISH_CANDLE: bool = True
    PULLBACK_CONFIRM_BARS: int = 2


@dataclass
class ShortTermConfig(TradingConfig):
    """超短线配置：强制大盘过滤。

    吃情绪溢价，大盘暴跌泥沙俱下，必须看大盘做个股。
    """

    ENABLE_MARKET_FILTER: bool = True
    BENCHMARK_INDEX: str = "sh000001"
    BENCHMARK_MA_PERIOD: int = 20
    BENCHMARK_MIN_CHANGE_PCT: float = -0.5


@dataclass
class UltraShortConfig(TradingConfig):
    """超短线止盈止损配置：紧止损 + 短期压力位止盈。

    - 止损：入场价 - 1.0×ATR（紧止损，超短线快速认错）
    - 止盈：10日最高价 + 2×ATR（以10日最高点为压力基准，上方再博取2倍ATR波动率空间）
    - 适合日内/超短线，与 short_term 大盘过滤叠加使用效果更佳
    """

    STOP_MODE: str = "atr"
    ATR_STOP_MULT: float = 1.0            # 1.0倍ATR，紧止损
    PROFIT_MODE: str = "swing_ultra"      # 10日最高 + N×ATR
    RR_WINDOW: int = 10                   # 10日最高点为压力基准
    ATR_PROFIT_MULT: float = 2.0          # 压力位上方2倍ATR
    ATR_PERIOD: int = 14


@dataclass
class SwingConfig(TradingConfig):
    """波段配置：放宽大盘涨跌幅限制。

    可忍受短期回调，但防股灾。
    """

    ENABLE_MARKET_FILTER: bool = True
    BENCHMARK_INDEX: str = "sh000001"
    BENCHMARK_MA_PERIOD: int = 20
    BENCHMARK_MIN_CHANGE_PCT: float = -1.0       # 允许 1% 跌幅


@dataclass
class LongTermConfig(TradingConfig):
    """长线价投配置：关闭大盘过滤。

    看内在价值，跌了反而便宜。
    """

    ENABLE_MARKET_FILTER: bool = False


# ── 配置注册表 ──

_CONFIG_REGISTRY: dict = {
    "default": TradingConfig,
    "high_vol": HighVolatilityConfig,
    "conservative": ConservativeConfig,
    "trend": TrendFollowingConfig,
    "aggressive_short": AggressiveShortConfig,
    "steady_short": SteadyShortConfig,
    "short_term": ShortTermConfig,
    "ultra_short": UltraShortConfig,
    "swing": SwingConfig,
    "long_term": LongTermConfig,
}

_PROFILE_DESCRIPTIONS: dict = {
    "default": "标准配置 - 日常使用",
    "high_vol": "高波动配置 - 急跌反弹行情",
    "conservative": "保守配置 - 震荡市/不确定行情",
    "trend": "趋势跟随配置 - 单边行情",
    "aggressive_short": "激进短线 - 精准踩MA5",
    "steady_short": "稳健短线 - MA5/MA10双均线确认",
    "short_term": "超短线 - 强制大盘过滤",
    "ultra_short": "超短线止盈止损 - 紧止损+10日高压基准",
    "swing": "波段 - 放宽大盘涨跌幅限制",
    "long_term": "长线价投 - 关闭大盘过滤",
}


def get_config(profile: str = "default") -> TradingConfig:
    """按名称获取配置实例。

    Args:
        profile: 配置档名称，可选值见 list_profiles()

    Returns:
        对应配置的实例

    Raises:
        ValueError: 未知的 profile 名称
    """
    cls = _CONFIG_REGISTRY.get(profile)
    if cls is None:
        available = ", ".join(_CONFIG_REGISTRY.keys())
        raise ValueError(f"未知配置档 '{profile}'，可用: {available}")
    return cls()


def merge_profiles(profile_names: list) -> tuple:
    """按顺序叠加多个配置档，返回 (合并后的实例, 字段来源字典)。

    规则：
        - 第一个 profile 作为基底
        - 后续每个 profile 只覆盖"**它自己显式定义过的字段**"（即：在配置类 __dict__ 中
          直接出现的字段；继承自父类的默认值不参与覆盖，避免误覆盖）
        - 叠加顺序是从左到右（后面的覆盖前面的同名显式字段）
        - 返回的第二个值是 `dict[field_name, source]`，用于 --show-config 时标注来源

    Args:
        profile_names: 配置档名称列表，例如 ['aggressive_short', 'long_term']
                       空列表会自动兜底 default。

    Returns:
        (merged_config: TradingConfig, field_sources: dict[str, str])

    Raises:
        ValueError: 列表中包含未知的 profile 名称
    """
    from dataclasses import fields as _dc_fields

    if not profile_names:
        profile_names = ["default"]

    merged = get_config(profile_names[0])
    # 每字段来源：初始默认第一个 profile
    field_sources = {f.name: profile_names[0] for f in _dc_fields(TradingConfig)}

    for p_name in profile_names[1:]:
        p_cls = _CONFIG_REGISTRY.get(p_name)
        if p_cls is None:
            available = ", ".join(_CONFIG_REGISTRY.keys())
            raise ValueError(f"未知配置档 '{p_name}'，可用: {available}")
        p_instance = p_cls()

        # 只覆盖该"子类**显式声明**"的字段（class.__dict__ 中存在的 dataclass field）
        explicit_in_subclass = set()
        for f in _dc_fields(p_cls):
            # 通过类层面检查该属性是在本类直接定义还是从父类继承
            if f.name in p_cls.__dict__:
                explicit_in_subclass.add(f.name)

        for f_name in explicit_in_subclass:
            new_val = getattr(p_instance, f_name)
            setattr(merged, f_name, new_val)
            field_sources[f_name] = p_name

    return merged, field_sources


def _parse_bool(s: str) -> bool:
    """宽松解析 bool 字符串。支持 true/1/yes/y/on 及反义词，大小写不敏感。"""
    v = s.strip().lower()
    if v in {"true", "1", "yes", "y", "on"}:
        return True
    if v in {"false", "0", "no", "n", "off"}:
        return False
    raise ValueError(f"无法解析为 bool: {s!r} (true/false/1/0/yes/no)")


def _parse_list_type(tp, s: str):
    """把逗号分隔字符串解析成 List[T]。支持 '[a,b]' 或直接 'a,b'。"""
    import typing as _typing
    origin = getattr(tp, "__origin__", None)
    args = getattr(tp, "__args__", ())
    if origin is list and args:
        inner_type = args[0]
    else:
        # 兜底：当作 str 列表
        inner_type = str
    stripped = s.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        stripped = stripped[1:-1]
    if not stripped:
        return []
    parts = [p.strip() for p in stripped.split(",") if p.strip() != ""]
    result = []
    for p in parts:
        if inner_type is int:
            result.append(int(p))
        elif inner_type is float:
            result.append(float(p))
        elif inner_type is bool:
            result.append(_parse_bool(p))
        else:
            # 去掉两端引号（若是字面量）
            if (p.startswith("'") and p.endswith("'")) or (p.startswith('"') and p.endswith('"')):
                p = p[1:-1]
            result.append(p)
    return result


def _parse_tuple_type(tp, s: str):
    """类似 list，但结果是 tuple。"""
    return tuple(_parse_list_type(tp, s))


def apply_overrides(
    config: TradingConfig,
    overrides: list,
    existing_sources: dict | None = None,
) -> dict:
    """逐个应用 key=value 覆盖到 config 实例，返回更新后的来源字典。

    每个 override 是 "KEY=VALUE" 字符串。
    按 TradingConfig 中对应字段的声明类型进行安全解析：

        - bool    : true/1/yes / false/0/no
        - int     : 十进制整数
        - float   : 十进制浮点
        - List[T] : 逗号分隔（如 '[5,10]' 或 '5,10'），按内部 T 解析
        - Tuple[T]: 同上，返回 tuple
        - str     : 原样（自动去掉两端引号）

    Args:
        config: 待修改的配置实例（会原地 modify）
        overrides: ["KEY=VAL", ...]
        existing_sources: 传入来自 merge_profiles() 的来源字典，会被更新为 "override:KEY=VAL"

    Returns:
        更新后的来源字典（与 existing_sources 是同一对象引用，方便链式调用）

    Raises:
        ValueError: KEY 不存在 / VALUE 类型解析失败
    """
    import typing as _typing
    from dataclasses import fields as _dc_fields

    if existing_sources is None:
        existing_sources = {f.name: "default" for f in _dc_fields(TradingConfig)}

    field_map = {f.name: f for f in _dc_fields(TradingConfig)}

    for idx, raw in enumerate(overrides):
        if "=" not in raw:
            raise ValueError(
                f"第 {idx+1} 个 override {raw!r} 格式错误，应为 KEY=VALUE（例如 ENABLE_MARKET_FILTER=false）")
        key, val_str = raw.split("=", 1)
        key = key.strip()
        val_str = val_str.strip()
        if key not in field_map:
            available = ", ".join(field_map.keys())
            raise ValueError(f"未知字段 {key!r}，可用字段: {available}")

        f = field_map[key]
        tp = f.type

        # 针对各种字段类型解析
        if tp is bool or (getattr(tp, "__origin__", None) is None and isinstance(tp, type) and issubclass(tp, bool)):
            value = _parse_bool(val_str)  # type: ignore[assignment]
        elif tp is int or (isinstance(tp, type) and issubclass(tp, int)):
            value = int(val_str, 0)  # 支持 0x / 0b 前缀
        elif tp is float or (isinstance(tp, type) and issubclass(tp, float)):
            value = float(val_str)
        elif tp is str or (isinstance(tp, type) and issubclass(tp, str)):
            if (val_str.startswith("'") and val_str.endswith("'")) or (val_str.startswith('"') and val_str.endswith('"')):
                value = val_str[1:-1]
            else:
                value = val_str
        elif getattr(tp, "__origin__", None) is list:
            value = _parse_list_type(tp, val_str)
        elif getattr(tp, "__origin__", None) is tuple:
            value = _parse_tuple_type(tp, val_str)
        else:
            # 兜底：尝试字符串直接赋，失败再报类型不支持
            try:
                value = val_str  # type: ignore[assignment]
                # 试一下类型是否兼容，这里只做启发式
                # 直接 setattr，让 dataclass 的自然类型在使用阶段暴露问题
            except Exception as e:  # pragma: no cover
                raise ValueError(f"字段 {key} 类型 {tp!r} 暂不支持从命令行 override: {e}") from e

        setattr(config, key, value)
        existing_sources[key] = f"override:{raw}"

    return existing_sources


def list_profiles() -> str:
    """返回所有可用配置档的描述文本。"""
    lines = []
    for name, desc in _PROFILE_DESCRIPTIONS.items():
        cls = _CONFIG_REGISTRY[name]
        lines.append(f"  --profile {name:<14s} {desc}  ({cls.__name__})")
    return "\n".join(lines)