# -*- coding: utf-8 -*-
"""数据获取模块：baostock（ETF 日线）、efinance（国债/ETF 备用）、akshare（股息率）。
所有获取均带降级，数据不可用时打日志而不崩溃。"""

import datetime as dt
import warnings
from pathlib import Path

import pandas as pd
import numpy as np

import barbell_config as cfg
from config.settings import settings
from core.logger import get_logger

warnings.filterwarnings("ignore")
log = get_logger("barbell.data_fetcher")


# ----------------------------------------------------------------------
# baostock 登录 / 登出
# ----------------------------------------------------------------------
_bs_logged_in = False


def login_baostock():
    """登录 baostock，返回是否成功（已登录时直接返回 True，可重复调用）。"""
    global _bs_logged_in
    if _bs_logged_in:
        return True
    try:
        import baostock as bs
        lg = bs.login()
        if lg.error_code == "0":
            _bs_logged_in = True
            log.info("baostock 登录成功: %s", lg.error_msg)
            return True
        log.warning("baostock 登录失败: %s %s", lg.error_code, lg.error_msg)
        return False
    except Exception as e:
        log.error("baostock 登录异常: %s", e)
        return False


def logout_baostock():
    global _bs_logged_in
    if not _bs_logged_in:
        return
    try:
        import baostock as bs
        bs.logout()
        _bs_logged_in = False
        log.info("baostock 已登出")
    except Exception as e:
        log.error("baostock 登出异常: %s", e)


def ensure_baostock_login() -> bool:
    """确保 baostock 已登录。

    screener/sector_etf_map 会在 main 的登录动作之前调用数据接口，
    这里做懒登录，避免出现 "10001001 you don't login" 这种伪故障。
    """
    return login_baostock()


# ----------------------------------------------------------------------
# 数据源健康状态：efinance(eastmoney) 熔断 + 板块 ETF 日线当日缓存
# ----------------------------------------------------------------------
# 故障背景：efinance 的 get_quote_history 走 eastmoney
# /api/qt/stock/kline/get，该接口在部分网络/本机代理下会被服务端直接断连
# （RemoteDisconnected → requests ProxyError），efinance 的 urllib3 适配器
# 还会自动重试 5 次；sector 模式要拉 31 个板块，逐个重试会刷屏日志并卡住运行。
# 因此这里做三件事：会话加固（忽略代理/关重试/短超时）、失败熔断、当日本地缓存。
_EFINANCE_DISABLED = False
_EFINANCE_DISABLED_REASON = ""
_EFINANCE_PREPARED = False

_CONN_ERROR_MARKS = (
    "ProxyError", "ConnectionError", "RemoteDisconnected", "MaxRetryError",
    "ConnectTimeout", "ReadTimeout", "NewConnectionError", "ProtocolError",
)


def _is_conn_error(e: Exception) -> bool:
    """异常是否属于连接/代理层不可用（可熔断，无需逐个重试）。"""
    txt = f"{type(e).__name__}: {e}"
    return any(m in txt for m in _CONN_ERROR_MARKS)


def efinance_available() -> bool:
    """efinance 数据源当前是否可用（已被熔断则返回 False）。"""
    return not _EFINANCE_DISABLED


def disable_efinance(reason: str) -> None:
    """熔断 efinance：本次进程内不再尝试，直接走备源。"""
    global _EFINANCE_DISABLED, _EFINANCE_DISABLED_REASON
    if _EFINANCE_DISABLED:
        return
    _EFINANCE_DISABLED = True
    _EFINANCE_DISABLED_REASON = reason
    log.warning("efinance(eastmoney) 已熔断，本次运行后续请求直接走备源。原因: %s", reason)


