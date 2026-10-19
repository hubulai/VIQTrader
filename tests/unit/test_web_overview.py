"""P6 总览页 + Web 基建（deps/锁纪律）单测。

测试形态：tmp 目录最小 config + tmp 文件库（migrate 全量迁移）+ TestClient。
锁行为依赖 DuckDB 单写者文件锁，同进程双写连接必冲突（Windows LockFile 语义）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from viqtrader.config.loader import Config
from viqtrader.store.db import connect, db_path_from_config
from viqtrader.store.migrate import migrate
from viqtrader.web.app import create_app
from viqtrader.web.deps import LockConflictError, query_conn, write_conn
from viqtrader.web.services.overview import list_account_cards, load_live_state


def _write_config(tmp_path: Path) -> Config:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "default.yaml").write_text(
        f"platform:\n  data_dir: {(tmp_path / 'data').as_posix()}\n  duckdb_file: test.duckdb\n",
        encoding="utf-8",
    )
    return Config.load(config_dir)


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    return _write_config(tmp_path)


@pytest.fixture()
def conn(config: Config) -> Iterator[duckdb.DuckDBPyConnection]:
    c = connect(config)
    migrate(c)
    yield c
    c.close()


def _seed_accounts(conn: duckdb.DuckDBPyConnection) -> None:
    conn.executemany(
        "INSERT INTO account (account_id, mode, capital_quota, target_ratio) VALUES (?,?,?,?)",
        [
            ("acc_value", "value", 800000.0, 0.8),
            ("acc_swing", "swing", 200000.0, 0.2),
            ("acc_wf", "swing", 0.0, 0.0),
        ],
    )
    conn.executemany(
        """
        INSERT INTO fact_daily_bar (code, trade_date, open, high, low, close, volume, amount)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        [
            ("600519", "2026-10-16", 1690.0, 1710.0, 1680.0, 1700.0, 1000, 1.7e6),
            ("00700", "2026-10-16", 375.0, 382.0, 372.0, 380.0, 2000, 7.6e5),
        ],
    )
    conn.execute(
        "INSERT INTO fact_fx_rate (pair, trade_date, rate) VALUES ('HKD/CNY', '2026-10-16', 0.92)"
    )
    conn.executemany(
        """
        INSERT INTO position
            (position_id, account_id, mode, code, qty, avg_cost, avg_cost_cny, open_date)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        [
            ("p1", "acc_value", "value", "600519", 100, 1600.0, 1600.0, "2026-09-01"),
            ("p2", "acc_swing", "swing", "00700", 200, 360.0, 331.2, "2026-10-10"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO cash_ledger (entry_id, account_id, trade_date, change_type,
                                 amount_cny, balance_cny)
        VALUES (?,?,?,?,?,?)
        """,
        [
            ("c1", "acc_value", "2026-09-01", "买入", -160000.0, 640000.0),
            ("c2", "acc_value", "2026-09-02", "分红", 500.0, 640500.0),
            ("c3", "acc_swing", "2026-10-10", "买入", -66240.0, 133760.0),
        ],
    )


# ---------------------------------------------------------------------------
# 服务层
# ---------------------------------------------------------------------------


class TestAccountCards:
    def test_main_accounts_first_and_values(self, conn: duckdb.DuckDBPyConnection) -> None:
        _seed_accounts(conn)
        cards = list_account_cards(conn)
        assert [c.account_id for c in cards] == ["acc_swing", "acc_value", "acc_wf"]
        value = cards[1]
        assert value.is_main and value.position_count == 1
        assert value.market_value_cny == pytest.approx(170000.0)
        # cash 取最新一行（entry_id 倒序平局兜底）
        assert value.cash_cny == pytest.approx(640500.0)
        swing = cards[0]
        # HK 持仓：200 x 380 HKD x 0.92 = 69920 CNY
        assert swing.market_value_cny == pytest.approx(69920.0)
        assert not swing.hk_no_fx

    def test_hk_position_without_fx_flagged(self, conn: duckdb.DuckDBPyConnection) -> None:
        _seed_accounts(conn)
        conn.execute("DELETE FROM fact_fx_rate")
        cards = {c.account_id: c for c in list_account_cards(conn)}
        assert cards["acc_swing"].hk_no_fx
        # 无汇率按原币计入（页面标注低估）
        assert cards["acc_swing"].market_value_cny == pytest.approx(76000.0)

    def test_empty_db(self, conn: duckdb.DuckDBPyConnection) -> None:
        assert list_account_cards(conn) == []


