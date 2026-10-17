#!/data/data/com.termux/files/usr/bin/sh
set -eu
umask 077

ROOT="$HOME/.local/share/pocketnode-hh"
CONFIG="$HOME/.config/pocketnode-hh"
# v2.3.7 caches the config path in __post_init__, before parsing CLI flags.
export CONFIG_DIR="$CONFIG"
export HH_PROFILE_ID=.

if [ "$#" -ne 2 ] || [ -z "$1" ] || [ -z "$2" ]; then
    printf 'Usage: %s RESUME_ID "SEARCH TEXT"\n' "$0" >&2
    exit 2
fi

# Do not accidentally run with the example model or an unrelated profile.
"$ROOT/venvs/hh/bin/python" - "$CONFIG/config.toml" <<'PY'
import sys
import tomllib
from pathlib import Path

try:
    config = tomllib.loads(Path(sys.argv[1]).read_text())
    ai = config["openai"]["default"]
    if ai["base_url"] != "http://127.0.0.1:8000/v1":
        raise ValueError("only the local proxy is allowed")
    if not ai["model"] or ai["model"].startswith("CHOOSE_"):
        raise ValueError("choose an authenticated model first")
except (OSError, ValueError, KeyError, TypeError, AttributeError):
    raise SystemExit("Preview configuration is not ready; configure the local model first.")
PY

# No passthrough arguments: --dry-run cannot be overridden by the caller.
# max-responses does not cap previews, hence the explicit one-page search.
exec "$ROOT/venvs/hh/bin/python" "$ROOT/bootstrap/preview_hh.py" \
    --config-dir "$CONFIG" --profile-id . --no-handle-captcha \
    apply --dry-run --no-send-email --no-ai-filter --skip-tests \
    --per-page 3 --total-pages 1 --max-responses 1 \
    --work-format REMOTE --currency RUR --salary 80000 --only-with-salary \
    --search-field name \
    --resume-id="$1" --search="$2"
