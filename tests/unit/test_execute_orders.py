"""P6 执行侧单测：consume 消费器 / execute_orders CLI / DAG 任务 / 今日页录单与立即执行。

链路红线：order_request 是唯一待执行入口；消费走 ExecutionService 全链留痕；
一次性消费语义（pending → executed/rejected，幂等锚点 request_id）。
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from viqtrader.config.loader import Config
from viqtrader.engine.execution.consume import (
    consume_order_requests,
    ensure_accounts,
    latest_trade_date,
)
from viqtrader.orchestrator.jobs import build_daily_tasks
from viqtrader.store.db import connect
from viqtrader.store.migrate import migrate
from viqtrader.web.app import create_app
from viqtrader.web.services.today import (
    ManualOrderError,
    create_manual_request,
    list_accounts,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DAY = date(2026, 10, 18)
PREV = date(2026, 10, 16)


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "default.yaml").write_text(
        "platform:\n"
        f"  data_dir: {(tmp_path / 'data').as_posix()}\n"
        "  duckdb_file: test.duckdb\n"
        "accounts:\n"
        "  value: {target_ratio: 0.8, initial_quota_cny: 800000}\n"
        "  swing: {target_ratio: 0.2, initial_quota_cny: 200000}\n",
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
def seeded_client(
    config: Config,
) -> Callable[[Callable[[duckdb.DuckDBPyConnection], None]], TestClient]:
    """种子后释放写锁再建 TestClient 的工厂（写路由冒烟用）。"""

    def _make(seed: Callable[[duckdb.DuckDBPyConnection], None]) -> TestClient:
        c = connect(config)
        migrate(c)
        seed(c)
        c.close()
        return TestClient(create_app(config))

    return _make


def _seed_market(conn: duckdb.DuckDBPyConnection) -> None:
    """低价股 000002 两日行情（执行日 2026-10-18 open 10.20）。"""
    conn.execute(
        "INSERT INTO dim_stock (code, market, name, industry, list_date, is_hk_connect) "
        "VALUES ('000002', 'SZ', '万科A', '房地产', '1991-01-29', false)"
    )
    conn.execute(
        """
        INSERT INTO fact_daily_bar
            (code, trade_date, open, high, low, close, volume, amount, adj_factor, turnover_rate)
        VALUES
            ('000002', ?, 10.00, 10.10, 9.90, 10.00, 1000000, 10000000, 1.0, 0.5),
            ('000002', ?, 10.20, 10.30, 10.10, 10.25, 1200000, 12300000, 1.0, 0.6)
        """,
        [PREV, DAY],
    )


def _insert_request(
    conn: duckdb.DuckDBPyConnection,
    *,
    order_type: str = "MARKET",
    qty: int = 100,
    price: float | None = None,
    side: str = "BUY",
) -> str:
    request_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO order_request
            (request_id, source_type, source_id, account_id, mode, code, market,
             side, order_type, qty, price, status, config_hash, code_version)
        VALUES (?, 'manual', NULL, 'acc_swing', 'swing', '000002', 'SZ',
                ?, ?, ?, ?, 'pending', 'testhash', 'test')
        """,
        [request_id, side, order_type, qty, price],
    )
    return request_id


class TestEnsureAccounts:
    def test_init_and_idempotent(self, conn: duckdb.DuckDBPyConnection, config: Config) -> None:
        created = ensure_accounts(conn, config)
        assert sorted(created) == ["acc_swing", "acc_value"]
        rows = conn.execute(
            "SELECT account_id, capital_quota FROM account ORDER BY account_id"
        ).fetchall()
        assert [(str(r[0]), float(r[1])) for r in rows] == [
            ("acc_swing", 200000.0),
            ("acc_value", 800000.0),
        ]
        # 可用现金口径 = capital_quota + Σ cash_ledger；
        # 配额已是初始资金，故初始化不得写入任何流水（否则额度重复计账）
        assert conn.execute("SELECT count(*) FROM cash_ledger").fetchone()[0] == 0

        again = ensure_accounts(conn, config)
        assert again == []
        assert conn.execute("SELECT count(*) FROM account").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM cash_ledger").fetchone()[0] == 0

    def test_quota_is_full_available_cash(
        self, conn: duckdb.DuckDBPyConnection, config: Config
    ) -> None:
        """建账后 acc_swing 可用现金应恰为配额 200000（防重复计账回归）。"""
        ensure_accounts(conn, config)
        cash = conn.execute(
            "SELECT capital_quota + COALESCE("
            "  (SELECT sum(amount_cny) FROM cash_ledger WHERE account_id = 'acc_swing'), 0)"
            " FROM account WHERE account_id = 'acc_swing'"
        ).fetchone()[0]
        assert float(cash) == pytest.approx(200000.0)


