#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
EXTRA=()
if [ -n "${ENVIRONMENT_VARS:-}" ]; then EXTRA+=(-e "@$ENVIRONMENT_VARS"); fi
ansible-playbook -i "${INVENTORY:-$ROOT_DIR/inventory/hosts.yml}" ansible/site.yml "${EXTRA[@]}" "$@"
