# -*- coding: utf-8 -*-
"""哑铃策略主流程入口。

运行方式（从项目根目录）：
    # 风格哑铃（默认）
    uv run python services/barbell_system/main.py

    # 板块哑铃
    BARBELL_MODE=sector uv run python services/barbell_system/main.py

流程：登录 → (sector 模式：板块筛选 → ETF 映射 → 参数覆盖) → 拉数据 →
       算信号 → 定权重 → 再平衡检查 → 失效预警 → 报告 + CSV 日志
"""

import datetime as dt

import pandas as pd

import barbell_config as cfg
import data_fetcher as df_mod
import signals
import weights as weights_mod
import rebalance
from config.settings import settings
from core.logger import get_logger

log = get_logger("barbell.main")


# ====================================================================
# sector 模式：参数覆盖 + 资产池解析
# ====================================================================
# 顶层 cfg 变量 ← SECTOR_PARAMS 同名字段的覆盖映射
_SECTOR_OVERLAY_KEYS = [
    "BASE_WEIGHT", "REBALANCE_THRESHOLD", "RUN_FREQUENCY",
    "CORRELATION_ALERT", "SPREAD_FLOOR", "SPREAD_HIGH", "CORR_LOW",
    "ZSCORE_LOOKBACK", "ZSCORE_WINDOW", "ZSCORE_LOW", "ZSCORE_HIGH",
    "CORR_WINDOW",
    "FAILURE_CORR_THRESHOLD", "FAILURE_SPREAD_THRESHOLD",
    "FAILURE_DOWN_DAYS", "FAILURE_CONDITIONS_REQUIRED",
    "FAILURE_REDUCE_PP", "FAILURE_WAIT_DAYS",
]
# SECTOR_PARAMS 字段名 → cfg 顶层变量名
_PARAM_KEY_MAP = {
    "base_weight": "BASE_WEIGHT",
    "offensive_base_weight": "OFFENSIVE_BASE_WEIGHT",
    "rebalance_threshold": "REBALANCE_THRESHOLD",
    "run_frequency": "RUN_FREQUENCY",
    "correlation_alert": "CORRELATION_ALERT",
    "spread_floor": "SPREAD_FLOOR",
    "spread_high": "SPREAD_HIGH",
    "corr_low": "CORR_LOW",
    "zscore_lookback": "ZSCORE_LOOKBACK",
    "zscore_window": "ZSCORE_WINDOW",
    "zscore_low": "ZSCORE_LOW",
    "zscore_high": "ZSCORE_HIGH",
    "corr_window": "CORR_WINDOW",
    "failure_corr": "FAILURE_CORR_THRESHOLD",
    "failure_spread": "FAILURE_SPREAD_THRESHOLD",
    "failure_down_days": "FAILURE_DOWN_DAYS",
    "failure_required": "FAILURE_CONDITIONS_REQUIRED",
    "failure_reduce_pp": "FAILURE_REDUCE_PP",
    "failure_wait_days": "FAILURE_WAIT_DAYS",
}


def _apply_sector_params():
    """将 SECTOR_PARAMS 字段写回 cfg 顶层变量。"""
    for pk, ck in _PARAM_KEY_MAP.items():
        if pk in cfg.SECTOR_PARAMS:
            setattr(cfg, ck, cfg.SECTOR_PARAMS[pk])
    log.info("sector 模式参数已覆盖（共 %d 项）", len(_PARAM_KEY_MAP))


def _pick_eligible_etf(candidates, end_label, exclude_name=None):
    """从候选板块里挑第一个"ETF 合格且不与该端重复"的板块。

    原来直接取 [0]，一旦榜首板块的 ETF 流动性不够/无映射就整体放弃、
    退回 style 默认池；这里改为逐个候选试，跳过不合格的板块。

    Returns:
        (sector_dict, etf_dict)；无合格候选返回 (None, None)
    """
    for cand in candidates or []:
        name = str(cand.get("name", ""))
        etf = cand.get("etf") or {}
        if exclude_name and name == exclude_name:
            log.info("%s候选 %s 已被另一端占用，跳过", end_label, name)
            continue
        if "fallback" in etf or not etf.get("efinance_code"):
            log.warning("%s候选 %s 无合格 ETF（%s），跳过", end_label, name,
                        etf.get("fallback", "missing_code"))
            continue
        return cand, etf
    return None, None


