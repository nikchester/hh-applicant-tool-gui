"""AI provider for letters and chat replies: ChatMock, an OpenAI-compatible API or a local model.

The provider kind follows from the configured base_url, so no extra setting can disagree
with it. Network guards ask allowed() before any model request: only GET <base>/models and
POST <base>/chat/completions of the active provider pass. API keys live in the Windows
Credential Manager, never in config files, argv, environment or logs.
"""

import ctypes
from dataclasses import dataclass, field
import ipaddress
import time
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
    default_model: str = None  # Chosen after a model list load when the current model is not listed.


FREE_APIS = (
    FreeApi("OpenRouter", "https://openrouter.ai/api/v1", "https://openrouter.ai/settings/keys",
            "В списке моделей будут только бесплатные (:free). Без пополнения баланса — около 50 "
            "запросов в день.", "openrouter/free"),
    FreeApi("Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "https://aistudio.google.com/apikey",
            "Бесплатный уровень Google AI Studio. Google может использовать запросы бесплатного уровня "
            "для улучшения своих продуктов. В некоторых странах Gemini API недоступен."),
    FreeApi("Groq", "https://api.groq.com/openai/v1", "https://console.groq.com/keys",
            "Бесплатный уровень с лимитами запросов в минуту и в день.", "openai/gpt-oss-120b"),
)
# Ids of models that cannot write a letter: speech (Groq's Orpheus and PlayAI voices), images,
# embeddings, safety classifiers.
NOT_CHAT = ("embedding", "whisper", "tts", "orpheus", "playai", "imagen", "veo", "lyria", "image", "audio",
            "guard", "aqa")


