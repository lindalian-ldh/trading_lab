"""龙虎榜累积层 —— zzshare `lhb_list` / `lhb_detail` → 本地可复算存储。

## 为什么存在

需求 #1（板块轮动印象面板）的原料是**龙虎榜**。但龙虎榜只有 zzshare 一个可用源
（东财已禁用），且**历史只能回溯到 2025-01-02**（实测：2024-12-31 返回空，
2025-01-02 起才有数据）。因此：

> **每拖一天，就永久少一天龙虎榜历史。** 本模块的第一价值是"从今天开始每天落盘"。

## 两级取数（关键设计）

zzshare 的两个接口给的东西**完全不同**，且**不能互相替代**：

| 层级 | 接口 | 调用量 | 拿到什么 |
|---|---|---|---|
| **Tier 1** | `lhb_list(date1)` | **1 次/天** | 当日全部上榜股：净买入额 `buy_in`、换手率 `turnover_ratio`、成交额 `turnover`、涨跌幅、`concepts`（板块） |
| **Tier 2** | `lhb_detail(date1, stock_code)` | **1 次/股** | **毛额 `buy_total` / `sell_total`** + 每边 top5 席位 `traders` |

### 两个必须记住的字段语义（实测确认，2026-10-03）

1. **`buy_in` 是净额，不是毛额**。实测 `buy_in == buy_total - sell_total`
   （万科A：688,965,000 − 617,580,000 = 71,385,000 ≈ 71,384,600 ✅）。
2. **`buy_group_icons` / `sell_group_icons` 只是"有标记的席位"**（机构 / 知名游资 / 深股通），
   **不是全部席位**。万科A 的 icon 买入额合计 3.9 亿，而真实 `buy_total` 是 6.9 亿。
   ⇒ **绝不能用 icon 金额之和当毛额**，否则"买卖比"会被系统性低估。
   ⇒ 要算**买卖比 = buy_total / (buy_total + sell_total)**，**必须走 Tier 2**。

Tier 1 便宜（回填 430 天 ≈ 430 次调用），Tier 2 昂贵（430 天 × ~70 股 ≈ 3 万次调用）。
因此本模块**回填默认只做 Tier 1**，Tier 2 按天增量补（面板需要的买卖比在 Tier 2 到位前显示 `n/a`）。

## 存储

```
data/lhb/lhb_stocks.parquet     (date, stock_code) 唯一；Tier1 字段 + Tier2 字段(可空)
data/lhb/lhb_concepts.parquet   (date, stock_code, plate_code) 唯一
data/lhb/lhb_seats.parquet      (date, stock_code, side, rank) 唯一
data/lhb/_state.json            {"list": {date: n}, "detail": {date: n}}  ← 断点续跑用
```

**可断点续跑**：每成功一天立即落盘 + 更新 state；重跑自动跳过已完成的日期。
空结果也会被记录（`n=0`），避免每天重复试一个节假日。

标的板块归属用 LHB 自带 `concepts`（801xxx/803xxx），
与 `plates_list(plate_type=17)`（题材，同为 801xxx）按**代码直连**（实测命中率 87.6%）；
未命中的编码保留 `plate_name=''`，**不丢弃**。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date as _date
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ====================================================================
# 常量与路径
# ====================================================================

STORE_DIR: Optional[Path] = None          # 测试可 monkeypatch
THROTTLE_SECONDS: float = 1.5             # 单次调用间隔（zzshare 匿名 30 次/分钟）
MAX_CONSECUTIVE_FAILURES: int = 8         # 连续失败到此数即中止（避免把限频打成封禁）
MAX_RETRY: int = 2

# 题材板块类型编码（zzshare）：17=题材（801xxx，与 LHB concepts 同码空间）
PLATE_TYPE_THEME: int = 17

STOCK_COLUMNS = [
    "date", "stock_code", "stock_name", "up_reason", "up_desc",
    "quote_change", "amplitude", "turnover", "turnover_ratio",
    "capitalization", "circ_price", "buy_in", "t_type", "join_num",
    "concepts_raw",
    # —— Tier 2（可空）——
    "buy_total", "sell_total", "buy_sell_ratio", "detail_fetched",
]
CONCEPT_COLUMNS = ["date", "stock_code", "plate_code", "plate_name"]
SEAT_COLUMNS = ["date", "stock_code", "side", "rank", "trader_id",
                "trader_name", "buy_amount", "sell_amount", "youzi_icon", "group_icon"]


class LhbStoreError(RuntimeError):
    """取数或存储失败（仅在 strict 场景下抛出）。"""


# ====================================================================
# 内部工具
# ====================================================================

def _suppress_output(func):
    """压制 zzshare SDK 的 stdout/stderr 噪声（沿用仓库既有约定）。"""
    import io
    import os
    import sys
    devnull_fd = os.open(os.devnull, os.O_RDWR)
    saved_out_fd, saved_err_fd = os.dup(1), os.dup(2)
    os.dup2(devnull_fd, 1)
    os.dup2(devnull_fd, 2)
    saved_out_obj, saved_err_obj = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        return func()
    finally:
        sys.stdout, sys.stderr = saved_out_obj, saved_err_obj
        os.dup2(saved_out_fd, 1)
        os.dup2(saved_err_fd, 2)
        os.close(saved_out_fd)
        os.close(saved_err_fd)
        os.close(devnull_fd)


def store_dir() -> Path:
    """存储根目录（延迟解析，便于测试 monkeypatch STORE_DIR）。"""
    global STORE_DIR
    if STORE_DIR is not None:
        return Path(STORE_DIR)
    from config.settings import PROJECT_ROOT
    return Path(PROJECT_ROOT) / "data" / "lhb"


def _path(name: str) -> Path:
    return store_dir() / name


def _api():
    """创建 zzshare DataApi（token 取自 config.settings）。"""
    import zzshare
    from config.settings import settings
    token = (settings.zzshare_token or "").strip()
    if not token:
        logger.warning("未配置 ZZSHARE_TOKEN，龙虎榜将走匿名限频（30 次/分钟）")
    return zzshare.pro_api(token=token or None, timeout=30)


def _norm_date(d) -> str:
    """归一化为 'YYYY-MM-DD'。接受 str/date/datetime/YYYYMMDD。"""
    if isinstance(d, (_date, datetime)):
        return pd.Timestamp(d).strftime("%Y-%m-%d")
    s = str(d).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return pd.Timestamp(s).strftime("%Y-%m-%d")


def _ymd(d: str) -> str:
    return _norm_date(d).replace("-", "")


# ====================================================================
# 交易日（复用 P0.1 的腾讯源缓存，不额外引入数据源）
# ====================================================================

def trading_days(start: Optional[str] = None,
                 end: Optional[str] = None,
                 allow_online: bool = True) -> list:
    """返回 [start, end] 内的 A 股交易日（'YYYY-MM-DD'）。

    来源：`core.marketdata_tx` 的上证综指（sh000001）日线 —— **有 K 线即交易日**。
    这样就不必再引入一个交易日历数据源，且与择时模块的日历天然一致。
    """
    from core.marketdata_tx import fetch_index_history
    df = fetch_index_history("sh000001", prefer_cache=not allow_online, allow_stale=True)
    if df is None or df.empty:
        if not allow_online:
            raise LhbStoreError("无法确定交易日：sh000001 缓存缺失（先跑 scripts/verify_marketdata.py --online）")
        raise LhbStoreError("无法确定交易日：sh000001 取数失败且无缓存")
    days = df["date"].dt.strftime("%Y-%m-%d").tolist()
    if start:
        days = [d for d in days if d >= _norm_date(start)]
    if end:
        days = [d for d in days if d <= _norm_date(end)]
    return days


# ====================================================================
# 状态（断点续跑）
# ====================================================================

def _state_path() -> Path:
    return _path("_state.json")


def load_state() -> dict:
    p = _state_path()
    if not p.exists():
        return {"list": {}, "detail": {}}
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
        st.setdefault("list", {})
        st.setdefault("detail", {})
        return st
    except Exception as e:
        logger.warning("状态文件损坏（将重建）: %s → %s", p, e)
        return {"list": {}, "detail": {}}


def save_state(state: dict) -> None:
    p = _state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        state = dict(state)
        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        p.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:  # pragma: no cover
        logger.warning("状态写入失败: %s → %s", p, e)


# ====================================================================
# parquet 读写（追加 + 按唯一键去重，新数据覆盖旧值）
# ====================================================================

def _read_parquet(path: Path, columns: list) -> Optional[pd.DataFrame]:
    if not Path(path).exists():
        return None
    try:
        df = pd.read_parquet(path)
    except Exception as e:  # pragma: no cover
        logger.warning("parquet 读取失败（将忽略）: %s → %s", path, e)
        return None
    if df is None or df.empty:
        return None
    for c in columns:
        if c not in df.columns:
            df[c] = pd.NA
    return df[columns]


def _append_parquet(path: Path, fresh: pd.DataFrame, keys: list, columns: list) -> int:
    """追加并去重（同键以 fresh 为准）。返回落盘后的总行数。"""
    if fresh is None or fresh.empty:
        return 0 if not Path(path).exists() else len(_read_parquet(path, columns) or [])
    old = _read_parquet(path, columns)
    frames = [f for f in (old, fresh) if f is not None and not f.empty]
    out = pd.concat(frames, ignore_index=True)
    for c in columns:
        if c not in out.columns:
            out[c] = pd.NA
    out = out[columns]
    out = out.drop_duplicates(subset=keys, keep="last").reset_index(drop=True)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(path, index=False)
    return len(out)


def load_stocks(start: Optional[str] = None, end: Optional[str] = None) -> pd.DataFrame:
    """读龙虎榜个股日表。"""
    df = _read_parquet(_path("lhb_stocks.parquet"), STOCK_COLUMNS)
    if df is None:
        return pd.DataFrame(columns=STOCK_COLUMNS)
    return _slice_dates(df, start, end)


def load_concepts(start: Optional[str] = None, end: Optional[str] = None) -> pd.DataFrame:
    """读龙虎榜个股↔题材明细表。"""
    df = _read_parquet(_path("lhb_concepts.parquet"), CONCEPT_COLUMNS)
    if df is None:
        return pd.DataFrame(columns=CONCEPT_COLUMNS)
    return _slice_dates(df, start, end)


def load_seats(start: Optional[str] = None, end: Optional[str] = None) -> pd.DataFrame:
    """读龙虎榜席位明细表（仅 Tier 2 覆盖到的日期有数据）。"""
    df = _read_parquet(_path("lhb_seats.parquet"), SEAT_COLUMNS)
    if df is None:
        return pd.DataFrame(columns=SEAT_COLUMNS)
    return _slice_dates(df, start, end)


def _slice_dates(df: pd.DataFrame, start: Optional[str], end: Optional[str]) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df
    if start:
        out = out[out["date"] >= _norm_date(start)]
    if end:
        out = out[out["date"] <= _norm_date(end)]
    return out.reset_index(drop=True)


# ====================================================================
# 题材表（plates_list(plate_type=17)，801xxx）
# ====================================================================

def fetch_theme_table(force: bool = False) -> pd.DataFrame:
    """取题材板块表（code → name），带本地缓存。

    用途：把 LHB 的 `concepts`（801xxx/803xxx）解析成题材名。
    实测 801xxx 命中率 87.6%；803xxx 不在该表内，保留 `plate_name=''`。
    """
    p = _path("theme_table.parquet")
    if p.exists() and not force:
        df = _read_parquet(p, ["plate_code", "plate_name", "plate_type"])
        if df is not None and not df.empty:
            return df
    api = _api()
    raw = _suppress_output(lambda: api.plates_list(plate_type=PLATE_TYPE_THEME))
    if not raw:
        logger.warning("题材表取数失败（plates_list(17) 返回空）")
        return pd.DataFrame(columns=["plate_code", "plate_name", "plate_type"])
    df = pd.DataFrame([{
        "plate_code": str(x.get("plate_code", "")).strip(),
        "plate_name": str(x.get("plate_name", "")).strip(),
        "plate_type": PLATE_TYPE_THEME,
    } for x in raw if isinstance(x, dict)])
    df = df[df["plate_code"] != ""].drop_duplicates(subset=["plate_code"], keep="last")
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(p, index=False)
    logger.info("题材表已缓存: %d 个题材 → %s", len(df), p)
    return df.reset_index(drop=True)


# ====================================================================
# 板块成分股（plates_stocks）—— "上榜率" 的分母来源
# ====================================================================

PLATE_CONST_COLUMNS = ["plate_type", "plate_code", "plate_name",
                       "stock_code", "stock_name", "time_in"]

# 题材热度（plates_rank(17)）的字段。rank = 按 score 降序的名次（1 = 最热）
THEME_RANK_COLUMNS = ["date", "plate_code", "plate_name", "rate",
                      "trade_money", "volume_ration", "score", "rank"]


def load_theme_rank(start: Optional[str] = None, end: Optional[str] = None) -> pd.DataFrame:
    """读已缓存的题材热度排名。"""
    df = _read_parquet(_path("theme_rank.parquet"), THEME_RANK_COLUMNS)
    if df is None:
        return pd.DataFrame(columns=THEME_RANK_COLUMNS)
    return _slice_dates(df, start, end)


def fetch_theme_rank(date_str: str,
                     plate_type: int = PLATE_TYPE_THEME,
                     limit: int = 600,
                     force: bool = False) -> pd.DataFrame:
    """取某个交易日的题材热度排名（`plates_rank`），带缓存。

    **P1 面板的「题材热度」列来源。** 实测（2026-10-03）该接口**支持历史日期**
    （2025-01-03 / 2025-04-01 / 2026-06-01 均成功返回），因此面板对过去日期**可复现**；
    但厂商对**很老的日期**字段会退化（实测 2024-01-02 的 `trade_money` 为 None）。

    `rank` 由本函数按 `score` 降序计算（接口本身不返回名次）。
    """
    d = _norm_date(date_str)
    path = _path("theme_rank.parquet")
    old = _read_parquet(path, THEME_RANK_COLUMNS)
    if old is not None and not old.empty and not force:
        have = old[old["date"] == d]
        if not have.empty:
            return have.reset_index(drop=True)

    api = _api()
    raw = _call_with_retry(
        lambda: api.plates_rank(plate_type=plate_type, date1=_ymd(d), limit=limit),
        f"plates_rank({plate_type},{d})")
    rows = []
    for x in (raw or []):
        if not isinstance(x, dict):
            continue
        code = str(x.get("plate_code", "") or "").strip()
        if not code:
            continue
        rows.append({
            "date": d,
            "plate_code": code,
            "plate_name": str(x.get("plate_name", "") or "").strip(),
            "rate": _num(x.get("rate")),
            "trade_money": _num(x.get("trade_money")),
            "volume_ration": _num(x.get("volume_ration")),
            "score": _num(x.get("score")),
        })
    fresh = pd.DataFrame(rows)
    if fresh.empty:
        logger.warning("题材热度 %s 返回空（可能非交易日或尚未发布）", d)
        return pd.DataFrame(columns=THEME_RANK_COLUMNS)
    # 名次：按 score 降序（NaN 排最后）
    fresh = fresh.sort_values("score", ascending=False, na_position="last").reset_index(drop=True)
    fresh["rank"] = range(1, len(fresh) + 1)
    fresh = fresh[THEME_RANK_COLUMNS]

    if old is not None and not old.empty:
        keep = old[old["date"] != d]
        fresh = pd.concat([keep, fresh], ignore_index=True)
    fresh = fresh.drop_duplicates(subset=["date", "plate_code"], keep="last")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fresh.to_parquet(path, index=False)
    return fresh[fresh["date"] == d].reset_index(drop=True)


def load_plate_constituents(plate_type: Optional[int] = None) -> pd.DataFrame:
    """读已缓存的板块成分股。"""
    df = _read_parquet(_path("plate_constituents.parquet"), PLATE_CONST_COLUMNS)
    if df is None:
        return pd.DataFrame(columns=PLATE_CONST_COLUMNS)
    if plate_type is not None:
        df = df[df["plate_type"].astype(str) == str(plate_type)]
    return df.reset_index(drop=True)


def fetch_plate_constituents(plate_code: str,
                             plate_type: int = PLATE_TYPE_THEME,
                             force: bool = False) -> pd.DataFrame:
    """取某个板块的成分股（带缓存）。**这是"上榜率"的分母。**

    ⚠️ 实测（2026-10-03）：`plates_stocks` 对**不在 `plates_list(17)` 内的编码返回空**
    （如 801663 信创 / 801722 存储 / 801881 DeepSeek）⇒ 那些题材**只能给上榜家数，
    不能给上榜率**。本函数对空结果返回带列的空表，由调用方显式处理。

    注意：空结果**不落盘**（避免把"未取到"写成"该板块无成分股"），
    因此空板块每次都会重试 —— 数量很少，可接受。
    """
    code = str(plate_code).strip()
    path = _path("plate_constituents.parquet")
    old = _read_parquet(path, PLATE_CONST_COLUMNS)

    if old is not None and not old.empty and not force:
        have = old[(old["plate_code"].astype(str) == code)
                   & (old["plate_type"].astype(str) == str(plate_type))]
        if not have.empty:
            return have.reset_index(drop=True)

    api = _api()
    raw = _call_with_retry(
        lambda: api.plates_stocks(plate_type=plate_type, plate_code=code),
        f"plates_stocks({plate_type},{code})")
    rows = []
    for x in (raw or []):
        if not isinstance(x, dict):
            continue
        sc = str(x.get("stock_code", "") or "").strip()
        if not sc:
            continue
        rows.append({
            "plate_type": plate_type,
            "plate_code": code,
            "plate_name": str(x.get("plate_name", "") or "").strip(),
            "stock_code": sc.zfill(6),
            "stock_name": str(x.get("stock_name", "") or "").strip(),
            "time_in": str(x.get("time_in", "") or "").strip(),
        })
    fresh = pd.DataFrame(rows, columns=PLATE_CONST_COLUMNS)
    if fresh.empty:
        logger.warning("板块 %s(%s) 成分股为空 —— 该题材无法计算上榜率（只能给上榜家数）",
                       code, plate_type)
        return fresh

    if old is not None and not old.empty:
        keep = old[~((old["plate_code"].astype(str) == code)
                     & (old["plate_type"].astype(str) == str(plate_type)))]
        fresh = pd.concat([keep, fresh], ignore_index=True)
    fresh = fresh.drop_duplicates(subset=["plate_type", "plate_code", "stock_code"], keep="last")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fresh.to_parquet(path, index=False)
    return fresh[(fresh["plate_code"].astype(str) == code)
                 & (fresh["plate_type"].astype(str) == str(plate_type))].reset_index(drop=True)


# ====================================================================
# Tier 1: lhb_list(date1)
# ====================================================================

def _parse_concepts(raw) -> list:
    """'801007:房地产,801678:物业服务' → [('801007','房地产'), ('801678','物业服务')]。"""
    out = []
    for item in str(raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        code, _, name = item.partition(":")
        code = code.strip()
        if code:
            out.append((code, name.strip()))
    return out


def normalize_list_day(raw: list, date_str: str) -> tuple:
    """把 `lhb_list` 原始返回拆成 (个股表, 题材明细表)。纯函数，零 IO。"""
    d = _norm_date(date_str)
    stocks, concepts = [], []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("stock_code", "") or "").strip().zfill(6)
        if not code or code == "000000":
            continue
        concepts_raw = str(item.get("concepts") or "")
        stocks.append({
            "date": d,
            "stock_code": code,
            "stock_name": str(item.get("stock_name", "") or "").strip(),
            "up_reason": str(item.get("up_reason", "") or "").strip(),
            "up_desc": str(item.get("up_desc", "") or "").strip(),
            "quote_change": _num(item.get("quote_change")),
            "amplitude": _num(item.get("amplitude")),
            "turnover": _num(item.get("turnover")),
            "turnover_ratio": _num(item.get("turnover_ratio")),
            "capitalization": _num(item.get("capitalization")),
            "circ_price": _num(item.get("circ_price")),
            "buy_in": _num(item.get("buy_in")),
            "t_type": _num(item.get("t_type")),
            "join_num": _num(item.get("join_num")),
            "concepts_raw": concepts_raw,
            # Tier 2 字段先留空
            "buy_total": float("nan"), "sell_total": float("nan"),
            "buy_sell_ratio": float("nan"), "detail_fetched": False,
        })
        for pcode, pname in _parse_concepts(concepts_raw):
            concepts.append({"date": d, "stock_code": code,
                             "plate_code": pcode, "plate_name": pname})
    sdf = pd.DataFrame(stocks, columns=STOCK_COLUMNS)
    cdf = pd.DataFrame(concepts, columns=CONCEPT_COLUMNS)
    if not sdf.empty:
        sdf = sdf.drop_duplicates(subset=["date", "stock_code"], keep="last")
    if not cdf.empty:
        cdf = cdf.drop_duplicates(subset=["date", "stock_code", "plate_code"], keep="last")
    return sdf, cdf


def _num(v):
    try:
        if v is None or v == "":
            return float("nan")
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


# ====================================================================
# Tier 2: lhb_detail(date1, stock_code)
# ====================================================================

def normalize_detail(raw, date_str: str, stock_code: str) -> tuple:
    """把 `lhb_detail` 返回拆成 (个股覆盖字段 dict, 席位表 DataFrame)。纯函数，零 IO。"""
    d = _norm_date(date_str)
    code = str(stock_code).strip().zfill(6)
    if not isinstance(raw, dict):
        return None, pd.DataFrame(columns=SEAT_COLUMNS)
    det = raw.get("detail") or {}
    buy_total = _num(det.get("buy_total"))
    sell_total = _num(det.get("sell_total"))
    ratio = float("nan")
    if pd.notna(buy_total) and pd.notna(sell_total) and (buy_total + sell_total) > 0:
        ratio = buy_total / (buy_total + sell_total)
    stock = {
        "date": d, "stock_code": code,
        "buy_total": buy_total, "sell_total": sell_total,
        "buy_sell_ratio": ratio, "detail_fetched": True,
        # detail 里更权威的标量（可能与 list 略有差异，以 detail 为准）
        "up_reason": str(det.get("up_reason", "") or "").strip(),
        "quote_change": _num(det.get("quote_change")),
        "turnover": _num(det.get("turnover")),
        "turnover_ratio": _num(det.get("turnover_ratio")),
        "circ_price": _num(det.get("circ_price")),
        "buy_in": _num(det.get("buy_in")),
        "join_num": _num(det.get("join_num")),
    }
    rows = []
    for t in raw.get("traders") or []:
        if not isinstance(t, dict):
            continue
        side = "buy" if str(t.get("type")) == "1" else "sell"
        rows.append({
            "date": d, "stock_code": code, "side": side,
            "rank": int(_num(t.get("rank"))) if pd.notna(_num(t.get("rank"))) else 0,
            "trader_id": str(t.get("trader_id", "") or "").strip(),
            "trader_name": str(t.get("trader_name", "") or "").strip(),
            "buy_amount": _num(t.get("buy_amount")),
            "sell_amount": _num(t.get("sell_amount")),
            "youzi_icon": str(t.get("youzi_icon", "") or "").strip(),
            "group_icon": str(t.get("group_icon", "") or "").strip(),
        })
    seats = pd.DataFrame(rows, columns=SEAT_COLUMNS)
    if not seats.empty:
        seats = seats.drop_duplicates(subset=["date", "stock_code", "side", "rank"], keep="last")
    return stock, seats


# ====================================================================
# 更新主流程（可断点续跑）
# ====================================================================

def _call_with_retry(fn, label: str, retry: int = MAX_RETRY):
    last = None
    for attempt in range(retry + 1):
        try:
            return _suppress_output(fn)
        except Exception as e:
            last = e
            if attempt < retry:
                time.sleep(THROTTLE_SECONDS * (attempt + 1))
    logger.warning("%s 失败（已重试 %d 次）: %s: %s", label, retry, type(last).__name__, last)
    return None


def _today_str() -> str:
    return _norm_date(_date.today())


def _is_publish_risky(d: str) -> bool:
    """该日期是否**可能尚未发布**（厂商日批实测约 18:00 落库，见 plates_rank 的 time 字段）。

    判定：日期 >= 今天。即"今天及以后"都算有风险 —— 下午 16:30 跑时当天数据往往还没落库。
    """
    return d >= _today_str()


def update(start: Optional[str] = None,
           end: Optional[str] = None,
           *,
           tier: int = 1,
           force: bool = False,
           throttle: float = THROTTLE_SECONDS,
           max_days: Optional[int] = None,
           recent: Optional[int] = None,
           detail_codes: Optional[Iterable[str]] = None,
           verbose: bool = True) -> dict:
    """增量/回填龙虎榜。**可断点续跑**（已完成的日期默认跳过）。

    Args:
        start/end: 日期区间（含）。默认 = 龙虎榜可用起点(2025-01-02) ~ 今天。
        tier: 1 = 只跑 `lhb_list`（便宜，1 次/天）；2 = 再补 `lhb_detail`（毛额+席位，1 次/股）。
            ⚠️ tier=2 的"待处理"判据是 **`state['detail']` 而非 `state['list']`** ——
            否则对已经回填过 Tier 1 的日期会被整体跳过，Tier 2 永远补不上。
            若某天的 Tier 1 也缺，会在同一次循环里先补 Tier 1 再补 Tier 2。
        force: True 时忽略 state，重取（用于修复）。
        throttle: 单次调用间隔秒数。
        max_days: 本次最多处理多少个交易日（便于分批）。
        recent: **只对 Tier 2 生效** —— 只在最近 N 个交易日内补 detail。
            ⚠️ 必须给！Tier 1 是 424 天全量回填的，而 Tier 2 只回填了最近一段；
            不加窗口的话，每天都会试图补历史那 300+ 天（2 万多次调用）。
            Tier 1 仍按**整个区间**扫描，以保留"自动补齐任何漏跑"的自愈能力。
        detail_codes: Tier 2 只补这些股票代码（默认全部当日上榜股）。

    Returns:
        {'days_scanned', 'days_fetched', 'days_skipped', 'days_pending', 'stocks',
         'concepts', 'detail_fetched', 'seats', 'failures', 'aborted'}

    ``days_pending``：是交易日但返回空、且日期还没过去一天 ⇒ 判为"尚未发布"，
    **不写入 state**（下次运行会重试）。这是为了防止把真实交易日的洞永久封死。
    """
    if tier not in (1, 2):
        raise ValueError("tier 只能是 1 或 2")
    start = _norm_date(start) if start else EARLIEST_DATE
    end = _norm_date(end) if end else _today_str()
    days_all = [d for d in trading_days(start, end) if d >= start]
    state = load_state()
    have_list = state["list"]
    have_detail = state["detail"]

    # Tier 2 的滚动窗口（Tier 1 不受限，以保证自愈能力）
    window = days_all if recent is None else days_all[-int(recent):]

    if tier == 1:
        todo = days_all if force else [d for d in days_all if d not in have_list]
        detail_set: set = set()
    else:
        todo_list = [d for d in days_all if force or d not in have_list]
        todo_detail = [d for d in window if force or d not in have_detail]
        detail_set = set(todo_detail)
        todo = sorted(set(todo_list) | detail_set)
    if max_days:
        todo = todo[:int(max_days)]

    stats = {"days_scanned": len(days_all), "days_fetched": 0,
             "days_skipped": len(days_all) - len(todo),
             "days_pending": 0, "stocks": 0, "concepts": 0,
             "detail_fetched": 0, "seats": 0, "failures": 0, "aborted": False}

    if verbose:
        win_note = f"，Tier2 窗口 {len(window)} 天" if (tier == 2 and recent is not None) else ""
        print(f"龙虎榜更新：区间 {start} ~ {end}，交易日 {len(days_all)} 天，"
              f"待处理 {len(todo)} 天（Tier {tier}{win_note}）")

    if not todo:
        if verbose:
            print("✅ 无待处理日期（已是最新）。")
        return stats

    api = _api()
    consecutive_failures = 0

    for i, d in enumerate(todo, 1):
        need_list = force or d not in have_list

        # ================= Tier 1 =================
        sdf = None
        if need_list:
            raw = _call_with_retry(lambda d=d: api.lhb_list(date1=_ymd(d)), f"lhb_list({d})")
            if raw is None:
                consecutive_failures += 1
                stats["failures"] += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    stats["aborted"] = True
                    logger.warning(
                        "连续失败 %d 次，中止（避免被限频封禁）。已完成的日期已落盘，可重跑续传。",
                        consecutive_failures)
                    break
                time.sleep(throttle)
                continue
            consecutive_failures = 0
            raw = raw if isinstance(raw, list) else []
            sdf, cdf = normalize_list_day(raw, d)
        else:
            # Tier 1 已在库：直接读当日股票池，不再联网
            sdf = load_stocks(start=d, end=d)
            cdf = pd.DataFrame(columns=CONCEPT_COLUMNS)

        # —— 空结果护栏（关键修正）——
        # 交易日却没有任何上榜股，几乎不可能是真的（全期日均 71.8 只、最少 46 只）。
        # 若该日期还没过去一天，最可能是**厂商尚未发布**（日批约 18:00 落库）⇒
        # 绝不写 state，留给下次重试；否则会把真实交易日的洞永久封死。
        if need_list and (sdf is None or sdf.empty) and _is_publish_risky(d):
            stats["days_pending"] += 1
            logger.warning(
                "%s 是交易日但龙虎榜为空，且该日期尚未过去一天 ⇒ 判为「尚未发布」，"
                "不写入 state（下次运行会重试）。厂商日批实测约 18:00 落库。", d)
            time.sleep(throttle)
            continue

        if need_list:
            if not sdf.empty:
                _append_parquet(_path("lhb_stocks.parquet"), sdf,
                                keys=["date", "stock_code"], columns=STOCK_COLUMNS)
            if not cdf.empty:
                _append_parquet(_path("lhb_concepts.parquet"), cdf,
                                keys=["date", "stock_code", "plate_code"],
                                columns=CONCEPT_COLUMNS)
            stats["stocks"] += len(sdf)
            stats["concepts"] += len(cdf)
            stats["days_fetched"] += 1
            # 已通过护栏 ⇒ 可以安全记录（含"过去日期的空结果"，避免反复重试）
            have_list[d] = int(len(sdf))
            state["list"] = have_list
            save_state(state)

        if verbose:
            extra = ""
            if not need_list:
                extra = "（Tier1 已在库）"
            elif len(sdf) == 0:
                extra = "（无上榜）"
            print(f"  [{i}/{len(todo)}] {d} 上榜 {len(sdf):>3} 只"
                  f"{'' if need_list else ''}{extra}")

        # ================= Tier 2 =================
        # 只对"窗口内且 detail 未覆盖"的日期跑（见 update 的 recent 参数说明）
        if tier == 2 and d in detail_set and sdf is not None and not sdf.empty:
            codes = [str(c) for c in (detail_codes if detail_codes is not None
                                      else sdf["stock_code"].tolist())]
            detail_rows, seat_frames, n_ok = [], [], 0
            for code in codes:
                det = _call_with_retry(
                    lambda code=code, d=d: api.lhb_detail(date1=_ymd(d), stock_code=code),
                    f"lhb_detail({d},{code})")
                time.sleep(throttle)
                if det is None:
                    stats["failures"] += 1
                    continue
                stock_row, seats = normalize_detail(det, d, code)
                if stock_row is None:
                    continue
                detail_rows.append(stock_row)
                if seats is not None and not seats.empty:
                    seat_frames.append(seats)
                n_ok += 1
                stats["detail_fetched"] += 1
            # —— 按天批量落盘（不要每股重写整个 parquet：60 天 × 72 股 = 4300 次重写会极慢）——
            if detail_rows:
                _upsert_stock_details_bulk(detail_rows)
            if seat_frames:
                stats["seats"] += _append_parquet(
                    _path("lhb_seats.parquet"), pd.concat(seat_frames, ignore_index=True),
                    keys=["date", "stock_code", "side", "rank"], columns=SEAT_COLUMNS)
            have_detail[d] = int(n_ok)
            state["detail"] = have_detail
            save_state(state)

        time.sleep(throttle)

    if verbose:
        print(f"\n完成：新增/更新 {stats['days_fetched']} 天，跳过 {stats['days_skipped']} 天，"
              f"个股 {stats['stocks']} 行，题材明细 {stats['concepts']} 行"
              + (f"，Tier2 {stats['detail_fetched']} 股 / 席位 {stats['seats']} 行" if tier == 2 else "")
              + (f"，失败 {stats['failures']} 次" if stats["failures"] else "")
              + (f"，待发布 {stats['days_pending']} 天（未记录，将重试）" if stats["days_pending"] else ""))
        if stats["aborted"]:
            print("⚠️  因连续失败被中止 —— 已完成的日期已落盘，直接重跑即可续传。")
    return stats


def _upsert_stock_details_bulk(rows: list) -> None:
    """按天批量合并 Tier 2 结果进个股表（**一次读写**，避免每股重写整个 parquet）。

    语义：只覆盖 Tier 2 提供的**非空**字段 ——
    Tier 1 的 `stock_name` / `concepts_raw` / `capitalization` 等必须保留，
    否则会把 Tier 1 的信息抹掉。
    """
    if not rows:
        return
    path = _path("lhb_stocks.parquet")
    old = _read_parquet(path, STOCK_COLUMNS)
    if old is None or old.empty:
        return
    fresh = pd.DataFrame(rows)
    keys = ["date", "stock_code"]
    old_idx = old.set_index(keys)
    fresh_idx = fresh.set_index(keys)
    # 只保留库中存在的键，避免凭空插入新行（Tier2 不应新增标的日子）
    fresh_idx = fresh_idx[fresh_idx.index.isin(old_idx.index)]
    for col in fresh_idx.columns:
        if col in keys or col not in old_idx.columns:
            continue
        vals = fresh_idx[col].dropna()
        if len(vals):
            old_idx.loc[vals.index, col] = vals
    old_idx.reset_index()[STOCK_COLUMNS].to_parquet(path, index=False)


def _upsert_stock_detail(stock_row: dict) -> None:
    """单行版本的 Tier 2 合并（保留给测试与零星修数用）。"""
    _upsert_stock_details_bulk([stock_row])


# ====================================================================
# 覆盖情况体检
# ====================================================================

EARLIEST_DATE = "2025-01-02"      # 实测：zzshare 龙虎榜历史起点（2024-12-31 返回空）


def status() -> dict:
    """返回累积层覆盖概况（供 CLI / 报告使用）。"""
    st = load_state()
    stocks = load_stocks()
    concepts = load_concepts()
    seats = load_seats()
    have = sorted(st.get("list", {}).keys())
    with_data = {d: n for d, n in st.get("list", {}).items() if n}
    zero_days = sorted([d for d, n in st.get("list", {}).items() if not n])

    # 与题材宇宙（plates_list(17)）的 join 命中率 —— 这才是"板块归属能不能用"的判据。
    # ⚠️ 不要拿 concepts 自带的 plate_name 去算命中率：那是 LHB 自己给的，永远 100%，
    #    量不出"编码是否落在题材宇宙内"这件事。
    #    全量实测（2026-10-03，431,537 行 / 822 个编码）：
    #      · plates_list(17) = 520 个编码 → 命中 86.4% 的行，且**命中的都有成分股**
    #        （可用 plates_stocks(17) 取到，用于"上榜率"归一化）；
    #      · 另有 13.6% 的行 / 342 个编码不在该表内，且 **plates_stocks 取不到成分股**
    #        （如 801663 信创、801722 存储、801725 数据要素、801881 DeepSeek、801792 鸿蒙概念）
    #        ⇒ 这些题材**只能给原始上榜家数，不能给上榜率**，面板必须标注，不许编一个率出来。
    #    ⚠️ plate_type=18（541 个）是一套**不同的**分类，不是 17 的超集：
    #       它漏掉了 801001 芯片 / 801085 人工智能 / 801366 业绩增长等高频题材，行覆盖仅 33.7%。
    universe_hit = None
    tt = _read_parquet(_path("theme_table.parquet"), ["plate_code", "plate_name", "plate_type"])
    if tt is not None and not tt.empty and not concepts.empty:
        codes = set(tt["plate_code"].astype(str))
        universe_hit = float(concepts["plate_code"].astype(str).isin(codes).mean())

    return {
        "earliest": EARLIEST_DATE,
        "days_recorded": len(have),
        "days_with_data": len(with_data),
        "first_date": have[0] if have else None,
        "last_date": have[-1] if have else None,
        "stock_rows": int(len(stocks)),
        "concept_rows": int(len(concepts)),
        "seat_rows": int(len(seats)),
        "detail_days": len([d for d, n in st.get("detail", {}).items() if n]),
        "distinct_stocks": int(stocks["stock_code"].nunique()) if not stocks.empty else 0,
        "distinct_plates": int(concepts["plate_code"].nunique()) if not concepts.empty else 0,
        "universe_hit_rate": universe_hit,
        "theme_table_ready": tt is not None and not tt.empty,
        "tier2_row_rate": (float(stocks["detail_fetched"].fillna(False).astype(bool).mean())
                           if not stocks.empty else None),
        # 「记成 0 的交易日」= 潜在空洞。全期日均 71.8 只、最少 46 只，
        # 所以交易日出现 0 几乎不可能是真的 —— 这是静默失败唯一的可见线索。
        "zero_days": zero_days,
    }


def missing_days(start: Optional[str] = None, end: Optional[str] = None) -> list:
    """列出区间内**尚未抓取**的交易日（用于判断还差多少）。"""
    start = _norm_date(start) if start else EARLIEST_DATE
    end = _norm_date(end) if end else _norm_date(_date.today())
    done = set(load_state()["list"].keys())
    return [d for d in trading_days(start, end) if d >= start and d not in done]


__all__ = [
    "LhbStoreError",
    "EARLIEST_DATE",
    "STOCK_COLUMNS",
    "CONCEPT_COLUMNS",
    "SEAT_COLUMNS",
    "PLATE_CONST_COLUMNS",
    "THEME_RANK_COLUMNS",
    "STORE_DIR",
    "store_dir",
    "trading_days",
    "load_state",
    "save_state",
    "load_stocks",
    "load_concepts",
    "load_seats",
    "fetch_theme_table",
    "load_plate_constituents",
    "fetch_plate_constituents",
    "load_theme_rank",
    "fetch_theme_rank",
    "normalize_list_day",
    "normalize_detail",
    "update",
    "status",
    "missing_days",
]
