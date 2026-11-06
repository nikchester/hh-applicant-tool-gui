"""AI provider for letters and chat replies: ChatMock, an OpenAI-compatible API or a local model.

The provider kind follows from the configured base_url, so no extra setting can disagree
with it. Network guards ask allowed() before any model request: only GET <base>/models and
POST <base>/chat/completions of the active provider pass. API keys live in the Windows
Credential Manager, never in config files, argv, environment or logs.
"""

import ctypes
from dataclasses import dataclass
import ipaddress
from urllib.parse import urlsplit

CHATMOCK_URL = "http://127.0.0.1:8000/v1"
CHATMOCK, OPENAI, LOCAL = "chatmock", "openai", "local"
TITLES = {CHATMOCK: "ChatGPT через ChatMock", OPENAI: "OpenAI-совместимый API", LOCAL: "Локальная модель"}
PRESETS = {OPENAI: "https://api.openai.com/v1", LOCAL: "http://127.0.0.1:11434/v1"}
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_KEY = 4096


@dataclass(frozen=True)
class Provider:
    kind: str
    base_url: str

    @property
    def title(self):
        return TITLES[self.kind]

    @property
    def needs_gateway(self):
        return self.kind == CHATMOCK

    @property
    def needs_key(self):
        return self.kind == OPENAI


def kind_of(base_url):
    if base_url == CHATMOCK_URL:
        return CHATMOCK
    host = urlsplit(base_url).hostname or ""
    return LOCAL if host in LOOPBACK else OPENAI


def parse(base_url, *, allow_external=True):
    """Validate a configured base_url and return its Provider; ValueError explains why not."""
    if not isinstance(base_url, str) or not base_url.strip() or len(base_url) > 500:
        raise ValueError("Укажи адрес API, например https://api.openai.com/v1.")
    base_url = base_url.strip().rstrip("/")
    if base_url == CHATMOCK_URL:
        return Provider(CHATMOCK, CHATMOCK_URL)
    if not allow_external:
        raise ValueError("Only the local model gateway is allowed")
    try:
        url = urlsplit(base_url)
        port = url.port
    except ValueError:
        raise ValueError("Адрес API записан с ошибкой.") from None
    if url.username or url.password or url.query or url.fragment or not url.hostname:
        raise ValueError("Адрес API — только схема, хост, порт и путь, без логина, ? и #.")
    if any(character.isspace() for character in base_url):
        raise ValueError("Адрес API не должен содержать пробелов.")
    host = url.hostname.lower()
    if host == "hh.ru" or host.endswith(".hh.ru"):
        raise ValueError("Адрес API не может быть адресом hh.ru.")
    if host in LOOPBACK:
        if url.scheme not in ("http", "https") or port is None:
            raise ValueError("Для локальной модели укажи порт, например http://127.0.0.1:11434/v1.")
        if port == 8000:
            raise ValueError("Порт 8000 занят ChatMock. Выбери «ChatGPT через ChatMock» или другой порт.")
        return Provider(LOCAL, base_url)
    if url.scheme != "https":
        raise ValueError("Внешний провайдер подключается только по https://.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (address.is_private or address.is_loopback or address.is_link_local):
        raise ValueError("Внешний провайдер не может быть адресом локальной сети.")
    return Provider(OPENAI, base_url)


def from_config(config, *, allow_external=True):
    """Provider of a parsed config.toml; SystemExit keeps the existing refusal style."""
    try:
        return parse(config["openai"]["default"]["base_url"], allow_external=allow_external)
    except (KeyError, TypeError, ValueError) as error:
        raise SystemExit(str(error) or "Invalid AI provider") from None


_active = Provider(CHATMOCK, CHATMOCK_URL)


def activate(provider):
    """One worker process serves one action, so the guards share one active provider."""
    global _active
    _active = provider


def active():
    return _active


def model_endpoint(method, url, provider=None):
    """True only for the active provider's models list and chat completions."""
    provider = provider or _active
    try:
        target, base = urlsplit(url), urlsplit(provider.base_url)
        target_port, base_port = target.port, base.port
    except ValueError:
        return False
    if (target.username or target.password or target.query or target.fragment
            or target.scheme != base.scheme or (target.hostname or "").lower() != (base.hostname or "").lower()
            or target_port != base_port):
        return False
    prefix = base.path.rstrip("/")
    if method == "GET" and target.path in (prefix + "/models",):
        return True
    if method == "POST" and target.path == prefix + "/chat/completions":
        return True
    # ChatMock's own readiness probe.
    return provider.kind == CHATMOCK and method == "GET" and target.path == "/health"


def generation(method, url, provider=None):
    provider = provider or _active
    return method == "POST" and model_endpoint(method, url, provider) and urlsplit(url).path.endswith(
        "/chat/completions")


def describe(url, provider=None):
    """Fixed, secret-free label of a model request for error messages."""
    provider = provider or _active
    for method in ("GET", "POST"):
        if model_endpoint(method, url, provider):
            path = urlsplit(url).path
            return f"{provider.title}: {path.rsplit('/', 1)[-1]}"
    return None


# ---- Windows Credential Manager ------------------------------------------------------------

TARGET = "PocketNodeHH/ai-provider/{}"
CRED_TYPE_GENERIC, CRED_PERSIST_LOCAL_MACHINE, ERROR_NOT_FOUND = 1, 2, 1168


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class CREDENTIAL(ctypes.Structure):
    _fields_ = [("Flags", ctypes.c_uint32), ("Type", ctypes.c_uint32), ("TargetName", ctypes.c_wchar_p),
                ("Comment", ctypes.c_wchar_p), ("LastWritten", FILETIME),
                ("CredentialBlobSize", ctypes.c_uint32), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", ctypes.c_uint32), ("AttributeCount", ctypes.c_uint32),
                ("Attributes", ctypes.c_void_p), ("TargetAlias", ctypes.c_wchar_p),
                ("UserName", ctypes.c_wchar_p)]


def credential_api():
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.CredWriteW.argtypes = (ctypes.POINTER(CREDENTIAL), ctypes.c_uint32)
    api.CredWriteW.restype = ctypes.c_int
    api.CredReadW.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                              ctypes.POINTER(ctypes.POINTER(CREDENTIAL)))
    api.CredReadW.restype = ctypes.c_int
    api.CredDeleteW.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32)
    api.CredDeleteW.restype = ctypes.c_int
    api.CredFree.argtypes = (ctypes.c_void_p,)
    api.CredFree.restype = None
    return api


