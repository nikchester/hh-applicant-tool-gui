"""Packaging-only dispatch; smoke branches run before app/profile/Tk startup.

The application modules are not frozen into the executable. They live in a code
directory, so an update can replace them without a reinstall (see updater.py):

* bundled: <_internal>/code, installed with the executable;
* downloaded: ~/.local/state/hh-applicant-tool-gui/updates/<version>, used only when it
  is newer than the bundled code, was built for this exact runtime, passed signature
  checks (.verified) and did not fail to open the window twice (.boot, .bad).

This file is frozen and never updated, so it stays small and conservative.
"""

import json
import os
from pathlib import Path
import sys

MAX_FAILED_STARTS = 2


def _version_key(version):
    """Same order as updater.version_key: a beta sorts below its release (0.6.0-beta.1 < 0.6.0)."""
    number, _, beta = version.partition("-beta.")
    return tuple(int(part) for part in number.split(".")) + ((0, int(beta)) if beta else (1, 0))


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def choose_code(bundled, updates, *, window):
    """Code directory to run. `window` counts a start of the GUI, not of a worker."""
    info = _read_json(bundled / "version.json")
    if info is None:
        raise SystemExit("Bundled application code is missing")
    selected = _read_json(updates / "current.json") or {}
    try:
        candidate = updates / selected["version"]
        if (selected.get("runtime") != info["runtime"] or candidate.parent != updates
                or _version_key(selected["version"]) <= _version_key(info["version"])
                or not (candidate / ".verified").is_file() or (candidate / ".bad").exists()
                or _read_json(candidate / "version.json") != {"version": selected["version"],
                                                               "runtime": info["runtime"]}):
            return bundled, info
    except (KeyError, TypeError, ValueError, AttributeError):
        return bundled, info
    if window and not (candidate / ".good").exists():
        boot = candidate / ".boot"
        try:
            attempts = int(boot.read_text(encoding="ascii") or 0)
        except (OSError, ValueError):
            attempts = 0
        try:
            if attempts >= MAX_FAILED_STARTS:
                (candidate / ".bad").write_text("window did not open", encoding="ascii")
                return bundled, info  # Roll back to the installed code.
            boot.write_text(str(attempts + 1), encoding="ascii")
        except OSError:
            return bundled, info
    return candidate, {"version": selected["version"], "runtime": info["runtime"]}


def activate_code():
    if not getattr(sys, "frozen", False):
        return  # Source runs import windows/*.py directly.
    inherited = os.environ.get("HH_APP_CODE_DIR")
    bundled = Path(sys._MEIPASS) / "code"
    updates = Path.home() / ".local/state/hh-applicant-tool-gui/updates"
    if sys.argv[1:2] and sys.argv[1].startswith("--packaging-"):
        code, info = bundled, _read_json(bundled / "version.json")  # Smoke checks test this build.
    elif inherited and (Path(inherited) == bundled or Path(inherited).parent == updates):
        code, info = Path(inherited), _read_json(Path(inherited) / "version.json")
    else:
        # Only the window counts starts; its workers inherit HH_APP_CODE_DIR above.
        code, info = choose_code(bundled, updates, window=not sys.argv[1:])
    if info is None:
        code, info = choose_code(bundled, updates, window=False)
    os.environ.update(HH_APP_CODE_DIR=str(code), HH_APP_VERSION=info["version"],
                      HH_APP_RUNTIME=info["runtime"])
    sys.path.insert(0, str(code))


def main():
    activate_code()
    if sys.argv[1:] in (["--packaging-smoke-check"], ["--packaging-smoke-worker"],
                       ["--packaging-gateway-smoke"]):
        from gui_runtime import attach_standard_streams
        attach_standard_streams()
        from smoke import gateway_smoke, smoke_check, smoke_worker
        return {"--packaging-smoke-check": smoke_check,
                "--packaging-smoke-worker": smoke_worker,
                "--packaging-gateway-smoke": gateway_smoke}[sys.argv[1]]()
    from gui import main as gui_main
    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
