"""复盘报告页路由（P6.4）：区间 Brinson 归因 + 持仓变动 + 成交明细。"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from viqtrader.web.deps import LockConflictError, query_conn
from viqtrader.web.routes import lock_page, render
from viqtrader.web.services.review import build_review, list_accounts

router = APIRouter()


@router.get("/review", response_class=HTMLResponse)
def review(request: Request) -> Response:
    config = request.app.state.config

    account = request.query_params.get("account", "")
    start_str = request.query_params.get("start", "")
    end_str = request.query_params.get("end", "")

    # 默认区间：最近 90 个自然日
    today = date.today()
    default_start = today - timedelta(days=90)

    try:
        with query_conn(config) as conn:
            accounts = list_accounts(conn)

            if not account or account not in accounts:
                # 未选账户：展示选择表单 + 最近账户概要
                return render(
                    request,
                    "review.html",
                    {
                        "active_page": "review",
                        "accounts": accounts,
                        "result": None,
                        "selected_account": account or (accounts[0] if accounts else ""),
                        "selected_start": start_str or default_start.isoformat(),
                        "selected_end": end_str or today.isoformat(),
                    },
                )

            start = date.fromisoformat(start_str) if start_str else default_start
            end = date.fromisoformat(end_str) if end_str else today
            if start > end:
                # 倒序修正
                start, end = end, start

            result = build_review(conn, account, start, end)
    except LockConflictError as exc:
        return lock_page(request, str(exc))
    except ValueError as exc:
        return render(
            request,
            "review.html",
            {
                "active_page": "review",
                "accounts": accounts if "accounts" in dir() else [],
                "result": None,
                "error": str(exc),
                "selected_account": account,
                "selected_start": start_str or default_start.isoformat(),
                "selected_end": end_str or today.isoformat(),
            },
        )

    return render(
        request,
        "review.html",
        {
            "active_page": "review",
            "accounts": accounts,
            "result": result,
            "selected_account": account,
            "selected_start": result["summary"].start,
            "selected_end": result["summary"].end,
        },
    )
