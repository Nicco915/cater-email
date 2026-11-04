#Requires -Version 5.1
param([string]$ReferencePath = "")
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
. (Join-Path $PSScriptRoot "native.ps1")
try {
    Write-Host "CATER test initialization (no mailbox connection or sending)."
    if (-not (Test-Path "config\test.local.toml")) {
        Copy-Item "config\test.example.toml" "config\test.local.toml"
    }
    $Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $Python)) {
        Write-Host "Installing the Python environment first..."
        $PreviousConfig = $env:CATER_CONFIG
        $PreviousRuntime = $env:CATER_RUNTIME_DIR
        try {
            # The installer's own init must also target TEST, even if .env selects production.
            $env:CATER_CONFIG = Join-Path $RepoRoot "config\test.local.toml"
            $env:CATER_RUNTIME_DIR = Join-Path $RepoRoot "runtime\test"
            & (Join-Path $PSScriptRoot "install.ps1")
        } finally {
            $env:CATER_CONFIG = $PreviousConfig
            $env:CATER_RUNTIME_DIR = $PreviousRuntime
        }
        if (-not (Test-Path $Python)) { throw "Python installation did not complete." }
    }
    if (-not (Test-Path ".env")) {
        Copy-Item ".env.example" ".env"
        Write-Host "Created .env. Replace sample mailbox values before starting mail tests."
    }
    $Base = @("-m", "cater.app.cli", "--config", "config/test.local.toml", "--runtime", "runtime/test")
    Invoke-CaterNative -Description "Initializing test database" -FilePath $Python -Arguments ($Base + @("init"))
    if (-not $ReferencePath) {
        $Candidates = @(Get-ChildItem -Path "database" -Filter "*.xlsx" -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -notlike '~$*' -and $_.Name -notlike '.~*' })
        if ($Candidates.Count -eq 1) { $ReferencePath = $Candidates[0].FullName }
        else {
            Write-Host "Select the machine reference workbook (Sheet1)."
            Add-Type -AssemblyName System.Windows.Forms
            $Picker = New-Object System.Windows.Forms.OpenFileDialog
            $Picker.Title = "Select CATER machine reference workbook"
            $Picker.Filter = "Excel workbook (*.xlsx)|*.xlsx"
            try {
                if ($Picker.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
                    throw "No workbook selected. Run initialization again when the workbook is available."
                }
                $ReferencePath = $Picker.FileName
            } finally { $Picker.Dispose() }
        }
    }
    if (-not (Test-Path -LiteralPath $ReferencePath -PathType Leaf)) { throw "Reference workbook not found." }
    Invoke-CaterNative -Description "Importing machine references into TEST" -FilePath $Python -Arguments ($Base + @("import-machines", $ReferencePath))
    Invoke-CaterNative -Description "Checking TEST health" -FilePath $Python -Arguments ($Base + @("health", "--json"))
    Invoke-CaterNative -Description "Showing effective TEST settings" -FilePath $Python -Arguments ($Base + @("settings", "--json"))
    Write-Host "TEST initialization completed. Existing tasks and settings were preserved."
    Write-Host "Edit .env and config\test.local.toml, then double-click start-test.bat."
    Write-Host "This script does not enable sending or change production data."
    exit 0
} catch {
    Write-Host "TEST initialization failed:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host "Review the error above. No production data was initialized."
    exit 1
}
