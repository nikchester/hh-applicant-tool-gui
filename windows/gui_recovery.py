"""Conservative crash warning, not a send ledger. No candidate/request IDs or text."""

import json

from gui_profile import atomic_write, state_dir

MUTATING_ACTIONS = frozenset({"apply", "auto", "chats"})
WARNING = ("Прошлая отправка завершилась без подтверждения. "
           "Отклик или ответ в чат мог уйти в HH.\n\n"
           "Открой hh.ru и проверь отклики и чаты. Приложение не знает, что именно ушло.\n\n"
           "Ты проверил hh.ru и разрешаешь новый запуск?")


class RecoveryMarker:
    def __init__(self):
        self.path = state_dir() / "mutation-state.json"
        self.written = None

    def fingerprint(self):
        info = self.path.stat()
        return info.st_mtime_ns, info.st_ino, info.st_size

    def pending(self):
        try:
            with self.path.open(encoding="utf-8") as stream:
                data = json.loads(stream.read(257))
            if (data == {"v": 1, "unresolved": False} and type(data["v"]) is int
                    and type(data["unresolved"]) is bool):
                return None
        except FileNotFoundError:
            return None
        except (ValueError, UnicodeError):
            pass  # Malformed/truncated markers conservatively require acknowledgement.
        return self.fingerprint()

    def begin(self, acknowledgement=None):
        pending = self.pending()
        if pending is not None and pending != acknowledgement:
            raise RuntimeError("Explicit recovery acknowledgement required")
        atomic_write(self.path, '{"v":1,"unresolved":true}\n')
        self.written = self.fingerprint()

    def complete(self):
        if self.written is not None and self.fingerprint() == self.written:
            atomic_write(self.path, '{"v":1,"unresolved":false}\n')