def _resolve_sector_pools(today: str):
    """运行板块筛选 + ETF 映射，写回 cfg.DEFENSIVE / OFFENSIVE。

    优先级：手动配置 > 动态筛选 > style 默认池（由 caller 处理）。
    """
    import sector_etf_map as sem

    # ---- 1. 手动配置优先：SECTOR_PARAMS.manual_*_etf 非空时直接钉死两端 ----
    manual_def = cfg.SECTOR_PARAMS.get("manual_defensive_etf")
    manual_off = cfg.SECTOR_PARAMS.get("manual_offensive_etf")
    if manual_def and manual_off:
        log.info("sector 模式：检测到手动 ETF 配置，跳过 screener 动态筛选")
        try:
            def_asset = sem._asset_dict_from_etf(str(manual_def["code"]).zfill(6),
                                                 manual_def.get("name", "ETF"))
            off_asset = sem._asset_dict_from_etf(str(manual_off["code"]).zfill(6),
                                                 manual_off.get("name", "ETF"))
        except Exception as e:
            log.error("手动 ETF 配置异常: %s，回退到动态筛选", e)
            manual_def = manual_off = None
        else:
            def_sector_name = manual_def.get("sector", "未命名板块")
            off_sector_name = manual_off.get("sector", "未命名板块")
            cfg.DEFENSIVE = def_asset
            cfg.OFFENSIVE = off_asset
            # 构造简化的 SCREENER_RESULT 用于报告/日志
            cfg.SCREENER_RESULT = {
                "as_of": today,
                "defensive": [{"name": def_sector_name,
                               "defensive_score": 1.0,
                               "etf": def_asset,
                               "factors": {"manual": True}}],
                "offensive": [{"name": off_sector_name,
                               "offensive_score": 1.0,
                               "etf": off_asset,
                               "factors": {"manual": True}}],
                "excluded": [],
                "warnings": ["使用手动 ETF 配置，跳过动态筛选"],
                "factor_summary": {"n_sectors_total": 0, "n_excluded": 0,
                                   "n_defensive": 1, "n_offensive": 1, "manual": True},
            }
            cfg.DEFENSIVE_SECTOR_POOL = cfg.SCREENER_RESULT["defensive"]
            cfg.OFFENSIVE_SECTOR_POOL = cfg.SCREENER_RESULT["offensive"]
            cfg.DEFENSIVE_SECTOR_NAME = def_sector_name
            cfg.OFFENSIVE_SECTOR_NAME = off_sector_name
            cfg.POLICY_FLAG = cfg.SECTOR_PARAMS.get("policy_flag_offensive", [])
            log.info("sector 模式资产池已选定: 防御=%s·%s | 进攻=%s·%s",
                     def_sector_name, def_asset.get("name"),
                     off_sector_name, off_asset.get("name"))
            return True

    # ---- 2. 动态筛选 ----
    import screener
    res = screener.run_sectors(today)
    cfg.SCREENER_RESULT = res

    if not res["defensive"] or not res["offensive"]:
        log.error("板块筛选结果为空，回退到 style 默认资产池")
        return False

    # 防御端：候选里第一个 ETF 合格的板块
    def_sector, def_etf = _pick_eligible_etf(res["defensive"], "防御端")
    if def_sector is None:
        log.error("防御端无任何合格 ETF 候选，降级使用 style 默认池")
        return False
    cfg.DEFENSIVE = {
        "code": def_etf.get("baostock_code", def_etf.get("code")),
        "name": f"{def_sector['name']}·{def_etf.get('name', 'ETF')}",
        "baostock_code": def_etf["baostock_code"],
        "efinance_code": def_etf["efinance_code"],
    }

    # 进攻端：同样逐个候选试，且必须与防御端不同板块（否则两端同一只 ETF）
    off_sector, off_etf = _pick_eligible_etf(res["offensive"], "进攻端",
                                             exclude_name=def_sector["name"])
    if off_sector is None:
        log.error("进攻端无合格 ETF 候选，降级使用 style 默认池")
        return False
    cfg.OFFENSIVE = {
        "code": off_etf.get("baostock_code", off_etf.get("code")),
        "name": f"{off_sector['name']}·{off_etf.get('name', 'ETF')}",
        "baostock_code": off_etf["baostock_code"],
        "efinance_code": off_etf["efinance_code"],
    }

    # 记录运行时板块池与政策标志
    cfg.DEFENSIVE_SECTOR_POOL = res["defensive"]
    cfg.OFFENSIVE_SECTOR_POOL = res["offensive"]
    cfg.DEFENSIVE_SECTOR_NAME = def_sector["name"]
    cfg.OFFENSIVE_SECTOR_NAME = off_sector["name"]
    cfg.POLICY_FLAG = cfg.SECTOR_PARAMS.get("policy_flag_offensive", [])

    log.info("sector 模式资产池已选定: 防御=%s | 进攻=%s",
             cfg.DEFENSIVE["name"], cfg.OFFENSIVE["name"])
    return True


