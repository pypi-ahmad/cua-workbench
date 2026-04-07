@echo off
setlocal EnableExtensions

REM Usage:
REM   setup.bat
REM   setup.bat --clean

echo [INFO] Checking prerequisites...

where docker >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Docker CLI not found. Install Docker Desktop.
  exit /b 1
)

where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python not found.
  exit /b 1
)

where node >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Node.js not found.
  exit /b 1
)

docker info >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Docker daemon is not running. Start Docker Desktop and retry.
  exit /b 1
)

echo [INFO] All prerequisites met.

REM B-16: Disk-space check
for /f "tokens=3" %%a in ('dir /-c "%~dp0" ^| find "bytes free"') do set FREE_BYTES=%%a
set /a FREE_GB=%FREE_BYTES:~0,-9% 2>nul
if defined FREE_GB (
  if %FREE_GB% LSS 10 (
    echo [WARN] Low disk space: ~%FREE_GB%GB available ^(10GB recommended^).
    echo [WARN] Docker image build may fail. Free up space or press Ctrl+C to abort.
    timeout /t 3 /nobreak >nul
  ) else (
    echo [INFO] Disk space OK: ~%FREE_GB%GB available.
  )
)

REM Destructive cleanup only when explicitly requested
if /I "%~1"=="--clean" (
  echo [WARN] Running destructive Docker cleanup ^(--clean^)...
  docker compose down --rmi all -v
  docker system prune -a --volumes -f
)

echo [INFO] Building Docker image (compose)... This may take several minutes on first run.
docker compose build --progress=plain
if errorlevel 1 (
  echo [ERROR] Docker compose build failed.
  exit /b 1
)
echo [INFO] Docker image built successfully.

echo [INFO] Installing Python dependencies...
if not exist ".venv" (
  python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
echo [INFO] Python dependencies installed.

echo [INFO] Installing frontend dependencies...
pushd frontend >nul
call npm install
popd >nul
echo [INFO] Frontend dependencies installed.

echo.
echo === Setup complete! ===
echo.
echo Quick start:
echo   start.bat
echo.
echo Or run manually:
echo   1. Start container: docker compose up -d --build
echo   2. Start backend:   .venv\Scripts\activate ^& python -m backend.main
echo   3. Start frontend:  cd frontend ^& npm run dev
echo   4. Open http://localhost:5173
echo.

endlocal