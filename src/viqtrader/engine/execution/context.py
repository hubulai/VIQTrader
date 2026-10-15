"""P2 RuleContext 构建：从 DB 汇总规则校验所需的账户与市场状态。

所有市值/现金口径统一 CNY；港股持仓按最近汇率折算。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import duckdb

from viqtrader.engine.market_status import get_market_status
from viqtrader.engine.types import (
    MarketStatusView,
    OrderIntent,
    PositionView,
    RuleContext,
    to_decimal,
)


def get_latest_close(
    conn: duckdb.DuckDBPyConnection, code: str, as_of: date, *, same_day_only: bool = False
) -> Decimal | None:
    """最近收盘价；same_day_only=True 时仅取当日（无则 None）。"""
    op = "=" if same_day_only else "<="
    row = conn.execute(
        f"""
        SELECT close FROM fact_daily_bar
        WHERE code = ? AND trade_date {op} ? AND close IS NOT NULL
        ORDER BY trade_date DESC LIMIT 1
        """,
        [code, as_of],
    ).fetchone()
    return to_decimal(row[0]) if row else None


def get_fx_rate(
    conn: duckdb.DuckDBPyConnection, code: str, as_of: date, pair: str = "HKD/CNY"
) -> Decimal:
    """原币→CNY：A 股（6 位代码）恒 1；港股取 ≤as_of 最近汇率（无数据容忍为 1）。"""
    if len(code) != 5:
        return Decimal("1")
    row = conn.execute(
        """
        SELECT rate FROM fact_fx_rate
        WHERE pair = ? AND trade_date <= ?
        ORDER BY trade_date DESC LIMIT 1
        """,
        [pair, as_of],
    ).fetchone()
    if row is None:
        return Decimal("1")
    return to_decimal(row[0], Decimal("1"))


def get_position_view(
    conn: duckdb.DuckDBPyConnection, account_id: str, code: str
) -> PositionView | None:
    """读取 (account, code) 持仓视图；无行返回 None。"""
    row = conn.execute(
        """
        SELECT account_id, mode, code, qty, COALESCE(frozen_qty, 0), avg_cost, avg_cost_cny
        FROM position WHERE account_id = ? AND code = ?
        """,
        [account_id, code],
    ).fetchone()
    if row is None:
        return None
    return PositionView(
        account_id=row[0],
        mode=row[1],
        code=row[2],
        qty=int(row[3]),
        frozen_qty=int(row[4]),
        avg_cost=to_decimal(row[5], Decimal("0")),
        avg_cost_cny=to_decimal(row[6], Decimal("0")),
    )


def build_rule_context(
    conn: duckdb.DuckDBPyConnection,
    config: Any,
    intent: OrderIntent,
    trade_date: date,
    *,
    ledger: Any = None,
    is_trading_day: bool = True,
) -> RuleContext:
    """构建规则校验上下文。

    Args:
        ledger: LedgerEngine（取现金/轨值）；None 时按初始配额近似（仅测试用）。
    """
    cfg = config.as_dict()
    ms: MarketStatusView | None = get_market_status(conn, intent.code, trade_date)
    position = get_position_view(conn, intent.account_id, intent.code)
    fx_pair = str(cfg.get("data", {}).get("fx_pair", "HKD/CNY"))
    fx = get_fx_rate(conn, intent.code, trade_date, fx_pair)

    # 当前价：当日 open（执行时点），缺省最近收盘
    current_price: Decimal | None = None
    row = conn.execute(
        "SELECT open FROM fact_daily_bar WHERE code = ? AND trade_date = ?",
        [intent.code, trade_date],
    ).fetchone()
    if row and row[0] is not None:
        current_price = to_decimal(row[0])
    else:
        current_price = get_latest_close(conn, intent.code, trade_date)

    # 账户集合（双轨总资产需要两个账户）
    account_id = intent.account_id
    accounts = conn.execute("SELECT account_id, mode, target_ratio FROM account").fetchall()

    if ledger is not None:
        cash = ledger.get_available_cash(account_id)
        market_value = _market_value(conn, account_id, trade_date, fx)
        total = Decimal("0")
        for acc_id, _m, _r in accounts:
            total += ledger.get_available_cash(acc_id) + _market_value(conn, acc_id, trade_date, fx)
    else:
        cash = _quota_of(conn, account_id)
        market_value = Decimal("0")
        total = Decimal("0")
        for acc_id, _m, _r in accounts:
            total += _quota_of(conn, acc_id)

    target_ratio = _target_ratio_of(conn, account_id)
    account_mode = _mode_of(conn, account_id)
    return RuleContext(
        trade_date=trade_date,
        market_status=ms,
        position=position,
        track_cash=cash,
        track_market_value=market_value,
        total_portfolio_value=total,
        config=cfg,
        is_trading_day=is_trading_day,
        current_price=current_price,
        account_mode=account_mode,
        target_ratio=target_ratio,
        fx_rate=fx,
    )


def _market_value(
    conn: duckdb.DuckDBPyConnection, account_id: str, as_of: date, _default_fx: Decimal
) -> Decimal:
    rows = conn.execute(
        "SELECT code, qty FROM position WHERE account_id = ? AND qty > 0", [account_id]
    ).fetchall()
    total = Decimal("0")
    for code, qty in rows:
        close = get_latest_close(conn, code, as_of)
        if close is None:
            continue
        fx = get_fx_rate(conn, code, as_of)
        total += close * qty * fx
    return total


def _quota_of(conn: duckdb.DuckDBPyConnection, account_id: str) -> Decimal:
    row = conn.execute(
        "SELECT capital_quota FROM account WHERE account_id = ?", [account_id]
    ).fetchone()
    if row is None:
        return Decimal("0")
    return to_decimal(row[0], Decimal("0"))


def _target_ratio_of(conn: duckdb.DuckDBPyConnection, account_id: str) -> Decimal:
    row = conn.execute(
        "SELECT target_ratio FROM account WHERE account_id = ?", [account_id]
    ).fetchone()
    if row is None:
        return Decimal("0")
    return to_decimal(row[0], Decimal("0"))


def _mode_of(conn: duckdb.DuckDBPyConnection, account_id: str) -> str:
    row = conn.execute("SELECT mode FROM account WHERE account_id = ?", [account_id]).fetchone()
    return str(row[0]) if row else ""
