param(
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$ResumeId,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$SearchText
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo 'venvs\hh\Scripts\python.exe'
$env:CONFIG_DIR = Join-Path $HOME '.config\pocketnode-hh'
$env:HH_PROFILE_ID = '.'
$env:POCKETNODE_HH_RESUME_ID = $ResumeId
$env:POCKETNODE_HH_SEARCH = $SearchText
if (-not (Test-Path -LiteralPath $python)) { throw 'Run windows/bootstrap.ps1 first.' }

# The helper builds a fixed argv in Python; PS 5.1 otherwise strips quoted search phrases.
$previousEncoding = [Console]::OutputEncoding
$previousPythonEncoding = $env:PYTHONIOENCODING
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $env:PYTHONIOENCODING = 'utf-8'
    & $python (Join-Path $repo 'windows\send.py')
    $exitCode = $LASTEXITCODE
} finally {
    [Console]::OutputEncoding = $previousEncoding
    $env:PYTHONIOENCODING = $previousPythonEncoding
}
if ($exitCode -ne 0) { throw "HH send stopped (exit $exitCode); check the summary and HH account before retrying" }
