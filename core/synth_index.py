"""合成主题指数（P0.4c）—— 用成分股 + 个股日线拼出一条**长历史**主题指数。

## 为什么需要

交易所发布的行业/主题指数覆盖面有限：`电网设备`、`稀土` 在 sh/sz 里**根本没有**对应指数
（已用名称索引全表确认）。没有指数就没有"主题级择时"的价格锚。
ETF 自身历史又太短（稀土 ETF 只有 5.1~5.6 年）⇒ 唯一出路是**自己合成**。

## 构造口径（**必须写死，否则不同批次不可比**）

1. **等权、逐日再平衡**：每天的"指数收益" = 当日**有收益数据且是成分股**的股票收益的**算术平均**，
   再把日收益**链式相乘**成指数点位（基准 1000）。
   ⇒ 不需要股本/权重数据，也不受股价量纲影响；成分股进出天然被"当日平均"处理。
2. **时点还原（近似）**：成分股自 `time_in` **当日**起计入；`time_out` 字段 zzshare 不提供
   ⇒ **假设进入后一直留在成分股里**。这是**残余幸存者偏差**，越往前越严重，
   所以强制 `SYNTH_START = 2015-01-01` 起算（用户可以传更晚的日期）。
3. **当日缺数据的个股当天剔除**（不 forward-fill）：停牌/未上市/退市都会自然表现为"当天不在"。
4. **最少成分股数** `min_members`（默认 5）：不足则该日指数为 NaN（**不填 0、不外推**）。
5. **价格必须是前复权价**：否则送转会在个股上留下断崖，被平均进指数。
   腾讯源经 `core.marketdata_tx` 的按代码阈值修复后可用；baostock（`adjustflag="2"`）更干净。

## 诚实性

- 本模块产出的是**近似指数**，不等于任何官方指数；
- **必须**用对应的 ETF 做相关性验证（`correlation`），< 0.8 就按 kill criterion 5 降级为"只显示、不验证"；
- ⚠️ **不许按"与 ETF 相关性"来筛成分股** —— 那会让验证变成循环论证。
  筛选必须基于**行业归属**（人工判断），相关性只能当**事后体检**。
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable, Optional

import numpy as np
import pandas as pd

from core.marketdata_tx import MarketDataError, normalize_tx_code

logger = logging.getLogger(__name__)

#: 合成指数的起算日 —— 幸存者偏差控制（见模块 docstring 第 2 条）
SYNTH_START = "2015-01-01"
#: 最少成分股数（不足则该日指数为 NaN）
MIN_MEMBERS = 5
#: 指数基准点位
BASE_LEVEL = 1000.0
#: 验证门槛（kill criterion 5）
MIN_CORR = 0.8
#: 重叠不足此日数 ⇒ 相关性不可信（只作提示）
MIN_OVERLAP = 200


def to_tx_codes(codes: Iterable[str]) -> tuple:
    """把 6 位股票代码转成腾讯代码；**北交所等取不到的会被剔除并返回**。

    Returns:
        ``(kept, dropped)`` —— ``kept`` 为 ``[(tx_code, raw_code), …]``，``dropped`` 为原因列表。
    """
    kept, dropped = [], []
    for raw in codes:
        s = str(raw).strip().zfill(6)
        try:
            kept.append((normalize_tx_code(s, kind="equity"), s))
        except MarketDataError as e:
            dropped.append((s, str(e)))
    return tuple(kept), tuple(dropped)


def membership_table(constituents: pd.DataFrame, dates: pd.DatetimeIndex,
                     codes: Optional[list] = None) -> pd.DataFrame:
    """构造**逐日成分股矩阵**（True = 该股当日属于成分股）。

    Args:
        constituents: 需含 ``stock_code``（6 位）与 ``time_in``（``YYYY-MM-DD``）。
        dates: 指数日期轴。
        codes: 列的腾讯代码顺序；None 时按 ``constituents`` 里的顺序推断。
    """
    t = pd.to_datetime(dates)
    df = pd.DataFrame(False, index=pd.DatetimeIndex(t), columns=list(codes or []))
    for _, r in constituents.iterrows():
        raw = str(r["stock_code"]).strip().zfill(6)
        try:
            tx = normalize_tx_code(raw, kind="equity")
        except MarketDataError:
            continue
        if tx not in df.columns:
            continue
        t_in = pd.to_datetime(r.get("time_in"), errors="coerce")
        df.loc[(df.index >= t_in) if pd.notna(t_in) else slice(None), tx] = True
    return df


def build_index(returns: pd.DataFrame, constituents: pd.DataFrame,
                *, start: str = SYNTH_START, min_members: int = MIN_MEMBERS,
                base: float = BASE_LEVEL) -> pd.DataFrame:
    """由**日收益矩阵** + 成分股表合成等权指数。

    Args:
        returns: ``index = date``，``columns = 腾讯代码``，值为**日收益（小数，不是 %）**。
        constituents: 见 :func:`membership_table`。
        start: 起算日（默认 :data:`SYNTH_START`）。
        min_members: 最少成分股数。

    Returns:
        列 ``date / close / ret / n_members / n_missing``（升序）；成分股不足的日子 ``close`` 为 NaN。
    """
    if returns is None or returns.empty:
        return pd.DataFrame(columns=["date", "close", "ret", "n_members", "n_missing"])
    r = returns.copy()
    r.index = pd.to_datetime(r.index)
    r = r.sort_index()
    r = r[r.index >= pd.Timestamp(start)]
    if r.empty:
        return pd.DataFrame(columns=["date", "close", "ret", "n_members", "n_missing"])

    raw_codes = [c for c in r.columns]
    memb = membership_table(constituents, r.index, codes=raw_codes)
    valid = r.notna() & memb
    n = valid.sum(axis=1)

    idx_ret = r.where(valid).mean(axis=1)
    idx_ret = idx_ret.where(n >= int(min_members))
    level = base * (1.0 + idx_ret.fillna(0.0)).cumprod()
    level = level.where(n >= int(min_members))

    out = pd.DataFrame({
        "date": r.index,
        "close": level.to_numpy(),
        "ret": idx_ret.to_numpy(),
        "n_members": n.to_numpy(),
        # n_missing = 当日**属于成分股但没有收益数据**的只数（停牌/退市/未上市）
        "n_missing": (memb.sum(axis=1) - n).to_numpy(),
    })
    return out.reset_index(drop=True)


def build_index_from_prices(prices: dict, constituents: pd.DataFrame,
                            *, start: str = SYNTH_START,
                            min_members: int = MIN_MEMBERS,
                            base: float = BASE_LEVEL) -> pd.DataFrame:
    """由 ``{腾讯代码: 收盘价 Series}`` 合成指数（内部先算日收益）。

    **收盘价必须是前复权价**（见模块 docstring 第 5 条）。
    """
    if not prices:
        return pd.DataFrame(columns=["date", "close", "ret", "n_members", "n_missing"])
    series = {}
    for code, s in prices.items():
        if s is None or len(s) == 0:
            continue
        x = pd.Series(s).copy()
        x.index = pd.to_datetime(x.index)
        x = pd.to_numeric(x, errors="coerce")
        x = x[~x.index.duplicated(keep="last")].sort_index()
        series[code] = x
    if not series:
        return pd.DataFrame(columns=["date", "close", "ret", "n_members", "n_missing"])
    px = pd.DataFrame(series).sort_index()
    rets = px.pct_change()
    rets = rets.iloc[1:]                     # 第一根没有收益
    return build_index(rets, constituents, start=start, min_members=min_members, base=base)


def correlation(synth: pd.DataFrame, other: pd.DataFrame,
                tail: Optional[int] = None) -> tuple:
    """合成指数与 ETF（或任何价格序列）的日收益相关性。

    Returns:
        ``(corr, overlap_days)``；样本不足返回 ``(None, n)``。
    """
    def _ret(df):
        if df is None or len(df) == 0:
            return pd.Series(dtype="float64")
        d = df[["date", "close"]].copy()
        d["date"] = pd.to_datetime(d["date"])
        d["close"] = pd.to_numeric(d["close"], errors="coerce")
        d = d.dropna().drop_duplicates(subset=["date"], keep="last").sort_values("date")
        return d.set_index("date")["close"].astype(float).pct_change().dropna()

    a, b = _ret(synth), _ret(other)
    if a.empty or b.empty:
        return None, 0
    j = pd.concat([a.rename("s"), b.rename("e")], axis=1, sort=True).dropna()
    if tail:
        j = j.tail(int(tail))
    if len(j) < MIN_OVERLAP:
        return None, len(j)
    v = float(j["s"].corr(j["e"]))
    return (v if np.isfinite(v) else None), len(j)


__all__ = [
    "SYNTH_START", "MIN_MEMBERS", "BASE_LEVEL", "MIN_CORR", "MIN_OVERLAP",
    "to_tx_codes", "membership_table", "build_index", "build_index_from_prices",
    "correlation",
]
