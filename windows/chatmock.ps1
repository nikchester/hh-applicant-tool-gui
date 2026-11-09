param([Parameter(Mandatory = $true)][ValidateSet('login', 'serve')][string]$Action)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo 'venvs\chatmock\Scripts\python.exe'
$env:CHATGPT_LOCAL_HOME = Join-Path $HOME '.local\state\hh-applicant-tool-cli\chatmock'
$env:CODEX_HOME = $env:CHATGPT_LOCAL_HOME
if (-not (Test-Path -LiteralPath $python)) { throw 'Run windows/bootstrap.ps1 first.' }
# Move pre-rename PocketNode profile folders once; refuses to merge or delete.
& $python -I (Join-Path $PSScriptRoot 'profile_migration.py')
if ($LASTEXITCODE -ne 0) { throw 'Legacy profile migration failed.' }
# Call the entry point directly: pip .exe launchers break when the venv is moved.
$chatmock = @('-c', "import sys; from chatmock.cli import main; sys.argv[0] = 'chatmock'; main()")

if ($Action -eq 'login') {
    New-Item -ItemType Directory -Force -Path $env:CHATGPT_LOCAL_HOME | Out-Null
    if (Test-Path -LiteralPath (Join-Path $env:CHATGPT_LOCAL_HOME 'auth.json')) {
        throw 'A separate ChatMock login exists; refusing to overwrite it.'
    }
    & $python @chatmock login --headless
} else {
    $auth = Join-Path $env:CHATGPT_LOCAL_HOME 'auth.json'
    if (-not (Test-Path -LiteralPath $auth)) { throw 'ChatMock login required first.' }
    $data = Get-Content -LiteralPath $auth -Raw | ConvertFrom-Json
    foreach ($name in @('access_token', 'refresh_token', 'id_token')) {
        if (-not $data.tokens.$name) { throw 'Incomplete ChatMock login.' }
    }
    & $python @chatmock serve --host 127.0.0.1 --port 8000 --reasoning-summary none --reasoning-compat legacy --no-enable-web-search --no-fast-mode
}
if ($LASTEXITCODE -ne 0) { throw "ChatMock exited with code $LASTEXITCODE" }
