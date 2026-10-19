"""总览页路由（P6）：平台运行状态 + 全账户卡片。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from viqtrader.web.deps import LockConflictError, outputs_dir, query_conn
from viqtrader.web.services.overview import list_account_cards, load_live_state

router = APIRouter()


def _lock_page(request: Request, exc: LockConflictError) -> Response:
    """锁冲突统一提示页（DAG 运行中）。"""
    templates = request.app.state.templates
    resp: Response = templates.TemplateResponse(
        request=request,
        name="lock.html",
        context={"message": str(exc)},
        status_code=503,
    )
    return resp


@router.get("/", response_class=HTMLResponse)
def overview(request: Request) -> Response:
    config = request.app.state.config
    try:
        with query_conn(config) as conn:
            cards = list_account_cards(conn)
    except LockConflictError as exc:
        return _lock_page(request, exc)
    live = load_live_state(outputs_dir(config))
    templates = request.app.state.templates
    resp2: Response = templates.TemplateResponse(
        request=request,
        name="overview.html",
        context={"cards": cards, "live": live, "active_page": "overview"},
    )
    return resp2
