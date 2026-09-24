"""交易习惯约束器 MVP 业务规则与计算层。

纯 Python 函数，无 tkinter 依赖，便于单测与复用。
依赖 db 模块的查询函数。
"""
from __future__ import annotations

import csv
import json
import math
from datetime import date, datetime
from typing import Optional

import db


# ====================================================================
# 仓位建议与计划单校验
# ====================================================================

def suggest_position_size(
    account_balance: float,
    risk_pct: float,
    entry_price_high: float,
    stop_loss: float,
) -> tuple[int, float, str]:
    """仓位建议计算。

    Args:
        account_balance: 账户资金
        risk_pct: 单笔风险比例（0-100）
        entry_price_high: 入场价上沿
        stop_loss: 止损价

    Returns:
        (建议股数, 建议最大亏损, 警告信息)
        - 单笔风险金额 = account_balance * risk_pct / 100
        - 每股风险 = abs(entry_price_high - stop_loss)
        - 建议股数 = floor(单笔风险金额 / 每股风险 / 100) * 100
        - 建议最大亏损 = 建议股数 * 每股风险
        - 警告：若建议最大亏损 > 单笔风险金额，返回红字警告
    """
    if account_balance <= 0 or risk_pct < 0:
        return 0, 0.0, "账户资金或风险比例无效"
    if entry_price_high <= 0 or stop_loss <= 0:
        return 0, 0.0, ""
    per_share_risk = abs(entry_price_high - stop_loss)
    if per_share_risk <= 0:
        return 0, 0.0, "入场价上沿与止损价相同，每股风险为 0"
    risk_amount = account_balance * risk_pct / 100.0
    raw_lots = risk_amount / per_share_risk / 100.0
    shares = max(0, int(math.floor(raw_lots))) * 100
    max_loss = shares * per_share_risk
    warning = ""
    if max_loss > risk_amount + 1e-9:
        warning = (
            f"建议最大亏损 {max_loss:.2f} 超过单笔风险金额 {risk_amount:.2f}"
        )
    return shares, round(max_loss, 2), warning


def validate_plan(data: dict) -> list[str]:
    """新增/编辑计划单校验。返回错误列表（空 = 通过）。

    必填：stock_code、entry_trigger、stop_loss、time_stop_date、
          planned_shares、max_loss_amount
    多头规则：stop_loss < entry_price_high（若 entry_price_high 给定且 > 0）
    """
    errs: list[str] = []
    required = {
        "stock_code": "股票代码",
        "entry_trigger": "入场触发条件",
        "stop_loss": "止损价",
        "time_stop_date": "时间止损日期",
        "planned_shares": "计划股数",
        "max_loss_amount": "最大亏损金额",
    }
    for k, label in required.items():
        v = data.get(k)
        if v is None or v == "":
            errs.append(f"{label} 不能为空")
            continue
        if k in ("stop_loss", "planned_shares", "max_loss_amount"):
            try:
                fv = float(v)
                if fv <= 0:
                    errs.append(f"{label} 必须为正数")
            except (TypeError, ValueError):
                errs.append(f"{label} 不是有效数字")
    entry_high = data.get("entry_price_high")
    stop_loss = data.get("stop_loss")
    if entry_high is not None and entry_high != "":
        try:
            eh = float(entry_high)
            sl = float(stop_loss) if stop_loss not in (None, "") else 0.0
            if sl > 0 and sl >= eh:
                errs.append("做多计划要求止损价 < 入场价上沿")
        except (TypeError, ValueError):
            errs.append("入场价上沿或止损价不是有效数字")
    return errs


# ====================================================================
# 盈亏计算
# ====================================================================

def calc_pnl(
    entry_price: float,
    exit_price: float,
    shares: int,
    fees: float,
    stop_loss: float,
) -> tuple[float, float]:
    """计算盈亏金额与盈亏R。

    - pnl_amount = (exit_price - entry_price) * shares - fees
    - 初始风险 = abs(entry_price - stop_loss) * shares
    - pnl_r = pnl_amount / 初始风险（初始风险=0 时返回 0.0）
    """
    pnl = (exit_price - entry_price) * shares - fees
    init_risk = abs(entry_price - stop_loss) * shares
    if init_risk <= 0:
        return pnl, 0.0
    return pnl, pnl / init_risk


# ====================================================================
# 时间工具
# ====================================================================

