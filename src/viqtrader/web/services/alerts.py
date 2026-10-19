"""告警中心页数据装配（P6）：risk_event 表 + 按日告警文件日志。

日志行格式（P5.1 告警路由文件通道）：
`HH:MM:SS [LEVEL] source: message`，解析失败的行原样保留。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import duckdb

_LOG_LINE = re.compile(r"^(\d{2}:\d{2}:\d{2})\s+\[(\w+)\]\s+([^:]+):\s?(.*)$")
_FILE_NAME = re.compile(r"^alerts-(\d{8})\.log$")


@dataclass(frozen=True, slots=True)
class RiskEventRow:
    event_ts: str
    severity: str
    rule_key: str
    code: str | None
    account_id: str | None
    decision: str
    message: str | None


@dataclass(frozen=True, slots=True)
class AlertLogLine:
    time: str
    level: str
    source: str
    message: str


def list_risk_events(
    conn: duckdb.DuckDBPyConnection,
    *,
    severity: str | None = None,
    limit: int = 200,
) -> list[RiskEventRow]:
    where = "WHERE severity = ?" if severity else ""
    params: list[object] = [severity] if severity else []
    rows = conn.execute(
        f"""
        SELECT CAST(event_ts AS TEXT), severity, rule_key, code, account_id, decision, message
        FROM risk_event
        {where}
        ORDER BY event_ts DESC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    return [
        RiskEventRow(
            str(r[0]),
            str(r[1]),
            str(r[2]),
            str(r[3]) if r[3] is not None else None,
            str(r[4]) if r[4] is not None else None,
            str(r[5]),
            str(r[6]) if r[6] is not None else None,
        )
        for r in rows
    ]


def list_alert_dates(alerts: Path) -> list[str]:
    """可用告警日志日期（YYYY-MM-DD 倒序）。"""
    if not alerts.exists():
        return []
    dates = []
    for f in alerts.iterdir():
        m = _FILE_NAME.match(f.name)
        if m:
            d = m.group(1)
            dates.append(f"{d[:4]}-{d[4:6]}-{d[6:]}")
    return sorted(dates, reverse=True)


def parse_alert_file(alerts: Path, day: str) -> list[AlertLogLine] | None:
    """解析指定日期（YYYY-MM-DD）告警日志；文件不存在返回 None。"""
    path = alerts / f"alerts-{day.replace('-', '')}.log"
    if not path.exists():
        return None
    lines: list[AlertLogLine] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        m = _LOG_LINE.match(raw)
        if m:
            lines.append(AlertLogLine(m.group(1), m.group(2), m.group(3).strip(), m.group(4)))
        else:
            lines.append(AlertLogLine("", "RAW", "", raw))
    return lines
