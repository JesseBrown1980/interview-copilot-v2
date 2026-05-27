# ============================================================================
# Asolaria Abacus AI Citizen Watchdog
# Anchor PID: PROF-X-ABACUS-AI-CITIZEN-PID-2026-05-27 (DESIGN-MINTED-PENDING-APEX-MINT)
# Wave: 18-agent-wave-2026-05-27 / Slot A04 AbacusFabricCitizen
# Law: ASOLARIA-FOUNDATION-V3-LAW-2026-05-23 (4mo window; 2mo Special-OP sub-window)
# ----------------------------------------------------------------------------
# Polls Get-Process AbacusAI every 30s. If absent or exit-code non-zero, emits
# a fabric envelope to cosign chain via :4953. Falls back to local HBP-write
# under C:/HyperBEHCS/data/abacus-watchdog/ if :4953 unreachable. If 3 crashes
# in 1h, escalates by writing an escalation marker and (in dry-run mode by
# default) shells the A02 patch script. NEVER auto-launches Abacus AI — that
# is an operator-gated action per v3-LAW restriction list.
# ----------------------------------------------------------------------------
# Per asolaria-self-reflect-daemon.ps1 + v48-citizens-watchdog.ps1 + antigravity-
# cdp-watchdog.ps1 patterns. HBP-first emission (.hbp + sha-chain) per
# feedback_hbp_first_json_cold_only_2026_05_22.
# ============================================================================

[CmdletBinding()]
param(
  [int]$IntervalSec       = 30,
  [int]$CrashCapPerHour   = 3,
  [string]$DataDir        = 'C:\HyperBEHCS\data\abacus-watchdog',
  [string]$CosignUri      = 'http://127.0.0.1:4953',
  [string]$DashboardUri   = 'http://127.0.0.1:4949',
  [string]$A02PatchScript = 'C:\HyperBEHCS\bin\abacus-A02-patch.ps1',
  [string]$A03ReinstallScript = 'C:\HyperBEHCS\bin\abacus-A03-reinstall.ps1',
  [string]$ProcessNameRegex = '^(AbacusAI|Abacus\.AI|abacus-ai)$',
  [switch]$DryRunRecovery = $true,
  [switch]$NoRecovery     = $false
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

$AnchorPid = 'PROF-X-ABACUS-AI-CITIZEN-PID-2026-05-27'
$WatchdogPid = 'ABACUS-WATCHDOG-DAEMON-PID-2026-05-27'

$HbpPath  = Join-Path $DataDir 'abacus-tick.hbp'
$HbiPath  = Join-Path $DataDir 'abacus-tick.hbi'
$ShaPath  = Join-Path $DataDir 'abacus-tick.sha256'
$PidPath  = Join-Path $DataDir 'abacus-tick.daemon.pid'
$LogPath  = Join-Path $DataDir 'abacus-watchdog.log'
$EscPath  = Join-Path $DataDir 'escalation-marker.json'

if (-not (Test-Path $DataDir)) { New-Item -ItemType Directory -Path $DataDir -Force | Out-Null }

# ---------- sha-chain helpers (mirrors asolaria-self-reflect-daemon.ps1) ------
function Get-LastRowHash {
  if (-not (Test-Path $HbpPath)) { return '0000000000000000' }
  $lastLine = Get-Content $HbpPath -Tail 1 -ErrorAction SilentlyContinue
  if (-not $lastLine) { return '0000000000000000' }
  if ($lastLine -match 'row_hash=([a-f0-9]{16})') { return $Matches[1] }
  return '0000000000000000'
}

function Get-LastTick {
  if (-not (Test-Path $HbpPath)) { return 0 }
  $tail = Get-Content $HbpPath -Tail 8 -ErrorAction SilentlyContinue
  $maxTick = 0
  foreach ($line in $tail) {
    if ($line -match 'tick=(\d+)') { if ([int]$Matches[1] -gt $maxTick) { $maxTick = [int]$Matches[1] } }
  }
  return $maxTick
}

function Sha16 {
  param([string]$Text)
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
  $sha = [System.Security.Cryptography.SHA256]::Create()
  $hash = $sha.ComputeHash($bytes)
  $sha.Dispose()
  ($hash | ForEach-Object { $_.ToString('x2') }) -join '' | ForEach-Object { $_.Substring(0,16) }
}

function Emit-Hbp-Row {
  param([hashtable]$Fields, [string]$Antecedent)
  $ts = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
  $orderedFields = @()
  foreach ($k in $Fields.Keys) { $orderedFields += "$k=$($Fields[$k])" }
  $orderedFields += "anchor_pid=$AnchorPid"
  $orderedFields += "antecedents=$Antecedent"
  $orderedFields += "ts=$ts"
  $orderedFields += "json=0"
  $orderedFields += "runtime=0"
  $orderedFields += "promote=0"
  $rowPrefix = 'HBPv1|' + ($orderedFields -join '|')
  $rowHash = Sha16 -Text $rowPrefix
  $fullRow = "$rowPrefix|row_hash=$rowHash"
  Add-Content -Path $HbpPath -Value $fullRow -Encoding UTF8
  Add-Content -Path $HbiPath -Value $fullRow -Encoding UTF8
  return $rowHash
}

function Write-Log([string]$msg) {
  $ts = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
  Add-Content -Path $LogPath -Value "$ts $msg" -ErrorAction SilentlyContinue
}

# ---------- probes ------------------------------------------------------------
function Probe-AbacusProcess {
  try {
    $procs = Get-Process -ErrorAction SilentlyContinue |
             Where-Object { $_.ProcessName -match $ProcessNameRegex }
    if (-not $procs) { return @{ alive = $false; count = 0; pids = ''; cpu = 0.0; ws_mb = 0 } }
    $arr = @($procs)
    $pids = ($arr | ForEach-Object { $_.Id }) -join ','
    $cpu  = 0.0
    $ws   = 0
    foreach ($p in $arr) {
      try { $cpu += [double]$p.CPU } catch {}
      try { $ws  += [int]($p.WorkingSet64 / 1MB) } catch {}
    }
    return @{ alive = $true; count = $arr.Count; pids = $pids; cpu = $cpu; ws_mb = $ws }
  } catch {
    return @{ alive = $false; count = 0; pids = ''; cpu = 0.0; ws_mb = 0 }
  }
}

function Probe-CosignChain {
  try {
    $r = Invoke-WebRequest -Uri "$CosignUri/health" -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
    return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500)
  } catch { return $false }
}

