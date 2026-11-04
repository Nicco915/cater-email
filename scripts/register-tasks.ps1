#Requires -Version 5.1
<#
.SYNOPSIS
    Register Windows Task Scheduler entries for CATER.

.DESCRIPTION
    CATER-App       Starts the loopback web UI + poller at user logon and
                    restarts it if the process exits unexpectedly.
    CATER-Backup    Creates and verifies a database backup daily at 02:00.

    Run this script from an elevated PowerShell prompt if your policy requires
    it. It does not store mail passwords.

.PARAMETER Remove
    Unregister both scheduled tasks instead of creating them.

.EXAMPLE
    PowerShell -ExecutionPolicy Bypass -File scripts\register-tasks.ps1
    PowerShell -ExecutionPolicy Bypass -File scripts\register-tasks.ps1 -Remove
#>
param(
    [switch]$Remove
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# This script only calls ScheduledTasks cmdlets. With ErrorActionPreference =
# "Stop", cmdlet failures are terminating, so no native exit-code guard is
# needed here. scripts\native.ps1 covers native calls in the other scripts.

$RepoRoot = Split-Path -Parent $PSScriptRoot
$AppTaskName = "CATER-App"
$BackupTaskName = "CATER-Backup"

if ($Remove) {
    foreach ($Name in @($AppTaskName, $BackupTaskName)) {
        $Existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
        if ($Existing) {
            Unregister-ScheduledTask -TaskName $Name -Confirm:$false
            Write-Host "Removed scheduled task $Name."
        }
    }
    return
}

$PowerShell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$RunWeb = Join-Path $PSScriptRoot "run-web.ps1"
$Backup = Join-Path $PSScriptRoot "backup.ps1"

$AppAction = New-ScheduledTaskAction `
    -Execute $PowerShell `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$RunWeb`"" `
    -WorkingDirectory $RepoRoot

$AppTrigger = New-ScheduledTaskTrigger -AtLogOn
$AppSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
    -TaskName $AppTaskName `
    -Action $AppAction `
    -Trigger $AppTrigger `
    -Settings $AppSettings `
    -Description "CATER local admin UI and mailbox poller (loopback only)" `
    -Force | Out-Null

$BackupAction = New-ScheduledTaskAction `
    -Execute $PowerShell `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Backup`"" `
    -WorkingDirectory $RepoRoot
$BackupTrigger = New-ScheduledTaskTrigger -Daily -At 02:00
$BackupSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName $BackupTaskName `
    -Action $BackupAction `
    -Trigger $BackupTrigger `
    -Settings $BackupSettings `
    -Description "CATER daily verified database backup" `
    -Force | Out-Null

Write-Host "Registered $AppTaskName (at logon) and $BackupTaskName (daily 02:00)."
Write-Host "Inspect with: Get-ScheduledTask -TaskName 'CATER-*'"
Write-Host "Start now:    Start-ScheduledTask -TaskName '$AppTaskName'"