[CmdletBinding()]
param(
    [string]$PythonExecutable = 'python',
    [switch]$Development
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $projectRoot '.venv'
$venvPython = Join-Path $venvPath 'Scripts\python.exe'
$environmentPath = Join-Path $projectRoot '.env'

Push-Location -LiteralPath $projectRoot
try {
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        if (Test-Path -LiteralPath $venvPath) {
            throw 'The existing .venv is incomplete. Inspect it before choosing a replacement environment.'
        }
        $bootstrapPython = (Get-Command -Name $PythonExecutable -CommandType Application).Source
        & $bootstrapPython -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'
        if ($LASTEXITCODE -ne 0) {
            throw 'Python 3.11 or newer is required. Pass its executable using -PythonExecutable.'
        }
        & $bootstrapPython -m venv $venvPath
        if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
    }

    & $venvPython -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'
    if ($LASTEXITCODE -ne 0) { throw 'The project virtual environment needs Python 3.11 or newer.' }

    $requirementsFile = if ($Development) { 'requirements-dev.txt' } else { 'requirements.txt' }
    & $venvPython -m pip install --disable-pip-version-check --requirement $requirementsFile
    if ($LASTEXITCODE -ne 0) { throw 'Pinned dependency installation failed.' }
    & $venvPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Installed dependency compatibility check failed.' }

    if (-not (Test-Path -LiteralPath $environmentPath)) {
        Copy-Item -LiteralPath (Join-Path $projectRoot '.env.example') -Destination $environmentPath
        Write-Host 'Created .env. Enter replacement HackerOne credentials and your Telegram settings locally.'
    } else {
        Write-Host 'Preserved the existing .env file.'
    }
    Write-Host 'Setup complete. Run .\.venv\Scripts\python.exe -m tower check, then .\scripts\start.ps1.'
    Write-Host 'Setup does not start the monitor or register a scheduled task.'
} finally {
    Pop-Location
}
