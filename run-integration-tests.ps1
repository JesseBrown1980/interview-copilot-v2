# run-integration-tests.ps1
# Interview-copilot v2 integration test runner (agent B13 wave 2026-05-27).
#
# Usage:
#   .\run-integration-tests.ps1                      # run full integration suite
#   .\run-integration-tests.ps1 -K test_zoom_en      # filter by keyword
#   .\run-integration-tests.ps1 -Verbose             # extra pytest -vv
#   .\run-integration-tests.ps1 -FailFast            # stop on first failure
#   .\run-integration-tests.ps1 -JUnit               # write JUnit XML for CI
#
# Notes:
#   - Runs ONLY tests/integration/ (B12 owns tests/unit/).
#   - All external services are mocked in conftest.py — no network needed.
#   - Expected runtime: < 2 minutes for the full integration suite.
#   - Exits 0 on green, non-zero on failure.

[CmdletBinding()]
param(
    [string]$K = "",
    [switch]$FailFast,
    [switch]$Verbose,
    [switch]$JUnit,
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$TestDir = Join-Path $Root "tests\integration"
$ReportsDir = Join-Path $Root "tests\.reports"

if (-not (Test-Path $TestDir)) {
    Write-Error "Integration test directory not found: $TestDir"
    exit 2
}

if (-not (Test-Path $ReportsDir)) {
    New-Item -ItemType Directory -Path $ReportsDir -Force | Out-Null
}

Write-Host "[B13] Interview-copilot v2 integration suite" -ForegroundColor Cyan
Write-Host "[B13] Root         : $Root"
Write-Host "[B13] Test dir     : $TestDir"
Write-Host "[B13] Python       : $PythonExe"
Write-Host "[B13] Started      : $(Get-Date -Format o)"
Write-Host ""

# Ensure pytest is importable (don't auto-install; fail loud if missing).
$probe = & $PythonExe -c "import pytest, sys; print(pytest.__version__)" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[B13] pytest is not installed for '$PythonExe'." -ForegroundColor Red
    Write-Host "[B13] Install with:  $PythonExe -m pip install pytest" -ForegroundColor Yellow
    exit 3
}
Write-Host "[B13] pytest version : $probe"

# Build pytest arg vector.
$pytestArgs = @($TestDir, "-m", "integration")

if ($Verbose) { $pytestArgs += "-vv" } else { $pytestArgs += "-v" }
if ($FailFast) { $pytestArgs += "-x" }
if ($K -ne "") { $pytestArgs += @("-k", $K) }

$pytestArgs += "--tb=short"
$pytestArgs += "--durations=10"

if ($JUnit) {
    $junitPath = Join-Path $ReportsDir "integration-junit-$(Get-Date -Format 'yyyyMMddHHmmss').xml"
    $pytestArgs += @("--junitxml", $junitPath)
    Write-Host "[B13] JUnit XML    : $junitPath"
}

Write-Host "[B13] pytest argv  : $pytestArgs"
Write-Host ""

$sw = [System.Diagnostics.Stopwatch]::StartNew()
& $PythonExe -m pytest @pytestArgs
$exit = $LASTEXITCODE
$sw.Stop()

Write-Host ""
Write-Host "[B13] Finished     : $(Get-Date -Format o)"
Write-Host ("[B13] Elapsed      : {0:N2}s" -f $sw.Elapsed.TotalSeconds)
Write-Host "[B13] pytest exit  : $exit"

if ($sw.Elapsed.TotalSeconds -gt 120) {
    Write-Host "[B13] WARNING: integration suite exceeded 2-minute target." -ForegroundColor Yellow
}

if ($exit -eq 0) {
    Write-Host "[B13] RESULT: PASS" -ForegroundColor Green
} else {
    Write-Host "[B13] RESULT: FAIL" -ForegroundColor Red
}

exit $exit