class TestConsume:
    def test_market_buy_filled(self, conn: duckdb.DuckDBPyConnection, config: Config) -> None:
        _seed_market(conn)
        request_id = _insert_request(conn)
        report = consume_order_requests(conn, config, DAY)

        assert report.executed == 1 and report.rejected == 0
        assert sorted(report.accounts_initialized) == ["acc_swing", "acc_value"]
        req = conn.execute(
            "SELECT status, note, intent_id FROM order_request WHERE request_id = ?",
            [request_id],
        ).fetchone()
        assert str(req[0]) == "executed"
        assert str(req[1]).startswith("FILLED@10.2")
        intent = conn.execute(
            "SELECT status, filled_price FROM order_intent WHERE intent_id = ?", [req[2]]
        ).fetchone()
        assert str(intent[0]) == "FILLED"
        assert float(intent[1]) == pytest.approx(10.20)
        assert conn.execute("SELECT count(*) FROM trade_log").fetchone()[0] == 1
        pos = conn.execute(
            "SELECT qty FROM position WHERE account_id = 'acc_swing' AND code = '000002'"
        ).fetchone()
        assert int(pos[0]) == 100

    def test_limit_not_filled_rejected(
        self, conn: duckdb.DuckDBPyConnection, config: Config
    ) -> None:
        """dry-run 即时成交语义：参考价劣于限价 → 请求 rejected（一次性作废）。"""
        _seed_market(conn)
        request_id = _insert_request(conn, order_type="LIMIT", price=10.15)
        report = consume_order_requests(conn, config, DAY)

        assert report.rejected == 1
        req = conn.execute(
            "SELECT status, note FROM order_request WHERE request_id = ?", [request_id]
        ).fetchone()
        assert str(req[0]) == "rejected"
        assert "高于限价" in str(req[1])

    def test_rule_rejected(self, conn: duckdb.DuckDBPyConnection, config: Config) -> None:
        """现金不足硬规则拒绝 → 请求 rejected，note 记录原因。"""
        _seed_market(conn)
        request_id = _insert_request(conn, qty=1_000_000)
        report = consume_order_requests(conn, config, DAY)

        assert report.rejected == 1
        req = conn.execute(
            "SELECT status, note FROM order_request WHERE request_id = ?", [request_id]
        ).fetchone()
        assert str(req[0]) == "rejected"
        assert "所需资金" in str(req[1])
        assert conn.execute("SELECT count(*) FROM trade_log").fetchone()[0] == 0

    def test_pending_consumed_once(self, conn: duckdb.DuckDBPyConnection, config: Config) -> None:
        """复跑幂等：pending 已清空，二次消费 0 笔，不重复成交。"""
        _seed_market(conn)
        _insert_request(conn)
        consume_order_requests(conn, config, DAY)
        again = consume_order_requests(conn, config, DAY)
        assert again.outcomes == ()
        assert conn.execute("SELECT count(*) FROM trade_log").fetchone()[0] == 1


class TestCLI:
    def test_cli_end_to_end(self, config: Config) -> None:
        conn = connect(config)
        migrate(conn)
        _seed_market(conn)
        _insert_request(conn)
        conn.close()

        proc = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "execute_orders.py"),
                "--config-dir",
                str(config.config_dir),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert "消费完成" in proc.stdout
        assert "成交 1" in proc.stdout

        check = connect(config, read_only=True)
        status = check.execute("SELECT status FROM order_request").fetchone()
        check.close()
        assert str(status[0]) == "executed"

    def test_cli_no_bars_exit_2(self, config: Config) -> None:
        conn = connect(config)
        migrate(conn)
        conn.close()
        proc = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "execute_orders.py"),
                "--config-dir",
                str(config.config_dir),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert proc.returncode == 2
        assert "fact_daily_bar 为空" in proc.stderr


class TestJobs:
    def test_daily_tasks_registered(self, config: Config) -> None:
        names = [t.name for t in build_daily_tasks(config, DAY)]
        assert "export_strategies" in names
        assert "execute_orders" in names


