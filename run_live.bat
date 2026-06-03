@echo off
REM ===========================================================================
REM  Jarvis 1.0 - FULL LIVE PIPELINE
REM  Runs the orchestrator: Pi WS link (:8765), ASR (Whisper), VLM, TTS (Kokoro),
REM  agent -> LM Studio, MCP (:8770), and the read-only Observatory dashboard.
REM
REM  The dashboard is exposed on your LAN as  http://elena.local:8772
REM  (configured in desktop\config.local.toml -> [dashboard] enable/host/mdns_name).
REM
REM  Prereqs:
REM    * LM Studio running with the Qwen3.5-9B model loaded (port 1234)
REM    * Pi + Teensy powered on and connected (they stream over the WS link)
REM    * Firewall rule for TCP 8772 (already added) so phones/laptops can reach it
REM ===========================================================================
setlocal
cd /d "%~dp0"

REM Use the FULL-STACK Python on PATH (where you ran: cd desktop  ^&^&  pip install -e ".[dev]").
REM NOT the lightweight .venv used for lint/tests -- that one has no faster_whisper/torch.
set "PY=python"

%PY% -c "import faster_whisper" 1>nul 2>nul
if errorlevel 1 (
  echo [ERROR] The 'python' on PATH is missing the ML stack ^(faster_whisper^).
  echo         Install it:   cd desktop   ^&^&   %PY% -m pip install -e ".[dev]"
  echo         For the elena.local name also:   %PY% -m pip install zeroconf
  pause
  exit /b 1
)

echo.
echo   Jarvis - full live pipeline starting...
echo   Dashboard:  http://elena.local:8772    or  http://YOUR-PC-IP:8772
echo   (Press Ctrl+C in this window to stop everything.)
echo.

%PY% "%~dp0desktop\orchestrator.py"
set "RC=%ERRORLEVEL%"

echo.
echo   Orchestrator exited (code %RC%).
pause
endlocal
