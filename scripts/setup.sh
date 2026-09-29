#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
PYTHON="${PYTHON:-python3}"
"$PYTHON" -c 'import sys; assert (3,11) <= sys.version_info[:2] < (3,14), "Use Python 3.11–3.13"'
command -v git >/dev/null
"$PYTHON" scripts/fetch_dependencies.py
"$PYTHON" -m venv .venv
.venv/bin/python -m pip install -r requirements.txt .deps/EvalNet
mkdir -p .generated/ansible-tmp .generated/local-labs .generated/p4
printf '\nDependencies installed. Activate with: source .venv/bin/activate\n'
