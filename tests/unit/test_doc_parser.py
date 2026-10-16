"""P4.8 研报解析 Agent 测试：截断纯函数 / 契约校验 / 端到端落库 / 幂等 / 批量隔离。

全离线：LLM 一律 FakeProvider，PDF 提取注入假 extractor，数据库一律内存库 + 全迁移。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from viqtrader.agents.contracts.doc_analysis import DocAnalysisContract
from viqtrader.agents.llm.base import Message
from viqtrader.agents.llm.fake import FakeProvider
from viqtrader.agents.shared.doc_parser import (
    parse_document,
    parse_pending,
    truncate_text,
)
from viqtrader.config.loader import Config

_VALID_ANALYSIS: dict[str, Any] = {
    "summary": "公司三季报超预期，上调盈利预测，维持买入评级。",
    "core_assumptions": ["2026 年销量增长 5%", "毛利率维持 92%"],
    "forecasts": [
        {"fiscal_year": "2025E", "net_profit": 870.5, "eps": 69.3, "target_price": None},
        {"fiscal_year": "2026E", "net_profit": 950.0, "eps": 75.6, "target_price": 1800.0},
    ],
    "rating": "买入",
    "target_price": 1800.0,
    "risks": ["批价下行风险", "消费复苏不及预期"],
}


class _FakeExtractor:
    def __init__(self, text: str | Exception) -> None:
        self._text = text

    def extract_text(self, file_path: str) -> str:
        if isinstance(self._text, Exception):
            raise self._text
        return self._text


def _seed_doc(
    conn,
    doc_id: str = "doc1",
    *,
    code: str | None = "600519",
    parse_status: str = "pending",
) -> None:
    conn.execute(
        """
        INSERT INTO document
            (doc_id, file_path, content_hash, doc_type, code, industry,
             org, publish_date, parse_status)
        VALUES (?, 'x.pdf', ?, '个股研报', ?, '白酒', '某券商', '2026-04-10', ?)
        """,
        [doc_id, f"hash_{doc_id}", code, parse_status],
    )


def _fixed_provider(payload: dict[str, Any]) -> FakeProvider:
    def responder(messages: Sequence[Message], model: str, params: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False)

    return FakeProvider(responder)


# ---------- truncate_text 纯函数 ----------


def test_truncate_short_text_untouched() -> None:
    text = "x" * 100
    assert truncate_text(text, head_chars=60, tail_chars=40) == text


def test_truncate_long_text_head_tail() -> None:
    text = "H" * 100 + "M" * 100 + "T" * 100
    out = truncate_text(text, head_chars=50, tail_chars=30)
    assert out.startswith("H" * 50)
    assert out.endswith("T" * 30)
    assert "[正文中部省略]" in out
    assert "M" * 50 not in out.replace("[正文中部省略]", "")


def test_truncate_boundary_exact_fit() -> None:
    text = "y" * 100
    assert truncate_text(text, head_chars=50, tail_chars=50) == text


# ---------- DocAnalysisContract ----------


def test_contract_valid_payload() -> None:
    result = DocAnalysisContract().validate(_VALID_ANALYSIS)
    assert result.ok
    parsed = result.parsed
    assert parsed is not None
    assert parsed.rating == "买入"
    assert parsed.forecasts[0].target_price is None  # 可空字段容忍
    assert parsed.forecasts[1].fiscal_year == "2026E"


def test_contract_nullables_absent() -> None:
    payload = {
        "summary": "摘要",
        "core_assumptions": [],
        "forecasts": [],
        "rating": None,
        "target_price": None,
        "risks": [],
    }
    assert DocAnalysisContract().validate(payload).ok


def test_contract_rejects_malformed() -> None:
    bad = {**_VALID_ANALYSIS, "summary": 123}  # 类型错误
    result = DocAnalysisContract().validate(bad)
    assert not result.ok
    missing_fy = {
        **_VALID_ANALYSIS,
        "forecasts": [{"net_profit": 1.0}],  # 缺 fiscal_year
    }
    result2 = DocAnalysisContract().validate(missing_fy)
    assert not result2.ok
    assert any("fiscal_year" in e for e in result2.errors)


# ---------- parse_document 端到端 ----------


def test_parse_document_success_full_chain(conn, config: Config) -> None:
    _seed_doc(conn)
    provider = _fixed_provider(_VALID_ANALYSIS)
    result = parse_document(
        conn, config, provider, doc_id="doc1", extractor=_FakeExtractor("研报全文" * 100)
    )

    assert result.status == "parsed"
    assert result.analysis_id is not None
    row = conn.execute("SELECT parse_status FROM document WHERE doc_id = 'doc1'").fetchone()
    assert row is not None and row[0] == "parsed"

    analysis = conn.execute(
        "SELECT result_json, model, prompt_version FROM document_analysis WHERE doc_id = 'doc1'"
    ).fetchone()
    assert analysis is not None
    payload = json.loads(analysis[0])
    assert payload["rating"] == "买入"
    assert analysis[1] == str(config.get("llm.model_cheap"))  # 轻任务用便宜模型
    assert analysis[2] == "doc_parse.v1"

    forecasts = conn.execute(
        """
        SELECT fiscal_year, forecast_net_profit, forecast_eps, target_price, org
        FROM analyst_forecast WHERE doc_id = 'doc1' ORDER BY fiscal_year
        """
    ).fetchall()
    assert len(forecasts) == 2
    assert forecasts[0] == ("2025E", 870.5, 69.3, None, "某券商")
    assert forecasts[1][0] == "2026E" and forecasts[1][3] == 1800.0

    audit = conn.execute(
        "SELECT agent_name, input_snapshot FROM agent_run WHERE run_id = ?",
        [result.run_id],
    ).fetchone()
    assert audit is not None and audit[0] == "doc_parse"
    snapshot = json.loads(audit[1])
    assert snapshot["doc_id"] == "doc1"
    assert "learning_context" in snapshot  # 注入内容随快照可还原


def test_parse_document_prompt_carries_metadata_and_text(conn, config: Config) -> None:
    _seed_doc(conn)
    provider = _fixed_provider(_VALID_ANALYSIS)
    parse_document(conn, config, provider, doc_id="doc1", extractor=_FakeExtractor("核心观点文本"))

    prompt = provider.calls[0][0][-1].content
    assert "某券商" in prompt and "600519" in prompt  # 元数据供对照
    assert "核心观点文本" in prompt  # 全文进 prompt
    assert "暂无历史案例与评分卡记录" in prompt  # 空库学习上下文降级注入
    assert "外部观点" in prompt  # 治理红线标注


def test_parse_document_skip_when_already_parsed(conn, config: Config) -> None:
    _seed_doc(conn)
    provider = _fixed_provider(_VALID_ANALYSIS)
    first = parse_document(conn, config, provider, doc_id="doc1", extractor=_FakeExtractor("文本"))
    second = parse_document(conn, config, provider, doc_id="doc1", extractor=_FakeExtractor("文本"))

    assert first.status == "parsed"
    assert second.status == "skipped"
    assert second.analysis_id == first.analysis_id
    assert len(provider.calls) == 1  # 第二次零 LLM 调用
    n = conn.execute("SELECT COUNT(*) FROM document_analysis WHERE doc_id = 'doc1'").fetchone()
    assert n is not None and n[0] == 1  # 一份只解析一次


def test_parse_document_unknown_doc_fails(conn, config: Config) -> None:
    result = parse_document(
        conn, config, FakeProvider(), doc_id="ghost", extractor=_FakeExtractor("文本")
    )
    assert result.status == "failed" and "不存在" in (result.error or "")


def test_parse_document_extractor_error_marks_failed(conn, config: Config) -> None:
    _seed_doc(conn)
    result = parse_document(
        conn,
        config,
        FakeProvider(),
        doc_id="doc1",
        extractor=_FakeExtractor(RuntimeError("PDF 损坏")),
    )
    assert result.status == "failed" and "PDF 提取失败" in (result.error or "")
    row = conn.execute("SELECT parse_status FROM document WHERE doc_id = 'doc1'").fetchone()
    assert row is not None and row[0] == "failed"


def test_parse_document_empty_text_marks_failed(conn, config: Config) -> None:
    _seed_doc(conn)
    result = parse_document(
        conn, config, FakeProvider(), doc_id="doc1", extractor=_FakeExtractor("   ")
    )
    assert result.status == "failed" and "无文本" in (result.error or "")


def test_parse_document_contract_failure_marks_failed(conn, config: Config) -> None:
    _seed_doc(conn)
    provider = _fixed_provider({"bad": "shape"})  # 畸形输出，重试后仍 DEGRADED
    result = parse_document(conn, config, provider, doc_id="doc1", extractor=_FakeExtractor("文本"))
    assert result.status == "failed"
    assert result.run_id is not None  # agent_run 留痕
    row = conn.execute("SELECT parse_status FROM document WHERE doc_id = 'doc1'").fetchone()
    assert row is not None and row[0] == "failed"
    n = conn.execute("SELECT COUNT(*) FROM document_analysis WHERE doc_id = 'doc1'").fetchone()
    assert n is not None and n[0] == 0  # 不合格产出不入库


def test_parse_document_forecast_rules(conn, config: Config) -> None:
    _seed_doc(conn, "no_code", code=None)  # 无标的代码
    _seed_doc(conn, "empty_rows", code="600519")
    payload = {
        **_VALID_ANALYSIS,
        "forecasts": [
            {"fiscal_year": "2027E", "net_profit": None, "eps": None, "target_price": None}
        ],
    }
    r1 = parse_document(
        conn,
        config,
        _fixed_provider(payload),
        doc_id="no_code",
        extractor=_FakeExtractor("文本"),
    )
    r2 = parse_document(
        conn,
        config,
        _fixed_provider(payload),
        doc_id="empty_rows",
        extractor=_FakeExtractor("文本"),
    )
    assert r1.status == "parsed" and r2.status == "parsed"
    n = conn.execute("SELECT COUNT(*) FROM analyst_forecast").fetchone()
    assert n is not None and n[0] == 0  # 无 code 不拆行；全空行不写


# ---------- parse_pending ----------


def test_parse_pending_scans_only_pending_with_limit(conn, config: Config) -> None:
    _seed_doc(conn, "p1")
    _seed_doc(conn, "p2")
    _seed_doc(conn, "p3", parse_status="failed")  # failed 不重扫
    _seed_doc(conn, "p4", parse_status="parsed")

    provider = _fixed_provider(_VALID_ANALYSIS)
    results = parse_pending(conn, config, provider, limit=1, extractor=_FakeExtractor("文本"))
    assert len(results) == 1  # limit 生效

    results = parse_pending(conn, config, provider, extractor=_FakeExtractor("文本"))
    statuses = {r.doc_id: r.status for r in results}
    assert "p2" in statuses  # 剩余 pending 被扫到
    assert "p3" not in statuses and "p4" not in statuses  # failed/parsed 不重扫


def test_parse_pending_isolates_failures(conn, config: Config) -> None:
    _seed_doc(conn, "ok1")
    _seed_doc(conn, "bad", code="000001")  # 用元数据区分，重试轮次也稳定命中
    _seed_doc(conn, "ok2")

    class _FlakyExtractor:
        def extract_text(self, file_path: str) -> str:
            return "文本"

    def responder(messages: Sequence[Message], model: str, params: dict[str, Any]) -> str:
        text = "\n".join(m.content for m in messages)
        if "000001" in text:
            return '{"bad": true}'  # 该份重试后仍 DEGRADED
        return json.dumps(_VALID_ANALYSIS, ensure_ascii=False)

    provider = FakeProvider(responder)
    results = parse_pending(conn, config, provider, extractor=_FlakyExtractor())
    by_id = {r.doc_id: r.status for r in results}
    assert by_id == {"bad": "failed", "ok1": "parsed", "ok2": "parsed"}  # 单份失败不扩散
