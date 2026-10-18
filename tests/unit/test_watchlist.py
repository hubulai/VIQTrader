"""狩猎清单状态机单测（P5.2）：纯函数 + 盯价扫描内存库四态 + CLI 冒烟。"""

from __future__ import annotations

from datetime import date

import duckdb
import pytest

from viqtrader.engine.watchlist.scan import scan_watchlist
from viqtrader.engine.watchlist.state import (
    adjust_target_price,
    has_new_report,
    is_expired,
    is_triggered,
)

# ---------------------------------------------------------------------------
# state.py 纯函数
# ---------------------------------------------------------------------------


def test_is_expired_boundary():
    today = date(2026, 10, 18)
    # 刚好 90 天：仍有效
    assert is_expired(date(2026, 7, 20), today, 90) is False
    # 超 1 天：过期
    assert is_expired(date(2026, 7, 19), today, 90) is True
    # last_review 缺失：过期
    assert is_expired(None, today, 90) is True


def test_adjust_target_price_no_dividend():
    """无分红时复权因子恒为 1，目标价不变。"""
    assert adjust_target_price(1500.0, 1.0, 1.0) == 1500.0


def test_adjust_target_price_with_dividend():
    """除息后最新复权因子变小，目标价同比下移。"""
    # target_day adj=1.0, 最新 adj=0.995（每股分 5 元，原 1000 元股）
    adj = adjust_target_price(1500.0, 1.0, 0.995)
    assert adj == pytest.approx(1492.5)


def test_adjust_target_price_missing_factor():
    assert adjust_target_price(1500.0, None, 1.0) is None
    assert adjust_target_price(1500.0, 1.0, None) is None


def test_is_triggered():
    assert is_triggered(1490.0, 1500.0) is True  # 低于目标
    assert is_triggered(1500.0, 1500.0) is True  # 等于目标
    assert is_triggered(1510.0, 1500.0) is False  # 高于目标
    assert is_triggered(None, 1500.0) is False


def test_has_new_report():
    assert has_new_report(date(2026, 7, 1), date(2026, 8, 15)) is True
    assert has_new_report(date(2026, 7, 1), date(2026, 6, 30)) is False
    assert has_new_report(None, date(2026, 8, 15)) is True
    assert has_new_report(date(2026, 7, 1), None) is False


# ---------------------------------------------------------------------------
# scan.py 内存库四态
# ---------------------------------------------------------------------------


