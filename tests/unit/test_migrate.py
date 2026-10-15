from __future__ import annotations

from pathlib import Path

import pytest

from viqtrader.store.db import connect_memory
from viqtrader.store.migrate import MigrationError, migrate

M1 = "CREATE TABLE t1 (id INTEGER PRIMARY KEY, name TEXT);"
M2 = "CREATE TABLE t2 (id INTEGER PRIMARY KEY);"


@pytest.fixture()
def mig_dir(tmp_path: Path) -> Path:
    d = tmp_path / "migrations"
    d.mkdir()
    (d / "0001_first.sql").write_text(M1, encoding="utf-8")
    (d / "0002_second.sql").write_text(M2, encoding="utf-8")
    return d


def test_fresh_db_applies_all(mig_dir: Path) -> None:
    conn = connect_memory()
    applied = migrate(conn, mig_dir)
    assert applied == ["0001_first", "0002_second"]
    tables = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
    assert {"schema_migrations", "t1", "t2"} <= tables


def test_rerun_is_idempotent(mig_dir: Path) -> None:
    conn = connect_memory()
    migrate(conn, mig_dir)
    assert migrate(conn, mig_dir) == []
    count = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
    assert count == 2


def test_modified_applied_script_is_rejected(mig_dir: Path) -> None:
    conn = connect_memory()
    migrate(conn, mig_dir)
    (mig_dir / "0001_first.sql").write_text(M1 + "\n-- tampered\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="只增不改"):
        migrate(conn, mig_dir)


def test_new_script_applies_incrementally(mig_dir: Path) -> None:
    conn = connect_memory()
    migrate(conn, mig_dir)
    (mig_dir / "0003_third.sql").write_text(
        "CREATE TABLE t3 (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )
    assert migrate(conn, mig_dir) == ["0003_third"]


def test_builtin_migrations_apply_cleanly() -> None:
    conn = connect_memory()
    applied = migrate(conn)
    assert "0001_meta" in applied
    assert migrate(conn) == []  # 幂等
    cols = conn.execute("DESCRIBE config_snapshot").fetchall()
    assert {c[0] for c in cols} == {"config_hash", "yaml", "recorded_at", "note"}
