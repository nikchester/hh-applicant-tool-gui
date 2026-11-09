"""Dependency-free offline checks of packaging safety and the closed staging set."""

import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

WINDOWS = Path(__file__).resolve().parent
PACKAGING = WINDOWS / "packaging"
sys.path.insert(0, str(PACKAGING))
import build_support
import manifest
import smoke


class PackagingChecks(unittest.TestCase):
    def test_allowlist_covers_gui_and_all_local_runtime_imports(self):
        local = {p.stem for p in WINDOWS.glob("*.py")}
        included = set(manifest.APP_MODULES)
        self.assertTrue({"gui", "gui_worker", "chatmock_server", "gui_chatmock_auth",
                         "gui_owned_process", "gui_recovery"} <= included)
        self.assertEqual({p.stem for p in WINDOWS.glob("gui*.py")}, {n for n in included if n.startswith("gui")})
        for module in included:
            tree = ast.parse((WINDOWS / (module + ".py")).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = ([node.module.split(".")[0]] if isinstance(node, ast.ImportFrom) and node.module else
                         [name.name.split(".")[0] for name in node.names] if isinstance(node, ast.Import) else [])
                for name in names:
                    if name in local:
                        self.assertIn(name, included, f"{module} imports unpackaged local module {name}")

    def test_stage_is_closed_and_uses_only_packaging_generic_template(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = build_support.stage_sources(Path(temporary) / "stage")
            expected = {n + ".py" for n in manifest.APP_MODULES} | set(manifest.ENTRY_FILES) | {"config.example.toml"}
            self.assertEqual({p.name for p in stage.iterdir()}, expected)
            self.assertEqual((stage / "config.example.toml").read_bytes(), (PACKAGING / "config.example.toml").read_bytes())
            self.assertFalse(manifest.PRIVATE_NAMES & {p.name for p in stage.iterdir()})
            with self.assertRaises(FileExistsError):
                build_support.stage_sources(stage)

    def test_generic_schema_rejects_history_and_identifiers(self):
        build_support.validate_template(PACKAGING / "config.example.toml")
        original = (PACKAGING / "config.example.toml").read_text(encoding="utf-8")
        for changed in (original.replace("[general]", '[general]\ncover_letter_resume_id = "synthetic"'),
                        original.replace("[openai.default]", '[openai.default]\nhistory = ["synthetic"]')):
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "config.example.toml"
                path.write_text(changed, encoding="utf-8")
                with self.assertRaises(ValueError):
                    build_support.validate_template(path)

    def test_privacy_audit_fails_closed_and_recovers_after_removal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bad = root / "auth.json"
            bad.write_text("{}", encoding="utf-8")  # Synthetic fixture only.
            with self.assertRaises(ValueError):
                build_support.audit_tree(root)
            bad.unlink()
            self.assertEqual(build_support.audit_tree(root), [])

    def test_integrity_hashes_are_stable_and_detect_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            file = root / "fixture.txt"
            file.write_bytes(b"first")
            build_support.hashes(root)
            original = (root / "SHA256SUMS.txt").read_bytes()
            self.assertIn(hashlib.sha256(b"first").hexdigest().encode(), original)
            build_support.hashes(root)
            self.assertEqual(original, (root / "SHA256SUMS.txt").read_bytes())
            file.write_bytes(b"second")
            build_support.hashes(root)
            self.assertNotEqual(original, (root / "SHA256SUMS.txt").read_bytes())

    def test_incomplete_frozen_runtime_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(RuntimeError):
                build_support.audit_runtime(temporary)

    def test_smoke_guard_rejects_network_and_personal_paths(self):
        for event, args in (("socket.connect", ()), ("socket.bind", ()),
                            ("socket.getaddrinfo", ()), ("open", ("auth.json",)),
                            ("open", ("C:/synthetic/.config/hh-applicant-tool-gui/settings.txt",))):
            with self.assertRaises(RuntimeError):
                smoke.deny_external_io(event, args)
        smoke.deny_external_io("open", ("config.example.toml",))
        for profile in ("C:/u/.config/pocketnode-hh/config.toml", "C:/u/.local/state/hh-applicant-tool-cli/run/x"):
            with self.assertRaises(RuntimeError):
                smoke.deny_external_io("open", (profile,))
        # The portable app folder shares the GUI profile name but is not a profile.
        smoke.deny_external_io("open", ("C:/dist/app/hh-applicant-tool-gui/_internal/config.example.toml",))

    def test_gateway_smoke_exception_allows_only_its_exact_synthetic_fixture(self):
        fixture = Path("synthetic-temp/auth.json").absolute()
        smoke.deny_external_io("open", (fixture,), allowed_auth=fixture)
        with self.assertRaises(RuntimeError):
            smoke.deny_external_io("open", (fixture.parent / "other/auth.json",), allowed_auth=fixture)
        with self.assertRaises(RuntimeError):
            smoke.deny_external_io("socket.connect", (), allowed_auth=fixture)

    def test_entrypoint_dispatches_smoke_without_gui_import(self):
        spec = importlib.util.spec_from_file_location("packaging_entrypoint", PACKAGING / "entrypoint.py")
        entrypoint = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entrypoint)
        from types import SimpleNamespace
        runtime = SimpleNamespace(attach_standard_streams=lambda: None)
        with patch.dict(sys.modules, {"gui_runtime": runtime}), patch.object(smoke, "smoke_worker", return_value=0) as worker:
            with patch.object(sys, "argv", ["app.exe", "--packaging-smoke-worker"]):
                self.assertEqual(entrypoint.main(), 0)
                worker.assert_called_once()
        with patch.dict(sys.modules, {"gui_runtime": runtime}), patch.object(smoke, "gateway_smoke", return_value=0) as gateway:
            with patch.object(sys, "argv", ["app.exe", "--packaging-gateway-smoke"]):
                self.assertEqual(entrypoint.main(), 0)
                gateway.assert_called_once()

    def test_compiler_is_per_user_and_preserves_profile(self):
        text = (PACKAGING / "installer.iss").read_text(encoding="utf-8")
        active = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(";"))
        self.assertIn("PrivilegesRequired=lowest", active)
        self.assertIn("DefaultDirName={localappdata}", active)
        self.assertIn("ArchitecturesAllowed=x64os", active)
        self.assertIn("{userprograms}", active)
        self.assertIn("{userdesktop}", active)
        self.assertNotIn("[UninstallDelete]", active)
        self.assertNotIn("[Run]", active)
        # No profile folders: uninstall must never touch ~/.config or ~/.local/state.
        self.assertNotIn(".config", active)
        self.assertNotIn(".local", active)
        self.assertIn('Type: files; Name: "{app}\PocketNodeHH.exe"', active)

    def test_compiler_pin_uses_actual_engine_not_unreliable_pe_version(self):
        probe = (PACKAGING / "compiler-version.iss").read_text(encoding="utf-8")
        self.assertIn("VER != EncodeVer(6, 4, 3)", probe)
        self.assertIn("Output=no", probe)
        builder = (WINDOWS / "build-installer.ps1").read_text(encoding="utf-8")
        self.assertIn("packaging/compiler-version.iss", builder)
        self.assertIn("'--packaging-gateway-smoke'", builder)

    def test_pinned_tools_and_frozen_discovery_mode(self):
        lines = (PACKAGING / "requirements-build.txt").read_text(encoding="utf-8").splitlines()
        pins = {line.split("==")[0].lower(): line.split("==")[1] for line in lines if line and not line.startswith("#")}
        self.assertEqual(pins, manifest.BUILD_PINS)
        hook = (PACKAGING / "hooks/hook-hh_applicant_tool.py").read_text(encoding="utf-8")
        self.assertIn('"hh_applicant_tool.operations": "pyz+py"', hook)
        spec = (PACKAGING / "hh-applicant-tool-gui.spec").read_text(encoding="utf-8")
        self.assertIn("console=False", spec)
        self.assertIn("hiddenimports=list(APP_MODULES)", spec)
        self.assertIn("upx=False", spec)
        self.assertNotIn('collect_all(', spec)

    def test_dependency_staging_omits_install_paths_and_copies_license_texts(self):
        class Distribution:
            def __init__(self, root, name):
                self.version, self.metadata = "1.0", {}
                self.root = root
                self.directory = name + "-1.0.dist-info"
                self.files = [Path(self.directory) / name for name in (
                    "METADATA", "direct_url.json", "licenses/LICENSE")]
                license_path = root / self.directory / "licenses/LICENSE"
                license_path.parent.mkdir(parents=True)
                license_path.write_text("Synthetic test license", encoding="utf-8")

            def read_text(self, name):
                return "Name: synthetic\nVersion: 1.0\n" if name == "METADATA" else None

            def locate_file(self, file):
                return self.root / file

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / "stage"
            stage.mkdir()
            python = root / "python"
            python.mkdir()
            (python / "LICENSE.txt").write_text("Synthetic Python license", encoding="utf-8")
            app = Distribution(root, "synthetic-app")
            bootloader = Distribution(root, "synthetic-bootloader")
            with patch.object(build_support, "dependency_closure", return_value={"synthetic-app": app}), \
                 patch.object(build_support.metadata, "distribution", return_value=bootloader), \
                 patch.object(sys, "base_prefix", str(python)):
                build_support.stage_dependencies(stage)
            build_support.audit_tree(stage)
            inventory = json.loads((stage / "metadata/dependency-inventory.json").read_text(encoding="utf-8"))
            self.assertEqual(len(inventory["dependencies"]), 2)
            self.assertTrue(all(item["license"] == "UNSPECIFIED" for item in inventory["dependencies"]))
            self.assertFalse(list(stage.rglob("direct_url.json")))
            self.assertEqual(len(list(stage.rglob("LICENSE"))), 2)
            self.assertNotIn(str(root), json.dumps(inventory))

    def test_missing_tools_fail_preflight_before_generation(self):
        with patch.object(build_support.metadata, "version", side_effect=build_support.metadata.PackageNotFoundError):
            with self.assertRaisesRegex(RuntimeError, "pyinstaller: expected 6.16.0, found missing"):
                build_support.preflight()


if __name__ == "__main__":
    unittest.main()
