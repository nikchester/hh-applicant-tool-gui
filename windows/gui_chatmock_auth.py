"""GUI-only ChatMock 1.40 auth boundary, installed before importing ChatMock.

The upstream loader intentionally searches other accounts on read failure. Never
call it here. Patch utils as its loader finishes, before app/cli/oauth/catalog
can capture aliases. Keep this guard installed for background token refresh too.
"""

import importlib.abc
from importlib.metadata import version
import json
import sys

from gui_profile import atomic_write, state_dir

MAX_AUTH = 256 * 1024
ERROR = "GUI ChatGPT authentication unavailable; log in through the GUI. No account fallback."


def valid_auth(auth):
    if not isinstance(auth, dict) or not isinstance(auth.get("tokens"), dict):
        return False
    tokens = auth["tokens"]
    for field in ("access_token", "refresh_token", "id_token", "account_id"):
        value = tokens.get(field)
        if (not isinstance(value, str) or not value or len(value) > 65536
                or any(character.isspace() or ord(character) < 32 for character in value)):
            return False
    return (auth.get("OPENAI_API_KEY") is None
            and (auth.get("last_refresh") is None or isinstance(auth["last_refresh"], str)))


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Invalid GUI auth")
        result[key] = value
    return result


def invalid_constant(_value):
    raise ValueError("Invalid GUI auth")


class GUIAuthStore:
    def __init__(self):
        self.home = state_dir() / "chatmock"
        self.path = self.home / "auth.json"

    def read(self):
        try:
            # Do not follow a linked auth file into another account's directory.
            if self.path.is_symlink() or self.home.is_symlink():
                return None
            if getattr(self.home, "is_junction", lambda: False)():
                return None
            with self.path.open(encoding="utf-8") as stream:
                text = stream.read(MAX_AUTH + 1)
            if len(text) > MAX_AUTH:
                return None
            auth = json.loads(text, object_pairs_hook=unique_object, parse_constant=invalid_constant)
            return auth if valid_auth(auth) else None
        except (OSError, ValueError, RecursionError):
            return None  # Never stringify path/JSON/OS exceptions or try another path.

    def write(self, auth):
        if not valid_auth(auth):
            return False
        try:
            if self.path.is_symlink() or self.home.is_symlink():
                return False
            if getattr(self.home, "is_junction", lambda: False)():
                return False
            text = json.dumps(auth, ensure_ascii=True, allow_nan=False)
            if len(text) > MAX_AUTH:
                return False
            atomic_write(self.path, text + "\n")
            return True
        except (OSError, ValueError, TypeError, RecursionError):
            return False


class ExclusiveAuth(importlib.abc.MetaPathFinder):
    def __init__(self):
        self.store = GUIAuthStore()
        self.finders = tuple(sys.meta_path)
        self.applied = False

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "chatmock.utils":
            return None
        for finder in self.finders:
            spec = finder.find_spec(fullname, path, target)
            if spec is not None:
                if spec.loader is None or not hasattr(spec.loader, "exec_module"):
                    raise RuntimeError(ERROR)
                spec.loader = AuthLoader(spec.loader, self)
                return spec
        raise RuntimeError(ERROR)

    def apply(self, module):
        module.read_auth_file = self.store.read
        module.write_auth_file = self.store.write
        module.get_home_dir = lambda: str(self.store.home)
        # Upstream refresh errors can contain transport URLs/exception contents.
        # All importers receive only this fixed diagnostic in GUI processes.
        module.eprint = lambda *_args, **_kwargs: print(ERROR, file=sys.stderr, flush=True)
        self.applied = True

    def require_auth(self):
        if not self.applied or self.store.read() is None:
            raise RuntimeError(ERROR)


class AuthLoader:
    def __init__(self, original, guard):
        self.original, self.guard = original, guard

    def create_module(self, spec):
        method = getattr(self.original, "create_module", None)
        return method(spec) if method else None

    def exec_module(self, module):
        self.original.exec_module(module)
        self.guard.apply(module)

    def __getattr__(self, name):
        return getattr(self.original, name)


_guard = None


def install():
    global _guard
    if _guard is not None:
        return _guard
    if version("chatmock") != "1.40" or any(
            name == "chatmock" or name.startswith("chatmock.") for name in sys.modules):
        # Late installation cannot undo an account already loaded by upstream.
        raise RuntimeError(ERROR)
    _guard = ExclusiveAuth()
    sys.meta_path.insert(0, _guard)
    return _guard
