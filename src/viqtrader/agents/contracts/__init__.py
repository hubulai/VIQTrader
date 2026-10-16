"""Agent 输出契约（P4.2）：通用机制 + 样例契约。

- base：ValidationResult / AgentContract 协议 / run_contract（重试→降级）/ JSON 抽取
- valuation：深度估值 Agent 输出契约（设计方案 4.1 ②）
- dissent：反方 Agent 输出契约（设计方案 4.1 ③）
- persist：契约运行落库（DEGRADED 不写 agent_output）
"""

from __future__ import annotations

from viqtrader.agents.contracts.base import (
    AgentContract,
    BaseContract,
    ContractError,
    ContractRun,
    ValidationResult,
    parse_json_response,
    run_contract,
)
from viqtrader.agents.contracts.dissent import DissentContract, DissentOutput
from viqtrader.agents.contracts.persist import persist_contract_run
from viqtrader.agents.contracts.valuation import (
    ValuationContract,
    ValuationMethod,
    ValuationOutput,
)

__all__ = [
    "AgentContract",
    "BaseContract",
    "ContractError",
    "ContractRun",
    "DissentContract",
    "DissentOutput",
    "ValidationResult",
    "ValuationContract",
    "ValuationMethod",
    "ValuationOutput",
    "parse_json_response",
    "persist_contract_run",
    "run_contract",
]
