#!/usr/bin/env python3
"""场景化批量复盘编排器。

按「复盘场景」把多个服务串成流水线，单任务超时隔离，失败不阻断后续。
跑完即退，无常驻循环。命令均以 uv run 在项目根执行。

──────── 场景预设（--scenario / -s）────────
  daily     (默认) 日常盘后复盘
            alarming → bankuai → news → holdings → report
  macro     宏观复盘
            macro_data → macro_charts
  stock     个股复盘，需 --symbol
            calc → yanbao → sell → report
  pipeline  龙头批量筛查（详见 scripts/run_leader_pipeline.py）
            pipeline
  full      完整复盘：macro + daily + pipeline；给 --symbol 再补 stock 三件套

──────── 参数说明 ────────
场景与标的：
  -s, --scenario    复盘场景，见上，默认 daily
  --date            参考日期 YYYY-MM-DD，默认昨天
  --symbol          个股代码（stock 必填；full/pipeline 给了更完整）

配置透传（传给下游 calc/alarming/sell/pipeline/bankuai）：
  -p, --profile     配置档，默认 aggressive_short
  --plate-type      板块类型 概念/题材/行业，默认 概念
  --report          calc 加 -r 生成 PNG 报告

精细控制：
  --include  a,b,c  在场景基础上追加服务
  --exclude  a,b,c  从场景中移除服务
  --skip-slow       跳过触网重服务（无网/赶时间）
                    慢服务：bankuai yanbao alarming macro_data news pipeline

执行控制：
  --timeout N       单任务超时秒数，默认 600
  --dry-run         只打印命令不执行
  -v, --verbose     详细日志
  --list-services   列出全部服务+场景后退出（查 --include/--exclude 合法值）
  -h, --help        显示本帮助

服务名速查（--include/--exclude 用这些名字）：
  宏观  macro_data  macro_charts
  大盘  alarming
  板块  bankuai
  个股  calc  yanbao  sell
  辅助  news  holdings  report  pipeline

──────── 用法示例 ────────
  # 日常盘后复盘（默认）
  uv run scripts/run_all.py

  # 宏观复盘
  uv run scripts/run_all.py -s macro

  # 个股复盘（超短线 + PNG 报告）
  uv run scripts/run_all.py -s stock --symbol 600550 -p ultra_short --report

  # 完整复盘 + 个股
  uv run scripts/run_all.py -s full --symbol 600550

  # 跳过慢服务（无网/赶时间，只跑 holdings + report）
  uv run scripts/run_all.py --skip-slow

  # 自定义：日常 + 加研报 - 去新闻（需 --symbol）
  uv run scripts/run_all.py --include yanbao --exclude news --symbol 600550

  # 预览将执行的命令
  uv run scripts/run_all.py --dry-run -v

  # 查全部服务与场景
  uv run scripts/run_all.py --list-services

──────── 说明 ────────
  退出码：0=全部成功；2=参数错误；N(≥1)=失败任务数
  参考日：默认昨天；早上复盘取昨日数据，晚上加 --date 今日
  失败隔离：单任务失败/超时不阻断后续任务，末尾打印 ✓/✗ 汇总
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PER_TASK_TIMEOUT = 600          # 单任务默认 600s
GLOBAL_TIMEOUT = 1800          # 全流程 30 分钟硬上限

# 日志（与各服务一致；缺 core 时降级到 logging）
try:
    from core.logger import get_logger
    log = get_logger("run_all")
except Exception:
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("run_all")


# ====================================================================
# 服务注册表
#   cmd        : 由 args 生成命令列表的函数
#   scenarios  : 哪些预设场景包含此服务
#   slow       : --skip-slow 时跳过
#   needs_symbol: 需要 --symbol（stock 场景必填）
# ====================================================================
SERVICES: dict[str, dict] = {
    # —— 宏观层 ——
    "macro_data": {
        "desc": "宏观数据采集（CPI/PPI/M2 等）",
        "cmd": lambda a: ["uv", "run", "services/macro-data-service/main.py"],
        "scenarios": {"macro", "full"},
        "slow": True, "needs_symbol": False,
    },
    "macro_charts": {
        "desc": "宏观指标可视化",
        "cmd": lambda a: ["uv", "run", "services/macro-charts/main.py", "--output", "html"],
        "scenarios": {"macro", "full"},
        "slow": False, "needs_symbol": False,
    },

    # —— 大盘层 ——
    "alarming": {
        "desc": "大盘预警 + 板块轮动 + 连板",
        "cmd": lambda a: ["uv", "run", "services/alarming_monitor/main.py",
                          "--date", a.date, "--profile", a.profile]
                         + (["--no-margin"] if a.skip_slow else []),
        "scenarios": {"daily", "full"},
        "slow": True, "needs_symbol": False,
    },

    # —— 板块层 ——
    "bankuai": {
        "desc": "板块扫描 + 三级龙头梯队",
        "cmd": lambda a: ["uv", "run", "services/bankuai-service/main.py",
                          "--date", a.date, "--plate-type", a.plate_type,
                          "--top-n", "5", "--quiet"],
        "scenarios": {"daily", "full"},
        "slow": True, "needs_symbol": False,
    },

    # —— 个股层 ——
    "calc": {
        "desc": "开仓三维度筛查",
        "cmd": lambda a: ["uv", "run", "python", "services/calc_indicators/main.py",
                          a.symbol, "-p", a.profile]
                         + (["-r"] if a.report else []),
        "scenarios": {"stock"},
        "slow": False, "needs_symbol": True,
    },
    "yanbao": {
        "desc": "券商研报摘要",
        "cmd": lambda a: ["uv", "run", "services/yanbao-info/main.py",
                          "--symbol", a.symbol, "--quiet"],
        "scenarios": {"stock"},
        "slow": True, "needs_symbol": True,
    },
    "sell": {
        "desc": "持仓卖出监控",
        "cmd": lambda a: ["uv", "run", "services/sell_monitor/main.py",
                          "--symbol", a.symbol, "--date", a.date],
        "scenarios": {"stock"},
        "slow": False, "needs_symbol": True,
    },

    # —— 辅助 ——
    "news": {
        "desc": "财经新闻采集",
        "cmd": lambda a: ["uv", "run", "python", "services/crawler_news/main.py", "crawl"],
        "scenarios": {"daily", "full"},
        "slow": True, "needs_symbol": False,
    },
    "holdings": {
        "desc": "全部持仓状态一览",
        "cmd": lambda a: ["uv", "run", "services/sell_monitor/main.py", "--list"],
        "scenarios": {"daily", "full"},
        "slow": False, "needs_symbol": False,
    },
    "report": {
        "desc": "单标的复盘报告",
        "cmd": lambda a: ["uv", "run", "services/generate_report/main.py",
                          "--date", a.date]
                         + (["--symbol", a.symbol] if a.symbol else []),
        "scenarios": {"daily", "stock", "full"},
        "slow": False, "needs_symbol": False,
    },
    "pipeline": {
        "desc": "龙头流水线（bankuai→calc 批量筛查）",
        "cmd": lambda a: ["uv", "run", "python", "scripts/run_leader_pipeline.py",
                          "--date", a.date, "--plate-type", a.plate_type,
                          "-p", a.profile],
        "scenarios": {"pipeline", "full"},
        "slow": True, "needs_symbol": False,
    },
}

# 场景 → 服务有序列表（顺序即执行顺序）
SCENARIO_ORDER: dict[str, list[str]] = {
    "macro":     ["macro_data", "macro_charts"],
    "daily":     ["alarming", "bankuai", "news", "holdings", "report"],
    "stock":     ["calc", "yanbao", "sell", "report"],
    "pipeline":  ["pipeline"],
    "full":      ["macro_data", "macro_charts", "alarming", "bankuai",
                  "news", "holdings", "pipeline", "report"],
}


# ====================================================================
# 任务执行
# ====================================================================
def run_task(name: str, cmd: list[str], timeout: int, deadline: float, dry_run: bool) -> bool:
    """执行单个任务。返回 True=成功，False=失败/超时。"""
    log.info("▶ [%s] %s", name, " ".join(cmd))
    if dry_run:
        return True

    remaining = deadline - time.time()
    if remaining <= 0:
        log.error("⏰ 全流程超时，跳过 %s", name)
        return False
    task_timeout = min(timeout, int(remaining) + 1)

    try:
        result = subprocess.run(
            cmd, cwd=str(PROJECT_ROOT), timeout=task_timeout,
            capture_output=True, text=True,
        )
    except subprocess.TimeoutExpired:
        log.error("⏰ [%s] 超时（>%ds）", name, task_timeout)
        return False
    except Exception as e:
        log.error("💥 [%s] 异常: %s", name, e)
        return False

    if result.stdout.strip():
        print(result.stdout.rstrip())
    if result.returncode != 0:
        log.error("✗ [%s] 退出码 %d", name, result.returncode)
        if result.stderr.strip():
            print(result.stderr.rstrip(), file=sys.stderr)
        return False
    log.info("✓ [%s] 完成", name)
    return True


# ====================================================================
# 参数解析
# ====================================================================
def _parse_args(argv):
    p = argparse.ArgumentParser(
        description="场景化批量复盘编排器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("-s", "--scenario",
                   choices=["daily", "macro", "stock", "pipeline", "full"],
                   default="daily", help="复盘场景（默认 daily）")
    p.add_argument("--date", default=(date.today() - timedelta(days=1)).isoformat(),
                   help="参考日期 YYYY-MM-DD，默认昨天")
    p.add_argument("--symbol", default="",
                   help="个股代码（stock 场景必填；full/pipeline 给了会更完整）")

    p.add_argument("-p", "--profile", default="aggressive_short",
                   help="配置档，传给 calc/alarming/sell/pipeline，默认 aggressive_short")
    p.add_argument("--plate-type", default="概念", choices=["概念", "题材", "行业"],
                   help="板块类型，传给 bankuai/pipeline，默认 概念")
    p.add_argument("--report", action="store_true",
                   help="calc_indicators 生成 PNG 报告（-r）")

    p.add_argument("--include", default="", help="追加服务（逗号分隔，如 yanbao,calc）")
    p.add_argument("--exclude", default="", help="移除服务（逗号分隔，如 news,bankuai）")
    p.add_argument("--skip-slow", action="store_true",
                   help="跳过触网重服务（无网/赶时间，见下慢服务清单）")

    p.add_argument("--timeout", type=int, default=PER_TASK_TIMEOUT,
                   help=f"单任务超时秒数，默认 {PER_TASK_TIMEOUT}")
    p.add_argument("--dry-run", action="store_true", help="只打印命令不执行")
    p.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    p.add_argument("--list-services", action="store_true", help="列出所有可用服务后退出")
    return p.parse_args(argv)


# ====================================================================
# 主入口
# ====================================================================
def main() -> int:
    args = _parse_args(sys.argv[1:])

    # --list-services：列出服务清单后退出
    if args.list_services:
        print("可用服务：")
        for name, spec in SERVICES.items():
            tags = []
            if spec["slow"]:
                tags.append("慢")
            if spec["needs_symbol"]:
                tags.append("需--symbol")
            tag_str = f"  [{', '.join(tags)}]" if tags else ""
            print(f"  {name:<14} {spec['desc']}{tag_str}")
        print("\n场景预设：")
        for sc, svcs in SCENARIO_ORDER.items():
            print(f"  {sc:<10} → {', '.join(svcs)}")
        return 0

    if args.verbose:
        try:
            import logging as _l
            _l.getLogger().setLevel(_l.INFO)
        except Exception:
            pass

    # 场景校验：stock 必须给 --symbol
    if args.scenario == "stock" and not args.symbol:
        log.error("stock 场景需要 --symbol 参数")
        return 2

    # 组装服务列表
    services = _resolve_services(args)
    if not services:
        log.error("解析后服务列表为空，无任务可执行")
        return 1

    # 校验 needs_symbol 服务
    for name in services:
        if SERVICES[name]["needs_symbol"] and not args.symbol:
            log.error("服务 %s 需要 --symbol，当前未提供（已跳过）", name)
            return 2

    # 打印执行计划
    print("=" * 64)
    print(f"场景: {args.scenario}  日期: {args.date}  "
          + (f"标的: {args.symbol}  " if args.symbol else "")
          + (f"[skip-slow] " if args.skip_slow else "")
          + (f"[dry-run] " if args.dry_run else ""))
    print(f"将执行 {len(services)} 个任务: {', '.join(services)}")
    print("=" * 64)

    deadline = time.time() + GLOBAL_TIMEOUT
    results: list[tuple[str, bool]] = []

    for name in services:
        spec = SERVICES[name]
        cmd = spec["cmd"](args)
        ok = run_task(name, cmd, args.timeout, deadline, args.dry_run)
        results.append((name, ok))

    # 汇总
    print()
    print("=" * 64)
    print("执行汇总")
    print("=" * 64)
    failed = 0
    for name, ok in results:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name}")
        if not ok:
            failed += 1

    if failed:
        log.warning("完成：%d 成功 / %d 失败", len(results) - failed, failed)
    else:
        log.info("全部成功：%d 个任务", len(results))
    return 1 if failed else 0


def _resolve_services(args) -> list[str]:
    """根据 scenario + include/exclude/skip-slow 解析出有序服务列表。"""
    base = list(SCENARIO_ORDER.get(args.scenario, []))

    # full 场景若给了 --symbol，补 stock 三件套
    if args.scenario == "full" and args.symbol:
        for s in ["calc", "yanbao", "sell"]:
            if s not in base:
                base.append(s)

    # include（追加，去重，保持 include 顺序）
    for inc in [x.strip() for x in args.include.split(",") if x.strip()]:
        if inc in SERVICES and inc not in base:
            base.append(inc)

    # exclude（移除）
    exc = {x.strip() for x in args.exclude.split(",") if x.strip()}
    base = [s for s in base if s not in exc]

    # skip-slow（移除慢服务）
    if args.skip_slow:
        base = [s for s in base if not SERVICES[s]["slow"]]

    # 过滤掉 needs_symbol 但没给 symbol 的（避免无谓失败）
    if not args.symbol:
        base = [s for s in base if not SERVICES[s]["needs_symbol"]]

    return base


if __name__ == "__main__":
    sys.exit(main())