function Emit-Cosign-Envelope {
  param([hashtable]$Envelope)
  $body = $Envelope | ConvertTo-Json -Depth 6 -Compress
  try {
    $r = Invoke-WebRequest -Uri "$CosignUri/envelope" -Method POST -Body $body -ContentType 'application/json' -TimeoutSec 4 -UseBasicParsing -ErrorAction Stop
    return @{ ok = $true; status = $r.StatusCode; via = 'cosign-chain-4953' }
  } catch {
    return @{ ok = $false; status = 0; via = 'fallback-local-hbp'; error = $_.Exception.Message }
  }
}

# ---------- recovery escalation ----------------------------------------------
function Invoke-Recovery-Escalation {
  param([int]$CrashesInWindow)
  if ($NoRecovery) { Write-Log "ESCALATION_SKIPPED reason=NoRecovery_flag crashes=$CrashesInWindow"; return 'skipped-norecovery' }
  $now = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
  $payload = @{
    anchor_pid       = $AnchorPid
    watchdog_pid     = $WatchdogPid
    crashes_in_hour  = $CrashesInWindow
    cap              = $CrashCapPerHour
    dry_run          = [bool]$DryRunRecovery
    escalation_class = 'A02-PATCH-DRY-RUN'
    operator_gate    = 'REQUIRED_BEFORE_LIVE_INVOCATION'
    ts               = $now
  }
  $payload | ConvertTo-Json -Depth 4 | Out-File -FilePath $EscPath -Encoding UTF8 -Force
  if ($DryRunRecovery) {
    Write-Log "ESCALATION_DRY_RUN A02_patch_script=$A02PatchScript marker=$EscPath"
    return 'dry-run-A02-marker-written'
  }
  if (Test-Path $A02PatchScript) {
    Write-Log "ESCALATION_LIVE invoking A02 patch=$A02PatchScript"
    Start-Process powershell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',"$A02PatchScript" -WindowStyle Hidden
    return 'live-A02-invoked'
  }
  Write-Log "ESCALATION_FAILED A02_script_missing=$A02PatchScript"
  return 'A02-missing'
}

# ---------- boot -------------------------------------------------------------
$PID | Out-File -FilePath $PidPath -Encoding ASCII -Force
$prevHash = Get-LastRowHash
$tick     = (Get-LastTick) + 1

