"""止损扫描单测（engine/risk）：纯函数判定 + 内存库扫描装配 + 幂等 + DAG 装配。"""

from __future__ import annotations

from datetime import date

import duckdb

from viqtrader.engine.risk.scan import (
    is_below_min_consecutive,
    is_dividend_cut,
    is_fcf_negative_consecutive,
    is_max_drawdown,
    is_over_holding,
    is_price_stop_loss,
    scan_position_risk,
)
from viqtrader.orchestrator.jobs import build_daily_tasks

TODAY = date(2026, 10, 18)


# ---------------------------------------------------------------------------
# 纯函数判定
# ---------------------------------------------------------------------------


def test_below_min_consecutive():
    assert is_below_min_consecutive([0.10, 0.09], 0.12) is True
    assert is_below_min_consecutive([0.10, 0.15], 0.12) is False
    assert is_below_min_consecutive([0.10, None], 0.12) is False  # 宁缺勿错
    assert is_below_min_consecutive([], 0.12) is False


def test_fcf_negative_consecutive():
    assert is_fcf_negative_consecutive([-1.0, -2.0]) is True
    assert is_fcf_negative_consecutive([-1.0, 0.5]) is False
    assert is_fcf_negative_consecutive([-1.0, None]) is False


def test_dividend_cut():
    assert is_dividend_cut(0.5, 1.0) is True
    assert is_dividend_cut(1.0, 1.0) is False
    assert is_dividend_cut(None, 1.0) is False
    assert is_dividend_cut(0.5, None) is False


def test_price_stop_loss_boundary():
    # 成本 100，止损 -8% → 线 92；收盘 92 触发，92.1 不触发
    assert is_price_stop_loss(92.0, 100.0, -0.08) is True
    assert is_price_stop_loss(92.1, 100.0, -0.08) is False
    assert is_price_stop_loss(None, 100.0, -0.08) is False


def test_max_drawdown():
    # 峰值 120，回撤 -15% → 线 102
    assert is_max_drawdown(102.0, 120.0, -0.15) is True
    assert is_max_drawdown(102.1, 120.0, -0.15) is False
    assert is_max_drawdown(90.0, None, -0.15) is False


def test_over_holding_strict():
    assert is_over_holding(date(2026, 9, 28), TODAY, 20) is False  # 恰好 20 天
    assert is_over_holding(date(2026, 9, 27), TODAY, 20) is True  # 21 天


# ---------------------------------------------------------------------------
# 内存库扫描装配
# ---------------------------------------------------------------------------


def _seed_bar(conn: duckdb.DuckDBPyConnection, code: str, rows: list[tuple]) -> None:
    """rows: (trade_date, close)；其余 OHLC 用 close 填充。"""
    for trade_date, close in rows:
        conn.execute(
            "INSERT INTO fact_daily_bar "
            "(code, trade_date, open, high, low, close, volume, amount, adj_factor) "
            "VALUES (?,?,?,?,?,?,?,?,1.0)",
            [code, trade_date, close, close, close, close, 1000, close * 1000],
        )


def _insert_position(
    conn: duckdb.DuckDBPyConnection,
    position_id: str,
    account_id: str,
    mode: str,
    code: str,
    avg_cost: float,
    open_date: str,
) -> None:
    conn.execute(
        "INSERT INTO position "
        "(position_id, account_id, mode, code, qty, avg_cost, avg_cost_cny, open_date) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [position_id, account_id, mode, code, 100, avg_cost, avg_cost, open_date],
    )