def key_target(provider):
    # One key per provider host: switching providers never sends a key to another host.
    return TARGET.format((urlsplit(provider.base_url).hostname or "").lower())


def validate_key(key):
    if (not isinstance(key, str) or not key.strip() or len(key) > MAX_KEY
            or any(character.isspace() or ord(character) < 32 for character in key.strip())):
        raise ValueError("Ключ API пуст или содержит пробелы и служебные символы.")
    return key.strip()


def save_key(provider, key, api=None):
    key = validate_key(key)
    api = api or credential_api()
    blob = key.encode("utf-16-le")
    buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    credential = CREDENTIAL(Type=CRED_TYPE_GENERIC, TargetName=key_target(provider),
                            CredentialBlobSize=len(blob), CredentialBlob=buffer,
                            Persist=CRED_PERSIST_LOCAL_MACHINE, UserName="api-key")
    if not api.CredWriteW(ctypes.byref(credential), 0):
        raise OSError("Не удалось сохранить ключ в диспетчере учётных данных Windows.")


def read_key(provider, api=None):
    """Stored key or None. Never raises with key material in the message."""
    try:
        api = api or credential_api()
        pointer = ctypes.POINTER(CREDENTIAL)()
        if not api.CredReadW(key_target(provider), CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            return None
        try:
            credential = pointer.contents
            blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        finally:
            api.CredFree(pointer)
        return blob.decode("utf-16-le") or None
    except (OSError, AttributeError, ValueError, UnicodeDecodeError):
        return None


def delete_key(provider, api=None):
    api = api or credential_api()
    if not api.CredDeleteW(key_target(provider), CRED_TYPE_GENERIC, 0):
        if ctypes.get_last_error() != ERROR_NOT_FOUND:
            raise OSError("Не удалось удалить ключ из диспетчера учётных данных Windows.")


def keyed_config(original, provider, key):
    """Wrap HHApplicantTool.get_openai_config: add the key only for the provider's own base_url."""
    def get_openai_config(tool, *args, **kwargs):
        config = original(tool, *args, **kwargs)
        if key and str(config.get("base_url", "")).rstrip("/") == provider.base_url:
            config["api_key"] = key
        return config
    return get_openai_config


def ready(provider, *, chatgpt_signed_in=False, api=None):
    """Whether letters can be generated: ChatMock needs a login, an API needs a stored key."""
    if provider.kind == CHATMOCK:
        return chatgpt_signed_in
    if provider.kind == OPENAI:
        return read_key(provider, api) is not None
    return True
