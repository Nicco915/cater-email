#Requires -Version 5.1
<#
.SYNOPSIS
    Run the mailbox poller continuously without the web UI.

.DESCRIPTION
    Intended for Task Scheduler when the web UI is not needed. Polling and
    live sending still honour the configured allowlist and dry-run switches.
    The first scan only establishes a cursor unless -History is supplied.

.EXAMPLE
    PowerShell -File scripts\run-poller.ps1
    PowerShell -File scripts\run-poller.ps1 -History
#>
param(
    [switch]$History,
    [int]$Interval = 0
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

$Arguments = @("-m", "cater.app.cli", "poll", "--loop")
if ($History) { $Arguments += "--history" }
if ($Interval -gt 0) { $Arguments += @("--interval", "$Interval") }
Invoke-CaterNative `
    -Description "CATER mailbox poller" `
    -FilePath $Python `
    -Arguments $Arguments `
    -ExitOnFailure
exit 0