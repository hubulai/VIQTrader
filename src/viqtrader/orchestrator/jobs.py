"""每日 DAG 任务定义（P5.1）。

任务形态分两类：
- 子进程任务：sync_market / run_swing_pipeline / review_portfolio —— 外部依赖重
  （QMT 终端、LLM），进程隔离避免粘连，复用既有 CLI 与盒带归档；
- 进程内任务：quality（轻量 SQL 口径）/ label_outcomes（learning API）—— 库内操作，
  快速且错误可精确捕获。

占位任务（placeholder=True，固定 skipped 留痕）：watchlist_watch（盯价→P5.2）、
risk_scan（止损扫描→engine/risk 待立项）、value_screen（价值初筛→P4.4 欠账待立项）。
占位任务仅当对应模块落地后替换为真实实现，DAG 结构不变。

财报月复盘：run_date 落在 config `orchestrator.earnings_months` 且为
`orchestrator.review_weekday`（isoweekday）时追加 review_value/review_swing，
区间=当季季首→run_date；价值初筛占位同日常驻周六注册，与未来「每周」口径对齐。
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb

from viqtrader.alerting.router import AlertRouter
from viqtrader.config.loader import Config
from viqtrader.learning.labeler import label_swing_signals, label_valuation_sessions
from viqtrader.learning.scorecard import write_scorecards

from .dag import Task, TaskResult

logger = logging.getLogger(__name__)

# 子进程输出摘要只保留末尾若干行，避免状态文件膨胀
_TAIL_LINES = 3
_DETAIL_MAX = 200


@dataclass(slots=True)
class JobContext:
    """任务执行上下文。conn 与进程内任务共用同一连接（DuckDB 单写者，串行安全）。"""

    config: Config
    conn: duckdb.DuckDBPyConnection
    repo_root: Path
    run_date: date
    alerter: AlertRouter

    @property
    def run_date_str(self) -> str:
        return self.run_date.isoformat()


# ---------------------------------------------------------------------------
# 子进程任务
# ---------------------------------------------------------------------------


def _run_script(ctx: JobContext, script: str, *args: str, timeout_seconds: int) -> TaskResult:
    """以当前解释器子进程跑 scripts/<script>，退出码非 0 → failed。"""
    cmd = [sys.executable, str(ctx.repo_root / "scripts" / script), *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=ctx.repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return TaskResult(status="failed", error=f"超时（>{timeout_seconds}s）: {script}")
    tail = [line for line in (proc.stdout or "").strip().splitlines() if line][-_TAIL_LINES:]
    summary = " | ".join(tail)[-_DETAIL_MAX:]
    if proc.returncode != 0:
        err_tail = [line for line in (proc.stderr or "").strip().splitlines() if line][-1:]
        hint = f"；stderr: {err_tail[0][-_DETAIL_MAX:]}" if err_tail else ""
        return TaskResult(status="failed", error=f"exit={proc.returncode} {summary}{hint}")
    return TaskResult(status="ok", detail=summary)


def _job_sync_daily(ctx: JobContext) -> TaskResult:
    timeout = int(ctx.config.get("orchestrator.sync_timeout_seconds", 1800))
    return _run_script(ctx, "sync_market.py", timeout_seconds=timeout)


def _job_swing_signal(ctx: JobContext) -> TaskResult:
    timeout = int(ctx.config.get("orchestrator.script_timeout_seconds", 1800))
    return _run_script(ctx, "run_swing_pipeline.py", timeout_seconds=timeout)


def _make_review_job(account_id: str) -> Task[JobContext]:
    def _run(ctx: JobContext) -> TaskResult:
        quarter_start = date(ctx.run_date.year, 3 * ((ctx.run_date.month - 1) // 3) + 1, 1)
        timeout = int(ctx.config.get("orchestrator.script_timeout_seconds", 1800))
        # review_portfolio 退出码语义：failed=1；unavailable/empty/skipped=0（P4.6 既定）
        return _run_script(
            ctx,
            "review_portfolio.py",
            "--account",
            account_id,
            "--start",
            quarter_start.isoformat(),
            "--end",
            ctx.run_date_str,
            timeout_seconds=timeout,
        )

    task_name = f"review_{account_id.removeprefix('acc_')}"
    return Task(name=task_name, run=_run, depends_on=("sync_daily",))


# ---------------------------------------------------------------------------
# 进程内任务
# ---------------------------------------------------------------------------


def _job_quality(ctx: JobContext) -> TaskResult:
    """轻量质检口径（v1）：最新交易日 bar 覆盖率 + 当日重复键 + 数据陈旧度。

    阈值 `orchestrator.quality_min_coverage`（默认 0.85）：最新交易日有 bar 的
    SH/SZ/BJ 标的数 / dim_stock 同口径总数。逐 code 全量质检归 audit_db（P7）。
    """
    conn = ctx.conn
    row = conn.execute("SELECT max(trade_date) FROM fact_daily_bar").fetchone()
    latest = row[0] if row else None
    if latest is None:
        return TaskResult(status="failed", error="fact_daily_bar 为空，请先完成数据同步")

    row = conn.execute("SELECT count(*) FROM dim_stock WHERE market IN ('SH','SZ','BJ')").fetchone()
    total = int(row[0]) if row else 0
    row = conn.execute(
        "SELECT count(DISTINCT code) FROM fact_daily_bar WHERE trade_date = ?", [latest]
    ).fetchone()
    covered = int(row[0]) if row else 0
    row = conn.execute(
        """
        SELECT count(*) FROM (
            SELECT code, trade_date FROM fact_daily_bar WHERE trade_date = ?
            GROUP BY code, trade_date HAVING count(*) > 1
        )
        """,
        [latest],
    ).fetchone()
    dup = int(row[0]) if row else 0

    warnings: list[str] = []
    coverage = (covered / total) if total else 0.0
    min_coverage = float(ctx.config.get("orchestrator.quality_min_coverage", 0.85))
    if total == 0:
        warnings.append("dim_stock 无 SH/SZ/BJ 标的")
    elif coverage < min_coverage:
        warnings.append(
            f"最新交易日覆盖率 {coverage:.1%}（{covered}/{total}）低于阈值 {min_coverage:.0%}"
        )
    if dup:
        warnings.append(f"最新交易日重复主键 {dup} 组")
    stale_days = (ctx.run_date - latest).days
    if stale_days > int(ctx.config.get("orchestrator.quality_stale_days", 5)):
        warnings.append(f"行情最新交易日 {latest} 距业务日期 {stale_days} 天，疑似数据陈旧")

    for w in warnings:
        ctx.alerter.emit("WARN", "quality", w)
    detail = f"最新交易日 {latest}，覆盖率 {coverage:.1%}（{covered}/{total}），重复键 {dup}"
    return TaskResult(status="ok", detail=detail)


def _job_label_outcomes(ctx: JobContext) -> TaskResult:
    """打标 + 评分卡聚合（P5.3 入口进程内复用，幂等每日重跑安全）。"""
    swing = label_swing_signals(ctx.conn)
    value = label_valuation_sessions(ctx.conn)
    for w in (*swing.warnings, *value.warnings):
        ctx.alerter.emit("WARN", "label_outcomes", w)
    written = write_scorecards(ctx.conn, window_end=ctx.run_date)
    detail = (
        f"打标 波段+{swing.inserted}/价值+{value.inserted}"
        f"（观察中 {swing.pending + value.pending}），评分卡 {len(written)} 行"
    )
    return TaskResult(status="ok", detail=detail)


# ---------------------------------------------------------------------------
# 占位任务
# ---------------------------------------------------------------------------

_PLACEHOLDER_REASONS = {
    "watchlist_watch": "盯价：狩猎清单状态机归 P5.2",
    "risk_scan": "止损扫描：engine/risk 持仓巡检待立项",
    "value_screen": "价值初筛：P4.4 三件套欠账，待立项",
}


def _placeholder(name: str) -> Task[JobContext]:
    reason = _PLACEHOLDER_REASONS[name]

    def _run(ctx: JobContext) -> TaskResult:  # 不会被调用（placeholder 短路）
        return TaskResult(status="skipped", detail=reason)

    return Task(name=name, run=_run, depends_on=("sync_daily",), placeholder=True)


# ---------------------------------------------------------------------------
# DAG 装配
# ---------------------------------------------------------------------------


def is_review_day(run_date: date, earnings_months: list[int], review_weekday: int) -> bool:
    """财报月内的复盘日（isoweekday：1=周一 … 7=周日）。"""
    return run_date.month in earnings_months and run_date.isoweekday() == review_weekday


def build_daily_tasks(config: Config, run_date: date) -> list[Task[JobContext]]:
    """装配当日任务清单（注册序即拓扑序，依赖必须先注册）。"""
    tasks: list[Task[JobContext]] = [
        Task(name="sync_daily", run=_job_sync_daily),
        Task(name="quality", run=_job_quality, depends_on=("sync_daily",)),
        Task(name="swing_signal", run=_job_swing_signal, depends_on=("quality",)),
        Task(name="label_outcomes", run=_job_label_outcomes),
        _placeholder("watchlist_watch"),
        _placeholder("risk_scan"),
    ]

    earnings_months = [int(m) for m in config.get("orchestrator.earnings_months", [1, 4, 8, 10])]
    review_weekday = int(config.get("orchestrator.review_weekday", 6))
    if is_review_day(run_date, earnings_months, review_weekday):
        for account_id in config.get("orchestrator.review_accounts", ["acc_value", "acc_swing"]):
            tasks.append(_make_review_job(str(account_id)))
        tasks.append(_placeholder("value_screen"))
    return tasks


__all__ = ["JobContext", "build_daily_tasks", "is_review_day"]
