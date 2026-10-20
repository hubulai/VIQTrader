"""每日编排入口（P5.1 Orchestrator）。

由 Windows 任务计划（schtasks）每个交易日收盘后触发，跑完即退出，无常驻进程。
DAG：sync_daily → quality → swing_signal → export_swing（CSV 快照）；
label_outcomes 独立分支；watchlist_watch / risk_scan 占位留痕；
初筛周六追加 value_screen → export_value；财报月周六追加双账户复盘。

用法（仓库根目录）：
    .venv\\Scripts\\python.exe scripts\\run_daily.py                  # 全链
    .venv\\Scripts\\python.exe scripts\\run_daily.py --task quality   # 单任务补跑
    .venv\\Scripts\\python.exe scripts\\run_daily.py --date 2026-10-17
    .venv\\Scripts\\python.exe scripts\\run_daily.py --list           # 列出当日任务不执行

退出码：任一任务 failed=1；全 ok/skipped=0；参数/任务名非法=2。
状态文件：<data_dir>/outputs/runs/<run_id>.json + live_state.json（P5.4 契约运行侧）。
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from viqtrader.alerting.router import AlertRouter  # noqa: E402
from viqtrader.config.loader import Config  # noqa: E402
from viqtrader.orchestrator.dag import DagRunner, Task  # noqa: E402
from viqtrader.orchestrator.jobs import JobContext, build_daily_tasks  # noqa: E402
from viqtrader.orchestrator.state import build_run_state, make_run_id, write_run_state  # noqa: E402
from viqtrader.store.db import connect  # noqa: E402
from viqtrader.store.migrate import migrate  # noqa: E402

logger = logging.getLogger(__name__)


def _setup_logging(data_dir: Path) -> Path:
    """root logger：文件（每次运行独立）+ 控制台双输出，与 sync_market 同约定。"""
    log_dir = data_dir / "logs" / "orchestrator"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"run_daily-{datetime.now():%Y%m%d_%H%M%S}.log"
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console)
    return log_file


def main() -> int:
    parser = argparse.ArgumentParser(description="每日编排：同步→质检→波段信号→打标（P5.1）")
    parser.add_argument("--date", default=None, help="业务日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--task", default=None, help="只跑指定任务（补跑用，忽略依赖）")
    parser.add_argument("--list", action="store_true", help="只列出当日任务，不执行")
    args = parser.parse_args()

    try:
        run_date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    except ValueError:
        print(f"--date 格式非法（应为 YYYY-MM-DD）: {args.date!r}", file=sys.stderr)
        return 2

    config = Config.load(REPO_ROOT / "config")
    data_dir = Path(str(config.get("platform.data_dir", REPO_ROOT / "data")))
    log_file = _setup_logging(data_dir)

    tasks = build_daily_tasks(config, run_date)
    if args.list:
        print(f"业务日期 {run_date} 的任务清单：")
        for t in tasks:
            mark = "（占位）" if t.placeholder else ""
            deps = f" ← {','.join(t.depends_on)}" if t.depends_on else ""
            print(f"  {t.name}{mark}{deps}")
        return 0

    if args.task:
        matched = [t for t in tasks if t.name == args.task]
        if not matched:
            valid = ", ".join(t.name for t in tasks)
            print(f"未知任务 {args.task!r}，当日可选: {valid}", file=sys.stderr)
            return 2
        src = matched[0]
        tasks = [Task(name=src.name, run=src.run, placeholder=src.placeholder)]

    conn = connect(config)
    migrate(conn)
    alerter = AlertRouter(data_dir / "logs" / "alerts")
    ctx = JobContext(
        config=config, conn=conn, repo_root=REPO_ROOT, run_date=run_date, alerter=alerter
    )

    run_id = make_run_id(run_date.isoformat())
    started = datetime.now()
    logger.info("===== run_daily 启动 run_id=%s 业务日期=%s 日志=%s", run_id, run_date, log_file)

    report = DagRunner(tasks).execute(ctx)

    # 任务结果 → 告警级别映射：failed→CRITICAL，ok 摘要→INFO（末尾聚合），skipped→INFO
    for o in report.outcomes:
        if o.status == "failed":
            alerter.emit("CRITICAL", o.name, o.error or o.detail)
        elif o.status == "ok":
            alerter.emit("INFO", o.name, o.detail or "ok")
        else:
            alerter.emit("INFO", o.name, f"skipped: {o.skipped_reason}")
    alerter.flush()

    finished = datetime.now()
    counts = Counter(r.level for r in alerter.records)
    state = build_run_state(
        run_id=run_id,
        run_date=run_date.isoformat(),
        started_at=started,
        finished_at=finished,
        report=report,
        alert_counts=dict(counts),
    )
    state_file = write_run_state(data_dir / "outputs", state)

    print(f"\n===== run_daily 完成 run_id={run_id} 退出码={report.exit_code}")
    for o in report.outcomes:
        line = f"  [{o.status}] {o.name}（{o.duration_seconds:.1f}s）"
        line += f"  {o.detail or o.error or o.skipped_reason}"
        print(line)
    print(f"告警文件: {alerter.log_file}")
    print(f"状态文件: {state_file}")
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
