$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo 'venvs\hh\Scripts\python.exe'
$env:CONFIG_DIR = Join-Path $HOME '.config\hh-applicant-tool-cli'
$env:HH_PROFILE_ID = '.'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run windows/bootstrap.ps1 first.' }
# Move pre-rename PocketNode profile folders once; refuses to merge or delete.
& $python -I (Join-Path $PSScriptRoot 'profile_migration.py')
if ($LASTEXITCODE -ne 0) { throw 'Legacy profile migration failed.' }
# HH 2.3.7 initializes its config before parsing CLI flags, hence the environment above.
# Credentials and one-time codes must be entered only in the interactive terminal.
# windows/authorize.py adds a redacted summary when HH rejects the OTP code.
& $python (Join-Path $repo 'windows\authorize.py') --config-dir $env:CONFIG_DIR --profile-id . --no-handle-captcha --manual-captcha authorize
if ($LASTEXITCODE -ne 0) { throw "HH authorization failed (exit $LASTEXITCODE)" }
