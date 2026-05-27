#requires -Version 5.1
<#
.SYNOPSIS
    Dry-run validation tests for restore-screenshotter.ps1.
    Does NOT modify any file under C:\Users\acer\AppData\Local\Programs\AbacusAI.

.DESCRIPTION
    Run as:  pwsh -NoProfile -File restore-screenshotter.test.ps1

    Expected outcome on this machine (2026-05-27 18-agent wave findings):
      - Phase 0 pre-flight should PASS (Abacus v1.106.21004 confirmed, parents exist,
        targets confirmed missing).
      - Phase 1 donor scan should return ZERO valid donors and exit 1
        with "NO DONOR FOUND" — because A02 verified that even yarn-cached
        playwright-core@1.55.0 lacks screenshotter.js.
      - With -DonorOverride pointing at a fake file, donor signature check
        should FAIL (exit 2) on garbage and PASS-then-dry-copy on a hand-crafted
        valid signature file.
      - With -SkipTopLevel set, only the inner target should be reported.

    Tests use Pester-free assertions to keep zero external deps.
#>

[CmdletBinding()]
param([switch]$Verbose)

$ErrorActionPreference = 'Stop'
$scriptUnderTest = Join-Path $PSScriptRoot 'restore-screenshotter.ps1'
$testFixtureDir  = Join-Path $env:TEMP "restore-screenshotter-tests-$([Guid]::NewGuid().ToString('N').Substring(0,8))"
$results = @()

function Add-Result {
    param([string]$Name, [bool]$Pass, [string]$Detail)
    $script:results += [PSCustomObject]@{ Test=$Name; Pass=$Pass; Detail=$Detail }
    $marker = if ($Pass) { 'PASS' } else { 'FAIL' }
    Write-Host ("  [{0}] {1}{2}" -f $marker, $Name, $(if ($Detail) { " — $Detail" } else { '' }))
}

function Invoke-Patch {
    param([string[]]$Args)
    # Run in a child pwsh so the parent isn't polluted by $LASTEXITCODE etc.
    $argList = @('-NoProfile','-ExecutionPolicy','Bypass','-File',$scriptUnderTest) + $Args
    $out = & pwsh @argList 2>&1
    return [PSCustomObject]@{
        ExitCode = $LASTEXITCODE
        Stdout   = ($out -join "`n")
    }
}

# --- Setup --------------------------------------------------------------------
Write-Host "Creating test fixtures in $testFixtureDir"
New-Item -ItemType Directory -Path $testFixtureDir -Force | Out-Null

# Fixture 1: garbage file masquerading as donor
$garbage = Join-Path $testFixtureDir 'garbage-screenshotter.js'
"console.log('not a real screenshotter');" | Set-Content -LiteralPath $garbage

# Fixture 2: synthetic valid-signature donor (large enough, contains markers)
$synthetic = Join-Path $testFixtureDir 'synthetic-screenshotter.js'
@'
"use strict";
// Synthetic screenshotter for restore-screenshotter.test.ps1 fixture.
// This file passes Test-IsLikelyScreenshotterJs signature check ONLY.
// Do NOT deploy this to the real Abacus install — it is not functional.
class Screenshotter {
    constructor(page) { this.page = page; }
    async screenshotPage() { throw new Error('synthetic fixture, not functional'); }
}
exports.Screenshotter = Screenshotter;
'@ + ("// padding " * 200) | Set-Content -LiteralPath $synthetic

# --- T1: pre-flight on real machine should PASS through Phase 0 ---------------
Write-Host "`nT1 — default dry-run exercises Phase 0 pre-flight"
$r = Invoke-Patch @()
$phase0Ok = ($r.Stdout -match 'Confirmed Abacus AI v1\.106\.21004')
Add-Result -Name 'T1.preflight-passes' -Pass $phase0Ok -Detail "stdout matched version line"

# --- T2: target-exists guard --------------------------------------------------
# Can't safely create a real screenshotter.js on the real path, so we just
# assert that the script source contains the "Target already exists" guard.
Write-Host "`nT2 — target-exists guard present in source"
$src = Get-Content -LiteralPath $scriptUnderTest -Raw
$hasGuard = ($src -match 'Target already exists') -and ($src -match 'exit 4')
Add-Result -Name 'T2.target-exists-guard' -Pass $hasGuard -Detail "source contains guard + exit 4"

