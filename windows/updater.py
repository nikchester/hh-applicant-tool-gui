"""Self-update from GitHub releases, without a reinstall for ordinary releases.

The frozen executable carries the Python runtime and libraries. The application code
lives outside it, in a "code" directory: the bundled one inside the installation, or a
downloaded one under ~/.local/state/hh-applicant-tool-gui/updates/<version>.
packaging/entrypoint.py chooses the directory at every start.

A release publishes, next to the installer:
    update.json       {"version", "runtime", "code": {...}, "installer": {...}}
    update.json.sig   base64 ed25519 signature of update.json
    <code zip>        the code directory of that release

Only update.json is signed; it pins the SHA-256 of the code zip and of the installer.
If the runtime of the release equals ours, the code zip is enough: download, verify,
unpack, restart. Otherwise the signed installer runs silently and restarts the app.

Two channels: "stable" reads GitHub's "latest" release and never offers a beta;
"beta" lists the releases, takes the newest one, beta or not, and reads its files.
The listing is not signed: it only names a version, and update.json of that version
must be signed and carry the same version.
"""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import zipfile

REPOSITORY = "nikchester/hh-applicant-tool-gui"
LATEST = f"https://github.com/{REPOSITORY}/releases/latest/download/"
DOWNLOAD = f"https://github.com/{REPOSITORY}/releases/download/"
RELEASES = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=30"
CHANNELS = ("stable", "beta")
# Public half of the release signing key. The private half is only a CI secret.
PUBLIC_KEY = "SGWT6/fpRuHEbC04XXLBgb2xLhZOI1xoJC0D+OX87Q8="
ALLOWED_HOSTS = frozenset({"github.com", "objects.githubusercontent.com",
                           "release-assets.githubusercontent.com"})
MAX_METADATA = 64 * 1024
MAX_LISTING = 4 * 1024 * 1024
MAX_CODE = 20 * 1024 * 1024
MAX_INSTALLER = 400 * 1024 * 1024
# A release "0.6.0" or a beta "0.6.0-beta.1"; a beta sorts below its release.
VERSION = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}(?:-beta\.\d{1,3})?$")
ASSET = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
CODE_FILE = re.compile(r"^(?:[a-z_][a-z0-9_]{0,60}\.py|config\.example\.toml|version\.json)$")
INSTALLER_ARGS = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-")


class UpdateError(Exception):
    """Plain Russian reason; the app keeps working on the current version."""


def updates_dir():
    return Path.home() / ".local/state/hh-applicant-tool-gui/updates"


def current():
    """Version and runtime of the running code, set by packaging/entrypoint.py."""
    version, runtime = os.environ.get("HH_APP_VERSION", ""), os.environ.get("HH_APP_RUNTIME", "")
    return (version, runtime) if VERSION.match(version) and HEX64.match(runtime) else None


def enabled():
    return getattr(sys, "frozen", False) and os.name == "nt" and current() is not None


def version_key(version):
    """(major, minor, patch, 1 for a release or 0 for a beta, beta number): 0.6.0-beta.1 < 0.6.0."""
    number, _, beta = version.partition("-beta.")
    return tuple(int(part) for part in number.split(".")) + ((0, int(beta)) if beta else (1, 0))


def prerelease(version):
    return "-" in version


def default_channel(version):
    """Without a saved choice a beta install stays on betas and a stable one on stable releases."""
    return "beta" if version and prerelease(version) else "stable"


def _get(url, limit, opener, hosts=ALLOWED_HOSTS):
    request = Request(url, headers={"User-Agent": "hh-applicant-tool-gui-updater",
                                    "Accept": "application/vnd.github+json"})
    try:
        with opener(request, timeout=30) as response:
            host = (urlsplit(response.geturl()).hostname or "").lower()
            if host not in hosts:
                raise UpdateError("Обновление пришло не с GitHub.")
            data = response.read(limit + 1)
    except UpdateError:
        raise
    except (OSError, ValueError):
        raise UpdateError("Не удалось связаться с GitHub. Обновление проверю позже.") from None
    if len(data) > limit:
        raise UpdateError("Файл обновления слишком большой.")
    return data


def _download(name, limit, opener=urlopen, base=LATEST):
    if not ASSET.match(name):
        raise UpdateError("Неверное имя файла обновления.")
    return _get(base + name, limit, opener)


def newest_version(opener=urlopen):
    """The newest published version, beta or not, from the GitHub release list."""
    data = _get(RELEASES, MAX_LISTING, opener, hosts=frozenset({"api.github.com"}))
    try:
        items = json.loads(data)
        if not isinstance(items, list):
            raise ValueError  # E.g. GitHub's rate-limit answer.
        versions = [item["tag_name"][1:] for item in items
                    if isinstance(item, dict) and not item.get("draft")
                    and isinstance(item.get("tag_name"), str) and item["tag_name"].startswith("v")
                    and VERSION.match(item["tag_name"][1:])]
    except (ValueError, TypeError, KeyError):
        raise UpdateError("Список релизов GitHub повреждён.") from None
    return max(versions, key=version_key, default=None)


def verify(metadata, signature, public_key=PUBLIC_KEY):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key))
        key.verify(base64.b64decode(signature.strip(), validate=True), metadata)
    except (InvalidSignature, ValueError):
        raise UpdateError("Подпись обновления не прошла проверку. Обновление не установлено.") from None


def parse(metadata):
    try:
        data = json.loads(metadata)
        files = {key: data[key] for key in ("code", "installer")}
        if (not VERSION.match(data["version"]) or not HEX64.match(data["runtime"])
                or any(set(item) != {"name", "sha256", "size"} or not ASSET.match(item["name"])
                       or not HEX64.match(item["sha256"]) or type(item["size"]) is not int
                       or item["size"] <= 0 for item in files.values())):
            raise ValueError
        return data
    except (ValueError, KeyError, TypeError, AttributeError):
        raise UpdateError("Описание обновления повреждено.") from None


