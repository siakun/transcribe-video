@echo off
setlocal EnableExtensions

cd /d "%~dp0"

set "APP_NAME=whisper_server"
set "ENTRY=src\server.py"
set "ENTRY_ABS=%CD%\src\server.py"
set "INDEX_HTML_ABS=%CD%\src\index.html"
set "PYTHON_EXE=.venv\Scripts\python.exe"
set "OUTPUT_ROOT=build"
set "TEMP_ROOT=temp"
set "LOG_DIR=logs"
set "PAUSE_ON_EXIT=1"

if /i "%~1"=="--no-pause" set "PAUSE_ON_EXIT=0"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "BUILD_STAMP=%%i"
set "BUILD_LOG=%LOG_DIR%\[build] %BUILD_STAMP%.log"
> "%BUILD_LOG%" echo ================================================================================
>> "%BUILD_LOG%" echo build.bat started in %CD%
>> "%BUILD_LOG%" echo.

set "OUTPUT_DIR=%OUTPUT_ROOT%\%BUILD_STAMP%"
set "TEMP_DIR=%TEMP_ROOT%\%BUILD_STAMP%"
set "SPEC_DIR=%TEMP_DIR%\spec"
set "WORK_DIR=%TEMP_DIR%\work"
call :log Build stamp: %BUILD_STAMP%
call :log Output dir: %CD%\%OUTPUT_DIR%\%APP_NAME%
call :log Temp dir: %CD%\%TEMP_DIR%

if not exist "%ENTRY%" (
  call :log [ERROR] %ENTRY% not found.
  goto :error_exit
)

if not exist "src\index.html" (
  call :log [ERROR] src\index.html not found.
  goto :error_exit
)

if not exist "%PYTHON_EXE%" (
  where python >nul 2>nul
  if errorlevel 1 (
    call :log [ERROR] python not found in PATH and .venv is missing.
    call :log         Run these first:
    call :log         uv venv
    call :log         uv sync --group dev
    goto :error_exit
  )
  set "PYTHON_EXE=python"
)

%PYTHON_EXE% -c "import fastapi, uvicorn, whisper, torch" >nul 2>nul
if errorlevel 1 (
  call :log [ERROR] Required runtime packages are missing in this Python environment.
  call :log         Install the project dependencies first, then run this file again.
  call :log         Recommended:
  call :log         uv sync --group dev
  goto :error_exit
)

%PYTHON_EXE% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
  call :log [ERROR] PyInstaller is not installed in this Python environment.
  call :log         Install it with:
  call :log         uv sync --group dev
  goto :error_exit
)

if exist "dist" rmdir /s /q "dist"
if exist "%APP_NAME%.spec" del /q "%APP_NAME%.spec"
if not exist "%OUTPUT_ROOT%" mkdir "%OUTPUT_ROOT%"
if not exist "%TEMP_ROOT%" mkdir "%TEMP_ROOT%"
if exist "%OUTPUT_DIR%" (
  call :log [ERROR] Output directory already exists: %OUTPUT_DIR%
  goto :error_exit
)
if exist "%TEMP_DIR%" (
  call :log [ERROR] Temp directory already exists: %TEMP_DIR%
  goto :error_exit
)
if not exist "%OUTPUT_DIR%" mkdir "%OUTPUT_DIR%"
if not exist "%SPEC_DIR%" mkdir "%SPEC_DIR%"
if not exist "%WORK_DIR%" mkdir "%WORK_DIR%"

call :log [1/2] Building %APP_NAME%.exe ...
call :log         Build log: %CD%\%BUILD_LOG%
>> "%BUILD_LOG%" echo [PyInstaller]

%PYTHON_EXE% "scripts\tee_runner.py" "%BUILD_LOG%" ^
  %PYTHON_EXE% -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --console ^
  --onedir ^
  --name "%APP_NAME%" ^
  --distpath "%OUTPUT_DIR%" ^
  --specpath "%SPEC_DIR%" ^
  --workpath "%WORK_DIR%" ^
  --add-data "%INDEX_HTML_ABS%;." ^
  --collect-submodules whisper ^
  --collect-data whisper ^
  --collect-submodules tiktoken ^
  --collect-submodules tiktoken_ext ^
  --collect-data tiktoken ^
  --collect-submodules fastapi ^
  --collect-submodules starlette ^
  --collect-submodules anyio ^
  --collect-submodules uvicorn ^
  --collect-submodules websockets ^
  --collect-data fastapi ^
  --collect-data starlette ^
  --collect-data uvicorn ^
  --collect-binaries torch ^
  --collect-data torch ^
  --hidden-import=tiktoken_ext.openai_public ^
  --hidden-import=uvicorn.logging ^
  --hidden-import=uvicorn.loops.auto ^
  --hidden-import=uvicorn.protocols.http.auto ^
  --hidden-import=uvicorn.protocols.websockets.auto ^
  --hidden-import=websockets.legacy.server ^
  --hidden-import=websockets.legacy.client ^
  "%ENTRY_ABS%"

if errorlevel 1 (
  call :log [ERROR] Build failed.
  call :log         See %CD%\%BUILD_LOG%
  goto :error_exit
)

call :log [2/2] Build complete.
call :log Output: %CD%\%OUTPUT_DIR%\%APP_NAME%\%APP_NAME%.exe
echo.
>> "%BUILD_LOG%" echo.
rem Block redirection writes run_latest.bat atomically; splitting into
rem multiple > / >> calls was observed to leave the file at an older
rem BUILD_STAMP when one of the sub-writes silently failed.
(
  echo @echo off
  echo cd /d "%%~dp0%BUILD_STAMP%\%APP_NAME%"
  echo "%APP_NAME%.exe"
) > "%OUTPUT_ROOT%\run_latest.bat"
call :log Notes:
call :log Run: %OUTPUT_DIR%\%APP_NAME%\%APP_NAME%.exe
call :log Launcher: %OUTPUT_ROOT%\run_latest.bat
call :log Keep the whole %OUTPUT_DIR%\%APP_NAME% folder together when moving it.
call :log Startup/crash logs are written to logs\%APP_NAME%.log next to the exe.
call :log ffmpeg must be installed and available in PATH at runtime.
call :log The first transcription can still download a Whisper model if it is not cached yet.

if exist "%TEMP_DIR%" rmdir /s /q "%TEMP_DIR%"
if exist "%TEMP_ROOT%" rd "%TEMP_ROOT%" 2>nul

goto :success_exit

:log
echo %*
>> "%BUILD_LOG%" echo %*
exit /b 0

:success_exit
if not "%PAUSE_ON_EXIT%"=="0" pause
exit /b 0

:error_exit
if not "%PAUSE_ON_EXIT%"=="0" pause
exit /b 1
