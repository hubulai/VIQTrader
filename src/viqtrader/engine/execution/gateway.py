"""P2 执行网关（设计文档第八、九节）。

- SimulatedGateway：回测用，按当日 bar 撮合（开盘价 ± 滑点，限价/涨跌停约束）
- DryRunGateway：模拟盘用，按最近收盘参考价模拟成交，不真实下单

三个环境差异只封装在网关；OrderIntent + 规则引擎 + 账本引擎完全同构。
STOP / STOP_LIMIT 订单类型 v1 不支持（策略层触发后转 LIMIT/MARKET）。
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

import duckdb

from viqtrader.engine.market_status import get_market_status
from viqtrader.engine.types import (
    FillResult,
    FillStatus,
    OrderIntent,
    OrderSide,
    OrderType,
    to_decimal,
)

_CENT = Decimal("0.01")


class ExecutionGateway(Protocol):
    """执行网关契约：只负责「撮合出价」，记账由 LedgerEngine 完成。"""

    def fill(self, intent: OrderIntent, trade_date: date) -> FillResult: ...


def _round_cent(price: Decimal) -> Decimal:
    return price.quantize(_CENT, rounding=ROUND_HALF_UP)


class _BarBasedGateway:
    """公共基类：从 fact_daily_bar 取行情做模拟撮合。"""

    def __init__(self, conn: duckdb.DuckDBPyConnection, config: Any) -> None:
        self._conn = conn
        self._slippage = Decimal(
            str((config.as_dict().get("fees", {}) or {}).get("slippage_bps", 0))
        ) / Decimal("10000")

    def _bar(self, code: str, trade_date: date) -> tuple[Any, ...] | None:
        return self._conn.execute(
            """
            SELECT open, high, low, close FROM fact_daily_bar
            WHERE code = ? AND trade_date = ?
            """,
            [code, trade_date],
        ).fetchone()

    def _slippage_price(self, price: Decimal, side: OrderSide) -> Decimal:
        """MARKET 单滑点：买入向上、卖出向下。"""
        if side == OrderSide.BUY:
            return _round_cent(price * (Decimal("1") + self._slippage))
        return _round_cent(price * (Decimal("1") - self._slippage))


class SimulatedGateway(_BarBasedGateway):
    """回测撮合网关：按当日 bar 撮合。"""

    def fill(self, intent: OrderIntent, trade_date: date) -> FillResult:
        if intent.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            return FillResult(
                status=FillStatus.UNSUPPORTED,
                message=f"v1 网关不支持 {intent.order_type} 订单",
            )
        bar = self._bar(intent.code, trade_date)
        if bar is None or bar[0] is None:
            return FillResult(
                status=FillStatus.NOT_FILLED,
                message=f"{intent.code} 在 {trade_date} 无行情（停牌或未上市）",
            )
        open_, _high, low, _close = (to_decimal(v) for v in bar)
        if open_ is None:
            return FillResult(status=FillStatus.NOT_FILLED, message="当日 bar 无开盘价")

        ms = get_market_status(self._conn, intent.code, trade_date)
        up = ms.up_limit_price if ms else None
        down = ms.down_limit_price if ms else None

        if intent.order_type == OrderType.MARKET:
            fill_price = self._slippage_price(open_, intent.side)
            if intent.side == OrderSide.BUY and up is not None and open_ >= up:
                return FillResult(
                    status=FillStatus.NOT_FILLED,
                    message=f"开盘 {open_} 已达涨停 {up}，无法买入",
                )
            if intent.side == OrderSide.SELL and down is not None and open_ <= down:
                return FillResult(
                    status=FillStatus.NOT_FILLED,
                    message=f"开盘 {open_} 已达跌停 {down}，无法卖出",
                )
            return FillResult(status=FillStatus.FILLED, fill_price=fill_price)

        # LIMIT 单
        limit = intent.price
        if limit is None:
            return FillResult(status=FillStatus.NOT_FILLED, message="LIMIT 单缺少限价")
        if intent.side == OrderSide.BUY:
            if low is None or low > limit:
                return FillResult(
                    status=FillStatus.NOT_FILLED,
                    message=f"当日最低 {low} 未触及限价买入 {limit}",
                )
            return FillResult(
                status=FillStatus.FILLED,
                fill_price=_round_cent(min(open_, limit)),  # 开盘即满足按开盘价成交
            )
        # SELL
        high = to_decimal(bar[1])
        if high is None or high < limit:
            return FillResult(
                status=FillStatus.NOT_FILLED,
                message=f"当日最高 {high} 未触及限价卖出 {limit}",
            )
        return FillResult(
            status=FillStatus.FILLED,
            fill_price=_round_cent(max(open_, limit)),
        )


class DryRunGateway(_BarBasedGateway):
    """模拟盘 dry-run 网关：按参考价（当日 open，缺省最近收盘）模拟成交。"""

    def fill(self, intent: OrderIntent, trade_date: date) -> FillResult:
        if intent.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            return FillResult(
                status=FillStatus.UNSUPPORTED,
                message=f"v1 网关不支持 {intent.order_type} 订单",
            )
        bar = self._bar(intent.code, trade_date)
        reference: Decimal | None = to_decimal(bar[0]) if bar else None
        if reference is None:
            row = self._conn.execute(
                """
                SELECT close FROM fact_daily_bar
                WHERE code = ? AND trade_date <= ? AND close IS NOT NULL
                ORDER BY trade_date DESC LIMIT 1
                """,
                [intent.code, trade_date],
            ).fetchone()
            reference = to_decimal(row[0]) if row else None
        if reference is None:
            return FillResult(
                status=FillStatus.NOT_FILLED,
                message=f"{intent.code} 无可用参考价",
            )
        if intent.order_type == OrderType.LIMIT and intent.price is not None:
            # dry-run 限价单：参考价劣于限价则不成交
            if intent.side == OrderSide.BUY and reference > intent.price:
                return FillResult(
                    status=FillStatus.NOT_FILLED,
                    message=f"参考价 {reference} 高于限价买入 {intent.price}",
                )
            if intent.side == OrderSide.SELL and reference < intent.price:
                return FillResult(
                    status=FillStatus.NOT_FILLED,
                    message=f"参考价 {reference} 低于限价卖出 {intent.price}",
                )
            return FillResult(status=FillStatus.FILLED, fill_price=_round_cent(intent.price))
        return FillResult(
            status=FillStatus.FILLED, fill_price=self._slippage_price(reference, intent.side)
        )
