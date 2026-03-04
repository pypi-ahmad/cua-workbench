@echo off
REM setup.bat — One-command setup for CUA (Windows)
REM Usage: setup.bat

echo [INFO] Checking prerequisites...

where docker >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Docker is required. Install: https://docs.docker.com/get-docker/
    exit /b 1
)

where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python 3 is required.
    exit /b 1
)

where node >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Node.js is required.
    exit /b 1
)

echo [INFO] All prerequisites met.

REM Build Docker image
echo [INFO] Building CUA Docker image...
docker build -t cua-ubuntu:latest -f docker/Dockerfile .
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Docker build failed.
    exit /b 1
)
echo [INFO] Docker image built successfully.

REM Install Python deps
echo [INFO] Installing Python dependencies...
if not exist ".venv" (
    python -m venv .venv
)
call .venv\Scripts\activate.bat
pip install --upgrade pip
pip install -r requirements.txt
echo [INFO] Python dependencies installed.

REM Install frontend deps
echo [INFO] Installing frontend dependencies...
cd frontend
call npm install
cd ..
echo [INFO] Frontend dependencies installed.

echo.
echo === Setup complete! ===
echo.
echo To run the system:
echo   1. Start backend:  .venv\Scripts\activate ^& python -m backend.main
echo   2. Start frontend: cd frontend ^& npm run dev
echo   3. Open http://localhost:3000
echo.
