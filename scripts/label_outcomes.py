"""结果打标 + 评分卡聚合一体入口（P5.3 学习地基）。

扫描 swing_signal / committee_session，按拍板口径（波段 T+5/10/20 + 触位、
价值 T+20/60/250）到期打标入 outcome_label，再按滚动 90 天窗口聚合
agent_scorecard（P4.7 学习注入侧直接消费）。

幂等：已落库标签按主键跳过，评分卡按 (agent_name, window_end) 先删后插，
每日重跑安全。标签未到期/未触位不落库，次日重跑继续观察。

用法（仓库根目录）：
    .venv\\Scripts\\python.exe scripts\\label_outcomes.py
    .venv\\Scripts\\python.exe scripts\\label_outcomes.py --as-of 2026-10-17
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from viqtrader.config.loader import Config  # noqa: E402
from viqtrader.learning.labeler import label_swing_signals, label_valuation_sessions  # noqa: E402
from viqtrader.learning.scorecard import write_scorecards  # noqa: E402
from viqtrader.store.db import connect  # noqa: E402
from viqtrader.store.migrate import migrate  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="结果打标 + 评分卡聚合（P5.3）")
    parser.add_argument(
        "--as-of",
        default=None,
        help="评分卡窗口截止日 YYYY-MM-DD（默认今天）",
    )
    args = parser.parse_args()
    window_end = datetime.strptime(args.as_of, "%Y-%m-%d").date() if args.as_of else date.today()

    config = Config.load(REPO_ROOT / "config")
    conn = connect(config)
    migrate(conn)

    swing_report = label_swing_signals(conn)
    value_report = label_valuation_sessions(conn)
    print(
        f"[打标-波段] 扫描 {swing_report.scanned}，落库 {swing_report.inserted}，"
        f"观察中 {swing_report.pending}，告警 {len(swing_report.warnings)}"
    )
    print(
        f"[打标-价值] 扫描 {value_report.scanned}，落库 {value_report.inserted}，"
        f"观察中 {value_report.pending}，告警 {len(value_report.warnings)}"
    )
    for w in (*swing_report.warnings, *value_report.warnings):
        print(f"  警告: {w}")

    written = write_scorecards(conn, window_end=window_end)
    if written:
        print(f"[评分卡] 窗口截止 {window_end}，写入: {', '.join(written)}")
    else:
        print(f"[评分卡] 窗口截止 {window_end}，窗口内无样本，未写入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
