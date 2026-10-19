"""FastAPI 应用工厂（P6）。

本机自用安全基线：仅监听 127.0.0.1（scripts/run_console.py 强制校验），
无认证——回环之外不可达。
"""

from __future__ import annotations

from pathlib import Path

import duckdb
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from viqtrader.config.loader import Config

_PACKAGE_DIR = Path(__file__).resolve().parent
_DEFAULT_CONFIG_DIR = _PACKAGE_DIR.parents[2] / "config"


def create_app(config: Config | None = None, *, config_dir: Path | None = None) -> FastAPI:
    """应用工厂：注入 Config 单例（热加载由 Config 自身负责）。"""
    if config is None:
        config = Config.load(config_dir or _DEFAULT_CONFIG_DIR)
    app = FastAPI(title="VIQTrader 控制台", docs_url=None, redoc_url=None)
    app.state.config = config
    app.state.templates = Jinja2Templates(directory=_PACKAGE_DIR / "templates")
    app.mount("/static", StaticFiles(directory=_PACKAGE_DIR / "static"), name="static")

    from .routes import lock_page

    @app.exception_handler(duckdb.Error)
    def _db_error_page(request: Request, exc: duckdb.Error) -> Response:
        """数据库层异常统一收敛为 503 提示页，避免直接抛 500 令页面白屏卡死。"""
        return lock_page(request, f"数据库访问失败（可稍后重试）：{exc}")

    from .routes import (
        alerts,
        overview,
        params,
        positions,
        research,
        signals,
        strategies,
        today,
        watchlist,
    )

    for module in (
        overview,
        today,
        positions,
        research,
        watchlist,
        signals,
        strategies,
        alerts,
        params,
    ):
        app.include_router(module.router)
    return app
