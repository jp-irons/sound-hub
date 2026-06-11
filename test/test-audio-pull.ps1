# test-audio-pull.ps1
# Exercise the audio pull pipeline end-to-end.
#
# Usage:
#   .\test-audio-pull.ps1                         # hub-mediated (new path)
#   .\test-audio-pull.ps1 -Direct                 # direct broker relay (old path)
#   .\test-audio-pull.ps1 -NodeId soundcapture-ed5de4
#   .\test-audio-pull.ps1 -Direct -BrokerIp 192.168.101.2 -TargetMac "20:6e:f1:b2:8d:90"

param(
    [switch]$Direct,

    # Hub-mediated path
    [string]$NodeId   = "soundcapture-ed5de4",   # hub registry ID - check GET /api/nodes
    [string]$HubUrl   = "http://localhost:8000",

    # Direct path (mirrors existing broker-relay test)
    [string]$BrokerIp  = "192.168.101.2",
    [string]$TargetMac = "20:6e:f1:b2:8d:90",
    [string]$HubIp     = "192.168.101.220",
    [int]$HubPort      = 8000,

    # Direct path only - hub assigns its own ID in hub-mediated mode
    [int]$RequestId  = (Get-Random -Minimum 1 -Maximum 2147483647),
    [int]$PollSecs   = 15    # how long to poll for result
)

$t      = [long]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) * 1000L
$tStart = $t - 30000000
$tEnd   = $t - 5000000

$tStartFmt = [DateTimeOffset]::FromUnixTimeMilliseconds($tStart / 1000).ToString('HH:mm:ss.fff')
$tEndFmt   = [DateTimeOffset]::FromUnixTimeMilliseconds($tEnd   / 1000).ToString('HH:mm:ss.fff')
$mode      = if ($Direct) { 'DIRECT (broker relay)' } else { 'HUB-MEDIATED' }

Write-Host ""
$reqLabel = if ($Direct) { $RequestId } else { "assigned by hub" }

Write-Host "--- Audio pull test ---" -ForegroundColor Cyan
Write-Host "  Mode      : $mode"
Write-Host "  tStart    : $tStart  [$tStartFmt UTC]"
Write-Host "  tEnd      : $tEnd  [$tEndFmt UTC]"
Write-Host "  RequestId : $reqLabel"
Write-Host ""

if ($Direct) {
    # Direct broker relay (original test path)
    $body = @{
        requestId = $RequestId
        targetMac = $TargetMac
        tStartUs  = $tStart
        tEndUs    = $tEnd
        hubIp     = $HubIp
        hubPort   = $HubPort
    } | ConvertTo-Json -Compress

    [System.IO.File]::WriteAllText("$PWD\body.json", $body, [System.Text.Encoding]::UTF8)

    Write-Host "-> POST https://$BrokerIp/espnow/relay" -ForegroundColor Yellow
    curl.exe -k -s -X POST "https://$BrokerIp/espnow/relay" `
        -H "Content-Type: application/json" -d "@body.json" | Write-Host
    Write-Host ""

} else {
    # Hub-mediated path (new endpoint)
    $body = @{
        tStartUs = $tStart
        tEndUs   = $tEnd
    } | ConvertTo-Json -Compress
    [System.IO.File]::WriteAllText("$PWD\body.json", $body, [System.Text.Encoding]::UTF8)

    Write-Host "-> POST $HubUrl/api/nodes/$NodeId/sample" -ForegroundColor Yellow
    $resp = curl.exe -s -X POST "$HubUrl/api/nodes/$NodeId/sample" `
        -H "Content-Type: application/json" -d "@body.json"

    Write-Host "  $resp"
    $parsed = $resp | ConvertFrom-Json -ErrorAction SilentlyContinue
    if ($parsed.requestId) { $RequestId = $parsed.requestId }
    Write-Host ""
}

# Poll for result
Write-Host "Polling $HubUrl/api/audio/requests/$RequestId ..." -ForegroundColor Cyan
$deadline = (Get-Date).AddSeconds($PollSecs)
$done     = $false
$parsed   = $null

while ((Get-Date) -lt $deadline -and -not $done) {
    Start-Sleep -Seconds 2
    $result = curl.exe -s "$HubUrl/api/audio/requests/$RequestId"
    $parsed = $result | ConvertFrom-Json -ErrorAction SilentlyContinue

    $latestStatus = ($parsed.acks | Select-Object -Last 1).status
    $file         = $parsed.file
    $bytes        = $parsed.bytes

    Write-Host "  acks: $($parsed.acks.Count)  latest: $latestStatus  file: $file  bytes: $bytes"

    if ($latestStatus -in @("done", "unavailable", "error")) { $done = $true }
}

Write-Host ""
if ($parsed -and $parsed.file) {
    $f = $parsed.file
    $b = $parsed.bytes
    Write-Host "[OK] WAV saved: audio/$f  [$b bytes]" -ForegroundColor Green
} elseif ($latestStatus -eq "unavailable") {
    Write-Host "[UNAVAILABLE] Node has no audio for that time range" -ForegroundColor Red
    Write-Host "  Check: GPS locked? Audio chunks on SD? tStart/tEnd within stored window?"
} elseif (-not $done) {
    Write-Host "[TIMEOUT] No result within $PollSecs s" -ForegroundColor Yellow
} else {
    Write-Host "[ERROR] Check hub and node logs" -ForegroundColor Red
}
Write-Host ""
