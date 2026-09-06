"""yanbao-info 数据获取层。

职责:
    1. fetch_report_list   调用 ak.stock_research_report_em 获取研报列表
    2. download_pdf       下载研报 PDF 到 data/yanbao/pdfs/
    3. fetch_current_price  获取当前股价（Tencent 主 + adata 兜底）
    4. fetch_consensus_forecast  调用 ak.stock_profit_forecast_ths 获取一致预期

关键设计:
    - 限频保护：相邻请求间隔 ≥ request_interval（复用 crawler_news _rate_limit 模式）
    - 失败重试（仅网络异常）：retry_times 次；业务级空结果不重试
    - "未找到研报"语义：akshare 返回空 DataFrame 时返回 status="no_report_found"，
      不降级、不重试（用户硬要求）
    - 原始响应落盘到 data/yanbao/cache/，便于复盘
    - PDF 下载带 User-Agent + Referer 防盗链
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date
from pathlib import Path
from typing import Any, Optional

import pandas as pd

try:
    from config import YanbaoConfig
except ImportError:
    # 兼容直接从 yanbao-info 目录运行（sys.path 未注入项目根时）
    from .config import YanbaoConfig

logger = logging.getLogger(__name__)


# ====================================================================
# 限频装饰器（复用 crawler_news _rate_limit 模式）
# ====================================================================

_last_call_time: float = 0.0


def _rate_limit(cfg: YanbaoConfig) -> None:
    """确保相邻两次接口调用间隔 ≥ request_interval。"""
    global _last_call_time
    elapsed = time.time() - _last_call_time
    if elapsed < cfg.request_interval:
        time.sleep(cfg.request_interval - elapsed)
    _last_call_time = time.time()


# ====================================================================
# 工具函数
# ====================================================================


def _safe_str(val) -> str:
    """将单元格值转为字符串，NaN/NaT/None 转为空字符串。"""
    if val is None:
        return ""
    try:
        if isinstance(val, float) and pd.isna(val):
            return ""
        if pd.isna(val):
            return ""
    except (TypeError, ValueError):
        pass
    return str(val).strip()


def _parse_date(val) -> str:
    """将日期/时间单元格解析为 YYYY-MM-DD 格式字符串。"""
    if val is None:
        return ""
    try:
        if isinstance(val, date):
            return val.strftime("%Y-%m-%d")
        if isinstance(val, pd.Timestamp):
            return val.strftime("%Y-%m-%d")
        s = str(val).strip()
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return s[:10]
        ts = pd.to_datetime(s, errors="coerce")
        if pd.notna(ts):
            return ts.strftime("%Y-%m-%d")
        return ""
    except Exception:
        return ""


def _cache_path(cfg: YanbaoConfig, symbol: str, today: str) -> Path:
    """返回研报列表原始响应的缓存路径。"""
    d = cfg.cache_dir_path()
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{symbol}_list_{today}.json"


def _save_list_cache(cfg: YanbaoConfig, symbol: str, today: str, records: list) -> Path:
    """把研报列表原始响应写入 JSON 缓存。"""
    path = _cache_path(cfg, symbol, today)
    try:
        path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("写入研报列表缓存失败 %s: %s", path, e)
    return path


# ====================================================================
# 1. 研报列表获取
# ====================================================================


def fetch_report_list(symbol: str, cfg: YanbaoConfig) -> dict:
    """调用 ak.stock_research_report_em，返回包含研报列表与状态的 dict。

    返回结构:
        {
            "status": "ok" | "no_report_found" | "fetch_failed",
            "reason": str,
            "reports": list[dict],   # 按 publish_date 倒序
            "source": "api" | "cache",
        }

    行为（对齐用户硬要求"找不到研报不过度尝试"）:
        - akshare 调用成功且有数据 → status="ok"，原始响应落盘
        - akshare 返回空 DataFrame（业务级空结果）→ status="no_report_found"，不重试、不降级
        - akshare 抛数据结构异常（KeyError/AttributeError/IndexError，如无效股票代码导致
          API 返回空 JSON，akshare 访问 infoCode 列时抛 KeyError）→ status="no_report_found"，
          不重试（业务级空结果）
        - akshare 抛网络异常/超时 → 重试 retry_times 次；全部失败 → status="fetch_failed"，
          不降级读跨日缓存
    """
    today = date.today().isoformat()

    last_err: Optional[Exception] = None
    for attempt in range(cfg.retry_times + 1):
        try:
            _rate_limit(cfg)
            logger.info("调用 ak.stock_research_report_em(symbol=%s) 第 %d 次尝试",
                       symbol, attempt + 1)
            import akshare as ak
            df = ak.stock_research_report_em(symbol=symbol)

            if df is None or df.empty:
                logger.warning("akshare 返回空 DataFrame，视为未找到研报")
                return {
                    "status": "no_report_found",
                    "reason": "akshare 返回空 DataFrame（该股票近期无券商研报覆盖或代码错误）",
                    "reports": [],
                    "source": "api",
                }

            # 标准化字段
            reports = _normalize_report_list(df, symbol)

            # 落盘原始响应
            _save_list_cache(cfg, symbol, today, reports)

            logger.info("研报列表获取成功：%d 条", len(reports))
            return {
                "status": "ok",
                "reason": f"成功获取 {len(reports)} 条研报",
                "reports": reports,
                "source": "api",
            }

        except (KeyError, AttributeError, IndexError) as e:
            # 数据结构异常：通常是无效股票代码导致 API 返回空 JSON，
            # akshare 访问缺失字段时抛出。视为业务级空结果，不重试。
            logger.warning("akshare 数据结构异常（视为未找到研报）: %s", e)
            return {
                "status": "no_report_found",
                "reason": f"股票代码 {symbol} 无效或无研报覆盖（akshare 数据结构异常: {e}）",
                "reports": [],
                "source": "api",
            }

        except Exception as e:
            last_err = e
            logger.warning("第 %d 次尝试失败：%s", attempt + 1, e)
            if attempt < cfg.retry_times:
                time.sleep(cfg.request_interval)  # 重试前等待
            continue

    # 全部重试失败
    logger.error("研报列表获取失败：%s", last_err)
    return {
        "status": "fetch_failed",
        "reason": f"网络异常重试 {cfg.retry_times + 1} 次后仍失败：{last_err}",
        "reports": [],
        "source": "api",
    }


def _normalize_report_list(df: pd.DataFrame, symbol: str) -> list[dict]:
    """将 akshare 返回的 DataFrame 标准化为按 publish_date 倒序的 dict 列表。

    akshare 返回列（实测字段）:
        序号 / 股票代码 / 股票简称 / 报告名称 / 东财评级 / 机构 / 近一月个股研报数 /
        {year}-盈利预测-收益 / {year}-盈利预测-市盈率 / 行业 / 日期 / 报告PDF链接
    """
    reports: list[dict] = []
    for _, row in df.iterrows():
        rd = row.to_dict()
        # 提取盈利预测字段（列名形如 "2024-盈利预测-收益"）
        eps_forecast = {}
        pe_forecast = {}
        for col in df.columns:
            if "盈利预测-收益" in col:
                year = col.split("-")[0]
                eps_forecast[year] = _safe_str(rd.get(col))
            elif "盈利预测-市盈率" in col:
                year = col.split("-")[0]
                pe_forecast[year] = _safe_str(rd.get(col))

        item = {
            "title": _safe_str(rd.get("报告名称")),
            "stock_code": _safe_str(rd.get("股票代码")) or symbol,
            "stock_name": _safe_str(rd.get("股票简称")),
            "org_name": _safe_str(rd.get("机构")),
            "publish_date": _parse_date(rd.get("日期")),
            "rating": _safe_str(rd.get("东财评级")),
            "industry": _safe_str(rd.get("行业")),
            "pdf_url": _safe_str(rd.get("报告PDF链接")),
            "research_report_count": _safe_str(rd.get("近一月个股研报数")),
            "eps_forecast": eps_forecast,
            "pe_forecast": pe_forecast,
        }
        # 跳过无 PDF 链接的研报（无法解析）
        if not item["pdf_url"]:
            logger.warning("跳过无 PDF 链接研报: %s - %s", item["publish_date"], item["title"][:50])
            continue
        reports.append(item)

    # 按发布日期倒序
    reports.sort(key=lambda x: x["publish_date"], reverse=True)
    return reports


# ====================================================================
# 2. PDF 下载
# ====================================================================


def _pdf_cache_path(cfg: YanbaoConfig, symbol: str, publish_date: str, org: str) -> Path:
    """返回 PDF 缓存路径: data/yanbao/pdfs/{symbol}_{date}_{org}.pdf

    org 可能含特殊字符（如"/"），需清洗为安全文件名。
    """
    safe_org = re.sub(r"[^\w\u4e00-\u9fa5]", "_", org)[:20]
    d = cfg.pdfs_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{symbol}_{publish_date}_{safe_org}.pdf"


def download_pdf(
    url: str,
    symbol: str,
    publish_date: str,
    org: str,
    cfg: YanbaoConfig,
) -> Optional[Path]:
    """下载 PDF 到 data/yanbao/pdfs/，返回本地路径；失败返回 None。

    - 缓存命中检测：文件存在且 size > 0 跳过下载
    - 模拟浏览器 UA + Referer 防盗链
    - 重试 retry_times 次（仅网络异常重试），失败返回 None（不抛异常）
    """
    out_path = _pdf_cache_path(cfg, symbol, publish_date, org)

    # 缓存命中
    if out_path.exists() and out_path.stat().st_size > 0:
        logger.info("PDF 缓存命中，跳过下载: %s", out_path.name)
        return out_path

    headers = {
        "User-Agent": cfg.pdf_user_agent,
        "Referer": cfg.pdf_referer,
        "Accept": "application/pdf,*/*",
    }

    last_err: Optional[Exception] = None
    for attempt in range(cfg.retry_times + 1):
        try:
            _rate_limit(cfg)
            logger.info("下载 PDF 第 %d 次尝试: %s", attempt + 1, url)
            import requests
            r = requests.get(
                url,
                headers=headers,
                timeout=cfg.request_timeout,
                stream=True,
            )
            r.raise_for_status()

            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            if out_path.stat().st_size == 0:
                raise IOError("下载文件大小为 0")

            logger.info("PDF 下载成功: %s (%.1f KB)",
                       out_path.name, out_path.stat().st_size / 1024)
            return out_path

        except Exception as e:
            last_err = e
            logger.warning("第 %d 次下载失败：%s", attempt + 1, e)
            # 清理可能的部分下载文件
            if out_path.exists():
                try:
                    out_path.unlink()
                except Exception:
                    pass
            if attempt < cfg.retry_times:
                time.sleep(cfg.request_interval)
            continue

    logger.error("PDF 下载失败：%s", last_err)
    return None


# ====================================================================
# 3. 当前股价获取（Tencent 主 + adata 兜底）
# ====================================================================


def _tencent_market_prefix(symbol: str) -> str:
    """根据股票代码前缀返回 Tencent 行情接口的市场前缀。

    沪市: 6 开头 → sh
    深市: 0/3 开头 → sz
    北交所: 8/4 开头 → bj（Tencent 不一定支持，但尝试）
    """
    if symbol.startswith("6"):
        return "sh"
    if symbol.startswith(("0", "3")):
        return "sz"
    if symbol.startswith(("8", "4")):
        return "bj"
    return "sh"  # 默认


def fetch_current_price(symbol: str, cfg: YanbaoConfig) -> Optional[float]:
    """获取当前股价（Tencent 主 + adata 兜底）。

    - Tencent: https://qt.gtimg.cn/q={market}{code}
    - 失败返回 None（不重试，当前股价非主流程必需）

    akshare 的 stock_zh_a_spot_em 已弃用（project_memory 中明确不稳定）。
    """
    # 1. Tencent 主源
    try:
        _rate_limit(cfg)
        market = _tencent_market_prefix(symbol)
        url = cfg.tencent_quote_url.format(symbol=f"{market}{symbol}")
        logger.info("Tencent 行情请求: %s", url)
        import requests
        r = requests.get(url, timeout=cfg.request_timeout)
        r.raise_for_status()
        text = r.text
        # Tencent 返回形如: v_sh600036="1~招商银行~600036~32.50~..."
        # 第 4 个字段为当前价
        parts = text.split("~")
        if len(parts) > 3:
            price_str = parts[3].strip()
            try:
                price = float(price_str)
                if price > 0:
                    logger.info("Tencent 当前股价: %s = %.2f", symbol, price)
                    return price
            except ValueError:
                pass
        logger.warning("Tencent 行情解析失败: %s", text[:100])
    except Exception as e:
        logger.warning("Tencent 行情请求失败：%s", e)

    # 2. adata 兜底
    try:
        _rate_limit(cfg)
        logger.info("adata 行情兜底请求: %s", symbol)
        import adata
        df = adata.stock.market.market_snapshot(code=symbol)
        if df is not None and not df.empty:
            # 优先取收盘价或最新价
            for col in ["最新价", "close", "收盘价", "price"]:
                if col in df.columns:
                    val = df.iloc[0].get(col)
                    if val is not None:
                        try:
                            price = float(val)
                            if price > 0:
                                logger.info("adata 当前股价: %s = %.2f", symbol, price)
                                return price
                        except (ValueError, TypeError):
                            continue
        logger.warning("adata 行情无可用价格列")
    except Exception as e:
        logger.warning("adata 行情请求失败：%s", e)

    logger.warning("无法获取当前股价: %s", symbol)
    return None


# ====================================================================
# 4. 一致预期获取（ak.stock_profit_forecast_ths）
# ====================================================================


def fetch_consensus_forecast(symbol: str, cfg: YanbaoConfig) -> dict:
    """调用 ak.stock_profit_forecast_ths(symbol, indicator="预测年报净利润")

    返回 {year: consensus_net_profit_str, ...} 用于"盈利预测上调"判断。
    - 失败时返回空 dict，signal_judge 标注"无法获取一致预期"
    - 不重试（一致预期非主流程必需）

    同花顺返回列（实测）:
        预测年度 / 预测机构数 / 预测净利润均值 / 预测净利润最小 / 预测净利润最大 /
        预测每股收益均值 / 上年每股收益 / ...
    """
    try:
        _rate_limit(cfg)
        logger.info("调用 ak.stock_profit_forecast_ths(symbol=%s, indicator=%s)",
                   symbol, cfg.consensus_indicator)
        import akshare as ak
        df = ak.stock_profit_forecast_ths(
            symbol=symbol,
            indicator=cfg.consensus_indicator,
        )

        if df is None or df.empty:
            logger.warning("一致预期返回空 DataFrame")
            return {}

        consensus: dict = {}
        # 实测列名可能漂移，按关键词匹配
        # stock_profit_forecast_ths indicator="预测年报净利润" 实测列名:
        # ['年度', '预测机构数', '最小值', '均值', '最大值', '行业平均数']
        year_col = _find_col(df, ["预测年度", "年度", "年份"])
        value_col = _find_col(df, ["预测净利润均值", "净利润均值", "预测净利润", "均值", "最小值", "最大值"])

        if year_col is None or value_col is None:
            logger.warning("一致预期列名未匹配: %s", list(df.columns))
            return {}

        for _, row in df.iterrows():
            year = _safe_str(row.get(year_col))
            val = _safe_str(row.get(value_col))
            if year and val:
                consensus[year] = val

        logger.info("一致预期获取成功: %s", consensus)
        return consensus

    except Exception as e:
        logger.warning("一致预期获取失败：%s", e)
        return {}


def _find_col(df: pd.DataFrame, candidates: list) -> Optional[str]:
    """在 DataFrame 列名中查找第一个匹配候选关键词的列。

    按候选优先级遍历（外层候选，内层列）：candidates[0] 优先级最高，
    确保如"均值"优先于"最小值"/"最大值"。
    """
    for cand in candidates:
        for col in df.columns:
            if cand in str(col):
                return col
    return None


# ====================================================================
# 5. 历史快照加载与更新（用于评级跳升判断）
# ====================================================================


def _history_path(cfg: YanbaoConfig, symbol: str) -> Path:
    """返回历史快照路径: data/yanbao/history/{symbol}.json"""
    d = cfg.history_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{symbol}.json"


def load_history(cfg: YanbaoConfig, symbol: str) -> list:
    """加载该股票的历史报告快照列表。返回空列表如果无历史。"""
    path = _history_path(cfg, symbol)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except Exception as e:
        logger.warning("历史快照读取失败 %s: %s", path, e)
    return []


def save_history_snapshot(cfg: YanbaoConfig, symbol: str, snapshot: dict) -> None:
    """追加本次报告快照到历史文件，保留最近 history_keep_count 份。

    snapshot 应包含 publish_date / org_name / rating / target_price 等关键字段。
    """
    history = load_history(cfg, symbol)
    # 去重：同机构同日期的报告覆盖旧记录
    dedup_key = (snapshot.get("org_name", ""), snapshot.get("publish_date", ""))
    history = [
        h for h in history
        if (h.get("org_name", ""), h.get("publish_date", "")) != dedup_key
    ]
    history.append(snapshot)
    # 按 publish_date 倒序，保留最近 N 份
    history.sort(key=lambda x: x.get("publish_date", ""), reverse=True)
    history = history[:cfg.history_keep_count]

    path = _history_path(cfg, symbol)
    try:
        path.write_text(
            json.dumps(history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("历史快照已更新: %s (共 %d 份)", path.name, len(history))
    except Exception as e:
        logger.warning("历史快照写入失败 %s: %s", path, e)