def _prepare_efinance():
    """导入 efinance 并做一次性加固；任何一步失败都保持默认行为、不影响流程。

    加固内容：
        1. trust_env=False + 清空 proxies：不再复用 HTTP(S)_PROXY / macOS 系统代理
           （本机代理会直接断连，表现为 ProxyError 连环重试）
        2. 关闭 urllib3 自动重试、超时收到 5/10s：失败快速返回，交给上层降级
    """
    global _EFINANCE_PREPARED
    if _EFINANCE_DISABLED:
        return None
    import efinance as ef
    if _EFINANCE_PREPARED:
        return ef
    _EFINANCE_PREPARED = True
    try:
        import requests
        sess = ef.shared.session
        sess.trust_env = False
        sess.proxies = {}
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4,
                                                max_retries=0)
        sess.mount("http://", adapter)
        sess.mount("https://", adapter)
        _orig_request = sess.request

        def _request(*args, **kwargs):
            kwargs.setdefault("timeout", (5, 10))  # 覆盖 efinance 默认的 180s
            return _orig_request(*args, **kwargs)

        sess.request = _request
        log.info("efinance 会话已加固（忽略代理 / 关闭自动重试 / 超时 5-10s）")
    except Exception as e:
        log.warning("efinance 会话加固失败（保持默认行为）: %s", e)
    return ef


# ----------------------------------------------------------------------
# 板块 ETF 日线当日本地缓存
# ----------------------------------------------------------------------
def _etf_cache_path(code6: str, days: int) -> Path:
    day = dt.datetime.now().strftime("%Y%m%d")
    return settings.data_dir / "cache" / f"barbell_etf_{code6}_{int(days)}_{day}.csv"


def _load_etf_cache(code6: str, days: int):
    """读取当日板块 ETF 日线缓存；无缓存或读取失败返回 None。"""
    if not cfg.ETF_KLINE_CACHE:
        return None
    p = _etf_cache_path(code6, days)
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p, dtype={"date": str})
        if df is None or len(df) == 0 or "close" not in df.columns:
            return None
        return df
    except Exception as e:
        log.warning("[板块ETF %s] 缓存读取失败: %s", code6, e)
        return None


def _save_etf_cache(code6: str, days: int, df) -> None:
    """写入当日板块 ETF 日线缓存（best-effort，失败不影响流程）。"""
    if not cfg.ETF_KLINE_CACHE:
        return
    try:
        p = _etf_cache_path(code6, days)
        p.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(p, index=False)
    except Exception as e:
        log.warning("[板块ETF %s] 缓存写入失败: %s", code6, e)


# ----------------------------------------------------------------------
# ETF 日线数据（baostock 主，efinance 备）
# ----------------------------------------------------------------------
def fetch_etf_kline(asset, days=None):
    """获取 ETF 前复权日线数据。

    Args:
        asset: 资产字典，含 baostock_code / efinance_code / name
        days: 拉取交易日数量，默认 cfg.DATA_FETCH_DAYS

    Returns:
        DataFrame[date, open, high, low, close, volume, amount, pctChg]
        失败返回 None。
    """
    if days is None:
        days = cfg.DATA_FETCH_DAYS
    df = _fetch_etf_baostock(asset, days)
    if df is None or len(df) == 0:
        log.warning("[%s] baostock 不可用，尝试 efinance 降级...", asset['name'])
        df = _fetch_etf_efinance(asset, days)
    if df is None or len(df) == 0:
        log.error("[%s] 数据获取失败，baostock 与 efinance 均不可用。", asset['name'])
        return None
    df = df.sort_values("date").reset_index(drop=True)
    df = df.ffill().dropna(subset=["close"])
    log.info("[%s] 获取 %d 条日线，区间 %s ~ %s",
             asset['name'], len(df), df['date'].iloc[0], df['date'].iloc[-1])
    return df


