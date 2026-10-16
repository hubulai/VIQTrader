"""契约运行结果落库：写 agent_run；仅 SUCCESS 时写 agent_output。

设计与审计约定：契约不合格（DEGRADED / FAILED）**拒绝入库** agent_output，
但 agent_run 仍留痕（供复盘追溯失败原因）。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import duckdb

from viqtrader.agents.contracts.base import ContractRun
from viqtrader.agents.llm.runs import STATUS_SUCCESS, build_run_record, write_agent_run
from viqtrader.config.loader import Config


def persist_contract_run(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    run: ContractRun,
    *,
    agent_name: str,
    prompt_version: str,
    input_snapshot: dict[str, Any] | None = None,
) -> str:
    """落 agent_run 一行；SUCCESS 时追加 agent_output。返回 run_id。"""
    response = run.response
    record = build_run_record(
        config,
        agent_name=agent_name,
        model=response.model if response is not None else "unknown",
        prompt_version=prompt_version,
        response=response,
        input_snapshot=input_snapshot,
        status=run.status,
    )
    write_agent_run(conn, record)

    if run.status == STATUS_SUCCESS and run.validation.payload is not None:
        payload = run.validation.payload
        refs = payload.get("data_refs")
        conn.execute(
            """
            INSERT INTO agent_output (output_id, run_id, output_json, data_refs)
            VALUES (?, ?, ?, ?)
            """,
            [
                str(uuid.uuid4()),
                record.run_id,
                json.dumps(payload, ensure_ascii=False, default=str),
                json.dumps(refs, ensure_ascii=False) if refs is not None else None,
            ],
        )
    return record.run_id


__all__ = ["persist_contract_run"]
