# -*- coding: utf-8 -*-
"""板块 → ETF 映射表与流动性过滤。

功能：
    1. SECTOR_ETF_MAP：板块名 → 主流 ETF 6 位代码（efinance 风格）
    2. pick_etf：为单个板块选 1 只流动性合格 ETF
    3. filter_by_liquidity：批量过滤

流动性过滤使用 data_fetcher.compute_etf_liquidity 取近 20 日均量与基金规模。
无合格 ETF 时降级为场外基金或龙头个股（在结果中标注 fallback_reason）。

代码格式说明：
    efinance/baostock 均支持 6 位 ETF 代码，前缀 sh./sz. 由 baostock 用；
    efinance 直接用 6 位。本表的 6 位代码以 sh/sz 实际交易所归属决定。
"""

from __future__ import annotations

import barbell_config as cfg
from core.logger import get_logger

log = get_logger("barbell.sector_etf_map")


# ====================================================================
# 板块 → ETF 映射表
# 6 位代码前 3 位判定交易所：510/511/512/513/515/516/517/518/520/588 → sh
# 159/150 → sz
# 多候选时按规模/流动性优先序排列
# ====================================================================
SECTOR_ETF_MAP: dict[str, list[dict]] = {
    "煤炭":         [{"code": "515220", "name": "煤炭 ETF"}],
    "石油石化":     [{"code": "159745", "name": "石化 ETF"}],
    "有色金属":     [{"code": "512400", "name": "有色 ETF"}, {"code": "159871", "name": "有色金属 ETF"}],
    "钢铁":         [{"code": "515210", "name": "钢铁 ETF"}],
    "基础化工":     [{"code": "159870", "name": "化工 ETF"}],
    "建筑材料":     [{"code": "159745", "name": "建材 ETF"}],
    "建筑装饰":     [{"code": "159749", "name": "建筑 ETF"}],
    "机械设备":     [{"code": "159886", "name": "机械 ETF"}],
    "电力设备":     [{"code": "516160", "name": "新能源 ETF"}, {"code": "159611", "name": "电力 ETF"}],
    "国防军工":     [{"code": "512660", "name": "军工 ETF"}, {"code": "159638", "name": "军工龙头 ETF"}],
    "汽车":         [{"code": "516110", "name": "汽车 ETF"}],
    "家用电器":     [{"code": "159996", "name": "家电 ETF"}],
    "轻工制造":     [{"code": "159608", "name": "轻工 ETF"}],
    "农林牧渔":     [{"code": "159825", "name": "农业 ETF"}],
    "食品饮料":     [{"code": "515170", "name": "食品饮料 ETF"}],
    "纺织服饰":     [{"code": "159605", "name": "纺织服装 ETF"}],
    "医药生物":     [{"code": "512010", "name": "医药 ETF"}, {"code": "159929", "name": "医药 ETF"}],
    "电子":         [{"code": "159997", "name": "电子 ETF"}, {"code": "515050", "name": "电子 50 ETF"}],
    "通信":         [{"code": "515880", "name": "通信 ETF"}],
    "计算机":       [{"code": "512720", "name": "计算机 ETF"}],
    "传媒":         [{"code": "512980", "name": "传媒 ETF"}],
    "银行":         [{"code": "512800", "name": "银行 ETF"}, {"code": "515020", "name": "银行指数 ETF"}],
    "非银金融":     [{"code": "512880", "name": "证券 ETF"}, {"code": "512070", "name": "非银 ETF"}],
    "房地产":       [{"code": "512200", "name": "房地产 ETF"}],
    "交通运输":     [{"code": "159663", "name": "交运 ETF"}],
    "商贸零售":     [{"code": "516880", "name": "零售 ETF"}],
    "社会服务":     [{"code": "159768", "name": "社服 ETF"}],
    "公用事业":     [{"code": "159607", "name": "公用事业 ETF"}],
    "环保":         [{"code": "512580", "name": "环保 ETF"}],
    "综合":         [],
    "美容护理":     [{"code": "159623", "name": "医美 ETF"}],
    # 板块哑铃策略中常见的概念性板块（防御/进攻延伸）
    "半导体":       [{"code": "512480", "name": "半导体 ETF"}, {"code": "159516", "name": "半导体材料 ETF"}],
    "人工智能":     [{"code": "515070", "name": "AI ETF"}, {"code": "159819", "name": "人工智能 ETF"}],
    "机器人":       [{"code": "562500", "name": "机器人 ETF"}, {"code": "159770", "name": "机器人产业 ETF"}],
    "新能源车":     [{"code": "515030", "name": "新能源车 ETF"}],
    "光伏":         [{"code": "515790", "name": "光伏 ETF"}],
    "白酒":         [{"code": "512690", "name": "酒 ETF"}],
    "券商":         [{"code": "512880", "name": "证券 ETF"}, {"code": "515010", "name": "证券指数 ETF"}],
    "医药":         [{"code": "512010", "name": "医药 ETF"}],
    "电力":         [{"code": "159611", "name": "电力 ETF"}],
    "银行指数":     [{"code": "512800", "name": "银行 ETF"}],
}


