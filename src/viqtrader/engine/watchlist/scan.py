"""狩猎清单盯价扫描（P5.2）。

批量扫描 watching 标的，判定 triggered / expired，更新 watchlist 表，
返回变更报告供告警与状态文件使用。

判定优先级：expired（TTL 或新财报）> triggered。研究结论已作废时即使
价格到位也不触发，强制重新投委会复核。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import duckdb

from viqtrader.config.loader import Config

from .state import (
    ScanVerdict,
    adjust_target_price,
    has_new_report,
    is_expired,
    is_triggered,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WatchScanReport:
    """盯价扫描结果。"""

    today: date
    latest_trade_date: date | None
    watched: int
    triggered: list[ScanVerdict] = field(default_factory=list)
    expired: list[ScanVerdict] = field(default_factory=list)
    unchanged: int = 0
    skipped_no_market: list[str] = field(default_factory=list)  # watch_id 列表


def scan_watchlist(
    conn: duckdb.DuckDBPyConnection,
    config: Config,
    today: date,
) -> WatchScanReport:
    """扫描 watching 标的并更新状态。幂等：已 triggered/expired 不重处理。"""
    ttl_days = int(config.get("value_track.watchlist.research_ttl_days", 90))
    reverify_on_new_report = bool(config.get("value_track.watchlist.reverify_on_new_report", True))

    row = conn.execute("SELECT max(trade_date) FROM fact_daily_bar").fetchone()
    latest_trade_date = row[0] if row else None
    if latest_trade_date is None:
        return WatchScanReport(today, None, 0)

    rows = conn.execute(
        """
        SELECT w.watch_id, w.code, w.target_buy_price, w.last_review_date,
               CAST(w.created_at AS DATE) AS created_date,
               b_latest.close AS latest_close, b_latest.adj_factor AS latest_adj,
               b_target.adj_factor AS target_adj,
               f.latest_ann_date
        FROM watchlist w
        LEFT JOIN fact_daily_bar b_latest
          ON b_latest.code = w.code AND b_latest.trade_date = ?
        LEFT JOIN LATERAL (
            SELECT adj_factor FROM fact_daily_bar
            WHERE code = w.code AND trade_date <= CAST(w.created_at AS DATE)
            ORDER BY trade_date DESC LIMIT 1
        ) b_target ON true
        LEFT JOIN (
            SELECT code, max(ann_date) AS latest_ann_date
            FROM fact_financial
            WHERE ann_date <= ?
            GROUP BY code
        ) f ON f.code = w.code
        WHERE w.status = 'watching'
        ORDER BY w.watch_id
        """,
        [latest_trade_date, today],
    ).fetchall()

    triggered: list[ScanVerdict] = []
    expired: list[ScanVerdict] = []
    skipped: list[str] = []
    unchanged = 0

    for r in rows:
        (
            watch_id,
            code,
            target_buy_price,
            last_review_date,
            _created_date,
            latest_close,
            latest_adj,
            target_adj,
            latest_ann_date,
        ) = r

        if latest_close is None:
            skipped.append(watch_id)
            continue

        # 1. 保质期过期
        if is_expired(last_review_date, today, ttl_days):
            expired.append(
                ScanVerdict(
                    watch_id,
                    code,
                    "expired",
                    f"研究保质期超 {ttl_days} 天（last_review={last_review_date}）",
                    latest_close=latest_close,
                )
            )
            continue

        # 2. 新财报自动复核（强制过期）
        if reverify_on_new_report and has_new_report(last_review_date, latest_ann_date):
            expired.append(
                ScanVerdict(
                    watch_id,
                    code,
                    "expired",
                    f"有新财报披露（ann_date={latest_ann_date}），研究结论需复核",
                    latest_close=latest_close,
                )
            )
            continue

        # 3. 价格触发
        adjusted_target = adjust_target_price(float(target_buy_price), target_adj, latest_adj)
        if is_triggered(float(latest_close), adjusted_target):
            triggered.append(
                ScanVerdict(
                    watch_id,
                    code,
                    "triggered",
                    f"最新价 {latest_close:.2f} ≤ 目标价 {adjusted_target:.2f}"
                    if adjusted_target is not None
                    else f"最新价 {latest_close:.2f} 触及目标",
                    latest_close=float(latest_close),
                    adjusted_target=adjusted_target,
                )
            )
            continue

        unchanged += 1

    # 批量更新状态（显式 commit，不用 with conn 以免关闭共享连接）
    if expired or triggered:
        sql_expired = (
            "UPDATE watchlist SET status = 'expired' WHERE watch_id = ? AND status = 'watching'"
        )
        sql_triggered = (
            "UPDATE watchlist SET status = 'triggered' WHERE watch_id = ? AND status = 'watching'"
        )
        try:
            for v in expired:
                conn.execute(sql_expired, [v.watch_id])
            for v in triggered:
                conn.execute(sql_triggered, [v.watch_id])
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return WatchScanReport(
        today=today,
        latest_trade_date=latest_trade_date,
        watched=len(rows),
        triggered=triggered,
        expired=expired,
        unchanged=unchanged,
        skipped_no_market=skipped,
    )


__all__ = ["WatchScanReport", "scan_watchlist"]
