#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${CUMCM_PYTHON:-}"
if [[ -z "$python_bin" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    python_bin="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    python_bin="$(command -v python)"
  else
    echo "Python was not found. Set CUMCM_PYTHON to a Python executable." >&2
    exit 2
  fi
fi

exec "$python_bin" "$project_root/tools/launch_claude_deepseek.py" "$@"
