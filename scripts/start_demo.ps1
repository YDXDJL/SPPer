$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$computerRoot = Join-Path $projectRoot "computer"
$venvPython = Join-Path $computerRoot ".venv\Scripts\python.exe"
$runtimeRoot = Join-Path $projectRoot ".runtime"
$pidFile = Join-Path $runtimeRoot "computer.pid"

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "尚未安装，请先运行 scripts\setup_laptop.ps1。"
}
if (-not (Test-Path -LiteralPath (Join-Path $computerRoot "out\index.html"))) {
    throw "缺少预构建网页；请重新复制完整展示包。"
}

$alreadyRunning = $false
try {
    Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/state" -TimeoutSec 1 -UseBasicParsing | Out-Null
    $alreadyRunning = $true
} catch {
    $alreadyRunning = $false
}

if (-not $alreadyRunning) {
    New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
    $process = Start-Process -FilePath $venvPython -ArgumentList "run.py" -WorkingDirectory $computerRoot -WindowStyle Hidden -PassThru
    Set-Content -LiteralPath $pidFile -Value $process.Id -Encoding ASCII
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/state" -TimeoutSec 1 -UseBasicParsing | Out-Null
            $ready = $true
            break
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $ready) { throw "电脑端未能在 15 秒内启动，请手动运行 computer\.venv\Scripts\python.exe computer\run.py 查看错误。" }
}

Start-Process "http://127.0.0.1:8000/"
Write-Host "演示系统已启动：http://127.0.0.1:8000/" -ForegroundColor Green
