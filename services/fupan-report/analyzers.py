"""五大维度量化复盘分析器（纯函数，无 IO）。

每个函数接收清洗后的 DataFrame，返回结构化 dict，供 reporter 渲染。

资金流水口径约定（招商证券）：
- 买入：成交数量 > 0，发生金额 < 0（资金流出）
- 卖出：成交数量 < 0，发生金额 > 0（资金流入）
故：买入成本 = -发生金额[买].sum()，卖出收入 = 发生金额[卖].sum()
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from config import (
    COMMISSION_RATE_WARN_BPS,
    OPTIONAL_FEE_COLUMNS,
    TOP_N_HIGH_COMMISSION,
    TOP_N_HOLDINGS,
    TOP_N_LOSS,
    TOP_N_PROFIT,
)

_BUY_MASK = lambda df: df["成交数量"] > 0
_SELL_MASK = lambda df: df["成交数量"] < 0


def _total_fees(df: pd.DataFrame) -> float:
    """规费合计 = 经手费 + 证管费 + 过户费（可选列缺失已补 0）。"""
    return float(sum(df[c].sum() for c in OPTIONAL_FEE_COLUMNS))


def _avg_buy_price(df_sym: pd.DataFrame) -> float | None:
    """单标的加权平均买入价 = Σ(价格×数量) / Σ数量，仅买入。无买入返回 None。"""
    buys = df_sym[_BUY_MASK(df_sym)]
    if buys.empty:
        return None
    qty = buys["成交数量"].sum()
    if qty == 0:
        return None
    return float((buys["成交价格"] * buys["成交数量"]).sum() / qty)


def _avg_sell_price(df_sym: pd.DataFrame) -> float | None:
    """单标的加权平均卖出价 = Σ(价格×|数量|) / Σ|数量|，仅卖出。无卖出返回 None。"""
    sells = df_sym[_SELL_MASK(df_sym)]
    if sells.empty:
        return None
    qty = sells["成交数量"].abs().sum()
    if qty == 0:
        return None
    return float((sells["成交价格"] * sells["成交数量"].abs()).sum() / qty)


# ============================================================================
# 维度1：交易成本分析
# ============================================================================
def analyze_cost(df: pd.DataFrame) -> dict:
    total_turnover = float(df["发生金额"].abs().sum())  # 双边总交易额
    total_commission = float(df["佣金"].sum())
    total_stamp_tax = float(df["印花税"].sum())
    total_fees = _total_fees(df)

    commission_rate_bps = (
        total_commission / total_turnover * 10000 if total_turnover > 0 else 0.0
    )
    warn = commission_rate_bps > COMMISSION_RATE_WARN_BPS

    # 单笔佣金最高前 N
    top = df.nlargest(TOP_N_HIGH_COMMISSION, "佣金")[
        ["成交日期", "证券名称", "成交价格", "成交数量", "佣金"]
    ].copy()
    top["方向"] = np.where(top["成交数量"] > 0, "买", "卖")
    top_commission_trades = [
        {
            "日期": r["成交日期"].strftime("%Y-%m-%d"),
            "证券名称": r["证券名称"],
            "方向": r["方向"],
            "佣金": float(r["佣金"]),
        }
        for _, r in top.iterrows()
    ]

    return {
        "total_turnover": total_turnover,
        "total_commission": total_commission,
        "total_stamp_tax": total_stamp_tax,
        "total_fees": total_fees,
        "commission_rate_bps": commission_rate_bps,
        "warn": warn,
        "top_commission_trades": top_commission_trades,
    }


# ============================================================================
# 维度2：标的绩效复盘
# ============================================================================
def analyze_performance(df: pd.DataFrame) -> dict:
    rows = []
    for name, g in df.groupby("证券名称"):
        buy_cost = -float(g[_BUY_MASK(g)]["发生金额"].sum())  # 买入支出
        sell_income = float(g[_SELL_MASK(g)]["发生金额"].sum())  # 卖出收入
        realized_pnl = sell_income - buy_cost
        rows.append(
            {
                "证券名称": name,
                "证券代码": str(g["证券代码"].iloc[-1]),
                "buy_cost": buy_cost,
                "sell_income": sell_income,
                "realized_pnl": realized_pnl,
                "trade_count": int(len(g)),
            }
        )

    perfs = pd.DataFrame(rows)
    if perfs.empty:
        return {"profit_top": [], "loss_top": [], "all": []}

    # 盈利榜仅取 realized_pnl>0，降序；亏损榜仅取 realized_pnl<0，升序
    profit_top = perfs[perfs["realized_pnl"] > 0].sort_values(
        "realized_pnl", ascending=False
    ).head(TOP_N_PROFIT)
    loss_top = perfs[perfs["realized_pnl"] < 0].sort_values(
        "realized_pnl", ascending=True
    ).head(TOP_N_LOSS)

    return {
        "profit_top": profit_top.to_dict("records"),
        "loss_top": loss_top.to_dict("records"),
        "all": perfs.to_dict("records"),
    }


# ============================================================================
# 维度3：买卖点效率
# ============================================================================
def analyze_timing(df: pd.DataFrame) -> dict:
    items = []
    for name, g in df.groupby("证券名称"):
        avg_buy = _avg_buy_price(g)
        avg_sell = _avg_sell_price(g)
        # 仅对同时有买卖的标的计算
        if avg_buy is None or avg_sell is None:
            continue
        spread_rate = (avg_sell - avg_buy) / avg_buy * 100 if avg_buy else 0.0
        verdict = "高抛低吸有效" if spread_rate > 0 else "低抛高吸，操作反向"
        items.append(
            {
                "证券名称": name,
                "avg_buy_price": avg_buy,
                "avg_sell_price": avg_sell,
                "spread_rate": spread_rate,
                "verdict": verdict,
            }
        )
    # 按价差率降序
    items.sort(key=lambda x: x["spread_rate"], reverse=True)
    return {"items": items}


# ============================================================================
# 维度4：资金利用率与仓位管理
# ============================================================================
def analyze_capital(df: pd.DataFrame) -> dict:
    # 每日净流入资金（发生金额 sum：正=净卖出回笼，负=净买入加仓）
    daily = df.groupby("成交日期")["发生金额"].sum().sort_index()
    daily_net_flow = [
        {"日期": d.strftime("%Y-%m-%d"), "net_flow": float(v)}
        for d, v in daily.items()
    ]

    # 前三大重仓：每标的按日期排序后最后一条记录的剩余数量 × 最新成交价
    holding_rows = []
    for name, g in df.groupby("证券名称"):
        last = g.sort_values("成交日期", kind="stable").iloc[-1]
        qty = float(last["剩余数量"])
        if qty <= 0:
            continue
        price = float(last["成交价格"])
        holding_rows.append(
            {
                "证券名称": name,
                "证券代码": str(last["证券代码"]),
                "剩余数量": qty,
                "最新价": price,
                "估算市值": qty * price,
            }
        )
    holding_rows.sort(key=lambda x: x["估算市值"], reverse=True)
    top_holdings = holding_rows[:TOP_N_HOLDINGS]

    # 日均买入金额
    n_days = df["成交日期"].nunique()
    buy_total = float(df[_BUY_MASK(df)]["发生金额"].abs().sum())
    daily_avg_buy = buy_total / n_days if n_days else 0.0

    return {
        "daily_net_flow": daily_net_flow,
        "top_holdings": top_holdings,
        "daily_avg_buy": daily_avg_buy,
        "n_days": n_days,
    }


# ============================================================================
# 维度5：行为纪律复盘（按完整交易轮次聚合，区分浮动 vs 已实现盈亏）
# ============================================================================
def analyze_discipline(df: pd.DataFrame) -> dict:
    buy_count = int((_BUY_MASK(df)).sum())
    sell_count = int((_SELL_MASK(df)).sum())
    sell_buy_ratio = sell_count / buy_count if buy_count else 0.0

    # —— 按证券名称聚合完整交易轮次 ——
    # 判定清仓：该标的历史上出现过剩余数量=0（持仓归零即该轮已结束）。
    # 注意：不用"最终剩余=0"判定，因原始文件行顺序可能不按日期排列；
    # 也不用累计成交数量判定，因可能有买入前的初始持仓。
    # 清仓标的：合并所有交易算一个轮次，计入盈亏比。
    # 持仓标的（从未出现过剩余=0）：盈亏计入浮动，不计入盈亏比。
    rounds = []
    for name, g in df.groupby("证券名称"):
        g_sorted = g.sort_values("成交日期", kind="stable")
        code = str(g_sorted["证券代码"].iloc[0])
        had_cleared = (g_sorted["剩余数量"] == 0).any()  # 曾出现过持仓归零
        buy_cost = -float(g[_BUY_MASK(g)]["发生金额"].sum())
        sell_income = float(g[_SELL_MASK(g)]["发生金额"].sum())
        last_qty = float(g_sorted.iloc[-1]["剩余数量"])
        rounds.append({
            "证券名称": name,
            "证券代码": code,
            "已清仓": had_cleared,
            "剩余数量": last_qty,
            "buy_cost": buy_cost,
            "sell_income": sell_income,
            "round_pnl": sell_income - buy_cost,
            "trade_count": int(len(g)),
        })

    # 已实现盈亏池：仅取已清仓轮次
    closed = [r for r in rounds if r["已清仓"]]
    realized_profit_total = sum(r["round_pnl"] for r in closed if r["round_pnl"] > 0)
    realized_loss_total = abs(sum(r["round_pnl"] for r in closed if r["round_pnl"] < 0))
    # 真实盈亏比 = 已实现总盈利 ÷ 已实现总亏损
    profit_loss_ratio = (
        realized_profit_total / realized_loss_total if realized_loss_total > 0 else float("inf")
    )
    # 胜率（按轮次）：盈利轮数 / 已清仓轮数
    win_rounds = sum(1 for r in closed if r["round_pnl"] > 0)
    loss_rounds = sum(1 for r in closed if r["round_pnl"] < 0)
    win_rate = win_rounds / len(closed) * 100 if closed else 0.0

    # 浮动盈亏：未清仓标的的未锁定盈亏（卖出收入 - 买入成本，仅已卖出部分）
    floating_pnl = sum(r["round_pnl"] for r in rounds if not r["已清仓"])

    # 全局扣费前/后盈亏（含浮动）
    buy_cost_total = -float(df[_BUY_MASK(df)]["发生金额"].sum())
    sell_income_total = float(df[_SELL_MASK(df)]["发生金额"].sum())
    gross_pnl = sell_income_total - buy_cost_total  # = 已实现 + 浮动
    total_commission = float(df["佣金"].sum())
    total_stamp_tax = float(df["印花税"].sum())
    total_fees = _total_fees(df)
    net_pnl = gross_pnl - total_commission - total_stamp_tax - total_fees

    # 综合结论：以已实现扣费后净盈亏为判断基准
    # 费用按已清仓标的的买卖额占比分摊
    closed_buy = sum(r["buy_cost"] for r in closed)
    closed_sell = sum(r["sell_income"] for r in closed)
    fee_ratio = (closed_buy + closed_sell) / (buy_cost_total + sell_income_total) if (buy_cost_total + sell_income_total) else 0
    realized_net = sum(r["round_pnl"] for r in closed) - (total_commission + total_stamp_tax + total_fees) * fee_ratio

    if realized_net >= 0:
        conclusion = "本期已实现盈利，建议维持/适度交易频率"
    else:
        conclusion = "本期已实现亏损，建议减少交易频率、关注佣金侵蚀"

    return {
        "buy_count": buy_count,
        "sell_count": sell_count,
        "sell_buy_ratio": sell_buy_ratio,
        # 真实盈亏比（按轮次）
        "realized_profit_total": realized_profit_total,
        "realized_loss_total": realized_loss_total,
        "profit_loss_ratio": profit_loss_ratio,
        "win_rounds": win_rounds,
        "loss_rounds": loss_rounds,
        "win_rate": win_rate,  # 按轮次的胜率
        "closed_count": len(closed),
        "holding_count": len(rounds) - len(closed),
        "floating_pnl": floating_pnl,
        # 全局盈亏（含浮动）
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "realized_net_pnl": realized_net,
        "total_commission": total_commission,
        "total_stamp_tax": total_stamp_tax,
        "total_fees": total_fees,
        "rounds_detail": closed,  # 已清仓轮次明细，供报告展示
        "conclusion": conclusion,
    }