def today_iso() -> str:
    return date.today().isoformat()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def is_today(ts_iso: Optional[str]) -> bool:
    """判断 ISO 时间戳的日期部分是否为今天。"""
    if not ts_iso:
        return False
    return ts_iso[:10] == today_iso()


# ====================================================================
# 红黄绿灯
# ====================================================================

def _to_float(s, default: float = 0.0) -> float:
    try:
        return float(s) if s is not None else default
    except (TypeError, ValueError):
        return default


def _to_int(s, default: int = 0) -> int:
    try:
        return int(float(s)) if s is not None else default
    except (TypeError, ValueError):
        return default


def get_traffic_light(
    settings: dict,
    today_pnl: float,
    today_entries: int,
) -> tuple[str, str]:
    """红黄绿灯。

    Returns:
        (颜色, 说明) 颜色 ∈ {'green', 'yellow', 'red'}
    """
    max_loss = _to_float(settings.get("daily_max_loss"), 0.0)
    max_trades = _to_int(settings.get("daily_max_trades"), 0)

    loss_red = max_loss > 0 and today_pnl <= -max_loss
    loss_yellow = max_loss > 0 and today_pnl <= -max_loss * 0.8
    trades_red = max_trades > 0 and today_entries >= max_trades
    trades_yellow = (
        max_trades > 0
        and today_entries >= max_trades * 0.8
        and not trades_red
    )

    if loss_red or trades_red:
        reasons = []
        if loss_red:
            reasons.append(
                f"今日亏损 {today_pnl:.2f} 达到/超过日亏上限 -{max_loss:.2f}"
            )
        if trades_red:
            reasons.append(
                f"今日入场 {today_entries} 次达到日交易次数上限 {max_trades}"
            )
        return "red", "；".join(reasons)
    if loss_yellow or trades_yellow:
        reasons = []
        if loss_yellow:
            reasons.append(
                f"今日亏损 {today_pnl:.2f} 接近日亏上限 -{max_loss:.2f} 的 80%"
            )
        if trades_yellow:
            reasons.append(
                f"今日入场 {today_entries} 次接近日交易次数上限 {max_trades} 的 80%"
            )
        return "yellow", "；".join(reasons)
    return "green", "正常"


# ====================================================================
# 检查清单
# ====================================================================

CHECKLIST_ITEMS = [
    # (key, label, auto)
    ("stock_tradable", "股票在股票池且状态为「可交易」", True),
    ("plan_complete", "已创建完整计划单", True),
    ("trigger_met", "入场触发条件已满足", False),
    ("stop_loss_executable", "止损价明确且可执行", False),
    ("time_stop_set", "时间止损已设定", False),
    ("position_within_risk", "仓位/最大亏损未超单笔风险上限", True),
    ("trades_under_limit", "今日交易次数未超上限", True),
    ("no_red_light", "当前无红灯", True),
]


