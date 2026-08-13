#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MANAGEMENT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${HOME}/.venvs/ott-ansible"

python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/python" -m pip install \
  -r "${MANAGEMENT_DIR}/requirements-control.txt"

echo "Installed: $("${VENV_DIR}/bin/ansible" --version | head -n 1)"
echo "Control venv: ${VENV_DIR}"
