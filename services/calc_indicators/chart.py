#!/usr/bin/env python3
"""开仓筛查可视化报告生成器（阶段三 - 修复版）。

修复：
- 输出路径改为 trading_lab/data/reports/indicator-png/
- 空白图片：显式 Agg backend + 全局字体配置 + 强制渲染 + 英文 fallback
- axes 索引：按 "panel i -> axes[2*i]" 规则建立映射字典 + 调试 log
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

# ---- 第一优先级：设置 Agg backend（必须在 pyplot 之前）----
import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import mplfinance as mpf
import numpy as np
import pandas as pd
from matplotlib.font_manager import FontProperties, findfont
from matplotlib.lines import Line2D

from config import TradingConfig

logger = logging.getLogger(__name__)


# ==================== 字体与全局配置 ====================

_FONT_CANDIDATES = [
    "PingFang SC", "Heiti TC", "STHeiti", "Hiragino Sans GB",
    "Arial Unicode MS", "Microsoft YaHei", "SimHei", "WenQuanYi Zen Hei",
]
_font_ready: bool = False
_font_name: str | None = None


def _setup_font_and_rc(verbose: bool = False) -> None:
    """配置全局 matplotlib 参数 + 中文字体。仅执行一次。

    关键修复：mplfinance 的 base_mpf_style（如 'charles'）内部会覆盖 font.family
    回 DejaVu Sans，导致中文渲染失败。因此必须：
    1) 全局 rcParams 设置字体（基础保险）
    2) 在 mpf.make_mpf_style 的 rc= 参数里也显式传字体（关键！对抗 base style 覆盖）
    3) 在 mpf.plot() 后对所有 axes 再次设置字体（三保险）
    """
    global _font_ready, _font_name
    if _font_ready:
        return

    # ---- 先找到可用中文字体 ----
    # findfont(fallback_to_default=False) 在字体不存在时会抛异常，用 try/except 跳过
    for font in _FONT_CANDIDATES:
        try:
            fp = FontProperties(family=font)
            path = findfont(fp, fallback_to_default=False)
            # 排除 fallback 到默认字体的情况
            if path and "LastResort" not in path and "DejaVu" not in path:
                _font_name = font
                if verbose or logger.isEnabledFor(logging.DEBUG):
                    logger.info("[图表] 找到中文字体: %s -> %s", font, path)
                break
        except Exception:
            continue

    # ---- 全局 rcParams（基础保险）----
    font_family_list = [_font_name, "sans-serif"] if _font_name else ["sans-serif"]
    font_sans_list = ([_font_name, "Arial", "DejaVu Sans"]
                      if _font_name else ["DejaVu Sans"])
    matplotlib.rcParams.update({
        "font.family": font_family_list,
        "font.sans-serif": font_sans_list,
        "axes.unicode_minus": False,
        "figure.facecolor": "white",
        "figure.dpi": 150,
        "savefig.dpi": 150,
        "savefig.facecolor": "white",
        "savefig.edgecolor": "white",
        "axes.grid": True,
        "grid.color": "#e5e7eb",
        "grid.linestyle": "--",
        "grid.linewidth": 0.5,
        "axes.facecolor": "#fafbfc",
        "axes.edgecolor": "#d1d5db",
        "axes.labelcolor": "#374151",
        "xtick.color": "#4b5563",
        "ytick.color": "#4b5563",
    })

    _font_ready = True
    if verbose or logger.isEnabledFor(logging.DEBUG):
        logger.info("[图表] 字体配置完成: %s | backend=%s | font.family=%s",
                    _font_name or "(无中文,走英文)", matplotlib.get_backend(),
                    matplotlib.rcParams.get("font.family"))


def _get_mpf_rc_extras() -> dict:
    """返回要传给 mpf.make_mpf_style(rc=...) 的字体相关 rc 参数。

    这是关键：对抗 base_mpf_style='charles' 对字体的覆盖。
    """
    if _font_name:
        return {
            "font.family": _font_name,
            "font.sans-serif": [_font_name, "DejaVu Sans"],
            "axes.unicode_minus": False,
        }
    return {"axes.unicode_minus": False}


# ==================== 数据预处理 ====================

def _to_mpf_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.rename(columns={
        "date": "Date", "open": "Open", "high": "High",
        "low": "Low", "close": "Close", "volume": "Volume",
    }).copy()
    out["Date"] = pd.to_datetime(out["Date"])
    out = out.set_index("Date")
    return out[["Open", "High", "Low", "Close", "Volume"]].astype(float)


def _calc_indicators(df: pd.DataFrame, config: TradingConfig) -> dict:
    close = df["close"]
    volume = df["volume"]

    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(config.MA_PERIODS[2] if len(config.MA_PERIODS) >= 3 else 20).mean()
    ma60 = close.rolling(config.MA_PERIODS[3] if len(config.MA_PERIODS) >= 4 else 60).mean()
    vol_ma5 = volume.rolling(config.VOLUME_MA_PERIOD).mean()

    ema_fast = close.ewm(span=config.MACD_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=config.MACD_SLOW, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=config.MACD_SIGNAL, adjust=False).mean()
    hist = (dif - dea) * 2

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(config.RSI_WINDOW).mean()
    loss = -delta.clip(upper=0).rolling(config.RSI_WINDOW).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))

    return {
        "ma5": ma5, "ma20": ma20, "ma60": ma60, "vol_ma5": vol_ma5,
        "dif": dif, "dea": dea, "hist": hist, "rsi": rsi,
    }


# ==================== 样式 ====================

def _make_mpf_marketcolors():
    """红涨绿跌：中国股市配色。"""
    return mpf.make_marketcolors(
        up="#dc2626",
        down="#059669",
        edge="inherit",
        wick="inherit",
        volume={"up": "#dc2626", "down": "#059669"},
        inherit=True,
    )


# ==================== 坐标轴映射 ====================

def _map_panel_axes(axes: list) -> dict:
    """根据经验 #488515：mplfinance 每个 panel 对应 2 个 axes（主/次）。

    规则: panel i 的主轴 = axes[2*i], 次轴 = axes[2*i+1]。
    返回: {0: main_ax, 1: vol_ax, 2: macd_ax, 3: rsi_ax}
    """
    n_axes = len(axes)
    n_panels = n_axes // 2
    mapping = {}
    for i in range(min(4, n_panels)):
        mapping[i] = axes[2 * i]
    logger.debug("[图表] axes 映射: n_axes=%d n_panels=%d -> %s",
                 n_axes, n_panels, {k: f"axes[{2*k}]" for k in mapping.keys()})
    return mapping


# ==================== 主函数 ====================

def generate_chart_report(
    df: pd.DataFrame,
    config: TradingConfig,
    symbol: str,
    a_result: tuple,
    b_result: tuple,
    c_result: tuple,
    pos: dict,
    out_dir: Path | str | None = None,
    auto_open: bool = False,
    stock_name: str = "",
) -> Path:
    """生成图表可视化报告 PNG。"""
    _setup_font_and_rc(verbose=True)

    a_pass, a_msg = a_result
    b_pass, b_signals = b_result
    c_pass, c_info = c_result
    all_pass = a_pass and b_pass and c_pass

    # ---- 数据 ----
    mpf_df = _to_mpf_df(df)
    indicators = _calc_indicators(df, config)
    n = len(mpf_df)
    idx = mpf_df.index

    cur_price = float(c_info["price"])
    stop_loss = float(c_info["stop"])
    take_profit = float(c_info["take"])
    ratio = c_info["ratio"]
    ratio_str = f"{ratio:.2f}" if ratio != float("inf") else "inf"
    shares = pos.get("shares", 0)

    # ---- 失败/通过文案（优先中文，无字体时 fallback 英文）----
    # 注意：用 √/× 代替 ✅/❌，因为 PingFang SC 不含 emoji 字符
    use_cn = _font_name is not None
    if all_pass:
        title_text_cn = "[√] 三维度全部通过，符合开仓条件"
        title_text_en = "PASS - All 3 dimensions OK"
        title_color = "#059669"
    else:
        failed_dims = []
        if not a_pass: failed_dims.append("A")
        if not b_pass: failed_dims.append("B")
        if not c_pass: failed_dims.append("C")
        fail_hint_cn = f"  (未通过: {', '.join(failed_dims)})"
        fail_hint_en = f"  (Failed: {', '.join(failed_dims)})"
        title_text_cn = f"[x] 不符合开仓条件{fail_hint_cn}"
        title_text_en = f"FAIL - Not Ready{fail_hint_en}"
        title_color = "#dc2626"

    title_text = title_text_cn if use_cn else title_text_en

    # ---- addplot 列表 ----
    addplots: list = []

    # 主图均线
    addplots.append(mpf.make_addplot(indicators["ma5"], color="#f59e0b", width=1.0, panel=0))
    addplots.append(mpf.make_addplot(indicators["ma20"], color="#3b82f6", width=1.1, panel=0))
    addplots.append(mpf.make_addplot(indicators["ma60"], color="#8b5cf6", width=1.1, panel=0))

    # 止损/止盈虚线
    stop_line = pd.Series([stop_loss] * n, index=idx)
    addplots.append(mpf.make_addplot(stop_line, color="#dc2626", width=1.2, linestyle="--", panel=0))
    profit_line = pd.Series([take_profit] * n, index=idx)
    addplots.append(mpf.make_addplot(profit_line, color="#059669", width=1.2, linestyle="--", panel=0))

    # 入场价⭐
    entry_marker = pd.Series([np.nan] * n, index=idx)
    entry_marker.iloc[-1] = cur_price
    addplots.append(mpf.make_addplot(
        entry_marker, type="scatter", marker="*", markersize=220,
        color="#fbbf24", panel=0,
    ))

    # 副图1（panel=1）：成交量（先画灰色基础柱）
    vol_series = pd.Series(df["volume"].values, index=idx)
    addplots.append(mpf.make_addplot(
        vol_series, type="bar", width=0.6, color="#9ca3af", alpha=0.5, panel=1,
    ))
    # 放量红色高亮柱
    vol_ma5 = indicators["vol_ma5"]
    vol_surge_flag = (vol_ma5 > 0) & (vol_series.values > vol_ma5.values * config.VOLUME_SURGE_RATIO)
    vol_surge_series = pd.Series(
        np.where(vol_surge_flag, vol_series.values, np.nan),
        index=idx,
    )
    addplots.append(mpf.make_addplot(
        vol_surge_series, type="bar", width=0.6, color="#dc2626", alpha=0.9, panel=1,
    ))
    # 5日均量线
    addplots.append(mpf.make_addplot(vol_ma5, color="#3b82f6", width=1.0, panel=1))

    # 副图2（panel=2）：MACD hist 柱
    hist = indicators["hist"]
    hist_colors = ["#dc2626" if v >= 0 else "#059669" for v in hist.fillna(0).values]
    addplots.append(mpf.make_addplot(
        hist.fillna(0), type="bar", width=0.7, color=hist_colors, alpha=0.8, panel=2,
    ))
    # DIF / DEA 线
    addplots.append(mpf.make_addplot(indicators["dif"], color="#f59e0b", width=1.1, panel=2))
    addplots.append(mpf.make_addplot(indicators["dea"], color="#3b82f6", width=1.1, panel=2))

    # 副图3（panel=3）：RSI
    rsi = indicators["rsi"]
    addplots.append(mpf.make_addplot(rsi, color="#8b5cf6", width=1.3, panel=3))
    rsi_30 = pd.Series([config.RSI_OVERSOLD] * n, index=idx)
    addplots.append(mpf.make_addplot(rsi_30, color="#059669", width=0.8, linestyle="--", panel=3))
    rsi_70 = pd.Series([config.RSI_OVERBOUGHT] * n, index=idx)
    addplots.append(mpf.make_addplot(rsi_70, color="#dc2626", width=0.8, linestyle="--", panel=3))

    # ---- 创建 mpf style（marketcolors + rc 字体对抗 base style 覆盖）----
    mc = _make_mpf_marketcolors()
    custom_style = mpf.make_mpf_style(
        base_mpf_style="default",           # 改用 default，避免 charles 对字体的覆盖
        marketcolors=mc,
        rc=_get_mpf_rc_extras(),            # 关键：显式传字体 rc，对抗 base style
        mavcolors=["#f59e0b", "#3b82f6", "#8b5cf6"],
    )

    # ---- 绘图 ----
    try:
        fig, axes = mpf.plot(
            mpf_df,
            type="candle",
            style=custom_style,
            addplot=addplots,
            volume=False,                 # 我们在 addplot 里自绘成交量
            figratio=(16, 12),            # 稍微调窄高度，给文本留空间
            figscale=1.2,
            panel_ratios=(3, 1.0, 1.2, 1.0),
            returnfig=True,
            tight_layout=False,
            datetime_format="%Y-%m-%d",
            xrotation=15,
            warn_too_much_data=10000,
        )
    except Exception as e:
        logger.exception("[图表] mpf.plot 失败，尝试降级：移除成交量放量高亮和五角星")
        # 降级：移除最可能出错的 bar+scatter addplot，再试一次
        try:
            addplots_fallback = [a for a in addplots if "type" not in str(a)]
        except Exception:
            addplots_fallback = addplots[:3] + addplots[6:9]  # 仅均线 + 止损止盈 + MACD/RSI 线
        fig, axes = mpf.plot(
            mpf_df,
            type="candle",
            style=custom_style,
            addplot=addplots_fallback,
            volume=False,
            figratio=(16, 12),
            figscale=1.2,
            panel_ratios=(3, 1.0, 1.2, 1.0),
            returnfig=True,
            tight_layout=False,
            datetime_format="%Y-%m-%d",
            xrotation=15,
            warn_too_much_data=10000,
        )

    # ---- 映射 axes ----
    panel_axes = _map_panel_axes(axes)
    ax_main = panel_axes.get(0)
    ax_vol = panel_axes.get(1)
    ax_macd = panel_axes.get(2)
    ax_rsi = panel_axes.get(3)

    # ---- 三保险：对所有 axes 显式设置字体（对抗 mplfinance 内部覆盖）----
    _fp_obj = None
    if _font_name:
        _fp_obj = FontProperties(family=_font_name)
        for ax in axes:
            try:
                for label in ax.get_xticklabels() + ax.get_yticklabels():
                    label.set_fontproperties(_fp_obj)
                if ax.title:
                    ax.title.set_fontproperties(_fp_obj)
                # 遍历 text 对象
                for txt in ax.texts:
                    txt.set_fontproperties(_fp_obj)
            except Exception as e:
                logger.debug("[图表] ax 字体设置失败: %s", e)

    # ---- 各副图标题 ----
    def _safe_ylabel(ax, text_cn, text_en):
        if ax is None:
            return
        label_text = text_cn if use_cn else text_en
        if _fp_obj:
            ax.set_ylabel(label_text, fontsize=10, color="#374151", fontproperties=_fp_obj)
        else:
            ax.set_ylabel(label_text, fontsize=10, color="#374151")

    _safe_ylabel(ax_main, "价格", "Price")
    _safe_ylabel(ax_vol, "成交量", "Volume")
    _safe_ylabel(ax_macd, "MACD", "MACD")
    _safe_ylabel(ax_rsi, "RSI", "RSI")

    # ---- 主图图例 ----
    if ax_main is not None:
        legend_elements = [
            Line2D([0], [0], color="#f59e0b", lw=1.5, label="MA5"),
            Line2D([0], [0], color="#3b82f6", lw=1.5, label="MA20"),
            Line2D([0], [0], color="#8b5cf6", lw=1.5, label="MA60"),
            Line2D([0], [0], color="#dc2626", lw=1.2, linestyle="--",
                   label=f"{'止损' if use_cn else 'Stop'} {stop_loss:.2f}"),
            Line2D([0], [0], color="#059669", lw=1.2, linestyle="--",
                   label=f"{'止盈' if use_cn else 'TP'} {take_profit:.2f}"),
            Line2D([0], [0], marker="*", color="w", markerfacecolor="#fbbf24",
                   markersize=16, label=f"{'入场' if use_cn else 'Entry'} {cur_price:.2f}"),
        ]
        # 图例移到主图轴外上方（避免和 K 线/均线重合），一行 6 列
        # 标签去掉冗余前缀（原 "MA5 (MA5)" → "MA5"），字号 7.5 + 紧凑列距，确保一行不挤
        leg = ax_main.legend(
            handles=legend_elements,
            bbox_to_anchor=(0.5, 1.04),   # axes 坐标：主图轴顶上方 4%
            loc="lower center",
            ncol=6,                        # 一行排开
            fontsize=7.5,
            frameon=True,
            framealpha=0.95,
            borderpad=0.5,
            handletextpad=0.4,
            columnspacing=1.0,
        )
        # legend 文本字体
        if _fp_obj and leg:
            for txt in leg.get_texts():
                txt.set_fontproperties(_fp_obj)

        # 止损/止盈虚线末端标签
        # 注意：不传 fontweight="bold"，PingFang SC 无 bold 字重会触发 findfont 警告；
        #       同时 fontweight 与 fontproperties 同传会让 tight bbox 计算异常（画布爆炸）。
        #       若需粗体，构造 FontProperties(weight="bold") 单独传入。
        try:
            ax_main.text(idx[-1], stop_loss, f"{' SL' if not use_cn else ' 止损'} {stop_loss:.2f}",
                         color="#dc2626", fontsize=9, va="center", ha="left",
                         fontproperties=_fp_obj)
            ax_main.text(idx[-1], take_profit, f"{' TP' if not use_cn else ' 止盈'} {take_profit:.2f}",
                         color="#059669", fontsize=9, va="center", ha="left",
                         fontproperties=_fp_obj)
        except Exception as e:
            logger.debug("[图表] 止损/止盈标注失败: %s", e)

    # ---- 顶部标题 ----
    # 用 FontProperties(weight="bold") 单独承载粗体，避免 fontweight+fontproperties 同传
    # y=0.985 + fontsize=15：避免向下渲染时和副标题重合
    # 有股票名称时显示 "代码 名称"，无名称时只显示代码
    _fp_bold = FontProperties(family=_font_name, weight="bold") if _font_name else None
    title_symbol = f"{symbol} {stock_name}" if stock_name else symbol
    fig.text(
        0.5, 0.985, f"{title_symbol}  {title_text}",
        ha="center", va="top", fontsize=15, color=title_color,
        fontproperties=_fp_bold if _fp_bold else None,
    )

    # ---- 维度详情副标题 ----
    a_icon = "[√]" if a_pass else "[x]"
    b_icon = "[√]" if b_pass else "[x]"
    c_icon = "[√]" if c_pass else "[x]"
    b_key_signals = [s for s in b_signals if not s.startswith(("[√]", "[x]", "(i)", "(-)"))]
    b_summary = ", ".join(b_key_signals) if b_key_signals else ("无动量信号" if use_cn else "No signal")
    if use_cn:
        dim_summary = f"结构 {a_icon}    动量 {b_icon} ({b_summary})    赔率 {c_icon} ({ratio_str}:1)"
    else:
        dim_summary = f"A {a_icon}  B {b_icon} ({b_summary})  C {c_icon} ({ratio_str}:1)"
    fig.text(
        0.5, 0.955, dim_summary,
        ha="center", va="top", fontsize=10, color="#4b5563",
        fontproperties=_fp_obj,
    )

    # ---- 底部信息栏 ----
    mode_label = {"swing": "结构派" if use_cn else "Swing",
                  "atr": "波动率派" if use_cn else "ATR",
                  "fixed": "固定派" if use_cn else "Fixed"}.get(config.STOP_MODE, str(config.STOP_MODE))
    if use_cn:
        info_line1 = (
            f"当前价: {cur_price:.2f}  |  "
            f"止损位: {stop_loss:.2f} (亏 {c_info['loss_space']:.2f})  |  "
            f"止盈位: {take_profit:.2f} (赚 {c_info['profit_space']:.2f})  |  "
            f"盈亏比: {ratio_str}:1 (门槛 {config.MIN_RR_RATIO}:1)"
        )
        info_line2 = (
            f"止损模式: {mode_label}  |  建议开仓: {shares:,} 股  |  "
            f"总资金 {config.TOTAL_CAPITAL:,.0f} 元，单笔风险 {config.MAX_RISK_PER_TRADE*100:.1f}%  |  "
            f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
    else:
        info_line1 = (
            f"Price: {cur_price:.2f}  |  "
            f"Stop: {stop_loss:.2f} (-{c_info['loss_space']:.2f})  |  "
            f"TP: {take_profit:.2f} (+{c_info['profit_space']:.2f})  |  "
            f"RR: {ratio_str}:1 (need {config.MIN_RR_RATIO}:1)"
        )
        info_line2 = (
            f"Mode: {mode_label}  |  Shares: {shares:,}  |  "
            f"Capital: {config.TOTAL_CAPITAL:,.0f} (Risk {config.MAX_RISK_PER_TRADE*100:.1f}%)  |  "
            f"At: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
    info_text = f"{info_line1}\n{info_line2}"
    try:
        fig.text(
            0.5, 0.005, info_text, ha="center", va="bottom", fontsize=9.5,
            color="#374151", fontproperties=_fp_obj,
            bbox=dict(boxstyle="round,pad=0.5", facecolor="#f9fafb", edgecolor="#d1d5db", linewidth=0.8),
        )
    except Exception as e:
        logger.debug("[图表] 底部信息栏标注失败: %s", e)

    # ---- 强制渲染 ----
    try:
        fig.canvas.draw()
    except Exception as e:
        logger.warning("[图表] canvas.draw() 失败: %s（继续保存）", e)

    # ---- 输出路径：trading_lab/data/reports/ ----
    if out_dir is None:
        # chart.py 在 trading_lab/services/calc_indicators/ 下
        # __file__.resolve().parents[0] = calc_indicators/
        # __file__.resolve().parents[1] = services/
        # __file__.resolve().parents[2] = trading_lab/
        project_root = Path(__file__).resolve().parents[2]
        out_dir = project_root / "data" / "reports"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now().strftime("%Y%m%d")
    out_path = out_dir / f"report_{symbol}_{date_str}.png"

    # ---- 保存 ----
    # 关键：禁用 bbox_inches="tight"！
    # 原因：mplfinance figure + fontproperties + 某些 text 元素组合下，tight bbox 计算会
    #       得到天文数字的宽度（实测 154703 像素），导致 99.5% 画布空白、图表被挤到左侧。
    #       改为 subplots_adjust 显式留出顶部/底部文本空间，配合固定 pad_inches。
    try:
        try:
            # top=0.86：给 主标题(0.985)+副标题(0.955)+一行图例(轴外上方) 留 14% 空间
            fig.subplots_adjust(top=0.86, bottom=0.10, left=0.06, right=0.97)
        except Exception as e:
            logger.debug("[图表] subplots_adjust 失败（mplfinance 自管布局）: %s", e)
        fig.savefig(
            out_path,
            dpi=150,
            pad_inches=0.3,
            facecolor="white",
            edgecolor="white",
        )
        # 二次校验：文件大小不能是 0 或极小
        size_kb = out_path.stat().st_size / 1024 if out_path.exists() else 0
        logger.info("[图表] 报告已保存: %s (%.1f KB)", out_path, size_kb)
        if size_kb < 10:
            logger.warning("[图表] 文件太小 (%.1f KB)，可能渲染异常，请确认", size_kb)
    finally:
        plt.close(fig)

    if auto_open:
        import webbrowser
        webbrowser.open(f"file://{out_path.resolve()}")
        logger.info("[图表] 已在默认浏览器打开")

    return out_path


# ==================== 对比图（上下双图叠加） ====================

def generate_compare_chart(
    result1: dict,
    result2: dict,
    config: TradingConfig,
    verdict: dict,
    out_dir: Path | str | None = None,
    auto_open: bool = False,
) -> Path:
    """生成上下双图对比报告。

    顶部为对比结论文字区，中部和底部分别为两只股票的完整图表。
    复用 generate_chart_report 生成单图后垂直拼接。

    Args:
        result1/result2: _evaluate_stock 返回的结果字典（含 symbol/name/df/各维度结果）
        config: TradingConfig
        verdict: 对比结论 {winner, loser, reasons}
        out_dir: 输出目录，默认 trading_lab/data/reports/
        auto_open: 生成后是否自动用浏览器打开
    """
    import matplotlib.image as mpimg

    _setup_font_and_rc(verbose=True)
    use_cn = _font_name is not None
    _fp_obj = FontProperties(family=_font_name) if _font_name else None
    _fp_bold = FontProperties(family=_font_name, weight="bold") if _font_name else None

    # ---- 1. 生成两张单图到临时目录 ----
    import tempfile
    tmp_dir = Path(tempfile.mkdtemp(prefix="calc_ind_compare_"))
    try:
        path1 = generate_chart_report(
            df=result1["df"], config=config, symbol=result1["symbol"],
            a_result=(result1["a_pass"], result1["a_msg"]),
            b_result=(result1["b_pass"], result1["b_signals"]),
            c_result=(result1["c_pass"], result1["c_info"]),
            pos=result1["pos"], out_dir=tmp_dir,
            auto_open=False, stock_name=result1.get("name", ""),
        )
        path2 = generate_chart_report(
            df=result2["df"], config=config, symbol=result2["symbol"],
            a_result=(result2["a_pass"], result2["a_msg"]),
            b_result=(result2["b_pass"], result2["b_signals"]),
            c_result=(result2["c_pass"], result2["c_info"]),
            pos=result2["pos"], out_dir=tmp_dir,
            auto_open=False, stock_name=result2.get("name", ""),
        )

        # ---- 2. 读取图片 ----
        img1 = mpimg.imread(str(path1))
        img2 = mpimg.imread(str(path2))

        # ---- 3. 创建拼接 figure ----
        fig = plt.figure(figsize=(16, 28), dpi=100)
        fig.patch.set_facecolor("white")

        # 顶部结论标题
        winner = verdict["winner"]
        loser = verdict["loser"]
        w_label = winner["symbol"] + (f" {winner.get('name', '')}" if winner.get("name") else "")
        l_label = loser["symbol"] + (f" {loser.get('name', '')}" if loser.get("name") else "")

        title_text = (f"对比结论：{w_label}  优于  {l_label}" if use_cn
                      else f"Verdict: {w_label}  >  {l_label}")
        fig.text(
            0.5, 0.978, title_text,
            ha="center", va="top", fontsize=16, color="#059669",
            fontproperties=_fp_bold if _fp_bold else None,
        )

        # 结论原因列表
        reasons_text = "\n".join(verdict["reasons"])
        fig.text(
            0.5, 0.948, reasons_text,
            ha="center", va="top", fontsize=10, color="#374151",
            fontproperties=_fp_obj,
        )

        # 中部图1（上方）
        ax1 = fig.add_axes([0.02, 0.49, 0.96, 0.44])
        ax1.imshow(img1)
        ax1.axis("off")

        # 底部图2（下方）
        ax2 = fig.add_axes([0.02, 0.02, 0.96, 0.44])
        ax2.imshow(img2)
        ax2.axis("off")

        # ---- 4. 保存 ----
        if out_dir is None:
            project_root = Path(__file__).resolve().parents[2]
            out_dir = project_root / "data" / "reports"
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        date_str = datetime.now().strftime("%Y%m%d")
        out_path = out_dir / f"compare_{result1['symbol']}_{result2['symbol']}_{date_str}.png"

        fig.savefig(out_path, dpi=100, facecolor="white", edgecolor="white", pad_inches=0.2)
        plt.close(fig)

        size_kb = out_path.stat().st_size / 1024 if out_path.exists() else 0
        logger.info("[图表] 对比图已保存: %s (%.1f KB)", out_path, size_kb)

    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if auto_open:
        import webbrowser
        webbrowser.open(f"file://{out_path.resolve()}")
        logger.info("[图表] 对比图已在默认浏览器打开")

    return out_path
