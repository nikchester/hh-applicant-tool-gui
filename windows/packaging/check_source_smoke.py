"""Fresh-process, offline source smoke using prepared dependencies; never pip/auth."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from build_support import stage_sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hh-site-packages", type=Path)
    parser.add_argument("--chatmock-site-packages", type=Path)
    args = parser.parse_args()
    overlay = []
    for path in (args.chatmock_site_packages, args.hh_site_packages):
        if path is not None:
            if not path.is_dir():
                parser.error("Explicit dependency overlay directory is missing")
            overlay.append(str(path.resolve()))
    preferred_temp = Path(tempfile.gettempdir()) / "opencode"
    with tempfile.TemporaryDirectory(prefix="hh-source-smoke-", dir=preferred_temp if preferred_temp.is_dir() else None) as temporary:
        stage = stage_sources(Path(temporary) / "stage")
        environment = dict(os.environ, PYTHONPATH=os.pathsep.join([str(stage), *overlay]),
                           PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        # Separate fresh interpreters, not imports in this runner or a reused test process.
        for mode in ("--packaging-gateway-smoke", "--packaging-smoke-check"):
            result = subprocess.run([sys.executable, "-B", str(stage / "entrypoint.py"), mode],
                                    env=environment, input="", capture_output=True, text=True,
                                    encoding="utf-8", timeout=120, **options)
            print(result.stdout, end="")
            if result.returncode:
                print(result.stderr, file=sys.stderr, end="")
                return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
