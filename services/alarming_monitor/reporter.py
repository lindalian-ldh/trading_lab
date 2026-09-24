"""输出层：控制台轻量摘要 + CSV 行格式化。

控制台摘要仿 sell_monitor 的状态卡片风格：红绿灯 ASCII + red_count + 仓位建议。
新增 ADX 市场状态行：显示 ADX 值 + 趋势/震荡/中性 + 动态阈值。
CSV 行由 storage.log_alarm_signal 写入按月分文件。
"""

from __future__ import annotations

import math


# CSV 表头（与 storage 保持一致）
CSV_FIELDS = [
    'run_time', 'ref_date', 'risk_level', 'red_count',
    'effective_red_count', 'effective_threshold', 'market_state', 'adx_value',
    's1_turnover_ratio', 's1_red',
    's2_divergence', 's2_red',
    's3_ad_ratio', 's3_red',
    's4_dividend_spread', 's4_red',
    's5_growth_ret', 's5_red',
    's6_net_leverage', 's6_red',
    's7_k_value', 's7_red',
    'position_advice', 'advice_desc', 'data_sufficient',
]

# 信号 name → CSV 值/红灯字段映射
_SIGNAL_MAP = {
    'turnover_ratio_high': ('s1_turnover_ratio', 's1_red'),
    'volume_price_divergence': ('s2_divergence', 's2_red'),
    'ad_ratio_bearish': ('s3_ad_ratio', 's3_red'),
    'dividend_strength': ('s4_dividend_spread', 's4_red'),
    'growth_breakdown': ('s5_growth_ret', 's5_red'),
    'margin_leverage':    ('s6_net_leverage',  's6_red'),
    'kdj_death_cross':    ('s7_k_value',        's7_red'),
}


def format_csv_row(result: dict) -> dict:
    """把聚合结果展平为 CSV 行 dict（与 CSV_FIELDS 对齐）。"""
    adx = result.get('adx', {}) or {}
    adx_val = adx.get('value')
    row = {
        'run_time': result.get('run_time', ''),
        'ref_date': result.get('ref_date', ''),
        'risk_level': result.get('risk_level', ''),
        'red_count': result.get('red_count', 0),
        'effective_red_count': result.get('effective_red_count', 0),
        'effective_threshold': result.get('effective_threshold', ''),
        'market_state': result.get('market_state', ''),
        'adx_value': ('' if (adx_val is None or (isinstance(adx_val, float) and math.isnan(adx_val)))
                      else round(adx_val, 2)),
        'position_advice': result.get('position_advice', ''),
        'advice_desc': result.get('advice_desc', ''),
        'data_sufficient': result.get('data_sufficient', False),
    }
    for sig in result.get('signals', []):
        name = sig.get('name')
        if name not in _SIGNAL_MAP:
            continue
        val_field, red_field = _SIGNAL_MAP[name]
        v = sig.get('value')
        # 防御 inf/NaN：None→空，NaN→空，inf→"∞"，其它 round(v,4)
        if v is None:
            row[val_field] = ''
        elif isinstance(v, float) and math.isnan(v):
            row[val_field] = ''
        elif isinstance(v, float) and math.isinf(v):
            row[val_field] = '∞'
        else:
            try:
                row[val_field] = round(float(v), 4)
            except (TypeError, ValueError):
                row[val_field] = ''
        row[red_field] = bool(sig.get('red', False))
    # 补齐缺失字段为空，保证写表头一致
    return {k: row.get(k, '') for k in CSV_FIELDS}


