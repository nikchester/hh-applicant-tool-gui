"""Offline real-ChatMock auth isolation tests; every auth read is instrumented.

Run with venvs/chatmock/Scripts/python.exe -B windows/check_gui_auth.py.
Only generated auth under TemporaryDirectory is ever opened; network is blocked.
"""

import builtins
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

import gui_chatmock_auth as exclusive

READS, WRITES = [], []


def sample_auth():
    return {"OPENAI_API_KEY": None, "tokens": {"access_token": "synthetic-access",
        "refresh_token": "synthetic-refresh", "id_token": "synthetic-id", "account_id": "synthetic-account"},
        "last_refresh": "2099-01-01T00:00:00Z"}


def setUpModule():
    global TEMP, HOME, PATH, GUARD, PATCHES, utils, oauth, cli, catalog
    TEMP = tempfile.TemporaryDirectory()
    HOME = Path(TEMP.name)
    PATH = HOME / ".local/state/hh-applicant-tool-gui/chatmock/auth.json"
    old_builtin, old_io, old_expand = builtins.open, io.open, os.path.expanduser

    def instrument(original):
        def opened(file, mode="r", *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)):
                path = Path(os.fsdecode(file)).absolute()
                if path.name == "auth.json":
                    if "r" in mode or "+" in mode:
                        READS.append(path)
                        if path != PATH:
                            raise AssertionError("Non-GUI auth read forbidden by offline test")
                    else:
                        WRITES.append(path)
                        if HOME not in path.parents:
                            raise AssertionError("Non-disposable auth write forbidden by offline test")
            return original(file, mode, *args, **kwargs)
        return opened

    PATCHES = [patch("pathlib.Path.home", return_value=HOME),
        patch("os.path.expanduser", side_effect=lambda value: str(HOME / value[2:])
              if isinstance(value, str) and value.startswith("~/") else old_expand(value)),
        patch.dict(os.environ, {"CHATGPT_LOCAL_HOME": str(HOME / "env-default"), "CODEX_HOME": str(HOME / "codex-default")}),
        patch.object(requests.sessions.Session, "send", side_effect=AssertionError("No network in auth checks")),
        patch("builtins.open", instrument(old_builtin)), patch("io.open", instrument(old_io))]
    for item in PATCHES:
        item.start()
    # The guard MUST be installed before importing chatmock's eager __init__.
    GUARD = exclusive.install()
    from chatmock import utils as actual_utils, oauth as actual_oauth, cli as actual_cli
    from chatmock import model_catalog as actual_catalog
    utils, oauth, cli, catalog = actual_utils, actual_oauth, actual_cli, actual_catalog


def tearDownModule():
    for item in reversed(PATCHES):
        item.stop()
    sys.meta_path.remove(GUARD)
    TEMP.cleanup()


