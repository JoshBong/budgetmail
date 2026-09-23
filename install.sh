#!/usr/bin/env bash
# budgetmail installer: python3 check → setup wizard → first sync → service. Idempotent; rerun after `git pull`.
set -euo pipefail
cd "$(dirname "$0")"
PY=$(command -v python3 || true)
[ -n "$PY" ] || { echo "python3 not found. Debian/Raspberry Pi OS: sudo apt install python3 — macOS: xcode-select --install"; exit 1; }
"$PY" - <<'PYCHK' || { echo "python3 >= 3.11 required (found $($PY --version))"; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PYCHK
if [ ! -f "${BUDGETMAIL_HOME:-$HOME/.budgetmail}/config.json" ]; then
  "$PY" budgetmail.py setup
  echo; "$PY" budgetmail.py restore --from-mail || true      # a previous machine's backup, if one was ever emailed
  echo; echo "first pull of your whole mailbox (a few minutes)…"; "$PY" budgetmail.py sync --full
fi
echo
if [ "$(uname)" = "Darwin" ]; then
  "$PY" budgetmail.py install
else
  echo "registering the systemd service (needs sudo once)…"
  sudo -E "$PY" budgetmail.py install
fi
echo
if ! curl -s localhost:11434/api/tags >/dev/null 2>&1; then
  echo "optional: the local merchant classifier needs Ollama (https://ollama.com/download) — after installing it:"
  echo "  ollama pull qwen2.5:3b && ./budgetmail classify"
elif ! curl -s localhost:11434/api/tags | grep -q '"qwen2.5:3b"'; then
  read -r -p "pull the merchant-classifier model (qwen2.5:3b, ~2 GB, runs on this box, nothing leaves it)? [y/N] " yn
  if [ "${yn:-n}" = "y" ] || [ "${yn:-n}" = "Y" ]; then ollama pull qwen2.5:3b && "$PY" budgetmail.py classify || true; fi
fi
