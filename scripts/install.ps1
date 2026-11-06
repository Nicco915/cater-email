#Requires -Version 5.1
<#
.SYNOPSIS
    Install CATER into a local virtual environment.

.DESCRIPTION
    Creates .venv, installs the project in editable mode, copies the example
    configuration on first run, then initialises the runtime database.
    This script never writes mail credentials and never sends e-mail.

.EXAMPLE
    PowerShell -ExecutionPolicy Bypass -File scripts\install.ps1
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
. (Join-Path $PSScriptRoot "native.ps1")

# CATER is deployed from a source checkout only. The core resolves its Excel
# templates relative to the repository root, so a normal (non-editable) wheel
# install cannot find templates/ and must not be used.
$Required = @(
    "src\cater\core\api.py",
    "templates\parts_ledger.xlsx",
    "templates\machine_materials.xlsx",
    "templates\machine_comm.xlsx"
)
$Missing = @($Required | Where-Object { -not (Test-Path $_) })
if ($Missing.Count -gt 0) {
    throw ("CATER requires a full source checkout with the repository templates. " +
        "Missing: " + ($Missing -join ", ") + ". " +
        "Do not deploy a normal wheel; use this checkout and editable install, " +
        "or set CATER_TEMPLATE_DIR to a directory containing the three template files.")
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment .venv ..."
    Invoke-CaterNative `
        -Description "Creating the virtual environment" `
        -FilePath "py" `
        -Arguments @("-3", "-m", "venv", ".venv")
}

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Virtual environment creation did not produce .venv\Scripts\python.exe."
}
Invoke-CaterNative `
    -Description "Upgrading pip" `
    -FilePath $Python `
    -Arguments @("-m", "pip", "install", "--upgrade", "pip")
Invoke-CaterNative `
    -Description "Installing CATER in editable mode" `
    -FilePath $Python `
    -Arguments @("-m", "pip", "install", "-e", ".[dev]")
# Explicit runtime dependency check: a missing xlrd must fail the install
# loudly instead of surfacing later as a false "missing attachment" on .xls.
Invoke-CaterNative `
    -Description "Verifying .xls parsing dependency (xlrd)" `
    -FilePath $Python `
    -Arguments @("-c", "import xlrd")

if (-not (Test-Path "config\config.toml")) {
    Copy-Item "config\config.example.toml" "config\config.toml"
    Write-Host "Created config\config.toml from the example."
}

Invoke-CaterNative `
    -Description "Initialising the CATER runtime" `
    -FilePath $Python `
    -Arguments @("-m", "cater.app.cli", "init")
Write-Host ""
Write-Host "Installed. Next steps:"
Write-Host "  1. Edit config\config.toml (account, IMAP/SMTP hosts, allowlists)."
Write-Host "  2. Store passwords:"
Write-Host "       .venv\Scripts\python.exe -m cater.app.cli credentials set --service imap"
Write-Host "       .venv\Scripts\python.exe -m cater.app.cli credentials set --service smtp"
Write-Host "  3. Keep dry_run = true and run offline samples first."
Write-Host "  4. Start the local UI: scripts\run-web.ps1"