class TestLiveState:
    def test_read_ok(self, tmp_path: Path) -> None:
        outputs = tmp_path / "outputs"
        outputs.mkdir()
        (outputs / "live_state.json").write_text(
            json.dumps(
                {
                    "last_run_id": "2026-10-18_163001",
                    "run_date": "2026-10-18",
                    "finished_at": "2026-10-18T16:30:05",
                    "exit_code": 1,
                    "failed_tasks": ["sync_daily"],
                    "alert_counts": {"WARN": 2},
                }
            ),
            encoding="utf-8",
        )
        live = load_live_state(outputs)
        assert live is not None
        assert live.last_run_id == "2026-10-18_163001"
        assert live.exit_code == 1
        assert live.failed_tasks == ["sync_daily"]
        assert live.alert_counts == {"WARN": 2}

    def test_missing_file(self, tmp_path: Path) -> None:
        assert load_live_state(tmp_path / "outputs") is None

    def test_corrupt_json(self, tmp_path: Path) -> None:
        outputs = tmp_path / "outputs"
        outputs.mkdir()
        (outputs / "live_state.json").write_text("{bad json", encoding="utf-8")
        assert load_live_state(outputs) is None


# ---------------------------------------------------------------------------
# 锁纪律（DuckDB 文件锁为进程级：同进程共享实例不互斥，必须跨进程验证）
# ---------------------------------------------------------------------------


@contextmanager
def _cross_process_write_lock(db_path: Path) -> Iterator[None]:
    """子进程持有读写连接（模拟 DAG 运行中），READY 握手后让出。"""
    script = (
        "import duckdb, time\n"
        f"conn = duckdb.connect(r'{db_path}')\n"
        "print('READY', flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "READY"
        yield
    finally:
        proc.kill()
        proc.wait(timeout=10)


class TestLockDiscipline:
    def test_write_conn_conflict_raises(
        self, config: Config, conn: duckdb.DuckDBPyConnection
    ) -> None:
        conn.close()  # 主进程释放，仅子进程持有
        with (
            _cross_process_write_lock(db_path_from_config(config)),
            pytest.raises(LockConflictError),
            write_conn(config, retries=0),
        ):
            pass

    def test_query_conn_conflict_raises(
        self, config: Config, conn: duckdb.DuckDBPyConnection
    ) -> None:
        conn.close()
        with (
            _cross_process_write_lock(db_path_from_config(config)),
            pytest.raises(LockConflictError),
            query_conn(config),
        ):
            pass

    def test_write_conn_after_release(self, config: Config) -> None:
        with write_conn(config) as c:
            migrate(c)
        with query_conn(config) as c:
            row = c.execute("SELECT 1").fetchone()
        assert row == (1,)


# ---------------------------------------------------------------------------
# 路由层
# ---------------------------------------------------------------------------


class TestOverviewPage:
    def test_page_renders_accounts(self, config: Config, conn: duckdb.DuckDBPyConnection) -> None:
        _seed_accounts(conn)
        conn.close()  # 释放写锁，web 只读连接方可打开
        client = TestClient(create_app(config))
        resp = client.get("/")
        assert resp.status_code == 200
        assert "acc_value" in resp.text
        assert "acc_swing" in resp.text
        assert "acc_wf" in resp.text
        assert "640,500.00" in resp.text

    def test_page_503_when_db_locked(self, config: Config, conn: duckdb.DuckDBPyConnection) -> None:
        _seed_accounts(conn)
        conn.close()
        client = TestClient(create_app(config), raise_server_exceptions=False)
        with _cross_process_write_lock(db_path_from_config(config)):
            resp = client.get("/")
        assert resp.status_code == 503
        assert "数据库占用中" in resp.text

    def test_page_degrades_without_live_state(
        self, config: Config, conn: duckdb.DuckDBPyConnection
    ) -> None:
        conn.close()
        client = TestClient(create_app(config))
        resp = client.get("/")
        assert resp.status_code == 200
        assert "暂无运行记录" in resp.text
