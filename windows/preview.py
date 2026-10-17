"""Construct fixed HH dry-run arguments without PowerShell native quoting."""

import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "termux"))
from preview_hh import run  # noqa: E402


def arguments(config, resume_id, search):
    if not resume_id or not search:
        raise SystemExit("Resume ID and search text are required")
    return [
        "--config-dir", config, "--profile-id", ".", "--no-handle-captcha",
        "apply", "--dry-run", "--no-send-email", "--no-ai-filter", "--skip-tests",
        "--per-page", "3", "--total-pages", "1", "--max-responses", "1",
        "--work-format", "REMOTE", "--currency", "RUR", "--salary", "80000",
        "--only-with-salary", "--search-field", "name",
        f"--resume-id={resume_id}", f"--search={search}",
    ]


if __name__ == "__main__":
    sys.exit(run(arguments(
        os.environ.get("CONFIG_DIR", ""),
        os.environ.get("POCKETNODE_HH_RESUME_ID", ""),
        os.environ.get("POCKETNODE_HH_SEARCH", ""),
    )))
