"""信号确认页路由（P6，6.3 红线人工确认闸）。

确认 = 仅写 swing_signal.status + order_request（服务层同事务）；
web 绝不写 order_intent、绝不直接触发执行——执行侧由每日 DAG 与
「立即执行」按钮（6.3）消费 order_request。
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from viqtrader.web.deps import LockConflictError, query_conn, write_conn
from viqtrader.web.routes import lock_page, render
from viqtrader.web.services.signals import (
    SignalActionError,
    confirm_signal,
    list_signals,
    reject_signal,
)

router = APIRouter()


def _redirect(msg: str, ok: bool) -> RedirectResponse:
    return RedirectResponse(url=f"/swing?msg={quote(msg)}&ok={'1' if ok else '0'}", status_code=303)


@router.get("/signals", response_class=HTMLResponse)
def signals(
    request: Request,
    status: str | None = "pending",
    msg: str | None = None,
    ok: bool = False,
) -> Response:
    config = request.app.state.config
    try:
        with query_conn(config) as conn:
            rows = list_signals(conn, status=status or None)
    except LockConflictError as exc:
        return lock_page(request, str(exc))
    return render(
        request,
        "signals.html",
        {"rows": rows, "status": status, "msg": msg, "ok": ok, "active_page": "signals"},
    )


@router.post("/signals/{signal_id}/confirm")
def confirm(
    request: Request,
    signal_id: str,
    qty: str = Form(...),
    price: str = Form(""),
    order_type: str = Form("LIMIT"),
) -> Response:
    config = request.app.state.config
    try:
        qty_val = int(qty.strip())
        price_val = float(price.strip()) if price.strip() else None
    except ValueError:
        return _redirect(f"数量/价格格式非法：qty={qty!r} price={price!r}", False)
    try:
        with write_conn(config) as conn:
            confirm_signal(
                conn, config, signal_id, qty=qty_val, price=price_val, order_type=order_type
            )
    except LockConflictError as exc:
        return lock_page(request, str(exc))
    except SignalActionError as exc:
        return _redirect(str(exc), False)
    return _redirect(f"信号已确认，写入待执行队列（{signal_id[:8]}…）", True)


@router.post("/signals/{signal_id}/reject")
def reject(request: Request, signal_id: str) -> Response:
    config = request.app.state.config
    try:
        with write_conn(config) as conn:
            reject_signal(conn, signal_id)
    except LockConflictError as exc:
        return lock_page(request, str(exc))
    except SignalActionError as exc:
        return _redirect(str(exc), False)
    return _redirect(f"信号已拒绝（{signal_id[:8]}…）", True)