def print_summary(result: dict) -> None:
    """控制台轻量摘要：红绿灯 + red_count + ADX 市场状态 + 仓位建议。"""
    sigs = result.get('signals', [])
    red_count = result.get('red_count', 0)
    effective_red = result.get('effective_red_count', red_count)
    effective_threshold = result.get('effective_threshold', 4)
    risk_level = result.get('risk_level', 'normal')
    total = len(sigs)
    ref_date = result.get('ref_date', '')
    profile = result.get('profile', '')
    advice = result.get('position_advice', '')
    advice_desc = result.get('advice_desc', '')
    sufficient = result.get('data_sufficient', False)
    market_state = result.get('market_state', 'neutral')
    threshold_reason = result.get('threshold_reason', '')
    adx = result.get('adx', {}) or {}
    adx_detail = adx.get('detail', '')

    bar = '=' * 64
    print(bar)
    print('🔍 市场大跌预警  参考日: %s  配置档: %s' % (ref_date, profile))
    # 风险等级行：显示有效红灯数（震荡市加权后）和动态阈值
    if effective_red != red_count:
        red_display = f'{red_count}红→加权{effective_red}红'
    else:
        red_display = f'{red_count}红'
    print('   风险等级: %s  红灯: %s/%d（阈值≥%d）'
          % (_risk_label(risk_level), red_display, total, effective_threshold))
    # ADX 市场状态行
    if adx_detail:
        print('   📊 ADX: %s' % adx_detail)
    if threshold_reason:
        print('   ⚙️  %s' % threshold_reason)
    print(bar)

    for s in sigs:
        is_red = s.get('red', False)
        s_sufficient = s.get('data_sufficient', False)
        light = '🔴 红' if is_red else ('⚪ 灰' if not s_sufficient else '🟢 绿')
        label = s.get('label', '')
        detail = s.get('detail', '')
        # 震荡市 S4/S5/S6 加权标记（与 config.RANGE_WEIGHTED_SIGNALS 一致）
        weighted_mark = ''
        if (market_state == 'range' and is_red
                and s.get('name') in ('dividend_strength', 'growth_breakdown', 'margin_leverage')):
            weighted_mark = ' [震荡×1.5]'
        print('  %s  %s%s' % (light, label, weighted_mark))
        print('        %s' % detail)

    if not sufficient:
        print('  ⚠️  部分信号数据不足，结果仅供参考（见灰灯）')

    print('-' * 64)
    print('💡 仓位建议: %s  (%s)' % (advice, advice_desc))
    print(bar)


def _risk_label(level: str) -> str:
    return {
        'high': '🔴 高风险',
        'warn': '🟡 警示',
        'normal': '🟢 正常',
    }.get(level, level)


# ====================================================================
# Markdown 全景报告（严格按用户模板）
# ====================================================================

def _risk_level_cn(level: str) -> str:
    """risk_level → 中文风险等级（用于 Markdown 标题）。"""
    return {
        'high': '**高风险**',
        'warn': '**中风险**',
        'normal': '**低风险**',
    }.get(level, level)


def _market_state_cn(state: str, adx_val: float) -> str:
    """ADX 市场状态 → 中文描述（过渡区/强趋势市/弱趋势震荡市）。"""
    import math
    if adx_val is None or (isinstance(adx_val, float) and math.isnan(adx_val)):
        return 'ADX 未取'
    if state == 'trend':
        return f'强趋势市（ADX={adx_val:.1f}）'
    if state == 'range':
        return f'弱趋势/震荡市（ADX={adx_val:.1f}）'
    return f'过渡区（ADX={adx_val:.1f}）'


