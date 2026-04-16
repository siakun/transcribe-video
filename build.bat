@echo off
setlocal EnableExtensions

cd /d "%~dp0"

set "APP_NAME=whisper_server"
set "ENTRY=src\server.py"
set "PYTHON_EXE=.venv\Scripts\python.exe"

if not exist "%ENTRY%" (
  echo [ERROR] %ENTRY% not found.
  exit /b 1
)

if not exist "src\index.html" (
  echo [ERROR] src\index.html not found.
  exit /b 1
)

if not exist "%PYTHON_EXE%" (
  where python >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] python not found in PATH and .venv is missing.
    echo         Run these first:
    echo         uv venv
    echo         uv sync --group dev
    exit /b 1
  )
  set "PYTHON_EXE=python"
)

%PYTHON_EXE% -c "import fastapi, uvicorn, whisper, torch" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Required runtime packages are missing in this Python environment.
  echo         Install the project dependencies first, then run this file again.
  echo         Recommended:
  echo         uv sync --group dev
  exit /b 1
)

%PYTHON_EXE% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
  echo [ERROR] PyInstaller is not installed in this Python environment.
  echo         Install it with:
  echo         uv sync --group dev
  exit /b 1
)

if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "%APP_NAME%.spec" del /q "%APP_NAME%.spec"

echo [1/2] Building %APP_NAME%.exe ...

%PYTHON_EXE% -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --console ^
  --onedir ^
  --name "%APP_NAME%" ^
  --add-data "src\index.html;." ^
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
  "%ENTRY%"

if errorlevel 1 (
  echo [ERROR] Build failed.
  exit /b 1
)

echo [2/2] Build complete.
echo Output: %CD%\dist\%APP_NAME%\%APP_NAME%.exe
echo.
echo Notes:
echo   - Keep the whole dist\%APP_NAME% folder together when moving it.
echo   - ffmpeg must be installed and available in PATH at runtime.
echo   - The first transcription can still download a Whisper model if it is not cached yet.

exit /b 0