def build_checklist(
    plan: dict,
    settings: dict,
    today_entries: int,
    traffic_light: str,
) -> list[dict]:
    """构建 8 项检查清单。每项 dict: {key, label, auto, passed, note}。

    auto=True 由系统判断；auto=False 默认 passed=False 待用户勾选。
    """
    items: list[dict] = []

    # 1. 股票在股票池且状态='可交易'
    stock = db.get_stock_pool_by_code(
        plan.get("stock_code", ""), status="可交易"
    )
    items.append({
        "key": "stock_tradable",
        "label": "股票在股票池且状态为「可交易」",
        "auto": True,
        "passed": stock is not None,
        "note": (
            f"股票池状态：{stock['status']}" if stock
            else f"股票 {plan.get('stock_code', '')} 不在可交易列表"
        ),
    })

    # 2. 已创建完整计划单
    plan_complete = (
        bool(plan)
        and plan.get("status") not in (None, "", "草稿")
        and bool(plan.get("entry_trigger"))
        and plan.get("stop_loss") not in (None, "")
        and plan.get("planned_shares") not in (None, "")
    )
    items.append({
        "key": "plan_complete",
        "label": "已创建完整计划单",
        "auto": True,
        "passed": plan_complete,
        "note": f"计划单状态：{plan.get('status', '')}",
    })

    # 3. 入场触发条件已满足（人工）
    items.append({
        "key": "trigger_met",
        "label": "入场触发条件已满足",
        "auto": False,
        "passed": False,
        "note": "人工勾选",
    })

    # 4. 止损价明确且可执行（人工）
    items.append({
        "key": "stop_loss_executable",
        "label": "止损价明确且可执行",
        "auto": False,
        "passed": False,
        "note": "人工勾选",
    })

    # 5. 时间止损已设定（人工）
    items.append({
        "key": "time_stop_set",
        "label": "时间止损已设定",
        "auto": False,
        "passed": False,
        "note": "人工勾选",
    })

    # 6. 仓位/最大亏损未超单笔风险上限
    balance = _to_float(settings.get("account_balance"))
    risk_pct = _to_float(settings.get("risk_per_trade_pct"))
    risk_amount = balance * risk_pct / 100.0
    plan_max_loss = _to_float(plan.get("max_loss_amount"))
    within = risk_amount > 0 and plan_max_loss <= risk_amount + 1e-9
    items.append({
        "key": "position_within_risk",
        "label": "仓位/最大亏损未超单笔风险上限",
        "auto": True,
        "passed": within,
        "note": (
            f"计划最大亏损 {plan_max_loss:.2f} vs 单笔风险上限 {risk_amount:.2f}"
        ),
    })

    # 7. 今日交易次数未超上限
    max_trades = _to_int(settings.get("daily_max_trades"))
    under = max_trades <= 0 or today_entries < max_trades
    items.append({
        "key": "trades_under_limit",
        "label": "今日交易次数未超上限",
        "auto": True,
        "passed": under,
        "note": f"今日 {today_entries}/{max_trades} 次",
    })

    # 8. 当前无红灯
    items.append({
        "key": "no_red_light",
        "label": "当前无红灯",
        "auto": True,
        "passed": traffic_light != "red",
        "note": (
            "当前：绿灯" if traffic_light == "green"
            else "当前：黄灯" if traffic_light == "yellow"
            else "当前：红灯"
        ),
    })

    return items


def all_checklist_passed(items: list[dict]) -> bool:
    """全部通过判断。"""
    return all(it.get("passed") for it in items)


# ====================================================================
# 条件单提醒文本
# ====================================================================

def build_condition_order_text(plan: dict, stock: Optional[dict] = None) -> str:
    """生成条件单提醒文本。"""
    code = plan.get("stock_code", "")
    name = plan.get("stock_name", "") or (
        stock.get("name", "") if stock else ""
    )
    trigger = plan.get("entry_trigger", "")
    stop = plan.get("stop_loss", "")
    time_stop = plan.get("time_stop_date", "")
    target = plan.get("target_price", "")
    shares = plan.get("planned_shares", "")
    max_loss = plan.get("max_loss_amount", "")
    lines = [
        f"代码：{code}",
        f"名称：{name}",
        f"入场触发：{trigger}",
        f"止损：{stop}",
        f"时间止损：{time_stop}",
        f"目标：{target}",
        f"计划股数：{shares}",
        f"最大亏损：{max_loss}",
        "请到券商 App 手动设置条件单/警报。",
    ]
    return "\n".join(lines)


# ====================================================================
# CSV 导出
# ====================================================================

TRADE_LOG_CSV_FIELDS = [
    "id", "plan_id", "stock_code", "stock_name",
    "entry_time", "entry_price", "shares",
    "exit_time", "exit_price",
    "pnl_amount", "pnl_r", "fees",
    "followed_plan", "deviation_reason",
    "emotion_tag", "emotion_intensity", "notes",
    "created_at",
]

TRADE_PLAN_CSV_FIELDS = [
    "id", "stock_code", "stock_name", "entry_trigger",
    "entry_price_low", "entry_price_high",
    "stop_loss", "time_stop_date", "target_price",
    "planned_shares", "max_loss_amount", "invalidation",
    "status", "created_at", "updated_at",
]


def _fmt_cell(v):
    if v is None:
        return ""
    if isinstance(v, float):
        # 保留 4 位小数，去尾零
        s = f"{v:.4f}".rstrip("0").rstrip(".")
        return s if s else "0"
    return str(v)


def export_trade_logs_csv(logs: list[dict], path: str) -> None:
    """导出交易日志 CSV，编码 utf-8-sig。"""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(TRADE_LOG_CSV_FIELDS)
        for row in logs:
            w.writerow([_fmt_cell(row.get(k)) for k in TRADE_LOG_CSV_FIELDS])


def export_trade_plans_csv(plans: list[dict], path: str) -> None:
    """导出计划单 CSV，编码 utf-8-sig。"""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(TRADE_PLAN_CSV_FIELDS)
        for row in plans:
            w.writerow([_fmt_cell(row.get(k)) for k in TRADE_PLAN_CSV_FIELDS])


