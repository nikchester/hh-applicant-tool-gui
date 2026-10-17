#!/data/data/com.termux/files/usr/bin/sh
set -eu
umask 077

ROOT="$HOME/.local/share/pocketnode-hh"
export CHATGPT_LOCAL_HOME="$HOME/.local/state/pocketnode-hh/chatmock"
export CODEX_HOME="$CHATGPT_LOCAL_HOME"

if [ "$#" -ne 1 ]; then
    printf 'Usage: %s login|serve\n' "$0" >&2
    exit 2
fi

case "$1" in
    login)
        mkdir -p "$CHATGPT_LOCAL_HOME"
        chmod 700 "$CHATGPT_LOCAL_HOME"
        if [ -e "$CHATGPT_LOCAL_HOME/auth.json" ]; then
            printf 'A separate ChatMock login already exists; not overwriting it.\n' >&2
            exit 2
        fi
        exec "$ROOT/venvs/chatmock/bin/chatmock" login --headless
        ;;
    serve)
        # Require our own login; never start using a fallback ~/.codex account.
        "$ROOT/venvs/chatmock/bin/python" - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["CHATGPT_LOCAL_HOME"]) / "auth.json"
try:
    auth = json.loads(path.read_text())
    tokens = auth["tokens"]
    if not all(isinstance(tokens.get(k), str) and tokens[k]
               for k in ("access_token", "refresh_token", "id_token")):
        raise ValueError("incomplete login")
except (OSError, ValueError, KeyError, TypeError, AttributeError):
    raise SystemExit("ChatMock login required: run chatmock.sh login first.")
PY
        exec "$ROOT/venvs/chatmock/bin/chatmock" serve \
            --host 127.0.0.1 --port 8000 \
            --reasoning-summary none --reasoning-compat legacy \
            --no-enable-web-search --no-fast-mode
        ;;
    *)
        printf 'Only login and serve are supported.\n' >&2
        exit 2
        ;;
esac
