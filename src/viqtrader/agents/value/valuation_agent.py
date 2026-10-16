"""深度估值 Agent（设计方案 4.1 ②，P4.4）。

从 DuckDB 装配投研输入 → PromptRegistry 渲染版本化 prompt →
run_contract 强类型校验（不合格带错误提示重试一次）→ persist_contract_run 落库。
研报共识仅作外部观点输入，与自身结论严格区分（来源标注机构与日期）。
"""

from __future__ import annotations

import json
from typing import Any

import duckdb

from viqtrader.agents.context import build_learning_context
from viqtrader.agents.contracts.base import ContractRun, run_contract
from viqtrader.agents.contracts.persist import persist_contract_run
from viqtrader.agents.contracts.valuation import ValuationContract
from viqtrader.agents.llm.base import LLMProvider, Message
from viqtrader.agents.prompts.registry import PromptRegistry
from viqtrader.config.loader import Config

_FINANCIAL_PERIODS = 8  # 近 8 期财报
_DIVIDEND_ROWS = 5
_DISCLOSURE_ROWS = 5
_CONSENSUS_DOCS = 3
_CONSENSUS_CHARS = 800  # 单份研报分析截断长度，控制 prompt 体量

FinancialRow = dict[str, Any]


def _trunc(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def load_valuation_inputs(
    conn: duckdb.DuckDBPyConnection,
    code: str,
    *,
    name: str,
    market: str = "SH",
    peer_codes: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """装配估值 Agent 的输入快照（同时作为 agent_run.input_snapshot 审计留痕）。"""
    financials: list[FinancialRow] = []
    for row in conn.execute(
        """
        SELECT report_period, ann_date, revenue, net_profit, roe, gross_margin,
               ocf, fcf, eps, bps, debt_ratio
        FROM fact_financial WHERE code = ?
        ORDER BY report_period DESC LIMIT ?
        """,
        [code, _FINANCIAL_PERIODS],
    ).fetchall():
        financials.append(
            {
                "report_period": str(row[0]),
                "ann_date": str(row[1]) if row[1] is not None else None,
                "revenue": row[2],
                "net_profit": row[3],
                "roe": row[4],
                "gross_margin": row[5],
                "ocf": row[6],
                "fcf": row[7],
                "eps": row[8],
                "bps": row[9],
                "debt_ratio": row[10],
            }
        )

    val_row = conn.execute(
        """
        SELECT trade_date, pe_ttm, pb, ps_ttm, dv_ratio, market_cap, pe_percentile
        FROM fact_valuation WHERE code = ? ORDER BY trade_date DESC LIMIT 1
        """,
        [code],
    ).fetchone()
    valuation: dict[str, Any] | str = "无估值数据"
    if val_row is not None:
        valuation = {
            "trade_date": str(val_row[0]),
            "pe_ttm": val_row[1],
            "pb": val_row[2],
            "ps_ttm": val_row[3],
            "dv_ratio": val_row[4],
            "market_cap": val_row[5],
            "pe_percentile": val_row[6],
        }

    dividends = [
        {"ex_date": str(r[0]), "dividend_per_share": r[1], "dividend_type": r[2]}
        for r in conn.execute(
            """
            SELECT ex_date, dividend_per_share, dividend_type
            FROM fact_dividend WHERE code = ?
            ORDER BY ex_date DESC LIMIT ?
            """,
            [code, _DIVIDEND_ROWS],
        ).fetchall()
    ]

    disclosures = [
        {"ann_date": str(r[0]), "title": r[1], "category": r[2], "summary": r[3]}
        for r in conn.execute(
            """
            SELECT ann_date, title, category, summary
            FROM fact_disclosure WHERE code = ?
            ORDER BY ann_date DESC LIMIT ?
            """,
            [code, _DISCLOSURE_ROWS],
        ).fetchall()
    ]

    peers: list[dict[str, Any]] = []
    for peer in peer_codes:
        prow = conn.execute(
            """
            SELECT pe_ttm, pb, dv_ratio FROM fact_valuation
            WHERE code = ? ORDER BY trade_date DESC LIMIT 1
            """,
            [peer],
        ).fetchone()
        if prow is not None:
            peers.append({"code": peer, "pe_ttm": prow[0], "pb": prow[1], "dv_ratio": prow[2]})

    return {
        "code": code,
        "market": market,
        "name": name,
        "financials": financials or "无财务数据",
        "valuation": valuation,
        "dividends": dividends or "无分红记录",
        "disclosures": disclosures or "无公告摘要",
        "peers": peers or "无同业对比数据",
        "consensus": load_consensus(conn, code),
    }


def load_consensus(
    conn: duckdb.DuckDBPyConnection,
    code: str,
    *,
    limit: int = _CONSENSUS_DOCS,
    chars: int = _CONSENSUS_CHARS,
) -> str:
    """研报共识：document + document_analysis 中该标的最近几份已解析研报的分析结果。"""
    rows = conn.execute(
        """
        SELECT d.org, d.publish_date, a.result_json
        FROM document d
        JOIN document_analysis a ON a.doc_id = d.doc_id
        WHERE d.code = ?
        ORDER BY d.publish_date DESC NULLS LAST
        LIMIT ?
        """,
        [code, limit],
    ).fetchall()
    if not rows:
        return "无外部研报输入"
    items = [
        {
            "org": r[0],
            "publish_date": str(r[1]) if r[1] is not None else None,
            "analysis": _trunc(str(r[2]), chars),  # 含盈利预测/核心假设/评级目标价
        }
        for r in rows
    ]
    return json.dumps(items, ensure_ascii=False)


def run_valuation_agent(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    provider: LLMProvider,
    *,
    code: str,
    name: str,
    market: str = "SH",
    peer_codes: tuple[str, ...] | list[str] = (),
) -> tuple[str, ContractRun]:
    """执行估值 Agent，返回 (run_id, 契约运行结果)。成功时 parsed 为 ValuationOutput。"""
    registry = PromptRegistry()
    bundle = load_valuation_inputs(conn, code, name=name, market=market, peer_codes=peer_codes)
    learning_context = build_learning_context(
        conn,
        config,
        agent_name="valuation",
        decision_type="valuation",
        code=code,
        market=market,
    )
    prompt = registry.render(
        "valuation",
        code=code,
        market=market,
        name=name,
        financials=json.dumps(bundle["financials"], ensure_ascii=False),
        valuation=json.dumps(bundle["valuation"], ensure_ascii=False),
        dividends=json.dumps(bundle["dividends"], ensure_ascii=False),
        disclosures=json.dumps(bundle["disclosures"], ensure_ascii=False),
        peers=json.dumps(bundle["peers"], ensure_ascii=False),
        consensus=str(bundle["consensus"]),
        learning_context=learning_context,
        schema_hint=ValuationContract.schema_hint,
    )
    bundle["learning_context"] = learning_context  # 注入内容随快照落库，可还原
    model = str(config.get("llm.model_deep"))
    run = run_contract(provider, ValuationContract(), [Message("user", prompt)], model)
    run_id = persist_contract_run(
        conn,
        config,
        run,
        agent_name="valuation",
        prompt_version=registry.version("valuation"),
        input_snapshot=bundle,
    )
    return run_id, run