# ====================================================================
# 外部导入（CSV / JSON → 待确认）
# ====================================================================

STOCK_POOL_IMPORT_FIELDS = [
    "code", "name", "logic", "key_level", "catalyst", "risk",
]


def _normalize_stock_pool_row(raw: dict) -> Optional[dict]:
    """规范化外部导入的一行。返回 None 表示跳过。"""
    code = (raw.get("code") or raw.get("Code") or raw.get("代码") or "").strip()
    if not code:
        return None
    return {
        "code": code,
        "name": (raw.get("name") or raw.get("Name")
                 or raw.get("名称") or "").strip(),
        "logic": (raw.get("logic") or raw.get("Logic")
                  or raw.get("逻辑") or "").strip(),
        "key_level": (raw.get("key_level") or raw.get("KeyLevel")
                      or raw.get("关键位") or "").strip(),
        "catalyst": (raw.get("catalyst") or raw.get("Catalyst")
                     or raw.get("催化剂") or "").strip(),
        "risk": (raw.get("risk") or raw.get("Risk")
                 or raw.get("风险") or "").strip(),
        "status": "待确认",
    }


def import_stock_pool_csv(path: str) -> list[dict]:
    """从 CSV 导入股票池候选。返回待插入的 dict 列表。

    编码尝试顺序：utf-8-sig → utf-8 → gbk。
    """
    rows: list[dict] = []
    content = None
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                content = f.read()
            break
        except UnicodeDecodeError:
            continue
    if content is None:
        return rows
    import io
    reader = csv.DictReader(io.StringIO(content))
    for r in reader:
        if r is None:
            continue
        normalized = _normalize_stock_pool_row(r)
        if normalized:
            rows.append(normalized)
    return rows


def import_stock_pool_json(path: str) -> list[dict]:
    """从 JSON 导入股票池候选。返回 dict 列表。

    支持两种结构：
        - 数组：[{code, name, ...}, ...]
        - 对象：{"pool": [{...}, ...]} 或 {"stock_pool": [...]}
    """
    rows: list[dict] = []
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(path, "r", encoding=enc) as f:
                data = json.load(f)
            break
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = None
            continue
    if data is None:
        return rows
    if isinstance(data, list):
        raw_list = data
    elif isinstance(data, dict):
        raw_list = (
            data.get("pool")
            or data.get("stock_pool")
            or data.get("data")
            or []
        )
    else:
        raw_list = []
    for r in raw_list:
        if not isinstance(r, dict):
            continue
        normalized = _normalize_stock_pool_row(r)
        if normalized:
            rows.append(normalized)
    return rows


# ====================================================================
# 分析报告解析（粘贴文本 → 计划单字段）
# ====================================================================

import re


def _regex_search(pattern: str, text: str, group: int = 1) -> Optional[str]:
    """安全正则搜索。"""
    m = re.search(pattern, text)
    if m and m.lastindex and group <= m.lastindex:
        return m.group(group)
    return None


