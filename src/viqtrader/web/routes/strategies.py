"""波段策略页路由（P6）：读 outputs/strategies.json 物化元数据渲染。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from viqtrader.web.deps import outputs_dir
from viqtrader.web.routes import render
from viqtrader.web.services.strategies import load_strategies

router = APIRouter()


@router.get("/strategies", response_class=HTMLResponse)
def strategies(request: Request) -> Response:
    config = request.app.state.config
    active = str(config.get("swing_track.active_strategy", ""))
    view = load_strategies(outputs_dir(config), active)
    return render(
        request,
        "strategies.html",
        {"view": view, "active_page": "strategies"},
    )
