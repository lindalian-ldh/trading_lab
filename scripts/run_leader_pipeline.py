#!/usr/bin/env python3
"""龙头股复盘流水线：bankuai 扫龙头 → calc_indicators 批量筛查 → yanbao / sell_monitor 衔接。

把原本需手动串接的链路自动化：
    bankuai-service 扫出的龙头池
      → calc_indicators 三维度批量筛查（结构/动量/赔率）
        → 按通过维度数过滤
          → （可选）yanbao-info 拉研报
          → （可选）打印 sell_monitor 登记命令（不自动登记，进场需人确认）
            → 龙头池 code↔name 沉淀回 data/stock_mapping.json

跑完即退，无常驻循环。所有阶段失败隔离，单只股票评估失败不影响其他。

用法示例：
    # 默认：概念板块 Top3 热门，tier1+tier2，aggressive_short，至少 2 维通过
    uv run python scripts/run_leader_pipeline.py

    # 行业板块 + 超短线配置 + 生成 PNG 报告
    uv run python scripts/run_leader_pipeline.py --plate-type 行业 -p ultra_short --report

    # 指定板块 + 三梯队全要 + 跑研报 + 打印 sell_monitor 登记命令
    uv run python scripts/run_leader_pipeline.py --sectors 电力 --tiers tier1,tier2,tier3 \
        --with-yanbao --with-sell-init

    # 只看龙头池不筛查（快速预览）
    uv run python scripts/run_leader_pipeline.py --dry-run

    # 复用已有 bankuai JSON（跳过 zzshare 扫描，最快）
    uv run python scripts/run_leader_pipeline.py --bankuai-json data/reports/bankuai/bankuai_2026-08-26_行业.json

退出码：0=成功（含「龙头池为空」业务正常态）；1=失败。
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

# 项目根
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 加载 .env（zzshare token / FRED key 等）
try:
    from dotenv import load_dotenv
    load_dotenv(_PROJECT_ROOT / ".env")
except Exception:
    pass

try:
    from core.logger import get_logger
    log = get_logger("leader_pipeline")
except Exception:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("leader_pipeline")


# ====================================================================
# 路径常量
# ====================================================================
DATA_DIR = _PROJECT_ROOT / "data"
BANKUAI_REPORT_DIR = DATA_DIR / "reports" / "bankuai"
STOCK_MAPPING_PATH = DATA_DIR / "stock_mapping.json"
PIPELINE_REPORT_DIR = DATA_DIR / "reports" / "pipeline"


# ====================================================================
# bankuai 报告获取
# ====================================================================
def _default_bankuai_json_path(date1: str, plate_type: str) -> Path:
    return BANKUAI_REPORT_DIR / f"bankuai_{date1}_{plate_type}.json"


def get_bankuai_report(args) -> dict | None:
    """获取 bankuai 扫描报告 dict。

    优先级：
        1. --bankuai-json 指定路径 → 直接读
        2. 当天已存在 JSON 且非 --rescan-bankuai → 复用
        3. 否则 subprocess 调 bankuai CLI 生成后读
    """
    # 1. 显式指定
    if args.bankuai_json:
        p = Path(args.bankuai_json)
        if not p.is_absolute():
            p = _PROJECT_ROOT / p
        if not p.exists():
            log.error("指定的 bankuai JSON 不存在: %s", p)
            return None
        log.info("复用指定 bankuai JSON: %s", p)
        return _load_json(p)

    # 2. 当天已存在
    target = _default_bankuai_json_path(args.date, args.plate_type)
    if target.exists() and not args.rescan_bankuai:
        log.info("复用当天已存在 bankuai JSON: %s", target)
        return _load_json(target)

    # 3. subprocess 调 CLI 生成
    log.info("当天 JSON 不存在或 --rescan-bankuai，调用 bankuai CLI 扫描…")
    cmd = [
        "uv", "run", "services/bankuai-service/main.py",
        "--date", args.date,
        "--plate-type", args.plate_type,
        "--top-n", str(args.top_n_sectors),
        "--bottom-n", "0",            # 流水线只关心热门龙头，不扫冷门省配额
        "--tier-size", str(args.tier_size),
        "--quiet",
        "--out", str(target),
    ]
    if args.sectors:
        cmd += ["--sectors", args.sectors]
    log.info("执行: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, cwd=str(_PROJECT_ROOT), timeout=180)
    except subprocess.TimeoutExpired:
        log.error("bankuai 扫描超时（>180s）")
        return None
    if result.returncode != 0:
        log.error("bankuai CLI 退出码 %d", result.returncode)
        return None
    if not target.exists():
        log.error("bankuai CLI 执行完但 JSON 未生成: %s", target)
        return None
    return _load_json(target)


def _load_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.error("读取 JSON 失败 %s: %s", p, e)
        return None


# ====================================================================
# 龙头池提取
# ====================================================================
def extract_leader_pool(report: dict, tiers: list[str], top_n_sectors: int,
                        max_stocks: int) -> list[dict]:
    """从 bankuai 报告提取龙头池，去重 + 截断。

    Returns: list[{"code","name","sector","tier","change"}]
    """
    sector_leaders = report.get("sector_leaders", {}) or {}
    if not sector_leaders:
        log.warning("bankuai 报告无 sector_leaders（扫描可能失败）")
        return []

    # 热门板块顺序：优先 hot_sectors 前几名；若缺则按 dict 顺序
    hot = report.get("hot_sectors", []) or []
    hot_names = [s.get("name", "") for s in hot[:top_n_sectors] if s.get("name")]
    if hot_names:
        ordered = [n for n in hot_names if n in sector_leaders]
        # 补上 sector_leaders 中未被 hot 覆盖的（如指定 --sectors）
        for n in sector_leaders:
            if n not in ordered:
                ordered.append(n)
    else:
        ordered = list(sector_leaders.keys())

    pool: list[dict] = []
    seen_codes: set[str] = set()
    for sector_name in ordered:
        info = sector_leaders.get(sector_name) or {}
        for tier in tiers:
            for st in info.get(tier, []) or []:
                code = str(st.get("code", "")).strip()
                name = str(st.get("name", "")).strip()
                if not code or len(code) != 6 or not code.isdigit():
                    continue
                if code in seen_codes:
                    continue
                seen_codes.add(code)
                pool.append({
                    "code": code,
                    "name": name,
                    "sector": sector_name,
                    "tier": tier,
                    "change": st.get("change"),
                    "turnover_rate": st.get("turnover_rate"),
                })
                if len(pool) >= max_stocks:
                    log.info("龙头池达上限 %d，停止提取", max_stocks)
                    return pool
    return pool


# ====================================================================
# stock_mapping.json 增量合并
# ====================================================================
def update_stock_mapping(pool: list[dict], dry_run: bool = False) -> int:
    """把龙头池 code↔name 增量合并进 data/stock_mapping.json。

    不覆盖已存在的名称（保留手工维护）。返回新增条数。
    """
    if not pool:
        return 0
    STOCK_MAPPING_PATH.parent.mkdir(parents=True, exist_ok=True)

    raw: dict = {}
    if STOCK_MAPPING_PATH.exists():
        try:
            raw = json.loads(STOCK_MAPPING_PATH.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raw = {}
        except Exception as e:
            log.warning("stock_mapping.json 解析失败，将重建: %s", e)
            raw = {}

    added = 0
    for st in pool:
        code, name = st["code"], st["name"]
        if not name:
            continue
        # 保留 _ 开头的元数据键；不覆盖已有名称
        if code in raw and raw[code]:
            continue
        raw[code] = name
        added += 1

    if added and not dry_run:
        tmp = STOCK_MAPPING_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STOCK_MAPPING_PATH)
    return added


# ====================================================================
# calc_indicators 批量筛查（subprocess CLI + 正则解析）
# ====================================================================
# 用 subprocess 而非 in-process 导入：calc_indicators 的 config.py 与项目根 config/ 包
# 命名冲突，且 CLI 调用解耦内部 API 变更，与 run_all.py 风格一致。
_RE_NUM = r"([\d.]+)"
_RE_RATIO = r"(∞|[\d.]+)"


def _parse_calc_output(text: str, symbol: str, stock_name: str) -> dict:
    """解析 calc_indicators stdout 的三维度清单，返回精简结果 dict。

    依赖稳定输出格式（_print_stock_result）：
        维度A通过 / 维度A不通过
        当前价: 18.71
        支撑位(止损): 17.39，亏损空间: 1.32
        压力位(止盈): 19.51，盈利空间: 0.80
        盈亏比: 0.60:1
        建议开仓: 757 股
    注：「维度A不通过」不含子串「维度A通过」（A 后接「不」），故直接字面判断安全。
    """
    # 维度通过状态（"维度X通过" 出现即视为通过；"维度X不通过" 不会误命中）
    a_pass = "维度A通过" in text
    b_pass = "维度B通过" in text
    c_pass = "维度C通过" in text

    def _find(pat: str, default=""):
        m = re.search(pat, text)
        return m.group(1) if m else default

    price = _safe_float(_find(rf"当前价[:：]\s*{_RE_NUM}"))
    stop = _safe_float(_find(rf"支撑位\(止损\)[:：]\s*{_RE_NUM}"))
    take = _safe_float(_find(rf"压力位\(止盈\)[:：]\s*{_RE_NUM}"))
    ratio_raw = _find(rf"盈亏比[:：]\s*{_RE_RATIO}:1", "—")
    shares = _safe_int(_find(rf"建议开仓[:：]\s*(\d+)\s*股"))

    return {
        "code": symbol,
        "name": stock_name,
        "a_pass": a_pass,
        "b_pass": b_pass,
        "c_pass": c_pass,
        "pass_count": sum([a_pass, b_pass, c_pass]),
        "all_pass": a_pass and b_pass and c_pass,
        "latest_close": round(price, 3) if price else 0,
        "entry_price": round(price, 3) if price else 0,
        "stop_price": round(stop, 3) if stop else 0,
        "take_profit": round(take, 3) if take else 0,
        "rr_ratio": ratio_raw,
        "suggested_shares": shares,
    }


def _safe_float(s: str) -> float:
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def _safe_int(s: str) -> int:
    try:
        return int(s)
    except (ValueError, TypeError):
        return 0


def screen_one_stock(symbol: str, profile_name: str, stock_name: str = "") -> dict | None:
    """对单只股票调 calc_indicators CLI 跑三维度判定，解析输出返回结果 dict。

    失败（CLI 异常 / 超时）返回 None。
    """
    cmd = ["uv", "run", "python", "services/calc_indicators/main.py",
           symbol, "-p", profile_name]
    try:
        result = subprocess.run(
            cmd, cwd=str(_PROJECT_ROOT), timeout=90,
            capture_output=True, text=True,
        )
    except subprocess.TimeoutExpired:
        log.warning("calc_indicators 超时 %s", symbol)
        return None
    except Exception as e:
        log.warning("calc_indicators 调用异常 %s: %s", symbol, e)
        return None
    if result.returncode not in (0, 2):  # 0=成功; 2=--skip-all-fail 跳过 PNG
        log.warning("calc_indicators 退出码 %d (%s)", result.returncode, symbol)
        return None
    return _parse_calc_output(result.stdout, symbol, stock_name)


def screen_batch(pool: list[dict], profile_name: str) -> list[dict]:
    """批量筛查龙头池，返回结果列表（失败项含 error 字段）。"""
    results: list[dict] = []
    total = len(pool)
    for i, st in enumerate(pool, 1):
        log.info("[%d/%d] 筛查 %s %s …", i, total, st["code"], st["name"])
        r = screen_one_stock(st["code"], profile_name, st["name"])
        if r is None:
            r = {"code": st["code"], "name": st["name"], "error": "评估失败"}
        else:
            r["sector"] = st.get("sector", "")
            r["tier"] = st.get("tier", "")
        results.append(r)
    return results


# ====================================================================
# yanbao-info 衔接（subprocess）
# ====================================================================
def run_yanbao_for(symbol: str) -> bool:
    """对单只股票跑 yanbao-info CLI 拉研报。成功返回 True。"""
    cmd = ["uv", "run", "services/yanbao-info/main.py", "--symbol", symbol, "--quiet"]
    log.info("  研报: %s", " ".join(cmd))
    try:
        r = subprocess.run(cmd, cwd=str(_PROJECT_ROOT), timeout=120)
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        log.warning("  研报拉取超时 %s", symbol)
        return False


# ====================================================================
# sell_monitor 登记命令生成（只打印，不自动登记）
# ====================================================================
def build_sell_init_command(r: dict, profile: str, ref_date: str) -> str:
    """根据筛查结果生成 sell_monitor 登记命令（entry-price 用 latest_close 提示）。"""
    code = r["code"]
    shares = r.get("suggested_shares", 0) or 1000
    entry = r.get("entry_price") or r.get("latest_close") or 0
    if not entry:
        return f"# {code} 无有效价格，跳过 sell_monitor 登记"
    return (
        f"uv run services/sell_monitor/main.py --symbol {code} --date {ref_date} "
        f"--init --entry-price {entry} --shares {shares} --profile {profile}"
    )


# ====================================================================
# 报告输出
# ====================================================================
def write_reports(summary: dict, date1: str) -> tuple[Path, Path]:
    """写 JSON + Markdown 汇总报告到 data/reports/pipeline/。"""
    PIPELINE_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = PIPELINE_REPORT_DIR / f"leaders_{date1}.json"
    md_path = PIPELINE_REPORT_DIR / f"leaders_{date1}.md"

    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    md = _render_markdown(summary)
    md_path.write_text(md, encoding="utf-8")
    return json_path, md_path


def _render_markdown(s: dict) -> str:
    date1 = s.get("date", "")
    lines = [
        f"# 龙头股复盘流水线报告 {date1}",
        "",
        f"- 板块类型: **{s.get('plate_type','')}**",
        f"- 配置档: **{s.get('profile','')}**",
        f"- 龙头池规模: **{s.get('pool_size',0)}**",
        f"- 通过筛查（≥{s.get('min_pass',2)} 维）: **{s.get('passed_count',0)}**",
        f"- stock_mapping 新增: **{s.get('mapping_added',0)}** 条",
        "",
        "## 龙头池",
        "",
        "| 代码 | 名称 | 板块 | 梯队 | 涨幅% |",
        "|---|---|---|---|---|",
    ]
    for st in s.get("leader_pool", []):
        chg = st.get("change")
        chg_s = f"{chg:.2f}" if isinstance(chg, (int, float)) else "—"
        lines.append(f"| {st['code']} | {st['name']} | {st.get('sector','')} | "
                     f"{st.get('tier','')} | {chg_s} |")
    lines.append("")

    lines += [
        "## 筛查结果",
        "",
        "| 代码 | 名称 | A结构 | B动量 | C赔率 | 通过 | 盈亏比 | 现价 | 止损 | 止盈 | 建议股数 |",
        "|---|---|:---:|:---:|:---:|:---:|---|---|---|---|---|",
    ]
    for r in s.get("screen_results", []):
        if "error" in r:
            lines.append(f"| {r['code']} | {r.get('name','')} | — | — | — | ❌ | — | — | — | — | — |")
            continue
        yn = lambda b: "✅" if b else "❌"
        lines.append(
            f"| {r['code']} | {r.get('name','')} | {yn(r['a_pass'])} | {yn(r['b_pass'])} | "
            f"{yn(r['c_pass'])} | {r['pass_count']}/3 | {r.get('rr_ratio','—')} | "
            f"{r.get('latest_close','—')} | {r.get('stop_price','—')} | "
            f"{r.get('take_profit','—')} | {r.get('suggested_shares',0)} |"
        )
    lines.append("")

    passed = [r for r in s.get("screen_results", []) if "error" not in r and r.get("pass_count", 0) >= s.get("min_pass", 2)]
    if passed:
        lines += ["## 通过筛查的标的（后续衔接）", ""]
        if s.get("with_yanbao"):
            lines += ["### 研报", "已对通过标的拉取研报，见 `data/yanbao/reports/`。", ""]
        if s.get("with_sell_init"):
            lines += ["### sell_monitor 登记命令", "```bash"]
            for r in passed:
                lines.append(build_sell_init_command(r, s.get("profile", "default"), date1))
            lines += ["```", ""]
        if not s.get("with_yanbao") and not s.get("with_sell_init"):
            lines.append("（未启用 --with-yanbao / --with-sell-init，无后续衔接）")
            lines.append("")
    else:
        lines += ["## 通过筛查的标的", "", "本日无标的达到通过门槛，观望。", ""]

    return "\n".join(lines)


# ====================================================================
# 参数解析
# ====================================================================
def _parse_args(argv):
    p = argparse.ArgumentParser(
        description="龙头股复盘流水线：bankuai→calc_indicators→yanbao/sell_monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--date", default=(date.today() - timedelta(days=1)).isoformat(),
                   help="参考日期 YYYY-MM-DD，默认昨天")
    p.add_argument("--plate-type", default="概念", help="板块类型: 概念/题材/行业，默认概念")
    p.add_argument("--sectors", default="", help="指定板块名（逗号分隔），非空时仅扫这些板块")
    p.add_argument("--top-n-sectors", type=int, default=3, help="从热门板块取前 N 个作为龙头池来源，默认 3")
    p.add_argument("--tiers", default="tier1,tier2",
                   help="参与筛查的梯队，逗号分隔: tier1,tier2,tier3，默认 tier1,tier2")
    p.add_argument("--tier-size", type=int, default=6, help="bankuai 每个梯队最大数量，默认 6")
    p.add_argument("--max-stocks", type=int, default=10, help="龙头池最大股票数（限频保护），默认 10")

    p.add_argument("-p", "--profile", default="aggressive_short",
                   help="calc_indicators 配置档，默认 aggressive_short")
    p.add_argument("--min-pass", type=int, default=2, choices=[1, 2, 3],
                   help="进入后续阶段的最少通过维度数，默认 2")

    p.add_argument("--with-yanbao", action="store_true", help="对通过标的拉取研报（慢）")
    p.add_argument("--with-sell-init", action="store_true",
                   help="对通过标的打印 sell_monitor 登记命令（不自动登记）")
    p.add_argument("--report", action="store_true",
                   help="对通过标的生成 calc_indicators PNG 报告（需 -r）")

    p.add_argument("--no-update-mapping", action="store_true",
                   help="不把龙头池写回 stock_mapping.json")
    p.add_argument("--rescan-bankuai", action="store_true",
                   help="强制重新扫描板块（忽略当天已存在 JSON）")
    p.add_argument("--bankuai-json", default="",
                   help="直接用指定 bankuai JSON 报告（跳过扫描）")
    p.add_argument("--dry-run", action="store_true", help="只输出龙头池，不跑 calc_indicators")
    p.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    return p.parse_args(argv)


# ====================================================================
# 主入口
# ====================================================================
def main() -> int:
    args = _parse_args(sys.argv[1:])
    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    log.info("=" * 30 + " 龙头复盘流水线开始 " + "=" * 30)
    log.info("date=%s plate=%s profile=%s min_pass=%d",
             args.date, args.plate_type, args.profile, args.min_pass)

    tiers = [t.strip() for t in args.tiers.split(",") if t.strip()]
    if not tiers:
        tiers = ["tier1", "tier2"]

    # ---- 1. 获取 bankuai 报告 ----
    report = get_bankuai_report(args)
    if report is None:
        log.error("无法获取 bankuai 报告，流水线终止")
        return 1

    # ---- 2. 提取龙头池 ----
    pool = extract_leader_pool(report, tiers, args.top_n_sectors, args.max_stocks)
    log.info("龙头池提取完成: %d 只", len(pool))
    if not pool:
        log.warning("龙头池为空（板块扫描可能失败或无匹配板块）")
        # 仍写一份空报告便于排查
        summary = {
            "date": args.date, "plate_type": args.plate_type,
            "profile": args.profile, "min_pass": args.min_pass,
            "pool_size": 0, "passed_count": 0, "mapping_added": 0,
            "leader_pool": [], "screen_results": [],
        }
        jp, mp = write_reports(summary, args.date)
        log.info("空报告已写: %s", mp)
        return 0

    for st in pool:
        log.info("  龙头: %s %s  [%s/%s  %+s%%]",
                 st["code"], st["name"], st.get("sector", ""), st.get("tier", ""),
                 st.get("change"))

    # ---- 3. 沉淀 stock_mapping ----
    added = 0
    if not args.no_update_mapping:
        added = update_stock_mapping(pool, dry_run=args.dry_run)
        log.info("stock_mapping.json 增量 +%d 条", added)

    # ---- 4. dry-run 退出 ----
    if args.dry_run:
        log.info("--dry-run 模式，跳过 calc_indicators 筛查")
        summary = {
            "date": args.date, "plate_type": args.plate_type,
            "profile": args.profile, "min_pass": args.min_pass,
            "pool_size": len(pool), "passed_count": 0, "mapping_added": added,
            "leader_pool": pool, "screen_results": [],
            "dry_run": True,
        }
        jp, mp = write_reports(summary, args.date)
        log.info("龙头池报告: %s", mp)
        return 0

    # ---- 5. calc_indicators 批量筛查 ----
    results = screen_batch(pool, args.profile)

    passed = [r for r in results if "error" not in r and r.get("pass_count", 0) >= args.min_pass]
    log.info("筛查完成: %d 只评估，%d 只通过（≥%d 维）",
             len(results), len(passed), args.min_pass)

    # ---- 6. PNG 报告（可选）----
    if args.report and passed:
        log.info("为 %d 只通过标的生成 PNG 报告…", len(passed))
        for r in passed:
            cmd = ["uv", "run", "python", "services/calc_indicators/main.py",
                   r["code"], "-p", args.profile, "-r", "--skip-all-fail"]
            if r.get("name"):
                cmd += ["--name", r["name"]]
            try:
                subprocess.run(cmd, cwd=str(_PROJECT_ROOT), timeout=90)
            except subprocess.TimeoutExpired:
                log.warning("  PNG 生成超时 %s", r["code"])

    # ---- 7. yanbao-info（可选）----
    if args.with_yanbao and passed:
        log.info("为 %d 只通过标的拉取研报…", len(passed))
        for r in passed:
            run_yanbao_for(r["code"])

    # ---- 8. 汇总报告 ----
    summary = {
        "date": args.date, "plate_type": args.plate_type,
        "profile": args.profile, "min_pass": args.min_pass,
        "pool_size": len(pool), "passed_count": len(passed),
        "mapping_added": added,
        "with_yanbao": args.with_yanbao, "with_sell_init": args.with_sell_init,
        "leader_pool": pool, "screen_results": results,
    }
    if args.with_sell_init:
        summary["sell_init_commands"] = [
            build_sell_init_command(r, args.profile, args.date) for r in passed
        ]
    jp, mp = write_reports(summary, args.date)
    log.info("汇总报告: %s", mp)
    log.info("JSON: %s", jp)

    # ---- 控制台打印通过标的速览 ----
    if passed:
        print()
        print("=" * 60)
        print(f"✅ 通过筛查（≥{args.min_pass} 维）: {len(passed)} 只")
        print("=" * 60)
        for r in passed:
            yn = lambda b: "✅" if b else "❌"
            print(f"  {r['code']} {r.get('name',''):<8} "
                  f"A{yn(r['a_pass'])} B{yn(r['b_pass'])} C{yn(r['c_pass'])} "
                  f"({r['pass_count']}/3)  盈亏比 {r.get('rr_ratio','—')}  "
                  f"现价 {r.get('latest_close','—')}")
        if args.with_sell_init:
            print()
            print("📌 sell_monitor 登记命令（请人工确认 entry-price 后执行）:")
            for cmd in summary["sell_init_commands"]:
                print(f"  {cmd}")
    else:
        print()
        print("=" * 60)
        print(f"❌ 本日无标的通过筛查（≥{args.min_pass} 维），观望")
        print("=" * 60)

    log.info("=" * 30 + " 龙头复盘流水线结束 " + "=" * 30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
