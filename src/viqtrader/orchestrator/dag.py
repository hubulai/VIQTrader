"""DAG 执行器（P5.1）：任务拓扑、依赖 skipped 传播、失败隔离、退出码。

设计口径：
- 任务按注册顺序执行，依赖仅允许指向**先注册**的任务（天然拓扑有序，无需排序算法）；
- 依赖任一 failed/skipped → 本任务 skipped 并向下游级联（失败隔离）；
- 任务抛异常收敛为 failed（error=repr），不中断后续无依赖分支；
- placeholder=True 的占位任务固定 skipped 留痕（盯价/止损扫描/价值初筛待模块落地）；
- 退出码：任一 failed → 1；全 ok/skipped → 0（与既有 CLI 语义一致；
  参数非法由入口 argparse/SystemExit 返回 2）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

logger = logging.getLogger(__name__)

TaskStatus = Literal["ok", "failed", "skipped"]


class JobContext(Protocol):
    """任务执行上下文（orchestrator.jobs.JobContext 的结构类型，dag 不越层依赖）。"""

    @property
    def run_date_str(self) -> str: ...


@dataclass(frozen=True, slots=True)
class TaskResult:
    """单任务执行结果。detail 为单行摘要（告警/状态文件复用）。"""

    status: TaskStatus
    detail: str = ""
    error: str = ""


@dataclass(frozen=True, slots=True)
class Task[CtxT: JobContext]:
    name: str
    run: Callable[[CtxT], TaskResult]
    depends_on: tuple[str, ...] = ()
    placeholder: bool = False


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    """执行后的任务状态（含耗时，供状态文件落盘）。"""

    name: str
    status: TaskStatus
    detail: str = ""
    error: str = ""
    duration_seconds: float = 0.0
    skipped_reason: str = ""


@dataclass(frozen=True, slots=True)
class RunReport:
    outcomes: tuple[TaskOutcome, ...] = field(default_factory=tuple)

    @property
    def exit_code(self) -> int:
        return 1 if any(o.status == "failed" for o in self.outcomes) else 0

    def by_name(self, name: str) -> TaskOutcome:
        for o in self.outcomes:
            if o.name == name:
                return o
        raise KeyError(name)


class DagRunner[CtxT: JobContext]:
    """按注册序执行的 DAG 执行器。CtxT 为具体上下文类型（jobs.JobContext）。"""

    def __init__(self, tasks: list[Task[CtxT]]) -> None:
        names = [t.name for t in tasks]
        if len(names) != len(set(names)):
            raise ValueError(f"任务名重复: {names}")
        seen: set[str] = set()
        for t in tasks:
            unknown = [d for d in t.depends_on if d not in seen]
            if unknown:
                raise ValueError(f"任务 {t.name} 依赖未先注册的任务: {unknown}")
            seen.add(t.name)
        self._tasks = tasks

    @property
    def task_names(self) -> tuple[str, ...]:
        return tuple(t.name for t in self._tasks)

    def execute(self, context: CtxT) -> RunReport:
        outcomes: list[TaskOutcome] = []
        status_by_name: dict[str, TaskStatus] = {}
        for task in self._tasks:
            outcome = self._execute_one(task, context, status_by_name)
            status_by_name[task.name] = outcome.status
            outcomes.append(outcome)
            logger.info("[dag] %s -> %s %s", task.name, outcome.status, outcome.detail)
        return RunReport(outcomes=tuple(outcomes))

    # ---------------- 内部 ----------------

    def _execute_one(
        self,
        task: Task[CtxT],
        context: CtxT,
        status_by_name: dict[str, TaskStatus],
    ) -> TaskOutcome:
        if task.placeholder:
            return TaskOutcome(name=task.name, status="skipped", skipped_reason="占位：待模块落地")
        blocking = [
            f"{d}({status_by_name[d]})" for d in task.depends_on if status_by_name[d] != "ok"
        ]
        if blocking:
            return TaskOutcome(
                name=task.name,
                status="skipped",
                skipped_reason=f"依赖未就绪: {', '.join(blocking)}",
            )
        started = time.monotonic()
        try:
            result = task.run(context)
        except Exception as exc:
            return TaskOutcome(
                name=task.name,
                status="failed",
                error=repr(exc),
                duration_seconds=time.monotonic() - started,
            )
        return TaskOutcome(
            name=task.name,
            status=result.status,
            detail=result.detail,
            error=result.error,
            duration_seconds=time.monotonic() - started,
        )


__all__ = [
    "DagRunner",
    "JobContext",
    "RunReport",
    "Task",
    "TaskOutcome",
    "TaskResult",
    "TaskStatus",
]
