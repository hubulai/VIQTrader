"""参数编辑页路由（P6，拍板⑤）：user.yaml 全文编辑 + 双校验 + 快照入库。"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from viqtrader.config.loader import Config
from viqtrader.store.config_snapshot import record_snapshot
from viqtrader.web.deps import LockConflictError, query_conn, write_conn
from viqtrader.web.routes import lock_page, render
from viqtrader.web.services.params import (
    IMPACT_HINTS,
    ParamsError,
    list_snapshots,
    read_user_yaml,
    save_user_yaml,
)

router = APIRouter()


@router.get("/params", response_class=HTMLResponse)
def params_page(request: Request, msg: str | None = None, ok: bool = False) -> Response:
    config = request.app.state.config
    try:
        with query_conn(config) as conn:
            snapshots = list_snapshots(conn, config)
    except LockConflictError as exc:
        return lock_page(request, str(exc))
    return render(
        request,
        "params.html",
        {
            "text": read_user_yaml(config),
            "snapshots": snapshots,
            "msg": msg,
            "ok": ok,
            "active_page": "params",
        },
    )


@router.post("/params")
def params_save(request: Request, text: str = Form(...)) -> Response:
    config = request.app.state.config

    def _back(message: str, success: bool) -> RedirectResponse:
        return RedirectResponse(
            url=f"/params?msg={quote(message)}&ok={'1' if success else '0'}",
            status_code=303,
        )

    try:
        changed = save_user_yaml(config, text)
    except ParamsError as exc:
        return _back(str(exc), False)

    # 快照入库（撞锁不阻断：文件已生效，快照可由下次成功保存或每日 DAG 补齐）
    try:
        fresh = Config.load(config.config_dir)
        with write_conn(config) as conn:
            is_new = record_snapshot(conn, fresh, note="web 参数编辑")
        snapshot_note = "，新快照已入库" if is_new else "（快照无变化）"
    except LockConflictError:
        snapshot_note = "，快照入库被锁跳过（文件已生效）"

    hints = "；".join(f"{s}: {IMPACT_HINTS.get(s, '影响见该节注释')}" for s in changed)
    return _back(f"已保存{snapshot_note}。影响：{hints or '无顶层节变化'}", True)
