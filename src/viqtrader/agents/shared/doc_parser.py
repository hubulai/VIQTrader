"""研报解析 Agent（P4.8）：document(pending) → LLM 抽取 → document_analysis + analyst_forecast。

流程：取 document 行 → PdfExtractor 重提全文（与 P1.8 入库同一缝隙，全文不落库只留 hash）
→ 头+尾截断控制 token → 渲染 doc_parse.v1 prompt → run_contract 契约校验
→ SUCCESS：写 document_analysis（doc_id UNIQUE，一份只解析一次）+ 拆 analyst_forecast 行
  + parse_status='parsed'；DEGRADED/FAILED：parse_status='failed'，仅留 agent_run 痕。

幂等红线：document_analysis 已有 doc_id 直接 skipped；DB 层 UNIQUE(doc_id) 兜底。
解析结果均为**外部观点**（卖方系统性乐观），下游估值方引用时与自身数据严格区分。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

import duckdb

from viqtrader.agents.context import build_learning_context
from viqtrader.agents.contracts.base import ContractRun, run_contract
from viqtrader.agents.contracts.doc_analysis import DocAnalysisContract, DocAnalysisOutput
from viqtrader.agents.contracts.persist import persist_contract_run
from viqtrader.agents.llm.base import LLMProvider, Message
from viqtrader.agents.llm.runs import STATUS_SUCCESS
from viqtrader.agents.prompts.registry import PromptRegistry
from viqtrader.config.loader import Config
from viqtrader.data.docs.ingestor import PdfExtractor, default_pdf_extractor

__all__ = ["DocParseResult", "parse_document", "parse_pending", "truncate_text"]

_OMIT_MARK = "\n\n[正文中部省略]\n\n"


@dataclass(frozen=True, slots=True)
class DocParseResult:
    """单份研报的解析结果（parsed / skipped / failed）。"""

    doc_id: str
    status: str
    analysis_id: str | None = None
    run_id: str | None = None
    error: str | None = None


def truncate_text(text: str, *, head_chars: int, tail_chars: int) -> str:
    """头+尾截断：封面（核心观点/盈利预测/评级）与末尾（风险点）保留，中部省略。"""
    if len(text) <= head_chars + tail_chars:
        return text
    return text[:head_chars] + _OMIT_MARK + text[len(text) - tail_chars :]


@dataclass(frozen=True, slots=True)
class _DocRow:
    doc_id: str
    file_path: str
    doc_type: str
    code: str | None
    industry: str | None
    org: str | None
    publish_date: date | None


def _load_doc(conn: duckdb.DuckDBPyConnection, doc_id: str) -> _DocRow | None:
    row = conn.execute(
        """
        SELECT doc_id, file_path, doc_type, code, industry, org, publish_date
        FROM document WHERE doc_id = ?
        """,
        [doc_id],
    ).fetchone()
    if row is None:
        return None
    return _DocRow(
        doc_id=str(row[0]),
        file_path=str(row[1]),
        doc_type=str(row[2]),
        code=row[3],
        industry=row[4],
        org=row[5],
        publish_date=row[6],
    )


def _already_parsed(conn: duckdb.DuckDBPyConnection, doc_id: str) -> str | None:
    row = conn.execute(
        "SELECT analysis_id FROM document_analysis WHERE doc_id = ?", [doc_id]
    ).fetchone()
    return None if row is None else str(row[0])


def _set_status(conn: duckdb.DuckDBPyConnection, doc_id: str, status: str) -> None:
    conn.execute("UPDATE document SET parse_status = ? WHERE doc_id = ?", [status, doc_id])


def _write_analysis(
    conn: duckdb.DuckDBPyConnection,
    doc: _DocRow,
    run: ContractRun,
    *,
    model: str,
    prompt_version: str,
) -> str:
    """落 document_analysis + 拆 analyst_forecast 行；返回 analysis_id。"""
    analysis_id = str(uuid.uuid4())
    payload = run.validation.payload or {}
    conn.execute(
        """
        INSERT INTO document_analysis (analysis_id, doc_id, result_json, model, prompt_version)
        VALUES (?, ?, ?, ?, ?)
        """,
        [analysis_id, doc.doc_id, json.dumps(payload, ensure_ascii=False), model, prompt_version],
    )
    parsed = run.validation.parsed
    if isinstance(parsed, DocAnalysisOutput) and doc.code:
        for item in parsed.forecasts:
            if item.net_profit is None and item.eps is None and item.target_price is None:
                continue  # 全空行无信息，不写
            conn.execute(
                """
                INSERT INTO analyst_forecast
                    (forecast_id, doc_id, org, forecast_date, code, fiscal_year,
                     forecast_net_profit, forecast_eps, target_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    str(uuid.uuid4()),
                    doc.doc_id,
                    doc.org,
                    doc.publish_date,
                    doc.code,
                    item.fiscal_year,
                    item.net_profit,
                    item.eps,
                    item.target_price,
                ],
            )
    return analysis_id


