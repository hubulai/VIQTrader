"""研究材料页路由（P6）：投委会会话列表/详情 + 研报浏览 + 人工拍板。"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from viqtrader.web.deps import LockConflictError, query_conn, write_conn
from viqtrader.web.routes import lock_page, render
from viqtrader.web.services.research import (
    DecideError,
    decide_session,
    get_session_detail,
    list_documents,
    list_sessions,
)

router = APIRouter()


@router.get("/research", response_class=HTMLResponse)
def research(request: Request) -> Response:
    config = request.app.state.config
    try:
        with query_conn(config) as conn:
            sessions = list_sessions(conn)
            docs = list_documents(conn)
    except LockConflictError as exc:
        return lock_page(request, str(exc))
    return render(
        request,
        "research.html",
        {"sessions": sessions, "docs": docs, "active_page": "research"},
    )


@router.get("/research/sessions/{session_id}", response_class=HTMLResponse)
def session_detail(
    request: Request,
    session_id: str,
    msg: str | None = None,
    ok: bool = False,
) -> Response:
    config = request.app.state.config
    try:
        with query_conn(config) as conn:
            detail = get_session_detail(conn, session_id)
    except LockConflictError as exc:
        return lock_page(request, str(exc))
    if detail is None:
        return render(
            request,
            "error.html",
            {"message": f"投委会会话不存在：{session_id}", "active_page": "research"},
            status_code=404,
        )
    return render(
        request,
        "research_session.html",
        {"s": detail, "msg": msg, "ok": ok, "active_page": "research"},
    )


@router.post("/research/sessions/{session_id}/decide")
def decide(
    request: Request,
    session_id: str,
    decision: str = Form(...),
    notes: str = Form(""),
) -> Response:
    config = request.app.state.config

    def _back(message: str, success: bool) -> RedirectResponse:
        return RedirectResponse(
            url=f"/research/sessions/{session_id}"
            f"?msg={quote(message)}&ok={'1' if success else '0'}",
            status_code=303,
        )

    try:
        with write_conn(config) as conn:
            decide_session(conn, session_id, decision=decision, notes=notes)
    except LockConflictError as exc:
        return lock_page(request, str(exc))
    except DecideError as exc:
        return _back(str(exc), False)
    return _back(f"已拍板：{decision.strip()}（仅存档结论，下单请走信号确认/手工录单）", True)
