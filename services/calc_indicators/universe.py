#!/usr/bin/env python3
"""股票池构建：按**等距抽样**选出可复现的回测样本（含已退市股票）。

为什么需要这一层（P0 报告里最致命的偏差）：

    之前 31 只样本是手动挑的知名股（茅台、平安、宁德…），那是**生存者偏差的极端形式**。
    实测 baostock 的股票池里，2016-01-01 前上市的真股票共 **2899 只，其中 321 只（11.1%）已退市**。
    手动挑样本等于把这 11.1% 全部丢掉 —— 而它们恰恰是"逆势抄底"最可能归零的那批。

本模块的选样规则（确定性、可复现）：

    1. 取 baostock 全量股票清单（query_stock_basic），筛 `type=1`（股票）
    2. 代码前缀限定真股票段：600/601/603/605/000/001/002/003/300/301/688/689
       （type=1 里混有 ETF/LOF，靠前缀剔除）
    3. 取**在回测区间内真实存在过**的标的：
       `ipo_date <= start` **且**（仍在市 **或** `out_date >= start`）
       —— 必须排除"回测开始前就已退市"的（如退市长油 2014 退市、TCL通讯 2004 退市），
          它们在整个回测区间没有任何数据，选进来只会浪费请求；
          而"区间内退市"的必须保留（这正是消除生存者偏差的关键）
    4. 按代码排序，等距抽样 N 只（步长 = len//N，带固定 offset 保证可复现）

关键设计：
    - **区间内退市者必须保留**（`status=0` 且 `out_date >= start`）—— 本模块存在的意义。
      实测股票池里 2016 前上市的真股票 2899 只、321 只已退市（11.1%），
      手动挑样本等于把这 11.1% 全丢掉，而那恰是"逆势抄底"最可能归零的一批。
    - 退市股数据只到退市日，回测里它会在退市日"消失"，自然产生"买入后停牌/归零"的样本
    - 结果写入 JSON 缓存，避免每次重拉清单（并让样本可审计）
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# 真股票代码段（用于剔除 type=1 里混入的 ETF/LOF）
STOCK_PREFIXES = ("600", "601", "603", "605", "000", "001", "002", "003",
                  "300", "301", "688", "689")

UNIVERSE_CACHE = "data/cache/calc_indicators/universe.json"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _cache_path() -> Path:
    p = _project_root() / UNIVERSE_CACHE
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def is_real_stock(code: str) -> bool:
    """baostock 的 type=1 里混有 ETF/LOF，用代码段过滤出真股票。"""
    num = code.split(".")[-1]
    return num.startswith(STOCK_PREFIXES)


def fetch_stock_list() -> pd.DataFrame:
    """从 baostock 拉全量股票清单（含已退市）。

    Returns:
        DataFrame[code, name, ipo_date, out_date, status, listed]
    """
    from market_filter import _ensure_bs_login
    import baostock as bs

    if not _ensure_bs_login():
        raise RuntimeError("baostock 登录失败，无法获取股票清单")
    rs = bs.query_stock_basic()
    if rs.error_code != "0":
        raise RuntimeError(f"query_stock_basic 失败: {rs.error_code} {rs.error_msg}")
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        raise RuntimeError("query_stock_basic 返回空")
    df = pd.DataFrame(rows, columns=rs.fields)
    df = df[df["type"] == "1"].copy()
    df = df[df["code"].apply(is_real_stock)].copy()
    df["ipo_date"] = pd.to_datetime(df["ipoDate"], errors="coerce")
    df["out_date"] = pd.to_datetime(df["outDate"], errors="coerce")
    df["status"] = df["status"].astype(str)
    df["listed"] = df["status"] == "1"
    df = df.rename(columns={"code_name": "name"})
    return df[["code", "name", "ipo_date", "out_date", "status", "listed"]]


def _to_symbol(code: str) -> str:
    """'sh.600519' → '600519'。"""
    return code.split(".")[-1]


def sample_universe(start: str = "2016-01-01", n: int = 300,
                    offset: int = 0, seed: int = 0) -> dict:
    """等距抽样出 n 只标的（含已退市），返回可审计的样本字典。

    Args:
        start: 回测起始日；只取该日之前已上市的标的
        n: 目标样本数
        offset: 等距抽样的相位（0 ≤ offset < 步长），用于换一批样本做稳健性检验
        seed: 仅用于记录（抽样本身是确定性的，不依赖随机数）

    Returns:
        dict: {'start','n','offset','seed','universe_size','delisted_in_universe',
               'picked','picked_delisted','step','generated_at','symbols':[...], 'detail':[...]}
    """
    df = fetch_stock_list()
    start_ts = pd.Timestamp(start)
    # 区间内存在过：上市不晚于 start，且（仍在市 或 退市不早于 start）
    elig = df[(df["ipo_date"] <= start_ts) &
              (df["listed"] | (df["out_date"] >= start_ts))].copy()
    elig = elig.sort_values("code").reset_index(drop=True)

    # 审计：被排除的"区间前已退市"数量（便于人工核对选样口径）
    pre_delisted = df[(df["ipo_date"] <= start_ts) & (~df["listed"]) &
                      (df["out_date"] < start_ts)]

    total = len(elig)
    if total == 0:
        raise RuntimeError(f"{start} 之前无已上市标的")
    if n >= total:
        picked = elig
        step = 1
    else:
        step = total // n
        idx = [min(offset + i * step, total - 1) for i in range(n)]
        picked = elig.iloc[sorted(set(idx))].reset_index(drop=True)

    symbols = [_to_symbol(c) for c in picked["code"]]
    detail = [
        {"symbol": _to_symbol(r["code"]), "code": r["code"], "name": r["name"],
         "ipo_date": None if pd.isna(r["ipo_date"]) else r["ipo_date"].strftime("%Y-%m-%d"),
         "out_date": None if pd.isna(r["out_date"]) else r["out_date"].strftime("%Y-%m-%d"),
         "listed": bool(r["listed"])}
        for _, r in picked.iterrows()
    ]
    return {
        "start": start, "n": n, "offset": offset, "seed": seed,
        "universe_size": int(total),
        "delisted_in_universe": int((~elig["listed"]).sum()),
        "pre_start_delisted_excluded": int(len(pre_delisted)),
        "picked": int(len(picked)),
        "picked_delisted": int((~picked["listed"]).sum()),
        "step": int(step),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "symbols": symbols,
        "detail": detail,
    }


def save_universe(sample: dict) -> Path:
    p = _cache_path()
    p.write_text(json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_universe() -> Optional[dict]:
    p = _cache_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_or_build(start: str = "2016-01-01", n: int = 300, offset: int = 0,
                  refresh: bool = False) -> dict:
    """优先读缓存；缓存参数不符或 refresh=True 时重建并落盘。"""
    cached = load_universe()
    if (not refresh and cached and cached.get("start") == start
            and cached.get("n") == n and cached.get("offset") == offset):
        return cached
    sample = sample_universe(start=start, n=n, offset=offset)
    save_universe(sample)
    return sample


def print_universe_summary(sample: dict, show_head: int = 8) -> None:
    print(f"  股票池: {sample['start']} 起仍在市的标的 {sample['universe_size']} 只"
          f"（含区间内退市 {sample['delisted_in_universe']} 只，"
          f"{sample['delisted_in_universe'] / max(1, sample['universe_size']) * 100:.1f}%）")
    if sample.get("pre_start_delisted_excluded"):
        print(f"  已排除「回测开始前就已退市」: {sample['pre_start_delisted_excluded']} 只"
              f"（区间内无数据，选进来只会浪费请求）")
    print(f"  等距抽样: 步长={sample['step']} offset={sample['offset']} "
          f"→ 选中 {sample['picked']} 只（其中区间内退市 {sample['picked_delisted']} 只）")
    if sample.get("detail"):
        head = sample["detail"][:show_head]
        txt = "  ".join(f"{d['symbol']}{d['name']}" + ("" if d["listed"] else "(退市)")
                        for d in head)
        print(f"  样本示例: {txt} …")
