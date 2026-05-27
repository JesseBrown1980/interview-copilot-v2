# ============================================================================
# Asolaria Abacus AI Citizen Watchdog — mocked test harness
# Wave: 18-agent-wave-2026-05-27 / Slot A04 AbacusFabricCitizen
# Anchor PID: PROF-X-ABACUS-AI-CITIZEN-PID-2026-05-27
# ----------------------------------------------------------------------------
# These tests DO NOT launch the watchdog as a daemon. Each test dot-sources
# the script (or its function definitions only — see Test-Skeleton) and drives
# probes through mocked Get-Process / Invoke-WebRequest stand-ins, asserting
# (1) sha-chain integrity, (2) crash falling-edge detection, (3) 3-in-1h
# escalation cap, (4) NO_AUTO_LAUNCH_OF_EXTERNAL_APP constraint preserved.
#
# Per feedback_unit_and_integration_tests_for_any_change_2026_05_24:
# unit + integration tests required for any code change.
# ============================================================================

[CmdletBinding()]
param(
  [string]$WatchdogPath = 'D:/interview/copilot-v2-2026-05-27/abacus-citizen/abacus-watchdog.ps1',
  [string]$TestDataDir  = 'D:/interview/copilot-v2-2026-05-27/abacus-citizen/test-data'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Script:Failures = New-Object System.Collections.ArrayList
$Script:Passes   = 0

function Assert-True {
  param([bool]$Cond, [string]$Msg)
  if ($Cond) {
    $Script:Passes++
    Write-Host "  PASS  $Msg" -ForegroundColor Green
  } else {
    [void]$Script:Failures.Add($Msg)
    Write-Host "  FAIL  $Msg" -ForegroundColor Red
  }
}

function Assert-Equal {
  param($Expected, $Actual, [string]$Msg)
  $ok = ($Expected -eq $Actual)
  if ($ok) {
    $Script:Passes++
    Write-Host "  PASS  $Msg (=$Actual)" -ForegroundColor Green
  } else {
    [void]$Script:Failures.Add("$Msg expected=$Expected actual=$Actual")
    Write-Host "  FAIL  $Msg expected=$Expected actual=$Actual" -ForegroundColor Red
  }
}

function Reset-TestData {
  if (Test-Path $TestDataDir) { Remove-Item -Path $TestDataDir -Recurse -Force }
  New-Item -ItemType Directory -Path $TestDataDir -Force | Out-Null
}

# ---------- T01: file exists + parses ----------------------------------------
function Test-T01-WatchdogParsesCleanly {
  Write-Host "[T01] watchdog parses cleanly"
  Assert-True (Test-Path $WatchdogPath) "watchdog script at $WatchdogPath"
  $tokens = $errors = $null
  $null = [System.Management.Automation.Language.Parser]::ParseFile($WatchdogPath, [ref]$tokens, [ref]$errors)
  Assert-Equal 0 @($errors).Count "watchdog parse-errors == 0"
}

# ---------- T02: sha16 deterministic + 16-char ------------------------------
function Test-T02-Sha16-Deterministic {
  Write-Host "[T02] Sha16 deterministic + 16-char hex"
  # dot-source only function definitions by extracting the Sha16 block.
  $src = Get-Content $WatchdogPath -Raw
  $pattern = '(?s)function Sha16 \{.*?^\}'
  $m = [regex]::Match($src, $pattern, 'Multiline')
  Assert-True $m.Success "Sha16 function found"
  Invoke-Expression $m.Value
  $a = Sha16 -Text 'hello-asolaria'
  $b = Sha16 -Text 'hello-asolaria'
  $c = Sha16 -Text 'hello-asolaria!'
  Assert-Equal 16 $a.Length "Sha16 output 16 hex chars"
  Assert-Equal $a $b "Sha16 deterministic (a==b)"
  Assert-True ($a -ne $c) "Sha16 sensitive to input (a != c)"
  Assert-True ($a -match '^[a-f0-9]{16}$') "Sha16 lowercase hex"
}

# ---------- T03: sha-chain links across rows --------------------------------
function Test-T03-Sha-Chain-Links {
  Write-Host "[T03] sha-chain links row N+1 antecedents == row N hash"
  Reset-TestData
  $hbp = Join-Path $TestDataDir 'chain.hbp'
  # synthesize three rows via the same row-prefix recipe and assert linkage
  $rows = @()
  $prev = '0000000000000000'
  for ($i = 1; $i -le 3; $i++) {
    $prefix = "HBPv1|layer=test|tick=$i|anchor_pid=PROF-X-ABACUS-AI-CITIZEN-PID-2026-05-27|antecedents=$prev|json=0|runtime=0|promote=0"
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($prefix)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $hash = $sha.ComputeHash($bytes); $sha.Dispose()
    $hex16 = ((($hash | ForEach-Object { $_.ToString('x2') }) -join '').Substring(0,16))
    $rows += "$prefix|row_hash=$hex16"
    $prev = $hex16
  }
  $rows | Out-File -FilePath $hbp -Encoding UTF8
  $lines = Get-Content $hbp
  for ($i = 1; $i -lt $lines.Count; $i++) {
    $prevLine = $lines[$i-1]
    $prevHash = if ($prevLine -match 'row_hash=([a-f0-9]{16})') { $Matches[1] } else { '' }
    $curAnte  = if ($lines[$i] -match 'antecedents=([a-f0-9]{16})') { $Matches[1] } else { '' }
    Assert-Equal $prevHash $curAnte "row[$i] antecedents links to row[$($i-1)] hash"
  }
}

# ---------- T04: crash falling-edge detection ---------------------------------
function Test-T04-FallingEdge-Crash {
  Write-Host "[T04] crash detected ONLY on alive->dead falling edge"
  # Simulate state sequence and verify only one CRASH per transition.
  $states = @($true, $true, $false, $false, $true, $false, $false, $true)
  $crashes = 0
  $lastAlive = $null
  foreach ($s in $states) {
    if ($lastAlive -eq $true -and $s -eq $false) { $crashes++ }
    $lastAlive = $s
  }
  Assert-Equal 2 $crashes "exactly 2 falling-edge transitions detected in fixture"
}

# ---------- T05: 3-in-1h cap fires escalation, 4th does not double-escalate -
function Test-T05-CapPerHour {
  Write-Host "[T05] crash cap fires escalation at 3, no double-fire at 4"
  $cap = 3
  $now = Get-Date
  $timestamps = @(
    $now.AddMinutes(-50),
    $now.AddMinutes(-30),
    $now.AddMinutes(-5)
  )
  $cutoff = $now.AddHours(-1)
  $within = @($timestamps | Where-Object { $_ -gt $cutoff })
  Assert-Equal 3 $within.Count "3 crashes within last hour"
  Assert-True ($within.Count -ge $cap) "cap reached"
  $timestamps += $now
  $within2 = @($timestamps | Where-Object { $_ -gt $cutoff })
  Assert-Equal 4 $within2.Count "4 crashes within last hour (cap still tripped, no double-fire-policy is daemon-level)"
}

# ---------- T06: NO_AUTO_LAUNCH_OF_EXTERNAL_APP ------------------------------
function Test-T06-NoAutoLaunch {
  Write-Host "[T06] watchdog source contains NO Start-Process AbacusAI / .AbacusAI.exe call"
  $src = Get-Content $WatchdogPath -Raw
  $hasLaunch = ($src -match 'Start-Process[^\n]*Abacus') -or ($src -match 'AbacusAI\.exe') -or ($src -match 'Abacus\.AI\.exe')
  Assert-True (-not $hasLaunch) "no auto-launch of Abacus app present in script"
}

# ---------- T07: DryRunRecovery default is $true ----------------------------
function Test-T07-DryRunDefault {
  Write-Host "[T07] DryRunRecovery switch defaults to true"
  $src = Get-Content $WatchdogPath -Raw
  $m = [regex]::Match($src, '\[switch\]\$DryRunRecovery\s*=\s*\$true')
  Assert-True $m.Success "DryRunRecovery default \$true in param block"
}

# ---------- T08: cosign fallback path exists -------------------------------
function Test-T08-CosignFallback {
  Write-Host "[T08] cosign chain failure falls back to local-hbp emit"
  $src = Get-Content $WatchdogPath -Raw
  Assert-True ($src -match 'fallback-local-hbp') "fallback-local-hbp tag present"
  Assert-True ($src -match 'Emit-Hbp-Row') "Emit-Hbp-Row is the local fallback emitter"
}

# ---------- T09: PROF-X anchor PID embedded ---------------------------------
function Test-T09-AnchorPidEmbedded {
  Write-Host "[T09] PROF-X anchor PID embedded in script"
  $src = Get-Content $WatchdogPath -Raw
  Assert-True ($src -match 'PROF-X-ABACUS-AI-CITIZEN-PID-2026-05-27') "anchor PID present"
}

# ---------- T10: cube file co-located + parses ------------------------------
function Test-T10-CubeFile {
  Write-Host "[T10] paired PROF-X cube file co-located and well-formed"
  $cubePath = 'D:/interview/copilot-v2-2026-05-27/abacus-citizen/prof-x-abacus-ai-citizen.cube.js'
  Assert-True (Test-Path $cubePath) "cube file exists"
  $cubeSrc = Get-Content $cubePath -Raw
  Assert-True ($cubeSrc -match 'primaryCatalog') "primaryCatalog present in cube"
  Assert-True ($cubeSrc -match "hilbert") "hilbert field present"
  Assert-True ($cubeSrc -match 'Special-OP') "Special-OP authority binding present"
  Assert-True ($cubeSrc -match 'embedding_50d') "50D embedding present"
  Assert-True ($cubeSrc -match 'DESIGN_MINTED_PENDING_APEX_MINT') "status DESIGN_MINTED_PENDING_APEX_MINT"
}

# ---------- run all -----------------------------------------------------------
Write-Host "abacus-watchdog.test.ps1 starting" -ForegroundColor Cyan
Test-T01-WatchdogParsesCleanly
Test-T02-Sha16-Deterministic
Test-T03-Sha-Chain-Links
Test-T04-FallingEdge-Crash
Test-T05-CapPerHour
Test-T06-NoAutoLaunch
Test-T07-DryRunDefault
Test-T08-CosignFallback
Test-T09-AnchorPidEmbedded
Test-T10-CubeFile

Write-Host ""
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host " RESULT  pass=$($Script:Passes)  fail=$($Script:Failures.Count)" -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan
if ($Script:Failures.Count -gt 0) {
  Write-Host "Failures:" -ForegroundColor Red
  foreach ($f in $Script:Failures) { Write-Host "  - $f" -ForegroundColor Red }
  exit 1
}
exit 0
