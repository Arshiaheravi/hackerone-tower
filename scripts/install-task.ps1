[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidatePattern('^[A-Za-z0-9._-]{1,100}$')]
    [string]$TaskName = 'HackerOneTower',
    [switch]$StartNow
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$towerPythonw = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$environmentPath = Join-Path $projectRoot '.env'

if (-not (Test-Path -LiteralPath $towerPythonw -PathType Leaf)) {
    throw 'Project pythonw.exe is missing. Run scripts\setup.ps1 first.'
}
if (-not (Test-Path -LiteralPath $environmentPath -PathType Leaf)) {
    throw 'The .env file is missing. Configure credentials locally before installing the task.'
}
if (Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction SilentlyContinue) {
    throw "Scheduled task '$TaskName' already exists. Inspect it or choose a different -TaskName."
}

$currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $currentIdentity.Name -LogonType Interactive -RunLevel Limited
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentIdentity.Name
$logonTrigger.Id = 'TowerLogon'
# A separate time trigger also recovers exits that Windows does not classify as
# restart-on-failure events. Omitted repetition duration means repeat indefinitely.
$recoveryTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1)
$recoveryTrigger.Id = 'TowerRecovery'
$taskAction = New-ScheduledTaskAction -Execute $towerPythonw -Argument '-m tower run' -WorkingDirectory $projectRoot
$taskSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

if ($PSCmdlet.ShouldProcess($TaskName, 'Register current-user HackerOne Tower logon and recovery task')) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -TaskPath '\' `
        -Action $taskAction `
        -Trigger @($logonTrigger, $recoveryTrigger) `
        -Principal $taskPrincipal `
        -Settings $taskSettings `
        -Description "Public HackerOne metadata monitor from $projectRoot. Requires the user to remain logged in and the computer awake." |
        Out-Null
    if ($StartNow) {
        Start-ScheduledTask -TaskName $TaskName -TaskPath '\'
        Write-Host "Registered and started '$TaskName'. Check application logs and python -m tower health."
    } else {
        Write-Host "Registered '$TaskName'. The recovery trigger begins in one minute; logon also triggers a start."
    }
    Write-Host 'The task uses pythonw.exe without a console; application logs are written to TOWER_LOG_DIR.'
    Write-Host 'No password is stored and no elevation is requested. The task continues on battery power.'
    Write-Host 'Recovery attempts repeat every minute. IgnoreNew leaves an existing task instance running.'
    Write-Host 'You must remain logged in, online, and awake. Sleep settings have not been changed.'
}
