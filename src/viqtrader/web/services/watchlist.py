"""狩猎清单页数据装配（P6）：列表 + 最新价距目标价差 + 保质期倒计时 + CRUD。

口径与 scripts/watchlist.py CLI 一致：gap=(latest-target)/target，
ttl=research_ttl_days - (today - last_review_date).days。
写操作（add/expire/reverify）逻辑照搬 CLI，保证两个入口行为一致。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

import duckdb


class WatchlistError(Exception):
    """清单操作非法：条目不存在 / 状态非法 / 参数非法。"""


@dataclass(frozen=True, slots=True)
class WatchRow:
    watch_id: str
    code: str
    name: str
    status: str
    target_buy_price: float
    latest_close: float | None
    gap_pct: float | None
    last_review_date: str | None
    ttl_days_left: int | None
    research_summary: str | None
    committee_session_id: str | None


def list_watchlist(
    conn: duckdb.DuckDBPyConnection,
    *,
    today: date,
    ttl_days: int = 90,
    status: str | None = None,
) -> list[WatchRow]:
    where = "WHERE w.status = ?" if status else ""
    params: list[object] = [status] if status else []
    rows = conn.execute(
        f"""
        SELECT w.watch_id, w.code, COALESCE(d.name, '') AS name, w.status,
               w.target_buy_price, b.close AS latest_close,
               CAST(w.last_review_date AS TEXT), w.research_summary,
               w.committee_session_id
        FROM watchlist w
        LEFT JOIN dim_stock d ON d.code = w.code
        LEFT JOIN (
            SELECT code, close FROM fact_daily_bar
            WHERE trade_date = (SELECT max(trade_date) FROM fact_daily_bar)
        ) b ON b.code = w.code
        {where}
        ORDER BY w.status, w.code
        """,
        params,
    ).fetchall()
    out: list[WatchRow] = []
    for r in rows:
        target = float(r[4])
        latest = float(r[5]) if r[5] is not None else None
        gap = ((latest - target) / target) if latest is not None and target else None
        ttl_left: int | None = None
        if r[6] is not None:
            last_review = date.fromisoformat(str(r[6])[:10])
            ttl_left = ttl_days - (today - last_review).days
        out.append(
            WatchRow(
                str(r[0]),
                str(r[1]),
                str(r[2]),
                str(r[3]),
                target,
                latest,
                gap,
                str(r[6]) if r[6] is not None else None,
                ttl_left,
                str(r[7]) if r[7] is not None else None,
                str(r[8]) if r[8] is not None else None,
            )
        )
    return out


def add_watch(
    conn: duckdb.DuckDBPyConnection,
    *,
    code: str,
    target: float,
    committee_session_id: str | None = None,
    summary: str | None = None,
    thesis_id: str | None = None,
    required_discount: float | None = None,
) -> str:
    """加入狩猎清单（status=watching，last_review_date=今天）。返回 watch_id。"""
    code = code.strip()
    if not code:
        raise WatchlistError("代码不能为空")
    if target <= 0:
        raise WatchlistError(f"目标价必须为正数，收到 {target}")
    if required_discount is not None and not (0 < required_discount < 1):
        raise WatchlistError(f"要求折价率必须在 (0,1) 区间，收到 {required_discount}")
    watch_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO watchlist
        (watch_id, code, committee_session_id, target_buy_price, required_discount,
         status, research_summary, last_review_date, thesis_id)
        VALUES (?, ?, ?, ?, ?, 'watching', ?, CURRENT_DATE, ?)
        """,
        [
            watch_id,
            code,
            committee_session_id or None,
            target,
            required_discount,
            summary or None,
            thesis_id or None,
        ],
    )
    return watch_id


def expire_watch(conn: duckdb.DuckDBPyConnection, watch_id: str) -> None:
    """人工标记 thesis 证伪 → expired。"""
    row = conn.execute("SELECT status FROM watchlist WHERE watch_id = ?", [watch_id]).fetchone()
    if row is None:
        raise WatchlistError(f"清单条目不存在：{watch_id}")
    if str(row[0]) == "expired":
        raise WatchlistError("该条目已是 expired 状态")
    conn.execute("UPDATE watchlist SET status = 'expired' WHERE watch_id = ?", [watch_id])


def reverify_watch(
    conn: duckdb.DuckDBPyConnection, watch_id: str, *, target: float | None = None
) -> None:
    """重新投委会后恢复 watching，刷新 last_review_date（可选更新目标价）。"""
    if target is not None and target <= 0:
        raise WatchlistError(f"目标价必须为正数，收到 {target}")
    exists = conn.execute("SELECT 1 FROM watchlist WHERE watch_id = ?", [watch_id]).fetchone()
    if exists is None:
        raise WatchlistError(f"清单条目不存在：{watch_id}")
    conn.execute(
        """
        UPDATE watchlist
        SET status = 'watching', last_review_date = CURRENT_DATE,
            target_buy_price = COALESCE(?, target_buy_price)
        WHERE watch_id = ?
        """,
        [target, watch_id],
    )