# ====================================================================
# 主流程
# ====================================================================
def run():
    today = dt.datetime.now().strftime("%Y-%m-%d")
    print("=" * 64)
    print(f"哑铃策略配置与监控系统  运行日期 {today}  模式: {cfg.MODE}")
    if cfg.MODE == "sector":
        print("【板块哑铃】动态板块筛选 + 板块 ETF 映射")
    else:
        print(f"【风格哑铃】防御端: {cfg.DEFENSIVE['name']} ({cfg.DEFENSIVE['code']})")
        print(f"【风格哑铃】进攻端: {cfg.OFFENSIVE['name']} ({cfg.OFFENSIVE['code']})")
    print("=" * 64)

    # 1. 登录 baostock
    #    必须排在 sector 筛选之前：screener 会经 data_fetcher 拉板块 ETF 日线，
    #    未登录时 baostock 直接返回 "10001001 you don't login"，备源形同虚设。
    bs_ok = df_mod.login_baostock()

    try:
        # 2. sector 模式：先做参数覆盖 + 板块筛选
        sector_pools_ok = False
        if cfg.MODE == "sector":
            _apply_sector_params()
            sector_pools_ok = _resolve_sector_pools(today)

        print(f"防御端: {cfg.DEFENSIVE['name']} ({cfg.DEFENSIVE['code']})")
        print(f"进攻端: {cfg.OFFENSIVE['name']} ({cfg.OFFENSIVE['code']})")
        print("=" * 64)

        # 3. 拉取两端 ETF 数据（sector 模式可能用 fetch_sector_etf_kline，但接口与 fetch_etf_kline 兼容）
        def_df = df_mod.fetch_etf_kline(cfg.DEFENSIVE)
        off_df = df_mod.fetch_etf_kline(cfg.OFFENSIVE)

        if def_df is None or off_df is None:
            # sector 模式：改走板块 ETF 数据接口重试一次（数据源顺序见
            # cfg.SECTOR_ETF_SOURCE_ORDER，默认 baostock 主 / efinance 备）
            if cfg.MODE == "sector":
                log.info("两端 ETF 日线缺失，sector 模式改用板块 ETF 接口重试...")
                def_df = df_mod.fetch_sector_etf_kline(cfg.DEFENSIVE.get("efinance_code"))
                off_df = df_mod.fetch_sector_etf_kline(cfg.OFFENSIVE.get("efinance_code"))

        if def_df is None or off_df is None:
            print("[ERROR] 两端 ETF 数据均不可用，无法运行。")
            return

        # 3. 国债收益率与股息率
        treasury = df_mod.fetch_treasury_yield()
        if cfg.MODE == "sector":
            # 板块哑铃：股息率用板块加权代理
            div_yield = df_mod.fetch_sector_dividend_yield(cfg.DEFENSIVE.get("name", ""))
        else:
            div_yield = df_mod.fetch_dividend_yield()

        # 国债不可用时用价格分位数代理
        price_pct = None
        if treasury is None:
            price_pct = df_mod.compute_defensive_price_percentile(def_df)

        # 4. 三信号（函数签名不变，sector 模式通过 cfg 覆盖自动用新阈值）
        spread_val, spread_score, spread_note = signals.signal_spread(
            div_yield, treasury, price_pct)
        corr_val, corr_score, corr_note = signals.signal_correlation(def_df, off_df)
        z_val, z_score, z_note = signals.signal_crowding(off_df)

        total = signals.compute_total_score(spread_score, corr_score, z_score)

        # 5. 目标权重（sector 模式下 cfg.BASE_WEIGHT=0.60 已被覆盖）
        tgt = weights_mod.determine_weights(total)

        # 6. 再平衡检查
        state = rebalance.load_state()
        def_close_now = float(def_df["close"].iloc[-1])
        off_close_now = float(off_df["close"].iloc[-1])
        cur_w = rebalance.estimate_current_weights(state, def_close_now, off_close_now)
        reb = rebalance.check_rebalance(cur_w, tgt)

        # 若触发再平衡或无状态，更新状态
        if reb["triggered"] or state is None:
            rebalance.save_state(def_close_now, off_close_now, tgt, today)

        # 7. 失效预警（按 MODE 分支）
        if cfg.MODE == "sector":
            import alert as alert_mod
            # corr_history 简易代理：从 signals 计算 60 日相关系数序列
            corr_history = _calc_corr_history(def_df, off_df, cfg.CORR_WINDOW)
            alert = alert_mod.check_failure_alert_sector(
                corr_val, spread_val, def_df, off_df,
                policy_flag=cfg.POLICY_FLAG,
                corr_history=corr_history,
            )
        else:
            alert = rebalance.check_failure_alert(corr_val, spread_val, def_df, off_df)

        # 8. 控制台报告
        _print_report(
            today, def_df, off_df, treasury, div_yield, price_pct,
            spread_val, spread_score, spread_note,
            corr_val, corr_score, corr_note,
            z_val, z_score, z_note,
            total, tgt, cur_w, reb, alert)

        # 9. CSV 日志
        _write_log(today, spread_val, corr_val, z_val, total, tgt, reb, alert)

    finally:
        if bs_ok:
            df_mod.logout_baostock()


