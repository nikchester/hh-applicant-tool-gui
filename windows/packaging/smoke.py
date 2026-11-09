"""Offline smoke checks: no personal profiles, real auth flows or listening server."""

import importlib
import importlib.abc
from importlib import metadata
import json
import os
from pathlib import Path
import pkgutil
import ssl
import subprocess
import sys
import tempfile
import tomllib
from functools import partial
from unittest.mock import patch
from uuid import uuid4

from manifest import APP_MODULES, APPLICATION_PINS, PRIVATE_NAMES, PROFILE_NAMES


def deny_external_io(event, args, *, allowed_auth=None):
    if event in {"socket.connect", "socket.connect_ex", "socket.bind", "socket.getaddrinfo",
                 "os.system", "webbrowser.open"}:
        raise RuntimeError("External IO prohibited during packaging smoke")
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(args[0]))
        parts = [part.lower() for part in path.parts]
        # Only gateway_smoke's fresh synthetic fixture can opt into this exception.
        if allowed_auth is not None and path.absolute() == allowed_auth:
            return
        # Reject profile reads/writes even when inherited environment points at real accounts.
        if (path.name.lower() in PRIVATE_NAMES or path.suffix.lower() in {".log", ".pid"}
                or ".codex" in parts or any(
                    name in PROFILE_NAMES and parent in {".config", "state"}
                    for parent, name in zip(parts, parts[1:]))):
            raise RuntimeError("Profile IO prohibited during packaging smoke")


def smoke_worker():
    sys.addaudithook(deny_external_io)
    from gui_protocol import decode, encode, frame
    request = decode(sys.stdin.readline(1024 * 1024 + 1))
    if request["type"] != "start" or request["payload"] != {"packaging": True}:
        return 2
    sys.stdout.write(encode(frame(request["run"], "event", {"packaging": "ok"})))
    sys.stdout.flush()
    return 0


def smoke_check():
    sys.addaudithook(deny_external_io)
    for name, expected in APPLICATION_PINS.items():
        if metadata.version(name) != expected:
            raise RuntimeError("Unexpected application dependency: " + name)
    # Production requires this hook BEFORE ChatMock's eager package imports.
    from gui_chatmock_auth import install
    guard = install()
    for name in APP_MODULES:
        importlib.import_module(name)
    assert guard.applied, "Exclusive auth loader did not intercept ChatMock"
    import certifi
    import tkinter
    from hh_applicant_tool import operations
    from hh_applicant_tool.tool import HHApplicantTool
    from gui_protocol import decode, encode, frame
    import gui_profile
    from chatmock import cli  # Import only; do not parse/start/authenticate the gateway.
    assert callable(cli.main)
    discovered = {item.name for item in pkgutil.iter_modules(operations.__path__)}
    assert {"authorize", "apply_vacancies", "reply_chats"} <= discovered
    # Actual upstream parser, NOT gui_worker's discovery patch, and never tool.run().
    parser = HHApplicantTool._create_parser()
    for operation in ("authorize", "apply-vacancies", "reply-chats"):
        parsed = parser.parse_args([operation])
        assert callable(parsed.operation_run)
    template = Path(gui_profile.__file__).with_name("config.example.toml")
    config = tomllib.loads(template.read_text(encoding="utf-8"))
    assert config["openai"]["default"]["model"] == "CHOOSE_AFTER_LOGIN"
    assert not config["general"].get("cover_letter_resume_id")
    assert not any(section.get("history") for section in config["openai"].values())
    ssl.create_default_context(cafile=certifi.where())
    # Exercise both Tcl and Tk resource lookup, no persistent window/profile.
    root = tkinter.Tk()
    root.withdraw()
    assert root.tk.call("info", "patchlevel")
    root.destroy()
    command = ([sys.executable] if getattr(sys, "frozen", False) else
               [sys.executable, str(Path(__file__).with_name("entrypoint.py"))])
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    run = uuid4().hex
    gateway = subprocess.run(command + ["--packaging-gateway-smoke"], input="",
                             capture_output=True, text=True, encoding="utf-8", timeout=45, **options)
    assert gateway.returncode == 0, "Fresh-process guarded gateway startup smoke failed"
    assert json.loads(gateway.stdout) == {"gateway_smoke": "ok", "serve_calls": 2}
    reply = subprocess.run(command + ["--packaging-smoke-worker"],
                           input=encode(frame(run, "start", {"packaging": True})),
                           capture_output=True, text=True, encoding="utf-8", timeout=45, **options)
    assert reply.returncode == 0, "Hidden private-pipe worker failed"
    response = decode(reply.stdout)
    assert response["run"] == run and response["payload"] == {"packaging": "ok"}
    # Invalid real worker start MUST fail before initialize/network. This also tests
    # gui.main's early worker dispatch in the actual windowed executable.
    failed = subprocess.run(command + ["--worker"], input=encode(frame(run, "start", {})),
                            capture_output=True, text=True, encoding="utf-8", timeout=45, **options)
    assert failed.returncode == 1 and not failed.stdout, "Worker rejection failed"
    print(json.dumps({"packaging_smoke": "ok", "frozen": bool(getattr(sys, "frozen", False)),
                      "operations": sorted(discovered)}), flush=True)
    return 0


