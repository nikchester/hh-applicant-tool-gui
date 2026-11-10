"""Versioned JSON-lines IPC. Logs are never protocol and cannot grant approval."""

import json
import queue
import threading
from uuid import UUID, uuid4

VERSION = 1
MAX_FRAME = 1024 * 1024
ACTIONS = frozenset({"hh_login", "chatgpt_login", "models", "resumes", "preview", "apply", "auto", "chats"})
# Fixed texts for the "retry" event: the worker sends only the key, never free text.
RETRY_NOTICES = {
    "generation_wait": "AI-провайдер не ответил. Повторяю генерацию письма через 30 с.",
    "generation_skip": "AI-провайдер не ответил после повторов. Вакансия пропущена, отклик не отправлен.",
    "send_check": "HH не ответил на отклик. Через 15 с проверю, дошёл ли отклик.",
    "send_confirmed": "Проверка: отклик есть в HH. Повтор не нужен.",
    "send_retry": "Проверка: отклика нет в HH. Отправляю отклик ещё раз.",
}
# Fixed reasons of the "letter_rejected" event: the GUI shows no other reason text.
LETTER_LEVEL_REJECTED = "Письмо упоминает уровень джун/миддл для вакансии без Senior/Lead"
LETTER_LANGUAGE_REJECTED = "Письмо для англоязычной вакансии должно быть на английском"
LETTER_REJECTIONS = frozenset({LETTER_LEVEL_REJECTED, LETTER_LANGUAGE_REJECTED})


class Cancelled(BaseException):
    pass


class ProtocolError(ValueError):
    pass


def identifier(value):
    try:
        return isinstance(value, str) and UUID(value).hex == value
    except ValueError:
        return False


def frame(run, kind, payload=None, request_id=None):
    return {"v": VERSION, "run": run, "id": request_id or uuid4().hex,
            "type": kind, "payload": payload or {}}


def decode(line):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError

    try:
        if not line or len(line) > MAX_FRAME:
            raise ValueError
        data = json.loads(line, object_pairs_hook=object_pairs, parse_constant=invalid_constant)
        if (not isinstance(data, dict) or set(data) != {"v", "run", "id", "type", "payload"}
                or type(data["v"]) is not int or data["v"] != VERSION
                or not identifier(data["run"]) or not identifier(data["id"])
                or data["type"] not in {"start", "response", "cancel", "review", "event", "done"}
                or not isinstance(data["payload"], dict)):
            raise ValueError
        return data
    except (ValueError, TypeError, RecursionError):
        raise ProtocolError("Invalid IPC frame") from None


def encode(data):
    text = json.dumps(data, ensure_ascii=True, separators=(",", ":")) + "\n"
    decode(text)
    return text


NETWORK_HOST_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.-:")


def valid_network_host(host):
    """A bare host name only: no scheme, path, query or credentials."""
    return isinstance(host, str) and 0 < len(host) <= 253 and set(host) <= NETWORK_HOST_CHARS


def validate_done(payload):
    keys = {"success", "cancelled", "unknown", "counts"}
    if (set(payload) not in (keys, keys | {"network"})
            or ("network" in payload and not valid_network_host(payload["network"]))
            or any(type(payload[k]) is not bool for k in ("success", "cancelled", "unknown"))
            or not isinstance(payload["counts"], dict)
            or set(payload["counts"]) != {"attempts", "http_success", "captcha_rejections"}
            or any(type(value) is not int or value < 0 for value in payload["counts"].values())):
        raise ProtocolError("Invalid completion")
    counts = payload["counts"]
    confirmed = counts["http_success"] + counts["captcha_rejections"]
    if (confirmed > counts["attempts"] or payload["unknown"] != (confirmed != counts["attempts"])
            or (payload["success"] and payload["cancelled"])
            or (payload["success"] and "network" in payload)):
        raise ProtocolError("Inconsistent completion")
    return payload


