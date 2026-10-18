"""alerting/router.py 单测：INFO 缓冲聚合、WARN/CRITICAL 实时、文件与 records。"""

from __future__ import annotations

from pathlib import Path

from viqtrader.alerting.router import AlertRouter


def _read_lines(log_file: Path) -> list[str]:
    return log_file.read_text(encoding="utf-8").strip().splitlines()


def test_info_buffered_until_flush(tmp_path: Path) -> None:
    router = AlertRouter(tmp_path, console=False)
    router.emit("INFO", "sync", "同步完成 +100")
    # INFO 缓冲，flush 前不落文件
    assert not router.log_file.exists()

    flushed = router.flush()
    assert len(flushed) == 1
    lines = _read_lines(router.log_file)
    assert any("INFO 聚合 1 条" in line for line in lines)
    assert any("[INFO] sync: 同步完成 +100" in line for line in lines)


def test_warn_and_critical_realtime(tmp_path: Path) -> None:
    router = AlertRouter(tmp_path, console=False)
    router.emit("WARN", "quality", "覆盖率不足")
    router.emit("CRITICAL", "sync", "QMT 终端离线")
    router.emit("FATAL", "engine", "账本负现金")

    lines = _read_lines(router.log_file)
    assert len(lines) == 3
    assert "[WARN] quality: 覆盖率不足" in lines[0]
    assert "[CRITICAL] sync: QMT 终端离线" in lines[1]
    assert "[FATAL] engine: 账本负现金" in lines[2]


def test_flush_empty_buffer_returns_empty(tmp_path: Path) -> None:
    router = AlertRouter(tmp_path, console=False)
    assert router.flush() == []
    assert not router.log_file.exists()


def test_records_cover_all_levels(tmp_path: Path) -> None:
    router = AlertRouter(tmp_path, console=False)
    router.emit("INFO", "a", "x")
    router.emit("WARN", "b", "y")
    assert [r.level for r in router.records] == ["INFO", "WARN"]
    router.flush()
    # flush 后 records 仍保留本 run 全部记录（供状态文件计数）
    assert len(router.records) == 2


def test_multiple_info_aggregated_with_sources(tmp_path: Path) -> None:
    router = AlertRouter(tmp_path, console=False)
    router.emit("INFO", "sync", "a")
    router.emit("INFO", "label", "b")
    router.flush()
    head = _read_lines(router.log_file)[0]
    assert "INFO 聚合 2 条" in head
    assert "'sync'" in head and "'label'" in head
