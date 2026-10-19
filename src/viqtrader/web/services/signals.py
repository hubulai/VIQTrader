"""信号确认闸服务（P6，6.3 红线核心）。

纪律：本模块是下单意图进入执行管线的唯一人工入口——
- 确认 = 同事务 UPDATE swing_signal.status='confirmed' + INSERT order_request；
- web 层绝不写 order_intent（那是 ExecutionService 的专属职责），
  未确认信号永远不会出现在 order_request，更不可能成交（v1 无全自动路径）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import duckdb

from viqtrader import __version__
from viqtrader.config.loader import Config


class SignalActionError(Exception):
    """信号状态非法（不存在/已处理）或参数非法。"""


@dataclass(frozen=True, slots=True)
class SignalRow:
    signal_id: str
    code: str
    name: str
    market: str
    signal_date: str
    signal_type: str | None
    pattern_desc: str | None
    entry_ref: float | None
    stop_ref: float | None
    target_ref: float | None
    interpretation: str | None
    status: str
    created_at: str


def list_signals(
    conn: duckdb.DuckDBPyConnection,
    *,
    status: str | None = "pending",
    limit: int = 100,
) -> list[SignalRow]:
    where = "WHERE s.status = ?" if status else ""
    params: list[object] = [status] if status else []
    rows = conn.execute(
        f"""
        SELECT s.signal_id, s.code, COALESCE(d.name, s.name) AS name, s.market,
               CAST(s.signal_date AS TEXT), s.signal_type, s.pattern_desc,
               s.entry_ref, s.stop_ref, s.target_ref, s.interpretation, s.status,
               CAST(s.created_at AS TEXT)
        FROM swing_signal s
        LEFT JOIN dim_stock d ON d.code = s.code
        {where}
        ORDER BY s.signal_date DESC, s.code
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    return [
        SignalRow(
            str(r[0]),
            str(r[1]),
            str(r[2]),
            str(r[3]),
            str(r[4]),
            str(r[5]) if r[5] is not None else None,
            str(r[6]) if r[6] is not None else None,
            float(r[7]) if r[7] is not None else None,
            float(r[8]) if r[8] is not None else None,
            float(r[9]) if r[9] is not None else None,
            str(r[10]) if r[10] is not None else None,
            str(r[11]),
            str(r[12]),
        )
        for r in rows
    ]


def confirm_signal(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    signal_id: str,
    *,
    qty: int,
    price: float | None,
    order_type: str = "LIMIT",
    account_id: str = "acc_swing",
) -> str:
    """确认信号 → order_request（同事务）。返回 request_id。

    非法情形抛 SignalActionError：信号不存在/非 pending、qty 非正、
    LIMIT 单缺 price、order_type 非法。
    """
    if qty <= 0:
        raise SignalActionError(f"数量必须为正整数，收到 {qty}")
    if order_type not in ("MARKET", "LIMIT"):
        raise SignalActionError(f"订单类型仅支持 MARKET/LIMIT，收到 {order_type!r}")
    if order_type == "LIMIT" and (price is None or price <= 0):
        raise SignalActionError("LIMIT 单必须提供正数限价")

    conn.execute("BEGIN TRANSACTION")
    try:
        row = conn.execute(
            "SELECT code, market, status FROM swing_signal WHERE signal_id = ?",
            [signal_id],
        ).fetchone()
        if row is None:
            raise SignalActionError(f"信号不存在：{signal_id}")
        if str(row[2]) != "pending":
            raise SignalActionError(f"信号已处理（status={row[2]}），不可重复确认")

        conn.execute(
            "UPDATE swing_signal SET status = 'confirmed' WHERE signal_id = ?",
            [signal_id],
        )
        request_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO order_request
                (request_id, source_type, source_id, account_id, mode, code, market,
                 side, order_type, qty, price, status, config_hash, code_version)
            VALUES (?, 'swing_signal', ?, ?, 'swing', ?, ?, 'BUY', ?, ?, ?, 'pending', ?, ?)
            """,
            [
                request_id,
                signal_id,
                account_id,
                str(row[0]),
                str(row[1]),
                order_type,
                qty,
                price,
                str(config.content_hash()),
                __version__,
            ],
        )
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return request_id


def reject_signal(conn: duckdb.DuckDBPyConnection, signal_id: str) -> None:
    """拒绝信号（pending → rejected）。"""
    row = conn.execute(
        "SELECT status FROM swing_signal WHERE signal_id = ?", [signal_id]
    ).fetchone()
    if row is None:
        raise SignalActionError(f"信号不存在：{signal_id}")
    if str(row[0]) != "pending":
        raise SignalActionError(f"信号已处理（status={row[0]}）")
    conn.execute(
        "UPDATE swing_signal SET status = 'rejected' WHERE signal_id = ?",
        [signal_id],
    )