def parse_document(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    provider: LLMProvider,
    *,
    doc_id: str,
    extractor: PdfExtractor | None = None,
) -> DocParseResult:
    """解析单份研报。任何失败收敛为 DocParseResult，不向批量入口抛异常。"""
    doc = _load_doc(conn, doc_id)
    if doc is None:
        return DocParseResult(doc_id=doc_id, status="failed", error="document 不存在")

    existing = _already_parsed(conn, doc_id)
    if existing is not None:
        return DocParseResult(doc_id=doc_id, status="skipped", analysis_id=existing)

    ext = extractor if extractor is not None else default_pdf_extractor()
    try:
        text = ext.extract_text(doc.file_path)
    except Exception as exc:
        _set_status(conn, doc_id, "failed")
        return DocParseResult(doc_id=doc_id, status="failed", error=f"PDF 提取失败: {exc}")
    if not text.strip():
        _set_status(conn, doc_id, "failed")
        return DocParseResult(doc_id=doc_id, status="failed", error="PDF 无文本内容")

    head_chars = int(config.get("documents.parse_head_chars", 12000))
    tail_chars = int(config.get("documents.parse_tail_chars", 4000))
    truncated = truncate_text(text, head_chars=head_chars, tail_chars=tail_chars)

    learning_context = build_learning_context(
        conn, config, agent_name="doc_parse", decision_type="doc_parse", code=doc.code
    )
    registry = PromptRegistry()
    prompt = registry.render(
        "doc_parse",
        org=doc.org or "未知",
        code=doc.code or "未知",
        industry=doc.industry or "未知",
        publish_date=str(doc.publish_date) if doc.publish_date else "未知",
        doc_type=doc.doc_type,
        full_text=truncated,
        learning_context=learning_context,
        schema_hint=DocAnalysisContract.schema_hint,
    )
    model = str(config.get("llm.model_cheap"))
    run = run_contract(provider, DocAnalysisContract(), [Message("user", prompt)], model)
    run_id = persist_contract_run(
        conn,
        config,
        run,
        agent_name="doc_parse",
        prompt_version=registry.version("doc_parse"),
        input_snapshot={
            "doc_id": doc_id,
            "content_chars": len(text),
            "truncated_chars": len(truncated),
            "learning_context": learning_context,
        },
    )
    if run.status != STATUS_SUCCESS:
        _set_status(conn, doc_id, "failed")
        errors = "；".join(run.validation.errors)
        return DocParseResult(
            doc_id=doc_id, status="failed", run_id=run_id, error=f"契约校验 {run.status}: {errors}"
        )

    analysis_id = _write_analysis(
        conn, doc, run, model=model, prompt_version=registry.version("doc_parse")
    )
    _set_status(conn, doc_id, "parsed")
    return DocParseResult(doc_id=doc_id, status="parsed", analysis_id=analysis_id, run_id=run_id)


def parse_pending(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    provider: LLMProvider,
    *,
    limit: int | None = None,
    extractor: PdfExtractor | None = None,
) -> list[DocParseResult]:
    """批量扫描 parse_status='pending' 的研报逐份解析，单份成败互不影响。"""
    sql = (
        "SELECT doc_id FROM document WHERE parse_status = 'pending' " "ORDER BY created_at, doc_id"
    )
    params: list[Any] = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    doc_ids = [str(r[0]) for r in conn.execute(sql, params).fetchall()]
    return [
        parse_document(conn, config, provider, doc_id=doc_id, extractor=extractor)
        for doc_id in doc_ids
    ]
