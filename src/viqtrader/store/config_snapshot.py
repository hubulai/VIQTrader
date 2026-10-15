"""配置快照入库：config 变更 → 新快照，旧快照永不覆盖。"""

from __future__ import annotations

import duckdb

from viqtrader.config.loader import Config


def record_snapshot(
    conn: duckdb.DuckDBPyConnection, config: Config, note: str | None = None
) -> bool:
    """按 content_hash 去重写入。返回是否为新快照。"""
    config_hash = config.content_hash()
    exists = conn.execute(
        "SELECT 1 FROM config_snapshot WHERE config_hash = ?", [config_hash]
    ).fetchone()
    if exists:
        return False
    conn.execute(
        "INSERT INTO config_snapshot (config_hash, yaml, note) VALUES (?, ?, ?)",
        [config_hash, config.canonical_yaml(), note],
    )
    return True
