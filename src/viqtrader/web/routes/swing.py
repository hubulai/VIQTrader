"""波段链主页路由（P6 重构）：合并原 overview + signals + strategies + positions + today + review。

所有数据按 account.mode='swing' 过滤。
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from viqtrader.web.deps import (
    REPO_ROOT,
    LockConflictError,
    outputs_dir,
    query_conn,
    write_conn,
)
from viqtrader.web.routes import lock_page, render
from viqtrader.web.services.chain import account_id_by_mode
from viqtrader.web.services.overview import load_live_state
from viqtrader.web.services.positions import list_positions, summarize_pnl
from viqtrader.web.services.signals import (
    SignalActionError,
    confirm_signal,
    list_signals,
    reject_signal,
)
from viqtrader.web.services.today import (
    ManualOrderError,
    create_manual_request,
    list_failed_rule_checks,
    list_intents,
    list_order_requests,
    list_trades,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# POST 子路由：redirect 统一回到波段链主页 /swing
# ---------------------------------------------------------------------------


def _redirect(msg: str, ok: bool) -> RedirectResponse:
    return RedirectResponse(
        url=f"/swing?msg={quote(msg)}&ok={'1' if ok else '0'}",
        status_code=303,
    )


@router.post("/swing/signals/{signal_id}/confirm")
def confirm_signal_post(
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


@router.post("/swing/signals/{signal_id}/reject")
def reject_signal_post(request: Request, signal_id: str) -> Response:
    config = request.app.state.config
    try:
        with write_conn(config) as conn:
            reject_signal(conn, signal_id)
    except LockConflictError as exc:
        return lock_page(request, str(exc))
    except SignalActionError as exc:
        return _redirect(str(exc), False)
    return _redirect(f"信号已拒绝（{signal_id[:8]}…）", True)


@router.post("/swing/orders")
def create_order(
    request: Request,
    code: str = Form(...),
    side: str = Form(...),
    order_type: str = Form(...),
    qty: str = Form(...),
    price: str = Form(""),
    note: str = Form(""),
) -> Response:
    """波段链手工录单：account_id 固定为 mode='swing' 的账本。"""
    config = request.app.state.config
    try:
        with query_conn(config) as conn:
            account_id = account_id_by_mode(conn, "swing")
        if account_id is None:
            return _redirect("波段链账本未初始化", False)
    except LockConflictError as exc:
        return _redirect(str(exc), False)

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


@router.post("/swing/execute")
def execute_now(request: Request) -> Response:
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
        err_msg = f"执行失败（退出码 {proc.returncode}）: {last_line} {detail}".strip()
        return _redirect(err_msg, False)
    return _redirect(f"执行完成: {last_line}", True)


# ---------------------------------------------------------------------------
# GET 主页：波段链全区块
# ---------------------------------------------------------------------------


@router.get("/swing", response_class=HTMLResponse)
def swing_chain(
    request: Request,
    status: str | None = "pending",
    day: date | None = None,
    review_start: str | None = None,
    review_end: str | None = None,
    msg: str | None = None,
    ok: str | None = None,
    review_open: str | None = None,
) -> Response:
    config = request.app.state.config
    target_day = day or date.today()

    try:
        with query_conn(config) as conn:
            account_id = account_id_by_mode(conn, "swing")
            if account_id is None:
                return render(
                    request,
                    "swing.html",
                    {
                        "active_page": "swing",
                        "account": None,
                        "live": load_live_state(outputs_dir(config)),
                        "msg": msg,
                        "ok": ok,
                        "error": "波段链账本未初始化（account.mode='swing' 不存在）",
                    },
                )

            # ① 概览卡（单账户）
            from viqtrader.web.services.overview import _ACCOUNT_CARDS_SQL, AccountCard

            rows = conn.execute(
                f"SELECT * FROM ({_ACCOUNT_CARDS_SQL}) WHERE account_id = ?",
                [account_id],
            ).fetchall()
            account_card = None
            if rows:
                r = rows[0]
                account_card = AccountCard(
                    account_id=str(r[0]),
                    mode=str(r[1]),
                    capital_quota=float(r[2]),
                    target_ratio=float(r[3]),
                    cash_cny=float(r[4]) if r[4] is not None else None,
                    market_value_cny=float(r[5]) if r[5] is not None else None,
                    position_count=int(r[6]),
                    is_main=True,
                    hk_no_fx=bool(r[7]),
                )

            # ② 信号确认闸（波段链专属，全表查）
            signals = list_signals(conn, status=status or None)

            # ③ 波段策略注册表（读物化文件，不查 DB）
            from viqtrader.web.services.strategies import load_strategies

            active_strategy = str(config.get("swing_track.active_strategy", ""))
            strategies_view = load_strategies(outputs_dir(config), active_strategy)

            # ④ 今日交易（按 account_id 过滤）
            requests_ = list_order_requests(conn, target_day, account_id=account_id)
            intents = list_intents(conn, target_day, account_id=account_id)
            trades = list_trades(conn, target_day, account_id=account_id)
            checks = list_failed_rule_checks(conn, target_day)

            # ⑤ 持仓明细（按 account_id 过滤）
            positions = list_positions(conn, account_id=account_id)
            pnl_summary = summarize_pnl(conn)
            my_pnl = next((p for p in pnl_summary if p.account_id == account_id), None)

            # ⑥ 复盘报告（折叠区）
            review = None
            start_default = (date.today() - timedelta(days=90)).isoformat()
            if review_open:
                from viqtrader.web.services.review import build_review

                try:
                    r_start = (
                        date.fromisoformat(review_start)
                        if review_start
                        else date.today() - timedelta(days=90)
                    )
                    r_end = date.fromisoformat(review_end) if review_end else date.today()
                    review = build_review(conn, account_id, r_start, r_end)
                except (ValueError, RuntimeError):
                    review = None

    except LockConflictError as exc:
        return lock_page(request, str(exc))

    return render(
        request,
        "swing.html",
        {
            "active_page": "swing",
            "account": account_card,
            "account_id": account_id,
            "live": load_live_state(outputs_dir(config)),
            # 信号
            "signals": signals,
            "status": status or "pending",
            # 策略
            "strategies_view": strategies_view,
            # 今日
            "day": target_day.isoformat(),
            "requests": requests_,
            "intents": intents,
            "trades": trades,
            "checks": checks,
            # 持仓
            "positions": positions,
            "my_pnl": my_pnl,
            # 复盘
            "review": review,
            "review_open": review_open is not None,
            "review_start": review_start or start_default,
            "review_end": review_end or date.today().isoformat(),
            # Flash
            "msg": msg,
            "ok": ok,
        },
    )
