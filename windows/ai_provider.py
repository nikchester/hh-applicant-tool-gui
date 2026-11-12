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


@dataclass(frozen=True)
class FreeApi:
    """An OpenAI-compatible API with a free tier; it is an ordinary OPENAI provider with a key."""
    title: str
    base_url: str
    key_page: str
    note: str


FREE_APIS = (
    FreeApi("OpenRouter", "https://openrouter.ai/api/v1", "https://openrouter.ai/settings/keys",
            "В списке моделей будут только бесплатные (:free). Без пополнения баланса — около 50 "
            "запросов в день."),
    FreeApi("Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "https://aistudio.google.com/apikey",
            "Бесплатный уровень Google AI Studio. Google может использовать запросы бесплатного уровня "
            "для улучшения своих продуктов. В некоторых странах Gemini API недоступен."),
    FreeApi("Groq", "https://api.groq.com/openai/v1", "https://console.groq.com/keys",
            "Бесплатный уровень с лимитами запросов в минуту и в день."),
)
# Ids of models that cannot write a letter: speech, images, embeddings, safety classifiers.
NOT_CHAT = ("embedding", "whisper", "tts", "imagen", "veo", "lyria", "image", "audio", "guard", "aqa")


def free_api(base_url):
    """The FREE_APIS entry with this exact base_url, or None."""
    base_url = str(base_url or "").strip().rstrip("/")
    return next((api for api in FREE_APIS if api.base_url == base_url), None)


def model_ids(provider, data):
    """Model ids of a /models response; the free APIs keep only free chat models."""
    ids = [item["id"] for item in data
           if isinstance(item, dict) and isinstance(item.get("id"), str) and len(item["id"]) <= 200]
    api = free_api(provider.base_url)
    if api is None:
        return ids
    if api.title == "OpenRouter":
        return [name for name in ids if name.endswith(":free") or name == "openrouter/free"]
    if api.title == "Gemini":
        # The compatibility layer lists "models/<id>"; chat completions take the bare id.
        ids = [name.removeprefix("models/") for name in ids]
    return [name for name in ids if not any(word in name.lower() for word in NOT_CHAT)]


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

TARGET = "hh-applicant-tool-gui/ai-provider/{}"
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


LEGACY_TARGET_PREFIX = "PocketNodeHH/ai-provider/"  # Name before the project rename.


def migrate_legacy_keys(api=None):
    """Copy each pre-rename key to its new target, then delete the old record.

    An existing new record wins: it is never overwritten. Returns the moved hosts.
    """
    api = api or credential_api()
    api.CredEnumerateW.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                                   ctypes.POINTER(ctypes.POINTER(ctypes.POINTER(CREDENTIAL))))
    api.CredEnumerateW.restype = ctypes.c_int
    count, items = ctypes.c_uint32(), ctypes.POINTER(ctypes.POINTER(CREDENTIAL))()
    if not api.CredEnumerateW(LEGACY_TARGET_PREFIX + "*", 0, ctypes.byref(count), ctypes.byref(items)):
        if ctypes.get_last_error() == ERROR_NOT_FOUND:
            return []
        raise OSError("Не удалось прочитать старые ключи из диспетчера учётных данных Windows.")
    legacy = []
    try:
        for index in range(count.value):
            credential = items[index].contents
            if credential.Type != CRED_TYPE_GENERIC or not credential.TargetName.startswith(LEGACY_TARGET_PREFIX):
                continue
            blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            legacy.append((credential.TargetName, credential.TargetName[len(LEGACY_TARGET_PREFIX):], blob))
    finally:
        api.CredFree(items)
    moved = []
    for old_target, host, blob in legacy:
        new_target = TARGET.format(host)
        existing = ctypes.POINTER(CREDENTIAL)()
        if api.CredReadW(new_target, CRED_TYPE_GENERIC, 0, ctypes.byref(existing)):
            api.CredFree(existing)
        else:
            buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob) if blob else None
            credential = CREDENTIAL(Type=CRED_TYPE_GENERIC, TargetName=new_target,
                                    CredentialBlobSize=len(blob), CredentialBlob=buffer,
                                    Persist=CRED_PERSIST_LOCAL_MACHINE, UserName="api-key")
            if not api.CredWriteW(ctypes.byref(credential), 0):
                raise OSError("Не удалось перенести ключ API в новую запись диспетчера учётных данных Windows.")
            moved.append(host)
        if not api.CredDeleteW(old_target, CRED_TYPE_GENERIC, 0) and ctypes.get_last_error() != ERROR_NOT_FOUND:
            raise OSError("Не удалось удалить старую запись ключа API из диспетчера учётных данных Windows.")
    return moved