def generate_markdown_report(alarm_result: dict,
                             rotation_result: dict,
                             linkban_result: Optional[dict] = None) -> str:
    """生成用户指定格式的 Markdown 全景报告。

    模板（严格对齐）：
    ### 📊 YYYY-MM-DD 市场全景分析报告
    #### 🚨 综合警报等级：**中风险**（ADX=24.5，过渡区，红灯计数: 3/5）
    - **亮灯信号**：S2（放量不涨）, S3（涨跌比恶化）, S4（高股息逆势）

    #### 🔄 板块轮动全景表
    | 板块 | 收盘价 | ADX | RS-Ratio | RS-Momentum | 象限标签 | 5维评分 | 有仓位 | 无仓位 | 超卖 | 反弹 | 趋势健康 | RS健康 | 拐点 | 领先 | 确认 | 持有 |
    |------|--------|-----|----------|-------------|----------|---------|----------|----------|------|------|----------|--------|------|------|------|------|
    | 银行ETF | 1.052 | 32.1 | 1.12 | +2.3% | 领涨主线 | 4.2 | 持有 | 可建仓 | 否 | 无 | 4.0 | 3.0 | — | 0 | 80 | 75持有 |
    ...

    Args:
        alarm_result: monitor.evaluate_signals 输出
        rotation_result: analysis.run_rotation_analysis 输出

    Returns:
        str: 完整 Markdown 文本
    """
    # ---- 第一部分：综合警报 ----
    ref_date = alarm_result.get('ref_date', '')
    risk = alarm_result.get('risk_level', 'normal')
    red_count = alarm_result.get('red_count', 0)
    total = len(alarm_result.get('signals', []))
    adx = alarm_result.get('adx', {}) or {}
    adx_val = adx.get('value')
    market_state = alarm_result.get('market_state', 'neutral')
    state_desc = _market_state_cn(market_state, adx_val)

    lines = []
    lines.append(f'### 📊 {ref_date} 市场全景分析报告')
    lines.append('')
    lines.append(f'#### 🚨 综合警报等级：{_risk_level_cn(risk)}'
                 f'（{state_desc}，红灯计数: {red_count}/{total}）')

    # 亮灯信号列表（带触发原因）
    red_signals = []
    for s in alarm_result.get('signals', []):
        if s.get('red'):
            # label 形如 "S1 成交额/总市值" → "S1（成交额/总市值）"
            label = s.get('label', '')
            parts = label.split(' ', 1)
            if len(parts) == 2:
                sig_id, sig_name = parts[0], parts[1]
            else:
                sig_id, sig_name = label, ''
            # red_reason 是各信号在 red=True 时给出的简短子分类
            # （如 S6 的"过热"/"去杠杆"、S5 的"近20日-5%破位；跌破20日均线"）
            reason = str(s.get('red_reason', '') or '').strip()
            if reason:
                red_signals.append(f'{sig_id}（{sig_name}·{reason}）' if sig_name else f'{sig_id}（{reason}）')
            else:
                red_signals.append(f'{sig_id}（{sig_name}）' if sig_name else sig_id)
    if red_signals:
        lines.append(f'- **亮灯信号**：{", ".join(red_signals)}')
    else:
        lines.append('- **亮灯信号**：无（绿灯安全）')

    # S7（大盘KDJ高位死叉）恒显段：无论红/绿/灰都输出 5 段结构化信息
    # 优先级：1.核心数值(K/D/J) > 2.趋势形态（四态）> 3.周期共振+底背离 > 4.操作指引 > 5.历史分位
    s7 = None
    for s in alarm_result.get('signals', []):
        if s.get('name') == 'kdj_death_cross':
            s7 = s
            break
    if s7 is not None:
        s7_red = bool(s7.get('red', False))
        s7_sufficient = bool(s7.get('data_sufficient', True))
        kdj_detail = s7.get('kdj_detail') if isinstance(s7.get('kdj_detail'), dict) else {}
        # —— 灰灯：数据不足，降级单行
        if not s7_sufficient:
            s7_detail = str(s7.get('detail', '') or '')
            lines.append(f'- **大盘KDJ状态**：[⚪ 灰灯] S7（大盘KDJ高位死叉）｜{s7_detail}')
            lines.append('')
        else:
            # 颜色标签（按红灯触发逻辑）
            if s7_red:
                tag = '🔴 红灯'
            else:
                tag = '🟢 绿灯'
            # 拉取扩展段
            period_states = kdj_detail.get('period_states') if isinstance(kdj_detail.get('period_states'), dict) else {}
            resonance = str(kdj_detail.get('resonance', '—'))
            directions = kdj_detail.get('directions') if isinstance(kdj_detail.get('directions'), dict) else {}
            bull_div = bool(kdj_detail.get('bull_divergence', False))
            weekly_pctl = kdj_detail.get('weekly_k_percentile')
            quality = str(kdj_detail.get('quality', '—'))
            advice_lines = kdj_detail.get('advice_lines') if isinstance(kdj_detail.get('advice_lines'), list) else []
            one_liner = str(kdj_detail.get('one_liner_summary', ''))
            red_trigger = kdj_detail.get('red_trigger') if isinstance(kdj_detail.get('red_trigger'), dict) else {}

            def _dir_emoji(d: str) -> str:
                return '⬆️' if d == 'up' else ('⬇️' if d == 'down' else '↔️')

            lines.append('- **大盘KDJ状态**')
            # 1. 核心数值区：周线 / 月线分开两行，J 值单独标红（Markdown 中用 **加粗 + 中文标注极度超卖/超买**）
            for period in ('周线', '月线'):
                s = period_states.get(period)
                if not isinstance(s, dict):
                    lines.append(f'  - {period}（{"短期" if period=="周线" else "中期"}）：数据不足')
                    continue
                K = s.get('K')
                D = s.get('D')
                J = s.get('J')
                j_tag = str(s.get('j_tag', '') or '')
                # J 单独强调，负值特别标"极度超卖"
                if J is None:
                    j_str = 'J=—'
                else:
                    try:
                        jv = float(J)
                        if jv < 0 or j_tag == '极度超卖':
                            # Markdown 加粗表示视觉突出，终端中也能被肉眼快速识别
                            j_str = f'**J={jv:.1f}（极度超卖）**'
                        elif jv > 100 or j_tag == '极度超买':
                            j_str = f'**J={jv:.1f}（极度超买）**'
                        else:
                            j_str = f'J={jv:.1f}'
                            if j_tag and j_tag not in ('中性偏弱', '中性偏强'):
                                j_str += f'（{j_tag}）'
                    except Exception:
                        j_str = f'J={J}'
                tag_phase = '（短期）' if period == '周线' else '（中期）'
                lines.append(
                    f'  - {period}{tag_phase}：K={float(K):.1f} ｜ D={float(D):.1f} ｜ {j_str}'
                    + (f'（{j_tag}）' if j_tag and j_tag in ('中性偏弱', '中性偏强') and J is not None and 0 <= float(J) <= 100 else '')
                )
            # 2. 趋势形态区（四态 + 金叉/死叉确认）
            #    结构："周线 死叉 ✅ / 金叉 ❌ ｜ 月线 死叉 ✅ / 金叉 ❌ ｜ 短中期{resonance}"
            cross_tokens = []
            for period in ('周线', '月线'):
                s = period_states.get(period)
                if not isinstance(s, dict):
                    continue
                cn = str(s.get('cross_state_cn', ''))
                st4_cn = str(s.get('state4_cn', ''))
                st4_em = str(s.get('state4_emoji', ''))
                # 四态区：高位死叉🔴 / 低位死叉🟡 / 高位金叉🟠 / 低位金叉🟢 / 中位
                cross_tokens.append(f'{period}{cn}（{st4_cn}{st4_em}）')
            lines.append(f'  - **趋势形态**：{" ｜ ".join(cross_tokens)}')

            # 3. 周期共振 + 底背离区
            dir_tokens = []
            for period in ('周线', '月线'):
                d = directions.get(period)
                if d:
                    dir_tokens.append(f'{period}{_dir_emoji(d)}')
            resonance_line = '  - **周期共振**：' + ' ｜ '.join(dir_tokens) + f'｜综合：{resonance}'
            lines.append(resonance_line)
            div_line = f'  - **底背离**：{"✅ 存在" if bull_div else "❌ 未发现"}'
            lines.append(div_line)

            # 4. 操作指引区（定性 + 操作建议列表 + 一句话总结）
            #    绿灯时的补充状态说明（用于状态灯后缀一行）
            wk = period_states.get('周线') if isinstance(period_states.get('周线'), dict) else {}
            overbought_v = float(kdj_detail.get('overbought', 80) or 80)
            wk_k_v = None
            try:
                wk_k_v = float(wk.get('K')) if wk.get('K') is not None else None
            except Exception:
                wk_k_v = None
            # 状态灯行（综合：tag + 绿灯/红灯的"为什么"解释）
            if s7_red:
                reason = str(s7.get('red_reason', '') or '')
                tag_reason = '（高位死叉触发风险警报）' + (f'·{reason}' if reason else '')
            else:
                if wk_k_v is not None and wk_k_v < overbought_v / 2:  # <40 弱势
                    tag_reason = f'（因K<{int(overbought_v)}，无高位泡沫风险，但处于弱势区间）'
                elif wk_k_v is not None and wk_k_v < overbought_v:
                    tag_reason = f'（因K<{int(overbought_v)}，无高位泡沫风险）'
                else:
                    tag_reason = f'（K≥{int(overbought_v)} 但未发生死叉或前一期K未超阈值，暂不触发红灯）'
            lines.append(f'  - **状态灯**：[{tag}] S7 {tag_reason}')

            lines.append(f'  - **定性**：{quality}')
            lines.append(f'  - **操作建议**：')
            if advice_lines:
                for adv in advice_lines:
                    lines.append(f'    · {adv}')
            else:
                lines.append('    · 随 S1-S6 组合建议')
            lines.append(f'  - **一句话总结**：{one_liner}')

            # 5. 历史分位数（可选）—— 周 K 近 1 年底部百分位
            if weekly_pctl is not None:
                try:
                    pctl_v = float(weekly_pctl)
                    if pctl_v <= 10:
                        tier = '极度底部区'
                    elif pctl_v <= 25:
                        tier = '底部区间'
                    elif pctl_v >= 90:
                        tier = '极度顶部区'
                    elif pctl_v >= 75:
                        tier = '顶部区间'
                    else:
                        tier = '历史中位区'
                    lines.append(f'  - **近1年周K分位**：{pctl_v:.1f}%（{tier}）')
                except Exception:
                    pass

            # 红逻辑触发行（单独一行，精准回应真实触发条件：近N周期高位死叉，K前值>80）
            #   Bug3 修复：不再写"周K>80？"这种让用户误解为"判定当前K值"的问句；
            #   同时在未触发时追加 untriggered_reason，彻底避免"15.9 > 80 打勾"这种数学级矛盾。
            if red_trigger:
                th = str(red_trigger.get('threshold_used', '近3周期高位死叉（死叉发生时 K前值>80）'))
                triggered = bool(red_trigger.get('triggered', False))
                cur = red_trigger.get('current_week_k')
                hits = red_trigger.get('trigger_reason_hits') or []
                if cur is None:
                    cur_desc = '—'
                else:
                    cur_desc = f'{cur}'
                trigger_line = f'  - **红逻辑触发**：条件「{th}」 → {"✅ 已触发" if triggered else "❌ 未触发"}（当前周线K={cur_desc}）'
                if hits:
                    # 显示死叉发生时的真实 K 值（k_prev → k_cur），以及距今几周期 bars_ago
                    hit_details = []
                    # 注意：命中详情在 kdj_detail.branch_hits
                    bh = kdj_detail.get('branch_hits') if isinstance(kdj_detail.get('branch_hits'), dict) else {}
                    for hp in hits:
                        h = bh.get(hp) if isinstance(bh.get(hp), dict) else {}
                        k_p = h.get('k_prev')
                        k_c = h.get('k_cur')
                        bars = h.get('bars_ago')
                        if k_p is not None and k_c is not None:
                            s = f'{hp}K {float(k_p):.1f}→{float(k_c):.1f}'
                            if bars is not None:
                                s += f'（{int(bars)} 周期前）'
                            hit_details.append(s)
                        else:
                            hit_details.append(hp)
                    trigger_line += f' → 命中：{"、".join(hit_details)}'
                else:
                    # 未命中 → 给 untriggered_reason（若 indicator 已准备好）
                    untr = str(red_trigger.get('untriggered_reason', '') or '')
                    if untr:
                        trigger_line += f' → 原因：{untr}'
                lines.append(trigger_line)

            lines.append('')

    # 仓位建议（附加，用户模板未显式但信息有价值）
    advice = alarm_result.get('position_advice', '')
    advice_desc = alarm_result.get('advice_desc', '')
    if advice:
        lines.append(f'- **仓位建议**：{advice}（{advice_desc}）')
        lines.append('')

    # ---- 第二部分：板块轮动全景表 ----
    from analysis import build_rotation_table_rows, build_rrg_summary

    lines.append('#### 🔄 板块轮动全景表')
    lines.append('')
    # RRG 一句话摘要
    lines.append(f'> {build_rrg_summary(rotation_result)}')
    lines.append('')
    # 表头（严格按用户模板列顺序，不加入排名Δ）
    lines.append('| 板块 | 收盘价 | ADX | RS-Ratio | RS-Momentum | 象限标签 | 5维评分 | 有仓位 | 无仓位 | 超卖 | 反弹 | 趋势健康 | RS健康 | 拐点 | 领先 | 确认 | 持有 |')
    lines.append('|------|--------|-----|----------|-------------|----------|---------|----------|----------|------|------|----------|--------|------|------|------|------|')
    # 行（截取用户模板所需的列，去掉排名Δ 列）
    # build_rotation_table_rows 返回的是 "| 银行ETF | 1.052 | ... | 排名Δ | 操作参考 |" 格式
    # 我们在此手动重写行，以严格匹配用户模板的 8 列（不含排名Δ）
    results = rotation_result.get('results', [])
    for r in results:
        label = r.get('label', r.get('symbol', ''))
        if not r.get('data_sufficient'):
            close_s = '—'
            adx_s = '—'
            ratio_s = '—'
            mom_s = '—'
            quad_s = f'数据不足'
            score_s = '—'
            action_with_s = '—'
            action_without_s = '—'
            oversold_s = '—'
            rebound_s = '—'
            trend_h_s = '—'
            rs_h_s = '—'
            improve_s = '—'
            lead_s = '—'
            confirm_s = '—'
            hold_s = '—'
        else:
            close_s = f"{r['close']:.3f}" if r.get('close') is not None else '—'
            if r.get('adx') is not None:
                adx_s = f"{r['adx']:.1f}"
                # 方向箭头：↑ 上涨趋势 / ↓ 下跌趋势
                adx_dir = r.get('adx_direction', '')
                if adx_dir == 'up':
                    adx_s += '↑'
                elif adx_dir == 'down':
                    adx_s += '↓'
            else:
                adx_s = '—'
            ratio_s = f"{r['rs_ratio']:.2f}" if r.get('rs_ratio') is not None else '—'
            mom_raw = r.get('rs_momentum')
            if mom_raw is None:
                mom_s = '—'
            else:
                sign = '+' if mom_raw >= 0 else ''
                mom_s = f'{sign}{mom_raw:.1f}%'
            emoji = r.get('quadrant_emoji', '')
            quad_s = f'{emoji} {r.get("quadrant", "")}'
            s5 = r.get('score_5d') or {}
            score_s = f"{s5.get('total_score', '—'):.1f}" if s5.get('total_score') is not None else '—'
            if s5.get('crowding_penalty_applied'):
                score_s += ' ⚠️'
            # 横截面 z-score 标注（维2标准化生效时显示）
            if s5.get('mom_acc_cross_sectional'):
                z = s5.get('mom_acc_zscore')
                if z is not None:
                    sign = '+' if z >= 0 else ''
                    score_s += f' z{sign}{z:.1f}'
            action_with_s = s5.get('action_with', '—')
            action_without_s = s5.get('action_without', '—')
            oversold_s = '是' if s5.get('oversold_flag') else '否'
            rebound_s = s5.get('rebound_level', '无')
            oversold_days = s5.get('oversold_days', 0)
            oversold_expired = s5.get('oversold_expired', False)
            valid_days = rotation_result.get('oversold_valid_days', 5)
            # 评分列加反弹级别后缀（含持续天数/有效期）
            if oversold_expired:
                # 已失效默认隐藏，展开时显示 [已失效 N/5]
                score_s += f' [已失效 {oversold_days}/{valid_days}]'
            elif rebound_s != '无':
                score_s += f' [{rebound_s} {oversold_days}/{valid_days}]'
            # 趋势健康 / RS健康 / 拐点预警
            trend_h = s5.get('trend_health')
            rs_h = s5.get('rs_health')
            trend_h_s = f'{trend_h:.1f}' if trend_h is not None else '—'
            rs_h_s = f'{rs_h:.1f}' if rs_h is not None else '—'
            improve_s = '改善' if s5.get('improvement_signal') else '—'
            # 领先预警分（0~100，仅观察名单）
            lead_sc = s5.get('lead_score')
            lead_s = f'{lead_sc}' if lead_sc is not None else '—'
            # 同步确认分（0~100，过滤假反弹）
            confirm_sc = s5.get('confirm_score')
            confirm_s = f'{confirm_sc}' if confirm_sc is not None else '—'
            # 持有分（0~100，加仓/减仓/移动止损，不预测拐点）
            hold_sc = s5.get('hold_score')
            hold_state = s5.get('hold_state')
            if hold_sc is None:
                hold_s = '—'
            elif hold_state == 'exit':
                hold_s = f'{hold_sc}离场'
            elif hold_state == 'hold':
                hold_s = f'{hold_sc}持有'
            else:
                hold_s = f'{hold_sc}减仓'
        lines.append(f'| {label} | {close_s} | {adx_s} | {ratio_s} | {mom_s} | {quad_s} | {score_s} | {action_with_s} | {action_without_s} | {oversold_s} | {rebound_s} | {trend_h_s} | {rs_h_s} | {improve_s} | {lead_s} | {confirm_s} | {hold_s} |')

    lines.append('')

    # 底部生成时间
    analyzed_at = rotation_result.get('analyzed_at', '')
    if analyzed_at:
        lines.append(f'<sub>生成时间: {analyzed_at}</sub>')
        lines.append('')

    # ---- 第三部分：连板龙头 & 大盘温度（可选）----
    if linkban_result is not None:
        lines.append('### 🔥 连板龙头与大盘温度')
        lines.append('')
        # 用 ```text 包裹，保持等宽对齐
        lines.append('```text')
        lines.append(format_linkban_report(linkban_result).rstrip())
        lines.append('```')
        lines.append('')

    return '\n'.join(lines)


