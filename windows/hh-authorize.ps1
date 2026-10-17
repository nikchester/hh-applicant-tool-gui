$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$cli = Join-Path $repo 'venvs\hh\Scripts\hh-applicant-tool.exe'
$env:CONFIG_DIR = Join-Path $HOME '.config\pocketnode-hh'
$env:HH_PROFILE_ID = '.'
if (-not (Test-Path -LiteralPath $cli)) { throw 'Run windows/bootstrap.ps1 first.' }
# HH 2.3.7 initializes its config before parsing CLI flags, hence the environment above.
# Credentials and one-time codes must be entered only in the interactive terminal.
& $cli --config-dir $env:CONFIG_DIR --profile-id . --no-handle-captcha authorize
if ($LASTEXITCODE -ne 0) { throw "HH authorization failed (exit $LASTEXITCODE)" }
