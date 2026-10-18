"""运行状态文件（P5.1）——P5.4 状态文件契约的运行侧。

引擎只写、控制台只读，进程间零耦合：
- `<outputs_dir>/runs/<run_id>.json`：单次 DAG 运行全量状态
  （每任务 status/耗时/错误摘要 + 告警计数 + 退出码）；
- `<outputs_dir>/live_state.json`：最近运行指针（run_id + 摘要），
  控制台首页只需读这一个文件。

所有写入均为原子写（tmp + replace），避免控制台读到半截 JSON。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .dag import RunReport


@dataclass(frozen=True, slots=True)
class RunState:
    """一次 DAG 运行的完整可序列化状态。"""

    run_id: str
    run_date: str  # 业务日期 YYYY-MM-DD
    started_at: str  # ISO 秒级
    finished_at: str
    exit_code: int
    tasks: tuple[dict[str, object], ...] = field(default_factory=tuple)
    alert_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def build_run_state(
    run_id: str,
    run_date: str,
    started_at: datetime,
    finished_at: datetime,
    report: RunReport,
    alert_counts: dict[str, int],
) -> RunState:
    """从执行报告装配运行状态（纯函数）。"""
    tasks = tuple(
        {
            "name": o.name,
            "status": o.status,
            "detail": o.detail,
            "error": o.error,
            "skipped_reason": o.skipped_reason,
            "duration_seconds": round(o.duration_seconds, 3),
        }
        for o in report.outcomes
    )
    return RunState(
        run_id=run_id,
        run_date=run_date,
        started_at=started_at.isoformat(timespec="seconds"),
        finished_at=finished_at.isoformat(timespec="seconds"),
        exit_code=report.exit_code,
        tasks=tasks,
        alert_counts=alert_counts,
    )


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def write_run_state(outputs_dir: Path, state: RunState) -> Path:
    """落 runs/<run_id>.json 并更新 live_state.json 指针，返回 run 文件路径。"""
    run_file = outputs_dir / "runs" / f"{state.run_id}.json"
    _atomic_write_json(run_file, state.to_dict())

    live = {
        "last_run_id": state.run_id,
        "run_date": state.run_date,
        "finished_at": state.finished_at,
        "exit_code": state.exit_code,
        "failed_tasks": [t["name"] for t in state.tasks if t["status"] == "failed"],
        "alert_counts": state.alert_counts,
    }
    _atomic_write_json(outputs_dir / "live_state.json", live)
    return run_file


def make_run_id(run_date: str, now: datetime | None = None) -> str:
    """run_id = 业务日期_当日序号格式时间戳，如 2026-10-18_163005。"""
    ts = now or datetime.now()
    return f"{run_date}_{ts:%H%M%S}"


__all__ = ["RunState", "build_run_state", "make_run_id", "write_run_state"]
