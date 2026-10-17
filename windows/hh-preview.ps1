param(
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$ResumeId,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$SearchText
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo 'venvs\hh\Scripts\python.exe'
$config = Join-Path $HOME '.config\pocketnode-hh'
$env:CONFIG_DIR = $config
$env:HH_PROFILE_ID = '.'
$env:POCKETNODE_HH_RESUME_ID = $ResumeId
$env:POCKETNODE_HH_SEARCH = $SearchText
if (-not (Test-Path -LiteralPath $python)) { throw 'Run windows/bootstrap.ps1 first.' }

# PS 5.1 strips embedded double quotes from native-process arguments. Pass values
# through the environment; Python constructs the fixed argv without shell parsing.
$previousEncoding = [Console]::OutputEncoding
$previousPythonEncoding = $env:PYTHONIOENCODING
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $env:PYTHONIOENCODING = 'utf-8'
    & $python (Join-Path $repo 'windows\preview.py')
    $exitCode = $LASTEXITCODE
} finally {
    [Console]::OutputEncoding = $previousEncoding
    $env:PYTHONIOENCODING = $previousPythonEncoding
}
if ($exitCode -ne 0) { throw "HH preview failed (exit $exitCode)" }
