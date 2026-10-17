"""风控解读 Agent（P4.6，设计方案 4.3 ⑧）。

把风控引擎落库的 risk_event 规则事件翻译成可读提示与具体建议动作。
事件由 a2 pipeline 按账户+区间从 risk_event 表装配为 RiskEventInput；
区间无事件时不应调用本 Agent（fail-fast），解读空列表没有业务意义。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date

import duckdb

from viqtrader.agents.context import build_learning_context
from viqtrader.agents.contracts.base import ContractRun, run_contract
from viqtrader.agents.contracts.persist import persist_contract_run
from viqtrader.agents.contracts.review import RiskInterpretContract
from viqtrader.agents.llm.base import LLMProvider, Message
from viqtrader.agents.prompts.registry import PromptRegistry
from viqtrader.config.loader import Config

__all__ = ["RiskEventInput", "run_risk_interpreter_agent"]


@dataclass(frozen=True, slots=True)
class RiskEventInput:
    """risk_event 表行的解读用子集（pipeline 装配）。"""

    event_id: str
    rule_key: str
    severity: str  # INFO / WARN / CRITICAL / FATAL
    decision: str  # ALLOW / DENY / ALERT
    message: str
    code: str | None = None
    event_ts: str | None = None  # ISO 时间字符串


def run_risk_interpreter_agent(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    provider: LLMProvider,
    *,
    account_id: str,
    start: date,
    end: date,
    events: Sequence[RiskEventInput],
) -> tuple[str, ContractRun]:
    """执行风控解读 Agent，返回 (run_id, 契约运行结果)。

    成功时 parsed 为 RiskInterpretOutput；DEGRADED/FAILED 时 agent_output 不落库。

    Raises:
        ValueError: 事件列表为空（无事件不应触发解读）或 start > end。
    """
    if start > end:
        raise ValueError(f"风控解读区间非法：start={start} > end={end}")
    if not events:
        raise ValueError("风控事件列表为空，无需解读（无事件不应调用风控解读 Agent）")

    registry = PromptRegistry()
    events_json = json.dumps([asdict(e) for e in events], ensure_ascii=False, indent=2, default=str)
    learning_context = build_learning_context(
        conn,
        config,
        agent_name="risk_interpret",
        decision_type="risk_interpret",
    )
    prompt = registry.render(
        "risk_interpret",
        account_id=account_id,
        start=start.isoformat(),
        end=end.isoformat(),
        risk_events=events_json,
        learning_context=learning_context,
        schema_hint=RiskInterpretContract.schema_hint,
    )
    model = str(config.get("llm.model_cheap"))
    run = run_contract(provider, RiskInterpretContract(), [Message("user", prompt)], model)
    run_id = persist_contract_run(
        conn,
        config,
        run,
        agent_name="risk_interpret",
        prompt_version=registry.version("risk_interpret"),
        input_snapshot={
            "account_id": account_id,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "events": json.loads(events_json),
            "learning_context": learning_context,
        },
    )
    return run_id, run
