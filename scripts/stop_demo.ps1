$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $projectRoot ".runtime\computer.pid"
if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Host "没有找到由 start_demo.ps1 启动的进程记录。"
    exit 0
}
$serverPid = [int](Get-Content -LiteralPath $pidFile -Raw)
$process = Get-Process -Id $serverPid -ErrorAction SilentlyContinue
if ($null -ne $process) {
    Stop-Process -Id $serverPid
    Write-Host "电脑端已停止。"
}
Remove-Item -LiteralPath $pidFile -Force