def _to_baostock_code(code6: str) -> str:
    """6 位 ETF 代码 → baostock 风格 sh./sz. 前缀。"""
    code6 = str(code6).zfill(6)
    if code6.startswith(("51", "52", "58", "50")):
        return f"sh.{code6}"
    if code6.startswith(("15", "16")):
        return f"sz.{code6}"
    return f"sh.{code6}"  # 兜底


def _asset_dict_from_etf(code6: str, name: str) -> dict:
    """构造与 cfg.DEFENSIVE 相同结构的 asset dict。"""
    return {
        "code": _to_baostock_code(code6),
        "name": name,
        "baostock_code": _to_baostock_code(code6),
        "efinance_code": str(code6).zfill(6),
    }


# ====================================================================
# 流动性过滤
# ====================================================================
def filter_by_liquidity(etf_candidates: list[dict], params: dict | None = None) -> list[dict]:
    """对候选 ETF 列表做流动性过滤。

    Args:
        etf_candidates: [{"code": "515070", "name": "AI ETF"}, ...]
        params: SECTOR_PARAMS 子集，缺省用 cfg.SECTOR_PARAMS

    Returns:
        [{'code','name','avg_amount','fund_size','efinance_code','baostock_code'}]
        仅返回流动性合格的 ETF，按 avg_amount 降序。
    """
    p = params or cfg.SECTOR_PARAMS
    amount_min = p.get("etf_amount_min", 5000) * 1e4   # 5000 万 → 5e7 元
    size_min = p.get("etf_size_min", 5) * 1e8           # 5 亿 → 5e8 元

    # 延迟导入避免循环依赖
    import data_fetcher as df_mod

    qualified = []
    for etf in etf_candidates:
        code = str(etf.get("code", "")).zfill(6)
        if not code:
            continue
        try:
            liq = df_mod.compute_etf_liquidity(code, lookback=20)
            if liq is None:
                log.warning("[ETF %s] 流动性数据不可用，跳过", code)
                continue
            avg_amt = liq.get("avg_amount", 0)
            fund_size = liq.get("fund_size", 0)
            if avg_amt >= amount_min and fund_size >= size_min:
                qualified.append({
                    **_asset_dict_from_etf(code, etf.get("name", code)),
                    "avg_amount": avg_amt,
                    "fund_size": fund_size,
                    "track_err": liq.get("track_err"),
                })
            else:
                log.info("[ETF %s] 流动性不足 amount=%.0f(<%g) size=%.0f(<%g)",
                         code, avg_amt, amount_min, fund_size, size_min)
        except Exception as e:
            log.warning("[ETF %s] 流动性检查异常: %s", code, e)

    qualified.sort(key=lambda x: x.get("avg_amount", 0), reverse=True)
    return qualified


# ====================================================================
# 单板块选 ETF
# ====================================================================
def pick_etf(sector_name: str, params: dict | None = None) -> dict:
    """为板块选 1 只流动性合格 ETF。

    Returns:
        dict: 包含 asset 结构（code/name/baostock_code/efinance_code）+
              avg_amount/fund_size + 可能的 fallback_reason
        无合格 ETF 时返回 fallback 字段：{'fallback':'no_etf','sector':sector_name, 'reason':...}
    """
    candidates = SECTOR_ETF_MAP.get(sector_name, [])
    if not candidates:
        log.warning("[板块 %s] 无 ETF 映射，降级", sector_name)
        return {"fallback": "no_etf", "sector": sector_name,
                "reason": f"板块 {sector_name} 未配置 ETF 映射"}

    qualified = filter_by_liquidity(candidates, params)
    if not qualified:
        # 全部流动性不合格 → 退化为场外基金占位（不实际跟踪）
        first = candidates[0]
        log.warning("[板块 %s] 所有候选 ETF 流动性不足，降级使用 %s（标记 fallback）",
                    sector_name, first.get("code"))
        return {
            **_asset_dict_from_etf(first.get("code", ""), first.get("name", sector_name)),
            "avg_amount": None,
            "fund_size": None,
            "fallback": "low_liquidity",
            "fallback_reason": f"板块 {sector_name} ETF 流动性不足，使用 {first.get('code')} 占位",
        }

    best = qualified[0]
    log.info("[板块 %s] 选用 ETF %s（均量 %.0f 万，规模 %.2f 亿）",
             sector_name, best["efinance_code"],
             (best.get("avg_amount") or 0) / 1e4,
             (best.get("fund_size") or 0) / 1e8)
    return best
