"""狩猎清单页路由（P6）：列表 + 目标价差 + 保质期倒计时 + 人工 CRUD。

CRUD 逻辑与 scripts/watchlist.py CLI 同一套（服务层共享），
控制台上线后 CLI 保留作运维备选。
"""

from __future__ import annotations

from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from viqtrader.web.deps import LockConflictError, query_conn, write_conn
from viqtrader.web.routes import lock_page, render
from viqtrader.web.services.watchlist import (
    WatchlistError,
    add_watch,
    expire_watch,
    list_watchlist,
    reverify_watch,
)

router = APIRouter()


def _redirect(msg: str, ok: bool) -> RedirectResponse:
    return RedirectResponse(
        url=f"/watchlist?msg={quote(msg)}&ok={'1' if ok else '0'}", status_code=303
    )


@router.get("/watchlist", response_class=HTMLResponse)
def watchlist(
    request: Request,
    status: str | None = None,
    msg: str | None = None,
    ok: bool = False,
) -> Response:
    config = request.app.state.config
    ttl_days = int(config.get("value_track.watchlist.research_ttl_days", 90))
    try:
        with query_conn(config) as conn:
            rows = list_watchlist(conn, today=date.today(), ttl_days=ttl_days, status=status)
    except LockConflictError as exc:
        return lock_page(request, str(exc))
    return render(
        request,
        "watchlist.html",
        {"rows": rows, "status": status, "msg": msg, "ok": ok, "active_page": "watchlist"},
    )


@router.post("/watchlist/add")
def add(
    request: Request,
    code: str = Form(...),
    target: str = Form(...),
    summary: str = Form(""),
    committee_session_id: str = Form(""),
    thesis_id: str = Form(""),
    required_discount: str = Form(""),
) -> Response:
    config = request.app.state.config
    try:
        target_val = float(target.strip())
        discount_val = float(required_discount.strip()) if required_discount.strip() else None
    except ValueError:
        return _redirect(f"目标价/折价率格式非法：target={target!r}", False)
    try:
        with write_conn(config) as conn:
            add_watch(
                conn,
                code=code,
                target=target_val,
                committee_session_id=committee_session_id.strip() or None,
                summary=summary.strip() or None,
                thesis_id=thesis_id.strip() or None,
                required_discount=discount_val,
            )
    except LockConflictError as exc:
        return lock_page(request, str(exc))
    except WatchlistError as exc:
        return _redirect(str(exc), False)
    return _redirect(f"已加入狩猎清单：{code.strip()} 目标价 {target_val:.2f}", True)


@router.post("/watchlist/{watch_id}/expire")
def expire(request: Request, watch_id: str) -> Response:
    config = request.app.state.config
    try:
        with write_conn(config) as conn:
            expire_watch(conn, watch_id)
    except LockConflictError as exc:
        return lock_page(request, str(exc))
    except WatchlistError as exc:
        return _redirect(str(exc), False)
    return _redirect(f"已标记证伪过期（{watch_id[:8]}…）", True)


@router.post("/watchlist/{watch_id}/reverify")
def reverify(request: Request, watch_id: str, target: str = Form("")) -> Response:
    config = request.app.state.config
    try:
        target_val = float(target.strip()) if target.strip() else None
    except ValueError:
        return _redirect(f"目标价格式非法：{target!r}", False)
    try:
        with write_conn(config) as conn:
            reverify_watch(conn, watch_id, target=target_val)
    except LockConflictError as exc:
        return lock_page(request, str(exc))
    except WatchlistError as exc:
        return _redirect(str(exc), False)
    suffix = f"，目标价更新为 {target_val:.2f}" if target_val is not None else ""
    return _redirect(f"已复核恢复 watching（{watch_id[:8]}…）{suffix}", True)
