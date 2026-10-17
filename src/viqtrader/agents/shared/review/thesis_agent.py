"""单持仓 thesis 复核 Agent（P4.6，设计方案 4.3 ⑥ 定性层）。

对照建仓时的 thesis 摘要/关键假设与区间事实，判断买入逻辑是否仍成立。
价值轨纪律硬编码进契约：thesis 证伪（falsified）必须建议 exit，价格下跌不算证伪。

输入 ThesisReviewInput 由 a2 的 review pipeline 装配（thesis 表 + 期间财报/公告事实），
本模块不做数据装配，保持「入口薄、IO 边界在 pipeline」与既有 Agent 一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import duckdb

from viqtrader.agents.context import build_learning_context
from viqtrader.agents.contracts.base import ContractRun, run_contract
from viqtrader.agents.contracts.persist import persist_contract_run
from viqtrader.agents.contracts.review import ThesisReviewContract
from viqtrader.agents.llm.base import LLMProvider, Message
from viqtrader.agents.prompts.registry import PromptRegistry
from viqtrader.config.loader import Config

__all__ = ["ThesisReviewInput", "run_thesis_review_agent"]


@dataclass(frozen=True, slots=True)
class ThesisReviewInput:
    """单持仓 thesis 复核的装配输入（pipeline 负责从库中组装）。"""

    position_id: str
    code: str
    market: str
    name: str
    mode: str  # value / swing
    start: date
    end: date
    thesis_summary: str  # thesis.summary 原文
    thesis_assumptions: str  # thesis.assumptions 渲染后的文本（无则「无」）
    period_facts: str  # 区间事实（财报/公告/行情要点，pipeline 装配）


def run_thesis_review_agent(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    provider: LLMProvider,
    *,
    review_input: ThesisReviewInput,
) -> tuple[str, ContractRun]:
    """执行单持仓 thesis 复核 Agent，返回 (run_id, 契约运行结果)。

    成功时 parsed 为 ThesisReviewOutput；DEGRADED/FAILED 时 agent_output 不落库。
    复核属卖出级判断，使用深度模型（llm.model_deep）。
    """
    registry = PromptRegistry()
    learning_context = build_learning_context(
        conn,
        config,
        agent_name="thesis_review",
        decision_type="thesis_review",
        code=review_input.code,
        market=review_input.market,
    )
    contract = ThesisReviewContract(expected_position_id=review_input.position_id)
    prompt = registry.render(
        "thesis_review",
        position_id=review_input.position_id,
        code=review_input.code,
        market=review_input.market,
        name=review_input.name,
        mode=review_input.mode,
        start=review_input.start.isoformat(),
        end=review_input.end.isoformat(),
        thesis_summary=review_input.thesis_summary,
        thesis_assumptions=review_input.thesis_assumptions,
        period_facts=review_input.period_facts,
        learning_context=learning_context,
        schema_hint=contract.schema_hint,
    )
    model = str(config.get("llm.model_deep"))
    run = run_contract(provider, contract, [Message("user", prompt)], model)
    run_id = persist_contract_run(
        conn,
        config,
        run,
        agent_name="thesis_review",
        prompt_version=registry.version("thesis_review"),
        input_snapshot={
            "position_id": review_input.position_id,
            "code": review_input.code,
            "market": review_input.market,
            "name": review_input.name,
            "mode": review_input.mode,
            "start": review_input.start.isoformat(),
            "end": review_input.end.isoformat(),
            "thesis_summary": review_input.thesis_summary,
            "thesis_assumptions": review_input.thesis_assumptions,
            "period_facts": review_input.period_facts,
            "learning_context": learning_context,
        },
    )
    return run_id, run
