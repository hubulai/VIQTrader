"""agent_run 审计落库（P4.1）。

审计红线（设计方案第十二节）：agent_run 必携带 config_hash + code_version，
保证历史决策可按当时参数与代码版本复盘。字段严格对齐 0002_domain_schema.sql。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import duckdb

from viqtrader import __version__
from viqtrader.agents.llm.base import LLMResponse
from viqtrader.config.loader import Config

STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"
STATUS_DEGRADED = "DEGRADED"


def new_run_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True, slots=True)
class AgentRunRecord:
    """一次 Agent 执行的审计记录（对齐 agent_run 表）。"""

    run_id: str
    agent_name: str
    started_at: datetime
    finished_at: datetime | None
    model: str
    prompt_version: str
    input_snapshot: dict[str, Any] | None
    token_in: int | None
    token_out: int | None
    cost_cny: Decimal | None
    status: str
    config_hash: str
    code_version: str


def build_run_record(
    config: Config,
    *,
    agent_name: str,
    model: str,
    prompt_version: str,
    response: LLMResponse | None = None,
    input_snapshot: dict[str, Any] | None = None,
    status: str = STATUS_FAILED,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    run_id: str | None = None,
) -> AgentRunRecord:
    """组装审计记录；有 response 时自动填充 token/cost 与完成时间。"""
    return AgentRunRecord(
        run_id=run_id or new_run_id(),
        agent_name=agent_name,
        started_at=started_at or datetime.now(),
        finished_at=finished_at or (datetime.now() if response is not None else None),
        model=model,
        prompt_version=prompt_version,
        input_snapshot=input_snapshot,
        token_in=response.token_in if response is not None else None,
        token_out=response.token_out if response is not None else None,
        cost_cny=response.cost_cny if response is not None else None,
        status=status,
        config_hash=str(config.content_hash()),
        code_version=__version__,
    )


def write_agent_run(conn: duckdb.DuckDBPyConnection, record: AgentRunRecord) -> None:
    """落 agent_run 一行。cost_cny 建表为 DOUBLE，Decimal 转 float 入库。"""
    conn.execute(
        """
        INSERT INTO agent_run
            (run_id, agent_name, started_at, finished_at, model, prompt_version,
             input_snapshot, token_in, token_out, cost_cny, status, config_hash, code_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            record.run_id,
            record.agent_name,
            record.started_at,
            record.finished_at,
            record.model,
            record.prompt_version,
            json.dumps(record.input_snapshot, ensure_ascii=False, default=str)
            if record.input_snapshot is not None
            else None,
            record.token_in,
            record.token_out,
            None if record.cost_cny is None else float(record.cost_cny),
            record.status,
            record.config_hash,
            record.code_version,
        ],
    )


__all__ = [
    "STATUS_DEGRADED",
    "STATUS_FAILED",
    "STATUS_SUCCESS",
    "AgentRunRecord",
    "build_run_record",
    "new_run_id",
    "write_agent_run",
]
