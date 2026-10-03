#!/usr/bin/env python
"""盈亏归因：把**资金流水**（你实际做了什么）与**台账**（系统标记了什么）拼起来。

它只回答一个问题：

    我赚的钱，是系统挑出来的，还是我自己挑出来的？

为什么需要它：
    · `fupan-report` 只看资金流水，不知道系统当时有没有标记 → 算不出归因
    · `analyze_signal_ledger.py` 只看台账的前瞻收益 → 不知道你实际盈亏
    · `autofill_ledger_bought.py` 只做桥接（回填 bought），不做归因
    三者拼起来才能回答上面那句话。

分账口径（互相独立、不重不漏）：
    系统标记    —— 台账里该标的在「本轮买入窗口内」有标记行
    系统未标记  —— 台账里有该标的，但窗口内无标记（系统当时没信号，是你自己挑的）
    台账未覆盖  —— 台账里从没有该标的（早期数据，开始记录之前）

判据提醒：轮次级结论需要**百米量级**的样本（检测 +2pp 需约 149 轮）。
本脚本会在末尾按实际样本量给出可信度警告，不要无视它。

用法:
    uv run python scripts/attribute_pnl.py
    uv run python scripts/attribute_pnl.py --flow data/reports/历史资金流水_20260909_174245.xls
    uv run python scripts/attribute_pnl.py --window 10 --json /tmp/attr.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parent.parent

# ⚠️ 两个服务各有一个 config.py，**同名模块会互相污染**：
#    calc_indicators/config.py 有 TradingConfig（etf.py 需要）
#    fupan-report/config.py    有 OPTIONAL_FEE_COLUMNS（本脚本需要）
#    若把两个目录同时放进 sys.path，谁先谁后决定 `import config` 命中哪个，
#    而 etf.py 的 `from config import TradingConfig` 会直接 ImportError。
#    故：先加载 fupan 侧 → 清掉 `config` 缓存 → 换路径 → 再加载 calc 侧。
_FUPAN = str(PROJECT / "services" / "fupan-report")
_CALC = str(PROJECT / "services" / "calc_indicators")

sys.path.insert(0, _FUPAN)
from data_loader import load_flow                      # noqa: E402
from config import OPTIONAL_FEE_COLUMNS               # noqa: E402

sys.modules.pop("config", None)                        # 关键：清掉同名缓存
sys.path.remove(_FUPAN)
sys.path.insert(0, _CALC)

# 禁止静默降级！曾因 try/except 吞掉 ImportError，导致 is_etf 恒为 False、
# **把所有 ETF 都标成个股**，报告看着正常但是错的（2026-10-01 实际发生过）。
try:
    from etf import is_etf                            # noqa: E402
except Exception as _e:                               # pragma: no cover
    print(f"❌ ETF 识别模块加载失败：{type(_e).__name__}: {_e}")
    print("   → ETF 与个股无法区分，归因结果会是错的，故拒绝运行。")
    raise SystemExit(3)

# 与 analyzers.py 完全一致的口径（招商证券）：买=数量>0，卖=数量<0
_BUY = lambda df: df["成交数量"] > 0                    # noqa: E731
_SELL = lambda df: df["成交数量"] < 0                   # noqa: E731

DEFAULT_LEDGER = PROJECT / "data" / "observations" / "signal_ledger.csv"

# 检测 +2pp / +1pp 所需轮次（单笔 10 日 SD=8.72pp，80% 功效 / 5% 双尾）
N_FOR_2PP, N_FOR_1PP = 149, 597


# ====================================================================
# 轮次重建（按证券代码聚合，并保留买入日期窗口 —— 台账匹配需要）
# ====================================================================
def load_rounds(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for code, g in df.groupby("证券代码"):
        g = g.sort_values("成交日期", kind="stable")
        buys, sells = g[_BUY(g)], g[_SELL(g)]
        if buys.empty:
            continue                                   # 无买入（只卖/只分红），不构成轮次
        buy_cost = -float(buys["发生金额"].sum())
        sell_income = float(sells["发生金额"].sum())
        fees = float(g["佣金"].sum() + g["印花税"].sum()
                     + sum(g[c].sum() for c in OPTIONAL_FEE_COLUMNS))
        rows.append({
            "code": str(code).zfill(6),
            "name": str(g["证券名称"].iloc[-1]),
            "is_etf": bool(is_etf(str(code).zfill(6))),
            "cleared": bool((g["剩余数量"] == 0).any()),
            "buy_cost": buy_cost,
            "sell_income": sell_income,
            "pnl": sell_income - buy_cost,             # 扣费前
            "fees": fees,
            "pnl_net": sell_income - buy_cost - fees,  # 扣费后
            "first_buy": buys["成交日期"].min(),
            "last_buy": buys["成交日期"].max(),
            "last_sell": sells["成交日期"].max() if not sells.empty else pd.NaT,
            "trade_count": int(len(g)),
        })
    return pd.DataFrame(rows)


def classify(rounds: pd.DataFrame, ledger: pd.DataFrame,
             window_days: int) -> pd.DataFrame:
    """给每个轮次打上 系统标记 / 系统未标记 / 台账未覆盖。"""
    rounds = rounds.copy()
    rounds["source"] = "台账未覆盖"
    if ledger.empty:
        return rounds
    led = ledger.copy()
    led["date"] = pd.to_datetime(led["date"], errors="coerce")
    led["symbol"] = led["symbol"].astype(str).str.zfill(6)
    by_sym = {s: g for s, g in led.groupby("symbol")}
    led_min, led_max = led["date"].min(), led["date"].max()

    for i, r in rounds.iterrows():
        lo = r["first_buy"] - pd.Timedelta(days=window_days)
        hi = r["last_sell"] if pd.notna(r["last_sell"]) else r["last_buy"]
        # ⚠️ 关键：台账若在「本轮窗口」内根本没在记录，就不能判成"系统未标记"。
        #    "系统看过但没标记" 与 "系统当时没在看" 是完全不同的结论，
        #    混为一谈会把"我自己挑的"错误地统计进去。
        if pd.isna(led_min) or hi < led_min or lo > led_max:
            continue                                   # 保持 台账未覆盖
        g = by_sym.get(r["code"])
        if g is None:
            continue                                   # 台账覆盖了该时段，但这只从没进过台账
        hit = g[(g["date"] >= lo) & (g["date"] <= hi)]
        rounds.at[i, "source"] = "系统标记" if not hit.empty else "系统未标记"
    return rounds


# ====================================================================
# 报告
# ====================================================================
def _split_table(r: pd.DataFrame, title: str) -> None:
    print(f"\n【{title}】")
    if r.empty:
        print("  无数据")
        return
    print(f"  {'类别':<12s} {'轮次':>5s} {'已清仓':>6s} {'扣费前':>13s} {'扣费后':>13s} "
          f"{'胜率':>6s} {'平均/轮':>11s}")
    print("  " + "-" * 76)
    for lab, g in r.groupby("source", sort=False):
        cl = g[g["cleared"]]
        wr = (cl["pnl_net"] > 0).mean() * 100 if len(cl) else float("nan")
        avg = g["pnl_net"].mean() if len(g) else float("nan")
        print(f"  {lab:<12s} {len(g):>5d} {len(cl):>6d} {g['pnl'].sum():>+13,.0f} "
              f"{g['pnl_net'].sum():>+13,.0f} {wr:>5.1f}% {avg:>+11,.0f}")


def report(rounds: pd.DataFrame, ledger: pd.DataFrame, window: int) -> dict:
    print("=" * 80)
    print("📊 盈亏归因：系统 vs 你的判断")
    print("=" * 80)

    n_led = len(ledger)
    n_sym_led = ledger["symbol"].nunique() if n_led else 0
    print(f"\n台账: {n_led} 行 / {n_sym_led} 只")
    if n_led == 0:
        print("  ⚠️  台账为空 —— 所有轮次都会落进「台账未覆盖」，归因无意义。")
        print("     先跑 log_signal_ledger.py 积累记录，再回来跑本脚本。")
    # 台账覆盖的日期范围
    if n_led:
        d = pd.to_datetime(ledger["date"], errors="coerce")
        print(f"  台账覆盖: {d.min():%Y-%m-%d} ~ {d.max():%Y-%m-%d}")
    print(f"流水轮次: {len(rounds)} 轮（{rounds['code'].nunique()} 只标的）")

    # —— 一、总览 ——
    _split_table(rounds, "一、按来源分账（这是本脚本的核心）")

    # —— 二、ETF vs 个股 × 来源 ——
    print("\n【二、ETF vs 个股（你的账本里这个差异最悬殊）】")
    for lab, g in rounds.groupby("is_etf"):
        kind = "ETF" if lab else "个股"
        cl = g[g["cleared"]]
        wr = (cl["pnl_net"] > 0).mean() * 100 if len(cl) else float("nan")
        print(f"  {kind:<4s} {len(g):>3d} 轮  扣费前 {g['pnl'].sum():>+11,.0f}  "
              f"扣费后 {g['pnl_net'].sum():>+11,.0f}  胜率 {wr:>5.1f}%")

    print("\n  ETF × 来源:")
    for lab, g in rounds[rounds["is_etf"]].groupby("source", sort=False):
        print(f"    {lab:<10s} {len(g):>3d} 轮  扣费后 {g['pnl_net'].sum():>+11,.0f}")
    print("  个股 × 来源:")
    for lab, g in rounds[~rounds["is_etf"]].groupby("source", sort=False):
        print(f"    {lab:<10s} {len(g):>3d} 轮  扣费后 {g['pnl_net'].sum():>+11,.0f}")

    # —— 三、核心结论 ——
    print("\n【三、结论】")
    own = rounds[rounds["source"] == "系统未标记"]
    marked = rounds[rounds["source"] == "系统标记"]
    if not own.empty and not marked.empty:
        a, b = own["pnl_net"].sum(), marked["pnl_net"].sum()
        print(f"  系统标记过的轮次: {len(marked):>3d} 轮  扣费后 {b:>+12,.0f}")
        print(f"  系统未标记（你自己挑的）: {len(own):>3d} 轮  扣费后 {a:>+12,.0f}")
        if a > b:
            print(f"  ⇒ 你的自主选择贡献 {a:+,.0f}，**高于**系统标记部分（{b:+,.0f}）")
            print(f"     → edge 在你的判断，不在系统。系统该降级为检查清单。")
        else:
            print(f"  ⇒ 系统标记部分 {b:+,.0f} 高于你的自主选择（{a:+,.0f}）")
    elif own.empty and not marked.empty:
        print("  ⚠️  没有「系统未标记」的轮次 —— 说明你买的每只都被系统标记过。")
        print("     （也可能是台账覆盖不全，或你的自选就是系统清单）")
    elif marked.empty and not own.empty:
        print("  ⚠️  没有「系统标记」的轮次 —— 台账尚未覆盖你的交易日期。")
        print("     需要积累记录，或确认台账与流水的日期区间有重叠。")
    elif marked.empty and own.empty:
        print("  ⚠️  全部轮次都落在「台账未覆盖」—— 台账与流水的**日期区间没有重叠**。")
        print("     这不是「你没赚钱 / 系统没用」的结论，而是「现在还算不出归因」。")
        print("     原因通常是：台账从今天才开始记，而流水是过去的交易。")
        print()
        print("     ⇒ 现阶段能做的只有【二、ETF vs 个股】（它不需要台账）：")
        _e = rounds[rounds["is_etf"]]; _s = rounds[~rounds["is_etf"]]
        if not _e.empty and not _s.empty:
            print(f"        ETF  {len(_e):>3d} 轮  扣费后 {_e['pnl_net'].sum():>+11,.0f}  "
                  f"胜率 {(_e[_e['cleared']]['pnl_net']>0).mean()*100 if _e['cleared'].any() else float('nan'):.1f}%")
            print(f"        个股 {len(_s):>3d} 轮  扣费后 {_s['pnl_net'].sum():>+11,.0f}  "
                  f"胜率 {(_s[_s['cleared']]['pnl_net']>0).mean()*100 if _s['cleared'].any() else float('nan'):.1f}%")
            print("        ⇒ 这一栏已足够回答「edge 在哪条线上」（见 进化路径.md 阶段 2）")
        print()
        print("     ⇒ 归因要等：从现在起连续记录 1~3 个月后重跑本脚本。")

    # —— 四、系统标记但你没买 ——
    print("\n【四、系统标记但你没买的票】")
    if n_led and "bought" in ledger.columns:
        b = ledger["bought"].astype(str).str.strip().str.lower()
        un = ledger[b.isin(("0", "0.0", "false", ""))]
        print(f"  {len(un)} 行（{un['symbol'].nunique()} 只）")
        print("  这批只有「前瞻收益」，没有你的实际盈亏 —— 用另一个脚本看：")
        print("     uv run python scripts/analyze_signal_ledger.py --horizon 10")
        print("     → 里面「买 vs 没买」那一节回答『我的判断加了多少分』")
    else:
        print("  台账缺少 bought 列或为空 —— 先跑 autofill_ledger_bought.py")

    # —— 五、样本量警告 ——
    print("\n【五、可信度】")
    n_closed = int(rounds["cleared"].sum())
    if n_closed < 30:
        v = "❌ 样本太小，任何结论都不可信（连描述性统计都勉强）"
    elif n_closed < N_FOR_2PP:
        v = f"⚠️  不足以确认 +2pp 级差异（需约 {N_FOR_2PP} 轮已清仓）"
    else:
        v = f"✅ 已超过 +2pp 的检测门槛（{N_FOR_2PP} 轮）"
    print(f"  已清仓轮次: {n_closed}   （+2pp 需 ~{N_FOR_2PP} 轮，+1pp 需 ~{N_FOR_1PP} 轮）")
    print(f"  {v}")
    print("  提醒：本节只是**描述性**归因。要做因果结论，需预注册 + 样本外验证。")

    return {
        "n_ledger_rows": int(n_led),
        "n_rounds": int(len(rounds)),
        "n_closed": n_closed,
        "by_source": {
            lab: {"rounds": int(len(g)), "pnl_net": float(g["pnl_net"].sum())}
            for lab, g in rounds.groupby("source")
        },
        "etf_vs_stock": {
            ("ETF" if k else "个股"): {
                "rounds": int(len(g)), "pnl_net": float(g["pnl_net"].sum())}
            for k, g in rounds.groupby("is_etf")
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="盈亏归因：系统标记的 vs 你自己挑的",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--flow", default="", help="资金流水 xls（默认取 data/reports/ 下最新的）")
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="台账 CSV")
    ap.add_argument("--window", type=int, default=5,
                    help="买入窗口回看天数：台账标记日在 [首次买入-N, 末次卖出] 内即算命中（默认 5）")
    ap.add_argument("--json", default="", help="另存 JSON 结果")
    args = ap.parse_args()

    flow = args.flow
    if not flow:
        cands = sorted((PROJECT / "data" / "reports").glob("历史资金流水_*.xls*"))
        if not cands:
            print("❌ 找不到资金流水文件（data/reports/历史资金流水_*.xls*）")
            return 1
        flow = str(cands[-1])
    print(f"资金流水: {Path(flow).name}")

    raw, dropped = load_flow(flow)
    print(f"  清洗后 {len(raw)} 行（剔除现金理财 {dropped} 条）")

    rounds = load_rounds(raw)
    if rounds.empty:
        print("❌ 流水中没有可识别的买入轮次")
        return 1

    led_path = Path(args.ledger)
    if led_path.exists():
        ledger = pd.read_csv(led_path, dtype={"symbol": str})
    else:
        print(f"⚠️  台账不存在: {led_path}")
        ledger = pd.DataFrame(columns=["date", "symbol", "bought"])

    rounds = classify(rounds, ledger, args.window)
    result = report(rounds, ledger, args.window)

    if args.json:
        Path(args.json).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n📝 JSON 已保存: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
