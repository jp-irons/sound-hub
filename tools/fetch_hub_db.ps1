<#
.SYNOPSIS
    Pull just the rows needed for the offline TDOA method comparison
    (tools/validate_toa_real_pulls.py) out of the live sound_hub.db on the
    hub (soundhub.local), without copying the whole database.

.DESCRIPTION
    The live sound_hub.db is ~2.7GB -- almost certainly dominated by
    trigger_events (db.py's own comment notes it reached "18M+ rows /
    2.3GB" with no pruning), not the tiny tdoa_attempts/tdoa_attempt_nodes/
    node_positions tables this comparison actually needs. Rather than
    scp-ing the whole file, this script:
      1. Pushes tools/extract_tdoa_rows.sql to the hub.
      2. Runs it there via sqlite3 against the real sound_hub.db, producing
         a small /tmp/sound_hub_extract.db containing just the filtered
         rows for attempt_17400/attempt_17407/attempt_pied_16-21.
      3. Copies that small extract back down as sound_hub_extract.db.
      4. Cleans up the temp files it left on the hub.

    config/soundhub.conf sets DB_PATH = "sound_hub.db" (a relative path),
    so on the hub it lives wherever the sound-hub server's working
    directory is -- almost certainly the sound-hub checkout root, but the
    exact remote path depends on how it's deployed there. Edit
    $RemoteDbPath below if it's not at the default guess.

.NOTES
    Requires OpenSSH's scp.exe/ssh.exe (built into Windows 10 1809+ /
    Windows 11 by default) and sqlite3 on the remote hub (already a
    sound-hub server dependency, should already be present). If SSH key
    auth isn't already set up for soundhub.local, you'll be prompted for a
    password up to three times (once per scp/ssh call).
#>

$RemoteUser   = "pi"                                  # <-- edit if different
$RemoteHost   = "soundhub.local"
$RemoteDbPath = "~/sound-hub/sound_hub.db"             # <-- edit if it lives elsewhere
$RemoteTmpSql = "/tmp/extract_tdoa_rows.sql"
$RemoteTmpDb  = "/tmp/sound_hub_extract.db"

$LocalSqlPath = Join-Path $PSScriptRoot "extract_tdoa_rows.sql"
$LocalDbPath  = "C:\Users\jon\Workspace\sound-hub\sound_hub_extract.db"

foreach ($cmd in @("scp.exe", "ssh.exe")) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Error "$cmd not found. Install the OpenSSH client: Settings > Apps > Optional Features > OpenSSH Client."
        exit 1
    }
}

Write-Host "1/3 Pushing extraction script to ${RemoteHost}:${RemoteTmpSql} ..."
scp $LocalSqlPath "${RemoteUser}@${RemoteHost}:${RemoteTmpSql}"
if ($LASTEXITCODE -ne 0) { Write-Error "scp (push) failed (exit code $LASTEXITCODE)"; exit 1 }

Write-Host "2/3 Running extraction on the hub (sqlite3 against the real DB) ..."
ssh "${RemoteUser}@${RemoteHost}" "rm -f $RemoteTmpDb && sqlite3 $RemoteDbPath < $RemoteTmpSql"
if ($LASTEXITCODE -ne 0) { Write-Error "ssh (extraction) failed (exit code $LASTEXITCODE)"; exit 1 }

Write-Host "3/3 Fetching small extract db -> $LocalDbPath ..."
scp "${RemoteUser}@${RemoteHost}:${RemoteTmpDb}" $LocalDbPath
if ($LASTEXITCODE -ne 0) { Write-Error "scp (pull) failed (exit code $LASTEXITCODE)"; exit 1 }

ssh "${RemoteUser}@${RemoteHost}" "rm -f $RemoteTmpSql $RemoteTmpDb"

$size = (Get-Item $LocalDbPath).Length
Write-Host "Done. Saved to $LocalDbPath ($size bytes)"