class TestManualOrder:
    def test_list_accounts(self, config: Config) -> None:
        options = list_accounts(config)
        assert [(a.account_id, a.mode) for a in options] == [
            ("acc_value", "value"),
            ("acc_swing", "swing"),
        ]

    def test_create_ok(self, conn: duckdb.DuckDBPyConnection, config: Config) -> None:
        _seed_market(conn)
        request_id = create_manual_request(
            conn,
            config,
            account_id="acc_value",
            code="000002",
            side="BUY",
            order_type="LIMIT",
            qty=100,
            price=10.20,
            note="投委会拍板后买入",
        )
        row = conn.execute(
            "SELECT source_type, account_id, mode, market, status, note FROM order_request "
            "WHERE request_id = ?",
            [request_id],
        ).fetchone()
        assert tuple(str(v) for v in row[:5]) == (
            "manual",
            "acc_value",
            "value",
            "SZ",
            "pending",
        )
        assert str(row[5]) == "投委会拍板后买入"

    def test_unknown_account(self, conn: duckdb.DuckDBPyConnection, config: Config) -> None:
        with pytest.raises(ManualOrderError, match="账户不在配置"):
            create_manual_request(
                conn,
                config,
                account_id="acc_x",
                code="000002",
                side="BUY",
                order_type="MARKET",
                qty=100,
                price=None,
            )

    def test_unknown_code(self, conn: duckdb.DuckDBPyConnection, config: Config) -> None:
        with pytest.raises(ManualOrderError, match="未在 dim_stock"):
            create_manual_request(
                conn,
                config,
                account_id="acc_swing",
                code="999999",
                side="BUY",
                order_type="MARKET",
                qty=100,
                price=None,
            )

    def test_limit_without_price(self, conn: duckdb.DuckDBPyConnection, config: Config) -> None:
        _seed_market(conn)
        with pytest.raises(ManualOrderError, match="正数限价"):
            create_manual_request(
                conn,
                config,
                account_id="acc_swing",
                code="000002",
                side="BUY",
                order_type="LIMIT",
                qty=100,
                price=None,
            )

    def test_qty_non_positive(self, conn: duckdb.DuckDBPyConnection, config: Config) -> None:
        with pytest.raises(ManualOrderError, match="正整数"):
            create_manual_request(
                conn,
                config,
                account_id="acc_swing",
                code="000002",
                side="BUY",
                order_type="MARKET",
                qty=0,
                price=None,
            )


class TestTodayRoutes:
    def test_get_today_renders_form(self, seeded_client: Callable[..., TestClient]) -> None:
        client = seeded_client(_seed_market)
        resp = client.get("/today")
        assert resp.status_code == 200
        assert "手工录单" in resp.text
        assert "立即执行" in resp.text

    def test_create_order_303(
        self, seeded_client: Callable[..., TestClient], config: Config
    ) -> None:
        client = seeded_client(_seed_market)
        resp = client.post(
            "/today/orders",
            data={
                "account_id": "acc_swing",
                "code": "000002",
                "side": "BUY",
                "order_type": "MARKET",
                "qty": "100",
                "price": "",
                "note": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "ok=1" in resp.headers["location"]
        check = connect(config, read_only=True)
        row = check.execute(
            "SELECT source_type, status FROM order_request WHERE code = '000002'"
        ).fetchone()
        check.close()
        assert tuple(str(v) for v in row) == ("manual", "pending")

    def test_create_order_invalid_qty_flash(self, seeded_client: Callable[..., TestClient]) -> None:
        client = seeded_client(_seed_market)
        resp = client.post(
            "/today/orders",
            data={
                "account_id": "acc_swing",
                "code": "000002",
                "side": "BUY",
                "order_type": "MARKET",
                "qty": "0",
                "price": "",
                "note": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "ok=0" in resp.headers["location"]

    def test_execute_button(self, seeded_client: Callable[..., TestClient], config: Config) -> None:
        """「立即执行」真实 subprocess：pending → executed（tmp config 隔离）。"""

        def _seed(conn: duckdb.DuckDBPyConnection) -> None:
            _seed_market(conn)
            _insert_request(conn)

        client = seeded_client(_seed)
        resp = client.post("/today/execute", follow_redirects=False)
        assert resp.status_code == 303
        assert "ok=1" in resp.headers["location"]

        check = connect(config, read_only=True)
        row = check.execute("SELECT status, note FROM order_request").fetchone()
        check.close()
        assert str(row[0]) == "executed"
        assert str(row[1]).startswith("FILLED@")


def test_latest_trade_date_empty(conn: duckdb.DuckDBPyConnection) -> None:
    assert latest_trade_date(conn) is None
