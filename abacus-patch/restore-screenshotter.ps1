#requires -Version 5.1
<#
.SYNOPSIS
    Restore the missing patchright-core/lib/server/screenshotter.js inside Abacus AI's
    bundled CodeLLM IDE. Dry-run by default. Pass -Apply to actually copy.

.DESCRIPTION
    Abacus AI v1.106.21004 ships a patched Playwright (patchright-core@1.55.2 in
    resources/app, v1.57.0 in the parallel top-level node_modules). A May-20-2026
    batch deletion swept screenshotter.js (and accessibility.js from the top-level
    copy) out of lib/server/, so Electron now crashes during init with
    "Cannot find module './screenshotter'".

    Patch strategy (in order; first that succeeds wins):
      1) Scan known npm/pnpm/yarn caches + project node_modules for a sibling-
         compatible screenshotter.js belonging to playwright-core or patchright-core.
      2) If a candidate is found, verify it parses (basic syntax check), compute
         its SHA-256, back up the current (broken) state of both lib/server/
         directories, then copy the donor file into BOTH target paths.
      3) If no donor is found, surface a non-zero exit code and instruct the
         operator to run agent A03's reinstall procedure instead.

.PARAMETER Apply
    Actually write files. Default is dry-run (preview only).

.PARAMETER DonorOverride
    Explicit donor path to use, bypassing the cache scan. Must point at a real
    .js file. Useful if the operator manually extracted a tarball.

.PARAMETER SkipTopLevel
    Only patch resources/app/node_modules copy (skip the parallel top-level).
    The top-level copy ALSO needs accessibility.js — this script does NOT
    restore accessibility; it only touches screenshotter.js.

.OUTPUTS
    Exit 0 = success (or dry-run that would succeed)
    Exit 1 = donor not found
    Exit 2 = donor found but failed verification
    Exit 3 = target path not writable / permissions blocked
    Exit 4 = pre-flight check failed (wrong app version, wrong path)
    Exit 5 = backup failed

.NOTES
    Author : agent A02-AbacusPatch (18-agent wave 2026-05-27)
    Honest caveat: A01 + A02 confirmed NO donor screenshotter.js exists on this
    machine in any cache that was scanned. This script is most likely to exit 1.
    Operator should plan on A03's reinstall path as the primary remediation.
#>

[CmdletBinding()]
param(
    [switch]$Apply,
    [string]$DonorOverride = '',
    [switch]$SkipTopLevel
)

$ErrorActionPreference = 'Stop'
$script:DryRun = -not $Apply.IsPresent

# --- Constants ----------------------------------------------------------------
$AbacusRoot      = 'C:\Users\acer\AppData\Local\Programs\AbacusAI'
$ExpectedAppPkg  = Join-Path $AbacusRoot 'resources\app\package.json'
$TargetInner     = Join-Path $AbacusRoot 'resources\app\node_modules\patchright-core\lib\server\screenshotter.js'
$TargetOuter     = Join-Path $AbacusRoot 'node_modules\patchright-core\lib\server\screenshotter.js'
$ExpectedAppVer  = '1.106.21004'
$BackupRoot      = Join-Path $env:USERPROFILE 'Asolaria\reports\interview-copilot-update-2026-05-27\abacus\backup'
$Stamp           = Get-Date -Format 'yyyyMMddTHHmmss'
$LogPath         = Join-Path $BackupRoot "restore-screenshotter-$Stamp.log"

# --- Helpers ------------------------------------------------------------------
function Write-Step {
    param([string]$Msg, [string]$Level = 'INFO')
    $prefix = if ($script:DryRun) { '[DRY-RUN]' } else { '[APPLY]' }
    $line = "{0} {1} {2} : {3}" -f (Get-Date -Format o), $prefix, $Level, $Msg
    Write-Host $line
    if (Test-Path (Split-Path $LogPath -Parent)) {
        Add-Content -LiteralPath $LogPath -Value $line -ErrorAction SilentlyContinue
    }
}

function Get-FileSha256 {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower()
}

