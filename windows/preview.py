"""Construct fixed HH dry-run arguments without PowerShell native quoting."""

import os
import sys

from preview_hh import run
from vacancy_filters import load, search_arguments


def arguments(config, resume_id, search):
    """Empty search means HH's suggested vacancies for the resume (similar_vacancies).

    The HH search part comes from the saved vacancy filters; without a filters file it is fixed.
    """
    if not resume_id:
        raise SystemExit("Resume ID is required")
    return [
        "--config-dir", config, "--profile-id", ".", "--no-handle-captcha",
        "apply", "--dry-run", "--no-send-email", "--no-ai-filter", "--no-skip-tests",
        "--per-page", "10", "--total-pages", "1", "--max-responses", "1",
    ] + search_arguments(load()) + [
        f"--resume-id={resume_id}",
    ] + ([f"--search={search}"] if search else [])


if __name__ == "__main__":
    sys.exit(run(arguments(
        os.environ.get("CONFIG_DIR", ""),
        os.environ.get("HH_APPLICANT_TOOL_RESUME_ID", ""),
        os.environ.get("HH_APPLICANT_TOOL_SEARCH", ""),
    )))
