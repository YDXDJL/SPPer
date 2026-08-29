$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot "computer\.venv\Scripts\python.exe"
$manifest = Join-Path $projectRoot "demo_assets\vision_replays\manifest.sha256"
$frontend = Join-Path $projectRoot "computer\out\index.html"

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "尚未安装 Python 环境，请先运行 scripts\setup_laptop.ps1。"
}
if (-not (Test-Path -LiteralPath $frontend)) {
    throw "缺少预构建网页 computer\out\index.html。"
}
if (-not (Test-Path -LiteralPath $manifest)) {
    throw "缺少演示资产校验清单。"
}

$failures = @()
foreach ($line in Get-Content -LiteralPath $manifest -Encoding UTF8) {
    if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith("#")) { continue }
    $parts = $line -split "\s+\*", 2
    if ($parts.Count -ne 2) { throw "无法解析校验行：$line" }
    $expected = $parts[0].Trim().ToUpperInvariant()
    $relative = $parts[1].Trim().Replace("/", "\")
    $path = Join-Path $projectRoot $relative
    if (-not (Test-Path -LiteralPath $path)) {
        $failures += "缺少：$relative"
        continue
    }
    $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
    if ($actual -ne $expected) { $failures += "校验失败：$relative" }
}
if ($failures.Count -gt 0) { throw ($failures -join [Environment]::NewLine) }

Push-Location (Join-Path $projectRoot "computer")
try {
    & $venvPython -c "import cv2, fastapi, serial, uvicorn; print('Python dependencies: OK')"
    & $venvPython -c "import asyncio; from backend.vision_replay import VisionReplayService; p=asyncio.run(VisionReplayService().samples_payload()); assert len(p['samples']) == 4; print('Replay catalog: OK (4 samples)')"
} finally {
    Pop-Location
}
Write-Host "展示包验收通过。" -ForegroundColor Green