# ====================================================================
# 连板龙头 & 大盘温度：ASCII 等宽文本报告 + CSV 行
# ====================================================================

# 连板 CSV 表头
LINKBAN_CSV_FIELDS = [
    'run_time', 'ref_date',
    'total_limit_up', 'total_consecutive', 'max_board',
    'tier_2_cnt', 'tier_3_cnt', 'tier_4_cnt', 'tier_5_cnt', 'tier_ge6_cnt',
    'jinji_rate', 'jinji_molecule', 'jinji_denominator',
    'temp_score', 'temp_level',
    'dragon_code', 'dragon_name', 'dragon_board', 'dragon_reason',
    'dragon_seal_money_yuan',
    'data_sufficient',
]


def format_linkban_csv_row(result: dict) -> dict:
    """把 analyze_linkban 的结果展平为一行 CSV dict。"""
    dragon = result.get('dragon_head') or {}
    tier_counts = result.get('tier_counts') or {}
    # 梯队计数（2/3/4/5 单独，≥6 合并）
    ge6 = sum(int(v) for k, v in tier_counts.items() if isinstance(k, int) and k >= 6)
    row = {
        'run_time': result.get('run_time', ''),
        'ref_date': result.get('ref_date', ''),
        'total_limit_up': int(result.get('total_limit_up', 0) or 0),
        'total_consecutive': int(result.get('total_consecutive', 0) or 0),
        'max_board': int(result.get('max_board', 0) or 0),
        'tier_2_cnt': int(tier_counts.get(2, 0)),
        'tier_3_cnt': int(tier_counts.get(3, 0)),
        'tier_4_cnt': int(tier_counts.get(4, 0)),
        'tier_5_cnt': int(tier_counts.get(5, 0)),
        'tier_ge6_cnt': int(ge6),
        'jinji_rate': ('' if result.get('jinji_rate') is None
                       else round(float(result['jinji_rate']), 4)),
        'jinji_molecule': int(result.get('jinji_today_continued', 0) or 0),
        'jinji_denominator': int(result.get('jinji_yesterday_total', 0) or 0),
        'temp_score': int(result.get('temp_score', 0) or 0),
        'temp_level': result.get('temp_level', ''),
        'dragon_code':   dragon.get('code', ''),
        'dragon_name':   dragon.get('name', ''),
        'dragon_board':  int(dragon['board_cnt']) if dragon.get('board_cnt') is not None else '',
        'dragon_reason': dragon.get('limit_up_reason', ''),
        'dragon_seal_money_yuan': ('' if dragon.get('seal_money_yuan') is None
                                   else round(float(dragon['seal_money_yuan']), 2)),
        'data_sufficient': bool(result.get('data_sufficient', False)),
    }
    return {k: row.get(k, '') for k in LINKBAN_CSV_FIELDS}


