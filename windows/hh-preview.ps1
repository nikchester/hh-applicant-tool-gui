param(
    [ValidateNotNullOrEmpty()][string]$ResumeId,
    [ValidateNotNullOrEmpty()][string]$SearchText
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo 'venvs\hh\Scripts\python.exe'
$config = Join-Path $HOME '.config\hh-applicant-tool-cli'
$env:CONFIG_DIR = $config
$env:HH_PROFILE_ID = '.'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run windows/bootstrap.ps1 first.' }
# Move pre-rename PocketNode profile folders once; refuses to merge or delete.
& $python -I (Join-Path $PSScriptRoot 'profile_migration.py')
if ($LASTEXITCODE -ne 0) { throw 'Legacy profile migration failed.' }
. (Join-Path $PSScriptRoot 'ensure-chatmock.ps1')
. (Join-Path $PSScriptRoot 'select-resume.ps1')

# PS 5.1 strips embedded double quotes from native-process arguments. Pass values
# through the environment; Python constructs the fixed argv without shell parsing.
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
    if (-not $ResumeId) { $ResumeId = Select-HHResume -Python $python -Repo $repo }
    $env:HH_APPLICANT_TOOL_RESUME_ID = $ResumeId
    $env:HH_APPLICANT_TOOL_SEARCH = Resolve-HHSearchText -Repo $repo -ResumeId $ResumeId -OverrideText $SearchText
    $ownedGateway = Start-ChatMockForHH
    & $python (Join-Path $repo 'windows\preview.py')
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
if ($exitCode -ne 0) { throw "HH preview failed (exit $exitCode)" }