def _fetch_etf_baostock(asset, days):
    if not ensure_baostock_login():
        log.warning("baostock 未登录，无法获取 %s", asset.get("name"))
        return None
    try:
        import baostock as bs
        end = dt.datetime.now().strftime("%Y-%m-%d")
        start = (dt.datetime.now() - dt.timedelta(days=days + 60)).strftime("%Y-%m-%d")
        rs = bs.query_history_k_data_plus(
            asset["baostock_code"],
            "date,code,open,high,low,close,volume,amount,pctChg",
            start_date=start, end_date=end,
            frequency="d", adjustflag="2",  # 前复权
        )
        if rs.error_code != "0":
            log.warning("baostock query error: %s %s", rs.error_code, rs.error_msg)
            return None
        data = []
        while (rs.error_code == "0") and rs.next():
            data.append(rs.get_row_data())
        if not data:
            return None
        df = pd.DataFrame(data, columns=rs.fields)
        for col in ["open", "high", "low", "close", "volume", "amount", "pctChg"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df[["date", "open", "high", "low", "close", "volume", "amount", "pctChg"]]
    except Exception as e:
        log.error("baostock %s 获取异常: %s", asset['name'], e)
        return None


def _fetch_etf_efinance(asset, days):
    try:
        ef = _prepare_efinance()
        if ef is None:
            return None
        # fbs=1 前复权；klt=101 日线
        df = ef.stock.get_quote_history(asset["efinance_code"], klt=101, fqt=1)
        if df is None or len(df) == 0:
            return None
        # efinance 列名统一
        col_map = {"日期": "date", "开盘": "open", "最高": "high", "最低": "low",
                   "收盘": "close", "成交量": "volume", "成交额": "amount",
                   "涨跌幅": "pctChg"}
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        if "date" not in df.columns:
            return None
        for col in ["open", "high", "low", "close", "volume", "amount", "pctChg"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        need = ["date", "open", "high", "low", "close", "volume", "amount", "pctChg"]
        return df[[c for c in need if c in df.columns]]
    except Exception as e:
        if _is_conn_error(e):
            disable_efinance(f"{type(e).__name__}: {str(e)[:200]}")
        else:
            log.error("efinance %s 获取异常: %s", asset['name'], e)
        return None


# ----------------------------------------------------------------------
# 10 年期国债收益率（akshare 主，efinance 备，ETF 分位数最终降级）
# ----------------------------------------------------------------------
def fetch_treasury_yield():
    """返回最新 10 年期国债收益率（%）。失败返回 None。"""
    val = _treasury_akshare()
    if val is not None:
        log.info("国债收益率 akshare 获取: %.3f%%", val)
        return val
    val = _treasury_efinance()
    if val is not None:
        log.info("国债收益率 efinance 获取: %.3f%%", val)
        return val
    log.warning("国债收益率全部源失败，将使用 ETF 分位数代理降级。")
    return None


def _treasury_akshare():
    try:
        import akshare as ak
        # 回退最近 15 天以保证有数据（当日可能未更新）
        end = dt.datetime.now().strftime("%Y%m%d")
        start = (dt.datetime.now() - dt.timedelta(days=15)).strftime("%Y%m%d")
        df = ak.bond_china_yield(start_date=start, end_date=end)
        if df is None or len(df) == 0:
            return None
        # 列结构：曲线名称 / 日期 / 3月 / 6月 / 1年 / 3年 / 5年 / 7年 / 10年 / 30年
        # 筛选中债国债收益率曲线
        if "曲线名称" in df.columns:
            df = df[df["曲线名称"].astype(str).str.contains("国债")]
        if len(df) == 0:
            return None
        # 取 "10年" 列
        col = "10年" if "10年" in df.columns else None
        if col is None:
            for c in df.columns:
                if "10" in str(c):
                    col = c
                    break
        if col is None:
            return None
        return float(df[col].dropna().iloc[-1])
    except Exception as e:
        log.error("akshare 国债收益率获取异常: %s", e)
        return None


def _treasury_efinance():
    try:
        import efinance as ef
        if hasattr(ef, "bond"):
            df = ef.bond.get_quote_history("中国国债收益率10年")
            if df is not None and len(df) > 0 and "收盘" in df.columns:
                return float(df["收盘"].dropna().iloc[-1])
    except Exception as e:
        log.error("efinance 国债收益率获取异常: %s", e)
    return None


# ----------------------------------------------------------------------
# 中证红利股息率（akshare 主，4.5% 固定降级）
# ----------------------------------------------------------------------
def fetch_dividend_yield():
    """返回中证红利股息率（%）。失败降级为 4.5。"""
    val = _dividend_akshare()
    if val is not None:
        log.info("股息率 akshare 获取: %.3f%%", val)
        return val
    log.warning("股息率降级使用固定值 %.1f%%", cfg.DIVIDEND_YIELD_FALLBACK)
    return cfg.DIVIDEND_YIELD_FALLBACK


_DIVIDEND_MEMO = {"done": False, "value": None}


def _dividend_akshare():
    """中证红利指数股息率（%）代理。进程内缓存，避免 31 个板块重复请求。"""
    if _DIVIDEND_MEMO["done"]:
        return _DIVIDEND_MEMO["value"]
    val = _dividend_akshare_fetch()
    _DIVIDEND_MEMO["done"] = True
    _DIVIDEND_MEMO["value"] = val
    return val


def _dividend_akshare_fetch():
    try:
        import akshare as ak
        # 中证红利指数估值（指数代码 000922），返回列含 股息率1/股息率2
        df = ak.stock_zh_index_value_csindex(symbol="000922")
        if df is None or len(df) == 0:
            return None
        for col in ["股息率1", "股息率2"]:
            if col in df.columns:
                v = pd.to_numeric(df[col], errors="coerce").dropna()
                if len(v) > 0:
                    return float(v.iloc[-1])
        # 通用回退：任意含 "股息率" 的列
        for col in df.columns:
            if "股息率" in str(col):
                v = pd.to_numeric(df[col], errors="coerce").dropna()
                if len(v) > 0:
                    return float(v.iloc[-1])
    except Exception as e:
        log.error("akshare 股息率获取异常: %s", e)
    return None


# ----------------------------------------------------------------------
# ETF 价格分位数代理（国债收益率最终降级用）
# ----------------------------------------------------------------------
def compute_defensive_price_percentile(def_df):
    """中证红利 ETF 当前价格在过去 N 日的分位数，作为防御端性价比代理。
    分位数越低（价格越便宜），防御性价比越高。返回 0~100。"""
    if def_df is None or len(def_df) == 0:
        return None
    lookback = min(len(def_df), cfg.ZSCORE_LOOKBACK * 2)
    recent = def_df["close"].tail(lookback)
    cur = recent.iloc[-1]
    pct = (recent <= cur).sum() / len(recent) * 100
    log.info("防御端代理: 当前价格 %.4f 在近 %d 日分位数: %.1f%%", cur, lookback, pct)
    return float(pct)


# ----------------------------------------------------------------------
# 板块哑铃扩展：板块 ETF 行情 / 板块指数 / 板块股息率 / ETF 流动性
# ----------------------------------------------------------------------
def fetch_sector_etf_kline(etf_code, days=None):
    """板块 ETF 日线。数据源顺序由 cfg.SECTOR_ETF_SOURCE_ORDER 决定。

    默认 baostock 主源、efinance 备源：eastmoney 的 kline 接口在部分网络/代理下
    会被服务端直接断连，而 baostock 同类数据可用。顺序可用环境变量覆盖：
        BARBELL_ETF_SOURCE=efinance,baostock

    Args:
        etf_code: 6 位 ETF 代码（如 "512800"）
        days: 拉取交易日数，默认 cfg.DATA_FETCH_DAYS

    Returns:
        DataFrame[date, open, high, low, close, volume, amount, pctChg]；失败 None
    """
    if days is None:
        days = cfg.DATA_FETCH_DAYS
    code6 = str(etf_code).zfill(6)

    cached = _load_etf_cache(code6, days)
    if cached is not None:
        log.info("[板块ETF %s] 命中当日本地缓存 %d 条", code6, len(cached))
        return cached

    asset = {"name": f"sector_etf_{code6}",
             "baostock_code": _etf_code_to_baostock(code6),
             "efinance_code": code6}

    df = None
    for source in cfg.SECTOR_ETF_SOURCE_ORDER:
        if source == "baostock":
            df = _fetch_etf_baostock(asset, days)
        elif source == "efinance":
            if not efinance_available():
                log.info("[板块ETF %s] efinance 已熔断，跳过", code6)
                continue
            df = _fetch_etf_efinance(asset, days)
        else:
            log.warning("[板块ETF %s] 未知数据源 %s，已忽略", code6, source)
            continue
        if df is not None and len(df) > 0:
            log.info("[板块ETF %s] 日线来源=%s", code6, source)
            break
        log.warning("[板块ETF %s] %s 源不可用，尝试下一个源...", code6, source)

    if df is None or len(df) == 0:
        log.error("[板块ETF %s] 数据获取失败（数据源顺序 %s）",
                  code6, cfg.SECTOR_ETF_SOURCE_ORDER)
        return None
    df = df.sort_values("date").reset_index(drop=True)
    df = df.ffill().dropna(subset=["close"])
    _save_etf_cache(code6, days, df)
    log.info("[板块ETF %s] 获取 %d 条日线", code6, len(df))
    return df


def _etf_code_to_baostock(code6):
    """6 位代码 → baostock sh./sz. 前缀。"""
    code6 = str(code6).zfill(6)
    if code6.startswith(("51", "52", "58", "50")):
        return f"sh.{code6}"
    if code6.startswith(("15", "16")):
        return f"sz.{code6}"
    return f"sh.{code6}"


def fetch_sector_index_kline(sector_name, days=None):
    """板块（申万一级行业）指数日线。用 akshare stock_board_industry_hist_em。

    Returns:
        DataFrame[date, close, pctChg, amount]；失败 None
    """
    if days is None:
        days = cfg.DATA_FETCH_DAYS
    try:
        import akshare as ak
        end = dt.datetime.now().strftime("%Y%m%d")
        start = (dt.datetime.now() - dt.timedelta(days=days + 60)).strftime("%Y%m%d")
        df = ak.stock_board_industry_hist_em(symbol=sector_name, start_date=start,
                                              end_date=end, period="日", adjust="")
        if df is None or len(df) == 0:
            return None
        col_map = {"日期": "date", "收盘": "close", "涨跌幅": "pctChg",
                   "成交额": "amount"}
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        if "close" not in df.columns:
            return None
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        for c in ["close", "pctChg", "amount"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return df
    except Exception as e:
        log.warning("[板块 %s] akshare 指数获取失败: %s", sector_name, e)
        return None


def fetch_sector_dividend_yield(sector_name, constituents=None):
    """板块加权股息率（%）。

    MVP 策略：
        1. 优先 akshare 中证红利指数股息率（防御端代理）
        2. 失败降级用 cfg.DIVIDEND_YIELD_FALLBACK
    TODO: 后续接入成分股加权计算
    """
    val = _dividend_akshare()
    if val is not None:
        log.info("[板块 %s] 股息率采用中证红利代理: %.3f%%", sector_name, val)
        return val
    log.warning("[板块 %s] 股息率降级使用固定值 %.1f%%", sector_name, cfg.DIVIDEND_YIELD_FALLBACK)
    return cfg.DIVIDEND_YIELD_FALLBACK


def compute_etf_liquidity(etf_code, lookback=20):
    """ETF 流动性快照。

    数据源与板块 ETF 日线一致（fetch_sector_etf_kline，默认 baostock 主 / efinance 备），
    这样 eastmoney kline 不可用时流动性过滤仍能工作——否则 pick_etf 全部降级成
    fallback，sector 模式会整体退回 style 默认资产池。

    Args:
        etf_code: 6 位 ETF 代码
        lookback: 取近 N 日均值

    Returns:
        {"avg_amount": float, "fund_size": float, "track_err": None}
        失败返回 None
    """
    code6 = str(etf_code).zfill(6)
    df = fetch_sector_etf_kline(code6, days=max(90, lookback * 3))
    if df is None or len(df) == 0 or "amount" not in df.columns:
        log.warning("[ETF %s] 流动性快照不可用（无成交额数据）", code6)
        return None

    amt = pd.to_numeric(df["amount"], errors="coerce").dropna()
    recent = amt.tail(lookback)
    if len(recent) == 0:
        return None
    avg_amount = float(recent.mean())

    # 基金规模：行情带市值列时直接用，否则用日均成交额粗估
    # （baostock ETF 日线没有市值列，走估算代理；精确规模需单独接口）
    fund_size = None
    for c in df.columns:
        if "市值" in str(c):
            v = pd.to_numeric(df[c].iloc[-1], errors="coerce")
            if pd.notna(v) and v > 0:
                fund_size = float(v)
                break
    if fund_size is None:
        fund_size = avg_amount * 60

    return {
        "avg_amount": avg_amount,
        "fund_size": fund_size,
        "track_err": None,  # TODO: 接入跟踪误差数据
    }
