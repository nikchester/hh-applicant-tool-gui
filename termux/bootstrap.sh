#!/data/data/com.termux/files/usr/bin/sh
# Install or verify the approved, preview-only stack on native Termux.
set -eu
umask 077

SOURCE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT="$HOME/.local/share/pocketnode-hh"
CONFIG="$HOME/.config/pocketnode-hh"

if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != "--check" ]; }; then
    printf 'Usage: %s [--check]\n' "$0" >&2
    exit 2
fi
if [ "${PREFIX:-}" != /data/data/com.termux/files/usr ] || [ ! -x "$PREFIX/bin/pkg" ]; then
    printf 'Native Termux is required (not a Linux VPS or proot).\n' >&2
    exit 2
fi

files='chatmock.sh hh-preview.sh preview_hh.py check_chatmock.py check_hh.py'
if [ "${1:-}" != --check ]; then
    # Detect local edits before making package or filesystem changes.
    for name in $files; do
        if [ -e "$ROOT/bootstrap/$name" ] && ! cmp -s "$SOURCE/$name" "$ROOT/bootstrap/$name"; then
            printf 'Existing %s differs. Back it up/reconcile it manually; nothing overwritten.\n' "$name" >&2
            exit 1
        fi
    done
    pkg install -y python python-pip python-brotli python-cryptography python-pillow git
    mkdir -p "$ROOT/venvs" "$ROOT/bootstrap" "$CONFIG" "$HOME/.local/state/pocketnode-hh/chatmock"
    chmod 700 "$ROOT" "$ROOT/bootstrap" "$CONFIG" "$HOME/.local/state/pocketnode-hh/chatmock"
    if [ ! -e "$ROOT/venvs/hh/bin/python" ]; then
        python -m venv --system-site-packages "$ROOT/venvs/hh"
    fi
    if [ ! -e "$ROOT/venvs/chatmock/bin/python" ]; then
        python -m venv "$ROOT/venvs/chatmock"
    fi
    "$ROOT/venvs/hh/bin/python" -m pip install -r "$SOURCE/requirements-hh.txt"
    "$ROOT/venvs/chatmock/bin/python" -m pip install -r "$SOURCE/requirements-chatmock.txt"
    for name in $files; do
        install -m 700 "$SOURCE/$name" "$ROOT/bootstrap/$name"
    done
    if [ ! -e "$CONFIG/config.toml" ]; then
        install -m 600 "$SOURCE/config.toml" "$CONFIG/config.toml"
    else
        printf 'Existing config.toml preserved; review differences from the repository manually.\n'
    fi
fi

for name in $files; do
    test -f "$ROOT/bootstrap/$name" || { printf 'Missing installed file: %s\n' "$name" >&2; exit 1; }
done
test -f "$CONFIG/config.toml" || { printf 'Missing config.toml\n' >&2; exit 1; }
"$ROOT/venvs/hh/bin/python" -m pip check
"$ROOT/venvs/chatmock/bin/python" -m pip check
"$ROOT/venvs/hh/bin/python" "$ROOT/bootstrap/check_hh.py"
"$ROOT/venvs/chatmock/bin/python" "$ROOT/bootstrap/check_chatmock.py"
"$ROOT/venvs/hh/bin/python" - "$CONFIG/config.toml" <<'PY'
import sys, tomllib
from pathlib import Path
c = tomllib.loads(Path(sys.argv[1]).read_text())
assert c['openai']['default']['base_url'] == 'http://127.0.0.1:8000/v1'
assert c['general']['cover_letter_resume_id']
print('Local gateway and resume configuration: OK (no account login tested)')
PY
printf 'Offline checks passed. No account login, preview, applications, or schedule started.\n'
