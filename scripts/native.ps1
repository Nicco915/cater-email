#Requires -Version 5.1
<#
.SYNOPSIS
    Shared native-command guard for the CATER deployment scripts.

.DESCRIPTION
    Windows PowerShell 5.1 does not turn a non-zero native process exit code
    into a terminating error when $ErrorActionPreference is "Stop". Every
    native executable invocation in the scripts\ deployment scripts therefore
    goes through Invoke-CaterNative, which checks $LASTEXITCODE and either
    throws (interactive install/backup) or exits with that exact code
    (long-running web/poller processes).
#>

function Invoke-CaterNative {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Description,

        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [string[]]$Arguments = @(),

        [switch]$ExitOnFailure
    )

    if (-not (Get-Command $FilePath -ErrorAction SilentlyContinue)) {
        $Message = "$Description failed: command '$FilePath' was not found."
        if ($ExitOnFailure) {
            [Console]::Error.WriteLine($Message)
            exit 127
        }
        throw $Message
    }

    & $FilePath @Arguments
    $ExitCode = 0
    if (Test-Path Variable:LASTEXITCODE) {
        $ExitCode = $LASTEXITCODE
    }
    if ($ExitCode -ne 0) {
        $Message = "$Description failed with exit code $ExitCode."
        if ($ExitOnFailure) {
            [Console]::Error.WriteLine($Message)
            exit $ExitCode
        }
        throw $Message
    }
}