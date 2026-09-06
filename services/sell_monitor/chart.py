"""退出路线图：开仓时绘制全部卖出价位，让交易者"看得见未来的每一步"。

参考 5.2 节"生成退出路线图（开仓时即绘制）"。

字体配置沿用 calc_indicators/chart.py 的方案：
    - 显式 Agg backend（必须在 pyplot 之前）
    - PingFang SC 优先的中文字体回退链
    - 显式为所有 text 元素传 FontProperties（三保险）

图布局：
    - X 轴：浮盈百分比（顶部副轴）+ 价格（底部主轴）
    - Y 轴：价格
    - 水平线：入场价 / 硬止损 / 保本线 / 各级网格 / 移动止损激活线
    - 右侧标注：每条线的名称 + 价格
"""

from __future__ import annotations

import logging
from pathlib import Path

# ---- 第一优先级：设置 Agg backend（必须在 pyplot 之前）----
import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties, findfont
from matplotlib.lines import Line2D

from config import ExitConfig
from position import Position

logger = logging.getLogger(__name__)


# ==================== 字体与全局配置 ====================

_FONT_CANDIDATES = [
    "PingFang SC", "Heiti TC", "STHeiti", "Hiragino Sans GB",
    "Arial Unicode MS", "Microsoft YaHei", "SimHei", "WenQuanYi Zen Hei",
]
_font_ready: bool = False
_font_name: str | None = None
_fp_obj: FontProperties | None = None
_fp_bold: FontProperties | None = None


def _setup_font_and_rc() -> None:
    """配置全局 matplotlib 参数 + 中文字体。仅执行一次。"""
    global _font_ready, _font_name, _fp_obj, _fp_bold
    if _font_ready:
        return

    for font in _FONT_CANDIDATES:
        try:
            fp = FontProperties(family=font)
            path = findfont(fp, fallback_to_default=False)
            if path and "LastResort" not in path and "DejaVu" not in path:
                _font_name = font
                break
        except Exception:
            continue

    family = ([_font_name, "sans-serif"] if _font_name else ["sans-serif"])
    sans = ([_font_name, "Arial", "DejaVu Sans"]
            if _font_name else ["DejaVu Sans"])
    matplotlib.rcParams.update({
        "font.family": family,
        "font.sans-serif": sans,
        "axes.unicode_minus": False,
        "figure.facecolor": "white",
        "figure.dpi": 150,
        "savefig.dpi": 150,
    })
    if _font_name:
        _fp_obj = FontProperties(family=_font_name)
        _fp_bold = FontProperties(family=_font_name, weight="bold")
    _font_ready = True


# ==================== 路线图绘制 ====================

