"""Prepare download assets after update_bundle.py has signed versioned files."""

import hashlib
from pathlib import Path
import re
import shutil
import sys

STABLE_INSTALLER = "hh-applicant-tool-gui-windows-x64-setup.exe"
VERSION = re.compile(r"\d+\.\d+\.\d+(?:-beta\.\d+)?")


def prepare(directory, version):
    if not VERSION.fullmatch(version):
        raise ValueError("Unsupported release version")
    directory = Path(directory)
    setup = directory / f"hh-applicant-tool-gui-{version}-windows-x64-setup.exe"
    files = [setup, directory / f"hh-applicant-tool-gui-{version}-code.zip",
             directory / "update.json", directory / "update.json.sig"]
    if any(path.is_symlink() or not path.is_file() for path in files):
        raise ValueError("Missing or linked release asset")
    alias = directory / STABLE_INSTALLER
    # Never replace a pre-existing asset, including a dangling link.
    with alias.open("xb") as target, setup.open("rb") as source:
        shutil.copyfileobj(source, target)
    files.append(alias)
    lines = [hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name
             for path in sorted(files)]
    (directory / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="ascii")
    return alias


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: release_assets.py <installer directory> <version>")
    print(prepare(*sys.argv[1:]))
