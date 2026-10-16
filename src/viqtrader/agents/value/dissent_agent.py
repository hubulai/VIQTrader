"""反方 Agent（设计方案 4.1 ③，P4.4）：投委会交叉验证的另一侧。

输入估值方完整 JSON 输出与研报（外部观点），逐条攻击假设、找财务雷点。
"""

from __future__ import annotations

import json
from typing import Any

import duckdb

from viqtrader.agents.contracts.base import ContractRun, run_contract
from viqtrader.agents.contracts.dissent import DissentContract
from viqtrader.agents.contracts.persist import persist_contract_run
from viqtrader.agents.llm.base import LLMProvider, Message
from viqtrader.agents.prompts.registry import PromptRegistry
from viqtrader.config.loader import Config


def run_dissent_agent(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    provider: LLMProvider,
    valuation_payload: dict[str, Any],
    *,
    code: str,
    documents: str = "无外部研报输入",
) -> tuple[str, ContractRun]:
    """执行反方 Agent，返回 (run_id, 契约运行结果)。成功时 parsed 为 DissentOutput。"""
    registry = PromptRegistry()
    prompt = registry.render(
        "dissent",
        valuation_output=json.dumps(valuation_payload, ensure_ascii=False),
        documents=documents,
        schema_hint=DissentContract.schema_hint,
    )
    model = str(config.get("llm.model_deep"))
    run = run_contract(provider, DissentContract(), [Message("user", prompt)], model)
    run_id = persist_contract_run(
        conn,
        config,
        run,
        agent_name="dissent",
        prompt_version=registry.version("dissent"),
        input_snapshot={
            "code": code,
            "valuation_output": valuation_payload,
            "documents": documents,
        },
    )
    return run_id, run