def render_exit_route_map(position: Position, config: ExitConfig,
                          out_path: Path | str) -> Path:
    """绘制退出路线图：水平线标注各卖出价位 + 文字说明。

    X 轴范围：从 -HARD_STOP_PCT*1.5 到 最后一层网格 + 5%（或 TRAILING_ACTIVATE + 5%）
    Y 轴范围：随 X 轴价格区间动态调整

    Args:
        position: 持仓实例（开仓时创建）
        config: ExitConfig 实例
        out_path: 输出 PNG 路径

    Returns:
        Path(out_path)
    """
    _setup_font_and_rc()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    entry = position.entry_price
    hard_stop = position.hard_stop_price
    break_even = position.break_even_price
    grid_levels = config.GRID_LEVELS if config.ENABLE_GRID_EXIT else []

    # ---- X 轴范围 ----
    x_min_pct = -config.HARD_STOP_PCT * 1.5
    if grid_levels:
        x_max_pct = grid_levels[-1][0] + 0.05
    else:
        x_max_pct = config.TRAILING_ACTIVATE_PCT + 0.05

    p_min = entry * (1 + x_min_pct)
    p_max = entry * (1 + x_max_pct)

    fig, ax = plt.subplots(figsize=(13, 7.5))

    # ---- 收集要画的水平线 ----
    # 元素: (label, price, color, linestyle, linewidth)
    lines_to_draw = []

    # 入场价
    lines_to_draw.append(("入场价", entry, "#3b82f6", "-", 2.0))
    # 硬止损
    lines_to_draw.append((f"硬止损 (-{config.HARD_STOP_PCT * 100:.1f}%)",
                          hard_stop, "#dc2626", "--", 2.0))
    # 保本线
    lines_to_draw.append(("保本线 (浮盈触发)", break_even, "#f59e0b", ":", 1.5))

    # 各级网格
    grid_colors = ["#10b981", "#059669", "#047857", "#065f46", "#064e3b"]
    for i, (lvl_pct, ratio) in enumerate(grid_levels):
        price = entry * (1 + lvl_pct)
        color = grid_colors[i % len(grid_colors)]
        is_last = (i == len(grid_levels) - 1)
        ratio_label = "清仓" if is_last else f"卖 {ratio * 100:.0f}%"
        lines_to_draw.append(
            (f"网格第{i + 1}层 (+{lvl_pct * 100:.0f}%): {ratio_label}",
             price, color, "--", 1.5)
        )

    # 移动止损激活线
    trailing_act_price = entry * (1 + config.TRAILING_ACTIVATE_PCT)
    if trailing_act_price < p_max:
        lines_to_draw.append(
            (f"移动止损激活 (+{config.TRAILING_ACTIVATE_PCT * 100:.0f}%)",
             trailing_act_price, "#8b5cf6", "-.", 1.2)
        )

    # ---- 画水平线 ----
    for label, price, color, ls, lw in lines_to_draw:
        ax.axhline(price, color=color, lw=lw, linestyle=ls)

    # ---- 右侧标注每条线 ----
    for label, price, color, _ls, _lw in lines_to_draw:
        ax.annotate(
            f"  {label}: {price:.2f}",
            xy=(1.0, price), xycoords=('axes fraction', 'data'),
            xytext=(5, 0), textcoords='offset points',
            color=color, fontsize=8.5, va='center',
            fontproperties=_fp_obj,
        )

    # ---- 顶部副轴：浮盈百分比刻度 ----
    ax2 = ax.secondary_xaxis('top', functions=(
        lambda p: (p - entry) / entry * 100,    # price -> pct
        lambda pct: entry * (1 + pct / 100),    # pct -> price
    ))
    ax2.set_xlabel("浮盈百分比 (%)", fontproperties=_fp_obj)

    # ---- 主轴样式 ----
    ax.set_xlim(p_min, p_max)
    ax.set_ylim(p_min * 0.97, p_max * 1.04)
    ax.set_xlabel("价格 (元)", fontproperties=_fp_obj)
    ax.set_ylabel("价格 (元)", fontproperties=_fp_obj)

    title = (f"{position.symbol} 退出路线图  "
             f"开仓价 {entry:.2f}  开仓日 {position.entry_date}")
    ax.set_title(title, fontsize=13, pad=15, fontproperties=_fp_bold)

    # ---- 图例（左上角，避免与右侧标注冲突）----
    legend_elements = [
        Line2D([0], [0], color=c, lw=1.5, linestyle=ls, label=lab)
        for lab, _, c, ls, _ in lines_to_draw
    ]
    ax.legend(handles=legend_elements, loc='upper left', fontsize=8,
              framealpha=0.95, prop=_fp_obj)

    ax.grid(True, alpha=0.3)

    # ---- 底部说明文字 ----
    fig.text(
        0.5, 0.01,
        (f"进场即锁定：硬止损 {hard_stop:.2f} → 保本 {break_even:.2f} → "
         + " → ".join([f"第{i + 1}层 {entry * (1 + p):.2f}"
                       for i, (p, _) in enumerate(grid_levels)])
         + f"  | 时间止损 {config.MAX_HOLDING_BARS} 根  | "
         f"移动止损 {config.TRAILING_ACTIVATE_PCT * 100:.0f}%激活/"
         f"{config.TRAILING_REGRET_PCT * 100:.0f}%回撤"),
        ha='center', va='bottom', fontsize=8.5, color="#4b5563",
        fontproperties=_fp_obj,
    )

    fig.subplots_adjust(top=0.88, bottom=0.13, left=0.07, right=0.78)
    fig.savefig(out_path, bbox_inches='tight', facecolor='white')
    plt.close(fig)

    logger.info("退出路线图已生成: %s", out_path)
    return out_path