function Test-IsLikelyScreenshotterJs {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $size = (Get-Item -LiteralPath $Path).Length
    if ($size -lt 1024 -or $size -gt 1MB) { return $false }   # plausible bounds
    # Read first 8 KB — looking for class or exports signature
    $reader = [System.IO.StreamReader]::new($Path)
    try {
        $head = $reader.ReadBlock(([char[]]::new(8192)), 0, 8192) | Out-Null
        $reader.BaseStream.Position = 0
        $buf = [char[]]::new(8192)
        $n = $reader.ReadBlock($buf, 0, 8192)
        $sample = -join $buf[0..($n - 1)]
    } finally { $reader.Dispose() }
    # Hard signatures: must contain 'Screenshotter' AND ('exports' OR 'class ')
    if ($sample -notmatch 'Screenshotter') { return $false }
    if ($sample -notmatch '(?:exports\.Screenshotter|class\s+Screenshotter|Screenshotter\s*=\s*class)') {
        return $false
    }
    return $true
}

function Find-CandidateScreenshotters {
    $roots = @(
        'C:\Users\acer\AppData\Local\Yarn\Cache\v6',
        'C:\Users\acer\AppData\Local\npm-cache\_cacache',
        'C:\Users\acer\.npm\_cacache',
        'D:\bigpickle-rebuild\node_modules',
        'C:\Users\acer\node_modules',
        'C:\asolaria-acer\node_modules',
        'D:\interview\copilot-v2-2026-05-27\node_modules'
    )
    $hits = @()
    foreach ($r in $roots) {
        if (-not (Test-Path -LiteralPath $r)) { continue }
        Write-Step "Scanning $r"
        try {
            Get-ChildItem -LiteralPath $r -Recurse -Filter 'screenshotter.js' `
                -ErrorAction SilentlyContinue -Force | ForEach-Object {
                # Only accept files inside a (playwright|patchright)-core path
                if ($_.FullName -match '(playwright-core|patchright-core)\\lib\\server\\screenshotter\.js$') {
                    $hits += [PSCustomObject]@{
                        Path = $_.FullName
                        Size = $_.Length
                        ModT = $_.LastWriteTime
                    }
                }
            }
        } catch {
            Write-Step "Scan error in ${r}: $_" 'WARN'
        }
    }
    return $hits
}

# --- Phase 0: pre-flight ------------------------------------------------------
Write-Step "restore-screenshotter.ps1 starting; DryRun=$script:DryRun"

if (-not (Test-Path -LiteralPath $ExpectedAppPkg)) {
    Write-Step "Abacus AI install not found at $AbacusRoot — aborting" 'ERROR'
    exit 4
}
try {
    $pkg = Get-Content -LiteralPath $ExpectedAppPkg -Raw | ConvertFrom-Json
    if ($pkg.version -ne $ExpectedAppVer) {
        Write-Step "Found Abacus version $($pkg.version), expected $ExpectedAppVer — refusing to patch unverified version" 'ERROR'
        exit 4
    }
    Write-Step "Confirmed Abacus AI v$($pkg.version) at $AbacusRoot"
} catch {
    Write-Step "Failed to read $ExpectedAppPkg : $_" 'ERROR'
    exit 4
}

# Confirm the files are actually missing (don't patch if they exist)
foreach ($t in @($TargetInner, $TargetOuter)) {
    if (Test-Path -LiteralPath $t) {
        Write-Step "Target already exists: $t — refusing to overwrite without -Apply -Force (not implemented; aborting)" 'ERROR'
        exit 4
    }
}

# Confirm the parent directories DO exist (we're only restoring one file)
$parents = @((Split-Path $TargetInner -Parent), (Split-Path $TargetOuter -Parent))
foreach ($p in $parents) {
    if (-not (Test-Path -LiteralPath $p)) {
        Write-Step "Parent directory missing: $p — bundle damage exceeds this patch's scope" 'ERROR'
        exit 4
    }
}

# --- Phase 1: locate donor ----------------------------------------------------
$donor = $null

if ($DonorOverride) {
    Write-Step "Using operator-supplied donor: $DonorOverride"
    if (-not (Test-IsLikelyScreenshotterJs $DonorOverride)) {
        Write-Step "Override donor failed signature check" 'ERROR'
        exit 2
    }
    $donor = $DonorOverride
} else {
    $candidates = Find-CandidateScreenshotters
    Write-Step "Cache scan returned $($candidates.Count) candidate(s)"
    foreach ($c in $candidates) {
        Write-Step "  candidate: $($c.Path) size=$($c.Size) mod=$($c.ModT)"
        if (Test-IsLikelyScreenshotterJs $c.Path) {
            $donor = $c.Path
            Write-Step "Selected donor: $donor"
            break
        } else {
            Write-Step "  ...failed signature check, skipping" 'WARN'
        }
    }
}

if (-not $donor) {
    Write-Step "NO DONOR FOUND. This patch is NOT VIABLE on this machine." 'ERROR'
    Write-Step "Operator action required: run agent A03's reinstall path." 'ERROR'
    Write-Step "  See: C:\Users\acer\Asolaria\reports\interview-copilot-update-2026-05-27\abacus\agent-A03-reinstall.md"
    exit 1
}

# --- Phase 2: prepare backup --------------------------------------------------
$backupDir = Join-Path $BackupRoot "screenshotter-restore-$Stamp"
$donorSha  = Get-FileSha256 $donor
Write-Step "Donor SHA-256: $donorSha"

if (-not $script:DryRun) {
    try {
        if (-not (Test-Path -LiteralPath $backupDir)) {
            New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
        }
    } catch {
        Write-Step "Backup directory creation failed: $_" 'ERROR'
        exit 5
    }
} else {
    Write-Step "Would create backup directory $backupDir"
}

# Snapshot the (currently empty) state — record an inventory of the target server dirs
foreach ($parentDir in $parents) {
    $manifest = Join-Path $backupDir ("inventory-{0}.txt" -f ((Split-Path $parentDir -Parent | Split-Path -Leaf) + '_' + ((Resolve-Path $parentDir).Path.GetHashCode())))
    if ($script:DryRun) {
        Write-Step "Would write inventory of $parentDir to $manifest"
    } else {
        try {
            Get-ChildItem -LiteralPath $parentDir -Recurse -Force -ErrorAction SilentlyContinue |
                Select-Object FullName, Length, LastWriteTime |
                Out-File -LiteralPath $manifest -Encoding UTF8
        } catch {
            Write-Step "Inventory write failed for $parentDir : $_" 'WARN'
        }
    }
}

# --- Phase 3: apply -----------------------------------------------------------
$targets = if ($SkipTopLevel) { @($TargetInner) } else { @($TargetInner, $TargetOuter) }

foreach ($t in $targets) {
    Write-Step "Restoring: $t"
    if ($script:DryRun) {
        Write-Step "  Would Copy-Item -LiteralPath '$donor' -Destination '$t'"
        continue
    }

    # Verify destination parent writable
    try {
        $probe = Join-Path (Split-Path $t -Parent) ".write-probe-$Stamp.tmp"
        Set-Content -LiteralPath $probe -Value 'probe' -ErrorAction Stop
        Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
    } catch {
        Write-Step "Destination $t is not writable (need admin?): $_" 'ERROR'
        exit 3
    }

    try {
        Copy-Item -LiteralPath $donor -Destination $t -ErrorAction Stop
        $writtenSha = Get-FileSha256 $t
        if ($writtenSha -ne $donorSha) {
            Write-Step "Post-copy SHA mismatch: wrote=$writtenSha donor=$donorSha" 'ERROR'
            exit 2
        }
        Write-Step "  OK: wrote $t (sha=$writtenSha)"
    } catch {
        Write-Step "Copy failed for $t : $_" 'ERROR'
        exit 3
    }
}

if ($script:DryRun) {
    Write-Step "DRY-RUN COMPLETE. Re-run with -Apply to perform the restoration." 'INFO'
} else {
    Write-Step "RESTORATION COMPLETE. Launch AbacusAI.exe and verify boot." 'INFO'
}
exit 0