def _calc_corr_history(def_df, off_df, window):
    """简易提取 60 日相关系数历史序列（用于 alert 的相关性骤升检测）。"""
    try:
        d = pd.merge(def_df[["date", "close"]], off_df[["date", "close"]],
                     on="date", suffixes=("_def", "_off"))
        d = d.sort_values("date").reset_index(drop=True)
        ret_def = d["close_def"].pct_change()
        ret_off = d["close_off"].pct_change()
        corr = ret_def.rolling(window).corr(ret_off).dropna()
        return [float(v) for v in corr.tail(60).tolist()]
    except Exception:
        return []


def _print_report(today, def_df, off_df, treasury, div_yield, price_pct,
                  spread_val, spread_score, spread_note,
                  corr_val, corr_score, corr_note,
                  z_val, z_score, z_note,
                  total, tgt, cur_w, reb, alert):
    print("\n" + "=" * 64)
    print("【一】数据区间")
    print(f"  模式: {cfg.MODE}")
    if cfg.MODE == "sector" and cfg.SCREENER_RESULT:
        res = cfg.SCREENER_RESULT
        print(f"  板块筛选: 共 {res.get('factor_summary', {}).get('n_sectors_total', '?')} 个板块，"
              f"排除 {res.get('factor_summary', {}).get('n_excluded', 0)} 个中间地带")
        def_used = cfg.DEFENSIVE_SECTOR_NAME or (
            res["defensive"][0]["name"] if res.get("defensive") else "")
        off_used = cfg.OFFENSIVE_SECTOR_NAME or (
            res["offensive"][0]["name"] if res.get("offensive") else "")
        def_score = next((s.get("defensive_score", 0) for s in res.get("defensive", [])
                          if s.get("name") == def_used), 0)
        off_score = next((s.get("offensive_score", 0) for s in res.get("offensive", [])
                          if s.get("name") == off_used), 0)
        if def_used:
            print(f"  防御端板块: {def_used} (得分 {def_score:.3f})")
        if off_used:
            print(f"  进攻端板块: {off_used} (得分 {off_score:.3f})")
        if res.get("warnings"):
            print(f"  筛选警告 ({len(res['warnings'])} 条):")
            for w in res["warnings"][:3]:
                print(f"    - {w}")
    print(f"  防御端 {cfg.DEFENSIVE['name']}: {def_df['date'].iloc[0]} ~ {def_df['date'].iloc[-1]} "
          f"({len(def_df)} 条)")
    print(f"  进攻端 {cfg.OFFENSIVE['name']}: {off_df['date'].iloc[0]} ~ {off_df['date'].iloc[-1]} "
          f"({len(off_df)} 条)")
    t_str = f"{treasury:.3f}%" if treasury is not None else "不可用(降级代理)"
    print(f"  10Y 国债收益率: {t_str}")
    print(f"  防御端股息率: {div_yield:.3f}%")
    if price_pct is not None:
        print(f"  防御端价格分位数(代理): {price_pct:.1f}%")

    print("\n【二】信号值")
    print(f"  信号一 股债利差: {spread_note}")
    print(f"  信号二 滚动相关性: {corr_note}")
    print(f"  信号三 拥挤度 Z-score: {z_note}")

    print("\n【三】综合得分")
    print(f"  spread={spread_score:+d}  corr={corr_score:+d}  zscore={z_score:+d}  "
          f"总分={total:+d}")

    print("\n【四】市场状态与目标权重")
    print(f"  市场状态: {tgt['regime']}")
    print(f"  目标 防御 {tgt['defensive']*100:.0f}% / 进攻 {tgt['offensive']*100:.0f}% / "
          f"现金 {tgt['cash']*100:.0f}%")

    print("\n【五】再平衡建议")
    if cur_w is None:
        print("  无状态文件，无法推算实际权重。")
    else:
        print(f"  当前隐含 防御 {cur_w['defensive']*100:.1f}% / "
              f"进攻 {cur_w['offensive']*100:.1f}% / 现金 {cur_w['cash']*100:.1f}%")
        print(f"  偏离: 防御 {reb['drifts'].get('defensive',0)*100:+.1f}pp / "
              f"进攻 {reb['drifts'].get('offensive',0)*100:+.1f}pp")
    if reb["triggered"]:
        print("  >>> 触发再平衡")
        for s in reb["suggestions"]:
            print(f"    - {s}")
    else:
        print(f"  未触发再平衡（偏离 < {cfg.REBALANCE_THRESHOLD*100:.0f}% 阈值）")

    print("\n【六】失效预警")
    for c in alert["conditions"]:
        mark = "✓" if c["met"] else "✗"
        v = c["value"]
        vstr = f"{v:.3f}" if isinstance(v, float) else str(v)
        print(f"  {mark} {c['name']} (值: {vstr})")
    print(f"  → {alert['action']}")
    print("=" * 64)


