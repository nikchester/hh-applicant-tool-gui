param([switch]$Check)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$hh = Join-Path $repo 'venvs\hh\Scripts\python.exe'
$chatmock = Join-Path $repo 'venvs\chatmock\Scripts\python.exe'
$configDir = Join-Path $HOME '.config\hh-applicant-tool-cli'
$config = Join-Path $configDir 'config.toml'

function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe failed (exit $LASTEXITCODE)" }
}

if (-not $Check) {
    # Move pre-rename PocketNode profile folders once; refuses to merge or delete.
    Invoke-Checked 'py' @('-3.12', '-I', (Join-Path $PSScriptRoot 'profile_migration.py'))
    New-Item -ItemType Directory -Force -Path (Join-Path $repo 'venvs'), $configDir | Out-Null
    if (-not (Test-Path -LiteralPath $hh)) {
        Invoke-Checked 'py' @('-3.12', '-m', 'venv', (Join-Path $repo 'venvs\hh'))
    }
    if (-not (Test-Path -LiteralPath $chatmock)) {
        Invoke-Checked 'py' @('-3.12', '-m', 'venv', (Join-Path $repo 'venvs\chatmock'))
    }
    Invoke-Checked $hh @('-m', 'pip', 'install', '-r', (Join-Path $repo 'windows\requirements-hh.txt'))
    Invoke-Checked $chatmock @('-m', 'pip', 'install', '-r', (Join-Path $repo 'windows\requirements-chatmock.txt'))
    if (-not (Test-Path -LiteralPath $config)) {
        Copy-Item -LiteralPath (Join-Path $repo 'windows\config.toml') -Destination $config
    } else {
        Write-Output 'Existing config.toml preserved; review repository changes manually.'
    }
}

if (-not (Test-Path -LiteralPath $config)) { throw 'Missing config.toml' }
Invoke-Checked $hh @('-m', 'pip', 'check')
Invoke-Checked $chatmock @('-m', 'pip', 'check')
Invoke-Checked $hh @((Join-Path $repo 'windows\check_preview.py'))
Invoke-Checked $chatmock @((Join-Path $repo 'windows\check_chatmock.py'))
Invoke-Checked $hh @((Join-Path $repo 'windows\check_windows.py'))
Invoke-Checked $hh @((Join-Path $repo 'windows\check_send.py'))
Invoke-Checked $hh @((Join-Path $repo 'windows\check_auto_send.py'))
Invoke-Checked $hh @((Join-Path $repo 'windows\check_reply_chats.py'))
Invoke-Checked $hh @((Join-Path $repo 'windows\check_manual_captcha.py'))
Invoke-Checked $hh @((Join-Path $repo 'windows\check_authorize.py'))
Write-Output 'Offline checks passed. No account login, HH request, or application was started.'
