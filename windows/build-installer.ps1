[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Python,
    [string]$Iscc,
    [switch]$PortableOnly
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Developer-only local compilation. No dependency installation, account access,
# compiler download, installer execution, signing, upload or automatic cleanup.
if (-not [Environment]::Is64BitOperatingSystem) { throw 'Windows x64 is required' }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Prepared venv Python.exe is missing' }
$Python = (Resolve-Path -LiteralPath $Python).Path
if (-not $PortableOnly) {
    if (-not $Iscc -or -not (Test-Path -LiteralPath $Iscc -PathType Leaf)) { throw 'Inno Setup 6.4.3 ISCC.exe is missing; install reviewed tools separately or use -PortableOnly' }
    $Iscc = (Resolve-Path -LiteralPath $Iscc).Path
    # Official signed ISCC 6.4.3 may expose 0.0.0.0 PE version resources.
    # Check the actual compiler's VER preprocessor constant without producing output.
    & $Iscc '/O-' (Join-Path $PSScriptRoot 'packaging/compiler-version.iss')
    if ($LASTEXITCODE -ne 0) { throw 'Use the reviewed/pinned Inno Setup 6.4.3 compiler' }
}
$packaging = Join-Path $PSScriptRoot 'packaging'
$helper = Join-Path $packaging 'build_support.py'

function Invoke-PythonChecked([string[]]$Arguments) {
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Build Python step failed (exit $LASTEXITCODE)" }
}

# -I excludes PYTHONPATH, cwd and user site; helper explicitly imports its own modules.
# No output dirs are created before prerequisite and installed-constraint validation.
Invoke-PythonChecked @('-I', '-c', "import runpy,sys; sys.path.insert(0,sys.argv[1]); sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name='__main__')", $packaging, $helper, 'preflight')
& $Python -I -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Prepared build venv has dependency conflicts; do not freeze conflicting runtimes' }

$buildId = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
$build = Join-Path $PSScriptRoot "build\$buildId"
$output = Join-Path $PSScriptRoot "dist\$buildId"
$stage = Join-Path $build 'stage'
$frozen = Join-Path $output 'app'
$installer = Join-Path $output 'installer'
New-Item -ItemType Directory -Path $build, $output | Out-Null
$oldStage = [Environment]::GetEnvironmentVariable('HH_PACKAGING_STAGE', 'Process')
try {
    Invoke-PythonChecked @('-I', '-c', "import runpy,sys; sys.path.insert(0,sys.argv[1]); sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name='__main__')", $packaging, $helper, 'stage', $stage)
    $env:HH_PACKAGING_STAGE = $stage
    Invoke-PythonChecked @('-I', '-m', 'PyInstaller', '--noconfirm', '--distpath', $frozen, '--workpath', (Join-Path $build 'pyinstaller'), (Join-Path $packaging 'PocketNodeHH.spec'))
    $app = Join-Path $frozen 'PocketNodeHH'
    Invoke-PythonChecked @('-I', '-c', "import runpy,sys; sys.path.insert(0,sys.argv[1]); sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name='__main__')", $packaging, $helper, 'audit', $app)
    # Python's subprocess API waits for windowed EXEs and supplies all private pipes.
    Invoke-PythonChecked @('-I', '-c', "import subprocess,sys; p=subprocess.run([sys.argv[1],'--packaging-smoke-check'],input='',text=True,encoding='utf-8',capture_output=True,timeout=120,creationflags=subprocess.CREATE_NO_WINDOW); print(p.stdout); print(p.stderr); sys.exit(p.returncode)", (Join-Path $app 'PocketNodeHH.exe'))
    Invoke-PythonChecked @('-I', '-c', "import subprocess,sys; p=subprocess.run([sys.argv[1],'--packaging-gateway-smoke'],input='',text=True,encoding='utf-8',capture_output=True,timeout=120,creationflags=subprocess.CREATE_NO_WINDOW); print(p.stdout); print(p.stderr); sys.exit(p.returncode)", (Join-Path $app 'PocketNodeHH.exe'))
    if (-not $PortableOnly) {
        New-Item -ItemType Directory -Path $installer | Out-Null
        $version = '0.1.0'
        & $Iscc "/DAppSource=$app" "/DOutputPath=$installer" "/DAppVersion=$version" (Join-Path $packaging 'installer.iss')
        if ($LASTEXITCODE -ne 0) { throw "Inno compilation failed (exit $LASTEXITCODE)" }
        $setup = Join-Path $installer "PocketNodeHH-$version-windows-x64-setup.exe"
        if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) { throw 'Compiler did not produce the expected installer' }
        Invoke-PythonChecked @('-I', '-c', "import runpy,sys; sys.path.insert(0,sys.argv[1]); sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name='__main__')", $packaging, $helper, 'hashes', $installer)
        Write-Host "Unsigned development installer: $setup"
    }
    Invoke-PythonChecked @('-I', '-c', "import runpy,sys; sys.path.insert(0,sys.argv[1]); sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name='__main__')", $packaging, $helper, 'hashes', $output)
    Write-Host "Unsigned portable app (keep full directory): $app"
    Write-Host "Integrity manifest: $(Join-Path $output 'SHA256SUMS.txt')"
} finally {
    [Environment]::SetEnvironmentVariable('HH_PACKAGING_STAGE', $oldStage, 'Process')
}
