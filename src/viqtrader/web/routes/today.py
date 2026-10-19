"""今日交易页路由（P6）：当日请求/意图/成交/规则校验四区块 + 手工录单 + 立即执行。"""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from viqtrader.web.deps import REPO_ROOT, LockConflictError, query_conn, write_conn
from viqtrader.web.routes import lock_page, render
from viqtrader.web.services.today import (
    ManualOrderError,
    create_manual_request,
    list_accounts,
    list_failed_rule_checks,
    list_intents,
    list_order_requests,
    list_trades,
)

router = APIRouter()


def _redirect(msg: str, ok: bool) -> RedirectResponse:
    """今日交易 POST → 默认回价值链主页（两条链的 execute 都消费同队列）。"""
    return RedirectResponse(url=f"/value?msg={quote(msg)}&ok={1 if ok else 0}", status_code=303)


@router.get("/today", response_class=HTMLResponse)
def today(
    request: Request,
    day: date | None = None,
    msg: str | None = None,
    ok: str | None = None,
) -> Response:
    config = request.app.state.config
    target_day = day or date.today()
    try:
        with query_conn(config) as conn:
            requests_ = list_order_requests(conn, target_day)
            intents = list_intents(conn, target_day)
            trades = list_trades(conn, target_day)
            checks = list_failed_rule_checks(conn, target_day)
    except LockConflictError as exc:
        return lock_page(request, str(exc))
    return render(
        request,
        "today.html",
        {
            "day": target_day.isoformat(),
            "requests": requests_,
            "intents": intents,
            "trades": trades,
            "checks": checks,
            "accounts": list_accounts(config),
            "msg": msg,
            "ok": ok,
            "active_page": "today",
        },
    )


@router.post("/today/orders")
def create_order(
    request: Request,
    account_id: str = Form(...),
    code: str = Form(...),
    side: str = Form(...),
    order_type: str = Form(...),
    qty: str = Form(...),
    price: str = Form(""),
    note: str = Form(""),
) -> Response:
    """手工录单（6.3 确认闸统一入口，双轨通用）。"""
    config = request.app.state.config
    try:
        qty_i = int(qty)
        price_f = float(price) if price.strip() else None
    except ValueError:
        return _redirect("数量/价格格式非法", False)
    try:
        with write_conn(config) as conn:
            request_id = create_manual_request(
                conn,
                config,
                account_id=account_id,
                code=code,
                side=side,
                order_type=order_type,
                qty=qty_i,
                price=price_f,
                note=note.strip() or None,
            )
    except ManualOrderError as exc:
        return _redirect(str(exc), False)
    except LockConflictError as exc:
        return _redirect(str(exc), False)
    return _redirect(f"已录单 {code} {side} x{qty_i}（request {request_id[:8]}），等待执行", True)


@router.post("/today/execute")
def execute_now(request: Request) -> Response:
    """「立即执行」按钮：subprocess 同步调 execute_orders.py 消费 pending 请求。

    拍板①选 B：执行时机 = DAG 每日消费 + 本按钮即时触发，二者共用同一
    消费器，幂等锚点 request_id，重复点击不会重复成交。
    """
    config = request.app.state.config
    timeout = int(config.get("web.execute_timeout_seconds", 120))
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "execute_orders.py"),
        "--config-dir",
        str(config.config_dir),
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _redirect(f"执行超时（>{timeout}s），请检查日志或改用 DAG 消费", False)
    last_line = (proc.stdout.strip().splitlines() or ["（无输出）"])[-1]
    if proc.returncode != 0:
        detail = (proc.stderr.strip().splitlines() or [""])[-1]
        return _redirect(
            f"执行失败（退出码 {proc.returncode}）: {last_line} {detail}".strip(), False
        )
    return _redirect(f"执行完成: {last_line}", True)
