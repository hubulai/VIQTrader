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


_MOAT_LABELS = {"none": "无", "narrow": "窄", "strong": "强", "wide": "宽"}
_SEVERITY_LABELS = {"low": "低", "medium": "中", "high": "高"}
_SEVERITY_CLASSES = {"low": "", "medium": "warn", "high": "bad"}
_MOAT_CLASSES = {"none": "bad", "narrow": "warn", "strong": "ok", "wide": "ok"}

# 估值方/反方契约的识别键（contracts/valuation.py、dissent.py，web 禁导 agents 故硬编码）
_VALUATION_KEYS = {
    "intrinsic_value_range",
    "current_price",
    "margin_of_safety",
    "methods",
    "moat_rating",
    "variant_perception",
}
_DISSENT_KEYS = {"rebuttals", "fatal_issues", "verdict", "needs_data"}


def _fmt_num(value: Any) -> str:
    return f"{value:,.2f}" if isinstance(value, int | float) else "—"


def _fmt_ratio_pct(value: Any) -> str:
    return f"{value * 100:.1f}%" if isinstance(value, int | float) else "—"


def _fmt_assumption(value: Any) -> str:
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _str_items(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


@dataclass(frozen=True, slots=True)
class MethodView:
    name: str
    value_text: str
    assumptions: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ValuationView:
    """估值方结论的人读视图（原始 JSON 仍随 valuation_json 保留供折叠核对）。"""

    current_price_text: str
    range_text: str
    margin_text: str
    confidence_text: str
    moat_label: str
    moat_class: str
    methods: tuple[MethodView, ...]
    moat_evidence: tuple[str, ...]
    key_risks: tuple[str, ...]
    variant_perception: str
    data_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RebuttalView:
    target: str
    attack: str
    severity_label: str
    severity_class: str


@dataclass(frozen=True, slots=True)
class DissentView:
    """反方校验的人读视图。"""

    verdict: str
    fatal_issues: tuple[str, ...]
    rebuttals: tuple[RebuttalView, ...]
    needs_data: tuple[str, ...]


def build_valuation_view(payload: dict[str, Any] | None) -> ValuationView | None:
    """把估值方 output_json 整成人读视图；不符合契约结构时返回 None（页面回退原文）。"""
    if not payload or "_raw" in payload or not (_VALUATION_KEYS & payload.keys()):
        return None
    rng = payload.get("intrinsic_value_range")
    if isinstance(rng, list) and len(rng) == 2:
        range_text = f"{_fmt_num(rng[0])} ~ {_fmt_num(rng[1])}"
    else:
        range_text = "—"
    methods: list[MethodView] = []
    raw_methods = payload.get("methods")
    if isinstance(raw_methods, list):
        for item in raw_methods:
            if not isinstance(item, dict):
                continue
            assumptions = item.get("assumptions")
            kv = (
                tuple((str(k), _fmt_assumption(v)) for k, v in assumptions.items())
                if isinstance(assumptions, dict)
                else ()
            )
            methods.append(MethodView(str(item.get("name", "")), _fmt_num(item.get("value")), kv))
    moat = str(payload.get("moat_rating", ""))
    return ValuationView(
        current_price_text=_fmt_num(payload.get("current_price")),
        range_text=range_text,
        margin_text=_fmt_ratio_pct(payload.get("margin_of_safety")),
        confidence_text=_fmt_ratio_pct(payload.get("confidence")),
        moat_label=_MOAT_LABELS.get(moat, moat or "—"),
        moat_class=_MOAT_CLASSES.get(moat, ""),
        methods=tuple(methods),
        moat_evidence=_str_items(payload.get("moat_evidence")),
        key_risks=_str_items(payload.get("key_risks")),
        variant_perception=str(payload.get("variant_perception", "")),
        data_refs=_str_items(payload.get("data_refs")),
    )


def build_dissent_view(payload: dict[str, Any] | None) -> DissentView | None:
    """把反方 output_json 整成人读视图；不符合契约结构时返回 None（页面回退原文）。"""
    if not payload or "_raw" in payload or not (_DISSENT_KEYS & payload.keys()):
        return None
    rebuttals: list[RebuttalView] = []
    raw_rebuttals = payload.get("rebuttals")
    if isinstance(raw_rebuttals, list):
        for item in raw_rebuttals:
            if not isinstance(item, dict):
                continue
            severity = str(item.get("severity", ""))
            rebuttals.append(
                RebuttalView(
                    target=str(item.get("target", "")),
                    attack=str(item.get("attack", "")),
                    severity_label=_SEVERITY_LABELS.get(severity, severity or "—"),
                    severity_class=_SEVERITY_CLASSES.get(severity, ""),
                )
            )
    return DissentView(
        verdict=str(payload.get("verdict", "")),
        fatal_issues=_str_items(payload.get("fatal_issues")),
        rebuttals=tuple(rebuttals),
        needs_data=_str_items(payload.get("needs_data")),
    )


# 研报解析契约的识别键（contracts/doc_analysis.py，web 禁导 agents 故硬编码）
_DOC_ANALYSIS_KEYS = {
    "summary",
    "core_assumptions",
    "forecasts",
    "rating",
    "target_price",
    "risks",
}


@dataclass(frozen=True, slots=True)
class ForecastView:
    fiscal_year: str
    net_profit_text: str
    eps_text: str
    target_price_text: str


@dataclass(frozen=True, slots=True)
class DocAnalysisView:
    """研报解析结果的人读视图（外部观点，与平台自身数据严格区分）。"""

    summary: str
    rating_text: str
    target_price_text: str
    forecasts: tuple[ForecastView, ...]
    core_assumptions: tuple[str, ...]
    risks: tuple[str, ...]


def build_doc_analysis_view(payload: dict[str, Any] | None) -> DocAnalysisView | None:
    """把研报解析 result_json 整成人读视图；不符合契约结构时返回 None（页面回退原文）。"""
    if not payload or "_raw" in payload or not (_DOC_ANALYSIS_KEYS & payload.keys()):
        return None
    forecasts: list[ForecastView] = []
    raw_forecasts = payload.get("forecasts")
    if isinstance(raw_forecasts, list):
        for item in raw_forecasts:
            if not isinstance(item, dict):
                continue
            forecasts.append(
                ForecastView(
                    fiscal_year=str(item.get("fiscal_year", "")),
                    net_profit_text=_fmt_num(item.get("net_profit")),
                    eps_text=_fmt_num(item.get("eps")),
                    target_price_text=_fmt_num(item.get("target_price")),
                )
            )
    rating = payload.get("rating")
    return DocAnalysisView(
        summary=str(payload.get("summary", "")),
        rating_text=str(rating) if rating is not None else "—",
        target_price_text=_fmt_num(payload.get("target_price")),
        forecasts=tuple(forecasts),
        core_assumptions=_str_items(payload.get("core_assumptions")),
        risks=_str_items(payload.get("risks")),
    )


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
    valuation_view: ValuationView | None  # 人读视图；None 时页面回退展示原文
    dissent_view: DissentView | None
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
    analysis_view: DocAnalysisView | None  # 人读视图；None 时页面回退展示原文


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
    valuation_json = _load_output(conn, row[4])
    dissent_json = _load_output(conn, row[5])
    return SessionDetail(
        session_id=str(row[0]),
        code=str(row[1]),
        name=str(row[2]),
        started_at=str(row[3]),
        valuation_json=valuation_json,
        dissent_json=dissent_json,
        valuation_view=build_valuation_view(valuation_json),
        dissent_view=build_dissent_view(dissent_json),
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
                build_doc_analysis_view(result),
            )
        )
    return out
