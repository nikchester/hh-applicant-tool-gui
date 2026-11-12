"""Offline self-update checks: signatures, downloads, unpacking and code selection."""

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "packaging"))
import entrypoint  # noqa: E402
import update_bundle  # noqa: E402
import updater  # noqa: E402

RUNTIME = "a" * 64


def keypair():
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    return key, public


def code_zip(version, runtime=RUNTIME, extra=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("gui.py", "print('gui')\n")
        archive.writestr("updater.py", "\n")
        archive.writestr("config.example.toml", "[general]\n")
        archive.writestr("version.json", json.dumps({"version": version, "runtime": runtime}))
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def describe(name, data):
    return {"name": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


class FakeResponse(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(data)
        self.url = url

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Release:
    """A fake GitHub release: name -> bytes, served from release-assets."""

    def __init__(self, key, version="9.9.9", runtime=RUNTIME, code=None, host="release-assets.githubusercontent.com"):
        self.code = code if code is not None else code_zip(version, runtime)
        self.installer = b"MZ fake installer"
        self.files = {"app-code.zip": self.code, "setup.exe": self.installer}
        metadata = json.dumps({"version": version, "runtime": runtime,
                               "code": describe("app-code.zip", self.code),
                               "installer": describe("setup.exe", self.installer)}).encode()
        self.files["update.json"] = metadata
        self.files["update.json.sig"] = base64.b64encode(key.sign(metadata))
        self.host, self.requests = host, []

    def open(self, request, timeout):
        name = request.full_url.rsplit("/", 1)[-1]
        self.requests.append(name)
        if name not in self.files:
            raise OSError("404")
        return FakeResponse(self.files[name], f"https://{self.host}/x/{name}")


class UpdaterChecks(unittest.TestCase):
    def setUp(self):
        self.key, self.public = keypair()
        self.env = patch.dict(os.environ, {"HH_APP_VERSION": "1.0.0", "HH_APP_RUNTIME": RUNTIME})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_check_finds_a_newer_signed_release(self):
        release = Release(self.key)
        found = updater.check(opener=release.open, public_key=self.public)
        self.assertEqual(found["version"], "9.9.9")
        self.assertEqual(release.requests, ["update.json", "update.json.sig"])

    def test_same_or_older_version_is_not_an_update(self):
        for version in ("1.0.0", "0.9.9"):
            release = Release(self.key, version=version)
            self.assertIsNone(updater.check(opener=release.open, public_key=self.public))

    def test_foreign_signature_is_rejected(self):
        release = Release(Ed25519PrivateKey.generate())
        with self.assertRaisesRegex(updater.UpdateError, "Подпись"):
            updater.check(opener=release.open, public_key=self.public)

    def test_redirect_outside_github_is_rejected(self):
        release = Release(self.key, host="evil.example")
        with self.assertRaisesRegex(updater.UpdateError, "не с GitHub"):
            updater.check(opener=release.open, public_key=self.public)

    def test_network_failure_is_a_plain_message(self):
        def offline(request, timeout):
            raise OSError("no route")
        with self.assertRaisesRegex(updater.UpdateError, "GitHub"):
            updater.check(opener=offline, public_key=self.public)

    def test_code_install_verifies_hash_and_switches_current(self):
        release = Release(self.key)
        metadata = updater.parse(release.files["update.json"])
        with tempfile.TemporaryDirectory() as root:
            target = updater.install_code(metadata, opener=release.open, root=root)
            self.assertEqual((target / "gui.py").read_text(), "print('gui')\n")
            self.assertTrue((target / ".verified").is_file())
            self.assertEqual(json.loads((Path(root) / "current.json").read_text()),
                             {"version": "9.9.9", "runtime": RUNTIME})

    def test_tampered_code_is_rejected(self):
        release = Release(self.key)
        metadata = updater.parse(release.files["update.json"])
        release.files["app-code.zip"] = code_zip("9.9.9", extra={"evil.py": "x"})
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(updater.UpdateError, "не совпал"):
                updater.install_code(metadata, opener=release.open, root=root)
            self.assertFalse((Path(root) / "current.json").exists())

    def test_unexpected_or_traversal_names_are_rejected(self):
        for extra in ({"../evil.py": "x"}, {"sub/dir.py": "x"}, {"run.exe": "x"}):
            code = code_zip("9.9.9", extra=extra)
            release = Release(self.key, code=code)
            metadata = updater.parse(release.files["update.json"])
            with tempfile.TemporaryDirectory() as root:
                with self.assertRaisesRegex(updater.UpdateError, "неожиданные файлы"):
                    updater.install_code(metadata, opener=release.open, root=root)
                self.assertEqual(list(Path(root).iterdir()), [])

    def test_version_mismatch_inside_zip_is_rejected(self):
        release = Release(self.key, code=code_zip("8.8.8"))
        metadata = updater.parse(release.files["update.json"])
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(updater.UpdateError, "Версия в архиве"):
                updater.install_code(metadata, opener=release.open, root=root)

    def test_other_runtime_uses_the_installer(self):
        release = Release(self.key, runtime="b" * 64)
        metadata = updater.parse(release.files["update.json"])
        with tempfile.TemporaryDirectory() as root, \
                patch.object(updater, "updates_dir", return_value=Path(root)):
            command = updater.prepare(metadata, opener=release.open)
            self.assertEqual(Path(command[0]).read_bytes(), release.installer)
            self.assertIn("/VERYSILENT", command)
            self.assertFalse((Path(root) / "current.json").exists())

    def test_same_runtime_restarts_the_executable(self):
        release = Release(self.key)
        metadata = updater.parse(release.files["update.json"])
        with tempfile.TemporaryDirectory() as root, \
                patch.object(updater, "updates_dir", return_value=Path(root)):
            self.assertEqual(updater.prepare(metadata, opener=release.open), [sys.executable])

    def test_launch_waits_for_this_process_before_the_installer(self):
        with patch.object(updater.subprocess, "Popen") as popen:
            updater.launch([r"C:\Users\O'Neil\setup.exe", "/VERYSILENT"], pid=4242)
        command = popen.call_args.args[0]
        self.assertEqual(command[0], "powershell.exe")
        self.assertIn("Wait-Process -Id 4242", command[-1])
        self.assertIn("'C:\\Users\\O''Neil\\setup.exe'", command[-1])
        self.assertNotIn("HH_APP_VERSION", popen.call_args.kwargs["env"])


class EntrypointChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.bundled, self.updates = root / "code", root / "updates"
        self.bundled.mkdir()
        (self.bundled / "version.json").write_text(json.dumps({"version": "1.0.0", "runtime": RUNTIME}))

    def downloaded(self, version="1.1.0", runtime=RUNTIME, verified=True):
        directory = self.updates / version
        directory.mkdir(parents=True)
        (directory / "version.json").write_text(json.dumps({"version": version, "runtime": runtime}))
        if verified:
            (directory / ".verified").write_text("x")
        (self.updates / "current.json").write_text(json.dumps({"version": version, "runtime": runtime}))
        return directory

    def choose(self, window=True):
        return entrypoint.choose_code(self.bundled, self.updates, window=window)[0]

    def test_bundled_without_updates(self):
        self.assertEqual(self.choose(), self.bundled)

    def test_newer_verified_download_wins(self):
        directory = self.downloaded()
        self.assertEqual(self.choose(), directory)

    def test_unverified_other_runtime_or_older_download_is_ignored(self):
        self.downloaded(verified=False)
        self.assertEqual(self.choose(), self.bundled)

    def test_other_runtime_is_ignored(self):
        self.downloaded(runtime="b" * 64)
        self.assertEqual(self.choose(), self.bundled)

    def test_installer_update_makes_bundled_newer(self):
        self.downloaded(version="0.9.0")
        self.assertEqual(self.choose(), self.bundled)

    def test_two_failed_window_starts_roll_back(self):
        directory = self.downloaded()
        self.assertEqual(self.choose(), directory)
        self.assertEqual(self.choose(), directory)
        self.assertEqual(self.choose(), self.bundled)  # Third start: the window never opened.
        self.assertTrue((directory / ".bad").exists())
        self.assertEqual(self.choose(), self.bundled)

    def test_good_start_stops_counting_and_workers_never_count(self):
        directory = self.downloaded()
        for _ in range(5):
            self.assertEqual(self.choose(window=False), directory)
        (directory / ".good").write_text("1")
        for _ in range(5):
            self.assertEqual(self.choose(), directory)

    def test_current_cannot_point_outside_updates(self):
        self.updates.mkdir()
        (self.updates / "current.json").write_text(json.dumps({"version": "../code", "runtime": RUNTIME}))
        self.assertEqual(self.choose(), self.bundled)


class BundleChecks(unittest.TestCase):
    def test_release_files_install_with_the_published_key(self):
        key, public = keypair()
        raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp) / "app"
            code = app / "_internal" / "code"
            code.mkdir(parents=True)
            (code / "gui.py").write_text("x = 1\n")
            (code / "updater.py").write_text("\n")
            (code / "config.example.toml").write_text("[general]\n")
            (code / "version.json").write_text(json.dumps({"version": "9.9.9", "runtime": RUNTIME}))
            installer = Path(temp) / "setup.exe"
            installer.write_bytes(b"MZ")
            out = Path(temp) / "out"
            archive = update_bundle.build(app, installer, out, base64.b64encode(raw).decode())
            files = {path.name: path.read_bytes() for path in out.iterdir()}
            release = Release(key)
            release.files = files
            with patch.dict(os.environ, {"HH_APP_VERSION": "1.0.0", "HH_APP_RUNTIME": RUNTIME}):
                found = updater.check(opener=release.open, public_key=public)
                self.assertEqual(found["code"]["name"], archive.name)
                target = updater.install_code(found, opener=release.open, root=Path(temp) / "updates")
            self.assertEqual((target / "gui.py").read_text(), "x = 1\n")


class PublicKeyCheck(unittest.TestCase):
    def test_public_key_is_a_raw_ed25519_key(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(base64.b64decode(updater.PUBLIC_KEY))


if __name__ == "__main__":
    unittest.main(verbosity=2)
