"""orchestrator 单测：DAG 拓扑/失败隔离、状态文件、任务装配、jobs 进程内任务。

子进程任务（sync/swing/review）不在单测中真实触发（依赖 QMT 终端与 LLM），
其形态由 dag 层 fake 任务等价覆盖。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

import duckdb
import pytest

from viqtrader.alerting.router import AlertRouter
from viqtrader.config.loader import Config
from viqtrader.orchestrator.dag import (
    DagRunner,
    RunReport,
    Task,
    TaskResult,
)
from viqtrader.orchestrator.dag import (
    JobContext as DagJobContext,
)
from viqtrader.orchestrator.jobs import (
    JobContext,
    _job_label_outcomes,
    _job_quality,
    build_daily_tasks,
    is_review_day,
)
from viqtrader.orchestrator.state import build_run_state, make_run_id, write_run_state

# ---------------------------------------------------------------------------
# dag.py
# ---------------------------------------------------------------------------


class _Ctx:
    @property
    def run_date_str(self) -> str:
        return "2026-10-18"


def _run_ok(name: str) -> Callable[[DagJobContext], TaskResult]:
    return lambda ctx: TaskResult(status="ok", detail=f"{name} done")


def _ok(name: str) -> Task:
    return Task(name=name, run=_run_ok(name))


def test_dag_all_ok_chain() -> None:
    order: list[str] = []
    tasks = [
        Task(name="a", run=lambda ctx: order.append("a") or TaskResult(status="ok")),
        Task(
            name="b",
            run=lambda ctx: order.append("b") or TaskResult(status="ok"),
            depends_on=("a",),
        ),
    ]
    report = DagRunner(tasks).execute(_Ctx())
    assert order == ["a", "b"]
    assert report.exit_code == 0
    assert [o.status for o in report.outcomes] == ["ok", "ok"]


def test_dag_failure_cascades_but_independent_branch_survives() -> None:
    def _boom(ctx: DagJobContext) -> TaskResult:
        raise RuntimeError("QMT 离线")

    tasks = [
        Task(name="sync", run=_boom),
        Task(name="quality", run=_run_ok("quality"), depends_on=("sync",)),
        Task(name="signal", run=_run_ok("signal"), depends_on=("quality",)),
        Task(name="label", run=_run_ok("label")),
    ]
    report = DagRunner(tasks).execute(_Ctx())
    by = {o.name: o for o in report.outcomes}
    assert by["sync"].status == "failed" and "RuntimeError" in by["sync"].error
    assert by["quality"].status == "skipped" and "sync(failed)" in by["quality"].skipped_reason
    assert by["signal"].status == "skipped" and "quality(skipped)" in by["signal"].skipped_reason
    assert by["label"].status == "ok"
    assert report.exit_code == 1


def test_dag_exit_one_when_task_returns_failed() -> None:
    tasks = [
        Task(name="a", run=lambda ctx: TaskResult(status="failed", error="x")),
        Task(name="b", run=_run_ok("b"), depends_on=("a",)),
    ]
    report = DagRunner(tasks).execute(_Ctx())
    assert report.exit_code == 1
    assert report.by_name("b").status == "skipped"


def test_dag_placeholder_never_runs() -> None:
    called: list[str] = []
    task = Task(
        name="watch",
        run=lambda ctx: called.append("x") or TaskResult(status="ok"),
        depends_on=("sync",),
        placeholder=True,
    )
    report = DagRunner([_ok("sync"), task]).execute(_Ctx())
    assert called == []
    outcome = report.by_name("watch")
    assert outcome.status == "skipped" and "占位" in outcome.skipped_reason


def test_dag_duplicate_name_rejected() -> None:
    with pytest.raises(ValueError, match="任务名重复"):
        DagRunner([_ok("a"), _ok("a")])


def test_dag_dependency_must_register_first() -> None:
    with pytest.raises(ValueError, match="依赖未先注册"):
        DagRunner([Task(name="b", run=_ok("b").run, depends_on=("a",))])


def test_dag_exit_zero_when_no_failure() -> None:
    report = DagRunner([_ok("a")]).execute(_Ctx())
    assert report.exit_code == 0
    assert report.by_name("a").duration_seconds >= 0


# ---------------------------------------------------------------------------
# state.py
# ---------------------------------------------------------------------------


def _sample_report() -> RunReport:
    tasks = [
        Task(name="a", run=lambda ctx: TaskResult(status="ok", detail="done")),
        Task(name="b", run=lambda ctx: TaskResult(status="failed", error="boom")),
    ]
    return DagRunner(tasks).execute(_Ctx())


def test_state_write_run_and_live(tmp_path: Path) -> None:
    state = build_run_state(
        run_id="2026-10-18_163000",
        run_date="2026-10-18",
        started_at=datetime(2026, 10, 18, 16, 30, 0),
        finished_at=datetime(2026, 10, 18, 16, 31, 0),
        report=_sample_report(),
        alert_counts={"CRITICAL": 1, "INFO": 1},
    )
    run_file = write_run_state(tmp_path, state)

    payload = json.loads(run_file.read_text(encoding="utf-8"))
    assert payload["run_id"] == "2026-10-18_163000"
    assert payload["exit_code"] == 1
    assert [t["name"] for t in payload["tasks"]] == ["a", "b"]
    assert payload["alert_counts"] == {"CRITICAL": 1, "INFO": 1}

    live = json.loads((tmp_path / "live_state.json").read_text(encoding="utf-8"))
    assert live["last_run_id"] == "2026-10-18_163000"
    assert live["failed_tasks"] == ["b"]
    # 原子写：无 tmp 残留
    assert list(tmp_path.rglob("*.tmp")) == []


def test_state_make_run_id_format() -> None:
    assert make_run_id("2026-10-18", now=datetime(2026, 10, 18, 9, 5, 3)) == "2026-10-18_090503"


# ---------------------------------------------------------------------------
# jobs.py：装配与进程内任务
# ---------------------------------------------------------------------------


def _ctx(
    config: Config, conn: duckdb.DuckDBPyConnection, tmp_path: Path, run_date: date
) -> JobContext:
    return JobContext(
        config=config,
        conn=conn,
        repo_root=tmp_path,
        run_date=run_date,
        alerter=AlertRouter(tmp_path / "alerts", console=False),
    )


def test_is_review_day() -> None:
    months = [1, 4, 8, 10]
    assert is_review_day(date(2026, 10, 17), months, 6) is True  # 周六
    assert is_review_day(date(2026, 10, 18), months, 6) is False  # 周日
    assert is_review_day(date(2026, 11, 7), months, 6) is False  # 非财报月周六


def test_build_daily_tasks_normal_day(config) -> None:
    names = [t.name for t in build_daily_tasks(config, date(2026, 10, 18))]  # 周日
    assert names == [
        "sync_daily",
        "quality",
        "swing_signal",
        "label_outcomes",
        "watchlist_watch",
        "risk_scan",
    ]
    assert "review_value" not in names


def test_build_daily_tasks_review_day(config) -> None:
    tasks = build_daily_tasks(config, date(2026, 10, 17))  # 财报月周六
    names = [t.name for t in tasks]
    assert names[-3:] == ["review_value", "review_swing", "value_screen"]
    by = {t.name: t for t in tasks}
    assert by["review_value"].depends_on == ("sync_daily",)
    assert by["value_screen"].placeholder is True
    # 占位与复盘任务依赖均指向先注册任务（DagRunner 构造不抛）
    DagRunner(tasks)


def test_job_quality_empty_bar_fails(config, conn, tmp_path: Path) -> None:
    ctx = _ctx(config, conn, tmp_path, date(2026, 10, 18))
    result = _job_quality(ctx)
    assert result.status == "failed"
    assert "fact_daily_bar 为空" in result.error


def test_job_quality_low_coverage_warns(config, conn, seed_stock, seed_bar, tmp_path: Path) -> None:
    seed_stock("600519")
    seed_stock("000858", market="SZ")
    seed_bar("600519", "2026-10-16")  # 仅 1/2 有 bar

    ctx = _ctx(config, conn, tmp_path, date(2026, 10, 16))
    result = _job_quality(ctx)
    assert result.status == "ok"
    assert "覆盖率 50.0%" in result.detail
    warns = [r for r in ctx.alerter.records if r.level == "WARN"]
    assert any("覆盖率" in r.message for r in warns)


def test_job_quality_stale_warns(config, conn, seed_stock, seed_bar, tmp_path: Path) -> None:
    seed_stock("600519")
    seed_bar("600519", "2026-10-01")

    ctx = _ctx(config, conn, tmp_path, date(2026, 10, 18))
    result = _job_quality(ctx)
    assert result.status == "ok"
    warns = [r for r in ctx.alerter.records if r.level == "WARN"]
    assert any("数据陈旧" in r.message for r in warns)


def test_job_label_outcomes_empty_db_safe(config, conn, tmp_path: Path) -> None:
    ctx = _ctx(config, conn, tmp_path, date(2026, 10, 18))
    result = _job_label_outcomes(ctx)
    assert result.status == "ok"
    assert "打标 波段+0/价值+0" in result.detail


def test_dag_full_smoke_with_fake_jobs(config, conn, tmp_path: Path) -> None:
    """全链冒烟：真实 quality/label 任务 + fake 子进程任务，等价 run_daily 主链。"""
    ctx = _ctx(config, conn, tmp_path, date(2026, 10, 18))
    tasks = [
        Task(name="sync_daily", run=lambda c: TaskResult(status="ok", detail="fake sync")),
        Task(name="quality", run=_job_quality, depends_on=("sync_daily",)),
        Task(name="label_outcomes", run=_job_label_outcomes),
        Task(
            name="risk_scan",
            run=lambda c: TaskResult(status="skipped"),
            depends_on=("sync_daily",),
            placeholder=True,
        ),
    ]
    report = DagRunner(tasks).execute(ctx)
    # quality 在空 bar 表上 failed → exit 1；label 独立 ok；占位 skipped
    assert report.by_name("quality").status == "failed"
    assert report.by_name("label_outcomes").status == "ok"
    assert report.by_name("risk_scan").status == "skipped"
    assert report.exit_code == 1

    state = build_run_state(
        run_id=make_run_id("2026-10-18"),
        run_date="2026-10-18",
        started_at=datetime.now(),
        finished_at=datetime.now(),
        report=report,
        alert_counts={},
    )
    run_file = write_run_state(tmp_path / "outputs", state)
    assert run_file.exists()
    assert (tmp_path / "outputs" / "live_state.json").exists()
