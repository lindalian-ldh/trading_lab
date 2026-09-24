# -*- coding: utf-8 -*-
"""板块筛选模块（MODE="sector" 使用）。

复用 `services/bankuai-service/data_loader.py` 的 zzshare 板块排名数据，
叠加 akshare 行业指数历史行情计算多因子得分。

输入：全市场申万一级行业板块（plate_type=14）
输出：
    defensive: 前N个防御板块（高股息/低波动/低估值）
    offensive: 前N个进攻板块（高动量/成交活跃）
    excluded:  防御与进攻得分均在中间地带(40-60分位)的板块
    warnings:  因子数据缺失警告

MVP 实现的因子（数据可获取）：
    防御端：
        - 股息率 TTM（akshare 指数估值；缺失用中位数）
        - 60日收益率波动率（akshare 行业指数日线）
        - 估值分位（akshare PE/PB；缺失用中位数）
    进攻端：
        - 12月动量（akshare 行业指数 250 日涨幅）
        - 成交额占比上升（plates_rank.trade_money 近10日趋势）

未实现因子（用中位数填充并打 warning，按 spec MVP 要求）：
    - ROE 稳定性（需财务数据接口）
    - 经营现金流/净利润（需财务数据接口）
    - 营收/净利润增速（需财务数据接口）
    - 研发投入占比（需财务数据接口）
    - 行业景气度 PMI 分项（需宏观数据接口）

24h 本地缓存：screener 输出写入 data/cache/barbell_screener_YYYYMMDD.json，
同一天内多次运行直接读缓存，满足"运行 < 5s"硬约束。
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import barbell_config as cfg
from config.settings import settings
from core.logger import get_logger

log = get_logger("barbell.screener")


# ====================================================================
# 复用 bankuai-service：直接 importlib 加载文件，避免项目 config 模块遮蔽
# bankuai-service 的 data_loader.py 内部 `from config import ScanConfig`，
# 若用 sys.path 注入会被 trading_lab/config/__init__.py 抢占，所以这里
# 用 importlib.util 显式从文件路径加载，并把模块注册到 sys.modules['config']
# 让 data_loader 的内部 import 能解析到 bankuai-service 的 config.py。
# ====================================================================
_BANKUAI_SERVICE_DIR = Path(__file__).resolve().parent.parent / "bankuai-service"
_BK_AVAILABLE = False
_bk_data = None
_percentile_func = None

try:
    import importlib.util

    def _load_module_from_path(name: str, path: Path):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    # 加载 bankuai-service 的 config 模块（独立名，不污染项目 config 包）
    _bk_config_mod = _load_module_from_path(
        "bankuai_bk_config", _BANKUAI_SERVICE_DIR / "config.py")
    # data_loader.py 内部 `from config import ScanConfig`，需临时把 bankuai
    # 的 config 注册为 "config" 让该 import 命中，加载完成后恢复原 config 包
    _orig_config = sys.modules.get("config")
    sys.modules["config"] = _bk_config_mod
    try:
        _bk_data = _load_module_from_path(
            "bankuai_bk_data", _BANKUAI_SERVICE_DIR / "data_loader.py")
        # 同时加载 leader_selector（其内部 from config import ScanConfig / from data_loader import fetch_plate_stocks）
        # 让 data_loader 的 bankuai_bk_data 也以 "data_loader" 名字可被解析
        _orig_data_loader = sys.modules.get("data_loader")
        sys.modules["data_loader"] = _bk_data
        try:
            _ls_mod = _load_module_from_path(
                "bankuai_bk_leader_selector", _BANKUAI_SERVICE_DIR / "leader_selector.py")
            _percentile_func = _ls_mod._percentile
        finally:
            if _orig_data_loader is not None:
                sys.modules["data_loader"] = _orig_data_loader
            else:
                sys.modules.pop("data_loader", None)
    finally:
        # 恢复项目原 config 包（若不恢复会让 from config.settings 失败）
        if _orig_config is not None:
            sys.modules["config"] = _orig_config
        else:
            sys.modules.pop("config", None)
    _BK_AVAILABLE = True
    log.info("已复用 bankuai-service 数据层: %s", _BANKUAI_SERVICE_DIR)
except Exception as e:
    _BK_AVAILABLE = False
    log.warning("bankuai-service 数据层不可用: %s，screener 将退化为仅 akshare 数据源", e)
    # 兜底百分位实现
    def _percentile_func(values, p):
        if not values:
            return 0.0
        s = sorted(values)
        if len(s) == 1:
            return s[0]
        k = (len(s) - 1) * p / 100.0
        f = int(k)
        c = min(f + 1, len(s) - 1)
        if f == c:
            return s[f]
        return s[f] + (s[c] - s[f]) * (k - f)


def _make_bk_config():
    """构造 bankuai-service 的 ScanConfig：plate_type=14（行业板块）。"""
    if not _BK_AVAILABLE:
        return None
    ScanConfig = sys.modules["bankuai_bk_config"].ScanConfig
    return ScanConfig(
        plate_type=14,        # 行业板块
        top_n=50,
        bottom_n=50,
        retry_times=2,
        request_interval=2,
        enable_uplimit_pool=False,
        enable_uplimit_hot=False,
        cache_dir=str(settings.data_dir / "raw" / "bankuai"),
    )


# ====================================================================
# 缓存
# ====================================================================
def _cache_path(today: str) -> Path:
    return settings.data_dir / "cache" / f"barbell_screener_{today.replace('-', '')}.json"


def _load_cache(today: str):
    p = _cache_path(today)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("screener 缓存读取失败: %s", e)
        return None


def _save_cache(today: str, data: dict) -> None:
    try:
        p = _cache_path(today)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("screener 缓存写入 %s", p)
    except Exception as e:
        log.warning("screener 缓存写入失败: %s", e)


# ====================================================================
# 行业板块历史行情：用板块 ETF 日线代理（避开 akshare py_mini_racer/eastmoney）
# ====================================================================
def _fetch_industry_kline(sector_name: str, lookback_days: int = 260) -> pd.DataFrame | None:
    """板块历史行情。

    数据源策略：从 sector_etf_map.SECTOR_ETF_MAP 找该板块主 ETF，
    调 data_fetcher.fetch_sector_etf_kline 拉 ETF 日线作为板块指数代理，
    数据源顺序见 cfg.SECTOR_ETF_SOURCE_ORDER（默认 baostock 主、efinance 备）。
    绕开 akshare 的两个雷区（py_mini_racer dlsym 失败 + eastmoney 接口被断连）。

    ETF 收盘价 ≈ 板块指数走势（误差由跟踪误差控制）；
    ETF 成交额 ≈ 板块流动性（与原 akshare 板块成交额方向一致）。
    """
    try:
        import sector_etf_map as sem
        import data_fetcher as df_mod
        candidates = sem.SECTOR_ETF_MAP.get(sector_name, [])
        if not candidates:
            log.warning("[板块 %s] 无 ETF 映射，无法代理历史行情", sector_name)
            return None
        code6 = str(candidates[0].get("code", "")).zfill(6)
        if not code6:
            return None
        df = df_mod.fetch_sector_etf_kline(code6, days=lookback_days + 30)
        if df is None or len(df) == 0:
            return None
        # fetch_sector_etf_kline 已规范化列名：
        # date/open/high/low/close/volume/amount/pctChg
        # 直接返回，调用方按需取列
        return df
    except Exception as e:
        log.warning("[板块 %s] ETF 代理行情获取失败: %s", sector_name, e)
        return None


def _fetch_industry_valuation_ak() -> dict | None:
    """行业估值快照：板块→{PE, PB}。

    原计划用 akshare.stock_board_industry_summary_ths，但该接口
    在当前环境依赖 py_mini_racer（macOS 上 dlsym mr_eval_context 失败）。
    暂时返回 None，让 val_pct 因子走中位数填充（不阻塞筛选流程）。
    TODO: 后续接入其他估值数据源（如 wind/choice 或成分股加权 PE）。
    """
    log.info("行业估值快照跳过（py_mini_racer 不可用），val_pct 将走中位数填充")
    return None


# ====================================================================
# 因子计算
# ====================================================================
def _calc_volatility_60d(kline: pd.DataFrame) -> float | None:
    """60 日收益率标准差（年化）。"""
    if kline is None or len(kline) < 60:
        return None
    ret = kline["close"].pct_change().dropna().tail(60)
    if len(ret) < 30:
        return None
    return float(np.std(ret, ddof=1) * np.sqrt(252))


def _calc_momentum_250d(kline: pd.DataFrame) -> float | None:
    """250 日累计涨幅（%）。"""
    if kline is None or len(kline) < 60:  # 至少 60 日才计算
        return None
    n = min(len(kline) - 1, 250)
    if n < 30:
        return None
    start_price = float(kline["close"].iloc[-(n + 1)])
    end_price = float(kline["close"].iloc[-1])
    if start_price <= 0:
        return None
    return (end_price / start_price - 1) * 100


def _calc_amount_uptrend(kline: pd.DataFrame, n_days: int = 10) -> bool | None:
    """近 n 日成交额 vs 前 n 日成交额是否上升。"""
    if kline is None or "amount" not in kline.columns:
        return None
    if len(kline) < 2 * n_days:
        return None
    amt = kline["amount"].dropna()
    recent = amt.tail(n_days).mean()
    prev = amt.iloc[-(2 * n_days):-n_days].mean()
    if prev <= 0:
        return None
    return recent > prev


def _calc_valuation_percentile(sector_name: str, valuation: dict | None,
                              all_sectors: list[str]) -> float | None:
    """PE/PB 综合分位（0-100）。缺失返回 None。"""
    if not valuation:
        return None
    pe_list, pb_list = [], []
    for name, v in valuation.items():
        if v.get("pe") is not None and v["pe"] > 0:
            pe_list.append(v["pe"])
        if v.get("pb") is not None and v["pb"] > 0:
            pb_list.append(v["pb"])
    cur = valuation.get(sector_name, {})
    pe = cur.get("pe")
    pb = cur.get("pb")
    if (pe is None or pe <= 0) and (pb is None or pb <= 0):
        return None
    pe_pct = (_percentile_func(pe_list, pe) / max(max(pe_list), 1) * 100) if pe and pe > 0 and pe_list else None
    pb_pct = (_percentile_func(pb_list, pb) / max(max(pb_list), 1) * 100) if pb and pb > 0 and pb_list else None
    vals = [v for v in (pe_pct, pb_pct) if v is not None]
    return float(np.mean(vals)) if vals else None


def _calc_dividend_yield(sector_name: str) -> float | None:
    """板块股息率（%）。复用 data_fetcher.fetch_sector_dividend_yield（中证红利代理）。"""
    try:
        import data_fetcher as df_mod
        return df_mod.fetch_sector_dividend_yield(sector_name)
    except Exception as e:
        log.warning("[板块 %s] 股息率获取失败: %s", sector_name, e)
        return None


# ====================================================================
# 主流程
# ====================================================================
def run_sectors(today: str | None = None) -> dict:
    """板块筛选主入口。

    Returns:
        {
          "defensive": [{"name": "银行", "score": 0.85, "factors": {...},
                          "etf": {...asset dict...}}, ...],
          "offensive": [{"name": "半导体", "score": 0.78, ...}, ...],
          "excluded":  ["综合", ...],
          "warnings":  ["银行: 股息率缺失，用中位数填充", ...],
          "as_of":     "2026-09-17"
        }
    """
    today = today or dt.datetime.now().strftime("%Y-%m-%d")

    # 24h 缓存
    cached = _load_cache(today)
    if cached is not None:
        log.info("screener 命中当日缓存，跳过重算")
        return cached

    p = cfg.SECTOR_PARAMS
    warnings = []
    sectors = list(cfg.SW_L1_SECTORS)

    # ---- 1. 拉取行业板块排名（bankuai-service 复用）----
    plate_data = _fetch_plate_rank(sectors)

    # ---- 2. 拉取行业估值快照 ----
    valuation = _fetch_industry_valuation_ak()

    # ---- 3. 逐板块计算因子 ----
    factor_rows = []
    for name in sectors:
        kline = _fetch_industry_kline(name, lookback_days=p["offensive_momentum_lookback"] + 30)
        if kline is None or len(kline) < 60:
            warnings.append(f"{name}: 行业历史行情不足，部分因子将填中位数")

        vol = _calc_volatility_60d(kline)
        mom = _calc_momentum_250d(kline)
        amt_up = _calc_amount_uptrend(kline, p["offensive_turnover_up_days"])
        val_pct = _calc_valuation_percentile(name, valuation, sectors)
        div = _calc_dividend_yield(name)

        plate_info = plate_data.get(name, {})
        factor_rows.append({
            "name": name,
            "vol_60d": vol,
            "momentum_250d": mom,
            "amount_uptrend": amt_up,
            "val_pct": val_pct,
            "div_yield": div,
            "rank": plate_info.get("rank"),
            "today_change": plate_info.get("change"),
            "today_amount": plate_info.get("amount"),
        })

    # ---- 4. 中位数填充 + 标准化 ----
    df = pd.DataFrame(factor_rows)
    for col in ["vol_60d", "momentum_250d", "val_pct", "div_yield"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
        missing = int(df[col].isna().sum())
        if missing > 0:
            med = df[col].median()
            if pd.isna(med) or med is None:
                med = 0.0
            df[col] = df[col].fillna(float(med))
            warnings.append(f"{col}: {missing} 个板块缺失，已用中位数 {med:.4f} 填充")

    # ---- 5. 计算防御得分与进攻得分（0-1 归一）----
    # 防御得分：高股息 + 低波动 + 低估值
    def_vol_score = _min_max_neg(df["vol_60d"])  # 波动越低得分越高
    def_div_score = _min_max(df["div_yield"])
    def_val_score = _min_max_neg(df["val_pct"])  # 估值分位越低得分越高
    df["defensive_score"] = (def_vol_score * 0.4 + def_div_score * 0.35 +
                              def_val_score * 0.25)

    # 进攻得分：高动量 + 成交额上升
    off_mom_score = _min_max(df["momentum_250d"])
    off_amt_score = df["amount_uptrend"].astype(float)
    df["offensive_score"] = off_mom_score * 0.7 + off_amt_score * 0.3

    # ---- 6. 中间地带排除 ----
    def_pct = _rank_pct(df["defensive_score"])
    off_pct = _rank_pct(df["offensive_score"])
    exclude_mask = ((def_pct >= p["exclude_pct_low"]) & (def_pct <= p["exclude_pct_high"]) &
                    (off_pct >= p["exclude_pct_low"]) & (off_pct <= p["exclude_pct_high"]))
    excluded = df.loc[exclude_mask, "name"].tolist()

    # ---- 7. 选前 N 个（两端必须错开：同一板块不能既是防御端又是进攻端）----
    top_n = p["top_n_sectors"]
    eligible = df[~exclude_mask]
    def_top = eligible.nlargest(top_n, "defensive_score")
    def_names = set(def_top["name"].tolist())

    off_pool = eligible[~eligible["name"].isin(def_names)]
    if len(off_pool) < top_n and len(def_top) > 0:
        # 候选不足时放宽为"至少排除防御端首位"，保证两端不是同一只 ETF
        off_pool = eligible[eligible["name"] != def_top["name"].iloc[0]]
        warnings.append("进攻端候选不足，已放宽为仅排除防御端首位板块")
    off_top = off_pool.nlargest(top_n, "offensive_score")
    if len(off_top) == 0:
        off_top = eligible.nlargest(top_n, "offensive_score")
        warnings.append("进攻端无可用候选，两端可能重叠，请检查板块数据")

    # 为每个板块挂 ETF
    import sector_etf_map as sem
    def_result = [_attach_etf(row, sem) for _, row in def_top.iterrows()]
    off_result = [_attach_etf(row, sem) for _, row in off_top.iterrows()]

    result = {
        "as_of": today,
        "defensive": def_result,
        "offensive": off_result,
        "excluded": excluded,
        "warnings": warnings,
        "factor_summary": {
            "n_sectors_total": len(sectors),
            "n_excluded": len(excluded),
            "n_defensive": len(def_result),
            "n_offensive": len(off_result),
        },
    }

    _save_cache(today, result)
    return result


# ====================================================================
# 辅助函数
# ====================================================================
def _fetch_plate_rank(sectors: list[str]) -> dict:
    """通过 bankuai-service 拉取今日行业板块排名，返回 {板块名: info}。"""
    out = {}
    if not _BK_AVAILABLE or _bk_data is None:
        log.warning("bankuai-service 不可用，跳过板块排名数据")
        return out
    try:
        bk_cfg = _make_bk_config()
        today = dt.datetime.now().strftime("%Y-%m-%d")
        data = _bk_data.fetch_plates_rank(today, bk_cfg, limit=100)
        if not data:
            return out
        for i, p in enumerate(data):
            name = str(p.get("plate_name", ""))
            if not name:
                continue
            out[name] = {
                "rank": i + 1,
                "change": _to_float(p.get("rate")),
                "amount": _to_float(p.get("trade_money")),
                "score": _to_float(p.get("score")),
                "plate_code": str(p.get("plate_code", "")),
            }
        log.info("bankuai-service 板块排名获取 %d 条", len(out))
    except Exception as e:
        log.warning("bankuai-service 板块排名获取异常: %s", e)
    return out


def _attach_etf(row: pd.Series, sem) -> dict:
    """为板块挂上选定的 ETF。"""
    name = str(row["name"])
    try:
        etf = sem.pick_etf(name, cfg.SECTOR_PARAMS)
    except Exception as e:
        log.warning("[板块 %s] ETF 选择失败: %s", name, e)
        etf = {"fallback": "pick_etf_error", "sector": name, "reason": str(e)}
    return {
        "name": name,
        "defensive_score": float(row.get("defensive_score", 0)),
        "offensive_score": float(row.get("offensive_score", 0)),
        "factors": {
            "vol_60d": float(row.get("vol_60d", 0)) if pd.notna(row.get("vol_60d")) else None,
            "momentum_250d": float(row.get("momentum_250d", 0)) if pd.notna(row.get("momentum_250d")) else None,
            "amount_uptrend": bool(row.get("amount_uptrend")) if pd.notna(row.get("amount_uptrend")) else None,
            "val_pct": float(row.get("val_pct", 0)) if pd.notna(row.get("val_pct")) else None,
            "div_yield": float(row.get("div_yield", 0)) if pd.notna(row.get("div_yield")) else None,
            "today_change": float(row.get("today_change")) if pd.notna(row.get("today_change")) else None,
            "today_amount": float(row.get("today_amount")) if pd.notna(row.get("today_amount")) else None,
        },
        "etf": etf,
    }


def _to_float(v, default=0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _min_max(series: pd.Series) -> pd.Series:
    """min-max 归一化到 0-1。"""
    s = pd.to_numeric(series, errors="coerce").fillna(0)
    if s.max() == s.min():
        return pd.Series([0.5] * len(s), index=s.index)
    return (s - s.min()) / (s.max() - s.min())


def _min_max_neg(series: pd.Series) -> pd.Series:
    """反向 min-max：值越小得分越高。"""
    s = pd.to_numeric(series, errors="coerce").fillna(0)
    if s.max() == s.min():
        return pd.Series([0.5] * len(s), index=s.index)
    return (s.max() - s) / (s.max() - s.min())


def _rank_pct(series: pd.Series) -> pd.Series:
    """百分位排名（0-100）。"""
    s = pd.to_numeric(series, errors="coerce").fillna(0)
    return s.rank(pct=True) * 100


if __name__ == "__main__":
    import os
    os.environ["BARBELL_MODE"] = "sector"
    # 简易调试入口
    res = run_sectors()
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
