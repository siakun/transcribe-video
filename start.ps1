<#
start.ps1 - transcribe-video 사용자용 진입점.

사용자는 리포지토리를 받은 뒤 이 파일(또는 더블클릭용 start.bat)만 실행하면 된다.
동작은 exe처럼 캡슐화돼 있다.
  1. .venv가 없으면 환경을 준비한다 (uv sync).
  2. 소스 내용 지문을 계산해 마지막 빌드와 비교한다.
     - 같으면 마지막 빌드를 그대로 실행한다 (중복 빌드 방지).
     - 다르거나 빌드가 없으면 새로 빌드한 뒤 실행한다.
  3. 서버를 별도 창에서 띄우고 브라우저를 연다.

실제 빌드는 transcribe-video/scripts/build.ps1 이 담당한다. 이 파일은 그 위에서
"필요할 때만 빌드하고 실행"을 조율하는 얇은 진입점이다.

사용:
  start.bat              더블클릭 (권장)
  .\start.ps1            직접 실행
  .\start.ps1 -Plan      빌드/실행 판단만 출력하고 끝낸다 (아무것도 안 바꿈)
  .\start.ps1 -ForceBuild  지문과 무관하게 무조건 새로 빌드
  .\start.ps1 -NoBrowser   브라우저 자동 열기를 끈다
#>
[CmdletBinding()]
param(
    [switch]$Plan,
    [switch]$ForceBuild,
    [switch]$NoBrowser,
    [switch]$NoPause
)

$ErrorActionPreference = 'Stop'

$ScriptRoot = $PSScriptRoot
if (-not $ScriptRoot) { $ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition }

$ProjectRoot = Join-Path $ScriptRoot 'transcribe-video'
$BuildScript = Join-Path $ProjectRoot 'scripts\build.ps1'
$SrcDir      = Join-Path $ProjectRoot 'src'
$BuildRoot   = Join-Path $ProjectRoot 'build'
$Marker      = Join-Path $BuildRoot '.last_build.json'
$Port        = 8765
$Url         = "http://localhost:$Port"

function Write-Step { param([string]$Message) Write-Host "[start] $Message" }

function Fail {
    param([string]$Message)
    Write-Host "[start][ERROR] $Message" -ForegroundColor Red
    if (-not $NoPause) { cmd /c pause }
    exit 1
}

# 빌드 입력(소스 + 빌드 구성)의 내용 지문. 타임스탬프나 git이 아니라 내용 해시라
# 다운로드 방식이나 파일 수정시각에 흔들리지 않고 "코드 내용이 바뀌었는가"만 본다.
function Get-SourceFingerprint {
    $files = @()
    if (Test-Path -LiteralPath $SrcDir) {
        $files += Get-ChildItem -LiteralPath $SrcDir -Recurse -File |
            Where-Object { $_.FullName -notmatch '\\__pycache__\\' }
    }
    foreach ($rel in @('pyproject.toml', 'uv.lock', 'scripts\build.ps1')) {
        $p = Join-Path $ProjectRoot $rel
        if (Test-Path -LiteralPath $p) { $files += Get-Item -LiteralPath $p }
    }
    $sb = [System.Text.StringBuilder]::new()
    foreach ($f in ($files | Sort-Object FullName)) {
        $rel = $f.FullName.Substring($ProjectRoot.Length).TrimStart('\', '/')
        $h = (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash
        [void]$sb.AppendLine("$rel=$h")
    }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($sb.ToString())
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes)) -replace '-', '').ToLower()
    } finally { $sha.Dispose() }
}

function Find-LatestExe {
    if (-not (Test-Path -LiteralPath $BuildRoot)) { return $null }
    $dirs = Get-ChildItem -LiteralPath $BuildRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^\d{8}_\d{6}$' } |
        Sort-Object Name -Descending
    foreach ($d in $dirs) {
        $exe = Join-Path $d.FullName 'whisper_server\whisper_server.exe'
        if (Test-Path -LiteralPath $exe) { return $exe }
    }
    return $null
}

