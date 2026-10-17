"""组合归因解读 Agent（P4.6，设计方案 4.3 ⑥ 定性层）。

输入为 attribution.brinson.compute_brinson 的定量结果（BrinsonResult），
本 Agent 只做定性解读：收益来自行业配置还是个股选择、现金拖累、关键发现。
不重算数字、不碰数据库写归因结论之外的业务表；落库走 persist_contract_run 统一通道。
"""

from __future__ import annotations

import json
from dataclasses import asdict

import duckdb

from viqtrader.agents.context import build_learning_context
from viqtrader.agents.contracts.base import ContractRun, run_contract
from viqtrader.agents.contracts.persist import persist_contract_run
from viqtrader.agents.contracts.review import AttributionReviewContract
from viqtrader.agents.llm.base import LLMProvider, Message
from viqtrader.agents.prompts.registry import PromptRegistry
from viqtrader.attribution.brinson import BrinsonResult
from viqtrader.config.loader import Config

__all__ = ["run_attribution_review_agent"]


def _format_brinson(result: BrinsonResult) -> str:
    """BrinsonResult → 紧凑 JSON（dataclass 嵌套结构直接 asdict）。"""
    return json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str)


def run_attribution_review_agent(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    provider: LLMProvider,
    *,
    result: BrinsonResult,
) -> tuple[str, ContractRun]:
    """执行组合归因解读 Agent，返回 (run_id, 契约运行结果)。

    成功时 parsed 为 AttributionReviewOutput；DEGRADED/FAILED 时 agent_output 不落库。
    """
    registry = PromptRegistry()
    brinson_json = _format_brinson(result)
    learning_context = build_learning_context(
        conn,
        config,
        agent_name="attribution_review",
        decision_type="attribution_review",
    )
    prompt = registry.render(
        "attribution_review",
        account_id=result.account_id,
        start=result.start,
        end=result.end,
        brinson_result=brinson_json,
        learning_context=learning_context,
        schema_hint=AttributionReviewContract.schema_hint,
    )
    model = str(config.get("llm.model_cheap"))
    run = run_contract(provider, AttributionReviewContract(), [Message("user", prompt)], model)
    run_id = persist_contract_run(
        conn,
        config,
        run,
        agent_name="attribution_review",
        prompt_version=registry.version("attribution_review"),
        input_snapshot={
            "account_id": result.account_id,
            "start": result.start,
            "end": result.end,
            "brinson_result": json.loads(brinson_json),
            "learning_context": learning_context,
        },
    )
    return run_id, run