# Per-crash timestamp ring for 1h cap.
$crashTimestamps = New-Object System.Collections.ArrayList

# Edge detection — only fire crash row on falling-edge transition alive->dead.
$lastAlive = $null

Write-Log "BOOT abacus-watchdog pid=$PID tick=${IntervalSec}s cap=${CrashCapPerHour}/hr anchor=$AnchorPid dry_run=$DryRunRecovery"

# Initial DESIGN_MINT row — written once per boot, links to PROF-X cube.
$prevHash = Emit-Hbp-Row -Fields ([ordered]@{
  'layer'        = 'design-mint-boot'
  'cube_47d'     = 'external-llm-citizen'
  'hilbert'      = 788
  'glyph_256'    = 'TLB-EXT-LLM-ABACUS-AI'
  'status'       = 'DESIGN_MINTED_PENDING_APEX_MINT'
  'authority'    = 'Special-OP'
  'tick'         = $tick
  'wave'         = '18-agent-wave-2026-05-27'
  'slot'         = 'A04-AbacusFabricCitizen'
}) -Antecedent $prevHash

while ($true) {
  $loopTs = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
  try {
    $proc = Probe-AbacusProcess
    $cosignUp = Probe-CosignChain

    # tick row — always emitted
    $prevHash = Emit-Hbp-Row -Fields ([ordered]@{
      'layer'      = 'abacus-tick'
      'alive'      = $proc.alive
      'count'      = $proc.count
      'pids'       = ($proc.pids -replace ',','-')
      'ws_mb'      = $proc.ws_mb
      'cosign_up'  = $cosignUp
      'tick'       = $tick
    }) -Antecedent $prevHash

    # crash detection — falling edge alive->dead
    if ($lastAlive -eq $true -and $proc.alive -eq $false) {
      $now = Get-Date
      $cutoff = $now.AddHours(-1)
      $keep = @($crashTimestamps | Where-Object { $_ -gt $cutoff })
      $crashTimestamps.Clear() | Out-Null
      foreach ($t in $keep) { [void]$crashTimestamps.Add($t) }
      [void]$crashTimestamps.Add($now)

      Write-Log "CRASH_DETECTED falling-edge alive->dead window_count=$($crashTimestamps.Count)"

      $envelope = @{
        envelope_class = 'abacus-ai-crash'
        anchor_pid     = $AnchorPid
        watchdog_pid   = $WatchdogPid
        crashes_in_hour = $crashTimestamps.Count
        cap             = $CrashCapPerHour
        cube_47d       = 'external-llm-citizen'
        hilbert        = 788
        glyph_256      = 'TLB-EXT-LLM-ABACUS-AI'
        gates          = @('hookwall','dual-emit','honest-fail-detector')
        ts             = $loopTs
        emit_via       = $(if ($cosignUp) { 'cosign-chain' } else { 'fallback-local-hbp' })
      }
      $emit = Emit-Cosign-Envelope -Envelope $envelope

      $prevHash = Emit-Hbp-Row -Fields ([ordered]@{
        'layer'          = 'abacus-crash-envelope'
        'crashes_in_hour'= $crashTimestamps.Count
        'cap'            = $CrashCapPerHour
        'cosign_ok'      = $emit.ok
        'emit_via'       = $emit.via
        'gates'          = 'hookwall-dual-emit-honest-fail-detector'
        'tick'           = $tick
      }) -Antecedent $prevHash

      if ($crashTimestamps.Count -ge $CrashCapPerHour) {
        $escResult = Invoke-Recovery-Escalation -CrashesInWindow $crashTimestamps.Count
        $prevHash = Emit-Hbp-Row -Fields ([ordered]@{
          'layer'        = 'abacus-escalation'
          'crashes_in_hour' = $crashTimestamps.Count
          'cap'          = $CrashCapPerHour
          'result'       = $escResult
          'dry_run'      = [bool]$DryRunRecovery
          'tick'         = $tick
        }) -Antecedent $prevHash
      }
    }
    $lastAlive = $proc.alive

    # update sha256 sidecar
    try {
      $fh = (Get-FileHash -Path $HbpPath -Algorithm SHA256).Hash.ToLower()
      "$fh  abacus-tick.hbp" | Out-File -FilePath $ShaPath -Encoding ASCII -Force
    } catch {}

  } catch {
    Write-Log "ERROR loop exception=$($_.Exception.Message)"
  }

  $tick++
  Start-Sleep -Seconds $IntervalSec
}
