#Requires -Version 5.1
param([string]$ReferencePath = "")
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
. (Join-Path $PSScriptRoot "native.ps1")
try {
    Write-Host "CATER production initialization (no mailbox connection or sending)."
    if (-not (Test-Path "config\production.local.toml")) {
        Copy-Item "config\production.example.toml" "config\production.local.toml"
    }
    $Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $Python)) {
        Write-Host "Installing the Python environment first..."
        $PreviousConfig = $env:CATER_CONFIG
        $PreviousRuntime = $env:CATER_RUNTIME_DIR
        try {
            # The installer's own init must also target PRODUCTION, even if .env selects production.
            $env:CATER_CONFIG = Join-Path $RepoRoot "config\production.local.toml"
            $env:CATER_RUNTIME_DIR = Join-Path $RepoRoot "runtime\production"
            & (Join-Path $PSScriptRoot "install.ps1")
        } finally {
            $env:CATER_CONFIG = $PreviousConfig
            $env:CATER_RUNTIME_DIR = $PreviousRuntime
        }
        if (-not (Test-Path $Python)) { throw "Python installation did not complete." }
    }
    if (-not (Test-Path ".env")) {
        Copy-Item ".env.example" ".env"
        Write-Host "Created .env. Replace sample mailbox values before starting production mail."
    }
    $Base = @("-m", "cater.app.cli", "--config", "config/production.local.toml", "--runtime", "runtime/production")
    Invoke-CaterNative -Description "Initializing production database" -FilePath $Python -Arguments ($Base + @("init"))
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
    Invoke-CaterNative -Description "Importing machine references into PRODUCTION" -FilePath $Python -Arguments ($Base + @("import-machines", $ReferencePath))
    Invoke-CaterNative -Description "Checking PRODUCTION health" -FilePath $Python -Arguments ($Base + @("health", "--json"))
    Invoke-CaterNative -Description "Showing effective PRODUCTION settings" -FilePath $Python -Arguments ($Base + @("settings", "--json"))
    Write-Host "PRODUCTION initialization completed. Existing tasks and settings were preserved."
    Write-Host "Edit .env and config\production.local.toml, then double-click start-production.bat."
    Write-Host "This script does not enable sending or delete existing production tasks."
    exit 0
} catch {
    Write-Host "PRODUCTION initialization failed:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host "Review the error above. Existing data was preserved; review the error before retrying."
    exit 1
}
