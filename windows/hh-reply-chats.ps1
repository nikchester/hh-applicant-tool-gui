$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo 'venvs\hh\Scripts\python.exe'
$env:CONFIG_DIR = Join-Path $HOME '.config\hh-applicant-tool-cli'
$env:HH_PROFILE_ID = '.'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run windows/bootstrap.ps1 first.' }
# Move pre-rename PocketNode profile folders once; refuses to merge or delete.
& $python -I (Join-Path $PSScriptRoot 'profile_migration.py')
if ($LASTEXITCODE -ne 0) { throw 'Legacy profile migration failed.' }
. (Join-Path $PSScriptRoot 'ensure-chatmock.ps1')

$previousEncoding = [Console]::OutputEncoding
$previousPythonEncoding = $env:PYTHONIOENCODING
$previousChatHome = $env:CHATGPT_LOCAL_HOME
$previousCodexHome = $env:CODEX_HOME
$ownedGateway = $null
$runSucceeded = $false
$runMutex = New-Object System.Threading.Mutex($false, 'Local\hh-applicant-tool-gui-run')
$runLocked = $false
try {
    if (-not $runMutex.WaitOne(0)) { throw 'Another HH run is active. Finish it before starting this one.' }
    $runLocked = $true
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $env:PYTHONIOENCODING = 'utf-8'
    $ownedGateway = Start-ChatMockForHH
    & $python (Join-Path $repo 'windows\reply_chats.py')
    $exitCode = $LASTEXITCODE
    $runSucceeded = $exitCode -eq 0
} finally {
    try {
        Stop-ChatMockForHH $ownedGateway -KeepLogs:(-not $runSucceeded)
    } finally {
        if ($runLocked) { $runMutex.ReleaseMutex() }
        $runMutex.Dispose()
        [Console]::OutputEncoding = $previousEncoding
        $env:PYTHONIOENCODING = $previousPythonEncoding
        $env:CHATGPT_LOCAL_HOME = $previousChatHome
        $env:CODEX_HOME = $previousCodexHome
    }
}
if ($exitCode -ne 0) { throw "HH chat reply stopped (exit $exitCode); check HH before retrying" }
