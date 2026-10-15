from __future__ import annotations

from pathlib import Path

from viqtrader.config.loader import Config
from viqtrader.store.config_snapshot import record_snapshot
from viqtrader.store.db import connect_memory
from viqtrader.store.migrate import migrate


def _make_config(tmp_path: Path, pe: float) -> Config:
    d = tmp_path / "config"
    d.mkdir(exist_ok=True)
    (d / "default.yaml").write_text(
        f"value_track:\n  gate:\n    pe_ttm_max: {pe}\n", encoding="utf-8"
    )
    return Config.load(d, env_path=d / "no.env")


def test_snapshot_dedup_and_history(tmp_path: Path) -> None:
    conn = connect_memory()
    migrate(conn)

    cfg1 = _make_config(tmp_path, 15.0)
    assert record_snapshot(conn, cfg1, note="v1") is True
    # 同 hash 重复记录 → 不产生新行
    assert record_snapshot(conn, cfg1) is False

    cfg2 = _make_config(tmp_path, 12.0)
    assert record_snapshot(conn, cfg2, note="v2") is True

    rows = conn.execute(
        "SELECT config_hash, note FROM config_snapshot ORDER BY recorded_at, config_hash"
    ).fetchall()
    assert len(rows) == 2
    notes = {r[1] for r in rows}
    assert notes == {"v1", "v2"}
    # 旧快照内容仍可完整回溯
    old_yaml = conn.execute("SELECT yaml FROM config_snapshot WHERE note = 'v1'").fetchone()[0]
    assert "15.0" in old_yaml
