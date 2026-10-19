"""P6 Web 控制台启动入口。

用法（仓库根目录）：
    .venv\\Scripts\\python.exe scripts\\run_console.py

重复启动行为：端口被本脚本上一实例占用时自动停止旧实例再启动；
被无关程序占用则拒绝启动并提示（避免误杀），退出码 2。

安全基线：仅允许 127.0.0.1 回环监听（本机自用，无认证）；
config web.host 改为其他值直接拒绝启动。
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from viqtrader.config.loader import Config  # noqa: E402

# 单发 PowerShell：找监听端口的进程，命令行含 run_console.py 才结束；
# 输出 KILLED <pid> / FOREIGN <pid> 供 Python 侧解析（ASCII 输出避免编码问题）
_PS_KILL_SCRIPT = """
$conns = Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue
foreach ($c in $conns) {{
  $pidNum = $c.OwningProcess
  $cmd = (
    Get-CimInstance Win32_Process -Filter "ProcessId=$pidNum" -ErrorAction SilentlyContinue
  ).CommandLine
  if ($cmd -and $cmd -match 'run_console\\.py') {{
    Stop-Process -Id $pidNum -Force
    Write-Output "KILLED $pidNum"
  }} else {{
    Write-Output "FOREIGN $pidNum"
  }}
}}
"""


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex((host, port)) == 0


def _stop_previous_instance(host: str, port: int) -> bool:
    """停止占用端口的旧控制台实例。成功（或无占用）返回 True，被无关程序占用返回 False。"""
    proc = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            _PS_KILL_SCRIPT.format(port=port),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    lines = proc.stdout.split()
    if "FOREIGN" in lines:
        pid = lines[lines.index("FOREIGN") + 1] if len(lines) > lines.index("FOREIGN") + 1 else "?"
        print(f"端口 {port} 被无关程序占用（PID={pid}），拒绝启动以免误杀", file=sys.stderr)
        return False
    # 等端口真正释放（Stop-Process 异步生效）
    for _ in range(20):
        if not _port_in_use(host, port):
            return True
        time.sleep(0.25)
    print(f"旧实例已结束但端口 {port} 未释放，请稍后重试", file=sys.stderr)
    return False


def main() -> int:
    config = Config.load(REPO_ROOT / "config")
    host = str(config.get("web.host", "127.0.0.1"))
    port = int(config.get("web.port", 8000))
    if host != "127.0.0.1":
        print(f"安全基线：仅允许 127.0.0.1 回环监听，收到 {host!r}", file=sys.stderr)
        return 2

    if _port_in_use(host, port):
        print(f"端口 {port} 已被占用，尝试停止上一控制台实例…")
        if not _stop_previous_instance(host, port):
            return 2
        print("旧实例已停止，继续启动")

    import uvicorn

    from viqtrader.web.app import create_app

    print(f"VIQTrader 控制台: http://{host}:{port}")
    uvicorn.run(create_app(config), host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