def _write_log(today, spread_val, corr_val, z_val, total, tgt, reb, alert):
    """CSV 日志写入项目 data 目录。新增 mode/sector 字段。"""
    fname = f"{cfg.LOG_PREFIX}{today.replace('-', '')}.csv"
    path = settings.data_dir / fname
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "date": today,
        "mode": cfg.MODE,
        "spread": "" if spread_val is None else round(spread_val, 4),
        "correlation": "" if corr_val is None else round(corr_val, 4),
        "zscore": "" if z_val is None else round(z_val, 4),
        "score": total,
        "regime": tgt["regime"],
        "target_defensive": tgt["defensive"],
        "target_offensive": tgt["offensive"],
        "target_cash": tgt["cash"],
        "rebalance_triggered": int(reb["triggered"]),
        "alert_info": alert["action"],
        # sector 模式新增字段（style 模式留空）
        "defensive_sector": "",
        "offensive_sector": "",
        "screener_score": "",
        "policy_flag": "",
    }
    if cfg.MODE == "sector" and cfg.SCREENER_RESULT:
        res = cfg.SCREENER_RESULT
        def_used = cfg.DEFENSIVE_SECTOR_NAME or (
            res["defensive"][0].get("name", "") if res.get("defensive") else "")
        off_used = cfg.OFFENSIVE_SECTOR_NAME or (
            res["offensive"][0].get("name", "") if res.get("offensive") else "")
        row["defensive_sector"] = def_used
        row["offensive_sector"] = off_used
        row["screener_score"] = round(next(
            (s.get("defensive_score", 0) for s in res.get("defensive", [])
             if s.get("name") == def_used), 0), 3)
        row["policy_flag"] = "|".join(cfg.POLICY_FLAG or [])
    pd.DataFrame([row]).to_csv(path, index=False, encoding="utf-8-sig")
    print(f"\n[日志] 已写入 {path}")


if __name__ == "__main__":
    run()
