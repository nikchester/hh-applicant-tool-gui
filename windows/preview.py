"""Construct fixed HH dry-run arguments without PowerShell native quoting."""

import os
import sys

from preview_hh import run


def arguments(config, resume_id, search):
    """Empty search means HH's suggested vacancies for the resume (similar_vacancies)."""
    if not resume_id:
        raise SystemExit("Resume ID is required")
    return [
        "--config-dir", config, "--profile-id", ".", "--no-handle-captcha",
        "apply", "--dry-run", "--no-send-email", "--no-ai-filter", "--no-skip-tests",
        "--per-page", "10", "--total-pages", "1", "--max-responses", "1",
        "--work-format", "REMOTE", "--currency", "RUR", "--salary", "80000",
        "--no-only-with-salary", "--search-field", "name",
        "--order-by", "publication_time",
        f"--resume-id={resume_id}",
    ] + ([f"--search={search}"] if search else [])


if __name__ == "__main__":
    sys.exit(run(arguments(
        os.environ.get("CONFIG_DIR", ""),
        os.environ.get("HH_APPLICANT_TOOL_RESUME_ID", ""),
        os.environ.get("HH_APPLICANT_TOOL_SEARCH", ""),
    )))
