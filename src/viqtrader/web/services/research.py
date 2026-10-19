"""研究材料页数据装配（P6）：投委会会话列表/详情 + 研报浏览 + 人工拍板。

会话双栏：估值方与反方结论并列（agent_output.output_json 原文展示，
JSON 结构由 P4.2 契约保证）；拍板仅写 committee_session.final_decision
存档（拍板②决策：与下单解耦，买入统一走 order_request manual 入口）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import duckdb


class DecideError(Exception):
    """拍板非法：会话不存在 / 已拍板 / 结论为空。"""


@dataclass(frozen=True, slots=True)
class SessionRow:
    session_id: str
    code: str
    name: str
    started_at: str
    has_valuation: bool
    has_dissent: bool
    final_decision: str | None
    decided_at: str | None


@dataclass(frozen=True, slots=True)
class SessionDetail:
    session_id: str
    code: str
    name: str
    started_at: str
    valuation_json: dict[str, Any] | None
    dissent_json: dict[str, Any] | None
    final_decision: str | None
    decided_by: str | None
    decided_at: str | None
    notes: str | None


@dataclass(frozen=True, slots=True)
class DocRow:
    doc_id: str
    org: str | None
    code: str | None
    industry: str | None
    doc_type: str
    publish_date: str | None
    parse_status: str
    result_json: dict[str, Any] | None  # parsed 时为解析结果（页面 <details> 展开）


def list_sessions(conn: duckdb.DuckDBPyConnection, limit: int = 100) -> list[SessionRow]:
    rows = conn.execute(
        """
        SELECT s.session_id, s.code, COALESCE(d.name, '') AS name,
               CAST(s.started_at AS TEXT),
               s.valuation_output_id IS NOT NULL,
               s.dissent_output_id IS NOT NULL,
               s.final_decision, CAST(s.decided_at AS TEXT)
        FROM committee_session s
        LEFT JOIN dim_stock d ON d.code = s.code
        ORDER BY s.started_at DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    return [
        SessionRow(
            str(r[0]),
            str(r[1]),
            str(r[2]),
            str(r[3]),
            bool(r[4]),
            bool(r[5]),
            str(r[6]) if r[6] is not None else None,
            str(r[7]) if r[7] is not None else None,
        )
        for r in rows
    ]


def _load_output(conn: duckdb.DuckDBPyConnection, output_id: Any) -> dict[str, Any] | None:
    if output_id is None:
        return None
    row = conn.execute(
        "SELECT output_json FROM agent_output WHERE output_id = ?", [output_id]
    ).fetchone()
    if row is None:
        return None
    try:
        parsed = json.loads(str(row[0]))
    except json.JSONDecodeError:
        return {"_raw": str(row[0])}
    return parsed if isinstance(parsed, dict) else {"_raw": str(row[0])}


def get_session_detail(conn: duckdb.DuckDBPyConnection, session_id: str) -> SessionDetail | None:
    row = conn.execute(
        """
        SELECT s.session_id, s.code, COALESCE(d.name, '') AS name,
               CAST(s.started_at AS TEXT), s.valuation_output_id, s.dissent_output_id,
               s.final_decision, s.decided_by, CAST(s.decided_at AS TEXT), s.notes
        FROM committee_session s
        LEFT JOIN dim_stock d ON d.code = s.code
        WHERE s.session_id = ?
        """,
        [session_id],
    ).fetchone()
    if row is None:
        return None
    return SessionDetail(
        session_id=str(row[0]),
        code=str(row[1]),
        name=str(row[2]),
        started_at=str(row[3]),
        valuation_json=_load_output(conn, row[4]),
        dissent_json=_load_output(conn, row[5]),
        final_decision=str(row[6]) if row[6] is not None else None,
        decided_by=str(row[7]) if row[7] is not None else None,
        decided_at=str(row[8]) if row[8] is not None else None,
        notes=str(row[9]) if row[9] is not None else None,
    )


def decide_session(
    conn: duckdb.DuckDBPyConnection,
    session_id: str,
    *,
    decision: str,
    notes: str | None = None,
) -> None:
    """人工拍板：写入 final_decision/decided_by/decided_at，仅允许拍一次。

    仅存档研究结论，不产生任何下单意图（拍板②：买入走 order_request manual）。
    """
    decision = decision.strip()
    if not decision:
        raise DecideError("拍板结论不能为空")
    notes_clean = notes.strip() if notes else ""
    row = conn.execute(
        "SELECT final_decision FROM committee_session WHERE session_id = ?",
        [session_id],
    ).fetchone()
    if row is None:
        raise DecideError(f"投委会会话不存在：{session_id}")
    if row[0] is not None:
        raise DecideError(f"该会话已拍板（{row[0]}），不可重复拍板")
    conn.execute(
        """
        UPDATE committee_session
        SET final_decision = ?, decided_by = 'user', decided_at = now(),
            notes = CASE WHEN ? <> '' THEN ? ELSE notes END
        WHERE session_id = ?
        """,
        [decision, notes_clean, notes_clean if notes_clean else None, session_id],
    )


def list_documents(conn: duckdb.DuckDBPyConnection, limit: int = 100) -> list[DocRow]:
    rows = conn.execute(
        """
        SELECT d.doc_id, d.org, d.code, d.industry, d.doc_type,
               CAST(d.publish_date AS TEXT), d.parse_status, a.result_json
        FROM document d
        LEFT JOIN document_analysis a ON a.doc_id = d.doc_id
        ORDER BY d.created_at DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    out: list[DocRow] = []
    for r in rows:
        result: dict[str, Any] | None = None
        if r[7] is not None:
            try:
                parsed = json.loads(str(r[7]))
                result = parsed if isinstance(parsed, dict) else {"_raw": str(r[7])}
            except json.JSONDecodeError:
                result = {"_raw": str(r[7])}
        out.append(
            DocRow(
                str(r[0]),
                str(r[1]) if r[1] is not None else None,
                str(r[2]) if r[2] is not None else None,
                str(r[3]) if r[3] is not None else None,
                str(r[4]),
                str(r[5]) if r[5] is not None else None,
                str(r[6]),
                result,
            )
        )
    return out
