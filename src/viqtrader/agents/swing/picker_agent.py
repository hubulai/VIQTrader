"""波段选股 Agent（P4.5，设计方案 4.2 ④）。

输入确定性初筛候选（screen.ScreenCandidate）→ swing_pick.v1 prompt →
SwingPickContract（信号类型枚举 + 参考位必须在 K 线真实区间）→ agent_run 落库。
只描述形态与风险，不预测；无资金流/题材数据，禁止编造。
"""

from __future__ import annotations

import json

import duckdb

from viqtrader.agents.context import build_learning_context
from viqtrader.agents.contracts.base import ContractRun, run_contract
from viqtrader.agents.contracts.persist import persist_contract_run
from viqtrader.agents.contracts.swing import SwingPickContract
from viqtrader.agents.llm.base import LLMProvider, Message
from viqtrader.agents.prompts.registry import PromptRegistry
from viqtrader.agents.swing.screen import ScreenBar, ScreenCandidate
from viqtrader.config.loader import Config

__all__ = ["format_bars", "run_picker_agent"]


def format_bars(bars: tuple[ScreenBar, ...]) -> str:
    """K 线渲染为紧凑 CSV 文本（升序）。"""
    return "\n".join(
        f"{b.trade_date},{b.open:.2f},{b.high:.2f},{b.low:.2f},"
        f"{b.close:.2f},{int(b.volume)},{b.amount:.0f}"
        for b in bars
    )


def run_picker_agent(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    provider: LLMProvider,
    *,
    candidate: ScreenCandidate,
) -> tuple[str, ContractRun]:
    """执行波段选股 Agent，返回 (run_id, 契约运行结果)。成功时 parsed 为 SwingPickOutput。"""
    prompt_bars_limit = int(config.get("swing_track.agent.prompt_bars", 60))
    bars = candidate.bars[-prompt_bars_limit:]
    price_bounds = (min(b.low for b in bars), max(b.high for b in bars))

    learning_context = build_learning_context(
        conn,
        config,
        agent_name="swing_pick",
        decision_type="swing_pick",
        code=candidate.code,
        market=candidate.market,
    )
    registry = PromptRegistry()
    prompt = registry.render(
        "swing_pick",
        code=candidate.code,
        market=candidate.market,
        name=candidate.name,
        signal_date=str(candidate.signal_date),
        screen_features=json.dumps(candidate.features, ensure_ascii=False),
        bar_count=len(bars),
        bars=format_bars(bars),
        learning_context=learning_context,
        schema_hint=SwingPickContract.schema_hint,
    )
    model = str(config.get("llm.model_cheap"))
    contract = SwingPickContract(code=candidate.code, price_bounds=price_bounds)
    run = run_contract(provider, contract, [Message("user", prompt)], model)
    snapshot = {
        "code": candidate.code,
        "market": candidate.market,
        "name": candidate.name,
        "signal_date": str(candidate.signal_date),
        "screen_features": candidate.features,
        "bars": format_bars(bars),
        "price_bounds": list(price_bounds),
        "learning_context": learning_context,
    }
    run_id = persist_contract_run(
        conn,
        config,
        run,
        agent_name="swing_pick",
        prompt_version=registry.version("swing_pick"),
        input_snapshot=snapshot,
    )
    return run_id, run
