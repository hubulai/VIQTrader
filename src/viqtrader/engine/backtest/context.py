"""P3.1 策略上下文：前视偏差硬防线（任务 3.2 融入骨架内建）。

决策时点模型（已确认方案 A）：T-1 收盘决策 → T 日成交。
本上下文一切历史数据查询在 SQL 层强制 `trade_date < T` / `ann_date < T`，
策略在机制上拿不到当日及未来数据，不靠自觉。

账户状态（现金/持仓）直接读账本实时值——账本是确定性状态机，
其内容只由已撮合成交驱动，不含未来信息，不受防线限制。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING

import duckdb

from viqtrader.engine.execution.context import get_fx_rate, get_position_view
from viqtrader.engine.types import (
    OrderIntent,
    OrderSide,
    OrderType,
    PositionView,
    TimeInForce,
    to_decimal,
)

if TYPE_CHECKING:
    from viqtrader.engine.ledger.engine import LedgerEngine


@dataclass(frozen=True, slots=True)
class BarView:
    """单日行情视图（close 为未复权价；复权计算用 adj_factor 自行处理）。"""

    code: str
    trade_date: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: int | None
    adj_factor: Decimal | None


@dataclass(frozen=True, slots=True)
class FinancialView:
    """单期财报视图：仅 ann_date 严格早于当日的期次可见。"""

    code: str
    report_period: date
    ann_date: date | None
    revenue: Decimal | None
    net_profit: Decimal | None
    roe: Decimal | None
    roa: Decimal | None
    gross_margin: Decimal | None
    net_margin: Decimal | None
    debt_ratio: Decimal | None
    ocf: Decimal | None
    fcf: Decimal | None
    eps: Decimal | None
    bps: Decimal | None


@dataclass(frozen=True, slots=True)
class ValuationView:
    """单日估值视图（P1.5 自算产物）。"""

    code: str
    trade_date: date
    pe_ttm: Decimal | None
    pb: Decimal | None
    ps_ttm: Decimal | None
    dv_ratio: Decimal | None
    market_cap: Decimal | None
    pe_percentile: Decimal | None


class StrategyContext:
    """策略在回测当日的数据与账户入口（前视偏差防线）。"""

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        ledger: LedgerEngine,
        trade_date: date,
    ) -> None:
        self._conn = conn
        self._ledger = ledger
        self._trade_date = trade_date

    @property
    def trade_date(self) -> date:
        """撮合日 T（决策基于严格早于 T 的数据）。"""
        return self._trade_date

    # ------------------------------------------------------------------
    # 历史数据查询（SQL 层强制 < T，前视防线）
    # ------------------------------------------------------------------

    def bars(self, code: str, n: int = 1) -> list[BarView]:
        """最近 n 条日线，严格早于当日，按日期升序。"""
        if n < 1:
            return []
        rows = self._conn.execute(
            """
            SELECT code, trade_date, open, high, low, close, volume, adj_factor
            FROM (
                SELECT code, trade_date, open, high, low, close, volume, adj_factor
                FROM fact_daily_bar
                WHERE code = ? AND trade_date < ?
                ORDER BY trade_date DESC
                LIMIT ?
            ) AS recent ORDER BY trade_date
            """,
            [code, self._trade_date, n],
        ).fetchall()
        return [
            BarView(
                code=r[0],
                trade_date=r[1],
                open=to_decimal(r[2]),
                high=to_decimal(r[3]),
                low=to_decimal(r[4]),
                close=to_decimal(r[5]),
                volume=int(r[6]) if r[6] is not None else None,
                adj_factor=to_decimal(r[7]),
            )
            for r in rows
        ]

    def latest_bar(self, code: str) -> BarView | None:
        """最近一条日线（严格早于当日）；无历史返回 None。"""
        got = self.bars(code, 1)
        return got[-1] if got else None

    def latest_financial(self, code: str) -> FinancialView | None:
        """最近一期财报：ann_date 严格早于当日才可见（公告次日生效）。"""
        row = self._conn.execute(
            """
            SELECT code, report_period, ann_date, revenue, net_profit, roe, roa,
                   gross_margin, net_margin, debt_ratio, ocf, fcf, eps, bps
            FROM fact_financial
            WHERE code = ? AND ann_date IS NOT NULL AND ann_date < ?
            ORDER BY ann_date DESC, report_period DESC
            LIMIT 1
            """,
            [code, self._trade_date],
        ).fetchone()
        if row is None:
            return None
        return FinancialView(
            code=row[0],
            report_period=row[1],
            ann_date=row[2],
            revenue=to_decimal(row[3]),
            net_profit=to_decimal(row[4]),
            roe=to_decimal(row[5]),
            roa=to_decimal(row[6]),
            gross_margin=to_decimal(row[7]),
            net_margin=to_decimal(row[8]),
            debt_ratio=to_decimal(row[9]),
            ocf=to_decimal(row[10]),
            fcf=to_decimal(row[11]),
            eps=to_decimal(row[12]),
            bps=to_decimal(row[13]),
        )

    def latest_valuation(self, code: str) -> ValuationView | None:
        """最近一条估值（trade_date 严格早于当日）。"""
        row = self._conn.execute(
            """
            SELECT code, trade_date, pe_ttm, pb, ps_ttm, dv_ratio, market_cap, pe_percentile
            FROM fact_valuation
            WHERE code = ? AND trade_date < ?
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            [code, self._trade_date],
        ).fetchone()
        if row is None:
            return None
        return ValuationView(
            code=row[0],
            trade_date=row[1],
            pe_ttm=to_decimal(row[2]),
            pb=to_decimal(row[3]),
            ps_ttm=to_decimal(row[4]),
            dv_ratio=to_decimal(row[5]),
            market_cap=to_decimal(row[6]),
            pe_percentile=to_decimal(row[7]),
        )

    # ------------------------------------------------------------------
    # 账户状态（账本实时值，无未来信息）
    # ------------------------------------------------------------------

    def cash(self, account_id: str) -> Decimal:
        """本轨可用现金（CNY）。"""
        return self._ledger.get_available_cash(account_id)

    def position(self, account_id: str, code: str) -> PositionView | None:
        """单标的持仓视图；无持仓返回 None。"""
        return get_position_view(self._conn, account_id, code)

    def positions(self, account_id: str) -> list[PositionView]:
        """本轨全部持仓。"""
        rows = self._conn.execute(
            "SELECT code FROM position WHERE account_id = ? AND qty > 0 ORDER BY code",
            [account_id],
        ).fetchall()
        out: list[PositionView] = []
        for (code,) in rows:
            view = get_position_view(self._conn, account_id, code)
            if view is not None:
                out.append(view)
        return out

    def fx_rate(self, code: str, pair: str = "HKD/CNY") -> Decimal:
        """原币→CNY 汇率（A 股恒 1；港股取 ≤当日最近已知汇率）。"""
        return get_fx_rate(self._conn, code, self._trade_date, pair)

    # ------------------------------------------------------------------
    # Intent 便捷构造（决策时间戳 = 回测当日 09:30，留痕可回放）
    # ------------------------------------------------------------------

    def make_intent(
        self,
        account_id: str,
        mode: str,
        code: str,
        market: str,
        side: OrderSide,
        qty: int,
        *,
        order_type: OrderType = OrderType.MARKET,
        price: Decimal | None = None,
        thesis_id: str | None = None,
        strategy_id: str | None = None,
        time_in_force: TimeInForce = TimeInForce.GTC,
    ) -> OrderIntent:
        """构造订单意图；submitted_at 固定为回测当日 09:30（非真实墙钟）。"""
        return OrderIntent(
            intent_id=str(uuid.uuid4()),
            account_id=account_id,
            mode=mode,
            code=code,
            market=market,
            side=side,
            order_type=order_type,
            qty=qty,
            price=price,
            thesis_id=thesis_id,
            strategy_id=strategy_id,
            submitted_at=datetime.combine(self._trade_date, time(9, 30)),
            time_in_force=time_in_force,
        )
