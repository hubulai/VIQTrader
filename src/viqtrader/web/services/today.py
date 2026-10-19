"""今日交易页数据装配（P6）：当日请求/意图/成交/规则校验四区块。

日期口径：请求与意图按提交时刻的自然日（created_at/submitted_at），
成交按 trade_date，规则校验按 checked_at；全部只读。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

import duckdb

from viqtrader import __version__
from viqtrader.config.loader import Config


class ManualOrderError(Exception):
    """手工录单参数非法（账户/标的/数量/价格）。"""


@dataclass(frozen=True, slots=True)
class OrderRequestRow:
    request_id: str
    source_type: str
    account_id: str
    code: str
    side: str
    qty: int
    price: float | None
    status: str
    note: str | None
    created_at: str
    executed_at: str | None


@dataclass(frozen=True, slots=True)
class IntentRow:
    intent_id: str
    account_id: str
    code: str
    side: str
    order_type: str
    qty: int
    price: float | None
    status: str
    rejection_reason: str | None
    filled_price: float | None
    submitted_at: str


@dataclass(frozen=True, slots=True)
class TradeRow:
    trade_id: str
    account_id: str
    code: str
    side: str
    qty: int
    price: float
    amount_cny: float


@dataclass(frozen=True, slots=True)
class RuleCheckRow:
    intent_id: str
    phase: str
    rule_type: str
    message: str | None
    checked_at: str


def list_order_requests(conn: duckdb.DuckDBPyConnection, day: date) -> list[OrderRequestRow]:
    rows = conn.execute(
        """
        SELECT request_id, source_type, account_id, code, side, qty, price, status,
               note, CAST(created_at AS TEXT), CAST(executed_at AS TEXT)
        FROM order_request
        WHERE created_at::DATE = ?
        ORDER BY created_at DESC
        """,
        [day],
    ).fetchall()
    return [
        OrderRequestRow(
            str(r[0]),
            str(r[1]),
            str(r[2]),
            str(r[3]),
            str(r[4]),
            int(r[5]),
            float(r[6]) if r[6] is not None else None,
            str(r[7]),
            str(r[8]) if r[8] is not None else None,
            str(r[9]),
            str(r[10]) if r[10] is not None else None,
        )
        for r in rows
    ]


def list_intents(conn: duckdb.DuckDBPyConnection, day: date) -> list[IntentRow]:
    rows = conn.execute(
        """
        SELECT intent_id, account_id, code, side, order_type, qty, price, status,
               rejection_reason, filled_price, CAST(submitted_at AS TEXT)
        FROM order_intent
        WHERE submitted_at::DATE = ?
        ORDER BY submitted_at DESC
        """,
        [day],
    ).fetchall()
    return [
        IntentRow(
            str(r[0]),
            str(r[1]),
            str(r[2]),
            str(r[3]),
            str(r[4]),
            int(r[5]),
            float(r[6]) if r[6] is not None else None,
            str(r[7]),
            str(r[8]) if r[8] is not None else None,
            float(r[9]) if r[9] is not None else None,
            str(r[10]),
        )
        for r in rows
    ]


def list_trades(conn: duckdb.DuckDBPyConnection, day: date) -> list[TradeRow]:
    rows = conn.execute(
        """
        SELECT trade_id, account_id, code, side, qty, price, amount_cny
        FROM trade_log
        WHERE trade_date = ?
        ORDER BY trade_id
        """,
        [day],
    ).fetchall()
    return [
        TradeRow(str(r[0]), str(r[1]), str(r[2]), str(r[3]), int(r[4]), float(r[5]), float(r[6]))
        for r in rows
    ]


def list_failed_rule_checks(conn: duckdb.DuckDBPyConnection, day: date) -> list[RuleCheckRow]:
    """当日被拒的规则校验（passed=false），按时间倒序。"""
    rows = conn.execute(
        """
        SELECT intent_id, phase, rule_type, message, CAST(checked_at AS TEXT)
        FROM rule_check_log
        WHERE checked_at::DATE = ? AND passed = false
        ORDER BY checked_at DESC
        LIMIT 200
        """,
        [day],
    ).fetchall()
    return [
        RuleCheckRow(
            str(r[0]), str(r[1]), str(r[2]), str(r[3]) if r[3] is not None else None, str(r[4])
        )
        for r in rows
    ]


@dataclass(frozen=True, slots=True)
class AccountOption:
    """录单表单的账户选项（来自 config accounts 节，不依赖执行侧是否已建账）。"""

    account_id: str
    mode: str


def list_accounts(config: Config) -> list[AccountOption]:
    accounts = config.get("accounts", {})
    return [AccountOption(f"acc_{key}", str(key)) for key in accounts]


def create_manual_request(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    *,
    account_id: str,
    code: str,
    side: str,
    order_type: str,
    qty: int,
    price: float | None,
    note: str | None = None,
) -> str:
    """手工录单 → order_request（source_type='manual'，6.3 确认闸统一入口）。

    价值轨买入与投委会拍板解耦（拍板只存档结论），下单一律走本入口。
    非法情形抛 ManualOrderError：账户不在配置、标的未同步、qty 非正、
    LIMIT 单缺正数限价、side/order_type 非法。
    """
    mode_by_account = {f"acc_{key}": str(key) for key in config.get("accounts", {})}
    mode = mode_by_account.get(account_id)
    if mode is None:
        raise ManualOrderError(f"账户不在配置 accounts 节：{account_id}")
    if side not in ("BUY", "SELL"):
        raise ManualOrderError(f"方向仅支持 BUY/SELL，收到 {side!r}")
    if order_type not in ("MARKET", "LIMIT"):
        raise ManualOrderError(f"订单类型仅支持 MARKET/LIMIT，收到 {order_type!r}")
    if qty <= 0:
        raise ManualOrderError(f"数量必须为正整数，收到 {qty}")
    if order_type == "LIMIT" and (price is None or price <= 0):
        raise ManualOrderError("LIMIT 单必须提供正数限价")

    code = code.strip()
    row = conn.execute("SELECT market FROM dim_stock WHERE code = ?", [code]).fetchone()
    if row is None:
        raise ManualOrderError(f"标的未在 dim_stock（请先完成数据同步）：{code!r}")

    request_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO order_request
            (request_id, source_type, source_id, account_id, mode, code, market,
             side, order_type, qty, price, status, note, config_hash, code_version)
        VALUES (?, 'manual', NULL, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
        """,
        [
            request_id,
            account_id,
            mode,
            code,
            str(row[0]),
            side,
            order_type,
            qty,
            price,
            note if note else None,
            str(config.content_hash()),
            __version__,
        ],
    )
    return request_id
