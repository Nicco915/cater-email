#Requires -Version 5.1
<#
.SYNOPSIS
    Start the loopback-only admin UI together with the mailbox poller.

.DESCRIPTION
    The web server and the poller share one application service. The server
    binds to 127.0.0.1 by default; do not expose it to the LAN.
    Use -NoPoll for pure offline UI work.

.EXAMPLE
    PowerShell -File scripts\run-web.ps1
    PowerShell -File scripts\run-web.ps1 -NoPoll -Port 8765
#>
param(
    [switch]$NoPoll,
    [int]$Port = 8765,
    [string]$Config = "",
    [string]$Runtime = ""
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
. (Join-Path $PSScriptRoot "native.ps1")
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Virtual environment not found. Run scripts\install.ps1 first."
}

$Arguments = @("-m", "cater.app.cli", "serve", "--port", "$Port")
if ($NoPoll) { $Arguments += "--no-poll" }
if ($Config) { $Arguments += @("--config", $Config) }
if ($Runtime) { $Arguments += @("--runtime", $Runtime) }
Invoke-CaterNative `
    -Description "CATER web UI" `
    -FilePath $Python `
    -Arguments $Arguments `
    -ExitOnFailure
exit 0