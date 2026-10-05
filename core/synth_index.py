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
from pathlib import Path
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


#: 合成指数在代码体系里的前缀：``synth:稀土`` ⇒ ``data/cache/synth_稀土_history.csv``
SYNTH_PREFIX = "synth:"


def is_synth_code(code) -> bool:
    return str(code or "").startswith(SYNTH_PREFIX)


def synth_name(code) -> str:
    return str(code)[len(SYNTH_PREFIX):].strip()


def synth_cache_path(name: str, cache_dir=None) -> Path:
    root = Path(cache_dir) if cache_dir is not None else _default_cache_dir()
    return root / f"synth_{name}_history.csv"


def _default_cache_dir() -> Path:
    from config.settings import PROJECT_ROOT
    return Path(PROJECT_ROOT) / "data" / "cache"


def load_synth(code, *, cache_dir=None, bars=None) -> Optional[pd.DataFrame]:
    """读合成指数缓存（``synth:名称``）。文件不存在或损坏返回 None 并打 WARNING。"""
    import logging as _logging
    name = synth_name(code)
    path = synth_cache_path(name, cache_dir=cache_dir)
    if not path.exists():
        _logging.getLogger(__name__).warning(
            "合成指数缓存不存在: %s（先用 scripts/build_synth_index.py --save 生成）", path)
        return None
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
    except Exception as e:                                  # pragma: no cover
        _logging.getLogger(__name__).warning("合成指数读取失败: %s → %s", path, e)
        return None
    if df is None or df.empty or "date" not in df.columns or "close" not in df.columns:
        _logging.getLogger(__name__).warning("合成指数内容不合法: %s", path)
        return None
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df.get(c, df["close"]), errors="coerce")
        if c != "close":
            df[c] = df[c].fillna(df["close"])               # 老文件缺 OHLC 时用收盘兜底
    df = df.dropna(subset=["date", "close"]).sort_values("date")
    df = df.drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)
    if bars:
        df = df.tail(int(bars)).reset_index(drop=True)
    return df


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
    """由 ``{腾讯代码: 收盘价 Series}`` 合成指数。

    只是 :func:`build_index_from_ohlc` 的薄包装（``open=high=low=close``）。
    ⚠️ 真要喂给 L1（摆动点用 high/low）时**请用** :func:`build_index_from_ohlc`，
    否则摆动点会退化成"收盘价极值"。
    """
    frames = {}
    for code, ser in (prices or {}).items():
        if ser is None or len(ser) == 0:
            continue
        x = pd.Series(ser).copy()
        x.index = pd.to_datetime(x.index)
        x = pd.to_numeric(x, errors="coerce")
        x = x[~x.index.duplicated(keep="last")].sort_index()
        frames[code] = pd.DataFrame({"date": x.index, "open": x.to_numpy(),
                                     "high": x.to_numpy(), "low": x.to_numpy(),
                                     "close": x.to_numpy()})
    return build_index_from_ohlc(frames, constituents, start=start,
                                 min_members=min_members, base=base)


def build_index_from_ohlc(frames: dict, constituents: pd.DataFrame,
                          *, start: str = SYNTH_START,
                          min_members: int = MIN_MEMBERS,
                          base: float = BASE_LEVEL) -> pd.DataFrame:
    """由**个股 OHLC** 合成带 OHLC 的等权指数（**不是**拿 close 假装高开低）。

    为什么必须真合成 OHLC：L1 的摆动点用 ``high`` / ``low`` 判极值
    （P0.5 冻结基准 §①）；只给 close 会让摆动点退化成"收盘价极值"，
    与 ETF 上的判定口径不一致。

    口径（与前一根**收盘**比，权重与收益口径一致，均为当日有效成分股的算术平均）：

    ```
    close_t = level_{t-1} × mean( close_it / close_i,t-1 )
    open_t  = level_{t-1} × mean( open_it  / close_i,t-1 )
    high_t  = level_{t-1} × mean( high_it  / close_i,t-1 )
    low_t   = level_{t-1} × mean( low_it   / close_i,t-1 )
    ```
    ``level`` 由 ``close`` 链式累乘得到 ⇒ ``close`` 列与 :func:`build_index` 完全一致。

    Args:
        frames: ``{腾讯代码: DataFrame(date/open/high/low/close)}``。
    """
    cols = ("open", "high", "low", "close")
    ratios, close_px, valid_src = {}, {}, {}
    for code, df in (frames or {}).items():
        if df is None or len(df) == 0:
            continue
        d = df[["date", *[c for c in cols if c in df.columns]]].copy()
        d["date"] = pd.to_datetime(d["date"], errors="coerce")
        for c in cols:
            d[c] = pd.to_numeric(d.get(c), errors="coerce")
        if "close" not in d.columns:
            continue
        d = d.dropna(subset=["date", "close"]).drop_duplicates(subset=["date"], keep="last")
        d = d.sort_values("date").set_index("date")
        prev = d["close"].shift(1)
        close_px[code] = d["close"]
        valid_src[code] = (prev.notna() & (prev > 0))
        for c in cols:
            ratios[(code, c)] = (d[c] / prev) if c in d.columns else pd.Series(np.nan, index=d.index)
    if not close_px:
        return pd.DataFrame(columns=["date", *cols, "ret", "n_members", "n_missing"])

    px = pd.DataFrame(close_px).sort_index()
    px = px[px.index >= pd.Timestamp(start)]
    if px.empty:
        return pd.DataFrame(columns=["date", *cols, "ret", "n_members", "n_missing"])
    memb = membership_table(constituents, px.index, codes=list(px.columns))
    ok = pd.DataFrame({c: valid_src[c].reindex(px.index).fillna(False).to_numpy(bool)
                       for c in px.columns}, index=px.index)
    valid = px.notna() & memb & ok
    n = valid.sum(axis=1)

    def _mean_ratio(field: str) -> pd.Series:
        r = pd.DataFrame({c: ratios[(c, field)].reindex(px.index) for c in px.columns})
        return r.where(valid).mean(axis=1)

    r_close = _mean_ratio("close")
    idx_ret = r_close - 1.0
    idx_ret = idx_ret.where(n >= int(min_members))
    level = base * (1.0 + idx_ret.fillna(0.0)).cumprod()
    level = level.where(n >= int(min_members))      # 成分股不足 ⇒ NaN，不外推
    prev_level = level.shift(1)
    prev_level.iloc[0] = base
    out = {"date": px.index, "close": level}
    for c in ("open", "high", "low"):
        out[c] = (prev_level * _mean_ratio(c)).where(n >= int(min_members))
    out["ret"] = idx_ret
    out["n_members"] = n
    out["n_missing"] = memb.sum(axis=1) - n
    res = pd.DataFrame(out).reset_index(drop=True)
    if res["close"].notna().any():
        first = res["close"].first_valid_index()
        for c in ("open", "high", "low"):
            res.loc[first, c] = res.loc[first, "close"]     # 第一根没有前收，用收盘兜底
    return res[["date", "open", "high", "low", "close", "ret", "n_members", "n_missing"]]


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
    "SYNTH_PREFIX", "is_synth_code", "synth_name", "synth_cache_path", "load_synth",
    "to_tx_codes", "membership_table", "build_index", "build_index_from_prices",
    "build_index_from_ohlc", "correlation",
]
