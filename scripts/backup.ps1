#Requires -Version 5.1
<#
.SYNOPSIS
    Create and verify a consistent SQLite backup.

.DESCRIPTION
    Writes a backup under runtime\backups and immediately verifies it.
    This script does not stop the service and does not contain credentials.
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
. (Join-Path $PSScriptRoot "native.ps1")
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Virtual environment not found. Run scripts\install.ps1 first."
}

Invoke-CaterNative `
    -Description "Creating the database backup" `
    -FilePath $Python `
    -Arguments @("-m", "cater.app.cli", "backup", "--reason", "scheduled")