def _rule_keys(conn: duckdb.DuckDBPyConnection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT rule_key FROM risk_event").fetchall()}


def test_value_roe_below_2y_triggers(conn, config):
    conn.execute("INSERT INTO dim_stock (code, market, name) VALUES ('600519','SH','贵州茅台')")
    _seed_bar(conn, "600519", [("2026-10-16", 1500.0)])
    _insert_position(conn, "p1", "acc_value", "value", "600519", 1300.0, "2026-09-01")
    # 近 2 个年度 ROE 连续低于 0.12
    conn.execute(
        "INSERT INTO fact_financial_version (code, report_period, ann_date, roe, fcf) "
        "VALUES ('600519','2024-12-31','2025-03-28',0.10,5e8),"
        "('600519','2025-12-31','2026-03-27',0.09,4e8)"
    )
    report = scan_position_risk(conn, config, TODAY)
    assert [e.rule_key for e in report.events] == ["value.roic_below_min_2y"]
    row = conn.execute(
        "SELECT severity, decision, config_hash, code_version FROM risk_event"
    ).fetchone()
    assert row[0] == "WARN" and row[1] == "ALERT"
    assert row[2] and row[3]


def test_value_roe_above_not_triggered(conn, config):
    conn.execute("INSERT INTO dim_stock (code, market, name) VALUES ('600519','SH','贵州茅台')")
    _seed_bar(conn, "600519", [("2026-10-16", 1500.0)])
    _insert_position(conn, "p1", "acc_value", "value", "600519", 1300.0, "2026-09-01")
    conn.execute(
        "INSERT INTO fact_financial_version (code, report_period, ann_date, roe, fcf) "
        "VALUES ('600519','2024-12-31','2025-03-28',0.30,5e8),"
        "('600519','2025-12-31','2026-03-27',0.28,4e8)"
    )
    report = scan_position_risk(conn, config, TODAY)
    assert report.events == []
    assert conn.execute("SELECT count(*) FROM risk_event").fetchone()[0] == 0


def test_value_fcf_negative_2y_triggers(conn, config):
    conn.execute("INSERT INTO dim_stock (code, market, name) VALUES ('600519','SH','贵州茅台')")
    _seed_bar(conn, "600519", [("2026-10-16", 1500.0)])
    _insert_position(conn, "p1", "acc_value", "value", "600519", 1300.0, "2026-09-01")
    # fcf 落库为**单季口径**：年度累计 = 同年四季求和。两年年度 FCF 均为负 → 触发。
    # （2024 四季各 -2.5e7 → 全年 -1e8；2025 四季各 -5e7 → 全年 -2e8）
    # roe 只取自 12-31 行（YTD 原值），保持 ≥ 下限以隔离 ROE 事件。
    conn.execute(
        "INSERT INTO fact_financial_version (code, report_period, ann_date, roe, fcf) VALUES "
        "('600519','2024-03-31','2024-04-25',0.07,-2.5e7),"
        "('600519','2024-06-30','2024-08-20',0.15,-2.5e7),"
        "('600519','2024-09-30','2024-10-25',0.22,-2.5e7),"
        "('600519','2024-12-31','2025-03-28',0.30,-2.5e7),"
        "('600519','2025-03-31','2025-04-25',0.07,-5e7),"
        "('600519','2025-06-30','2025-08-20',0.15,-5e7),"
        "('600519','2025-09-30','2025-10-25',0.22,-5e7),"
        "('600519','2025-12-31','2026-03-27',0.28,-5e7)"
    )
    report = scan_position_risk(conn, config, TODAY)
    assert [e.rule_key for e in report.events] == ["value.fcf_negative_2y"]


def test_value_dividend_cut(conn, config):
    conn.execute("INSERT INTO dim_stock (code, market, name) VALUES ('600519','SH','贵州茅台')")
    _seed_bar(conn, "600519", [("2026-10-16", 1500.0)])
    _insert_position(conn, "p1", "acc_value", "value", "600519", 1300.0, "2026-09-01")
    conn.execute(
        "INSERT INTO fact_financial_version (code, report_period, ann_date, roe, fcf) "
        "VALUES ('600519','2024-12-31','2025-03-28',0.30,5e8),"
        "('600519','2025-12-31','2026-03-27',0.28,4e8)"
    )
    # 当年 0.5 < 上年 1.0 → 触发
    conn.execute(
        "INSERT INTO fact_dividend (code, ex_date, dividend_per_share, dividend_type) "
        "VALUES ('600519','2025-06-20',1.0,'现金'),('600519','2026-06-15',0.5,'现金')"
    )
    report = scan_position_risk(conn, config, TODAY)
    assert [e.rule_key for e in report.events] == ["value.dividend_cut"]

    # 当年无分红记录 → 不触发（避免中途误报）
    conn.execute("DELETE FROM risk_event")
    conn.execute("DELETE FROM fact_dividend")
    conn.execute(
        "INSERT INTO fact_dividend (code, ex_date, dividend_per_share, dividend_type) "
        "VALUES ('600519','2025-06-20',1.0,'现金')"
    )
    report = scan_position_risk(conn, config, TODAY)
    assert report.events == []


def test_swing_all_three_events(conn, config):
    """波段持仓：跌破成本止损线 + 峰值回撤超限 + 持有超期，三事件齐触发。"""
    conn.execute("INSERT INTO dim_stock (code, market, name) VALUES ('000001','SZ','平安银行')")
    # 开仓 2026-09-01（47 天 > 20），期间峰值 120，最新 91.9（< 成本 100*0.92=92 且 < 120*0.85=102）
    _seed_bar(
        conn,
        "000001",
        [("2026-09-01", 100.0), ("2026-09-15", 120.0), ("2026-10-16", 91.9)],
    )
    _insert_position(conn, "p2", "acc_swing", "swing", "000001", 100.0, "2026-09-01")
    report = scan_position_risk(conn, config, TODAY)
    assert {e.rule_key for e in report.events} == {
        "swing.price_stop_loss",
        "swing.max_drawdown",
        "swing.max_holding_days",
    }


def test_swing_healthy_not_triggered(conn, config):
    conn.execute("INSERT INTO dim_stock (code, market, name) VALUES ('000001','SZ','平安银行')")
    # 开仓 10 天，收盘 95（> 92 止损线；峰值 100 回撤仅 5%）
    _seed_bar(
        conn,
        "000001",
        [("2026-10-08", 100.0), ("2026-10-16", 95.0)],
    )
    _insert_position(conn, "p2", "acc_swing", "swing", "000001", 100.0, "2026-10-08")
    report = scan_position_risk(conn, config, TODAY)
    assert report.events == []


def test_scan_idempotent(conn, config):
    """重复扫描不重复落事件（确定性 event_id 已存在即跳过）。"""
    conn.execute("INSERT INTO dim_stock (code, market, name) VALUES ('000001','SZ','平安银行')")
    _seed_bar(
        conn,
        "000001",
        [("2026-09-01", 100.0), ("2026-09-15", 120.0), ("2026-10-16", 91.9)],
    )
    _insert_position(conn, "p2", "acc_swing", "swing", "000001", 100.0, "2026-09-01")
    r1 = scan_position_risk(conn, config, TODAY)
    assert len(r1.events) == 3
    count1 = conn.execute("SELECT count(*) FROM risk_event").fetchone()[0]
    r2 = scan_position_risk(conn, config, TODAY)
    assert r2.events == []  # 已存在 → 不重复报告不重复告警
    assert r2.suppressed == 3  # 仅计数呈现
    count2 = conn.execute("SELECT count(*) FROM risk_event").fetchone()[0]
    assert count1 == count2 == 3


def test_scan_empty_positions(conn, config):
    report = scan_position_risk(conn, config, TODAY)
    assert report.scanned == 0
    assert report.events == []


def test_scan_skipped_no_market(conn, config):
    _insert_position(conn, "p1", "acc_value", "value", "600519", 1300.0, "2026-09-01")
    report = scan_position_risk(conn, config, TODAY)
    assert report.skipped_no_market == ["p1"]
    assert conn.execute("SELECT count(*) FROM risk_event").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# DAG 装配：risk_scan 占位已激活
# ---------------------------------------------------------------------------


def test_build_daily_tasks_has_risk_scan(config):
    tasks = build_daily_tasks(config, TODAY)
    by_name = {t.name: t for t in tasks}
    assert "risk_scan" in by_name
    assert by_name["risk_scan"].placeholder is False
    assert by_name["risk_scan"].depends_on == ("sync_daily",)