def check(opener=urlopen, public_key=PUBLIC_KEY, channel=None):
    """Signed description of a newer release in the channel, or None when there is none.

    Never a downgrade: a beta install on the stable channel waits for a newer stable release.
    """
    running = current()
    if running is None:
        return None
    channel = channel or default_channel(running[0])
    if channel not in CHANNELS:
        raise UpdateError("Неизвестный канал обновлений.")
    base, expected = LATEST, None
    if channel == "beta":
        expected = newest_version(opener)
        if expected is None or version_key(expected) <= version_key(running[0]):
            return None
        base = DOWNLOAD + "v" + expected + "/"
    metadata = _download("update.json", MAX_METADATA, opener, base)
    signature = _download("update.json.sig", 1024, opener, base)
    verify(metadata, signature, public_key)
    release = parse(metadata)
    if expected is not None and release["version"] != expected:
        raise UpdateError("Описание обновления не совпало с релизом на GitHub.")
    # The stable channel never offers a beta, even if a beta became GitHub's "latest" by mistake.
    if channel == "stable" and prerelease(release["version"]):
        return None
    if version_key(release["version"]) <= version_key(running[0]):
        return None
    release["base"] = base  # Where its code zip and installer lie; not part of the signed text.
    return release


def _fetch_verified(item, limit, opener, base=LATEST):
    if item["size"] > limit:
        raise UpdateError("Файл обновления слишком большой.")
    if base != LATEST and not (base.startswith(DOWNLOAD + "v") and base.endswith("/")
                               and VERSION.match(base[len(DOWNLOAD) + 1:-1])):
        raise UpdateError("Неверный адрес обновления.")
    data = _download(item["name"], limit, opener, base)
    if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise UpdateError("Скачанный файл не совпал с подписанным описанием. Обновление не установлено.")
    return data


def _atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        json.dump(data, stream)
        name = stream.name
    os.replace(name, path)


def install_code(release, opener=urlopen, root=None):
    """Download and unpack the code of a release with our runtime. Returns its directory."""
    root = Path(root or updates_dir())
    data = _fetch_verified(release["code"], MAX_CODE, opener, release.get("base", LATEST))
    target = root / release["version"]
    staging = root / (release["version"] + ".tmp")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        with zipfile.ZipFile(__import__("io").BytesIO(data)) as archive:
            names = archive.namelist()
            if (not names or len(names) > 200 or len(set(names)) != len(names)
                    or any(not CODE_FILE.match(name) for name in names)):
                raise UpdateError("В архиве обновления неожиданные файлы. Обновление не установлено.")
            for name in names:
                (staging / name).write_bytes(archive.read(name))
        info = json.loads((staging / "version.json").read_text(encoding="utf-8"))
        if info != {"version": release["version"], "runtime": release["runtime"]}:
            raise UpdateError("Версия в архиве не совпала с описанием обновления.")
        if not (staging / "gui.py").is_file():
            raise UpdateError("В архиве обновления нет программы.")
        (staging / ".verified").write_text(release["code"]["sha256"], encoding="ascii")
        shutil.rmtree(target, ignore_errors=True)
        os.replace(staging, target)
    except (zipfile.BadZipFile, ValueError, OSError):
        raise UpdateError("Архив обновления повреждён.") from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    _atomic_json(root / "current.json", {"version": release["version"], "runtime": release["runtime"]})
    return target


def download_installer(release, opener=urlopen, root=None):
    root = Path(root or updates_dir())
    data = _fetch_verified(release["installer"], MAX_INSTALLER, opener, release.get("base", LATEST))
    root.mkdir(parents=True, exist_ok=True)
    path = root / release["installer"]["name"]
    path.write_bytes(data)
    return path


def prepare(release, opener=urlopen):
    """Make the release ready; returns the command that finishes the update after exit."""
    running = current()
    if running is None:
        raise UpdateError("Автообновление работает только в установленной программе.")
    if release["runtime"] == running[1]:
        install_code(release, opener)
        return [sys.executable]
    return [str(download_installer(release, opener)), *INSTALLER_ARGS]


def _quote(text):
    return "'" + str(text).replace("'", "''") + "'"


def launch(command, pid=None):
    """Start the next step detached; the caller then closes the window.

    An installer must not start before this process exits: it replaces our files.
    A hidden PowerShell waits for our PID, then starts the installer.
    """
    environment = {key: value for key, value in os.environ.items() if not key.startswith("HH_APP_")}
    if command[0] != sys.executable:
        script = (f"Wait-Process -Id {int(pid or os.getpid())} -ErrorAction SilentlyContinue; "
                  f"Start-Process -FilePath {_quote(command[0])} -ArgumentList "
                  + ",".join(_quote(argument) for argument in command[1:]))
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                   "-Command", script]
    flags = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(command, close_fds=True, creationflags=flags, env=environment)


def mark_started():
    """The window opened on this code: keep it, forget failed-start counts, drop old versions."""
    code = os.environ.get("HH_APP_CODE_DIR")
    root = updates_dir()
    if not code or not root.is_dir():
        return
    code = Path(code)
    keep = {code.name}
    if code.parent == root:
        (code / ".good").write_text("1", encoding="ascii")
        (code / ".boot").unlink(missing_ok=True)
    for path in root.iterdir():
        try:
            if path.is_dir() and path.name not in keep and VERSION.match(path.name):
                shutil.rmtree(path, ignore_errors=True)
            elif path.suffix == ".exe":
                path.unlink(missing_ok=True)  # A finished installer update.
        except OSError:
            pass  # An installer may still be closing; the next start removes it.
