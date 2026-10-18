"""价值初筛 CLI（P4.4 三件套补全：初筛零 LLM）。

排除法漏斗（能力圈→硬剔除→质量→估值闸门→质量排序），结果写 value_candidate
周快照；候选池供人工挑选取做投委会（run_committee.py）→ 狩猎清单
（watchlist.py add），三段人工链路各有入口。

用法（仓库根目录）：
    .venv\\Scripts\\python.exe scripts\\screen_value.py                  # 最新交易日
    .venv\\Scripts\\python.exe scripts\\screen_value.py --date 2026-10-16
    .venv\\Scripts\\python.exe scripts\\screen_value.py --top 20        # 只展示前 20
    .venv\\Scripts\\python.exe scripts\\screen_value.py --all           # 含未过闸候选

退出码：成功 0 / 数据不可用（无行情）1 / 参数错误 2。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from viqtrader.agents.value.screen import run_value_screen  # noqa: E402
from viqtrader.config.loader import Config  # noqa: E402
from viqtrader.store.db import connect  # noqa: E402
from viqtrader.store.migrate import migrate  # noqa: E402


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main() -> int:
    parser = argparse.ArgumentParser(description="价值初筛（排除法漏斗，零 LLM）")
    parser.add_argument(
        "--date", type=_parse_date, default=None, help="筛选业务日（缺省最新交易日）"
    )
    parser.add_argument("--top", type=int, default=15, help="展示前 N 只（0=不展示明细）")
    parser.add_argument("--all", action="store_true", help="展示全部质量达标候选（含未过估值闸门）")
    args = parser.parse_args()

    if args.top < 0:
        print("--top 不能为负", file=sys.stderr)
        return 2

    config = Config.load(REPO_ROOT / "config")
    conn = connect(config)
    migrate(conn)

    latest_row = conn.execute("SELECT max(trade_date) FROM fact_daily_bar").fetchone()
    if latest_row is None or latest_row[0] is None:
        print("fact_daily_bar 为空，请先执行 scripts\\sync_market.py 同步行情", file=sys.stderr)
        return 1

    report = run_value_screen(conn, config, args.date)
    print(
        f"筛选日 {report.screen_date}：universe {report.universe} → "
        f"能力圈 {report.after_circle} → 硬剔除后 {report.after_hard} → "
        f"质量达标 {report.after_quality}（估值闸门通过 {report.gate_passed}）"
    )

    shown = report.candidates if args.all else [c for c in report.candidates if c.gate_passed]
    if args.top > 0:
        shown = shown[: args.top]

    if not shown:
        print("无候选（质量达标数 0 或闸门全未过；--all 查看含未过闸）")
        return 0

    print(
        f"\n{'代码':<8} {'名称':<10} {'行业':<10} {'ROE均':>7} {'达标年':>5} {'FCF均':>9} "
        f"{'负债率':>7} {'PE_TTM':>8} {'PE分位':>7} {'闸门':>4}"
    )
    for c in shown:
        print(
            f"{c.code:<8} {c.name:<10} {(c.industry or '—'):<10} "
            f"{c.roe_mean:>6.1%} {c.roe_years:>5} "
            f"{'—' if c.fcf_mean is None else format(c.fcf_mean, '.2e'):>9} "
            f"{'—' if c.debt_ratio_latest is None else format(c.debt_ratio_latest, '.1%'):>7} "
            f"{'—' if c.pe_ttm is None else format(c.pe_ttm, '.1f'):>8} "
            f"{'—' if c.pe_percentile is None else format(c.pe_percentile, '.0%'):>7} "
            f"{'✓' if c.gate_passed else '✗':>4}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
