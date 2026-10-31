#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/.venv-verifier"
COMMIT="8db8a114355a9d7fdf9a8d1d5c87f6aeebd18770"

echo "Installing llm-verifier from locked upstream commit $COMMIT"
if [[ ! -d "$VENV" ]]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install "git+https://github.com/llm-as-a-verifier/llm-as-a-verifier.git@$COMMIT"
"$VENV/bin/python" -c "import llm_verifier; assert llm_verifier.__version__ == '0.2.0', llm_verifier.__version__; print('llm-verifier', llm_verifier.__version__)"
echo "Installed faithful verifier environment: $VENV"
