#!/usr/bin/env bash
# Idempotent first-time install for the Pi side (Plan §7, M3). Run as the pet
# user with sudo privileges, from the repo's pi/ directory:
#
#   sudo ./setup.sh
#
# Installs apt deps, creates a venv at /opt/pet/venv, deploys the service files,
# configures the UART for a reliable 921600 baud link to the Teensy, and enables
# the systemd units. Safe to re-run.
set -euo pipefail

PET_USER="${SUDO_USER:-pet}"
PET_HOME="/opt/pet"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo." >&2
  exit 1
fi

echo "==> Installing apt dependencies"
apt-get update -y
apt-get install -y \
  python3 python3-venv python3-pip \
  libportaudio2 \
  libopenblas0 libjpeg62-turbo \
  git

echo "==> Configuring UART (Plan §3.1 — reliable 921600 needs the PL011, not the mini-UART)"
# On the Pi Zero 2 W the stable PL011 is tied to Bluetooth by default and
# /dev/serial0 falls back to the flaky mini-UART. Free the PL011 for the GPIO
# header and take the serial console off it.
BOOT_CONFIG="/boot/firmware/config.txt"
[[ -f "$BOOT_CONFIG" ]] || BOOT_CONFIG="/boot/config.txt"
CMDLINE="/boot/firmware/cmdline.txt"
[[ -f "$CMDLINE" ]] || CMDLINE="/boot/cmdline.txt"

add_config_line() {  # idempotent append
  local line="$1"
  grep -qxF "$line" "$BOOT_CONFIG" || echo "$line" >> "$BOOT_CONFIG"
}
add_config_line "enable_uart=1"
add_config_line "dtoverlay=disable-bt"

if grep -q "console=serial0" "$CMDLINE"; then
  echo "    removing serial console from $CMDLINE"
  sed -i 's/console=serial0,[0-9]* //g' "$CMDLINE"
fi
systemctl disable --now hciuart.service 2>/dev/null || true

echo "==> Creating venv at $PET_HOME"
install -d -o "$PET_USER" -g "$PET_USER" "$PET_HOME"
if [[ ! -d "$PET_HOME/venv" ]]; then
  sudo -u "$PET_USER" python3 -m venv "$PET_HOME/venv"
fi
sudo -u "$PET_USER" "$PET_HOME/venv/bin/pip" install --upgrade pip
sudo -u "$PET_USER" "$PET_HOME/venv/bin/pip" install \
  "websockets>=13" "pyserial-asyncio>=0.6" "opencv-python-headless>=4.10" \
  "sounddevice>=0.5" "webrtcvad>=2.0.10" "numpy>=1.26"

echo "==> Deploying source to $PET_HOME"
install -o "$PET_USER" -g "$PET_USER" -m 644 \
  "$SRC_DIR/bridge.py" "$SRC_DIR/capture.py" "$SRC_DIR/protocol.py" \
  "$SRC_DIR/wsclient.py" "$SRC_DIR/config.toml" "$PET_HOME/"

echo "==> Installing + enabling systemd units"
install -m 644 "$SRC_DIR/systemd/pet-bridge.service" /etc/systemd/system/
install -m 644 "$SRC_DIR/systemd/pet-capture.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable pet-bridge.service pet-capture.service

echo
echo "==> Done. Edit $PET_HOME/config.toml ([desktop].host = your desktop IP),"
echo "    then REBOOT for the UART/Bluetooth changes to take effect:"
echo "      sudo reboot"
echo "    After reboot the services start automatically; check with:"
echo "      systemctl status pet-bridge pet-capture"
