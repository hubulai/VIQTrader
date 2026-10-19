"""P6 只读页面批单测：今日交易/持仓/研究材料/狩猎清单/波段策略/告警中心。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from viqtrader.config.loader import Config
from viqtrader.store.db import connect
from viqtrader.store.migrate import migrate
from viqtrader.web.app import create_app
from viqtrader.web.services.alerts import list_alert_dates, list_risk_events, parse_alert_file
from viqtrader.web.services.positions import list_positions, summarize_pnl
from viqtrader.web.services.research import get_session_detail, list_documents, list_sessions
from viqtrader.web.services.strategies import load_strategies
from viqtrader.web.services.today import (
    list_failed_rule_checks,
    list_intents,
    list_order_requests,
    list_trades,
)
from viqtrader.web.services.watchlist import list_watchlist

DAY = date(2026, 10, 18)


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "default.yaml").write_text(
        "platform:\n"
        f"  data_dir: {(tmp_path / 'data').as_posix()}\n"
        "  duckdb_file: test.duckdb\n"
        "value_track:\n  watchlist:\n    research_ttl_days: 90\n"
        "swing_track:\n  active_strategy: trend_momentum\n",
        encoding="utf-8",
    )
    return Config.load(config_dir)


@pytest.fixture()
def conn(config: Config) -> Iterator[duckdb.DuckDBPyConnection]:
    c = connect(config)
    migrate(c)
    yield c
    c.close()


@pytest.fixture()
def client(config: Config, conn: duckdb.DuckDBPyConnection) -> Iterator[TestClient]:
    conn.close()  # 释放写锁，web 只读连接方可打开
    yield TestClient(create_app(config))


def _seed_base(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("INSERT INTO account VALUES ('acc_swing', 'swing', 'CNY', 200000, 0.2)")
    conn.execute(
        "INSERT INTO dim_stock (code, market, name, industry, list_date, is_hk_connect) "
        "VALUES ('600519', 'SH', '贵州茅台', '白酒', '2001-08-27', false)"
    )
    conn.execute(
        "INSERT INTO fact_daily_bar (code, trade_date, open, high, low, close, volume, amount) "
        "VALUES ('600519', '2026-10-17', 1690, 1710, 1680, 1700, 1000, 1700000)"
    )


def _seed_today(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """
        INSERT INTO order_request
            (request_id, source_type, source_id, account_id, mode, code, market, side,
             order_type, qty, price, status, created_at, config_hash, code_version)
        VALUES ('r1', 'swing_signal', 'sig1', 'acc_swing', 'swing', '600519', 'SH',
                'BUY', 'LIMIT', 100, 1690.0, 'pending', '2026-10-18 09:30:00', 'h1', 'v1')
        """
    )
    conn.execute(
        """
        INSERT INTO order_intent
            (intent_id, account_id, mode, code, market, side, order_type, qty, price,
             status, rejection_reason, submitted_at, config_hash, code_version)
        VALUES ('i1', 'acc_swing', 'swing', '600519', 'SH', 'BUY', 'LIMIT', 100, 1690.0,
                'REJECTED', '资金不足', '2026-10-18 09:31:00', 'h1', 'v1')
        """
    )
    conn.execute(
        """
        INSERT INTO rule_check_log
            (check_id, intent_id, phase, rule_type, passed, message, checked_at,
             config_hash, code_version)
        VALUES ('ck1', 'i1', 'ACCOUNT', 'CASH', false, '可用资金不足',
                '2026-10-18 09:31:01', 'h1', 'v1')
        """
    )
    conn.execute(
        """
        INSERT INTO trade_log
            (trade_id, account_id, mode, code, side, qty, price, amount_cny, trade_date)
        VALUES ('t1', 'acc_swing', 'swing', '600519', 'BUY', 100, 1700.0, 170000.0, '2026-10-18')
        """
    )


class TestTodayService:
    def test_four_blocks(self, conn: duckdb.DuckDBPyConnection) -> None:
        _seed_base(conn)
        _seed_today(conn)
        reqs = list_order_requests(conn, DAY)
        assert len(reqs) == 1 and reqs[0].status == "pending" and reqs[0].price == 1690.0
        intents = list_intents(conn, DAY)
        assert len(intents) == 1 and intents[0].status == "REJECTED"
        trades = list_trades(conn, DAY)
        assert len(trades) == 1 and trades[0].amount_cny == 170000.0
        checks = list_failed_rule_checks(conn, DAY)
        assert len(checks) == 1 and checks[0].rule_type == "CASH"

    def test_other_day_empty(self, conn: duckdb.DuckDBPyConnection) -> None:
        _seed_base(conn)
        _seed_today(conn)
        other = date(2026, 10, 19)
        assert list_order_requests(conn, other) == []
        assert list_intents(conn, other) == []
        assert list_trades(conn, other) == []
        assert list_failed_rule_checks(conn, other) == []


class TestPositionsService:
    def test_positions_and_pnl(self, conn: duckdb.DuckDBPyConnection) -> None:
        _seed_base(conn)
        conn.execute(
            "INSERT INTO position (position_id, account_id, mode, code, qty, avg_cost, "
            "avg_cost_cny, open_date) VALUES ('p1', 'acc_swing', 'swing', '600519', "
            "100, 1600, 1600, '2026-10-10')"
        )
        conn.execute(
            "INSERT INTO trade_log (trade_id, account_id, mode, code, side, qty, price, "
            "commission, stamp_tax, transfer_fee, amount_cny, trade_date) VALUES "
            "('t1', 'acc_swing', 'swing', '600519', 'BUY', 100, 1600, 80, 80, 1.6, "
            "160000, '2026-10-10')"
        )
        rows = list_positions(conn)
        assert len(rows) == 1
        p = rows[0]
        assert p.name == "贵州茅台"
        assert p.market_value_cny == pytest.approx(170000.0)
        assert p.pnl_cny == pytest.approx(10000.0)
        assert p.pnl_pct == pytest.approx(10000.0 / 160000.0)
        pnl = summarize_pnl(conn)
        assert len(pnl) == 1
        assert pnl[0].buy_amount_cny == 160000.0
        assert pnl[0].fees_cny == pytest.approx(161.6)
        assert pnl[0].float_pnl_cny == pytest.approx(10000.0)


class TestResearchService:
    def _seed_session(self, conn: duckdb.DuckDBPyConnection) -> None:
        _seed_base(conn)
        conn.execute(
            "INSERT INTO agent_run (run_id, agent_name, started_at, model, prompt_version, "
            "status, config_hash, code_version) VALUES "
            "('run1', 'valuation', '2026-10-16 10:00:00', 'm', 'v1', 'SUCCESS', 'h', 'v')"
        )
        conn.execute(
            "INSERT INTO agent_output (output_id, run_id, output_json) VALUES "
            "('out_v', 'run1', '{\"fair_value\": 1800}'), "
            "('out_d', 'run1', '{\"flaws\": [\"假设乐观\"]}')"
        )
        conn.execute(
            "INSERT INTO committee_session (session_id, code, valuation_output_id, "
            "dissent_output_id) VALUES ('s1', '600519', 'out_v', 'out_d')"
        )

    def test_sessions_and_detail(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._seed_session(conn)
        sessions = list_sessions(conn)
        assert len(sessions) == 1
        assert sessions[0].has_valuation and sessions[0].has_dissent
        assert sessions[0].final_decision is None
        detail = get_session_detail(conn, "s1")
        assert detail is not None
        assert detail.valuation_json == {"fair_value": 1800}
        assert detail.dissent_json == {"flaws": ["假设乐观"]}
        assert get_session_detail(conn, "nope") is None

    def test_documents(self, conn: duckdb.DuckDBPyConnection) -> None:
        conn.execute(
            "INSERT INTO document (doc_id, file_path, content_hash, doc_type, org, code, "
            "publish_date, parse_status) VALUES ('d1', '/x.pdf', 'hash1', '个股研报', "
            "'华安证券', '600519', '2026-10-15', 'parsed')"
        )
        conn.execute(
            "INSERT INTO document_analysis (analysis_id, doc_id, result_json, model, "
            "prompt_version) VALUES ('a1', 'd1', '{\"summary\": \"买入评级\"}', 'm', 'v1')"
        )
        docs = list_documents(conn)
        assert len(docs) == 1
        assert docs[0].org == "华安证券"
        assert docs[0].result_json == {"summary": "买入评级"}


class TestWatchlistService:
    def test_gap_and_ttl(self, conn: duckdb.DuckDBPyConnection) -> None:
        _seed_base(conn)
        conn.execute(
            "INSERT INTO watchlist (watch_id, code, target_buy_price, status, "
            "last_review_date, research_summary) VALUES "
            "('w1', '600519', 1500.0, 'watching', '2026-10-01', '好公司')"
        )
        rows = list_watchlist(conn, today=DAY, ttl_days=90)
        assert len(rows) == 1
        w = rows[0]
        assert w.gap_pct == pytest.approx((1700 - 1500) / 1500)
        assert w.ttl_days_left == 90 - 17
        rows_triggered = list_watchlist(conn, today=DAY, status="triggered")
        assert rows_triggered == []


class TestStrategiesService:
    def test_load_view(self, tmp_path: Path) -> None:
        outputs = tmp_path / "outputs"
        outputs.mkdir()
        (outputs / "strategies.json").write_text(
            json.dumps(
                {
                    "exported_at": "2026-10-18T16:30:00",
                    "strategies": [
                        {
                            "strategy_id": "trend_momentum",
                            "display_name": "趋势动量",
                            "description": "三形态并集",
                            "params": [
                                {
                                    "name": "lookback_bars",
                                    "kind": "int",
                                    "default": 60,
                                    "description": "K线窗口",
                                    "minimum": 20,
                                    "maximum": 120,
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        view = load_strategies(outputs, "trend_momentum")
        assert view is not None
        assert view.cards[0].is_active
        assert view.cards[0].params[0].minimum == 20
        assert load_strategies(tmp_path / "nowhere", "x") is None


class TestAlertsService:
    def test_risk_events_and_filter(self, conn: duckdb.DuckDBPyConnection) -> None:
        conn.execute(
            "INSERT INTO risk_event (event_id, rule_key, code, severity, decision, message, "
            "config_hash, code_version) VALUES "
            "('e1', 'price_stop_loss', '600519', 'WARN', 'ALERT', '触发止损', 'h', 'v'), "
            "('e2', 'quota', NULL, 'INFO', 'ALLOW', '配额提示', 'h', 'v')"
        )
        all_events = list_risk_events(conn)
        assert len(all_events) == 2
        warns = list_risk_events(conn, severity="WARN")
        assert len(warns) == 1 and warns[0].rule_key == "price_stop_loss"

    def test_alert_log_files(self, tmp_path: Path) -> None:
        alerts = tmp_path / "alerts"
        alerts.mkdir()
        (alerts / "alerts-20261018.log").write_text(
            "16:30:03 [INFO] label_outcomes: 打标完成\n"
            "16:30:04 [WARN] risk_scan: 600519 触发止损\n"
            "非标准行保留\n",
            encoding="utf-8",
        )
        assert list_alert_dates(alerts) == ["2026-10-18"]
        lines = parse_alert_file(alerts, "2026-10-18")
        assert lines is not None and len(lines) == 3
        assert lines[0].level == "INFO" and lines[0].source == "label_outcomes"
        assert lines[2].level == "RAW"
        assert parse_alert_file(alerts, "2026-10-19") is None


class TestPagesSmoke:
    def test_today_page(self, client: TestClient, conn2: None = None) -> None:
        resp = client.get("/today?day=2026-10-18")
        assert resp.status_code == 200
        assert "今日交易" in resp.text

    def test_positions_page(self, client: TestClient) -> None:
        resp = client.get("/positions")
        assert resp.status_code == 200
        assert "持仓与盈亏" in resp.text

    def test_research_page(self, client: TestClient) -> None:
        resp = client.get("/research")
        assert resp.status_code == 200
        assert "研究材料" in resp.text

    def test_watchlist_page(self, client: TestClient) -> None:
        resp = client.get("/watchlist")
        assert resp.status_code == 200
        assert "狩猎清单" in resp.text

    def test_strategies_page_empty(self, client: TestClient) -> None:
        resp = client.get("/strategies")
        assert resp.status_code == 200
        assert "尚未导出" in resp.text

    def test_alerts_page(self, client: TestClient) -> None:
        resp = client.get("/alerts")
        assert resp.status_code == 200
        assert "告警中心" in resp.text

    def test_session_detail_404(self, client: TestClient) -> None:
        resp = client.get("/research/sessions/nope")
        assert resp.status_code == 404
