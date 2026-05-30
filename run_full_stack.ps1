# Launch the pet on THIS desktop and let Claude drive it live (everything but LM Studio).
#
# This runs local_loop.py, which uses the desktop's own mic / webcam / speaker
# (body movements are printed to the console, since there's no Teensy attached),
# and with --mcp also serves the live MCP HTTP binding so the human's Claude can
# drive this same pet over the same RobotTools / WorldState. Net result, one
# command:  you SPEAK to it, it replies on the speaker + sees via the webcam, and
# Claude (Claude Desktop "robot-desk-pet-live", or the inspector) is connected live.
#
#   - local LLM brain : your running LM Studio (Gemma) on :1234 — start it yourself
#   - voice in        : desktop microphone (Whisper, GPU)
#   - voice out       : desktop speaker (Kokoro TTS)
#   - vision          : desktop webcam -> see()  (Gemma sees frames, unified mode)
#   - live MCP server : http://127.0.0.1:8770/mcp  (Claude drives this pet)
#
# This needs the FULL model stack (torch+CUDA, faster-whisper, kokoro), which lives
# on the system Python — NOT the lightweight .venv (that one is for lint/tests/
# stdio-MCP only). The script picks the right interpreter and tops up the two
# pure-python deps it's missing (websockets + mcp, idempotent).
#
# NOTE: this is the DESKTOP-ONLY runner. To talk to the real Pi/Teensy body over
# WebSocket instead, run `python desktop/orchestrator.py` (it takes mic audio from
# the Pi, not this machine's microphone).
#
# Usage (from anywhere):
#   powershell -ExecutionPolicy Bypass -File "C:\Users\persi\Desktop\Jarvis 1.0\run_full_stack.ps1"
# or:  .\run_full_stack.ps1            # voice + vision + live MCP (the full experience)
#      .\run_full_stack.ps1 -Text      # type to it instead of talking (still serves MCP)
#      .\run_full_stack.ps1 -NoVision  # skip the webcam
#      .\run_full_stack.ps1 -NoMcp     # don't expose the live MCP server

param(
    [switch]$Text,      # use the text REPL instead of the microphone
    [switch]$NoVision,  # don't enable the webcam / see()
    [switch]$NoMcp      # don't serve the live MCP HTTP binding
)

# Deliberately NOT "Stop": we probe for missing packages with `python -c import`,
# whose failing stderr PowerShell 5.1 would otherwise turn into a terminating
# NativeCommandError. We use explicit $LASTEXITCODE checks instead (each with exit 1).
$ErrorActionPreference = "Continue"

$Root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$Desktop = Join-Path $Root "desktop"

# Full-stack interpreter (has torch+CUDA, faster-whisper, kokoro, sounddevice).
# Override by setting $env:PET_PYTHON before launching if you move the install.
$Py = "C:\Users\persi\AppData\Local\Microsoft\WindowsApps\python.exe"
if ($env:PET_PYTHON) { $Py = $env:PET_PYTHON }

if (-not (Test-Path $Py)) {
    Write-Error "Full-stack Python not found at: $Py`nSet `$env:PET_PYTHON to the interpreter that has torch + faster-whisper."
    exit 1
}

# Build the local_loop argument list from the switches.
$loopArgs = @()
if (-not $Text)     { $loopArgs += "--voice" }
if (-not $NoVision) { $loopArgs += "--vision" }
if (-not $NoMcp)    { $loopArgs += "--mcp" }

Write-Host "=== Robot desk pet - desktop runner ===" -ForegroundColor Cyan
Write-Host "interpreter : $Py"
Write-Host "working dir : $Desktop"
Write-Host ("local_loop  : " + ($loopArgs -join " ") + "`n")

# --- 1. sanity: heavy deps must already be present (we do NOT install these) ---
$heavyCheck = & $Py -c 'import torch, faster_whisper, kokoro, sounddevice; print(torch.cuda.is_available())' 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "This interpreter is missing the model stack:`n$heavyCheck`nUse the Python that has torch+faster-whisper, or set `$env:PET_PYTHON."
    exit 1
}
Write-Host "model stack  : OK (cuda=$heavyCheck)" -ForegroundColor Green

# --- 2. top up the pure-python deps (idempotent). websockets is pulled in by
#        shared imports; mcp (+ starlette/uvicorn/sse-starlette) only when serving. -
$need = @()
& $Py -c "import websockets" 2>$null
if ($LASTEXITCODE -ne 0) { $need += "websockets" }
if (-not $NoMcp) {
    & $Py -c "import mcp" 2>$null
    if ($LASTEXITCODE -ne 0) { $need += "mcp>=1.0" }
}
if ($need.Count -gt 0) {
    Write-Host ("installing missing deps: " + ($need -join ", ")) -ForegroundColor Yellow
    & $Py -m pip install --quiet $need
    if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed."; exit 1 }
} else {
    Write-Host "glue deps    : OK" -ForegroundColor Green
}

# --- 3. warn (don't block) if LM Studio isn't serving the brain yet ----------
try {
    $null = Invoke-WebRequest -Uri "http://127.0.0.1:1234/v1/models" -TimeoutSec 2 -UseBasicParsing
    Write-Host "LM Studio    : reachable on :1234" -ForegroundColor Green
} catch {
    Write-Host "LM Studio    : NOT reachable on :1234 - start it and load Gemma," -ForegroundColor Yellow
    Write-Host "               or the pet will error on the first thing you say." -ForegroundColor Yellow
}

if (-not $NoMcp) {
    Write-Host "`nLive MCP server will be at http://127.0.0.1:8770/mcp" -ForegroundColor Cyan
    Write-Host "Restart Claude Desktop AFTER this is running to use 'robot-desk-pet-live'."
}
if ($Text) {
    Write-Host "`nText mode: type to the pet ('quit' to stop).`n" -ForegroundColor Cyan
} else {
    Write-Host "`nVoice mode: speak into your mic once it says 'listening' (Ctrl+C to stop).`n" -ForegroundColor Cyan
}

# Run in desktop/ so the relative paths (config, data/, persona) resolve.
Set-Location $Desktop
& $Py local_loop.py $loopArgs
exit $LASTEXITCODE