# Verified free models, best first. Each was asked for a real Russian cover letter on 2026-11-12 with
# the app's own settings and answered with a clean letter. OpenRouter was checked earlier that day,
# before its daily free limit ran out. Left out: speech, live and transcribe models, models with an
# empty answer, a leaked <thought> block or no free quota, models closed to new users, and ones that
# stayed overloaded (503). A model that later disappears answers 404 and rests for the run.
FREE_MODELS = (
    ("https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    ("https://api.groq.com/openai/v1", "qwen/qwen3.8-27b"),
    ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.5-flash"),
    ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.1-flash-lite"),
    ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.5-flash-lite"),
    ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-flash-lite-latest"),
    ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.1-flash-lite-preview"),
    ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.6-flash"),
    ("https://openrouter.ai/api/v1", "nvidia/nemotron-3-ultra-550b-a55b:free"),
    ("https://openrouter.ai/api/v1", "nvidia/nemotron-3.5-super-vl:free"),
    ("https://openrouter.ai/api/v1", "dots-studio/dots-3-note-preview:free"),
    ("https://openrouter.ai/api/v1", "openrouter/free"),
)


def free_mode(provider):
    """Free mode: the provider is one of FREE_APIS, so models are chosen and switched by the app."""
    return free_api(provider.base_url) is not None


def free_chain(read=None):
    """FREE_MODELS of every free API with a stored key, in order; each entry carries its own key."""
    read = read or read_key
    keys, chain = {}, []
    for base_url, model in FREE_MODELS:
        if base_url not in keys:
            keys[base_url] = read(parse(base_url))
        if keys[base_url]:
            chain.append(Fallback(parse(base_url), model, keys[base_url]))
    return tuple(chain)


def free_api(base_url):
    """The FREE_APIS entry with this exact base_url, or None."""
    base_url = str(base_url or "").strip().rstrip("/")
    return next((api for api in FREE_APIS if api.base_url == base_url), None)


def preferred_model(provider, ids):
    """The model to pick after a list load: the free API's tested default, else the first listed."""
    api = free_api(provider.base_url)
    if api is not None and api.default_model in ids:
        return api.default_model
    return ids[0] if ids else None


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


@dataclass(frozen=True)
class Fallback:
    """One model of the fallback list (#10); its key goes only to its own provider host."""
    provider: Provider
    model: str
    key: str = field(default=None, repr=False, compare=False)

    @property
    def label(self):
        return f"{(urlsplit(self.provider.base_url).hostname or '').lower()} · {self.model}"


_fallbacks = ()


def activate_fallbacks(entries, *, auto=False):
    """Models of this run beyond the configured one; the network guards admit their providers too.

    auto: the free mode, where these entries are the whole chain and the configured model is unused.
    """
    global _fallbacks, _auto
    _fallbacks, _auto = tuple(entries), bool(auto and entries)
    _excluded.clear()


_auto = False


def fallbacks():
    return _fallbacks


def allowed_providers():
    return (_active,) + tuple(entry.provider for entry in _fallbacks)


def model_endpoint(method, url, provider=None):
    """True only for a models list or chat completions of the active or a fallback provider."""
    if provider is None:
        return any(model_endpoint(method, url, candidate) for candidate in allowed_providers())
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
    return method == "POST" and model_endpoint(method, url, provider) and urlsplit(url).path.endswith(
        "/chat/completions")


def describe(url, provider=None):
    """Fixed, secret-free label of a model request for error messages."""
    for candidate in ((provider,) if provider is not None else allowed_providers()):
        for method in ("GET", "POST"):
            if model_endpoint(method, url, candidate):
                path = urlsplit(url).path
                return f"{candidate.title}: {path.rsplit('/', 1)[-1]}"
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


def _host(provider):
    return (urlsplit(provider.base_url).hostname or "").lower()


# Why the next fallback model gets the request (#10). Fixed codes: the GUI shows its own text.
SWITCH_REASONS = ("daily_limit", "rate_limit", "region", "reasoning", "key", "unavailable", "empty")
RATE_LIMIT_PAUSE = 60  # Seconds a rate-limited model rests when the provider names no time.
_excluded = {}  # (base_url, model) -> epoch until which the model is skipped in this run.
on_event = None  # The GUI worker sets channel.event to journal switches and the writing model.


def switch_reason(response):
    """Why this answer should go to the next fallback model, or None to keep it."""
    status, text = response.status_code, str(getattr(response, "text", "") or "")
    if status == 200:
        return "empty" if empty_reply(response) else None
    if status == 429:
        return "daily_limit" if DAILY_LIMIT_MARK in text else "rate_limit"
    if status == 403 and "region" in text.lower():
        return "region"
    if status == 400 and REASONING_MANDATORY in text:
        return "reasoning"
    if status in (401, 402, 403):
        return "key"
    if status in (404, 408) or status >= 500:
        return "unavailable"
    return None


def _notify(name, data):
    if on_event is not None:
        try:
            on_event(name, data)
        except Exception:  # A journal line must never break a generation.
            pass


def _exclude(chain, entry, reason, response):
    """Rest a failed model: a daily limit rests every model of that host until its reset."""
    now = time.time()
    if reason == "daily_limit":
        until = reset_epoch(response) or float("inf")
        for other in chain:
            if _host(other.provider) == _host(entry.provider):
                _excluded[(other.provider.base_url, other.model)] = until
    elif reason == "rate_limit":
        retry = str(getattr(response, "headers", {}).get("Retry-After", "") or "")
        _excluded[(entry.provider.base_url, entry.model)] = now + (
            min(int(retry), 3600) if retry.isdecimal() else RATE_LIMIT_PAUSE)
    elif reason in ("region", "key"):
        _excluded[(entry.provider.base_url, entry.model)] = float("inf")
    elif reason == "unavailable" and response.status_code in (404,):
        _excluded[(entry.provider.base_url, entry.model)] = float("inf")


def tuned_request(original, provider):
    """Wrap OpenAIClient._request for the provider's own base_url.

    OpenRouter: ask for no hidden reasoning. A letter needs none, and free reasoning models
    sometimes think until the token limit and return no text. A model that cannot turn
    reasoning off answers 400; the same request then goes once more without the field.
    Every provider: an empty answer gets one more attempt.
    With a fallback list (#10) a failed answer goes to the next model of the list; the
    client's address and key are swapped only for that call, so a key reaches only its host.
    """
    def send(client, payload, entry):
        if _host(entry.provider) == "openrouter.ai" and "reasoning" not in payload:
            response = original(client, {**payload, "reasoning": REASONING_OFF})
            if not (response.status_code == 400 and REASONING_MANDATORY in response.text):
                return response
        return original(client, payload)

    def attempt(client, payload, entry):
        response = send(client, payload, entry)
        return send(client, payload, entry) if empty_reply(response) else response

    def record(entry, response):
        global _failure
        _failure = (_host(entry.provider), response.status_code, DAILY_LIMIT_MARK in response.text,
                    reset_epoch(response), False, error_code(response))

    def _request(client, payload):
        if str(getattr(client, "base_url", "")).rstrip("/") != provider.base_url:
            return original(client, payload)
        global _failure
        _failure = None  # A timeout leaves no answer: the detail must not be stale.
        primary = Fallback(provider, payload.get("model"), getattr(client, "api_key", None))
        chain = list(_fallbacks) if _auto else [primary] + [
            entry for entry in _fallbacks if (entry.provider.base_url, entry.model) != (provider.base_url, primary.model)]
        if len(chain) == 1 and not _auto:  # No fallback list: one model, as before.
            response = attempt(client, payload, primary)
            if response.status_code >= 400:
                record(primary, response)
            return response
        import requests
        saved = (client.base_url, getattr(client, "api_key", None))
        response, lost = None, None
        try:
            for entry in chain:
                if _excluded.get((entry.provider.base_url, entry.model), 0) > time.time():
                    continue
                client.base_url, client.api_key = entry.provider.base_url, entry.key
                try:
                    response = attempt(client, {**payload, "model": entry.model}, entry)
                except requests.RequestException as error:
                    # An unreachable provider (e.g. a local model that is not running): try the next.
                    lost = error
                    _notify("model_switch", {"model": entry.label, "reason": "unavailable"})
                    continue
                reason = switch_reason(response)
                if reason is None:
                    if response.status_code >= 400:
                        record(entry, response)
                    else:
                        _notify("model_used", entry.label)
                    return response
                record(entry, response)
                _exclude(chain, entry, reason, response)
                _notify("model_switch", {"model": entry.label, "reason": reason})
        finally:
            client.base_url, client.api_key = saved
        resets = [until for until in _excluded.values() if until != float("inf") and until > time.time()]
        _failure = ("", 429, False, int(min(resets)) if resets else None, True, None)
        if response is None and lost is not None:
            raise lost  # No model answered at all: upstream handles it as a network failure.
        if response is None:  # Every model rests: answer like a rate limit, without a request.
            response = requests.Response()
            response.status_code, response._content = 429, b'{"error":{"message":"fallback list exhausted"}}'
        return response
    return _request


DAILY_LIMIT_MARK = "free-models-per-day"  # OpenRouter's 429 when the free daily quota is spent.
# (host, status, daily limit, reset epoch, fallback list exhausted) of the last failed model answer;
# never its body.
_failure = None


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
    host, status, daily, reset, exhausted, code = _failure
    code = f" ({code})" if code else ""
    if exhausted:
        return ("fallback list exhausted: every model failed or rests; "
                + (f"reset_epoch={reset}; " if reset else "") + "wait or add another free model")
    if status == 429 and daily:
        return (f"{host}: daily limit of free model requests reached (HTTP 429); "
                + (f"reset_epoch={reset}; " if reset else "")
                + "wait for the daily reset, add credits or choose another provider")
    if status == 429:
        return f"{host}: rate limit (HTTP 429); wait a minute and retry"
    if status in (401, 403):
        return f"{host}: HTTP {status}{code}; check the API key and model access"
    return f"{host}: HTTP {status}{code}"


def error_code(response):
    """The provider's own short error code, e.g. model_terms_required; never free text."""
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError, TypeError):
        return None
    if not isinstance(error, dict):
        return None
    for value in (error.get("code"), error.get("type"), error.get("status")):
        if (isinstance(value, str) and 0 < len(value) <= 64
                and all(c.isascii() and (c.isalnum() or c in "_.-") for c in value)):
            return value
    return None


def ready(provider, *, chatgpt_signed_in=False, api=None):
    """Whether letters can be generated: ChatMock needs a login, an API needs a stored key."""
    if provider.kind == CHATMOCK:
        return chatgpt_signed_in
    if free_mode(provider):  # Any free API key will do: the app switches between them.
        return any(read_key(parse(free.base_url), api) is not None for free in FREE_APIS)
    if provider.kind == OPENAI:
        return read_key(provider, api) is not None
    return True
