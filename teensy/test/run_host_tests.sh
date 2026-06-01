#!/usr/bin/env bash
# Build + run the hardware-free emotion-logic host test (Plan §6 face subsystem).
#
# EmotionLibrary.h includes only <stdint.h>/<stddef.h>/<math.h>, so it compiles
# with a plain host C++ compiler — no Teensyduino, no U8g2, no PlatformIO.
#
# CRITICAL: do NOT add -ffast-math. set_gaze()'s "keep this axis" sentinel is NaN
# and -ffast-math folds isnan() to false, silently breaking that path.
#
# Usage:  ./run_host_tests.sh            (uses g++, or $CXX if set)
#         CXX=clang++ ./run_host_tests.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/test_emotion_logic.cpp"
out="$here/emotion_logic_test"
cxx="${CXX:-g++}"

if ! command -v "$cxx" >/dev/null 2>&1; then
  echo "error: C++ compiler '$cxx' not found on PATH." >&2
  echo "Install g++/clang++ (or set CXX) and re-run. The firmware itself builds" >&2
  echo "only under PlatformIO; this host test just needs a standard C++17 compiler." >&2
  exit 2
fi

# -O2 matches platformio.ini; -std=c++17 matches the Teensy core. NO -ffast-math.
flags=(-std=c++17 -O2 -Wall -Wextra -Werror -o "$out" "$src")
echo "Compiling: $cxx ${flags[*]}"
"$cxx" "${flags[@]}"

echo "Running: $out"
if "$out"; then
  echo "emotion-logic host test PASSED"
else
  rc=$?
  echo "emotion-logic host test FAILED (exit $rc)" >&2
  exit "$rc"
fi
