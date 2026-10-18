# 本地一键检查：等价于提交前 CI
# 注意：PowerShell 5 不会因原生命令返回非零退出码而抛异常，
# 因此必须显式检查 $LASTEXITCODE，否则检查会「假绿」。
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)

$steps = @(
    @{ Name = 'ruff';          Run = { .venv\Scripts\python.exe -m ruff check src tests } },
    @{ Name = 'ruff-format';   Run = { .venv\Scripts\python.exe -m ruff format src tests } },
    @{ Name = 'mypy';          Run = { .venv\Scripts\python.exe -m mypy src } },
    @{ Name = 'import-linter'; Run = { .venv\Scripts\lint-imports.exe } },
    @{ Name = 'pytest';        Run = { .venv\Scripts\python.exe -m pytest } }
)

$failed = @()
foreach ($step in $steps) {
    Write-Host "== $($step.Name) ==" -ForegroundColor Cyan
    & $step.Run
    if ($LASTEXITCODE -ne 0) { $failed += $step.Name }
}

if ($failed.Count -gt 0) {
    Write-Host "FAILED: $($failed -join ', ')" -ForegroundColor Red
    exit 1
}
Write-Host 'ALL CHECKS PASSED' -ForegroundColor Green
