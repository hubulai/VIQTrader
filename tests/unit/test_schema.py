"""建表核对：迁移后表结构与设计文档第三节逐表核对。"""

from __future__ import annotations

from viqtrader.store.db import connect_memory
from viqtrader.store.migrate import migrate

EXPECTED_TABLES = {
    # 3.1 维度
    "dim_stock",
    "dim_hk_connect_history",
    # 3.2 事实
    "fact_daily_bar",
    "fact_financial",
    "fact_valuation",
    "fact_dividend",
    "fact_disclosure",
    "fact_fx_rate",
    # 3.3 账本
    "account",
    "position",
    "trade_log",
    "cash_ledger",
    # 3.4 规则与审计
    "risk_rule",
    "risk_event",
    "agent_run",
    "agent_output",
    "thesis",
    "committee_session",
    "config_snapshot",
    "outcome_label",
    "agent_scorecard",
    "case_library",
    "watchlist",
    # 3.5 文档与外部观点
    "document",
    "document_analysis",
    "analyst_forecast",
}


def _columns(conn, table: str) -> set[str]:
    return {row[0] for row in conn.execute(f"DESCRIBE {table}").fetchall()}


def test_all_tables_exist() -> None:
    conn = connect_memory()
    migrate(conn)
    tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
    missing = EXPECTED_TABLES - tables
    assert not missing, f"缺表: {missing}"


def test_critical_audit_fields() -> None:
    conn = connect_memory()
    migrate(conn)
    # 审计红线：risk_event / agent_run 必须携带 config_hash + code_version
    assert {"config_hash", "code_version"} <= _columns(conn, "risk_event")
    assert {"config_hash", "code_version", "prompt_version"} <= _columns(conn, "agent_run")


def test_critical_business_fields() -> None:
    conn = connect_memory()
    migrate(conn)
    # 财务表必须保留披露日（point-in-time 防前视）
    assert "ann_date" in _columns(conn, "fact_financial")
    # 持仓红线字段：双轨 mode + thesis 关联
    assert {"account_id", "mode", "code", "thesis_id"} <= _columns(conn, "position")
    # 港股通名单变动流水
    assert {"action", "change_date"} <= _columns(conn, "dim_hk_connect_history")
    # 狩猎清单状态机字段
    assert {"status", "target_buy_price", "last_review_date"} <= _columns(conn, "watchlist")


def test_position_unique_constraint_per_account() -> None:
    conn = connect_memory()
    migrate(conn)
    conn.execute(
        "INSERT INTO account VALUES ('acc_value', 'value', 'CNY', 800000, 0.8),"
        " ('acc_swing', 'swing', 'CNY', 200000, 0.2)"
    )
    # 同标的双轨两行：合法
    conn.execute(
        "INSERT INTO position VALUES"
        " ('p1', 'acc_value', 'value', '600519', 100, 1500, 1500, DATE '2026-01-01', 't1'),"
        " ('p2', 'acc_swing', 'swing', '600519', 100, 1510, 1510, DATE '2026-01-02', NULL)"
    )
    # 同一子账本内同标的重复：必须被拒
    try:
        conn.execute(
            "INSERT INTO position VALUES"
            " ('p3', 'acc_value', 'value', '600519', 50, 1490, 1490, DATE '2026-01-03', NULL)"
        )
        raise AssertionError("UNIQUE(account_id, code) 未生效")
    except Exception:
        pass
    count = conn.execute("SELECT count(*) FROM position").fetchone()[0]
    assert count == 2


def test_valuation_engine_columns_match_fact_valuation() -> None:
    """估值引擎输出列集必须与 fact_valuation 表完全一致（防拼写漂移）。"""
    from viqtrader.data import valuation as v

    conn = connect_memory()
    migrate(conn)
    db_cols = _columns(conn, "fact_valuation")
    engine_cols = set(v.FACT_VALUATION_COLUMNS)
    assert (
        db_cols == engine_cols
    ), f"fact_valuation 表列 {db_cols} 与估值引擎契约列 {engine_cols} 不一致"
