"""Plotly HTML 渲染层。

依据 query.query_chart() 的结果渲染交互式折线图，覆盖需求 7.x 的可视化：
  - 单点标记 / 降采样标注 / 缺口虚线+三角 / 双 Y 轴对比 / 修订(初值空心+⚠) /
    无波动标注 / 频率变更竖虚线 / 色盲友好(Viridis) / 图例含单位与数据范围。
plotly 懒加载：仅在使用 html 输出时才需要安装。
"""
from __future__ import annotations

from typing import Any

from core.logger import get_logger

log = get_logger("macro.charts.render")


def _viridis_colors(n: int) -> list[str]:
    import plotly.colors as pc

    if n <= 0:
        return []
    return pc.sample_colorscale("Viridis", [i / max(n - 1, 1) for i in range(n)])


def _assign_yaxes(series_list: list[dict]) -> list[str]:
    """按单位分配 Y 轴：同单位共享一轴，最多 3 个轴（y1/y2/y3）。

    第一个指标走左轴(y1)；其后出现的新单位依次走 y2、y3；
    单位相同的指标复用已分配的轴，避免量纲混叠。
    例如 DGS10(%)、GOLD(元/克)、DTWEXBGS(指数) → y1/y2/y3 三轴；
    而 CPI(%)、PPI(%) → 均走 y1，单轴。
    """
    unit_to_axis: dict[str, str] = {}
    axes_order = ["y1", "y2", "y3"]
    result: list[str] = []
    for s in series_list:
        u = s.get("unit", "") or ""
        if u not in unit_to_axis:
            unit_to_axis[u] = axes_order[min(len(unit_to_axis), 2)]
        result.append(unit_to_axis[u])
    return result


