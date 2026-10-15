"""P3.1 事件驱动回测引擎（回测/模拟盘同构，方案 A：T-1 收盘决策 → T 日成交）。"""

from viqtrader.engine.backtest.context import (
    BarView,
    FinancialView,
    StrategyContext,
    ValuationView,
)
from viqtrader.engine.backtest.engine import BacktestEngine, default_rules
from viqtrader.engine.backtest.result import (
    PRECISION_NOTE,
    BacktestResult,
    DailyEquity,
    RejectedIntent,
)
from viqtrader.engine.backtest.strategy import Strategy

__all__ = [
    "PRECISION_NOTE",
    "BacktestEngine",
    "BacktestResult",
    "BarView",
    "DailyEquity",
    "FinancialView",
    "RejectedIntent",
    "Strategy",
    "StrategyContext",
    "ValuationView",
    "default_rules",
]
