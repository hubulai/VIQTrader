"""P5.5 回放验证 CLI：重放历史决策 → 新旧规则判定 diff 报告（设计文档 12.1）。

只读离线：仅调用规则引擎 validate()，不走网关、不记账、不写库。
引擎/规则/配置改动后跑一次，人工审阅「旧通过 → 新拒绝」的回归项再上线。

用法（仓库根目录）：
    .venv\\Scripts\\python.exe tools\\replay.py                 # 近 90 天
    .venv\\Scripts\\python.exe tools\\replay.py --days 30
    .venv\\Scripts\\python.exe tools\\replay.py --start 2026-07-01 --end 2026-09-30
    .venv\\Scripts\\python.exe tools\\replay.py --account acc_value --strict
    .venv\\Scripts\\python.exe tools\\replay.py --json --out data/outputs/replay_report.json

退出码：回放完成 0 / --strict 且存在回归或数据库不可打开 1 / 参数错误 2。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from viqtrader.config.loader import Config  # noqa: E402
from viqtrader.engine.replay import (  # noqa: E402
    IntentDiff,
    ReplayReport,
    run_replay,
)
from viqtrader.store.db import connect, db_path_from_config  # noqa: E402


def _print_detail(d: IntentDiff) -> None:
    old_desc = "规则通过" if d.old_passed else f"规则拒绝（{d.old_reason or '—'}）"
    new_desc = "规则通过" if d.new_passed else (d.new_reason or "拒绝")
    print(f"\n[{d.diff_type}] {d.trade_date} {d.account_id} {d.code} {d.side} ({d.intent_id})")
    print(f"  旧：{d.old_status} / {old_desc}")
    print(f"  新：{'通过' if d.new_passed else '拒绝'} / {new_desc}")
    if d.skip_reason:
        print(f"  跳过原因：{d.skip_reason}")
    for r in d.rule_diffs:
        old_v = "—" if r.old_passed is None else ("pass" if r.old_passed else "fail")
        new_v = "—" if r.new_passed is None else ("pass" if r.new_passed else "fail")
        print(f"    {r.rule_id:<18} {old_v:>5} → {new_v:<5} {r.new_message}")
        if r.old_message and r.old_message.strip() != r.new_message.strip():
            print(f"      旧消息：{r.old_message}")


def _print_human(report: ReplayReport) -> None:
    print(f"== P5.5 回放验证 {report.start} ~ {report.end} ==")
    print(
        f"扫描决策 {report.scanned} 笔：REGRESSION {report.regressions} / FIXED {report.fixed} / "
        f"SHIFTED_REJECTION {report.shifted} / MESSAGE_ONLY {report.message_only} / "
        f"SAME {report.same} / SKIPPED {report.skipped}"
    )
    for d in report.diffs:
        if d.diff_type != "SAME":
            _print_detail(d)
    if report.same:
        print(f"\nSAME {report.same} 笔（判定无变化）")
    if report.notes:
        print("\n提示：")
        for note in report.notes:
            print(f"  - {note}")
    if report.regressions:
        print(
            f"\n⚠ 存在 {report.regressions} 笔 REGRESSION（旧通过 → 新拒绝），上线前需人工逐条确认"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="P5.5 回放验证（只读离线）")
    parser.add_argument(
        "--days", type=int, default=90, help="回放窗口天数（默认 90，自 --end 倒推）"
    )
    parser.add_argument("--start", type=str, default=None, help="起始日 YYYY-MM-DD（覆盖 --days）")
    parser.add_argument("--end", type=str, default=None, help="结束日 YYYY-MM-DD（默认今天）")
    parser.add_argument("--account", type=str, default=None, help="仅回放指定账户")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出报告（机器可读）")
    parser.add_argument("--out", type=str, default=None, help="同时把 JSON 报告写入指定路径")
    parser.add_argument(
        "--strict", action="store_true", help="存在 REGRESSION 时以退出码 1 结束（默认仅提示）"
    )
    args = parser.parse_args()

    if args.days < 0:
        print("--days 不能为负", file=sys.stderr)
        return 2
    try:
        end = date.fromisoformat(args.end) if args.end else date.today()
        start = date.fromisoformat(args.start) if args.start else end - timedelta(days=args.days)
    except ValueError as exc:
        print(f"日期格式错误（需 YYYY-MM-DD）：{exc}", file=sys.stderr)
        return 2
    if start > end:
        print(f"起始日 {start} 晚于结束日 {end}", file=sys.stderr)
        return 2

    config = Config.load(REPO_ROOT / "config")
    db_path = db_path_from_config(config)
    if not db_path.exists():
        print(f"数据库不存在：{db_path}（请先执行 scripts\\sync_market.py）", file=sys.stderr)
        return 1
    try:
        conn = connect(config, read_only=True)
    except Exception as exc:
        print(f"数据库不可打开：{exc}", file=sys.stderr)
        return 1

    try:
        report = run_replay(conn, config, start, end, account_id=args.account)
    finally:
        conn.close()

    payload: dict[str, Any] = report.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_human(report)

    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = REPO_ROOT / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if not args.json:
            print(f"\nJSON 报告已写入：{out_path}")

    if args.strict and report.regressions > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