def _render_single_figure(result: dict):
    """将单个 query_chart 结果（须 ok=True）构建为 Plotly Figure。

    供 render_html()（单图自包含 HTML）与 render_html_multi()（多图合并 HTML）复用，
    保证单图与多图两种模式下的多 Y 轴/标注/缺口逻辑完全一致。
    """
    import plotly.graph_objects as go

    series_list = result["series"]
    compare = result.get("compare", False)
    colors = _viridis_colors(max(len(series_list), 1))
    yaxes = _assign_yaxes(series_list)
    # 轴元信息：axis_id -> {unit, color}（取该轴首个 series 的颜色，用于轴标题/刻度配色）
    axis_meta: dict[str, dict] = {}
    for i, (s, ax) in enumerate(zip(series_list, yaxes)):
        if ax not in axis_meta:
            axis_meta[ax] = {"unit": s.get("unit", ""), "color": colors[i]}
    fig = go.Figure()

    for i, s in enumerate(series_list):
        color = colors[i]
        yaxis = yaxes[i]
        dates = s["dates"]
        values = s["values"]                # 原始精度（Tooltip）
        values_disp = s["values_display"]   # 显示精度
        unit = s.get("unit", "")
        name = f"{s['name']}({s['indicator']})"

        # 自定义 hover 显示原始精度 + 更新时间 + 修订状态
        hover_text = [
            f"{d}<br>{name}: {v} {unit}<br>更新时间: {ft}"
            + (f"<br>状态: 初值(preliminary)" if rs == "preliminary" else "")
            + (f"<br>状态: 终值(revised)" if rs == "revised" else "")
            for d, v, ft, rs in zip(
                dates, values, s.get("fetch_times", []),
                s.get("revision_status", [""] * len(dates)),
            )
        ]

        mode = "markers" if s.get("single_point") else "lines+markers"
        fig.add_trace(go.Scatter(
            x=dates, y=values_disp, mode=mode, name=f"{name} [{unit}]",
            line=dict(color=color, dash="solid"),
            marker=dict(color=color, size=7),
            text=hover_text, hoverinfo="text",
            yaxis=yaxis,
        ))

        # 7.5 初值：空心圆标记（虚线+空心）
        prelim_idx = [k for k, rs in enumerate(s.get("revision_status", []))
                      if rs == "preliminary"]
        if prelim_idx:
            fig.add_trace(go.Scatter(
                x=[dates[k] for k in prelim_idx],
                y=[values_disp[k] for k in prelim_idx],
                mode="markers", name=f"{name} [初值]",
                marker=dict(color=color, size=10, symbol="circle-open",
                            line=dict(width=2)),
                showlegend=False, yaxis=yaxis,
            ))

        # 7.2 缺口：虚线连接前后有效点 + 三角形标记
        for g in s.get("gaps", []):
            gx = [g["start"], g["end"]]
            # 取缺口两端对应的值
            vmap = dict(zip(dates, values_disp))
            gy = [vmap.get(g["start"]), vmap.get(g["end"])]
            fig.add_trace(go.Scatter(
                x=gx, y=gy, mode="lines+markers",
                line=dict(color=color, dash="dash", width=1.5),
                marker=dict(color=color, size=11, symbol="triangle-up"),
                name=f"{name} [缺失区间]", showlegend=False,
                hovertext=[f"该区间数据缺失<br>{g['start']} ~ {g['end']}"] * 2,
                hoverinfo="text", yaxis=yaxis,
            ))

    # ---------- 布局：多 Y 轴（按单位分配，最多 3 轴）/ 标题 / 图例 ----------
    y1 = axis_meta.get("y1", {})
    layout = dict(
        title=_build_title(result),
        xaxis=dict(title="日期", gridcolor="rgba(200,200,200,0.3)"),
        yaxis=dict(title=y1.get("unit", ""), color=y1.get("color"),
                   gridcolor="rgba(200,200,200,0.3)"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="closest",
        template="plotly_white",
        margin=dict(l=60, r=60, t=110, b=60),
    )
    if "y2" in axis_meta:
        y2 = axis_meta["y2"]
        layout["yaxis2"] = dict(title=y2["unit"], color=y2["color"],
                                overlaying="y", side="right",
                                gridcolor="rgba(200,200,200,0.15)")
    if "y3" in axis_meta:
        # 第三轴：Plotly 限制 position∈[0,1]，无法超出 domain 右边缘。
        # 故 yaxis3 锚定在绘图区域内 93% 位置（刻度压右侧 7%），yaxis2 仍在右边缘 margin。
        # 隐藏轴线/网格避免干扰主图，仅保留刻度与标题（颜色与曲线一致以便区分）。
        y3 = axis_meta["y3"]
        layout["yaxis3"] = dict(title=y3["unit"], color=y3["color"],
                                overlaying="y", side="right",
                                anchor="free", position=0.93,
                                showline=False, showgrid=False)
        layout["margin"] = dict(l=60, r=90, t=110, b=60)
    fig.update_layout(**layout)

    # ---------- 标注 ----------
    _add_annotations(fig, result)

    # 7.4 频率变更：竖虚线
    for s in series_list:
        for fc in s.get("frequency_changes", []):
            fig.add_shape(type="line", x0=fc["date"], x1=fc["date"],
                          y0=0, y1=1, yref="paper",
                          line=dict(color="red", dash="dash", width=1))
            fig.add_annotation(x=fc["date"], y=1, yref="paper",
                               text=f"频率变更:{fc['from']}→{fc['to']}",
                               showarrow=False, yshift=10, font=dict(size=10, color="red"))

    # 7.5 修订标识
    if any(s.get("revised") for s in series_list):
        fig.add_annotation(x=1, y=1, xref="paper", yref="paper",
                           text="⚠ 数据已修订", showarrow=False,
                           xanchor="right", yanchor="top",
                           font=dict(size=12, color="orange"),
                           bgcolor="rgba(255,165,0,0.1)")

    return fig


def render_html(result: dict) -> str:
    """将单个 query_chart 结果渲染为自包含 HTML 字符串（内联 plotly.js，断网可用）。"""
    if not result.get("ok"):
        return _error_html(result.get("message", "查询失败"), result.get("warnings", []))
    fig = _render_single_figure(result)
    return fig.to_html(full_html=True, include_plotlyjs=True)


def render_html_multi(results: list[dict]) -> str:
    """将多个 query_chart 结果合并到同一个 HTML 页面（纵向堆叠，plotly.js 只引一次）。

    每组结果渲染为一张独立图表，各自保留完整的多 Y 轴/标注/缺口/修订逻辑；
    ok=False 的结果渲染为错误提示块，不影响其余图表展示。
    适用于把多个指标组合（用 ``;`` 分隔传入）汇总为一个 dashboard 页面。
    """
    import plotly.offline as pyo

    sections: list[str] = []
    n_ok = 0
    for i, result in enumerate(results, 1):
        if result.get("ok"):
            title = result.get("title") or f"图表 {i}"
            fig = _render_single_figure(result)
            # full_html=False 仅返回 <div>+<script>，plotly.js 由页面 <head> 统一提供
            div = fig.to_html(full_html=False, include_plotlyjs=False)
            sections.append(f'<section><h2>{title}</h2>{div}</section>')
            n_ok += 1
        else:
            msg = result.get("message", "查询失败")
            warnings = result.get("warnings", [])
            warn_html = ("<ul>" + "".join(f"<li>{w}</li>" for w in warnings) + "</ul>") if warnings else ""
            sections.append(
                f'<section><h2>图表 {i}</h2>'
                f'<div class="err"><h3 style="color:#c0392b">{msg}</h3>{warn_html}</div></section>'
            )

    plotly_js = pyo.get_plotlyjs()
    page_title = f"宏观数据图表（{n_ok}/{len(results)} 组）"
    return (
        "<!DOCTYPE html>\n<html lang=\"zh\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        f"<title>{page_title}</title>\n"
        f"<script type=\"text/javascript\">{plotly_js}</script>\n"
        "<style>\n"
        "  body { font-family: -apple-system, \"PingFang SC\", \"Microsoft YaHei\", sans-serif; "
        "margin: 0; padding: 20px; background: #fafafa; color: #333; }\n"
        "  h1 { font-size: 20px; margin: 0 0 4px; }\n"
        "  .meta { font-size: 12px; color: #888; margin-bottom: 16px; }\n"
        "  section { background: #fff; border: 1px solid #e8e8e8; border-radius: 8px; "
        "margin: 16px 0; padding: 12px 16px; }\n"
        "  section h2 { font-size: 15px; color: #555; margin: 4px 0 8px; font-weight: 600; }\n"
        "  .err { padding: 8px 0; }\n"
        "  .err ul { color: #888; }\n"
        "</style>\n"
        "</head>\n<body>\n"
        f"<h1>{page_title}</h1>\n"
        f"<div class=\"meta\">共 {len(results)} 组指标组合，成功 {n_ok} 组</div>\n"
        f"{''.join(sections)}\n"
        "</body>\n</html>"
    )


def _build_title(result: dict) -> dict:
    s0 = result["series"][0]
    parts = [result.get("title", "")]
    meta = (f"{s0.get('name','')} | 单位:{s0.get('unit','')} | "
            f"{s0.get('date_range',{}).get('start','')}~{s0.get('date_range',{}).get('end','')} | "
            f"{s0.get('count',0)} 条")
    return dict(text=f"<b>{parts[0]}</b><br><span style='font-size:11px;color:#666'>{meta}</span>",
                x=0.5, xanchor="center")


def _add_annotations(fig, result: dict) -> None:
    s0 = result["series"][0]
    notes: list[tuple[str, str]] = []  # (text, position)
    if s0.get("single_point"):
        notes.append(("当前仅1期数据，趋势尚不可见", "top"))
    if s0.get("no_fluctuation"):
        notes.append(("数据无波动，请核实源数据", "top"))
    if s0.get("downsampled"):
        notes.append((f"已降采样显示，原始数据共 {s0.get('original_count')} 条", "bottom"))
    for msg, pos in notes:
        if pos == "bottom":
            fig.add_annotation(text=msg, x=0, y=0, xref="paper", yref="paper",
                               xanchor="left", yanchor="bottom",
                               showarrow=False, font=dict(size=10, color="#888"))
        else:
            fig.add_annotation(text=msg, x=0.5, y=1, xref="paper", yref="paper",
                               showarrow=False, yshift=-30,
                               font=dict(size=11, color="#c0392b"))


def _error_html(message: str, warnings: list[str]) -> str:
    body = f"<h3 style='color:#c0392b'>{message}</h3>"
    if warnings:
        body += "<ul>" + "".join(f"<li>{w}</li>" for w in warnings) + "</ul>"
    return f"<html><body style='font-family:sans-serif;padding:24px'>{body}</body></html>"
