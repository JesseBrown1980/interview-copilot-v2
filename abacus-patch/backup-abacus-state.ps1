#requires -Version 5.1
<#
.SYNOPSIS
  Read-only backup of Abacus AI user state ahead of clean reinstall.

.DESCRIPTION
  Agent A03-AbacusReinstall (18-agent wave 2026-05-27).
  Copies (never moves, never deletes) all known Abacus AI per-user state
  directories to D:\safety-backups\abacus-2026-05-27\ so the operator can
  restore after reinstall.

  Sources catalogued:
    - %APPDATA%\AbacusAI                  (~780 MB — Electron/VS Code profile)
    - %APPDATA%\abacusai-cli-nodejs       (CLI endpoint config)
    - %USERPROFILE%\.abacusai             (CLI edit-allowlist)
    - %USERPROFILE%\.abacusai-chromium-profile  (currently empty, captured for completeness)

  Excludes (cache, easily regenerable, bloats backup):
    - Cache, Code Cache, GPUCache, DawnGraphiteCache, DawnWebGPUCache
    - CachedExtensionVSIXs (167 MB single VSIX — re-downloadable)
    - CachedData, CachedConfigurations, CachedProfilesData
    - blob_storage, Shared Dictionary, Service Worker
    - logs, crashes
  These can be opted back in with -IncludeCaches.

  Critical-state preserved (always):
    - User\settings.json, User\snippets\, User\History\
    - User\globalStorage\ (includes state.vscdb — chat history, secret storage)
    - User\workspaceStorage\
    - Local Storage\leveldb (auth cookies / session)
    - Network\Cookies, Network Persistent State, TransportSecurity
    - Preferences, Local State, machineid
    - Backups\ (Abacus' own version backups — small)
    - Agent Workspaces\ (custom agents per memory project_charm — keep)
    - WebStorage, Session Storage

.PARAMETER DestinationRoot
  Override backup root. Default: D:\safety-backups\abacus-2026-05-27

.PARAMETER IncludeCaches
  Include cache directories (large; usually skip).

.PARAMETER WhatIf
  Dry-run; print actions only.

.NOTES
  Read-only by design. Never invokes uninstaller. Never writes inside source.
  Per memory feedback_hermes_era_usb_descriptors_not_mount_2026_05_22.md —
  this is descriptor-style cataloguing + copy, NOT mass mount/sweep.

  Run BEFORE closing Abacus AI for clean DB snapshots; the script will warn
  if AbacusAI.exe is running and offer to continue anyway (live copy may
  miss in-flight writes to state.vscdb).
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$DestinationRoot = 'D:\safety-backups\abacus-2026-05-27',
    [switch]$IncludeCaches,
    [switch]$Force  # skip running-process warning
)

$ErrorActionPreference = 'Stop'
$startedAt = Get-Date

function Write-Step($msg) { Write-Host "[A03-backup] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "[A03-backup] $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "[A03-backup] $msg" -ForegroundColor Yellow }
function Write-Err2($msg) { Write-Host "[A03-backup] $msg" -ForegroundColor Red }

# --- 1. Pre-flight: is AbacusAI.exe running? ---------------------------------
$running = Get-Process -Name 'AbacusAI' -ErrorAction SilentlyContinue
if ($running -and -not $Force) {
    Write-Warn2 "AbacusAI.exe is currently running (PID(s): $($running.Id -join ', '))."
    Write-Warn2 "Live copy may miss in-flight DB writes to state.vscdb."
    Write-Warn2 "Recommended: close Abacus AI (File > Exit), then re-run."
    Write-Warn2 "To proceed anyway: re-run with -Force"
    return
}

# --- 2. Resolve destination --------------------------------------------------
if (-not (Test-Path -LiteralPath $DestinationRoot)) {
    if ($PSCmdlet.ShouldProcess($DestinationRoot, 'Create backup root')) {
        New-Item -ItemType Directory -Path $DestinationRoot -Force | Out-Null
    }
}
$destAppdata    = Join-Path $DestinationRoot 'AppData-Roaming-AbacusAI'
$destCli        = Join-Path $DestinationRoot 'AppData-Roaming-abacusai-cli-nodejs'
$destDot        = Join-Path $DestinationRoot 'home-dot-abacusai'
$destDotChrome  = Join-Path $DestinationRoot 'home-dot-abacusai-chromium-profile'
$manifestPath   = Join-Path $DestinationRoot 'BACKUP-MANIFEST.json'

# --- 3. Source paths ---------------------------------------------------------
$sources = @{
    AppData     = Join-Path $env:APPDATA       'AbacusAI'
    Cli         = Join-Path $env:APPDATA       'abacusai-cli-nodejs'
    DotHome     = Join-Path $env:USERPROFILE   '.abacusai'
    DotChrome   = Join-Path $env:USERPROFILE   '.abacusai-chromium-profile'
}

# Cache-ish subdirs we skip inside AppData\AbacusAI unless -IncludeCaches
$cacheDirs = @(
    'Cache', 'Code Cache', 'GPUCache', 'DawnGraphiteCache', 'DawnWebGPUCache',
    'CachedExtensionVSIXs', 'CachedData', 'CachedConfigurations', 'CachedProfilesData',
    'blob_storage', 'Shared Dictionary', 'Service Worker',
    'logs', 'crashes'
)

