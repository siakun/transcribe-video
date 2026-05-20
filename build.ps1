<#
build.ps1 - PyInstaller build script for whisper_server.

This replaces the former build.bat logic. build.bat is now a thin launcher
that runs this script via:
    powershell -NoProfile -ExecutionPolicy Bypass -File build.ps1

Usage:
    .\build.ps1            Build, then pause before exit.
    .\build.ps1 -NoPause   Build without the final pause (CI / scripted use).
    build.bat              Double-click friendly wrapper for the above.
#>
[CmdletBinding()]
param(
    [switch]$NoPause,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

# Accept the legacy "--no-pause" spelling forwarded verbatim from build.bat.
if ($Rest -and ($Rest -contains '--no-pause')) { $NoPause = $true }

# Stop on cmdlet failures so they surface in the catch block below. Native
# commands (python / PyInstaller) never auto-throw; their exit codes are
# checked explicitly via $LASTEXITCODE.
$ErrorActionPreference = 'Stop'

$ScriptRoot = $PSScriptRoot
if (-not $ScriptRoot) { $ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition }
Set-Location -LiteralPath $ScriptRoot

# --- Configuration ----------------------------------------------------------
$AppName      = 'whisper_server'
$Entry        = 'src\server.py'
$EntryAbs     = Join-Path $ScriptRoot 'src\server.py'
$IndexHtml    = 'src\index.html'
$IndexHtmlAbs = Join-Path $ScriptRoot 'src\index.html'
$PythonExe    = Join-Path $ScriptRoot '.venv\Scripts\python.exe'
$OutputRoot   = 'build'
$TempRoot     = 'temp'
$LogDir       = 'logs'

# --- Logging ----------------------------------------------------------------
# StreamWriter resolves relative paths against the .NET working directory,
# which is not synced with Set-Location, so the build log path is absolute.
$LogWriter = $null

function Write-Log {
    param([string]$Message = '')
    Write-Host $Message
    if ($script:LogWriter) { $script:LogWriter.WriteLine($Message) }
}

# Log the message, then abort with a sentinel the catch block recognizes
# (so it knows the failure was already reported and need not re-log it).
function Fail {
    param([string[]]$Message)
    foreach ($line in $Message) { Write-Log $line }
    throw '__build_failed__'
}

$exitCode = 0
try {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

    $BuildStamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $BuildLog   = Join-Path $ScriptRoot ("$LogDir\[build] $BuildStamp.log")
    $LogWriter  = [System.IO.StreamWriter]::new(
        $BuildLog, $true, [System.Text.UTF8Encoding]::new($false))
    $LogWriter.AutoFlush = $true

    $LogWriter.WriteLine('=' * 80)
    $LogWriter.WriteLine("build.ps1 started in $ScriptRoot")
    $LogWriter.WriteLine('')

    $OutputDir = Join-Path $OutputRoot $BuildStamp
    $TempDir   = Join-Path $TempRoot   $BuildStamp
    $SpecDir   = Join-Path $TempDir 'spec'
    $WorkDir   = Join-Path $TempDir 'work'

    Write-Log "Build stamp: $BuildStamp"
    Write-Log "Output dir: $(Join-Path $ScriptRoot (Join-Path $OutputDir $AppName))"
    Write-Log "Temp dir: $(Join-Path $ScriptRoot $TempDir)"

    # --- Preflight checks ---------------------------------------------------
    if (-not (Test-Path -LiteralPath $Entry)) {
        Fail "[ERROR] $Entry not found."
    }
    if (-not (Test-Path -LiteralPath $IndexHtml)) {
        Fail '[ERROR] src\index.html not found.'
    }

    if (-not (Test-Path -LiteralPath $PythonExe)) {
        if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
            Fail @(
                '[ERROR] python not found in PATH and .venv is missing.',
                '        Run these first:',
                '        uv venv',
                '        uv sync --group dev'
            )
        }
        $PythonExe = 'python'
    }

    & $PythonExe -c 'import fastapi, uvicorn, faster_whisper, torch' 1>$null 2>$null
    if ($LASTEXITCODE -ne 0) {
        Fail @(
            '[ERROR] Required runtime packages are missing in this Python environment.',
            '        Install the project dependencies first, then run this file again.',
            '        Recommended:',
            '        uv sync --group dev'
        )
    }

    & $PythonExe -m PyInstaller --version 1>$null 2>$null
    if ($LASTEXITCODE -ne 0) {
        Fail @(
            '[ERROR] PyInstaller is not installed in this Python environment.',
            '        Install it with:',
            '        uv sync --group dev'
        )
    }

    # --- Prepare output / temp directories ----------------------------------
    if (Test-Path -LiteralPath 'dist') { Remove-Item -LiteralPath 'dist' -Recurse -Force }
    $specFile = "$AppName.spec"
    if (Test-Path -LiteralPath $specFile) { Remove-Item -LiteralPath $specFile -Force }

    New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null
    New-Item -ItemType Directory -Force -Path $TempRoot   | Out-Null

    if (Test-Path -LiteralPath $OutputDir) {
        Fail "[ERROR] Output directory already exists: $OutputDir"
    }
    if (Test-Path -LiteralPath $TempDir) {
        Fail "[ERROR] Temp directory already exists: $TempDir"
    }

    New-Item -ItemType Directory -Path $OutputDir | Out-Null
    New-Item -ItemType Directory -Path $SpecDir   | Out-Null
    New-Item -ItemType Directory -Path $WorkDir   | Out-Null

    # --- Build --------------------------------------------------------------
    Write-Log "[1/2] Building $AppName.exe ..."
    Write-Log "        Build log: $BuildLog"
    $LogWriter.WriteLine('[PyInstaller]')

    $pyiArgs = @(
        '-m', 'PyInstaller',
        '--noconfirm',
        '--clean',
        '--console',
        '--onedir',
        '--name', $AppName,
        '--distpath', $OutputDir,
        '--specpath', $SpecDir,
        '--workpath', $WorkDir,
        '--add-data', "$IndexHtmlAbs;.",
        '--collect-all', 'faster_whisper',
        '--collect-all', 'ctranslate2',
        '--collect-all', 'huggingface_hub',
        '--collect-all', 'tokenizers',
        '--collect-all', 'onnxruntime',
        '--collect-all', 'av',
        '--collect-submodules', 'fastapi',
        '--collect-submodules', 'starlette',
        '--collect-submodules', 'anyio',
        '--collect-submodules', 'uvicorn',
        '--collect-submodules', 'websockets',
        '--collect-data', 'fastapi',
        '--collect-data', 'starlette',
        '--collect-data', 'uvicorn',
        '--collect-binaries', 'torch',
        '--collect-data', 'torch',
        '--hidden-import=uvicorn.logging',
        '--hidden-import=uvicorn.loops.auto',
        '--hidden-import=uvicorn.protocols.http.auto',
        '--hidden-import=uvicorn.protocols.websockets.auto',
        '--hidden-import=websockets.legacy.server',
        '--hidden-import=websockets.legacy.client',
        $EntryAbs
    )

    # Tee PyInstaller output to the console and the build log in one pass --
    # this replaces the old scripts\tee_runner.py helper. 2>&1 merges
    # PyInstaller's stderr logging into the pipeline; ErrorActionPreference is
    # relaxed locally so those merged records cannot abort the run.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $PythonExe @pyiArgs 2>&1 | ForEach-Object {
            $line = "$_"
            Write-Host $line
            $LogWriter.WriteLine($line)
        }
        $pyiExit = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $prevEAP
    }

    if ($pyiExit -ne 0) {
        Fail @(
            '[ERROR] Build failed.',
            "        See $BuildLog"
        )
    }

    # --- Finish -------------------------------------------------------------
    Write-Log '[2/2] Build complete.'
    Write-Log "Output: $(Join-Path $ScriptRoot (Join-Path (Join-Path $OutputDir $AppName) "$AppName.exe"))"
    Write-Host ''
    $LogWriter.WriteLine('')

    # run_latest.bat stays a .bat so it remains double-clickable. Write it in a
    # single call: the old build.bat split this across > / >> redirects and was
    # seen to leave the file at a stale stamp when one sub-write failed.
    $launcher = @(
        '@echo off',
        "cd /d `"%~dp0$BuildStamp\$AppName`"",
        "`"$AppName.exe`""
    ) -join "`r`n"
    Set-Content -LiteralPath (Join-Path $OutputRoot 'run_latest.bat') `
        -Value $launcher -Encoding ascii

    Write-Log 'Notes:'
    Write-Log "Run: $(Join-Path (Join-Path $OutputDir $AppName) "$AppName.exe")"
    Write-Log "Launcher: $(Join-Path $OutputRoot 'run_latest.bat')"
    Write-Log "Keep the whole $(Join-Path $OutputDir $AppName) folder together when moving it."
    Write-Log "Startup/crash logs are written to logs\$AppName.log next to the exe."
    Write-Log 'ffmpeg must be installed and available in PATH at runtime.'
    Write-Log 'The first transcription can still download a Whisper model if it is not cached yet.'

    # Best-effort temp cleanup. The build already succeeded, so a cleanup
    # hiccup (e.g. a transiently locked file) must not fail the run -- the old
    # build.bat ignored these errors too (rmdir /q, rd 2>nul).
    try {
        if (Test-Path -LiteralPath $TempDir) {
            Remove-Item -LiteralPath $TempDir -Recurse -Force
        }
        # Drop the temp root only when it is empty, mirroring `rd %TEMP_ROOT%`,
        # so a leftover dir from another build is never clobbered. Remove-Item
        # on a non-empty directory without -Recurse pops a confirmation prompt
        # that -ErrorAction cannot suppress, so test for emptiness first.
        if ((Test-Path -LiteralPath $TempRoot) -and
            -not (Get-ChildItem -LiteralPath $TempRoot -Force)) {
            Remove-Item -LiteralPath $TempRoot -Force
        }
    }
    catch {
        Write-Log "[WARN] Temp cleanup skipped: $($_.Exception.Message)"
    }
}
catch {
    # The Fail helper already logged its message; anything else is unexpected.
    if ($_.Exception.Message -ne '__build_failed__') {
        Write-Log "[ERROR] $($_.Exception.Message)"
    }
    $exitCode = 1
}
finally {
    if ($LogWriter) {
        $LogWriter.Flush()
        $LogWriter.Dispose()
    }
}

if (-not $NoPause) { cmd /c pause }
exit $exitCode
