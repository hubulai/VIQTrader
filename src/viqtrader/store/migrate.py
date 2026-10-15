"""DuckDB 版本化迁移工具。

纪律（设计方案第十二节变更安全）：
- 迁移脚本只增不改：已应用脚本的 checksum 变化 → 拒绝执行
- 重复执行幂等：已应用的跳过
- 命名：NNNN_description.sql，按文件名排序执行
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import duckdb

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_FILENAME_RE = re.compile(r"^\d{4}_[a-z0-9_]+\.sql$")


class MigrationError(Exception):
    pass


def _bootstrap(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    TEXT PRIMARY KEY,
            checksum   TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def _applied(conn: duckdb.DuckDBPyConnection) -> dict[str, str]:
    rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    return {version: checksum for version, checksum in rows}


def _checksum(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def migrate(
    conn: duckdb.DuckDBPyConnection,
    migrations_dir: Path | None = None,
) -> list[str]:
    """执行所有未应用的迁移，返回本次应用的版本列表。"""
    directory = migrations_dir or MIGRATIONS_DIR
    _bootstrap(conn)
    applied = _applied(conn)
    newly_applied: list[str] = []

    files = sorted(f for f in directory.glob("*.sql") if _FILENAME_RE.match(f.name))
    for file in files:
        version = file.stem
        content = file.read_text(encoding="utf-8")
        checksum = _checksum(content)

        if version in applied:
            if applied[version] != checksum:
                raise MigrationError(
                    f"迁移脚本只增不改：{file.name} 已被应用但内容发生变化。"
                    "请新增迁移脚本而非修改旧脚本。"
                )
            continue

        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute(content)
            conn.execute(
                "INSERT INTO schema_migrations (version, checksum) VALUES (?, ?)",
                [version, checksum],
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        newly_applied.append(version)

    return newly_applied