class ExclusiveAuthChecks(unittest.TestCase):
    def setUp(self):
        PATH.parent.mkdir(parents=True, exist_ok=True)
        PATH.unlink(missing_ok=True)
        for base in (HOME / "env-default", HOME / "codex-default", HOME / ".chatgpt-local", HOME / ".codex"):
            base.mkdir(parents=True, exist_ok=True)
            (base / "auth.json").write_text(json.dumps(sample_auth()), encoding="utf-8")
        READS.clear()
        WRITES.clear()

    def tearDown(self):
        self.assertTrue(all(path == PATH for path in READS), "A fallback auth path was opened")

    def write_gui(self, value):
        PATH.write_text(json.dumps(value), encoding="utf-8")

    def test_aliases_captured_exclusive_functions_during_eager_package_import(self):
        self.assertTrue(GUARD.applied)
        self.assertEqual(utils.read_auth_file, GUARD.store.read)
        self.assertEqual(cli.read_auth_file, GUARD.store.read)
        self.assertEqual(catalog.read_auth_file, GUARD.store.read)
        self.assertEqual(oauth.write_auth_file, GUARD.store.write)
        self.assertEqual(utils.get_home_dir(), str(PATH.parent))
        self.assertEqual(cli.get_home_dir(), str(PATH.parent))
        self.assertEqual(catalog.get_home_dir(), str(PATH.parent))

    def test_missing_gui_auth_never_uses_any_of_four_existing_fallbacks(self):
        with patch.object(utils, "_refresh_chatgpt_tokens") as refresh:
            for _ in range(3):
                self.assertIsNone(utils.read_auth_file())
                self.assertEqual(utils.load_chatgpt_tokens(force_refresh=True), (None, None, None))
                self.assertIsNone(catalog._account_id_from_auth_file())
            refresh.assert_not_called()
        self.assertGreaterEqual(len(READS), 9)
        self.assertEqual(set(READS), {PATH})

    def test_malformed_incomplete_oversize_and_invalid_types_fail_closed(self):
        invalid = (None, [], {}, {"tokens": []}, {"tokens": {}},
                   {**sample_auth(), "tokens": {**sample_auth()["tokens"], "refresh_token": ""}},
                   {**sample_auth(), "tokens": {**sample_auth()["tokens"], "account_id": None}},
                   {**sample_auth(), "tokens": {**sample_auth()["tokens"], "access_token": "SECRET\nvalue"}},
                   {**sample_auth(), "OPENAI_API_KEY": "SECRET"})
        for value in invalid:
            with self.subTest(value_type=type(value).__name__):
                self.write_gui(value)
                self.assertIsNone(utils.read_auth_file())
                self.assertEqual(utils.load_chatgpt_tokens(force_refresh=True), (None, None, None))
        for text in ("SECRET malformed", '{"tokens":{},"tokens":{}}', "x" * (exclusive.MAX_AUTH + 1)):
            PATH.write_text(text, encoding="utf-8")
            self.assertIsNone(utils.read_auth_file())

    def test_unreadable_gui_file_and_fixed_errors_do_not_disclose_exception_or_secret(self):
        self.write_gui(sample_auth())
        output = StringIO()
        old_io = io.open
        def denied(file, *args, **kwargs):
            if Path(file).absolute() == PATH:
                READS.append(PATH)
                raise PermissionError("SECRET private exception")
            return old_io(file, *args, **kwargs)
        with patch("io.open", denied), redirect_stdout(output), redirect_stderr(output):
            self.assertIsNone(utils.read_auth_file())
            with self.assertRaises(RuntimeError) as caught:
                GUARD.require_auth()
            utils.eprint("SECRET private exception")
        self.assertNotIn("SECRET", str(caught.exception) + output.getvalue())
        self.assertIn("No account fallback", str(caught.exception))

    def test_valid_auth_and_refresh_read_write_only_captured_gui_path_after_env_changes(self):
        self.write_gui(sample_auth())
        updated = {**sample_auth()["tokens"], "access_token": "refreshed-synthetic"}
        with patch.dict(os.environ, {"CHATGPT_LOCAL_HOME": str(HOME / ".codex"), "CODEX_HOME": str(HOME / ".chatgpt-local")}), \
             patch.object(utils, "_refresh_chatgpt_tokens", return_value=updated) as refresh, \
             patch("gui_chatmock_auth.atomic_write", wraps=exclusive.atomic_write) as write:
            self.assertEqual(utils.load_chatgpt_tokens(force_refresh=True),
                             ("refreshed-synthetic", "synthetic-account", "synthetic-id"))
            refresh.assert_called_once_with("synthetic-refresh", utils.CLIENT_ID_DEFAULT)
            self.assertEqual([call.args[0] for call in write.call_args_list], [PATH])
            self.assertEqual(utils.read_auth_file()["tokens"]["access_token"], "refreshed-synthetic")
            self.assertEqual(utils.get_home_dir(), str(PATH.parent))

    def test_subsequent_corruption_or_deletion_cannot_switch_account_or_refresh_fallback(self):
        self.write_gui(sample_auth())
        self.assertEqual(utils.load_chatgpt_tokens(ensure_fresh=False)[0], "synthetic-access")
        for corrupt in (True, False):
            if corrupt:
                PATH.write_text("SECRET invalid JSON", encoding="utf-8")
            else:
                PATH.unlink()
            with patch.object(utils, "_refresh_chatgpt_tokens") as refresh:
                self.assertEqual(utils.force_refresh_chatgpt_auth(), (None, None))
                refresh.assert_not_called()

    def test_device_login_persistence_and_refresh_alias_are_exclusive_and_validate_shape(self):
        tokens = SimpleNamespace(**sample_auth()["tokens"])
        bundle = SimpleNamespace(api_key=None, token_data=tokens, last_refresh="2099-01-01T00:00:00Z")
        with patch("gui_chatmock_auth.atomic_write", wraps=exclusive.atomic_write) as write:
            self.assertTrue(oauth.persist_auth_bundle(bundle))
            self.assertEqual([call.args[0] for call in write.call_args_list], [PATH])
        GUARD.require_auth()
        bundle.token_data.refresh_token = ""
        self.assertFalse(oauth.persist_auth_bundle(bundle))

    def test_write_error_is_silent_and_cannot_fallback(self):
        output = StringIO()
        with patch("gui_chatmock_auth.atomic_write", side_effect=OSError("SECRET")), \
             redirect_stderr(output), redirect_stdout(output):
            self.assertFalse(utils.write_auth_file(sample_auth()))
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(WRITES, [])
        self.assertFalse(utils.write_auth_file({**sample_auth(), "invalid_extra": object()}))

    def test_installation_is_idempotent_and_rejects_late_installation(self):
        self.assertIs(exclusive.install(), GUARD)
        with patch.object(exclusive, "_guard", None), self.assertRaisesRegex(RuntimeError, "No account fallback"):
            exclusive.install()


if __name__ == "__main__":
    unittest.main()