# Reasoning models count their hidden reasoning against max_completion_tokens. With the
# profile's 500 they often spend it all and return an empty letter (finish_reason "length").
# The letter length is set by the prompt; this is only a ceiling.
REASONING_MIN_TOKENS = 4000


def keyed_config(original, provider, key):
    """Wrap HHApplicantTool.get_openai_config for the provider's own base_url.

    Adds the key and, for every provider except ChatMock, room for reasoning tokens.
    """
    def get_openai_config(tool, *args, **kwargs):
        config = original(tool, *args, **kwargs)
        if str(config.get("base_url", "")).rstrip("/") != provider.base_url:
            return config
        if key:
            config["api_key"] = key
        if provider.kind != CHATMOCK:
            limit = config.get("max_completion_tokens")
            if type(limit) is not int or limit < REASONING_MIN_TOKENS:
                config["max_completion_tokens"] = REASONING_MIN_TOKENS
        return config
    return get_openai_config


REASONING_OFF = {"enabled": False}
REASONING_MANDATORY = "Reasoning is mandatory"


def empty_reply(response):
    """True for a 200 answer without text: a reasoning model spent its turn thinking."""
    if response.status_code != 200:
        return False
    try:
        content = response.json()["choices"][0]["message"].get("content")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return False
    return not isinstance(content, str) or not content.strip()


def tuned_request(original, provider):
    """Wrap OpenAIClient._request for the provider's own base_url.

    OpenRouter: ask for no hidden reasoning. A letter needs none, and free reasoning models
    sometimes think until the token limit and return no text. A model that cannot turn
    reasoning off answers 400; the same request then goes once more without the field.
    Every provider: an empty answer gets one more attempt.
    """
    openrouter = (urlsplit(provider.base_url).hostname or "").lower() == "openrouter.ai"

    def send(client, payload):
        if openrouter and "reasoning" not in payload:
            response = original(client, {**payload, "reasoning": REASONING_OFF})
            if not (response.status_code == 400 and REASONING_MANDATORY in response.text):
                return response
        return original(client, payload)

    def _request(client, payload):
        if str(getattr(client, "base_url", "")).rstrip("/") != provider.base_url:
            return original(client, payload)
        global _failure
        _failure = None  # A timeout leaves no answer: the detail must not be stale.
        response = send(client, payload)
        if empty_reply(response):
            response = send(client, payload)
        if response.status_code >= 400:
            host = (urlsplit(provider.base_url).hostname or "").lower()
            _failure = (host, response.status_code, DAILY_LIMIT_MARK in response.text, reset_epoch(response))
        return response
    return _request


DAILY_LIMIT_MARK = "free-models-per-day"  # OpenRouter's 429 when the free daily quota is spent.
_failure = None  # (host, status, daily limit, reset epoch) of the last failed model answer; never its body.


def reset_epoch(response):
    """OpenRouter's X-RateLimit-Reset (epoch milliseconds) as whole seconds, or None."""
    headers = getattr(response, "headers", None)
    value = headers.get("X-RateLimit-Reset") if hasattr(headers, "get") else None
    if not isinstance(value, str) or not value.isdecimal() or len(value) > 16:
        return None
    seconds = int(value) // 1000 if len(value) > 11 else int(value)
    return seconds if 1_000_000_000 < seconds < 10_000_000_000 else None


def failure_detail():
    """Fixed, secret-free reason of the last failed model answer, or None."""
    if _failure is None:
        return None
    host, status, daily, reset = _failure
    if status == 429 and daily:
        return (f"{host}: daily limit of free model requests reached (HTTP 429); "
                + (f"reset_epoch={reset}; " if reset else "")
                + "wait for the daily reset, add credits or choose another provider")
    if status == 429:
        return f"{host}: rate limit (HTTP 429); wait a minute and retry"
    if status in (401, 403):
        return f"{host}: HTTP {status}; check the API key and model access"
    return f"{host}: HTTP {status}"


def ready(provider, *, chatgpt_signed_in=False, api=None):
    """Whether letters can be generated: ChatMock needs a login, an API needs a stored key."""
    if provider.kind == CHATMOCK:
        return chatgpt_signed_in
    if provider.kind == OPENAI:
        return read_key(provider, api) is not None
    return True
