"""页面路由层：HTTP 入参解析 + 服务层调用 + 模板渲染。"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import Response


def render(
    request: Request, name: str, context: dict[str, Any], status_code: int = 200
) -> Response:
    """统一模板渲染出口（starlette TemplateResponse 返回 Any，在此收口类型）。"""
    templates = request.app.state.templates
    resp: Response = templates.TemplateResponse(
        request=request, name=name, context=context, status_code=status_code
    )
    return resp


def lock_page(request: Request, message: str) -> Response:
    """锁冲突统一提示页（DAG 运行中占用数据库）。"""
    return render(
        request,
        "lock.html",
        {"message": message, "active_page": ""},
        status_code=503,
    )
