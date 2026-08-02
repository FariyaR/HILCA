@echo off
setlocal
cd /d "%~dp0"
title HILCA Mission Control

if /i "%~1"=="demo" (
  set "LLM_PROVIDER=mock"
  set "HILCA_ALLOW_MOCK=1"
  echo.
  echo   ============================================
  echo    HILCA Mission Control  --  DEMO MODE
  echo    Offline mock provider - no API key, no spend.
  echo   ============================================
) else (
  echo.
  echo   ============================================
  echo    HILCA Mission Control
  echo    Real LLM run - uses the provider + key in .env
  echo    Tip:  start.bat demo   ^<- try the UI offline
  echo   ============================================
)
echo.
echo    Opening http://localhost:8000 ...
echo    Press Ctrl+C to stop the server.
echo.

start "" http://localhost:8000/
python -m uvicorn web:app --host 127.0.0.1 --port 8000
pause
