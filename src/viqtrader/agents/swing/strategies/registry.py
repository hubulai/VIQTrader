"""波段初筛策略注册表（P4.5.1）。

后端契约先行：P6 控制台通过 list_strategies() 拿全部策略的展示元数据与
params_schema 渲染选择界面；screen/pipeline/backtest 统一经 get_strategy() 解析
strategy_id，新策略只需在本模块 _REGISTRY 注册一行即可全链路可用。
"""

from __future__ import annotations

from dataclasses import dataclass

from viqtrader.agents.swing.strategies.base import ParamSpec, ScreenStrategy
from viqtrader.agents.swing.strategies.trend_momentum import TrendMomentumStrategy

__all__ = ["DEFAULT_STRATEGY_ID", "StrategyInfo", "get_strategy", "list_strategies"]

DEFAULT_STRATEGY_ID = "trend_momentum"

_REGISTRY: dict[str, ScreenStrategy] = {
    s.strategy_id: s
    for s in (
        TrendMomentumStrategy(),
        # 新策略在此追加一行即可
    )
}


@dataclass(frozen=True, slots=True)
class StrategyInfo:
    """策略展示元数据 + 参数 schema（P6 界面渲染契约）。"""

    strategy_id: str
    display_name: str
    description: str
    params_schema: tuple[ParamSpec, ...]


def get_strategy(strategy_id: str) -> ScreenStrategy:
    """按 id 取策略；未知 id 抛 ValueError 并列出可用项。"""
    strategy = _REGISTRY.get(strategy_id)
    if strategy is None:
        raise ValueError(f"未知波段初筛策略 {strategy_id!r}，可用策略：{sorted(_REGISTRY)}")
    return strategy


def list_strategies() -> list[StrategyInfo]:
    """列出全部已注册策略（供界面渲染与 CLI 展示）。"""
    return [
        StrategyInfo(
            strategy_id=s.strategy_id,
            display_name=s.display_name,
            description=s.description,
            params_schema=s.params_schema(),
        )
        for s in _REGISTRY.values()
    ]