# --- T3: garbage donor override rejected --------------------------------------
Write-Host "`nT3 — garbage donor override rejected (exit 2)"
$r = Invoke-Patch @('-DonorOverride', $garbage)
$rejected = ($r.ExitCode -eq 2) -and ($r.Stdout -match 'failed signature check')
Add-Result -Name 'T3.garbage-rejected' -Pass $rejected -Detail "exit=$($r.ExitCode)"

# --- T4: synthetic donor accepted in dry-run ----------------------------------
Write-Host "`nT4 — synthetic donor accepted, dry-run completes (exit 0)"
$r = Invoke-Patch @('-DonorOverride', $synthetic)
$accepted = ($r.ExitCode -eq 0) -and
            ($r.Stdout -match 'Selected donor') -and
            ($r.Stdout -match 'DRY-RUN COMPLETE')
Add-Result -Name 'T4.synthetic-accepted-dryrun' -Pass $accepted -Detail "exit=$($r.ExitCode)"

# --- T5: dry-run never writes to real install path ----------------------------
Write-Host "`nT5 — dry-run leaves both targets STILL missing"
$inner = 'C:\Users\acer\AppData\Local\Programs\AbacusAI\resources\app\node_modules\patchright-core\lib\server\screenshotter.js'
$outer = 'C:\Users\acer\AppData\Local\Programs\AbacusAI\node_modules\patchright-core\lib\server\screenshotter.js'
$neither = (-not (Test-Path -LiteralPath $inner)) -and (-not (Test-Path -LiteralPath $outer))
Add-Result -Name 'T5.dryrun-no-side-effects' -Pass $neither -Detail "inner-exists=$(Test-Path $inner) outer-exists=$(Test-Path $outer)"

# --- T6: SkipTopLevel only references inner target ----------------------------
Write-Host "`nT6 — -SkipTopLevel restricts scope to resources/app copy only"
$r = Invoke-Patch @('-DonorOverride', $synthetic, '-SkipTopLevel')
$skipped = ($r.ExitCode -eq 0) -and
           ($r.Stdout -match 'resources\\app\\node_modules\\patchright-core') -and
           ($r.Stdout -notmatch 'Would Copy-Item.+\\AbacusAI\\node_modules\\patchright-core')
Add-Result -Name 'T6.skiptoplevel-scope' -Pass $skipped -Detail "exit=$($r.ExitCode)"

# --- T7: real cache scan returns honest empty result --------------------------
Write-Host "`nT7 — real cache scan yields NO donor (operator must reinstall)"
$r = Invoke-Patch @()
$honest = ($r.ExitCode -eq 1) -and
          ($r.Stdout -match 'NO DONOR FOUND') -and
          ($r.Stdout -match 'A03')
Add-Result -Name 'T7.honest-not-viable-verdict' -Pass $honest -Detail "exit=$($r.ExitCode)"

# --- T8: nonzero exit on missing app version ----------------------------------
Write-Host "`nT8 — exit-4 path covered by source (version mismatch refused)"
$hasVerCheck = ($src -match 'expected \$ExpectedAppVer') -or
               ($src -match 'refusing to patch unverified version')
Add-Result -Name 'T8.version-mismatch-refused' -Pass $hasVerCheck -Detail "source-side check present"

# --- Summary ------------------------------------------------------------------
Write-Host "`n----- TEST SUMMARY -----"
$passes = ($results | Where-Object Pass).Count
$total  = $results.Count
foreach ($r in $results) {
    Write-Host (" {0,-4} {1}" -f $(if ($r.Pass) {'OK'} else {'FAIL'}), $r.Test)
}
Write-Host "`nResult: $passes / $total tests passed"

# Cleanup
Remove-Item -LiteralPath $testFixtureDir -Recurse -Force -ErrorAction SilentlyContinue

if ($passes -ne $total) { exit 1 } else { exit 0 }
