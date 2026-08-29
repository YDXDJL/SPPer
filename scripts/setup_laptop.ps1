param(
    [switch]$Online
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$computerRoot = Join-Path $projectRoot "computer"
$venvPython = Join-Path $computerRoot ".venv\Scripts\python.exe"
$requirements = Join-Path $computerRoot "requirements.txt"
$wheelhouse = Join-Path $projectRoot "offline_wheels"

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $pythonCommand) {
    throw "未找到 Python。请先安装 64 位 Python 3.11 或 3.12，并勾选 Add Python to PATH。"
}

$versionText = & $pythonCommand.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$version = [version]$versionText
if ($version -lt [version]"3.11" -or $version -ge [version]"3.13") {
    throw "当前 Python 为 $versionText；展示包要求 64 位 Python 3.11 或 3.12。"
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    & $pythonCommand.Source -m venv (Join-Path $computerRoot ".venv")
}

if ((Test-Path -LiteralPath $wheelhouse) -and -not $Online) {
    & $venvPython -m pip install --no-index --find-links $wheelhouse -r $requirements
} else {
    & $venvPython -m pip install -r $requirements
}

Write-Host "安装完成。下一步运行：" -ForegroundColor Green
Write-Host "  powershell -ExecutionPolicy Bypass -File .\scripts\verify_demo.ps1"
Write-Host "  powershell -ExecutionPolicy Bypass -File .\scripts\start_demo.ps1"
