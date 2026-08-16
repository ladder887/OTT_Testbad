#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MANAGEMENT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${HOME}/.venvs/ott-ansible"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is missing. Run: sudo apt install -y python3 python3-venv" >&2
  exit 1
fi

if ! python3 - <<'PY'
import sys

raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
then
  echo "ansible-core 2.21 requires Python 3.12 or newer on ott-control." >&2
  echo "Detected: $(python3 --version 2>&1)" >&2
  echo "Use the current 64-bit Raspberry Pi OS release, then run this script again." >&2
  exit 1
fi

echo "[1/3] Creating the isolated Python environment: ${VENV_DIR}"
if ! python3 -m venv "${VENV_DIR}"; then
  echo "Could not create the virtual environment." >&2
  echo "Run: sudo apt install -y python3-venv" >&2
  exit 1
fi

echo "[2/3] Updating pip inside the isolated environment"
"${VENV_DIR}/bin/python" -m pip install --upgrade pip

echo "[3/3] Installing the pinned ott-control dependencies"
"${VENV_DIR}/bin/python" -m pip install \
  -r "${MANAGEMENT_DIR}/requirements-control.txt"

echo
echo "ott-control setup complete."
echo "Installed: $("${VENV_DIR}/bin/ansible" --version | head -n 1)"
echo "Python environment: ${VENV_DIR}"
echo "Only ott-control was changed; no remote device was contacted."
