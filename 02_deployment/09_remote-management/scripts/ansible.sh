#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MANAGEMENT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${HOME}/.venvs/ott-ansible"

if [[ ! -x "${VENV_DIR}/bin/ansible" ]]; then
  echo "Ansible is not installed. Run scripts/install_control_node.sh first." >&2
  exit 1
fi

if [[ $# -lt 1 ]]; then
  echo "Usage: bash scripts/ansible.sh HOST_PATTERN [ANSIBLE_OPTIONS...]" >&2
  exit 1
fi

export ANSIBLE_CONFIG="${MANAGEMENT_DIR}/ansible.cfg"
cd "${MANAGEMENT_DIR}"
exec "${VENV_DIR}/bin/ansible" "$@"
