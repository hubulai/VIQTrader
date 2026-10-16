"""P3.6 策略级净值重放：按 strategy_id 归集成交，重放现金 + 持仓市值的逐日净值。

数据源：trade_log JOIN order_intent（strategy_id 归因）。
口径（与账本引擎一致）：
- 基准现金 = 策略当前配额 = capital_pct x 波段子账本账户资本额度（account.capital_quota）；
  策略仅动用配额的一部分时，未动用部分视为闲置现金并入净值（分阶段横向可比）。
- 现金流：BUY 减 |amount_cny|（成交额+费用，trade_log.amount_cny 已为负值），SELL 反之。
- 持仓市值：持仓数量 x 当日收盘 x 汇率；策略单标的即波段轨单标的，允许负现金兜底。
- 首日（首次成交前）净值 = 基准现金，序列为单调填充。

引擎层纪律：纯代码零 LLM，只读不改账本；金额全程 float 输出（KPI 判定用，非记账口径）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import duckdb


@dataclass(frozen=True, slots=True)
class StrategyTrade:
    """单条策略归属成交（净值重放输入）。"""

    trade_date: date
    code: str
    side: str  # BUY / SELL
    qty: int
    amount_cny: float  # 现金流（BUY 负 / SELL 正，账本口径）


def strategy_base_cash(
    conn: duckdb.DuckDBPyConnection, capital_pct: float, account_id: str = "acc_swing"
) -> float:
    """策略基准现金 = capital_pct x 子账本配额（account 不存在时按 0）。"""
    row = conn.execute(
        "SELECT capital_quota FROM account WHERE account_id = ?", [account_id]
    ).fetchone()
    quota = float(row[0]) if row else 0.0
    return quota * capital_pct


def strategy_trades(
    conn: duckdb.DuckDBPyConnection,
    strategy_id: str,
    start: date | None = None,
    end: date | None = None,
) -> list[StrategyTrade]:
    """策略归属成交（按日期升序）；start/end 闭区间过滤。"""
    sql = """
        SELECT t.trade_date, t.code, t.side, t.qty, t.amount_cny
        FROM trade_log t
        JOIN order_intent oi ON t.intent_id = oi.intent_id
        WHERE oi.strategy_id = ?
    """
    params: list[object] = [strategy_id]
    if start is not None:
        sql += " AND t.trade_date >= ?"
        params.append(start)
    if end is not None:
        sql += " AND t.trade_date <= ?"
        params.append(end)
    sql += " ORDER BY t.trade_date, t.trade_id"
    rows = conn.execute(sql, params).fetchall()
    return [
        StrategyTrade(
            trade_date=r[0],
            code=r[1],
            side=r[2],
            qty=int(r[3]),
            amount_cny=float(r[4]),
        )
        for r in rows
    ]


def replay_strategy_equity(
    conn: duckdb.DuckDBPyConnection,
    strategy_id: str,
    start: date,
    end: date,
    base_cash: float,
) -> list[tuple[date, float]]:
    """重放策略逐日净值：[start, end] 内每个有行情的交易日一行。

    逐日语义：按当日及之前全部成交推进现金/持仓，市值用当日收盘价
    （无当日行情时用最近已知收盘，与账本引擎口径一致）。
    """
    days_rows = conn.execute(
        """
        SELECT DISTINCT trade_date FROM fact_daily_bar
        WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date
        """,
        [start, end],
    ).fetchall()
    days = [r[0] for r in days_rows]
    if not days:
        return []

    trades = strategy_trades(conn, strategy_id, start=days[0], end=days[-1])
    trades_by_day: dict[date, list[StrategyTrade]] = {}
    for t in trades:
        trades_by_day.setdefault(t.trade_date, []).append(t)

    cash = Decimal(str(base_cash))
    holdings: dict[str, int] = {}
    series: list[tuple[date, float]] = []
    for day in days:
        for t in trades_by_day.get(day, []):
            cash += Decimal(str(t.amount_cny))
            if t.side == "BUY":
                holdings[t.code] = holdings.get(t.code, 0) + t.qty
            else:
                remaining = holdings.get(t.code, 0) - t.qty
                if remaining > 0:
                    holdings[t.code] = remaining
                else:
                    holdings.pop(t.code, None)

        market_value = Decimal("0")
        for code, qty in holdings.items():
            close = _latest_close(conn, code, day)
            if close is None:
                continue
            fx = _fx_rate(conn, code, day)
            market_value += close * qty * fx
        series.append((day, float(cash + market_value)))
    return series


def _latest_close(conn: duckdb.DuckDBPyConnection, code: str, as_of: date) -> Decimal | None:
    row = conn.execute(
        """
        SELECT close FROM fact_daily_bar
        WHERE code = ? AND trade_date <= ? AND close IS NOT NULL
        ORDER BY trade_date DESC LIMIT 1
        """,
        [code, as_of],
    ).fetchone()
    return Decimal(str(row[0])) if row and row[0] is not None else None


def _fx_rate(conn: duckdb.DuckDBPyConnection, code: str, as_of: date) -> Decimal:
    """A 股恒 1；港股取 ≤as_of 最近 HKD/CNY（无数据容忍为 1，与账本一致）。"""
    if len(code) != 5:
        return Decimal("1")
    row = conn.execute(
        """
        SELECT rate FROM fact_fx_rate
        WHERE pair = 'HKD/CNY' AND trade_date <= ?
        ORDER BY trade_date DESC LIMIT 1
        """,
        [as_of],
    ).fetchone()
    return Decimal(str(row[0])) if row else Decimal("1")


__all__ = [
    "StrategyTrade",
    "replay_strategy_equity",
    "strategy_base_cash",
    "strategy_trades",
]
