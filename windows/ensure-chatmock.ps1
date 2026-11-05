# Shared, on-demand local gateway lifecycle for the HH wrappers.
function Test-ChatMockReady {
    try {
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 2
        if ($health.status -ne 'ok') { return $false }
        $models = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/v1/models' -TimeoutSec 2
        if (-not $models.data) { return $false }
        return @($models.data).Count -gt 0
    } catch {
        return $false
    }
}

function Stop-ChatMockForHH($gateway, [switch]$KeepLogs) {
    if ($null -eq $gateway) { return }
    try {
        if (-not $gateway.Process.HasExited) {
            Stop-Process -InputObject $gateway.Process -ErrorAction Stop
            if (-not $gateway.Process.WaitForExit(5000)) {
                Write-Warning 'ChatMock did not stop; check the local process before another run.'
                return
            }
        }
        if (-not $KeepLogs) {
            foreach ($log in $gateway.Logs) {
                Remove-Item -LiteralPath $log -ErrorAction SilentlyContinue
            }
        }
    } catch {
        Write-Warning 'Could not stop the ChatMock process started by this run; check it manually.'
    } finally {
        if ($KeepLogs) {
            foreach ($log in $gateway.Logs) {
                Write-Host "ChatMock diagnostic log preserved: $log"
            }
        }
        $gateway.Process.Dispose()
    }
}

function Start-ChatMockForHH {
    $repo = Split-Path -Parent $PSScriptRoot
    $python = Join-Path $repo 'venvs\chatmock\Scripts\python.exe'
    $entry = Join-Path $PSScriptRoot 'chatmock_server.py'
    if (-not (Test-Path -LiteralPath $python)) { throw 'Run windows/bootstrap.ps1 first (ChatMock missing).' }

    $authHome = Join-Path $HOME '.local\state\pocketnode-hh\chatmock'
    $auth = Join-Path $authHome 'auth.json'
    if (-not (Test-Path -LiteralPath $auth)) { throw 'Run windows/chatmock.ps1 login first.' }
    $data = Get-Content -LiteralPath $auth -Raw | ConvertFrom-Json
    foreach ($name in @('access_token', 'refresh_token', 'id_token')) {
        if (-not $data.tokens.$name) { throw 'Incomplete ChatMock login. Log in interactively first.' }
    }

    if (Test-ChatMockReady) {
        Write-Host 'ChatMock is already running on 127.0.0.1:8000; leaving it running.'
        return $null
    }

    $env:CHATGPT_LOCAL_HOME = $authHome
    $env:CODEX_HOME = $authHome
    $logDir = Join-Path $HOME '.local\state\pocketnode-hh\run'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $stem = Join-Path $logDir ('chatmock-' + [guid]::NewGuid().ToString('N'))
    $stdout = $stem + '.out.log'
    $stderr = $stem + '.err.log'
    $logs = @($stdout, $stderr)
    $process = Start-Process -FilePath $python -ArgumentList @(
        '-u',
        ('"' + $entry + '"'),
        'serve', '--host', '127.0.0.1', '--port', '8000',
        '--reasoning-summary', 'none', '--reasoning-compat', 'legacy',
        '--no-enable-web-search', '--no-fast-mode'
    ) -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru -ErrorAction Stop
    $gateway = [pscustomobject]@{ Process = $process; Logs = $logs }
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if ($process.HasExited) { break }
        if (Test-ChatMockReady) {
            if ($process.HasExited) { break }
            Write-Host 'ChatMock started on 127.0.0.1:8000 for this HH run.'
            return $gateway
        }
        Start-Sleep -Milliseconds 500
    }
    Stop-ChatMockForHH $gateway -KeepLogs
    throw 'ChatMock did not become ready; HH was not started. Check login and port 8000.'
}
