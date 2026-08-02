@echo off
setlocal
cd /d "%~dp0"

echo ===== Butler Startup =====
echo.

curl -s -m 2 http://127.0.0.1:11434/api/tags >nul 2>&1
if errorlevel 1 (
    echo [1/3] Starting Ollama service...
    start "" "C:\Users\blessyzhang\AppData\Local\Programs\Ollama\ollama.exe" serve
    timeout /t 5 /nobreak >nul
) else (
    echo [1/3] Ollama already running
)

echo [2/3] Browser will open in 3 seconds...
start "" cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:8000"

echo [3/3] Starting Butler backend (close this window to stop the service)...
echo.
"C:\Users\blessyzhang\anaconda3\python.exe" run.py serve

echo.
echo Service stopped.
pause
