@echo off
echo ============================================================
echo   GSK Regulatory Intelligence Platform
echo   Powered by Cognizant AI
echo ============================================================
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.10+
    pause
    exit /b 1
)

REM Install dependencies (only if not already installed)
echo [1/3] Checking dependencies...
pip install -q -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

echo [2/3] Starting the server...
echo.
echo  Open your browser and navigate to:
echo  http://localhost:8000
echo.
echo  Press CTRL+C to stop the server.
echo ============================================================

REM Start FastAPI server
python -m uvicorn app:app --reload --host 0.0.0.0 --port 8000

pause
