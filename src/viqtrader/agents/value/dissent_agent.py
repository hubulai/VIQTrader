"""反方 Agent（设计方案 4.1 ③，P4.4）：投委会交叉验证的另一侧。

输入估值方完整 JSON 输出与研报（外部观点），逐条攻击假设、找财务雷点。

三段拆分（P7 工程修复）：`prepare_dissent_inputs`（读库）→ `execute_dissent_llm`
（零 DB）→ `store_dissent_run`（写库）；旧入口 `run_dissent_agent` 保留为薄包装。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import duckdb

from viqtrader.agents.context import build_learning_context
from viqtrader.agents.contracts.base import ContractRun, run_contract
from viqtrader.agents.contracts.dissent import DissentContract
from viqtrader.agents.contracts.persist import persist_contract_run
from viqtrader.agents.llm.base import LLMProvider, Message
from viqtrader.agents.prompts.registry import PromptRegistry
from viqtrader.config.loader import Config


@dataclass(frozen=True, slots=True)
class DissentPrepared:
    """反方 Agent 的「已装配、未调用」状态：LLM 段只依赖本对象，不触碰数据库。"""

    code: str
    prompt: str
    prompt_version: str
    model: str
    input_snapshot: dict[str, Any]
    contract: DissentContract


def prepare_dissent_inputs(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    valuation_payload: dict[str, Any],
    *,
    code: str,
    documents: str = "无外部研报输入",
) -> DissentPrepared:
    """读库段：读学习上下文并渲染反方 prompt，退出即可释放连接。"""
    registry = PromptRegistry()
    learning_context = build_learning_context(
        conn, config, agent_name="dissent", decision_type="dissent", code=code
    )
    prompt = registry.render(
        "dissent",
        valuation_output=json.dumps(valuation_payload, ensure_ascii=False),
        documents=documents,
        learning_context=learning_context,
        schema_hint=DissentContract.schema_hint,
    )
    return DissentPrepared(
        code=code,
        prompt=prompt,
        prompt_version=registry.version("dissent"),
        model=str(config.get("llm.model_deep")),
        input_snapshot={
            "code": code,
            "valuation_output": valuation_payload,
            "documents": documents,
            "learning_context": learning_context,  # 注入内容随快照落库，可还原
        },
        contract=DissentContract(),
    )


def execute_dissent_llm(provider: LLMProvider, prepared: DissentPrepared) -> ContractRun:
    """LLM 段：零数据库依赖（调用方此时不应持有任何连接）。"""
    return run_contract(
        provider, prepared.contract, [Message("user", prepared.prompt)], prepared.model
    )


def store_dissent_run(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    run: ContractRun,
    prepared: DissentPrepared,
) -> str:
    """写库段：落 agent_run（仅 SUCCESS 追加 agent_output），返回 run_id。"""
    return persist_contract_run(
        conn,
        config,
        run,
        agent_name="dissent",
        prompt_version=prepared.prompt_version,
        input_snapshot=prepared.input_snapshot,
    )


def run_dissent_agent(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    provider: LLMProvider,
    valuation_payload: dict[str, Any],
    *,
    code: str,
    documents: str = "无外部研报输入",
) -> tuple[str, ContractRun]:
    """执行反方 Agent，返回 (run_id, 契约运行结果)。成功时 parsed 为 DissentOutput。

    三段串联的兼容入口：调用方若在长任务中复用，应改用三段函数并按段释放连接。
    """
    prepared = prepare_dissent_inputs(
        conn, config, valuation_payload, code=code, documents=documents
    )
    run = execute_dissent_llm(provider, prepared)
    return store_dissent_run(conn, config, run, prepared), run


__all__ = [
    "DissentPrepared",
    "execute_dissent_llm",
    "prepare_dissent_inputs",
    "run_dissent_agent",
    "store_dissent_run",
]
