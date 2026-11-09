"""Move pre-rename PocketNode profiles to the hh-applicant-tool names, once.

The project was called PocketNode HH. Profiles, HH/ChatMock authorization and the API key
lived under that name. Without this move the renamed app would silently start from an
empty profile. Only stdlib: PowerShell wrappers run this file with `python -I`.
"""

import os
from pathlib import Path
import sys

# (parent under home, old folder, new folder)
FOLDERS = (
    (".config", "pocketnode-hh", "hh-applicant-tool-cli"),
    (".config", "pocketnode-hh-gui", "hh-applicant-tool-gui"),
    (".local/state", "pocketnode-hh", "hh-applicant-tool-cli"),
    (".local/state", "pocketnode-hh-gui", "hh-applicant-tool-gui"),
)


class MigrationError(RuntimeError):
    """A legacy profile exists but could not be moved; nothing was started."""


def migrate_folders(home=None):
    """Rename each old folder whose new folder does not exist yet. Never merge or delete."""
    home = Path.home() if home is None else Path(home)
    moved = []
    for parent, old, new in FOLDERS:
        source, target = home / parent / old, home / parent / new
        if not source.is_dir() or source.is_symlink() or os.path.lexists(target):
            continue
        try:
            os.rename(source, target)
        except OSError as error:
            raise MigrationError(
                f"Не удалось переименовать папку профиля {source} в {target}. "
                "Закройте старую версию программы и окна PowerShell с её скриптами, "
                "затем запустите программу снова.") from error
        moved.append(target)
    return moved


def migrate_all(home=None):
    """GUI start: folders first, then API keys in Windows Credential Manager."""
    moved = migrate_folders(home)
    if os.name == "nt":
        import ai_provider
        try:
            ai_provider.migrate_legacy_keys()
        except OSError as error:
            raise MigrationError(str(error)) from error
    return moved


def main():
    try:
        migrate_folders()
    except MigrationError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