function Get-UpToDateExe {
    param([string]$Fingerprint)
    if (-not (Test-Path -LiteralPath $Marker)) { return $null }
    try {
        $m = Get-Content -Raw -LiteralPath $Marker | ConvertFrom-Json
    } catch { return $null }
    if ($m.fingerprint -ne $Fingerprint -or -not $m.exe) { return $null }
    $exe = Join-Path $ProjectRoot $m.exe
    if (Test-Path -LiteralPath $exe) { return $exe }
    return $null
}

function Test-Port {
    param([int]$PortNumber)
    $client = [System.Net.Sockets.TcpClient]::new()
    try { $client.Connect('127.0.0.1', $PortNumber); return $true }
    catch { return $false }
    finally { $client.Dispose() }
}

# --- 판단 ---------------------------------------------------------------
$fingerprint = Get-SourceFingerprint
$exe = $null
if (-not $ForceBuild) { $exe = Get-UpToDateExe -Fingerprint $fingerprint }

if ($Plan) {
    Write-Step "지문(sha256): $fingerprint"
    if ($ForceBuild) {
        Write-Step "판단: BUILD (강제 빌드)"
    }
    elseif ($exe) {
        Write-Step "판단: RUN (소스 변경 없음)"
        Write-Step "실행 대상: $exe"
    }
    else {
        Write-Step "판단: BUILD (소스 변경 또는 최초 실행)"
        $latest = Find-LatestExe
        if ($latest) { Write-Step "참고: 최신 빌드는 있으나 지문 불일치 또는 마커 없음 ($latest)" }
    }
    exit 0
}

# --- 빌드 (필요 시) -----------------------------------------------------
if (-not $exe) {
    Write-Step "빌드가 필요합니다 (소스 변경 또는 최초 실행)."

    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Fail "uv가 설치되어 있지 않습니다. uv를 먼저 설치하세요: https://docs.astral.sh/uv/"
    }
    Write-Step "환경 준비 중 (uv sync)..."
    Push-Location -LiteralPath $ProjectRoot
    try {
        & uv sync --group dev
        if ($LASTEXITCODE -ne 0) { Fail "uv sync 실패." }
    }
    finally { Pop-Location }

    # 실제 빌드는 자식 PowerShell 프로세스로 호출한다. build.ps1이 끝에서 exit를
    # 호출하므로, 같은 프로세스에서 부르면 start.ps1까지 함께 종료된다.
    Write-Step "빌드 실행 중 (scripts\build.ps1)..."
    & powershell -NoProfile -ExecutionPolicy Bypass -File $BuildScript -NoPause
    if ($LASTEXITCODE -ne 0) { Fail "빌드 실패. 위 로그를 확인하세요." }

    $exe = Find-LatestExe
    if (-not $exe) { Fail "빌드 산출물(whisper_server.exe)을 찾지 못했습니다." }

    $rel = $exe.Substring($ProjectRoot.Length).TrimStart('\', '/')
    New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null
    @{ fingerprint = $fingerprint; exe = $rel; builtAt = (Get-Date).ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath $Marker -Encoding utf8
    Write-Step "빌드 완료. 기록을 갱신했습니다."
}
else {
    Write-Step "소스 변경 없음. 마지막 빌드를 실행합니다."
}

# --- 실행 ---------------------------------------------------------------
Write-Step "서버 시작: $exe"
Start-Process -FilePath $exe -WorkingDirectory (Split-Path -Parent $exe)

if (-not $NoBrowser) {
    Write-Step "서버 기동 대기 중..."
    $up = $false
    for ($i = 0; $i -lt 100; $i++) {
        if (Test-Port -PortNumber $Port) { $up = $true; break }
        Start-Sleep -Milliseconds 300
    }
    if ($up) {
        Start-Process $Url
        Write-Step "브라우저를 열었습니다: $Url"
    }
    else {
        Write-Step "서버가 아직 응답하지 않습니다. 브라우저에서 직접 여세요: $Url"
    }
}

Write-Step "완료. 서버는 별도 창에서 실행 중입니다."
