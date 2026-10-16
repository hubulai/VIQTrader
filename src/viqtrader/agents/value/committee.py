"""投委会编排（设计方案 4.1 ④，P4.4）：估值方 → 反方 → 会话存档。

- 估值方未通过契约校验（DEGRADED/FAILED）→ 会话中止留痕，不跑反方。
- 反方未通过 → 会话保留估值方结论，反方留痕。
- 双方结论以 output_id 并列存档于 committee_session；
  final_decision / decided_by / decided_at 留空——最终买卖决策由用户拍板。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import duckdb

from viqtrader.agents.llm.base import LLMProvider
from viqtrader.agents.llm.runs import STATUS_SUCCESS
from viqtrader.agents.value.dissent_agent import run_dissent_agent
from viqtrader.agents.value.valuation_agent import (
    load_consensus,
    run_valuation_agent,
)
from viqtrader.config.loader import Config


@dataclass(frozen=True, slots=True)
class CommitteeSummary:
    """一次投委会会话的结果摘要。"""

    session_id: str
    code: str
    valuation_status: str
    dissent_status: str | None
    valuation_output_id: str | None
    dissent_output_id: str | None
    aborted: bool
    notes: str


def get_output_id(conn: duckdb.DuckDBPyConnection, run_id: str) -> str | None:
    row = conn.execute(
        "SELECT output_id FROM agent_output WHERE run_id = ? LIMIT 1", [run_id]
    ).fetchone()
    return None if row is None else str(row[0])


def _write_session(
    conn: duckdb.DuckDBPyConnection,
    code: str,
    started_at: datetime,
    *,
    valuation_output_id: str | None,
    dissent_output_id: str | None,
    notes: str,
) -> str:
    session_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO committee_session
            (session_id, code, started_at, valuation_output_id, dissent_output_id, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [session_id, code, started_at, valuation_output_id, dissent_output_id, notes],
    )
    return session_id


def run_committee(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    provider: LLMProvider,
    *,
    code: str,
    name: str,
    market: str = "SH",
    peer_codes: tuple[str, ...] | list[str] = (),
    documents: str | None = None,
) -> CommitteeSummary:
    """编排一次投委会。任一侧契约失败均不阻断审计留痕，但会话按中止/半程存档。"""
    started_at = datetime.now()

    valuation_run_id, valuation_run = run_valuation_agent(
        conn, config, provider, code=code, name=name, market=market, peer_codes=peer_codes
    )
    if valuation_run.status != STATUS_SUCCESS:
        errors = "；".join(valuation_run.validation.errors)
        session_id = _write_session(
            conn,
            code,
            started_at,
            valuation_output_id=None,
            dissent_output_id=None,
            notes=f"估值方未通过契约校验（{valuation_run.status}），会话中止。错误：{errors}",
        )
        return CommitteeSummary(
            session_id,
            code,
            valuation_run.status,
            None,
            None,
            None,
            True,
            f"估值方 {valuation_run.status}，会话中止",
        )

    valuation_output_id = get_output_id(conn, valuation_run_id)
    payload: dict[str, Any] = dict(valuation_run.validation.payload or {})

    dissent_run_id, dissent_run = run_dissent_agent(
        conn,
        config,
        provider,
        payload,
        code=code,
        documents=documents if documents is not None else load_consensus(conn, code),
    )
    if dissent_run.status != STATUS_SUCCESS:
        errors = "；".join(dissent_run.validation.errors)
        session_id = _write_session(
            conn,
            code,
            started_at,
            valuation_output_id=valuation_output_id,
            dissent_output_id=None,
            notes=f"反方未通过契约校验（{dissent_run.status}），估值方结论已存档。错误：{errors}",
        )
        return CommitteeSummary(
            session_id,
            code,
            valuation_run.status,
            dissent_run.status,
            valuation_output_id,
            None,
            True,
            f"反方 {dissent_run.status}，半程存档",
        )

    dissent_output_id = get_output_id(conn, dissent_run_id)
    parsed = valuation_run.validation.parsed
    dissent = dissent_run.validation.parsed
    notes = "投委会材料齐备，待人工拍板。"
    if parsed is not None and dissent is not None:
        notes = (
            f"估值置信度 {parsed.confidence:.0%}，安全边际 {parsed.margin_of_safety:.0%}；"
            f"反方攻击 {len(dissent.rebuttals)} 条（致命问题 {len(dissent.fatal_issues)} 条）。"
            "待人工拍板。"
        )
    session_id = _write_session(
        conn,
        code,
        started_at,
        valuation_output_id=valuation_output_id,
        dissent_output_id=dissent_output_id,
        notes=notes,
    )
    return CommitteeSummary(
        session_id,
        code,
        valuation_run.status,
        dissent_run.status,
        valuation_output_id,
        dissent_output_id,
        False,
        notes,
    )
