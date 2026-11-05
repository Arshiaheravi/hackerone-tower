[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$environmentPath = Join-Path $projectRoot '.env'
if (-not (Test-Path -LiteralPath $environmentPath)) {
    Copy-Item -LiteralPath (Join-Path $projectRoot '.env.example') -Destination $environmentPath
}
$botSecret = Read-Host 'Paste the Telegram BotFather token (input is hidden)' -AsSecureString
$secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($botSecret)
try {
    $botToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
    if ($botToken -notmatch '^\d+:[A-Za-z0-9_-]+$') { throw 'Invalid Telegram token format.' }
    $channelId = Read-Host 'Channel @username or numeric -100... ID (bot must be an admin)'
    if ($channelId -notmatch '^(?:-\d+|@[A-Za-z][A-Za-z0-9_]{4,})$') {
        throw 'Enter a channel @username or negative numeric ID.'
    }
    $configText = [IO.File]::ReadAllText($environmentPath)
    foreach ($entry in @{'TELEGRAM_BOT_TOKEN'=$botToken; 'TELEGRAM_CHANNEL_ID'=$channelId}.GetEnumerator()) {
        $linePattern = '(?m)^' + $entry.Key + '=.*$'
        if ([regex]::IsMatch($configText, $linePattern)) {
            $configText = [regex]::Replace($configText, $linePattern, ($entry.Key + '=' + $entry.Value))
        } else {
            $configText += [Environment]::NewLine + $entry.Key + '=' + $entry.Value
        }
    }
    [IO.File]::WriteAllText($environmentPath, $configText, (New-Object Text.UTF8Encoding($false)))
    Write-Host 'Telegram configuration saved in ignored .env. No messages have been sent.'
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer)
    $botSecret.Dispose()
    $botToken = $null
    $configText = $null
}
