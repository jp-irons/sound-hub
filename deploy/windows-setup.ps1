# Sound Hub — Windows host setup
#
# Run this script once on the NUC as Administrator:
#   Right-click PowerShell → "Run as administrator"
#   cd C:\path\to\sound-hub
#   .\deploy\windows-setup.ps1
#
# What it does:
#   1. Installs WSL2 + Ubuntu (if not already installed)
#   2. Adds mirrored networking to .wslconfig (makes WSL2 ports visible on LAN)
#   3. Creates a Task Scheduler task to keep WSL2 running after logon
#   4. Opens Windows Firewall for ports 80 (SPA/API) and 8000 (node audio push)

#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host "`n▶ $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "  ✓ $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  ⚠  $msg" -ForegroundColor Yellow }

Write-Host ""
Write-Host "╔══════════════════════════════════════════════╗" -ForegroundColor White
Write-Host "║    Sound Hub — Windows host configuration    ║" -ForegroundColor White
Write-Host "╚══════════════════════════════════════════════╝" -ForegroundColor White

# ── 1. WSL2 + Ubuntu ──────────────────────────────────────────────────────────
Write-Step "Checking WSL2 installation..."

$wslInstalled = (wsl --list --quiet 2>$null) -match "Ubuntu"

if (-not $wslInstalled) {
    Write-Warn "Ubuntu not found — installing WSL2 + Ubuntu (requires reboot)..."
    wsl --install --distribution Ubuntu
    Write-Warn "Reboot required after WSL2 installation."
    Write-Warn "After rebooting, open Ubuntu from the Start menu to finish setup,"
    Write-Warn "then re-run this script."
    exit 0
} else {
    Write-OK "WSL2 Ubuntu already installed."
}

# ── 2. Mirrored networking (.wslconfig) ───────────────────────────────────────
Write-Step "Configuring WSL2 mirrored networking..."

$wslConfigPath = "$env:USERPROFILE\.wslconfig"
$mirroredBlock = "[wsl2]`r`nnetworkingMode=mirrored"

if (Test-Path $wslConfigPath) {
    $content = Get-Content $wslConfigPath -Raw

    if ($content -match "networkingMode\s*=") {
        if ($content -match "networkingMode\s*=\s*mirrored") {
            Write-OK ".wslconfig already has networkingMode=mirrored."
        } else {
            Write-Warn ".wslconfig has a networkingMode setting that is NOT mirrored."
            Write-Warn "Edit $wslConfigPath manually and set networkingMode=mirrored"
            Write-Warn "under the [wsl2] section, then run: wsl --shutdown"
        }
    } elseif ($content -match "\[wsl2\]") {
        # [wsl2] section exists but no networkingMode — append the setting
        $updated = $content -replace "(\[wsl2\])", "`$1`r`nnetworkingMode=mirrored"
        Set-Content $wslConfigPath $updated -NoNewline
        Write-OK "Added networkingMode=mirrored to existing [wsl2] section."
        Write-Warn "Run 'wsl --shutdown' then re-open Ubuntu to apply."
    } else {
        # No [wsl2] section — append the block
        Add-Content $wslConfigPath "`r`n$mirroredBlock"
        Write-OK "Appended [wsl2] networkingMode=mirrored to .wslconfig."
        Write-Warn "Run 'wsl --shutdown' then re-open Ubuntu to apply."
    }
} else {
    Set-Content $wslConfigPath $mirroredBlock
    Write-OK "Created $wslConfigPath with networkingMode=mirrored."
    Write-Warn "Run 'wsl --shutdown' then re-open Ubuntu to apply."
}

# ── 3. Task Scheduler — keep WSL2 running after logon ────────────────────────
Write-Step "Creating WSL2 auto-start scheduled task..."

$taskName = "WSL2 Ubuntu — Sound Hub keepalive"

# Remove stale task if it exists
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

$action   = New-ScheduledTaskAction `
                -Execute "wsl.exe" `
                -Argument "-d Ubuntu -- sleep infinity"

$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
                -ExecutionTimeLimit ([TimeSpan]::Zero) `
                -MultipleInstances  IgnoreNew `
                -Hidden

$principal = New-ScheduledTaskPrincipal `
                -UserId   $env:USERNAME `
                -LogonType Interactive `
                -RunLevel Limited

Register-ScheduledTask `
    -TaskName  $taskName `
    -Action    $action `
    -Trigger   $trigger `
    -Settings  $settings `
    -Principal $principal `
    -Force | Out-Null

Write-OK "Scheduled task '$taskName' created (triggers at logon for $env:USERNAME)."

# ── 4. Windows Firewall rules ─────────────────────────────────────────────────
Write-Step "Opening Windows Firewall..."

$firewallRules = @(
    @{ Name = "Sound Hub — HTTP (port 80)";          Port = 80   },
    @{ Name = "Sound Hub — node audio push (port 8000)"; Port = 8000 }
)

foreach ($rule in $firewallRules) {
    $existing = Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-OK "Firewall rule already exists: $($rule.Name)"
    } else {
        New-NetFirewallRule `
            -DisplayName $rule.Name `
            -Direction   Inbound `
            -Protocol    TCP `
            -LocalPort   $rule.Port `
            -Action      Allow | Out-Null
        Write-OK "Created firewall rule: $($rule.Name) (TCP $($rule.Port))"
    }
}

# ── Done ──────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "✓ Windows host configuration complete." -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor White
Write-Host "  1. Run 'wsl --shutdown' then re-open Ubuntu (applies mirrored networking)"
Write-Host "  2. Inside Ubuntu, run:  bash ~/sound-hub/deploy/setup.sh"
Write-Host "  3. Edit ~/sound-hub/config/soundhub.conf — set BASE_STATION_IP"
Write-Host "  4. sudo systemctl restart soundhub"
Write-Host ""
Write-Host "  The app will then be available at http://<NUC-LAN-IP>" -ForegroundColor Cyan
Write-Host ""
