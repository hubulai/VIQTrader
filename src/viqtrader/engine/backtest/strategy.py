"""P3.1 策略接口：回测/模拟盘/未来实盘三环境同构的唯一契约。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from viqtrader.engine.backtest.context import StrategyContext
    from viqtrader.engine.types import OrderIntent


class Strategy(Protocol):
    """策略契约：每个交易日被调用一次，产出订单意图列表。

    - 决策时点模型（已确认方案 A）：T-1 收盘决策 → T 日成交。
      ctx 的一切数据查询严格限于 trade_date 之前（SQL 层机制保证，非自觉）。
    - 双轨：在 OrderIntent 上指定 account_id / mode；
      单轨策略只产本轨 Intent，混合策略可两轨都产。
    - 返回空列表表示当日无操作；回测首日通常无历史数据，策略需自行容忍。
    """

    def on_bar(self, ctx: StrategyContext) -> list[OrderIntent]: ...
