param(
    [int]$MaxResponses = 0,
    [ValidateRange(1, 86400)][int]$MinPauseSeconds = 60,
    [ValidateRange(1, 86400)][int]$MaxPauseSeconds = 180,
    [ValidateNotNullOrEmpty()][string]$ResumeId,
    [ValidateNotNullOrEmpty()][string]$SearchText
)

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
. (Join-Path $PSScriptRoot 'select-resume.ps1')

$previousEncoding = [Console]::OutputEncoding
$previousPythonEncoding = $env:PYTHONIOENCODING
$previousChatHome = $env:CHATGPT_LOCAL_HOME
$previousCodexHome = $env:CODEX_HOME
$previousAll = $env:HH_APPLICANT_TOOL_ALL
$previousResume = $env:HH_APPLICANT_TOOL_RESUME_ID
$previousSearch = $env:HH_APPLICANT_TOOL_SEARCH
$ownedGateway = $null
$runSucceeded = $false
$runMutex = New-Object System.Threading.Mutex($false, 'Local\hh-applicant-tool-gui-run')
$runLocked = $false
try {
    if (-not $runMutex.WaitOne(0)) { throw 'Another HH run is active. Finish it before starting this one.' }
    $runLocked = $true
    if (-not $PSBoundParameters.ContainsKey('MaxResponses')) {
        $entered = Read-Host 'How many applications should be sent automatically?'
        if (-not [int]::TryParse($entered, [ref]$MaxResponses)) { throw 'Enter a positive integer.' }
    }
    if ($MaxResponses -le 0) { throw 'MaxResponses must be a positive integer.' }
    if ($MinPauseSeconds -gt $MaxPauseSeconds) { throw 'MinPauseSeconds must not exceed MaxPauseSeconds.' }
    Write-Warning 'AUTO MODE: letters will be sent without individual approval. HH may restrict or block the account. Random pauses do not guarantee safety. Ctrl+C stops the run.'
    Write-Host "Maximum successful applications: $MaxResponses; random pause: $MinPauseSeconds-$MaxPauseSeconds seconds."
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $env:PYTHONIOENCODING = 'utf-8'
    if ($ResumeId) {
        $env:HH_APPLICANT_TOOL_ALL = $null
        $env:HH_APPLICANT_TOOL_RESUME_ID = $ResumeId
        $env:HH_APPLICANT_TOOL_SEARCH = Resolve-HHSearchText -Repo $repo -ResumeId $ResumeId -OverrideText $SearchText
    } else {
        if ($SearchText) { throw 'Use -ResumeId with -SearchText; all resumes use their own saved searches.' }
        $env:HH_APPLICANT_TOOL_ALL = '1'
        $env:HH_APPLICANT_TOOL_RESUME_ID = $null
        $env:HH_APPLICANT_TOOL_SEARCH = $null
    }
    $ownedGateway = Start-ChatMockForHH
    & $python (Join-Path $repo 'windows\auto_send.py') --max-responses $MaxResponses --min-pause $MinPauseSeconds --max-pause $MaxPauseSeconds
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
        $env:HH_APPLICANT_TOOL_ALL = $previousAll
        $env:HH_APPLICANT_TOOL_RESUME_ID = $previousResume
        $env:HH_APPLICANT_TOOL_SEARCH = $previousSearch
    }
}
if ($exitCode -ne 0) { throw "HH auto send stopped (exit $exitCode); check the summary and HH account before retrying" }
