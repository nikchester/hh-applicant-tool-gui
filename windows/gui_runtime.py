"""Windowed process construction, shared Windows lock and owned gateway lifecycle."""

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
from uuid import uuid4

from gui_profile import state_dir
from gui_protocol import Cancelled

MUTEX_NAME = "Local\\PocketNode-HH-Run"
GATEWAY_ACTIONS = frozenset({"models", "preview", "apply", "auto", "chats"})


def attach_standard_streams():
    """Windowed frozen bootloaders may set sys.std* to None despite private pipes."""
    if os.name != "nt":
        return
    import msvcrt
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetStdHandle.argtypes = (wintypes.DWORD,)
    api.GetStdHandle.restype = wintypes.HANDLE
    api.GetCurrentProcess.restype = wintypes.HANDLE
    api.DuplicateHandle.argtypes = (wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                   ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                   wintypes.BOOL, wintypes.DWORD)
    api.DuplicateHandle.restype = wintypes.BOOL
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    process = api.GetCurrentProcess()
    for name, number, mode, flags in (("stdin", -10, "r", os.O_RDONLY),
                                       ("stdout", -11, "w", os.O_WRONLY),
                                       ("stderr", -12, "w", os.O_WRONLY)):
        if getattr(sys, name) is not None:
            continue
        handle = api.GetStdHandle(number & 0xffffffff)
        copy = wintypes.HANDLE()
        if not handle or not api.DuplicateHandle(process, handle, process, ctypes.byref(copy), 0, False, 2):
            raise RuntimeError("Worker private standard handle unavailable")
        try:
            descriptor = msvcrt.open_osfhandle(copy.value, flags | os.O_BINARY)
        except OSError:
            api.CloseHandle(copy)
            raise
        setattr(sys, name, os.fdopen(descriptor, mode, encoding="utf-8", buffering=1))


def worker_command(mode="--worker", *, action=None):
    if mode not in {"--worker", "--gateway-worker"}:
        raise ValueError("Invalid worker mode")
    if getattr(sys, "frozen", False):
        return [sys.executable, mode]
    runtime = "chatmock" if mode == "--gateway-worker" or action == "chatgpt_login" else "hh"
    python = Path(__file__).resolve().parents[1] / "venvs" / runtime / "Scripts/python.exe"
    if not python.is_file():
        raise RuntimeError("Bundled runtime is missing; no runtime installation is allowed")
    return [str(python), "-u", str(Path(__file__).with_name("gui.py")), mode]


def process_options():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


class RunBusy(RuntimeError):
    """Another live owner (GUI or CLI) holds the shared run mutex; nothing was started."""


class RunMutex:
    def __init__(self):
        self.handle = None

    def acquire(self):
        if os.name != "nt":
            raise RuntimeError("Windows mutex requires Windows")
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        api.CreateMutexW.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        api.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = api.CreateMutexW(None, False, MUTEX_NAME)
        if not handle:
            raise RuntimeError("Could not create shared HH mutex")
        result = api.WaitForSingleObject(handle, 0)
        if result == 0x102:  # WAIT_TIMEOUT: a live owner holds it right now.
            api.CloseHandle(handle)
            raise RunBusy("Another HH run is active")
        if result != 0:
            if result == 0x80:  # Abandoned owner may have died during a POST.
                api.ReleaseMutex(handle)
            api.CloseHandle(handle)
            raise RuntimeError("Another HH run is active or ended abnormally; check HH before retrying")
        self.api, self.handle = api, handle

    def release(self):
        if self.handle is not None:
            self.api.ReleaseMutex(self.handle)
            self.api.CloseHandle(self.handle)
            self.handle = None


def gateway_ready():
    # Local readiness only; ignore HTTP_PROXY and do not follow redirects outside loopback.
    from urllib.request import HTTPRedirectHandler

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open("http://127.0.0.1:8000/health", timeout=2) as response:
            health = json.loads(response.read(65537))
        with opener.open("http://127.0.0.1:8000/v1/models", timeout=2) as response:
            models = json.loads(response.read(1024 * 1024 + 1))
        return health.get("status") == "ok" and bool(models.get("data"))
    except (OSError, ValueError, URLError):
        return False


class Gateway:
    def __init__(self, notify, cancel):
        self.notify, self.cancel = notify, cancel
        self.process, self.logs = None, []

    def start(self):
        if self.cancel.is_set():
            raise Cancelled
        if gateway_ready():
            self.notify("status", "Используется внешний ChatMock: его аккаунт может отличаться от GUI. Процесс не будет остановлен.")
            return
        from gui_chatmock_auth import GUIAuthStore
        if GUIAuthStore().read() is None:
            raise RuntimeError("Сначала войди в ChatGPT через GUI.")
        from gui_worker import environment
        from gui_owned_process import owned_job
        directory = state_dir() / "run"
        directory.mkdir(parents=True, exist_ok=True)
        stem = "chatmock-" + uuid4().hex
        self.logs = [directory / (stem + suffix) for suffix in (".out.log", ".err.log")]
        with self.logs[0].open("x", encoding="utf-8") as out, self.logs[1].open("x", encoding="utf-8") as err:
            self.process = owned_job().spawn(worker_command("--gateway-worker"),
                stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                env={**os.environ, **environment()}, **process_options())
        for _ in range(40):
            if self.cancel.wait(0.5):
                raise Cancelled
            if self.process.poll() is not None:
                break
            if gateway_ready() and self.process.poll() is None:
                return
        raise RuntimeError("ChatMock не готов: проверь вход, порт 8000 и сохранённые логи.")

    def close(self, *, success):
        stopped = True
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()  # Only the gateway process created by this object.
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                stopped = False
                self.notify("status", "Свой ChatMock не остановлен; проверь процессы перед повтором.")
        if success and stopped:
            import time
            for path in self.logs:
                # The venv launcher exits first; the real interpreter may hold the
                # log handles for a moment longer, so retry before preserving.
                for attempt in range(20):
                    try:
                        path.unlink(missing_ok=True)  # Only this successful run's new diagnostic logs.
                        break
                    except OSError:
                        if attempt == 19:
                            self.notify("status", "ChatMock diagnostic log preserved: " + str(path))
                        else:
                            time.sleep(0.25)
        else:
            for path in self.logs:
                self.notify("status", "ChatMock diagnostic log preserved: " + str(path))
        return stopped


def gateway_main():
    from importlib.metadata import version
    if version("chatmock") != "1.40":
        raise RuntimeError("Unsupported ChatMock adapter version")
    from gui_worker import environment
    os.environ.update(environment())
    from gui_chatmock_auth import install
    guard = install()
    # Import through the guard, before chatmock_server captures any utils aliases.
    from chatmock import utils
    guard.require_auth()
    import chatmock_server
    sys.argv = ["chatmock_server", "serve", "--host", "127.0.0.1", "--port", "8000",
                "--reasoning-summary", "none", "--reasoning-compat", "legacy",
                "--no-enable-web-search", "--no-fast-mode"]
    chatmock_server.main()
