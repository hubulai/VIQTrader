"""波段轨策略生命周期（P3.5 Walk-Forward 滚动验证 + P3.6 5 阶段 KPI 状态机）。

价值轨不适用此生命周期（走 thesis 证伪）。
"""

from viqtrader.lifecycle.equity import (
    StrategyTrade,
    replay_strategy_equity,
    strategy_base_cash,
    strategy_trades,
)
from viqtrader.lifecycle.kpi import (
    ROLLING_WINDOW,
    compute_kpi_snapshot,
    consecutive_low_sharpe_calendar_days,
    daily_returns,
    max_drawdown_of,
    rolling_max_drawdown,
    rolling_sharpe,
    sharpe_of,
)
from viqtrader.lifecycle.registry import LifecycleEngine, LifecycleEvaluation, Stage
from viqtrader.lifecycle.robustness import (
    BlockResult,
    PBOAnalysis,
    RobustnessReport,
    compute_pbo_cpcv,
    deflated_sharpe_ratio,
    evaluate_robustness,
)
from viqtrader.lifecycle.walk_forward import (
    TRIAL_ACCOUNT,
    WalkForwardResult,
    WalkForwardRunner,
    WindowResult,
)

__all__ = [
    "ROLLING_WINDOW",
    "TRIAL_ACCOUNT",
    "BlockResult",
    "LifecycleEngine",
    "LifecycleEvaluation",
    "PBOAnalysis",
    "RobustnessReport",
    "Stage",
    "StrategyTrade",
    "WalkForwardResult",
    "WalkForwardRunner",
    "WindowResult",
    "compute_kpi_snapshot",
    "compute_pbo_cpcv",
    "consecutive_low_sharpe_calendar_days",
    "daily_returns",
    "deflated_sharpe_ratio",
    "evaluate_robustness",
    "max_drawdown_of",
    "replay_strategy_equity",
    "rolling_max_drawdown",
    "rolling_sharpe",
    "sharpe_of",
    "strategy_base_cash",
    "strategy_trades",
]
