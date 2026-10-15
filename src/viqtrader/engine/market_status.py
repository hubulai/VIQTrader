"""P2 市场状态预计算（设计文档第六节）。

market_status 表是规则引擎 Phase 1 的数据来源：
- 每个交易日盘后（或回测循环内当日 bar 入库后）调用 refresh_market_status
- 涨跌停价 = 前一交易日未复权收盘价 x (1 ± 涨跌幅)，交易所规则四舍五入到分
- 停牌判定：当日无 bar 或 volume=0（数据来源 fact_daily_bar，与设计一致）
- ST 判定：dim_stock.name 含 "ST"（历史回溯精度受限，已确认为默认方案）
- 港股无涨跌停：up/down_limit_price 为 NULL
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import duckdb

from viqtrader.engine.types import MarketStatusView, to_decimal

BOARD_MAIN = "MAIN"
BOARD_CHINEXT = "CHINEXT"
BOARD_STAR = "STAR"
BOARD_BSE = "BSE"
BOARD_HK = "HK"


def infer_board(code: str) -> str:
    """从代码前缀推断板块（设计文档 6.2）。"""
    if code.startswith("68"):
        return BOARD_STAR
    if code.startswith("30"):
        return BOARD_CHINEXT
    if code.startswith(("8", "4")):
        return BOARD_BSE
    if len(code) == 5:
        return BOARD_HK
    return BOARD_MAIN


def _limit_pct(config: dict[str, Any], board: str, is_st: bool) -> Decimal | None:
    """按板块 + ST 状态取涨跌幅配置（trading.price_limit）。"""
    trading = config.get("trading", {}) or {}
    pl = trading.get("price_limit", {}) or {}
    if board == BOARD_HK:
        return None
    if board == BOARD_CHINEXT:
        return to_decimal(pl.get("chinext", 0.20))
    if board == BOARD_STAR:
        return to_decimal(pl.get("star", 0.20))
    if board == BOARD_BSE:
        return to_decimal(pl.get("bse", 0.30))
    # 主板
    return to_decimal(pl.get("main_st", 0.05) if is_st else pl.get("main_normal", 0.10))


def _round_price(price: Decimal) -> Decimal:
    """交易所规则：涨跌停价四舍五入到小数点后 2 位。"""
    return price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _prev_close(conn: duckdb.DuckDBPyConnection, code: str, trade_date: date) -> Decimal | None:
    """前一交易日未复权收盘价。"""
    row = conn.execute(
        """
        SELECT close FROM fact_daily_bar
        WHERE code = ? AND trade_date < ? AND close IS NOT NULL
        ORDER BY trade_date DESC LIMIT 1
        """,
        [code, trade_date],
    ).fetchone()
    return to_decimal(row[0]) if row else None


def _is_suspended(conn: duckdb.DuckDBPyConnection, code: str, trade_date: date) -> bool:
    """当日无 bar 或 volume=0 视为停牌。"""
    row = conn.execute(
        "SELECT volume FROM fact_daily_bar WHERE code = ? AND trade_date = ?",
        [code, trade_date],
    ).fetchone()
    if row is None:
        return True
    return row[0] is None or int(row[0]) == 0


def refresh_market_status(
    conn: duckdb.DuckDBPyConnection,
    config: dict[str, Any],
    trade_date: date,
    *,
    codes: list[str] | None = None,
) -> int:
    """为 trade_date 计算并写入 market_status 快照，顺带回填 dim_stock.board。

    前置条件：trade_date 当日 bar 已入库（盘后 / 回测当日循环内调用）。
    codes 为空时对 dim_stock 全部标的计算。返回写入行数。
    """
    where = ""
    args: list[Any] = []
    if codes:
        where = "WHERE code IN (" + ",".join(["?"] * len(codes)) + ")"
        args = list(codes)
    stocks = conn.execute(
        f"""
        SELECT code, market, name, is_hk_connect,
               (hk_connect_out_date IS NULL) AS in_list
        FROM dim_stock {where}
        """,
        args,
    ).fetchall()

    rows: list[tuple[Any, ...]] = []
    for code, market, name, is_hk_connect, in_list in stocks:
        board = infer_board(code)
        is_st = "ST" in (name or "").upper()
        suspended = _is_suspended(conn, code, trade_date)
        pct = _limit_pct(config, board, is_st)
        up_limit: Decimal | None = None
        down_limit: Decimal | None = None
        if pct is not None and board != BOARD_HK:
            prev = _prev_close(conn, code, trade_date)
            if prev is not None:
                up_limit = _round_price(prev * (Decimal("1") + pct))
                down_limit = _round_price(prev * (Decimal("1") - pct))
        hk_flag = bool(is_hk_connect) and bool(in_list) and market == "HK"
        rows.append((code, trade_date, board, suspended, is_st, up_limit, down_limit, hk_flag))

    conn.execute("BEGIN TRANSACTION")
    try:
        if rows:
            if codes:
                conn.execute(
                    "DELETE FROM market_status WHERE trade_date = ? AND code IN ("
                    + ",".join(["?"] * len(codes))
                    + ")",
                    [trade_date, *codes],
                )
            else:
                conn.execute("DELETE FROM market_status WHERE trade_date = ?", [trade_date])
            conn.executemany(
                """
                INSERT INTO market_status
                    (code, trade_date, board, is_suspended, is_st,
                     up_limit_price, down_limit_price, is_hk_connect)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    if rows:
        # 板块回填 dim_stock.board（幂等，只更新列的值）
        board_rows = [(r[2], r[0]) for r in rows]
        conn.executemany("UPDATE dim_stock SET board = ? WHERE code = ?", board_rows)
    return len(rows)


def get_market_status(
    conn: duckdb.DuckDBPyConnection, code: str, trade_date: date
) -> MarketStatusView | None:
    """读取 market_status 快照为规则引擎视图；无行返回 None。"""
    row = conn.execute(
        """
        SELECT code, trade_date, board, is_suspended, is_st,
               up_limit_price, down_limit_price, is_hk_connect
        FROM market_status WHERE code = ? AND trade_date = ?
        """,
        [code, trade_date],
    ).fetchone()
    if row is None:
        return None
    return MarketStatusView(
        code=row[0],
        trade_date=row[1],
        board=row[2],
        is_suspended=bool(row[3]),
        is_st=bool(row[4]),
        up_limit_price=to_decimal(row[5]),
        down_limit_price=to_decimal(row[6]),
        is_hk_connect=bool(row[7]),
    )