def parse_analysis_report(text: str) -> dict:
    """从结构化分析报告文本提取计划单可填字段。

    支持的报告结构（参考用户样例）：
        - 数据范围: YYYY-MM-DD 至 YYYY-MM-DD
        - 最新收盘价 / 当前价: <float>
        - 维度A - 结构（回踩信号排查）：均线、偏离、缩量等
        - 维度B - 动量：MACD、RSI、量比等
        - 维度C - 赔率：
            - 支撑位(止损): <float>，亏损空间: <float>
            - 压力位(止盈): <float>，盈利空间: <float>
            - 盈亏比: <float>:1

    Returns:
        dict，可能含以下键（缺失则不出现）：
            entry_price_low, entry_price_high, stop_loss, target_price,
            entry_trigger, invalidation, notes, rr_ratio, data_end_date
    """
    result: dict = {}

    # ── 数据范围 → 结束日期 ──
    end_date = _regex_search(
        r"数据范围\s*[:：]\s*\d{4}-\d{2}-\d{2}\s*[至到\-~]+\s*(\d{4}-\d{2}-\d{2})",
        text,
    )
    if end_date:
        result["data_end_date"] = end_date

    # ── 价格：最新收盘价 / 当前价 ──
    # 优先匹配「最新收盘价」「当前价」（维度C 里的「当前价」最准）
    price_patterns = [
        r"最新收盘价\s*[:：]\s*([\d.]+)",
        r"📈\s*最新收盘价\s*[:：]\s*([\d.]+)",
        r"当前价\s*[:：]\s*([\d.]+)",
    ]
    price = None
    for pat in price_patterns:
        price = _regex_search(pat, text)
        if price:
            break
    if price:
        try:
            result["entry_price_low"] = float(price)
            result["entry_price_high"] = float(price)
        except ValueError:
            pass

    # ── 止损价（支撑位）──
    # 形式：「支撑位(止损): 10.65」或「支撑位(止损):10.65，亏损空间: 0.49」
    stop = _regex_search(
        r"支撑位\s*[\(（]\s*止损\s*[\)）]\s*[:：]\s*([\d.]+)",
        text,
    )
    if stop:
        try:
            result["stop_loss"] = float(stop)
        except ValueError:
            pass

    # ── 目标价（压力位 / 止盈）──
    target = _regex_search(
        r"压力位\s*[\(（]\s*止盈\s*[\)）]\s*[:：]\s*([\d.]+)",
        text,
    )
    if target:
        try:
            result["target_price"] = float(target)
        except ValueError:
            pass

    # ── 盈亏比 ──
    rr = _regex_search(r"盈亏比\s*[:：]\s*([\d.]+)\s*[:：]?\s*1", text)
    if rr:
        try:
            result["rr_ratio"] = float(rr)
        except ValueError:
            pass

    # ── 入场触发条件（综合维度A/B 简短摘要）──
    trigger_parts: list[str] = []

    # 维度A 整体状态判断：
    # 只认行首的整行明确陈述，避免子项「✅ 偏离度检查通过」误匹配
    # 通过：「✅ 维度A通过」 或 「维度A ✅ 通过」
    # 不通过：「❌ 维度A不通过」
    dim_a_pass = re.search(
        r"(?:^|\n)\s*✅\s*维度A\s*通过", text,
    )
    dim_a_fail = re.search(
        r"(?:^|\n)\s*❌\s*维度A\s*不通过[—\-]*\s*(.+?)(?=\n\s*【维度|\n\s*❌|\Z)",
        text, re.DOTALL,
    )
    if dim_a_pass:
        trigger_parts.append("维度A结构通过")
    elif dim_a_fail:
        raw_reason = dim_a_fail.group(1).strip()
        # 取第一行 + 去前导连字符
        first_line = raw_reason.split("\n")[0].strip().lstrip("-").strip()
        if first_line:
            trigger_parts.append(f"维度A未通过：{first_line[:60]}")
        else:
            trigger_parts.append("维度A未通过")

    # 维度B 整体状态判断（同上规则）
    dim_b_pass = re.search(
        r"(?:^|\n)\s*✅\s*维度B\s*通过", text,
    )
    dim_b_fail = re.search(
        r"(?:^|\n)\s*❌\s*维度B\s*不通过\s*\n?\s*(.+?)(?=\n\s*【维度|\n\s*❌|\Z)",
        text, re.DOTALL,
    )
    if dim_b_pass:
        trigger_parts.append("维度B动量通过")
    elif dim_b_fail:
        raw_reason = dim_b_fail.group(1).strip()
        first_line = raw_reason.split("\n")[0].strip().lstrip("-").strip()
        if first_line:
            trigger_parts.append(f"维度B未通过：{first_line[:60]}")
        else:
            trigger_parts.append("维度B未通过")

    if trigger_parts:
        result["entry_trigger"] = "；".join(trigger_parts)

    # ── 失效条件（综合负面信号）──
    invalidation_parts: list[str] = []
    # 维度A 淘汰原因（多条）
    for m in re.finditer(r"淘汰原因\s*[:：]\s*(.+)", text):
        invalidation_parts.append(m.group(1).strip())
    # 维度A 整体不通过
    if dim_a_fail:
        invalidation_parts.append("维度A结构未通过")
    # 维度B 失败项
    if dim_b_fail:
        invalidation_parts.append("维度B动量未通过")
    if invalidation_parts:
        # 去重，保留前 5 条
        seen = set()
        uniq = []
        for x in invalidation_parts:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        result["invalidation"] = "；".join(uniq[:5])

    # ── 完整原文 → notes（最多 2000 字符，避免 SQLite TEXT 过长）──
    trimmed = text.strip()[:2000]
    if trimmed:
        result["notes_raw"] = trimmed

    return result
