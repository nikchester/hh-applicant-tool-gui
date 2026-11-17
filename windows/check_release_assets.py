"""Offline release download contract checks; never execute an installer."""

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent / "packaging"))
from release_assets import prepare, STABLE_INSTALLER


class ReleaseAssetsChecks(unittest.TestCase):
    def fixture(self, root, version):
        files = {f"hh-applicant-tool-gui-{version}-windows-x64-setup.exe": b"synthetic EXE",
                 f"hh-applicant-tool-gui-{version}-code.zip": b"synthetic zip",
                 "update.json": b"signed metadata fixture", "update.json.sig": b"signature fixture"}
        for name, data in files.items():
            (root / name).write_bytes(data)
        return files

    def test_copy_hashes_and_signed_inputs_for_stable_and_beta(self):
        for version in ("0.6.0", "0.6.1-beta.1"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                files = self.fixture(root, version)
                (root / "SHA256SUMS.txt").write_text("old pre-signing checksums\n")
                unrelated = root / "keep.txt"
                unrelated.write_bytes(b"existing asset")
                alias = prepare(root, version)
                self.assertEqual(alias.read_bytes(), files[f"hh-applicant-tool-gui-{version}-windows-x64-setup.exe"])
                self.assertEqual(unrelated.read_bytes(), b"existing asset")
                for name, data in files.items():
                    self.assertEqual((root / name).read_bytes(), data)
                expected = {**files, STABLE_INSTALLER: alias.read_bytes()}
                sums = (root / "SHA256SUMS.txt").read_text(encoding="ascii").splitlines()
                self.assertEqual(len(sums), len(expected))
                for line in sums:
                    digest, name = line.split("  ")
                    self.assertEqual(digest, hashlib.sha256(expected.pop(name)).hexdigest())
                self.assertEqual(expected, {})

    def test_missing_inputs_and_bad_version_fail_before_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for version in ("../escape", "0.6.0"):
                with self.assertRaises(ValueError):
                    prepare(root, version)
                self.assertFalse((root / STABLE_INSTALLER).exists())
                self.assertFalse((root / "SHA256SUMS.txt").exists())

    def test_existing_alias_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root, "0.6.0")
            alias = root / STABLE_INSTALLER
            alias.write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                prepare(root, "0.6.0")
            self.assertEqual(alias.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
