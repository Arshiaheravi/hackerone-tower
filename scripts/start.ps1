[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$towerPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$environmentPath = Join-Path $projectRoot '.env'

if (-not (Test-Path -LiteralPath $towerPython -PathType Leaf)) {
    throw 'Project Python is missing. Run scripts\setup.ps1 first.'
}
if (-not (Test-Path -LiteralPath $environmentPath -PathType Leaf)) {
    throw 'The .env file is missing. Run scripts\setup.ps1 and configure credentials locally.'
}

Push-Location -LiteralPath $projectRoot
try {
    & $towerPython -m tower --env-file $environmentPath run
    $towerExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $towerExitCode
