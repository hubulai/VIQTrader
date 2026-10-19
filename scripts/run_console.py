"""P6 Web 控制台启动入口。

用法（仓库根目录）：
    .venv\\Scripts\\python.exe scripts\\run_console.py

安全基线：仅允许 127.0.0.1 回环监听（本机自用，无认证）；
config web.host 改为其他值直接拒绝启动。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from viqtrader.config.loader import Config  # noqa: E402


def main() -> int:
    config = Config.load(REPO_ROOT / "config")
    host = str(config.get("web.host", "127.0.0.1"))
    port = int(config.get("web.port", 8000))
    if host != "127.0.0.1":
        print(f"安全基线：仅允许 127.0.0.1 回环监听，收到 {host!r}", file=sys.stderr)
        return 2

    import uvicorn

    from viqtrader.web.app import create_app

    print(f"VIQTrader 控制台: http://{host}:{port}")
    uvicorn.run(create_app(config), host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