# --- 4. Helper: robocopy wrapper --------------------------------------------
function Invoke-SafeCopy {
    param(
        [Parameter(Mandatory)] [string]$Source,
        [Parameter(Mandatory)] [string]$Dest,
        [string[]]$ExcludeDirs = @()
    )
    if (-not (Test-Path -LiteralPath $Source)) {
        Write-Warn2 "Source missing, skipping: $Source"
        return [pscustomobject]@{ Source=$Source; Dest=$Dest; Status='missing'; Bytes=0 }
    }
    if ($PSCmdlet.ShouldProcess($Source, "Copy to $Dest")) {
        $args = @($Source, $Dest, '/E', '/COPY:DAT', '/DCOPY:DAT', '/R:1', '/W:1',
                  '/NP', '/NFL', '/NDL', '/NJH', '/NJS', '/MT:8')
        if ($ExcludeDirs.Count -gt 0) {
            $args += '/XD'
            $args += $ExcludeDirs
        }
        # robocopy exit codes 0-7 are success-ish; >=8 is failure.
        & robocopy.exe @args | Out-Null
        $rc = $LASTEXITCODE
        $bytes = 0
        if (Test-Path -LiteralPath $Dest) {
            $bytes = (Get-ChildItem -LiteralPath $Dest -Recurse -File -ErrorAction SilentlyContinue |
                      Measure-Object -Property Length -Sum).Sum
        }
        if ($rc -ge 8) {
            Write-Err2 "robocopy failed (rc=$rc) for $Source"
            return [pscustomobject]@{ Source=$Source; Dest=$Dest; Status="fail-rc$rc"; Bytes=$bytes }
        }
        Write-Ok "Copied $Source  ->  $Dest  ($([math]::Round($bytes/1MB,2)) MB)"
        return [pscustomobject]@{ Source=$Source; Dest=$Dest; Status='ok'; Bytes=$bytes }
    }
}

# --- 5. Execute copies -------------------------------------------------------
Write-Step "Destination root: $DestinationRoot"
Write-Step "IncludeCaches:    $IncludeCaches"
$results = @()

$excl = if ($IncludeCaches) { @() } else { $cacheDirs }
$results += Invoke-SafeCopy -Source $sources.AppData   -Dest $destAppdata   -ExcludeDirs $excl
$results += Invoke-SafeCopy -Source $sources.Cli       -Dest $destCli
$results += Invoke-SafeCopy -Source $sources.DotHome   -Dest $destDot
$results += Invoke-SafeCopy -Source $sources.DotChrome -Dest $destDotChrome

# --- 6. Hash the critical state.vscdb files for integrity check --------------
$criticalFiles = @(
    'AppData-Roaming-AbacusAI\User\globalStorage\state.vscdb',
    'AppData-Roaming-AbacusAI\User\globalStorage\state.vscdb.backup',
    'AppData-Roaming-AbacusAI\User\settings.json',
    'AppData-Roaming-AbacusAI\Preferences',
    'AppData-Roaming-AbacusAI\Local State',
    'AppData-Roaming-AbacusAI\machineid',
    'AppData-Roaming-AbacusAI\Network\Cookies',
    'AppData-Roaming-abacusai-cli-nodejs\Config\storage.json',
    'home-dot-abacusai\config.json'
)
$hashes = @()
foreach ($rel in $criticalFiles) {
    $full = Join-Path $DestinationRoot $rel
    if (Test-Path -LiteralPath $full) {
        try {
            $h = Get-FileHash -LiteralPath $full -Algorithm SHA256
            $hashes += [pscustomobject]@{ relative=$rel; sha256=$h.Hash; bytes=(Get-Item -LiteralPath $full).Length }
        } catch {
            $hashes += [pscustomobject]@{ relative=$rel; sha256='hash-error'; bytes=-1 }
        }
    }
}

# --- 7. Write manifest -------------------------------------------------------
$manifest = [pscustomobject]@{
    agent         = 'A03-AbacusReinstall'
    wave          = '18-agent-2026-05-27'
    operator      = 'OP-JESSE'
    started_at    = $startedAt.ToString('o')
    finished_at   = (Get-Date).ToString('o')
    destination   = $DestinationRoot
    include_caches= [bool]$IncludeCaches
    sources       = $sources
    excluded_dirs = $excl
    copy_results  = $results
    critical_file_hashes = $hashes
    abacus_install_path  = 'C:\Users\acer\AppData\Local\Programs\AbacusAI'
    abacus_uninstaller   = 'C:\Users\acer\AppData\Local\Programs\AbacusAI\unins000.exe'
    abacus_appid         = '{ABEC3C4B-6586-4DB9-976F-004FB65C3380}_is1'
    download_url         = 'https://abacusai-desktop.abacus.ai/'
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
Write-Ok "Manifest written: $manifestPath"

# --- 8. Summary --------------------------------------------------------------
$totalMB = [math]::Round((($results | Measure-Object -Property Bytes -Sum).Sum)/1MB, 2)
Write-Step "Done. Total backed up: $totalMB MB in $((Get-Date) - $startedAt)"
Write-Step "Next step: see agent-A03-reinstall.md POST-INSTALL section to restore."
