"""P4.2 契约测试：强类型解析、重试→降级、agent_output 落库门禁、prompt 版本回溯。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import duckdb

from viqtrader.agents.contracts.base import (
    ContractRun,
    parse_json_response,
    run_contract,
)
from viqtrader.agents.contracts.dissent import DissentContract, DissentOutput
from viqtrader.agents.contracts.persist import persist_contract_run
from viqtrader.agents.contracts.valuation import ValuationContract, ValuationOutput
from viqtrader.agents.llm.base import Message
from viqtrader.agents.llm.fake import FakeProvider
from viqtrader.config.loader import Config

_VALID: dict[str, Any] = {
    "code": "600519",
    "intrinsic_value_range": [28.5, 34.0],
    "current_price": 19.2,
    "margin_of_safety": 0.32,
    "methods": [{"name": "DCF", "value": 31.0, "assumptions": {"wacc": 0.09}}],
    "moat_rating": "strong",
    "moat_evidence": ["市占率 35% 且稳定"],
    "key_risks": ["需求下滑"],
    "confidence": 0.72,
    "variant_perception": "我比市场更保守",
    "data_refs": ["fact_financial@2024-12-31"],
}


def test_valuation_contract_accepts_valid_payload() -> None:
    result = ValuationContract().validate(_VALID)
    assert result.ok
    assert result.errors == ()
    assert isinstance(result.parsed, ValuationOutput)
    assert result.parsed.moat_rating == "strong"
    assert result.parsed.intrinsic_value_range == (28.5, 34.0)
    assert result.payload == _VALID


def test_valuation_contract_collects_all_errors() -> None:
    bad = dict(_VALID, moat_rating="huge", confidence=1.5, data_refs=[])
    result = ValuationContract().validate(bad)
    assert not result.ok
    assert result.parsed is None
    joined = " | ".join(result.errors)
    assert "moat_rating" in joined
    assert "confidence" in joined
    assert "data_refs" in joined


def test_dissent_contract_parses_and_rejects_bad_severity() -> None:
    ok_payload = {
        "code": "600519",
        "rebuttals": [{"target": "assumptions.wacc", "attack": "过于乐观", "severity": "high"}],
        "fatal_issues": [],
        "verdict": "需补充数据",
        "needs_data": ["应收账款账龄"],
    }
    result = DissentContract().validate(ok_payload)
    assert result.ok
    assert isinstance(result.parsed, DissentOutput)
    assert result.parsed.rebuttals[0].severity == "high"

    bad = dict(ok_payload, rebuttals=[{"target": "t", "attack": "a", "severity": "critical"}])
    assert not DissentContract().validate(bad).ok


def test_parse_json_response_tolerates_code_fence_and_prose() -> None:
    text = "结论如下：\n```json\n" + json.dumps(_VALID) + "\n```\n以上。"
    assert parse_json_response(text)["code"] == "600519"


def test_run_contract_retries_once_then_success() -> None:
    seen: list[Sequence[Message]] = []

    def responder(messages: Sequence[Message], model: str, params: dict[str, Any]) -> str:
        seen.append(tuple(messages))
        if len(seen) == 1:
            return json.dumps({"code": "600519"})  # 缺字段 → 不合格
        return json.dumps(_VALID)

    run = run_contract(
        FakeProvider(responder), ValuationContract(), [Message("user", "估一下")], "deepseek-chat"
    )
    assert run.status == "SUCCESS"
    assert run.attempts == 2
    assert "不满足契约" in seen[1][-1].content  # 第二次带上错误提示


def test_run_contract_degrades_after_retry_exhausted() -> None:
    run = run_contract(
        FakeProvider(content="完全不是 JSON"), ValuationContract(), [Message("user", "x")], "m"
    )
    assert run.status == "DEGRADED"
    assert run.attempts == 2
    assert not run.validation.ok


def test_run_contract_failed_when_provider_errors() -> None:
    class Boom:
        def complete(self, messages: object, model: str, **params: object) -> object:
            raise RuntimeError("network down")

    run = run_contract(Boom(), ValuationContract(), [Message("user", "x")], "m")
    assert run.status == "FAILED"
    assert run.response is None


def test_persist_success_writes_agent_run_and_agent_output(
    conn: duckdb.DuckDBPyConnection, config: Config
) -> None:
    run = run_contract(
        FakeProvider(content=json.dumps(_VALID)),
        ValuationContract(),
        [Message("user", "x")],
        "deepseek-chat",
    )
    run_id = persist_contract_run(
        conn, config, run, agent_name="valuation", prompt_version="valuation.v1"
    )

    audit = conn.execute(
        "SELECT status, prompt_version, config_hash, code_version FROM agent_run WHERE run_id = ?",
        [run_id],
    ).fetchone()
    assert audit is not None
    assert audit[0] == "SUCCESS"
    assert audit[1] == "valuation.v1"
    assert audit[2] == str(config.content_hash())
    assert audit[3]

    row = conn.execute(
        "SELECT output_json, data_refs FROM agent_output WHERE run_id = ?", [run_id]
    ).fetchone()
    assert row is not None
    assert json.loads(row[0])["code"] == "600519"
    assert json.loads(row[1]) == ["fact_financial@2024-12-31"]


def test_persist_degraded_writes_run_but_not_output(
    conn: duckdb.DuckDBPyConnection, config: Config
) -> None:
    run: ContractRun = run_contract(
        FakeProvider(content="不是 JSON"),
        ValuationContract(),
        [Message("user", "x")],
        "deepseek-chat",
    )
    run_id = persist_contract_run(
        conn, config, run, agent_name="valuation", prompt_version="valuation.v1"
    )

    audit = conn.execute("SELECT status FROM agent_run WHERE run_id = ?", [run_id]).fetchone()
    assert audit is not None and audit[0] == "DEGRADED"
    count = conn.execute("SELECT COUNT(*) FROM agent_output WHERE run_id = ?", [run_id]).fetchone()
    assert count is not None and count[0] == 0
