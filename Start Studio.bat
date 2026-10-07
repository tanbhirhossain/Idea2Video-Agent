@echo off
title Idea2Video Studio
cd /d "S:\VidTools\idea2video (1)\idea2video"

echo ============================================
echo   Idea2Video Studio - starting services...
echo ============================================

REM ---- 1. Ollama (port 11434) ----
powershell -NoProfile -Command "try { (Invoke-WebRequest -Uri 'http://localhost:11434' -TimeoutSec 2 -UseBasicParsing) | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if %errorlevel%==0 (
    echo [OK] Ollama already running.
) else (
    echo [..] Starting Ollama...
    start "Ollama" /min cmd /c "ollama serve"
    timeout /t 3 /nobreak >nul
    echo [OK] Ollama started.
)

REM ---- 2. ComfyUI (port 8188) ----
powershell -NoProfile -Command "try { (Invoke-WebRequest -Uri 'http://127.0.0.1:8188/system_stats' -TimeoutSec 2 -UseBasicParsing) | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if %errorlevel%==0 (
    echo [OK] ComfyUI already running.
) else (
    echo [..] Starting ComfyUI Desktop...
    start "" "C:\Program Files\Comfy Desktop\Comfy Desktop.exe"
    echo      waiting for ComfyUI server...
    powershell -NoProfile -Command "$ok=$false; for($i=0;$i -lt 36;$i++){ Start-Sleep 5; try { if((Invoke-WebRequest -Uri 'http://127.0.0.1:8188/system_stats' -TimeoutSec 3 -UseBasicParsing).StatusCode -eq 200){ $ok=$true; break } } catch {} }; if($ok){ exit 0 } else { exit 1 }" >nul 2>&1
    if %errorlevel%==0 (
        echo [OK] ComfyUI is up.
    ) else (
        echo [!!] ComfyUI did not respond in 3 min - continuing anyway.
    )
)

REM ---- 3. Web server (port 5000) ----
powershell -NoProfile -Command "try { (Invoke-WebRequest -Uri 'http://127.0.0.1:5000' -TimeoutSec 2 -UseBasicParsing) | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if %errorlevel%==0 (
    echo [OK] Web UI already running.
) else (
    echo [..] Starting web server...
    start "Idea2Video Server" /min cmd /c "python webui.py"
    timeout /t 4 /nobreak >nul
    echo [OK] Web server started.
)

REM ---- 4. Open browser ----
echo [..] Opening browser...
start http://127.0.0.1:5000

echo ============================================
echo   All services launched. You can minimize
echo   this window. Server window: "Idea2Video Server"
echo ============================================
timeout /t 10 /nobreak >nul
