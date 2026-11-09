"""Extract one version's section from CHANGELOG.md as GitHub release notes.

Usage: release_notes.py VERSION CHANGELOG OUTPUT
The section starts at "## VERSION" and ends before the next "## " heading.
The heading itself is dropped: the release title already names the version.
"""
import re
import sys
from pathlib import Path


def extract(version: str, text: str) -> str:
    heading = re.compile(rf"^## {re.escape(version)}(\s|$)")
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        raise SystemExit(f"CHANGELOG has no section for {version}")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    body = "\n".join(lines[start + 1:end]).strip()
    if not body:
        raise SystemExit(f"CHANGELOG section for {version} is empty")
    return body + "\n"


def main(argv: list[str]) -> None:
    if len(argv) != 3:
        raise SystemExit(__doc__)
    version, changelog, output = argv
    notes = extract(version, Path(changelog).read_text(encoding="utf-8"))
    Path(output).write_text(notes, encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])