def _format_yi(yuan) -> str:
    """金额元 → "X.XX亿"。"""
    if yuan is None or (isinstance(yuan, float) and math.isnan(yuan)):
        return '—'
    try:
        y = float(yuan)
    except Exception:
        return '—'
    if y <= 0:
        return '—'
    return f'{y/1e8:.2f}亿'


def format_linkban_report(result: dict) -> str:
    """生成严格按用户"输出示例"风格的 ASCII 等宽文本报告。"""
    ref = result.get('ref_date', '')
    lines = [f'========== {ref} 大盘温度报告 ==========']
    lines.append('')

    # 1. 数据不足（非交易日 / 接口不可用）兜底
    if not result.get('data_sufficient', False):
        reason = result.get('reason') or '今日非交易日或数据不足'
        score = int(result.get('temp_score', 30) or 30)
        level = result.get('temp_level', '❄️ 低温（市场低迷）')
        advice = result.get('temp_advice', '观望为主或控制仓位')
        lines.append('【连板概况】')
        lines.append(f'  状态: {reason}')
        lines.append('')
        lines.append('【情绪指标】')
        lines.append(f'  晋级率: —')
        lines.append(f'  温度评分: {score}分')
        lines.append(f'  温度等级: {level}')
        lines.append(f'  操作建议: {advice}')
        lines.append(f'==========================================')
        return '\n'.join(lines)

    total_up  = int(result.get('total_limit_up', 0) or 0)
    total_con = int(result.get('total_consecutive', 0) or 0)
    max_board = int(result.get('max_board', 0) or 0)

    # 【连板概况】
    lines.append('【连板概况】')
    lines.append(f'  涨停总数: {total_up}只')
    lines.append(f'  连板总数: {total_con}只')
    lines.append(f'  最高连板: {max_board}板' if max_board > 0 else '  最高连板: —')
    lines.append('')

    # 【连板梯队】
    lines.append('【连板梯队】')
    tier_counts = result.get('tier_counts') or {}
    tier_dist = result.get('tier_distribution') or {}
    if not tier_counts:
        lines.append('  —（无连板股）')
    else:
        for board in sorted(tier_counts.keys(), reverse=True):
            count = int(tier_counts[board])
            items = tier_dist.get(board, [])
            names = [str(it.get('name', '')) for it in items if str(it.get('name', '')).strip()]
            names_line = '、'.join(names)
            suffix = f'  → {names_line}' if names_line else ''
            lines.append(f'  {board}板: {count}只 {suffix}')
    lines.append('')

    # 【龙头股详情】
    lines.append('【龙头股详情】')
    dr = result.get('dragon_head')
    if dr is None:
        lines.append('  —（当日无连板龙头）')
    else:
        code = str(dr.get('code', '—'))
        name = str(dr.get('name', '—'))
        board_cnt = int(dr['board_cnt']) if dr.get('board_cnt') is not None else 0
        reason = str(dr.get('limit_up_reason') or '—')
        seal = _format_yi(dr.get('seal_money_yuan'))
        lines.append(f'  名称: {name}')
        lines.append(f'  代码: {code}')
        lines.append(f'  连板: {board_cnt}连板' if board_cnt > 0 else '  连板: —')
        lines.append(f'  涨停原因: {reason}')
        lines.append(f'  封单金额: {seal}')
    lines.append('')

    # 【情绪指标】
    lines.append('【情绪指标】')
    jr = result.get('jinji_rate')
    if jr is None or (isinstance(jr, float) and math.isnan(jr)):
        jinji_s = '—'
    else:
        jinji_s = f'{float(jr)*100:.1f}%'
    lines.append(f'  晋级率: {jinji_s}')
    lines.append(f'  温度评分: {int(result.get("temp_score",0) or 0)}分')
    lines.append(f'  温度等级: {result.get("temp_level","")}')
    lines.append(f'  操作建议: {result.get("temp_advice","")}')
    lines.append(f'==========================================')

    return '\n'.join(lines)


# ====================================================================
# Markdown 报告落盘（按 REPORT_DIR 写入）
# ====================================================================

def save_markdown_report(md_text: str, cfg, ref_date: str) -> str:
    """把 Markdown 报告写入 data/reports/rotation/ 目录。

    文件名：{ref_date}_rotation.md（后续可加 symbol/source 后缀）。
    返回写入路径。
    """
    import os
    from datetime import datetime

    report_dir = os.path.join(
        os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')),
        cfg.REPORT_DIR,
        ref_date[:7],  # YYYY-MM 子目录
    )
    os.makedirs(report_dir, exist_ok=True)

    path = os.path.join(report_dir, f'{ref_date}_rotation.md')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(md_text)
    return path