def gateway_smoke():
    """Real gateway import order; synthetic auth only, replace serve before calling it.

    Must run in a fresh process: no ChatMock modules may be preloaded. Wrapping
    the real chatmock_server loader also exercises PyInstaller's frozen loader.
    """
    assert not any(name == "chatmock" or name.startswith("chatmock.") for name in sys.modules)
    preferred_temp = Path(tempfile.gettempdir()) / "opencode"
    with tempfile.TemporaryDirectory(prefix="hh-packaging-", dir=preferred_temp if preferred_temp.is_dir() else None) as temporary:
        state = Path(temporary) / "state"
        auth = (state / "chatmock/auth.json").absolute()
        auth.parent.mkdir(parents=True)
        dummy = {"OPENAI_API_KEY": None, "tokens": {
            "access_token": "packaging-synthetic-access", "refresh_token": "packaging-synthetic-refresh",
            "id_token": "packaging-synthetic-id", "account_id": "packaging-synthetic-account"}}
        auth.write_text(json.dumps(dummy), encoding="utf-8")
        sys.addaudithook(partial(deny_external_io, allowed_auth=auth))
        import gui_chatmock_auth as exclusive
        import gui_runtime
        import gui_worker
        calls = []

        def fake_serve():
            # No original cli.main(), create_app(), refresh, login or socket call.
            guard = exclusive.install()
            assert guard.applied and guard.store.path.absolute() == auth
            guard.require_auth()
            from chatmock import cli, oauth, utils, model_catalog
            assert cli.read_auth_file == model_catalog.read_auth_file == utils.read_auth_file == guard.store.read
            assert oauth.write_auth_file == guard.store.write
            assert sys.argv[1:6] == ["serve", "--host", "127.0.0.1", "--port", "8000"]
            calls.append(True)

        class ServerLoader:
            def __init__(self, original):
                self.original = original

            def create_module(self, spec):
                method = getattr(self.original, "create_module", None)
                return method(spec) if method else None

            def exec_module(self, module):
                self.original.exec_module(module)
                module.main = fake_serve

            def __getattr__(self, name):
                return getattr(self.original, name)

        class ServerFinder(importlib.abc.MetaPathFinder):
            finders = tuple(sys.meta_path)

            def find_spec(self, fullname, path=None, target=None):
                if fullname == "chatmock_server":
                    for finder in self.finders:
                        spec = finder.find_spec(fullname, path, target)
                        if spec is not None:
                            spec.loader = ServerLoader(spec.loader)
                            return spec
                    raise RuntimeError("Packaging smoke server loader unavailable")
                return None

        finder = ServerFinder()
        sys.meta_path.insert(0, finder)
        environment = {"CHATGPT_LOCAL_HOME": str(auth.parent), "CODEX_HOME": str(auth.parent)}
        try:
            with patch.object(exclusive, "state_dir", return_value=state), \
                 patch.object(gui_worker, "environment", return_value=environment), \
                 patch.dict(os.environ, environment):
                gui_runtime.gateway_main()  # Install -> real eager imports -> require_auth -> fake serve.
                auth.unlink()  # Only our newly created disposable fixture.
                try:
                    gui_runtime.gateway_main()
                except RuntimeError as error:
                    assert str(error) == exclusive.ERROR
                else:
                    raise AssertionError("Gateway accepted missing synthetic auth")
                assert len(calls) == 1
                auth.write_text(json.dumps(dummy), encoding="utf-8")
                gui_runtime.gateway_main()  # Recovery must still use the same exclusive loader/store.
                assert len(calls) == 2
        finally:
            sys.meta_path.remove(finder)
        print(json.dumps({"gateway_smoke": "ok", "serve_calls": len(calls)}), flush=True)
    return 0