class WorkerChannel:
    def __init__(self, reader, writer, run):
        self.reader, self.writer, self.run = reader, writer, run
        self.cancelled = threading.Event()
        self.responses = queue.Queue()
        self.lock = threading.Lock()
        self.pending = None
        self.consumed = set()

    def listen(self):
        def receive():
            try:
                while True:
                    data = decode(self.reader.readline(MAX_FRAME + 1))
                    with self.lock:
                        if data["run"] != self.run:
                            raise ProtocolError("Wrong run")
                        if data["type"] == "cancel":
                            self.cancelled.set()
                            return
                        if (data["type"] != "response" or data["id"] != self.pending
                                or data["id"] in self.consumed):
                            raise ProtocolError("Stale or duplicate response")
                        self.consumed.add(data["id"])
                        self.pending = None
                    self.responses.put(data)
            except (ProtocolError, OSError, ValueError):
                self.cancelled.set()  # Includes EOF; never authorize on disconnect.
        threading.Thread(target=receive, daemon=True).start()

    def check(self):
        if self.cancelled.is_set():
            raise Cancelled

    def emit(self, kind, payload, request_id=None):
        self.writer.write(encode(frame(self.run, kind, payload, request_id)))
        self.writer.flush()

    def event(self, name, data):
        self.emit("event", {"name": name, "data": data})

    def ask(self, payload):
        self.check()
        request_id = uuid4().hex
        with self.lock:
            if self.pending is not None:
                raise ProtocolError("Overlapping reviews")
            self.pending = request_id
        self.emit("review", payload, request_id)
        while True:
            self.check()
            try:
                result = self.responses.get(timeout=0.1)
                self.check()
                if result["id"] != request_id:
                    raise ProtocolError("Response mismatch")
                return result["payload"]
            except queue.Empty:
                continue

    def sleep(self, seconds):
        if self.cancelled.wait(seconds):
            raise Cancelled


class ReviewState:
    """UI-side one-shot response capability, invalidated before a cancel or edit."""
    def __init__(self, run):
        self.run, self.pending = run, None
        self.seen = set()
        self.cancelled = False

    def accept(self, data):
        if (self.cancelled or data["run"] != self.run or data["type"] != "review"
                or data["id"] in self.seen or self.pending is not None):
            raise ProtocolError("Invalid review lifecycle")
        self.seen.add(data["id"])
        self.pending = data

    def respond(self, action, text=""):
        if self.cancelled or self.pending is None:
            raise ProtocolError("No active review")
        pending, self.pending = self.pending, None
        return frame(self.run, "response", {"action": action, "text": text}, pending["id"])

    def cancel(self):
        self.cancelled, self.pending = True, None
        return frame(self.run, "cancel")


class ReviewAdapter:
    def __init__(self, channel):
        self.channel = channel

    def decision(self, kind, text, context, options=()):
        result = self.channel.ask({"kind": kind, "text": text,
                                   "context": context, "options": list(options)})
        allowed = {"yes", "no", "stop"} if kind == "application" else {"yes", "no", "stop", "edit", "manual"}
        if (set(result) != {"action", "text"} or result["action"] not in allowed
                or not isinstance(result["text"], str) or len(result["text"]) > 50000):
            raise ProtocolError("Invalid decision")
        if result["action"] == "stop":
            raise Cancelled
        return result

    def application(self, gate, text, *, retry=False):
        from send import vacancy_lines
        context = "\n".join(vacancy_lines(gate.current, gate.counts["vacancies"]))
        context += "\nРезюме: " + gate.resume_id
        if retry:
            context += "\nПосле капчи нужно новое подтверждение."
        return self.decision("application", text, context)["action"]

    def chat(self, gate, text, options):
        chat = gate.current
        context = (f"{chat.employer_name}\n{chat.vacancy_name}\n{chat.vacancy_url}\n"
                   f"Резюме: {chat.resume_title} ({chat.resume_id})\n"
                   f"{chat.contact_name}: {chat.reply_to_message}")
        return self.decision("chat", text, context, options)
