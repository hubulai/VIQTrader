"""价值链主页路由（P6 重构）：合并原 overview + research + watchlist + positions + today + review。

所有数据按 account.mode='value' 过滤。链账本未初始化时页面降级提示。
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
from viqtrader.web.services.research import list_documents, list_sessions
from viqtrader.web.services.today import (
    ManualOrderError,
    create_manual_request,
    list_failed_rule_checks,
    list_intents,
    list_order_requests,
    list_trades,
)
from viqtrader.web.services.watchlist import add_watch, expire_watch, list_watchlist, reverify_watch

router = APIRouter()


# ---------------------------------------------------------------------------
# POST 子路由：redirect 统一回到价值链主页 /value
# ---------------------------------------------------------------------------


def _redirect(msg: str, ok: bool, extra: str = "") -> RedirectResponse:
    suffix = f"&{extra.lstrip('&')}" if extra else ""
    return RedirectResponse(
        url=f"/value?msg={quote(msg)}&ok={'1' if ok else '0'}{suffix}",
        status_code=303,
    )


@router.post("/value/watchlist/add")
def watchlist_add(
    request: Request,
    code: str = Form(...),
    target: str = Form(...),
    summary: str = Form(""),
    committee_session_id: str = Form(""),
    thesis_id: str = Form(""),
    required_discount: str = Form(""),
) -> Response:
    from viqtrader.web.services.watchlist import WatchlistError

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
        return _redirect(str(exc), False)
    except WatchlistError as exc:
        return _redirect(str(exc), False)
    return _redirect(f"已加入狩猎清单：{code.strip()} 目标价 {target_val:.2f}", True)


@router.post("/value/watchlist/{watch_id}/expire")
def watchlist_expire(request: Request, watch_id: str) -> Response:
    from viqtrader.web.services.watchlist import WatchlistError

    config = request.app.state.config
    try:
        with write_conn(config) as conn:
            expire_watch(conn, watch_id)
    except LockConflictError as exc:
        return _redirect(str(exc), False)
    except WatchlistError as exc:
        return _redirect(str(exc), False)
    return _redirect(f"已标记证伪过期（{watch_id[:8]}…）", True)


@router.post("/value/watchlist/{watch_id}/reverify")
def watchlist_reverify(request: Request, watch_id: str, target: str = Form("")) -> Response:
    from viqtrader.web.services.watchlist import WatchlistError

    config = request.app.state.config
    try:
        target_val = float(target.strip()) if target.strip() else None
    except ValueError:
        return _redirect(f"目标价格式非法：{target!r}", False)
    try:
        with write_conn(config) as conn:
            reverify_watch(conn, watch_id, target=target_val)
    except LockConflictError as exc:
        return _redirect(str(exc), False)
    except WatchlistError as exc:
        return _redirect(str(exc), False)
    suffix = f"，目标价更新为 {target_val:.2f}" if target_val is not None else ""
    return _redirect(f"已复核恢复 watching（{watch_id[:8]}…）{suffix}", True)


@router.post("/value/orders")
def create_order(
    request: Request,
    code: str = Form(...),
    side: str = Form(...),
    order_type: str = Form(...),
    qty: str = Form(...),
    price: str = Form(""),
    note: str = Form(""),
) -> Response:
    """价值链手工录单：account_id 固定为 mode='value' 的账本。"""
    config = request.app.state.config
    try:
        with query_conn(config) as conn:
            account_id = account_id_by_mode(conn, "value")
        if account_id is None:
            return _redirect("价值链账本未初始化", False)
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


@router.post("/value/execute")
def execute_now(request: Request) -> Response:
    """价值链「立即执行」按钮：subprocess 调 execute_orders.py。"""
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
# GET 主页：价值链全区块
# ---------------------------------------------------------------------------


@router.get("/value", response_class=HTMLResponse)
def value_chain(
    request: Request,
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
            account_id = account_id_by_mode(conn, "value")
            if account_id is None:
                # 账本未初始化 → 降级提示
                return render(
                    request,
                    "value.html",
                    {
                        "active_page": "value",
                        "account": None,
                        "live": load_live_state(outputs_dir(config)),
                        "msg": msg,
                        "ok": ok,
                        "error": "价值链账本未初始化（account.mode='value' 不存在）",
                        "accounts_hint": "请先完成数据同步和建账",
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
                from viqtrader.web.services.overview import AccountCard

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

            # ② 投委会（价值链专属，全表查）
            sessions = list_sessions(conn, limit=50)
            docs = list_documents(conn, limit=20)

            # ③ 狩猎清单（价值链专属，全表查）
            ttl_days = int(config.get("value_track.watchlist.research_ttl_days", 90))
            watchlist_rows = list_watchlist(
                conn, today=date.today(), ttl_days=ttl_days, status=None
            )

            # ④ 今日交易（按 account_id 过滤）
            requests_ = list_order_requests(conn, target_day, account_id=account_id)
            intents = list_intents(conn, target_day, account_id=account_id)
            trades = list_trades(conn, target_day, account_id=account_id)
            checks = list_failed_rule_checks(conn, target_day)  # 规则校验不按链过滤

            # ⑤ 持仓明细（按 account_id 过滤）
            positions = list_positions(conn, account_id=account_id)
            pnl_summary = summarize_pnl(conn)
            my_pnl = next((p for p in pnl_summary if p.account_id == account_id), None)

            # ⑥ 复盘报告（折叠区，可选参数展开）
            review = None
            start_default = (date.today() - timedelta(days=90)).isoformat()
            if review_open:
                from viqtrader.web.services.review import build_review

                try:
                    fallback_start = date.today() - timedelta(days=90)
                    r_start = date.fromisoformat(review_start) if review_start else fallback_start
                    r_end = date.fromisoformat(review_end) if review_end else date.today()
                    review = build_review(conn, account_id, r_start, r_end)
                except (ValueError, RuntimeError):
                    review = None

    except LockConflictError as exc:
        return lock_page(request, str(exc))

    return render(
        request,
        "value.html",
        {
            "active_page": "value",
            "account": account_card,
            "account_id": account_id,
            "live": load_live_state(outputs_dir(config)),
            # 投委会
            "sessions": sessions,
            "docs": docs,
            # 狩猎清单
            "watchlist": watchlist_rows,
            "ttl_days": ttl_days,
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