def _seed(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("INSERT INTO dim_stock (code, market, name) VALUES ('600519','SH','贵州茅台')")
    conn.execute("INSERT INTO dim_stock (code, market, name) VALUES ('000001','SZ','平安银行')")
    # 两个交易日：10-17（target_day 基准）和 10-18（最新）
    conn.execute(
        "INSERT INTO fact_daily_bar "
        "(code, trade_date, open, high, low, close, volume, amount, adj_factor) "
        "VALUES ('600519','2026-10-17',1500,1510,1495,1500,1000,1.5e6,1.0),"
        "('600519','2026-10-18',1490,1500,1485,1490,1000,1.49e6,0.995),"
        "('000001','2026-10-17',12,12.1,11.9,12,5000,6e4,1.0),"
        "('000001','2026-10-18',12.05,12.1,12,12.05,5000,6e4,1.0)"
    )


def _insert_watch(conn, watch_id, code, target, last_review, created_at, status="watching"):
    conn.execute(
        "INSERT INTO watchlist "
        "(watch_id, code, target_buy_price, status, last_review_date, created_at) "
        "VALUES (?,?,?,?,?,?)",
        [watch_id, code, target, status, last_review, created_at],
    )


def test_scan_triggered_when_price_below_adjusted_target(conn, config):
    """600519 target=1500，除息后 adj_target=1492.5，最新 close=1490 → 触发。"""
    _seed(conn)
    today = date(2026, 10, 18)
    _insert_watch(conn, "w1", "600519", 1500, "2026-10-17", "2026-10-17 00:00:00+00")
    report = scan_watchlist(conn, config, today)
    assert len(report.triggered) == 1
    assert report.triggered[0].code == "600519"
    assert report.triggered[0].adjusted_target == pytest.approx(1492.5)
    # DB 状态已更新
    row = conn.execute("SELECT status FROM watchlist WHERE watch_id='w1'").fetchone()
    assert row[0] == "triggered"


def test_scan_unchanged_when_price_above_target(conn, config):
    """000001 target=11，最新 close=12.05，复权无变化 → 保持 watching。"""
    _seed(conn)
    today = date(2026, 10, 18)
    _insert_watch(conn, "w2", "000001", 11, "2026-10-17", "2026-10-17 00:00:00+00")
    report = scan_watchlist(conn, config, today)
    assert report.unchanged == 1
    assert not report.triggered
    row = conn.execute("SELECT status FROM watchlist WHERE watch_id='w2'").fetchone()
    assert row[0] == "watching"


def test_scan_expired_by_ttl(conn, config):
    """last_review 超 90 天 → expired，即使价格到位也不触发。"""
    _seed(conn)
    today = date(2026, 10, 18)
    _insert_watch(conn, "w3", "600519", 1500, "2026-07-01", "2026-07-01 00:00:00+00")
    report = scan_watchlist(conn, config, today)
    assert len(report.expired) == 1
    assert "保质期" in report.expired[0].reason
    row = conn.execute("SELECT status FROM watchlist WHERE watch_id='w3'").fetchone()
    assert row[0] == "expired"


def test_scan_expired_by_new_report(conn, config):
    """复核期内有新财报 → 自动 expired（强制复核）。"""
    _seed(conn)
    today = date(2026, 10, 18)
    _insert_watch(conn, "w4", "600519", 1500, "2026-10-17", "2026-10-17 00:00:00+00")
    conn.execute(
        "INSERT INTO fact_financial (code, report_period, ann_date) "
        "VALUES ('600519','2026-09-30','2026-10-18')"
    )
    report = scan_watchlist(conn, config, today)
    assert len(report.expired) == 1
    assert "新财报" in report.expired[0].reason
    row = conn.execute("SELECT status FROM watchlist WHERE watch_id='w4'").fetchone()
    assert row[0] == "expired"


def test_scan_skipped_when_no_market_data(conn, config):
    """标的无最新行情 → skipped，不改状态。"""
    _seed(conn)
    conn.execute("INSERT INTO dim_stock (code, market, name) VALUES ('000002','SZ','万科A')")
    today = date(2026, 10, 18)
    _insert_watch(conn, "w5", "000002", 8, "2026-10-17", "2026-10-17 00:00:00+00")
    report = scan_watchlist(conn, config, today)
    assert report.skipped_no_market == ["w5"]
    row = conn.execute("SELECT status FROM watchlist WHERE watch_id='w5'").fetchone()
    assert row[0] == "watching"


def test_scan_empty_bar_table(conn, config):
    """行情表为空 → 返回空报告，不报错。"""
    today = date(2026, 10, 18)
    report = scan_watchlist(conn, config, today)
    assert report.watched == 0
    assert report.latest_trade_date is None


def test_scan_idempotent(conn, config):
    """重复扫描不重复触发（已 triggered/expired 不重处理）。"""
    _seed(conn)
    today = date(2026, 10, 18)
    _insert_watch(conn, "w1", "600519", 1500, "2026-10-17", "2026-10-17 00:00:00+00")
    r1 = scan_watchlist(conn, config, today)
    r2 = scan_watchlist(conn, config, today)
    assert len(r1.triggered) == 1
    assert len(r2.triggered) == 0  # 第二次已是 triggered，不重扫


# ---------------------------------------------------------------------------
# CLI 冒烟（直接调 cmd_* 函数，避免 subprocess）
# ---------------------------------------------------------------------------


def test_cli_add_and_list(conn, config, monkeypatch):
    import scripts.watchlist as wl

    monkeypatch.setattr(wl, "_today", lambda: date(2026, 10, 18))

    import argparse

    # add
    args = argparse.Namespace(
        code="600519",
        target="1500",
        committee_session=None,
        summary="测试",
        thesis_id=None,
        required_discount=None,
    )
    rc = wl.cmd_add(args, conn)
    assert rc == 0

    # list（status 过滤）
    args = argparse.Namespace(status="watching")
    rc = wl.cmd_list(args, conn, ttl_days=90)
    assert rc == 0


def test_cli_add_invalid_target(conn, config):
    import argparse

    import scripts.watchlist as wl

    args = argparse.Namespace(
        code="600519",
        target="-1",
        committee_session=None,
        summary=None,
        thesis_id=None,
        required_discount=None,
    )
    rc = wl.cmd_add(args, conn)
    assert rc == 2


def test_cli_expire_and_reverify(conn, config):
    import argparse

    import scripts.watchlist as wl

    _insert_watch(conn, "w1", "600519", 1500, "2026-10-17", "2026-10-17 00:00:00+00")

    rc = wl.cmd_expire(argparse.Namespace(watch_id="w1"), conn)
    assert rc == 0
    assert (
        conn.execute("SELECT status FROM watchlist WHERE watch_id='w1'").fetchone()[0] == "expired"
    )

    rc = wl.cmd_reverify(argparse.Namespace(watch_id="w1", target="1450"), conn)
    assert rc == 0
    row = conn.execute(
        "SELECT status, target_buy_price, last_review_date FROM watchlist WHERE watch_id='w1'"
    ).fetchone()
    assert row[0] == "watching"
    assert row[1] == 1450.0
