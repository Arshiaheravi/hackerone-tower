[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidatePattern('^[A-Za-z0-9._-]{1,100}$')]
    [string]$TaskName = 'HackerOneTower'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$expectedExecutable = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$task = Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "Scheduled task '$TaskName' is not installed."
    return
}

$currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$expectedOwners = @($currentIdentity.Name, $currentIdentity.User.Value)
if (
    $task.Principal.UserId -notin $expectedOwners -or
    @($task.Actions).Count -ne 1 -or
    $task.Actions[0].Execute -ne $expectedExecutable -or
    $task.Actions[0].Arguments -ne '-m tower run' -or
    $task.Actions[0].WorkingDirectory -ne $projectRoot
) {
    throw "Task '$TaskName' does not match this user's project installation. Inspect it in Task Scheduler."
}

if ($PSCmdlet.ShouldProcess($TaskName, 'Stop and unregister this project scheduled task')) {
    if ($task.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $TaskName -TaskPath '\'
    }
    Unregister-ScheduledTask -TaskName $TaskName -TaskPath '\' -Confirm:$false
    Write-Host "Removed '$TaskName'. The database, logs, configuration, and virtual environment are preserved."
}
