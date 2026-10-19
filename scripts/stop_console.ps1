# stop_console.ps1 —— 停止 VIQTrader Web 控制台后台进程
# 用法：powershell -ExecutionPolicy Bypass -File scripts\stop_console.ps1
# 原理：按 config 中 web.port（默认 8000）找监听该端口的进程并结束。
# 仅当进程命令行包含 run_console.py 时才结束，避免误杀占用同端口的无关程序。

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

# 从 default.yaml 读端口（简单正则，避免引入 YAML 依赖）
$port = 8000
$yamlPath = Join-Path $repoRoot 'config\default.yaml'
if (Test-Path $yamlPath) {
    $match = Select-String -Path $yamlPath -Pattern '^\s*port:\s*(\d+)' | Select-Object -First 1
    if ($match) { $port = [int]$match.Matches[0].Groups[1].Value }
}

$conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if (-not $conns) {
    Write-Host "端口 $port 无监听进程，控制台未在运行。"
    exit 0
}

$stopped = 0
foreach ($conn in $conns) {
    $procId = $conn.OwningProcess
    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if (-not $proc) { continue }
    $cmdline = (Get-CimInstance Win32_Process -Filter "ProcessId=$procId").CommandLine
    if ($cmdline -and $cmdline -match 'run_console\.py') {
        Stop-Process -Id $procId -Force
        Write-Host "已停止控制台进程 PID=$procId（$cmdline）"
        $stopped++
    } else {
        Write-Host "跳过 PID=$procId：端口 $port 被非控制台程序占用（$($proc.ProcessName)），未结束。"
    }
}

if ($stopped -eq 0) { exit 1 }
Write-Host "完成：共停止 $stopped 个控制台进程。"
