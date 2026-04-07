@echo off
REM ────────────────────────────────────────────────────────────────────────────
REM start.bat — One-command launcher for CUA Workbench (Windows)
REM
REM Usage:  start.bat          Start backend + frontend
REM         start.bat --stop   Stop background processes
REM ────────────────────────────────────────────────────────────────────────────

cd /d "%~dp0"

if "%~1"=="--stop" (
    echo [CUA] Stopping processes...
    taskkill /f /fi "WINDOWTITLE eq cua-backend" >nul 2>&1
    taskkill /f /fi "WINDOWTITLE eq cua-frontend" >nul 2>&1
    taskkill /f /im "uvicorn.exe" >nul 2>&1
    echo [CUA] Stopped.
    exit /b 0
)

REM ── Pre-flight checks ──────────────────────────────────────────────────────
where python >nul 2>&1 || (
    echo [CUA] ERROR: python not found. Install Python 3.10+.
    exit /b 1
)
where node >nul 2>&1 || (
    echo [CUA] ERROR: node not found. Install Node.js 18+.
    exit /b 1
)
where docker >nul 2>&1 || echo [CUA] WARNING: Docker not found — container features will be unavailable.

REM ── Check Python deps ──────────────────────────────────────────────────────
python -c "import fastapi" >nul 2>&1 || (
    echo [CUA] Installing Python dependencies...
    pip install -r requirements.txt
)

REM ── Check Node deps ────────────────────────────────────────────────────────
if not exist "frontend\node_modules" (
    echo [CUA] Installing Node dependencies...
    cd frontend && npm install && cd ..
)

REM ── .env reminder ───────────────────────────────────────────────────────────
if not exist ".env" (
    echo [CUA] NOTE: No .env file found. Provide API keys via the UI or environment variables.
)

REM ── Launch backend ──────────────────────────────────────────────────────────
echo [CUA] Starting backend on http://localhost:8000 ...
start "cua-backend" /min python -m backend.main

REM Wait briefly for backend
timeout /t 3 /nobreak >nul

REM ── Launch frontend ─────────────────────────────────────────────────────────
echo [CUA] Starting frontend on http://localhost:3000 ...
start "cua-frontend" /min cmd /c "cd frontend && npm run dev"

echo.
echo [CUA] CUA Workbench is running!
echo [CUA]   Frontend: http://localhost:3000
echo [CUA]   Backend:  http://localhost:8000
echo [CUA]   Stop:     start.bat --stop
echo.
