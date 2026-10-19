"""持仓页路由（P6）：持仓明细 + 账户盈亏汇总 + 成交流水。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from viqtrader.web.deps import LockConflictError, query_conn
from viqtrader.web.routes import lock_page, render
from viqtrader.web.services.positions import (
    list_positions,
    list_recent_trades,
    summarize_pnl,
)

router = APIRouter()


@router.get("/positions", response_class=HTMLResponse)
def positions(request: Request) -> Response:
    config = request.app.state.config
    try:
        with query_conn(config) as conn:
            rows = list_positions(conn)
            pnl = summarize_pnl(conn)
            trades = list_recent_trades(conn)
    except LockConflictError as exc:
        return lock_page(request, str(exc))
    return render(
        request,
        "positions.html",
        {
            "positions": rows,
            "pnl": pnl,
            "trades": trades,
            "active_page": "positions",
        },
    )
