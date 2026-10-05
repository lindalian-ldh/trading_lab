"""腾讯源行情数据层（指数 / ETF / 个股）—— 项目共享的取数底座。

## 为什么存在

1. **本项目已决定不使用东财**：`efinance` 与 akshare 的 `*_em` 系列走东财通道，
   实测在本机**反复 `ConnectionError: RemoteDisconnected`**（2026-10-03 复核），
   且历史上 efinance 对 `000688` 存在**静默取错标的**的隐患
   （`000688` 被解析成深市个股「国城矿业」而非科创50指数）。
2. **腾讯源可提供多年历史**：实测 `sh000001` 8726 根、`sz002371` 3939 根、
   `sh512480` 1774 根 —— 这是本仓库"主题级择时历史不足"的唯一出路
   （baostock 对 ETF 只有 181 根）。
3. **历史必须落盘**：否则每次回测都重新联网，既慢又不可复现。

## 设计约定（重要）

- **只走腾讯**：`ak.stock_zh_index_daily_tx`（指数）/ `ak.stock_zh_a_hist_tx`（ETF、个股）。
  **绝不回退到东财**；失败就是失败，由调用方决定降级。
- **缓存命名沿用仓库既有约定**：
      指数      → ``data/cache/index_{code}_tx_history.csv``
      ETF/个股  → ``data/cache/equity_{code}_tx_history.csv``
  ``index_sz399006_tx_history.csv`` 是本仓库既有的同类文件（此前无代码引用），
  本模块正式启用该命名。
  **注意**：``index_{code}_history.csv``（无 ``_tx_``）是 baostock 源的旧缓存，
  仍被 ``scripts/audit_regime_*.py`` 使用，**本模块不写入、不覆盖**，
  以免污染既有审计基线。
- **指数与个股走两个不同接口，且 kind 必须显式或可判定** ——
  **不做 6 位数字的猜测**：``000001`` 既可能是上证综指（sh）也可能是平安银行（sz）。
  无法判定时**抛异常**，而不是猜一个。
- **失败必须显式**：返回 ``None`` 并打 WARNING 说明原因；``strict=True`` 时抛
  ``MarketDataError``。绝不静默返回空 DataFrame，也绝不静默换源。
- **列语义诚实**：腾讯的**指数**接口只给 ``amount``（成交额），没有 ``volume``。
  本模块因此把 ``volume`` 留为 ``NaN`` 而**不填 0** ——
  填 0 会让下游把"没有数据"误当成"成交量为零"（本仓库明确反对这类静默失效）。
  需要成交额时请用 ``amount`` 列。
- **equity 默认前复权**（2026-10-04 P0.4 追加）：腾讯的 ETF/个股日线**不含复权**，
  份额折算会留下断崖（实测 512480 在 2021-03-29 单日 −48.90%），
  足以把两只同质半导体 ETF 的相关性从 0.990 压到 0.666。
  ``fetch_history(kind='equity')`` 默认 ``adjust=True`` 做前复权，并把检测到的事件
  打 WARNING + 挂到 ``df.attrs['corporate_actions']``；**指数不复权**。
  **缓存里始终存原始未复权价**，复权只在读出时进行。

## 用法

    from core.marketdata_tx import fetch_index_history, fetch_equity_history

    df = fetch_index_history("sh000001")             # 全历史（含缓存合并）
    df = fetch_index_history("sh000001", bars=250)   # 只取最近 250 根
    df = fetch_equity_history("sh512480")            # ETF
    df = fetch_equity_history("sz002371")            # 个股
    df = fetch_index_history("sh000300", prefer_cache=True)   # 离线快路径
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# 标准输出列顺序（volume/amount/turnover 缺失时置 NaN，不填 0）
OHLCV_COLUMNS: tuple = ("date", "open", "high", "low", "close", "volume", "amount", "turnover")

# 缓存根目录。测试可 monkeypatch 本模块级变量。
CACHE_DIR: Optional[Path] = None

# 腾讯源的两种 kind
KIND_INDEX = "index"
KIND_EQUITY = "equity"

# 已验证的指数代码登记表（用于 kind 自动判定；**必须带 sh/sz 前缀**）。
# 数据来源：2026-10-03 实测腾讯源均可取数。新增指数请先验证再登记。
INDEX_CODES: frozenset = frozenset({
    # —— 宽基 ——
    "sh000001",  # 上证综指
    "sz399001",  # 深证成指
    "sz399006",  # 创业板指
    "sh000300",  # 沪深300
    "sh000905",  # 中证500
    "sh000852",  # 中证1000
    "sh000016",  # 上证50
    "sz399005",  # 中小板指（中小100）
    "sh000688",  # 科创50
    # —— 主题/行业（本项目主题择时所需，实测均有 8 年以上历史）——
    "sz399363",  # 国证半导体芯片类
    "sh000827",  # 中证环保
    "sz399808",  # 中证新能
    "sz399997",  # 中证白酒
    "sh000998",  # 中证TMT
    "sz399989",  # 中证医疗
    "sz399976",  # 中证新能车
    "sh000819",  # 有色金属
    # —— 2026-10-04 追加（P0.5 修订记录 #11：半导体系主题换指数）——
    # 选它的规则**在跑数之前声明**：在 4 个已过 ≥10 年门槛的候选里，
    # 取「半导体系 5 只 ETF × 2 个窗口（全重叠 / 近 500 日）中最小相关性」最大者。
    # 实测 sh000039 的最小相关性 0.835（其余：sz399811 0.824 / sh000993 0.823 / sh000935 0.820）。
    # ⚠️ 它是**上证信息**（宽泛 IT 指数，语义上并不等于"半导体设备"），属**便宜的近似**；
    #    语义正确的做法仍是 P0.4c 合成指数。详见 scripts/增强方案v2-...md 的补充勘探一节。
    "sh000039",  # 上证信息 17.7 年
    # —— 2026-10-04 追加（观察项 WATCH_ONLY，见 core/theme_universe.py）——
    "sh000015",  # 上证红利 21.7 年
    "sh000937",  # 中证公用 17.2 年（电力 ETF 的代理）
    "sz399998",  # 中证煤炭 10.9 年
    "sz399986",  # 中证银行 10.9 年
    "sz399973",  # 中证国防 11.1 年（军工）
    "sz399441",  # 生物医药 11.7 年（创新药代理）
    "sz399283",  # 机器人50 6.6 年 ⚠️ 历史不足 10 年
    "sz399975",  # 证券公司 10.9 年
})


class MarketDataError(RuntimeError):
    """取数或代码解析失败。仅在 strict=True 或 kind 无法判定时抛出。"""


# ====================================================================
# 代码归一化与 kind 判定
# ====================================================================

def normalize_tx_code(code: str, kind: Optional[str] = None) -> str:
    """归一化为腾讯源要求的 ``sh000001`` / ``sz399006`` / ``sh512480`` 形式。

    接受的输入：``sh000001`` / ``SH000001`` / ``sh.000001`` / ``sh.000001``。
    纯 6 位数字**仅在 kind='equity' 时**可推断市场（5/6→sh，0/1/2/3→sz）；
    kind='index' 或 kind 未知时，纯数字**一律抛异常** ——
    因为 ``000001`` 作为指数是上证综指(sh)、作为个股是平安银行(sz)，猜不得。
    """
    raw = str(code or "").strip().lower()
    if not raw:
        raise MarketDataError("代码为空")

    if raw.startswith(("sh.", "sz.")):
        raw = raw[:2] + raw[3:]
    elif "." in raw:
        head, _, tail = raw.partition(".")
        # 兼容 baostock 的 'sh.000001' 已在上方处理；其余点号视为非法
        raise MarketDataError(f"无法识别的代码格式: {code!r}（请用 sh000001 / sz399006 形式）")

    if raw.startswith(("sh", "sz")):
        market, num = raw[:2], raw[2:]
    else:
        num, market = raw, ""
        if not num.isdigit():
            raise MarketDataError(f"无法识别的代码格式: {code!r}")
        if kind == KIND_EQUITY:
            market = "sh" if num[0] in ("5", "6", "9") else "sz"
        else:
            raise MarketDataError(
                f"纯数字代码 {code!r} 无法判定交易所（指数 000001=上证综指(sh)，"
                f"个股 000001=平安银行(sz)）。请显式写成 sh000001 / sz000001，"
                f"或对个股传 kind='equity'。"
            )

    if not num.isdigit() or len(num) != 6:
        raise MarketDataError(f"无法识别的代码: {code!r}（需 6 位数字）")
    return f"{market}{num}"


def resolve_kind(code: str, kind: Optional[str] = None) -> str:
    """判定代码属于 index 还是 equity。

    ``kind`` 显式给出时直接采用（并校验取值）。
    否则查 :data:`INDEX_CODES`；查不到则**抛异常**，不做猜测。
    """
    if kind is not None:
        k = str(kind).strip().lower()
        if k in ("index", "idx", "指数"):
            return KIND_INDEX
        if k in ("equity", "etf", "stock", "个股"):
            return KIND_EQUITY
        if k != "auto":
            raise MarketDataError(f"未知 kind={kind!r}（应为 'index' / 'equity' / 'auto'）")

    tx = normalize_tx_code(code, kind=KIND_EQUITY if kind is None and str(code).strip().isdigit() else None)
    if tx in INDEX_CODES:
        return KIND_INDEX
    if kind == "auto":
        raise MarketDataError(
            f"无法自动判定 {tx} 的 kind（不在 INDEX_CODES 登记表内）。"
            f"请显式传 kind='index' 或 kind='equity'。"
        )
    raise MarketDataError(
        f"无法判定 {tx} 的 kind（不在 INDEX_CODES 登记表内）。"
        f"请显式传 kind='index' 或 kind='equity'。"
    )


# ====================================================================
# 缓存读写
# ====================================================================

def _cache_dir() -> Path:
    """缓存根目录（延迟解析，便于测试 monkeypatch CACHE_DIR）。"""
    global CACHE_DIR
    if CACHE_DIR is not None:
        return Path(CACHE_DIR)
    from config.settings import PROJECT_ROOT
    return Path(PROJECT_ROOT) / "data" / "cache"


def cache_path(kind: str, tx_code: str, cache_dir: Optional[Path] = None) -> Path:
    """缓存文件路径。``data/cache/index_{code}_tx_history.csv`` / ``equity_{code}_tx_history.csv``。"""
    root = Path(cache_dir) if cache_dir is not None else _cache_dir()
    return root / f"{kind}_{tx_code}_tx_history.csv"


def load_cache(path: Path) -> Optional[pd.DataFrame]:
    """读缓存。文件不存在或损坏返回 None（并打日志，不抛）。"""
    p = Path(path)
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p, encoding="utf-8-sig")
    except Exception as e:  # pragma: no cover - 磁盘异常兜底
        logger.warning("缓存读取失败（将忽略）: %s → %s", p, e)
        return None
    if df is None or df.empty or "date" not in df.columns:
        logger.warning("缓存内容为空或缺 date 列（将忽略）: %s", p)
        return None
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    return _reindex_columns(df)


def save_cache(path: Path, df: pd.DataFrame) -> None:
    """写缓存。失败只打 WARNING（缓存是加速手段，不是正确性前提）。"""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        _reindex_columns(df).to_csv(p, index=False, encoding="utf-8-sig")
    except Exception as e:  # pragma: no cover - 磁盘异常兜底
        logger.warning("缓存写入失败（不影响本次取数）: %s → %s", p, e)


def _reindex_columns(df: pd.DataFrame) -> pd.DataFrame:
    """统一列顺序，缺失列补 NaN（**不补 0**）。"""
    out = df.copy()
    for col in OHLCV_COLUMNS:
        if col not in out.columns:
            out[col] = pd.NA
    for col in ("open", "high", "low", "close", "volume", "amount", "turnover"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    return out[list(OHLCV_COLUMNS)]


def merge_history(cached: Optional[pd.DataFrame],
                  fresh: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """合并缓存与新取数据：按 date 去重，**新数据覆盖同日旧值**（应对盘中修正）。"""
    frames = [f for f in (cached, fresh) if f is not None and not f.empty]
    if not frames:
        return None
    out = pd.concat(frames, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out = out.dropna(subset=["date"])
    out = out.drop_duplicates(subset=["date"], keep="last")
    return _reindex_columns(out)


# ====================================================================
# 企业行为（份额折算 / 拆分 / 合并 / 送转股）—— 未复权断崖的修复
# ====================================================================
#
# ⚠️ 这是 2026-10-04 P0.4 实测发现的一个**会毁掉全部 ETF 对照检验**的问题：
#   腾讯的 ETF/个股日线是**未复权**的，份额折算（拆分/合并）会在序列里留下断崖。
#
#   实测证据（本仓库缓存数据）：
#     · sh512480 半导体ETF  2021-03-29 单日 **−48.90%**、2026-07-03 单日 **−50.70%**
#     · sh512760 芯片ETF    2020-09-10 单日 **+53.10%**、2026-03-27 单日 **+49.75%**
#   后果：两只几乎同质的半导体 ETF（512480 vs 512760）日收益相关性
#         **原始 0.666 → 剔除断崖后 0.990**。
#   若不修，P0.4 的"合成指数 vs ETF 相关性 ≥0.8"验收会**把达标的东西判成不达标**
#   （512480 vs 国证半导体芯片 0.683 → 0.831；561310 消费电子 0.712 → 0.909）。

# A 股与 ETF 的单日涨跌幅上限为 20%（科创板/创业板注册制 ±20%，其余 ±10%）。
# 因此单日 |收益| 超过 22% **只可能**来自企业行为 —— 除非该标的没有涨跌幅限制
# （部分跨境 ETF / 退市整理股），故检测结果**必须显式打印供人工复核**，不许静默处理。
CORPORATE_ACTION_THRESHOLD = 0.22

CA_COLUMNS: tuple = ("date", "prev_date", "prev_close", "close", "factor", "ret")


def detect_corporate_actions(df: pd.DataFrame,
                             *,
                             threshold: float = CORPORATE_ACTION_THRESHOLD) -> pd.DataFrame:
    """检测未复权序列中疑似**企业行为**造成的断崖。

    判据：单日 ``|收益| > threshold``（默认 22%，高于单日涨跌幅上限 20%）。

    Returns:
        DataFrame（列 :data:`CA_COLUMNS`，按日期升序）；无事件时返回**空表**（不是 None）。
        ``factor = close_t / prev_close`` —— 把事件日**之前**的价格乘以它即可与事件日连续
        （份额拆分 ⇒ factor < 1；份额合并 / 送转 ⇒ factor > 1）。

    ⚠️ 真实崩溃若超过涨跌幅限制，说明该标的**没有**涨跌幅限制 ⇒ 本函数会误报。
       所以事件必须由调用方打印出来人工复核，**绝不允许静默复权**。
    """
    if df is None or df.empty or "close" not in df.columns:
        return pd.DataFrame(columns=list(CA_COLUMNS))
    d = df[["date", "close"]].copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    d = d.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    if len(d) < 2:
        return pd.DataFrame(columns=list(CA_COLUMNS))
    d["prev_date"] = d["date"].shift(1)
    d["prev_close"] = d["close"].shift(1)
    # prev_close = 0 不可能出现（价格为 0 的日线已被上游剔除），除法安全
    d["factor"] = d["close"] / d["prev_close"]
    d["ret"] = d["factor"] - 1.0
    ev = d[d["ret"].abs() > float(threshold)].dropna(subset=["prev_close", "factor"])
    return ev[list(CA_COLUMNS)].reset_index(drop=True)


def adjust_corporate_actions(df: pd.DataFrame,
                             *,
                             threshold: float = CORPORATE_ACTION_THRESHOLD,
                             events: Optional[pd.DataFrame] = None):
    """前复权（以**最新价**为锚）修复企业行为断崖。

    返回 ``(adjusted_df, events)``：

    - ``open/high/low/close`` 乘以该行的累积复权因子；``volume/amount/turnover``
      **保持原值** —— 份额折算不改变成交额，因此"复权价 × 未复权量 ≠ 成交额"
      是**预期行为**，不是 bug。
    - 新增列 ``adj_factor``（该行累积因子；最新一段恒为 1.0）。
    - ``events`` 为 :func:`detect_corporate_actions` 的结果（可外部传入以避免重复计算）。

    ⚠️ 指数**不需要**复权，调用方只应对 equity 使用。
    """
    if df is None or df.empty:
        return df, pd.DataFrame(columns=list(CA_COLUMNS))
    if events is None:
        events = detect_corporate_actions(df, threshold=threshold)

    out = df.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out = out.sort_values("date").reset_index(drop=True)

    cum = pd.Series(1.0, index=out.index, dtype="float64")
    if events is not None and not events.empty:
        for _, e in events.sort_values("date").iterrows():
            mask = out["date"] < pd.Timestamp(e["date"])
            cum.loc[mask] = cum.loc[mask] * float(e["factor"])

    out["adj_factor"] = cum
    for col in ("open", "high", "low", "close"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce") * cum

    ordered = [c for c in (list(OHLCV_COLUMNS) + ["adj_factor"]) if c in out.columns]
    return out[ordered].reset_index(drop=True), events.reset_index(drop=True)


def _finalize(df: Optional[pd.DataFrame],
              *,
              kind: str,
              adjust: bool,
              tx_code: str,
              bars: Optional[int]) -> Optional[pd.DataFrame]:
    """统一收尾：equity 可选前复权 → 切片。事件挂在 ``df.attrs['corporate_actions']``。"""
    if df is None or df.empty:
        return df
    events = None
    if adjust and kind == KIND_EQUITY:
        df, events = adjust_corporate_actions(df)
        if events is not None and not events.empty:
            logger.warning(
                "检测到 %d 处疑似企业行为（未复权断崖），已按前复权修复: %s —— "
                "请人工复核是否为份额折算/拆分。", len(events), tx_code)
            for _, e in events.iterrows():
                logger.warning(
                    "    %s 前收 %.4f → 收 %.4f（%+.2f%%，因子 %.4f）",
                    pd.Timestamp(e["date"]).date(), float(e["prev_close"]),
                    float(e["close"]), 100 * float(e["ret"]), float(e["factor"]))
    out = _slice(df, bars)
    if events is not None and not events.empty and out is not None:
        out.attrs["corporate_actions"] = events
    return out


# ====================================================================
# 腾讯源取数（唯一的网络入口）
# ====================================================================

def _fetch_raw_tx(kind: str, tx_code: str) -> Optional[pd.DataFrame]:
    """调用腾讯接口取**全历史**。失败返回 None 并记录原因。

    指数用 ``stock_zh_index_daily_tx``（列：date/open/close/high/low/amount），
    个股与 ETF 用 ``stock_zh_a_hist_tx``（列：date/open/close/high/low/volume/turnover/amount）。
    """
    import akshare as ak
    import contextlib
    import io

    fn = ak.stock_zh_index_daily_tx if kind == KIND_INDEX else ak.stock_zh_a_hist_tx
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        raw = fn(symbol=tx_code)
    if raw is None or len(raw) == 0:
        return None

    df = raw.rename(columns={"日期": "date", "开盘": "open", "最高": "high",
                             "最低": "low", "收盘": "close", "成交量": "volume"})
    if "date" not in df.columns:
        return None
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return _reindex_columns(df)


# ====================================================================
# 对外主入口
# ====================================================================

def fetch_history(code: str,
                  kind: Optional[str] = None,
                  *,
                  bars: Optional[int] = None,
                  refresh: bool = False,
                  prefer_cache: bool = False,
                  allow_stale: bool = True,
                  strict: bool = False,
                  adjust: bool = True,
                  cache_dir: Optional[Path] = None) -> Optional[pd.DataFrame]:
    """取日线历史（含缓存合并）。这是本模块的主入口。

    Args:
        code: ``sh000001`` / ``sz399006`` / ``sh512480`` / ``sz002371``。
        kind: ``'index'`` / ``'equity'`` / ``None``（查 :data:`INDEX_CODES` 判定）。
            **判定不了会抛 MarketDataError，不会猜。**
        bars: 只返回最近 N 根（``None`` = 全部）。
        refresh: True 时忽略缓存、强制重新联网取全量。
        prefer_cache: True 时**只要缓存存在就直接返回**，不联网（离线/回测快路径）。
        allow_stale: 联网失败时是否退回旧缓存。True 会**明确打 WARNING 并报出最后日期**。
            **日频决策（如 regime 判定）应传 False** —— 宁可判定为"不可用"，
            也不要基于过期数据给出"安全"的错误结论。
        strict: True 时取数失败抛 ``MarketDataError``；False 时返回 None + WARNING。
        adjust: **仅对 equity 生效**。True（默认）时做前复权，修掉腾讯源的
            企业行为断崖（份额折算/拆分；实测 512480 在 2021-03-29 单日 −48.9%）。
            检测到的事件会打 WARNING、并挂在返回值的 ``df.attrs['corporate_actions']``。
            **指数不需要复权**，kind='index' 时本参数被忽略。
            缓存里存的**始终是原始未复权价**，复权在读出时进行（可复现、可改口径）。
        cache_dir: 覆盖缓存目录（测试用）。

    Returns:
        列 ``date/open/high/low/close/volume/amount/turnover``（equity 且 adjust=True 时
        追加 ``adj_factor``）的 DataFrame（升序），或 None（失败且 strict=False）。
    """
    k = resolve_kind(code, kind)
    tx = normalize_tx_code(code, kind=k)
    path = cache_path(k, tx, cache_dir=cache_dir)
    cached = None if refresh else load_cache(path)

    # —— 离线快路径 ——
    if prefer_cache and not refresh:
        if cached is not None:
            return _finalize(cached, kind=k, adjust=adjust, tx_code=tx, bars=bars)
        logger.warning("prefer_cache=True 但缓存不存在/不可用: %s", path)

    # —— 联网取全量 ——
    fresh: Optional[pd.DataFrame] = None
    err: Optional[BaseException] = None
    try:
        fresh = _fetch_raw_tx(k, tx)
    except Exception as e:  # 网络/接口异常一律降级，不向上抛（除 strict）
        err = e
        logger.warning("腾讯源异常 %s(%s): %s: %s", tx, k, type(e).__name__, e)

    if fresh is None or fresh.empty:
        reason = f"{type(err).__name__}: {err}" if err is not None else "接口返回空数据"
        if cached is not None and allow_stale:
            last = cached["date"].iloc[-1].date()
            logger.warning(
                "腾讯源取数失败 %s(%s) —— %s；**已退回过期缓存**（最后日期 %s，共 %d 根）。"
                "请勿在此数据上做日频决策。",
                tx, k, reason, last, len(cached),
            )
            return _finalize(cached, kind=k, adjust=adjust, tx_code=tx, bars=bars)
        msg = f"腾讯源取数失败 {tx}({k}): {reason}"
        if strict:
            raise MarketDataError(msg)
        logger.warning(msg + "（无可用缓存，返回 None）")
        return None

    merged = merge_history(cached, fresh)
    if merged is None or merged.empty:
        msg = f"腾讯源数据合并后为空 {tx}({k})"
        if strict:
            raise MarketDataError(msg)
        logger.warning(msg)
        return None

    if not prefer_cache:
        save_cache(path, merged)
    return _finalize(merged, kind=k, adjust=adjust, tx_code=tx, bars=bars)


def fetch_index_history(code: str, **kwargs) -> Optional[pd.DataFrame]:
    """取**指数**日线。等价于 ``fetch_history(code, kind='index', ...)``。"""
    return fetch_history(code, kind=KIND_INDEX, **kwargs)


def fetch_equity_history(code: str, **kwargs) -> Optional[pd.DataFrame]:
    """取 **ETF / 个股** 日线。等价于 ``fetch_history(code, kind='equity', ...)``。"""
    return fetch_history(code, kind=KIND_EQUITY, **kwargs)


def _slice(df: pd.DataFrame, bars: Optional[int]) -> pd.DataFrame:
    """取最近 bars 根（bars=None 或 <=0 时返回全部）。"""
    if df is None or df.empty:
        return df
    if bars is None or bars <= 0 or len(df) <= bars:
        return df.reset_index(drop=True)
    return df.tail(int(bars)).reset_index(drop=True)


def slice_as_of(df: pd.DataFrame, as_of) -> pd.DataFrame:
    """截取到 ``as_of`` 当日（**含**）为止，用于防未来函数。

    回测里任何"某日之后"的数据都必须先经此截断，否则会引入前视偏差。
    """
    if df is None or df.empty:
        return df
    cut = pd.Timestamp(as_of)
    out = df[df["date"] <= cut]
    return out.reset_index(drop=True)


def describe_cache(codes_kinds, cache_dir: Optional[Path] = None) -> pd.DataFrame:
    """体检：报告一批 (code, kind) 的缓存覆盖情况。供 verify 脚本与人工排查使用。

    Returns:
        列 ``code / kind / cached / rows / first / last / path`` 的 DataFrame。
    """
    rows = []
    for item in codes_kinds:
        code, kind = (item if isinstance(item, (tuple, list)) else (item, None))
        try:
            k = resolve_kind(code, kind)
            tx = normalize_tx_code(code, kind=k)
        except MarketDataError as e:
            rows.append({"code": code, "kind": kind or "-", "cached": False, "rows": 0,
                         "first": None, "last": None, "path": f"<无法解析: {e}>"})
            continue
        p = cache_path(k, tx, cache_dir=cache_dir)
        df = load_cache(p)
        rows.append({
            "code": tx,
            "kind": k,
            "cached": df is not None,
            "rows": 0 if df is None else len(df),
            "first": None if df is None else df["date"].iloc[0].date().isoformat(),
            "last": None if df is None else df["date"].iloc[-1].date().isoformat(),
            "path": str(p),
        })
    return pd.DataFrame(rows)


__all__ = [
    "MarketDataError",
    "INDEX_CODES",
    "KIND_INDEX",
    "KIND_EQUITY",
    "OHLCV_COLUMNS",
    "CA_COLUMNS",
    "CORPORATE_ACTION_THRESHOLD",
    "CACHE_DIR",
    "normalize_tx_code",
    "resolve_kind",
    "cache_path",
    "load_cache",
    "save_cache",
    "merge_history",
    "detect_corporate_actions",
    "adjust_corporate_actions",
    "fetch_history",
    "fetch_index_history",
    "fetch_equity_history",
    "slice_as_of",
    "describe_cache",
]
