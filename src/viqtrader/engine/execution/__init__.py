"""P2 执行层：网关 + 上下文构建 + 执行编排服务。"""

from viqtrader.engine.execution.context import build_rule_context
from viqtrader.engine.execution.gateway import DryRunGateway, ExecutionGateway, SimulatedGateway
from viqtrader.engine.execution.service import ExecutionResult, ExecutionService

__all__ = [
    "DryRunGateway",
    "ExecutionGateway",
    "ExecutionResult",
    "ExecutionService",
    "SimulatedGateway",
    "build_rule_context",
]
