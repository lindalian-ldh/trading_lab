#!/usr/bin/env python
"""生成台账观察清单 data/observations/watchlist.txt。

为什么需要它 / 为什么不能"从成交记录生成"：
    观察清单唯一的用途是给 `log_signal_ledger.py` 提供"系统每天评估哪些票"的名单。
    而分析器要回答「我的判断加了多少分」，靠的是**被标记但你没买**的那批当对照。
    若清单只由你的成交记录生成，对照就系统性失效了 —— 清单上每只都是你买过的，
    "没买"只代表"某天没买"，不代表"我看过但放弃"。
    （2026-10-01 第一版清单就是这个毛病：20 只全部来自成交记录，真对照 = 0。）

    故本生成器按**三段结构**产出：
        ① 持仓         —— 必须盯
        ② 近期成交     —— 保持连续性
        ③ 候选宇宙     —— **唯一真正的对照组**，来自 bankuai 热门板块的人气股，
                          与你是否交易过无关
        ④ 手工保留     —— 清单里原有、但不在 ①②③ 的代码（不静默丢弃）

用法:
    # 预览（不写盘）
    uv run python scripts/build_watchlist.py --target 50 --dry-run
    # 生成
    uv run python scripts/build_watchlist.py --target 50
    # 指定数据源
    uv run python scripts/build_watchlist.py --target 50 \\
        --flow data/reports/历史资金流水_20260909_174245.xls \\
        --bankuai-date 2026-09-30
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT = Path(__file__).resolve().parent.parent
_FUPAN = str(PROJECT / "services" / "fupan-report")
sys.path.insert(0, _FUPAN)
from data_loader import load_flow                      # noqa: E402

WATCHLIST = PROJECT / "data" / "observations" / "watchlist.txt"
BANKUAI_RAW = PROJECT / "data" / "raw" / "bankuai"
BANKUAI_REPORT = PROJECT / "data" / "reports" / "bankuai"

_BUY = lambda df: df["成交数量"] > 0                    # noqa: E731


# ====================================================================
# 数据源
# ====================================================================
def parse_existing(path: Path) -> tuple[list[str], dict[str, str]]:
    """读旧清单 → (代码顺序, {代码: 注释})。"""
    codes, notes = [], {}
    if not path.exists():
        return codes, notes
    for line in path.read_text(encoding="utf-8").splitlines():
        body = line.split("#", 1)[0].strip()
        if not body:
            continue
        code = body.split()[0]
        note = line.split("#", 1)[1].strip() if "#" in line else ""
        if code not in notes:
            codes.append(code)
        notes[code] = note
    return codes, notes


def from_flow(flow: str) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """返回 (持仓, 近期成交)。每项 = (code, name, extra)。"""
    df, _ = load_flow(flow)
    df = df.copy()
    df["证券代码"] = df["证券代码"].astype(str).str.zfill(6)
    holdings, traded = [], []
    for code, g in df.groupby("证券代码"):
        g = g.sort_values("成交日期", kind="stable")
        name = str(g["证券名称"].iloc[-1])
        last_qty = float(g["剩余数量"].iloc[-1])
        n = int(len(g))
        if last_qty > 0:
            holdings.append((code, name, f"持仓 {last_qty:,.0f} 股"))
        elif n > 0:
            traded.append((code, name, f"成交 {n} 笔"))
    holdings.sort(key=lambda x: -abs(float(x[2].split()[1].replace(",", ""))))
    traded.sort(key=lambda x: -int(x[2].split()[1]))
    return holdings, traded


def from_bankuai(date: str, sectors_wanted: int = 17, per_sector: int = 4,
                 exclude_st: bool = True) -> list[tuple[str, str, str]]:
    """从 bankuai 原始缓存取候选宇宙：跨板块**轮询**取名次最好的成分股。

    轮询而非"某板块取满"，是为了板块多样性 —— 否则候选池会被单一最热板块占满。
    """
    root = BANKUAI_RAW / date
    if not root.exists():
        cands = sorted(p.name for p in BANKUAI_RAW.glob("*") if p.is_dir())
        raise FileNotFoundError(
            f"没有 {date} 的 bankuai 缓存（现有: {cands[-5:] if cands else '无'}）\n"
            f"  先跑：uv run python services/bankuai-service/main.py --date {date}")

    # 板块热序：优先用报告里的 hot_sectors 顺序，缺失则按文件名
    order: list[str] = []
    rp = sorted(BANKUAI_REPORT.glob(f"bankuai_{date}_*.json"))
    if rp:
        rep = json.loads(rp[-1].read_text(encoding="utf-8"))
        order = [str(s.get("code", "")) for s in (rep.get("hot_sectors") or [])]
        name_of = {str(s.get("code", "")): s.get("name", "") for s in
                   (rep.get("hot_sectors") or [])}
    else:
        name_of = {}
    # 补全板块名：hot_sectors 通常只有前几名，其余板块名要从 rank 缓存取，
    # 否则候选行会显示成 [885846 #1] 这种光秃秃的代码。
    pr = root / "plates_rank_15.json"
    if pr.exists():
        try:
            pd_ = json.loads(pr.read_text(encoding="utf-8"))
            items = pd_ if isinstance(pd_, list) else (pd_.get("data") or [])
            for it in items:
                c = str(it.get("plate_code", ""))
                if c and c not in name_of:
                    name_of[c] = it.get("plate_name", "") or ""
        except Exception:
            pass

    files = {p.name.replace("stocks_15_", "").replace(".json", ""): p
             for p in root.glob("stocks_15_*.json")}
    ordered = [c for c in order if c in files] + [c for c in files if c not in order]
    ordered = ordered[:sectors_wanted]

    per: dict[str, list[dict]] = {}
    for c in ordered:
        try:
            d = json.loads(files[c].read_text(encoding="utf-8"))
        except Exception:
            continue
        items = d if isinstance(d, list) else (d.get("data") or [])
        items = [x for x in items if isinstance(x, dict) and x.get("stock_code")]
        items.sort(key=lambda x: x.get("rank") if isinstance(x.get("rank"), (int, float)) else 9e9)
        per[c] = items

    out, seen, sn = [], set(), name_of
    for pos in range(per_sector):                       # 轮询：各板块第 pos 名
        for c in ordered:
            lst = per.get(c) or []
            if pos >= len(lst):
                continue
            it = lst[pos]
            code = str(it["stock_code"]).zfill(6)
            nm = str(it.get("stock_name", ""))
            if code in seen:
                continue
            if exclude_st and ("ST" in nm.upper() or "退" in nm):
                continue
            seen.add(code)
            sec = sn.get(c) or c
            out.append((code, nm, f"[{sec} #{pos + 1}]"))
    return out


# ====================================================================
# 渲染
# ====================================================================
def render(target: int, holdings, traded, cands, keep, notes, meta) -> str:
    L = []
    L.append("# 台账观察清单 —— 由 scripts/build_watchlist.py 生成")
    L.append(f"# 生成时间: {meta['now']}    数据源: {meta['src']}")
    L.append("#")
    L.append("# 结构（三段，缺一不可）:")
    L.append("#   ① 持仓        —— 必须盯")
    L.append("#   ② 近期成交    —— 保持连续性")
    L.append("#   ③ 候选宇宙    —— **唯一真正的对照组**：你没交易过、但市场在关注的票")
    L.append("#                   没有它，分析器里的『买 vs 没买』就回答不了『我的判断加多少分』")
    L.append("# 可手动编辑：生成器会保留你手写的注释，也不丢弃你加进来的代码（归入 ④）")
    L.append("")

    def emit(title: str, items: list[tuple[str, str, str]]) -> None:
        if not items:
            return
        L.append(f"# ── {title}（{len(items)} 只）" + "─" * 20)
        for code, name, extra in items:
            old = notes.get(code, "")
            note = f"{name}  {extra}".strip()
            # 判断旧注释是否为**生成痕迹**（而非用户手写）：
            # 旧格式形如「名称  (近期成交 N 笔)」「名称  持仓 N 股」「名称 [板块 #N]」，
            # 去掉名称前缀后，剩下若为空或匹配生成模式，就不该当作用户备注保留。
            if old:
                rest = old[len(name):].strip() if old.startswith(name) else old
                generated = (not rest) or rest.startswith(
                    ("(", "（", "持仓", "成交", "["))
                if not generated:
                    note = f"{note}  ｜ 你的备注: {old}"
            L.append(f"{code:<8s} # {note}")
        L.append("")

    # 预算分配：持仓 > 候选宇宙 > 近期成交（持仓必须有；候选是对照组核心）
    h = holdings
    n_cand = max(0, min(len(cands), target - len(h) - max(0, min(len(traded), 5))))
    n_trade = max(0, min(len(traded), target - len(h) - n_cand))
    c = cands[:n_cand]
    t = traded[:n_trade]
    k = keep[: max(0, target - len(h) - len(t) - len(c))]

    emit("① 持仓", h)
    emit("② 近期成交", t)
    emit("③ 候选宇宙 —— 对照组（你没交易过、但市场在关注）", c)
    emit("④ 手工保留（原有清单中未覆盖的）", k)
    if len(keep) > len(k):
        L.append(f"# ⚠️ 因 target={target} 被截掉的手工代码: "
                 f"{', '.join(x[0] for x in keep[len(k):])}")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="生成台账观察清单（三段结构，含真正的对照组）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", type=int, default=50, help="目标只数（默认 50）")
    ap.add_argument("--flow", default="", help="资金流水 xls（默认取最新）")
    ap.add_argument("--bankuai-date", default="", help="bankuai 缓存日期（默认取最新）")
    ap.add_argument("--per-sector", type=int, default=4, help="每个板块轮询取前 N 名（默认 4）")
    ap.add_argument("--include-st", action="store_true", help="候选池包含 ST / 退市风险股")
    ap.add_argument("--out", default=str(WATCHLIST), help="输出路径")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写盘")
    args = ap.parse_args()

    out = Path(args.out)
    old_codes, notes = parse_existing(out)

    flow = args.flow or ""
    if not flow:
        c = sorted((PROJECT / "data" / "reports").glob("历史资金流水_*.xls*"))
        if not c:
            print("❌ 找不到资金流水文件")
            return 1
        flow = str(c[-1])
    holdings, traded = from_flow(flow)

    bdate = args.bankuai_date
    if not bdate:
        dirs = sorted(p.name for p in BANKUAI_RAW.glob("*") if p.is_dir())
        if not dirs:
            print("❌ data/raw/bankuai/ 下没有缓存 —— 先跑 bankuai 扫描")
            return 1
        bdate = dirs[-1]
    # 候选池要够填目标：按需放大 per_sector（每板块轮询名次数），
    # 否则 --target 50 会因候选只有 40 来只而填不满，对照组白白变小。
    need = max(1, args.target - len(holdings) - min(len(traded), 5))
    per = args.per_sector
    cands: list = []
    for _ in range(4):                                  # 最多放大 4 轮
        cands = from_bankuai(bdate, per_sector=per,
                             exclude_st=not args.include_st)
        if len(cands) >= need:
            break
        per *= 2
    try:
        pass
    except FileNotFoundError as _e:                      # 保持原有报错路径
        print(f"❌ {_e}")
        return 1
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return 1

    used = {x[0] for x in holdings} | {x[0] for x in traded} | {x[0] for x in cands}
    keep = [(c, notes.get(c, "").split()[0] if notes.get(c) else "", "手工保留")
            for c in old_codes if c not in used]

    text = render(args.target, holdings, traded, cands, keep, notes, {
        "now": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "src": f"流水 {Path(flow).name} + bankuai {bdate}",
    })

    n_lines = len([l for l in text.splitlines() if l.strip() and not l.startswith("#")])
    print(f"资金流水: {Path(flow).name}")
    print(f"bankuai : {bdate}  （候选宇宙可用 {len(cands)} 只）")
    print(f" ① 持仓       {len(holdings):>3d} 只")
    print(f" ② 近期成交   {len(traded):>3d} 只")
    print(f" ③ 候选宇宙   可用 {len(cands):>3d} 只（轮询 {per}/板块）  ← 对照组")
    print(f" ④ 手工保留   {len(keep):>3d} 只")
    print(f" → 目标 {args.target} 只，实际写入 {n_lines} 只")
    if n_lines < 15:
        print(" ⚠️  不足 15 只：同日对照会不够，分析器多数分组会被丢弃")

    if args.dry_run:
        print("\n（--dry-run 预览，未写盘）\n" + "=" * 60)
        print(text)
        return 0

    if out.exists():
        shutil.copy2(out, out.with_suffix(out.suffix + ".bak"))
        print(f" 已备份旧清单 → {out.name}.bak")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f" ✅ 已写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
