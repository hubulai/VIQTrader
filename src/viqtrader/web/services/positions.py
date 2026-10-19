"""持仓页数据装配（P6）：持仓明细（含浮盈）+ 成交流水 + 账户盈亏汇总。

市值/浮盈口径：A 股 qty 乘最新收盘价；港股通持仓按最新 HKD/CNY 汇率折算。
浮动盈亏 = 市值 - qty 乘 avg_cost_cny（未扣卖出费用，展示口径）。
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb


@dataclass(frozen=True, slots=True)
class PositionRow:
    account_id: str
    mode: str
    code: str
    name: str
    qty: int
    avg_cost_cny: float
    close: float | None
    market_value_cny: float | None
    pnl_cny: float | None
    pnl_pct: float | None
    open_date: str


@dataclass(frozen=True, slots=True)
class TradeFlowRow:
    trade_date: str
    account_id: str
    code: str
    name: str
    side: str
    qty: int
    price: float
    amount_cny: float
    fees_cny: float


@dataclass(frozen=True, slots=True)
class AccountPnl:
    account_id: str
    buy_amount_cny: float
    sell_amount_cny: float
    fees_cny: float
    float_pnl_cny: float | None


_POSITIONS_SQL = """
WITH latest_bar AS (
    SELECT code, close FROM fact_daily_bar
    WHERE trade_date = (SELECT max(trade_date) FROM fact_daily_bar)
),
latest_fx AS (
    SELECT rate FROM fact_fx_rate WHERE pair = 'HKD/CNY'
    ORDER BY trade_date DESC LIMIT 1
)
SELECT p.account_id, p.mode, p.code, COALESCE(d.name, '') AS name,
       p.qty, p.avg_cost_cny, lb.close,
       CASE WHEN lb.close IS NULL THEN NULL
            WHEN length(p.code) = 5 THEN p.qty * lb.close * COALESCE(fx.rate, 1)
            ELSE p.qty * lb.close END AS market_value,
       CAST(p.open_date AS TEXT) AS open_date
FROM position p
LEFT JOIN dim_stock d ON d.code = p.code
LEFT JOIN latest_bar lb ON lb.code = p.code
LEFT JOIN latest_fx fx ON true
ORDER BY p.account_id, p.open_date
"""


def list_positions(conn: duckdb.DuckDBPyConnection) -> list[PositionRow]:
    rows = conn.execute(_POSITIONS_SQL).fetchall()
    out: list[PositionRow] = []
    for r in rows:
        qty = int(r[4])
        avg_cost = float(r[5])
        mv = float(r[7]) if r[7] is not None else None
        pnl = (mv - qty * avg_cost) if mv is not None else None
        pnl_pct = (pnl / (qty * avg_cost)) if pnl is not None and qty * avg_cost else None
        out.append(
            PositionRow(
                account_id=str(r[0]),
                mode=str(r[1]),
                code=str(r[2]),
                name=str(r[3]),
                qty=qty,
                avg_cost_cny=avg_cost,
                close=float(r[6]) if r[6] is not None else None,
                market_value_cny=mv,
                pnl_cny=pnl,
                pnl_pct=pnl_pct,
                open_date=str(r[8]),
            )
        )
    return out


def list_recent_trades(conn: duckdb.DuckDBPyConnection, limit: int = 200) -> list[TradeFlowRow]:
    rows = conn.execute(
        """
        SELECT CAST(t.trade_date AS TEXT), t.account_id, t.code,
               COALESCE(d.name, '') AS name, t.side, t.qty, t.price, t.amount_cny,
               t.commission + t.stamp_tax + t.transfer_fee AS fees
        FROM trade_log t
        LEFT JOIN dim_stock d ON d.code = t.code
        ORDER BY t.trade_date DESC, t.trade_id DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    return [
        TradeFlowRow(
            str(r[0]),
            str(r[1]),
            str(r[2]),
            str(r[3]),
            str(r[4]),
            int(r[5]),
            float(r[6]),
            float(r[7]),
            float(r[8]),
        )
        for r in rows
    ]


def summarize_pnl(conn: duckdb.DuckDBPyConnection) -> list[AccountPnl]:
    """按账户汇总：累计买入/卖出/费用（trade_log）+ 当前浮动盈亏。"""
    rows = conn.execute(
        """
        SELECT account_id,
               sum(CASE WHEN side = 'BUY' THEN amount_cny ELSE 0 END),
               sum(CASE WHEN side = 'SELL' THEN amount_cny ELSE 0 END),
               sum(commission + stamp_tax + transfer_fee)
        FROM trade_log
        GROUP BY account_id
        ORDER BY account_id
        """
    ).fetchall()
    float_by_account: dict[str, float] = {}
    for p in list_positions(conn):
        if p.pnl_cny is not None:
            float_by_account[p.account_id] = float_by_account.get(p.account_id, 0.0) + p.pnl_cny
    return [
        AccountPnl(
            account_id=str(r[0]),
            buy_amount_cny=float(r[1]),
            sell_amount_cny=float(r[2]),
            fees_cny=float(r[3]),
            float_pnl_cny=float_by_account.get(str(r[0])),
        )
        for r in rows
    ]
