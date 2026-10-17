"""波段初筛信号回测 CLI（P4.5.1，零 LLM，不落库）。

对历史区间逐交易日 as-of 重放确定性初筛，按双口径（固定持有 N 日收益 /
ATR 推算触位出场）统计信号质量，产出明细 + 汇总两份 CSV 与控制台汇总表。
支持 --param 单组覆盖与 --grid 参数网格（笛卡尔积逐组合独立统计）。

用法（仓库根目录）：
    .venv\\Scripts\\python.exe scripts\\backtest_swing.py --start 2026-07-01 --end 2026-10-16
    .venv\\Scripts\\python.exe scripts\\backtest_swing.py --start 2026-07-01 --end 2026-10-16 ^
        --strategy trend_momentum --param volume_ratio_min=2.0
    .venv\\Scripts\\python.exe scripts\\backtest_swing.py --start 2026-07-01 --end 2026-10-16 ^
        --grid volume_ratio_min=1.5,2.0 --grid pullback_band=0.03,0.05

前置条件：本地 DuckDB 已含覆盖回测区间 + 前向评估窗口的日 K 数据。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from viqtrader.agents.swing.backtest import (  # noqa: E402
    DEFAULT_HOLD_WINDOWS,
    TouchExitRule,
    expand_grid,
    run_backtest,
    write_csvs,
)
from viqtrader.config.loader import Config  # noqa: E402
from viqtrader.store.db import connect  # noqa: E402
from viqtrader.store.migrate import migrate  # noqa: E402


def _parse_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise SystemExit(f"日期格式错误（应为 YYYY-MM-DD）: {raw!r}") from None


def _parse_param_pairs(pairs: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--param 格式错误（应为 k=v）: {pair!r}")
        key, _, value = pair.partition("=")
        result[key.strip()] = value.strip()
    return result


def _parse_grid_pairs(pairs: list[str]) -> dict[str, list[str]]:
    grid: dict[str, list[str]] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--grid 格式错误（应为 k=v1,v2,...）: {pair!r}")
        key, _, values = pair.partition("=")
        items = [v.strip() for v in values.split(",") if v.strip()]
        if not items:
            raise SystemExit(f"--grid 至少给一个取值: {pair!r}")
        grid[key.strip()] = items
    return grid


def _fmt_pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.2f}%"


def main() -> int:
    parser = argparse.ArgumentParser(description="波段初筛信号回测（零 LLM，CSV 产出）")
    parser.add_argument("--start", required=True, help="回测起始日 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="回测截止日 YYYY-MM-DD")
    parser.add_argument(
        "--strategy", default=None, help="策略 id（默认读配置 swing_track.active_strategy）"
    )
    parser.add_argument(
        "--param", action="append", default=[], metavar="k=v", help="单组参数覆盖（可重复）"
    )
    parser.add_argument(
        "--grid",
        action="append",
        default=[],
        metavar="k=v1,v2",
        help="参数网格（可重复，笛卡尔积逐组合统计）；与 --param 叠加，--param 为公共覆盖",
    )
    parser.add_argument("--top-n", type=int, default=None, help="每日初筛候选上限")
    parser.add_argument(
        "--hold-windows",
        default=",".join(str(n) for n in DEFAULT_HOLD_WINDOWS),
        help="固定持有窗口（逗号分隔交易日数，默认 5,10,20）",
    )
    parser.add_argument("--stop-atr", type=float, default=2.0, help="触位出场：止损 ATR 倍数")
    parser.add_argument("--target-atr", type=float, default=3.0, help="触位出场：目标 ATR 倍数")
    parser.add_argument("--max-hold", type=int, default=20, help="触位出场：最长持有交易日")
    parser.add_argument("--out", default=None, help="CSV 输出目录（默认 data_dir/backtest）")
    args = parser.parse_args()

    start, end = _parse_date(args.start), _parse_date(args.end)
    if start > end:
        raise SystemExit("--start 不能晚于 --end")
    hold_windows = tuple(sorted({int(x) for x in args.hold_windows.split(",") if x.strip()}))
    if not hold_windows:
        raise SystemExit("--hold-windows 至少一个窗口")

    base_override = _parse_param_pairs(args.param)
    grid = _parse_grid_pairs(args.grid)
    combos = [{**base_override, **combo} for combo in expand_grid(grid)]

    config = Config.load(REPO_ROOT / "config")
    conn = connect(config)
    migrate(conn)

    rule = TouchExitRule(
        stop_atr_mult=args.stop_atr,
        target_atr_mult=args.target_atr,
        max_holding_days=args.max_hold,
    )
    result = run_backtest(
        conn,
        config,
        strategy_id=args.strategy,
        param_combos=combos,
        start=start,
        end=end,
        hold_windows=hold_windows,
        rule=rule,
        top_n=args.top_n,
    )

    print(f"策略: {result.strategy_id}  区间: {start} ~ {end}  参数组合数: {len(result.combos)}")
    for combo in result.combos:
        s = combo.summary
        tag = "baseline" if not s.params else " ".join(f"{k}={v}" for k, v in s.params.items())
        print(f"\n== 组合 [{tag}]  信号数 {s.signal_count} ==")
        for w in s.window_stats:
            print(
                f"  固定持有 {w.window:>2} 日: 样本 {w.samples:>3}  "
                f"胜率 {_fmt_pct(w.win_rate)}  均值 {_fmt_pct(w.mean_return)}  "
                f"中位 {_fmt_pct(w.median_return)}"
            )
        print(
            f"  触位出场(ATR): 胜率 {_fmt_pct(s.touch_win_rate)}  "
            f"均值 {_fmt_pct(s.touch_mean)}  中位 {_fmt_pct(s.touch_median)}  "
            f"均持有 {s.touch_avg_days or '-'} 日  "
            f"stop {s.reason_counts.get('stop', 0)} / target {s.reason_counts.get('target', 0)} "
            f"/ timeout {s.reason_counts.get('timeout', 0)}"
        )
        for p in s.pattern_breakdown:
            print(
                f"    形态 {p.pattern:<9}: {p.count:>3} 笔  "
                f"触位胜率 {_fmt_pct(p.touch_win_rate)}  均值 {_fmt_pct(p.touch_mean)}"
            )

    out_dir = (
        Path(args.out) if args.out else Path(str(config.get("platform.data_dir"))) / "backtest"
    )
    detail_path, summary_path = write_csvs(result, out_dir)
    print(f"\n明细 CSV: {detail_path}")
    print(f"汇总 CSV: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
