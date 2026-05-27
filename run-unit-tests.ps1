# run-unit-tests.ps1 — CI-friendly pytest wrapper for interview-copilot-v2.
#
# Authored by B12-UnitTests, 2026-05-27.
#
# Behavior:
#   - Discovers + runs tests/ with pytest.
#   - Splits into 4 phases:
#         1. unit               (fast, mocked, isolated)
#         2. integration smoke  (cross-module init smoke)
#         3. error paths        (failure-mode coverage)
#         4. coverage roll-up   (if pytest-cov installed, with hard 80% gate)
#   - Exits 0 on full green, 1 on any failure (CI-friendly).
#   - Honors `-FastOnly` to skip the integration + coverage phases (dev loop).
#   - Honors `-NoCoverage` to skip the coverage phase (faster local runs).
#   - Honors `-Verbose` (PowerShell common parameter) for chatty output.
#
# Usage:
#   ./run-unit-tests.ps1
#   ./run-unit-tests.ps1 -FastOnly
#   ./run-unit-tests.ps1 -NoCoverage
#
# Federation discipline:
#   - Read/Write only — does NOT auto-PR or commit results.
#   - Emits a brief summary to stdout; CI captures via $LASTEXITCODE.

[CmdletBinding()]
param(
    [switch]$FastOnly,
    [switch]$NoCoverage,
    [switch]$JUnit
)

$ErrorActionPreference = "Stop"

# Move to script directory so test discovery uses tests/ rooted here.
Set-Location -Path $PSScriptRoot

$script:phaseFailures = 0
$script:phaseSummaries = @()

function Invoke-PytestPhase {
    param(
        [Parameter(Mandatory)] [string] $Name,
        [Parameter(Mandatory)] [string[]] $Args
    )
    Write-Host ""
    Write-Host "=============================================" -ForegroundColor Cyan
    Write-Host " Phase: $Name" -ForegroundColor Cyan
    Write-Host "=============================================" -ForegroundColor Cyan
    Write-Host "  cmd: python -m pytest $($Args -join ' ')"
    Write-Host ""

    & python -m pytest @Args
    $exit = $LASTEXITCODE
    $script:phaseSummaries += [pscustomobject]@{ Phase = $Name; Exit = $exit }
    if ($exit -ne 0) {
        $script:phaseFailures += 1
        Write-Host "[FAIL] $Name (exit $exit)" -ForegroundColor Red
    } else {
        Write-Host "[OK]   $Name" -ForegroundColor Green
    }
    return $exit
}

# ---------------------------------------------------------------------------
# Verify python + pytest available
# ---------------------------------------------------------------------------
try {
    $pyVer = & python --version 2>&1
    Write-Host "Python: $pyVer"
} catch {
    Write-Host "[FAIL] python not on PATH" -ForegroundColor Red
    exit 1
}

try {
    $pytestVer = & python -m pytest --version 2>&1
    Write-Host "pytest: $($pytestVer -join ' ')"
} catch {
    Write-Host "[FAIL] pytest not installed (pip install pytest pytest-asyncio)" -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------------------
# Phase 1 — Unit tests (default, mocked, fast)
# ---------------------------------------------------------------------------
$unitArgs = @("-m", "unit", "--tb=short", "-q")
if ($JUnit) { $unitArgs += @("--junitxml=test-results-unit.xml") }
Invoke-PytestPhase -Name "Unit tests" -Args $unitArgs | Out-Null

if ($FastOnly) {
    Write-Host ""
    Write-Host "FastOnly mode — skipping integration + coverage phases." -ForegroundColor Yellow
    if ($script:phaseFailures -eq 0) {
        Write-Host "ALL UNIT TESTS PASSED" -ForegroundColor Green
        exit 0
    } else {
        Write-Host "UNIT TESTS FAILED ($script:phaseFailures phase(s))" -ForegroundColor Red
        exit 1
    }
}

# ---------------------------------------------------------------------------
# Phase 2 — Integration smoke
# ---------------------------------------------------------------------------
$intArgs = @("-m", "integration", "--tb=short", "-q")
if ($JUnit) { $intArgs += @("--junitxml=test-results-integration.xml") }
Invoke-PytestPhase -Name "Integration smoke" -Args $intArgs | Out-Null

# ---------------------------------------------------------------------------
# Phase 3 — Error-path tests (run as part of unit but called out separately)
# ---------------------------------------------------------------------------
$errArgs = @("tests/test_error_paths.py", "--tb=short", "-q")
if ($JUnit) { $errArgs += @("--junitxml=test-results-errors.xml") }
Invoke-PytestPhase -Name "Error paths" -Args $errArgs | Out-Null

# ---------------------------------------------------------------------------
# Phase 4 — Coverage roll-up (hard 80% gate)
# ---------------------------------------------------------------------------
if (-not $NoCoverage) {
    $hasCov = $false
    try {
        & python -c "import pytest_cov" 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { $hasCov = $true }
    } catch {}

    if ($hasCov) {
        $covArgs = @(
            "--cov=src",
            "--cov-report=term-missing",
            "--cov-report=xml:coverage.xml",
            "--cov-fail-under=80",
            "--tb=short",
            "-q"
        )
        Invoke-PytestPhase -Name "Coverage gate (>= 80%)" -Args $covArgs | Out-Null
    } else {
        Write-Host ""
        Write-Host "[WARN] pytest-cov not installed; skipping coverage gate." -ForegroundColor Yellow
        Write-Host "       pip install pytest-cov to enable the 80% threshold." -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host " SUMMARY" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
foreach ($s in $script:phaseSummaries) {
    $tag = if ($s.Exit -eq 0) { "OK  " } else { "FAIL" }
    $color = if ($s.Exit -eq 0) { "Green" } else { "Red" }
    Write-Host ("  [{0}] {1}  (exit {2})" -f $tag, $s.Phase, $s.Exit) -ForegroundColor $color
}
Write-Host ""

if ($script:phaseFailures -eq 0) {
    Write-Host "ALL PHASES PASSED" -ForegroundColor Green
    exit 0
} else {
    Write-Host "FAILED — $script:phaseFailures phase(s) red" -ForegroundColor Red
    exit 1
}
