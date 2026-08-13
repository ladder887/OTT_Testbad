#!/usr/bin/env bash
set -euo pipefail

DEPLOY_USER="ottadmin"

usage() {
  echo "Usage: sudo bash bootstrap_node.sh PUBLIC_KEY_FILE" >&2
}

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script as root." >&2
  exit 1
fi

if [[ $# -ne 1 ]]; then
  usage
  exit 1
fi

PUBLIC_KEY_FILE="$1"
if [[ ! -f "${PUBLIC_KEY_FILE}" ]]; then
  echo "Public key file does not exist: ${PUBLIC_KEY_FILE}" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1 || ! getent group docker >/dev/null 2>&1; then
  echo "Install Docker Engine and the Compose plugin before running this script." >&2
  exit 1
fi

ssh-keygen -lf "${PUBLIC_KEY_FILE}" >/dev/null

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  ca-certificates \
  curl \
  git \
  python3 \
  rsync

if ! id "${DEPLOY_USER}" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "${DEPLOY_USER}"
fi

usermod -aG docker "${DEPLOY_USER}"

install -d -m 0700 -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" \
  "/home/${DEPLOY_USER}/.ssh"
AUTHORIZED_KEYS="/home/${DEPLOY_USER}/.ssh/authorized_keys"
touch "${AUTHORIZED_KEYS}"
PUBLIC_KEY="$(cat "${PUBLIC_KEY_FILE}")"
if ! grep -qxF "${PUBLIC_KEY}" "${AUTHORIZED_KEYS}"; then
  printf '%s\n' "${PUBLIC_KEY}" >> "${AUTHORIZED_KEYS}"
fi
chown "${DEPLOY_USER}:${DEPLOY_USER}" "${AUTHORIZED_KEYS}"
chmod 0600 "${AUTHORIZED_KEYS}"

echo "Prepared ${DEPLOY_USER}. Docker Engine and Compose must be verified separately."
