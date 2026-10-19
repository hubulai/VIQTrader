"""告警中心页路由（P6）：risk_event 表 + 按日告警文件日志。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from viqtrader.web.deps import LockConflictError, alerts_dir, query_conn
from viqtrader.web.routes import lock_page, render
from viqtrader.web.services.alerts import list_alert_dates, list_risk_events, parse_alert_file

router = APIRouter()

_SEVERITIES = ("INFO", "WARN", "CRITICAL", "FATAL")


@router.get("/alerts", response_class=HTMLResponse)
def alerts(
    request: Request,
    severity: str | None = None,
    log_date: str | None = None,
) -> Response:
    config = request.app.state.config
    sev = severity if severity in _SEVERITIES else None
    try:
        with query_conn(config) as conn:
            events = list_risk_events(conn, severity=sev)
    except LockConflictError as exc:
        return lock_page(request, str(exc))
    adir = alerts_dir(config)
    dates = list_alert_dates(adir)
    selected_date = log_date if log_date in dates else (dates[0] if dates else None)
    log_lines = parse_alert_file(adir, selected_date) if selected_date else None
    return render(
        request,
        "alerts.html",
        {
            "events": events,
            "severity": sev,
            "severities": _SEVERITIES,
            "dates": dates,
            "selected_date": selected_date,
            "log_lines": log_lines,
            "active_page": "alerts",
        },
    )
