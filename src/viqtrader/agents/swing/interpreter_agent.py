"""信号解读 Agent（P4.5，设计方案 4.2 ⑤）。

把选股 Agent 的结构化信号翻译成人话，输出客观可验证的失效条件与风险；
胜率佐证只能引用学习上下文，空库明示「待积累」，禁止编数字。
"""

from __future__ import annotations

import json

import duckdb

from viqtrader.agents.context import build_learning_context
from viqtrader.agents.contracts.base import ContractRun, run_contract
from viqtrader.agents.contracts.persist import persist_contract_run
from viqtrader.agents.contracts.swing import (
    SignalInterpretContract,
    SwingPickOutput,
)
from viqtrader.agents.llm.base import LLMProvider, Message
from viqtrader.agents.prompts.registry import PromptRegistry
from viqtrader.agents.swing.picker_agent import format_bars
from viqtrader.agents.swing.screen import ScreenCandidate
from viqtrader.config.loader import Config

__all__ = ["run_interpreter_agent"]


def run_interpreter_agent(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    provider: LLMProvider,
    *,
    candidate: ScreenCandidate,
    pick: SwingPickOutput,
) -> tuple[str, ContractRun]:
    """执行信号解读 Agent，返回 (run_id, 契约运行结果)。parsed 为 SignalInterpretOutput。"""
    prompt_bars_limit = int(config.get("swing_track.agent.prompt_bars", 60))
    bars = candidate.bars[-prompt_bars_limit:]

    pick_signal = {
        "signal_type": pick.signal_type,
        "pattern_desc": pick.pattern_desc,
        "entry_ref": pick.entry_ref,
        "stop_ref": pick.stop_ref,
        "target_ref": pick.target_ref,
        "risks": list(pick.risks),
    }
    learning_context = build_learning_context(
        conn,
        config,
        agent_name="signal_interpret",
        decision_type="signal_interpret",
        code=candidate.code,
        market=candidate.market,
    )
    registry = PromptRegistry()
    prompt = registry.render(
        "signal_interpret",
        code=candidate.code,
        market=candidate.market,
        name=candidate.name,
        signal_date=str(candidate.signal_date),
        pick_signal=json.dumps(pick_signal, ensure_ascii=False),
        bars=format_bars(bars),
        learning_context=learning_context,
        schema_hint=SignalInterpretContract.schema_hint,
    )
    model = str(config.get("llm.model_cheap"))
    run = run_contract(provider, SignalInterpretContract(), [Message("user", prompt)], model)
    snapshot = {
        "code": candidate.code,
        "signal_date": str(candidate.signal_date),
        "pick_signal": pick_signal,
        "bars": format_bars(bars),
        "learning_context": learning_context,
    }
    run_id = persist_contract_run(
        conn,
        config,
        run,
        agent_name="signal_interpret",
        prompt_version=registry.version("signal_interpret"),
        input_snapshot=snapshot,
    )
    return run_id, run
