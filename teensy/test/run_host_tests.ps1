# Build + run the hardware-free emotion-logic host test (Plan §6 face subsystem).
#
# EmotionLibrary.h includes only <stdint.h>/<stddef.h>/<math.h>, so it compiles
# with a plain host C++ compiler — no Teensyduino, no U8g2, no PlatformIO.
#
# CRITICAL: do NOT add -ffast-math. set_gaze()'s "keep this axis" sentinel is NaN
# and -ffast-math folds isnan() to false, silently breaking that path.
#
# Usage:  .\run_host_tests.ps1            (uses g++ on PATH)
#         .\run_host_tests.ps1 -Cxx clang++
[CmdletBinding()]
param([string]$Cxx = "g++")

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$src  = Join-Path $here "test_emotion_logic.cpp"
$out  = Join-Path $here "emotion_logic_test.exe"

$compiler = Get-Command $Cxx -ErrorAction SilentlyContinue
if (-not $compiler) {
    Write-Error "C++ compiler '$Cxx' not found on PATH. Install MinGW-w64/MSYS2 g++ (or pass -Cxx clang++) and re-run. This box may not have a host toolchain; the firmware itself builds only under PlatformIO."
    exit 2
}

# -O2 matches platformio.ini's optimization; -std=c++17 matches the Teensy core;
# warnings-as-errors keeps the math layer clean. NO -ffast-math (see above).
$flags = @("-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", "-o", $out, $src)
Write-Host "Compiling: $Cxx $($flags -join ' ')"
& $Cxx @flags
if ($LASTEXITCODE -ne 0) { Write-Error "compile failed"; exit $LASTEXITCODE }

Write-Host "Running: $out"
& $out
$rc = $LASTEXITCODE
if ($rc -eq 0) { Write-Host "emotion-logic host test PASSED" }
else           { Write-Error "emotion-logic host test FAILED (exit $rc)" }
exit $rc
