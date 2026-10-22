"""投委会编排（设计方案 4.1 ④，P4.4）：估值方 → 反方 → 会话存档。

- 估值方未通过契约校验（DEGRADED/FAILED）→ 会话中止留痕，不跑反方。
- 反方未通过 → 会话保留估值方结论，反方留痕。
- 双方结论以 output_id 并列存档于 committee_session；
  final_decision / decided_by / decided_at 留空——最终买卖决策由用户拍板。

连接纪律（P7 工程修复）：编排按「读 → LLM → 写」分段，每段各取一次 db_lease，
LLM 阶段（估值与反方各可达 600 秒）不持有任何 DuckDB 连接。否则 Windows 下写锁
会让 Web 控制台的只读连接持续 503。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import duckdb

from viqtrader.agents.llm.base import LLMProvider
from viqtrader.agents.llm.runs import STATUS_SUCCESS
from viqtrader.agents.shared.db_lease import LeaseFn
from viqtrader.agents.value.dissent_agent import (
    execute_dissent_llm,
    prepare_dissent_inputs,
    store_dissent_run,
)
from viqtrader.agents.value.valuation_agent import (
    execute_valuation_llm,
    load_consensus,
    prepare_valuation_inputs,
    store_valuation_run,
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
    lease: LeaseFn,
    config: Config,
    provider: LLMProvider,
    *,
    code: str,
    name: str,
    market: str = "SH",
    peer_codes: tuple[str, ...] | list[str] = (),
    documents: str | None = None,
) -> CommitteeSummary:
    """编排一次投委会。任一侧契约失败均不阻断审计留痕，但会话按中止/半程存档。

    `lease` 每次调用返回一个短连接上下文（真实库用 `file_lease(config)`，
    测试内存库用 `hold(conn)`）；进入读/写段才取连接，LLM 段无连接。
    """
    started_at = datetime.now()

    # S1 读：估值输入 + 研报共识（外部传 documents 时不读库）
    with lease() as conn:
        valuation_prepared = prepare_valuation_inputs(
            conn, config, code=code, name=name, market=market, peer_codes=peer_codes
        )
        external_docs = documents if documents is not None else load_consensus(conn, code)

    # S2 无连接：估值方 LLM
    valuation_run = execute_valuation_llm(provider, valuation_prepared)

    # S3 写：估值结果；失败则同租约内写中止会话后返回
    with lease() as conn:
        valuation_run_id = store_valuation_run(conn, config, valuation_run, valuation_prepared)
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

    # S4 读：反方输入（学习上下文）
    with lease() as conn:
        dissent_prepared = prepare_dissent_inputs(
            conn, config, payload, code=code, documents=external_docs
        )

    # S5 无连接：反方 LLM
    dissent_run = execute_dissent_llm(provider, dissent_prepared)

    # S6 写：反方结果 + 会话
    with lease() as conn:
        dissent_run_id = store_dissent_run(conn, config, dissent_run, dissent_prepared)
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
