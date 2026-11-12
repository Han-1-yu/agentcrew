# ci-check.ps1 — 本地复现 CI 的检查步骤（ruff + mypy + pytest）
# 用法： .\scripts\ci-check.ps1
# 需要： Python 3.10+，首次运行会自动创建 .venv-ci 虚拟环境。

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$venvPython = Join-Path $root ".venv-ci\Scripts\python.exe"

# 1. 准备 venv
if (-not (Test-Path $venvPython)) {
    Write-Host "==> 创建 .venv-ci ..." -ForegroundColor Cyan
    python -m venv .venv-ci
    & $venvPython -m pip install --upgrade pip --quiet
}

# 2. 安装依赖（幂等：已装则跳过）
Write-Host "==> 安装依赖 ..." -ForegroundColor Cyan
& $venvPython -m pip install -r requirements.txt --quiet
# 与 .github/workflows/ci.yml 钉同一版本：不钉的话"本地绿、CI 红"最难查。
& $venvPython -m pip install "ruff==0.17.3" "mypy==2.4.1" "pytest-cov>=7.0.0" --quiet

$failed = @()

# 3. ruff
Write-Host "`n==> ruff check" -ForegroundColor Cyan
& $venvPython -m ruff check .
if ($LASTEXITCODE -ne 0) { $failed += "ruff" }

# 4. mypy
Write-Host "`n==> mypy agentcrew/" -ForegroundColor Cyan
& $venvPython -m mypy agentcrew/
if ($LASTEXITCODE -ne 0) { $failed += "mypy" }

# 5. pytest
Write-Host "`n==> pytest -q" -ForegroundColor Cyan
$env:AGENTCREW_MOCK = "true"
& $venvPython -m pytest -q
if ($LASTEXITCODE -ne 0) { $failed += "pytest" }

# 6. Studio 前端（对应 ci.yml 的 studio job；无 node_modules 时先提示）
Write-Host "`n==> studio: npm run check / test:unit" -ForegroundColor Cyan
$studioDir = Join-Path $root "studio"
if ((Test-Path (Join-Path $studioDir "node_modules")) -and (Get-Command npm -ErrorAction SilentlyContinue)) {
    Push-Location $studioDir
    npm run check --silent
    if ($LASTEXITCODE -ne 0) { $failed += "studio:check" }
    npm run test:unit --silent
    if ($LASTEXITCODE -ne 0) { $failed += "studio:unit" }
    Pop-Location
} else {
    Write-Host "  跳过：studio/node_modules 不存在或未安装 npm（先 cd studio; npm ci）" -ForegroundColor Yellow
}

# 7. 汇总
Write-Host "`n========================================" -ForegroundColor Cyan
if ($failed.Count -eq 0) {
    Write-Host "ALL CHECKS PASSED" -ForegroundColor Green
} else {
    Write-Host "FAILED: $($failed -join ', ')" -ForegroundColor Red
    exit 1
}
