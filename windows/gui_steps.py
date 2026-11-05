"""Pure GUI guidance: step states, action prerequisites and plain-language results.

No Tk and no network here, so every rule is checked offline.
"""

import json
from dataclasses import dataclass

from gui_profile import profile_dir


def hh_signed_in():
    """True when the GUI profile holds an HH token. Reads only key presence."""
    try:
        data = json.loads((profile_dir() / "token.json").read_text(encoding="utf-8"))
        token = data.get("token") if isinstance(data, dict) else None
        return isinstance(token, dict) and isinstance(token.get("refresh_token"), str) and bool(token["refresh_token"])
    except (OSError, ValueError):
        return False


def chatgpt_signed_in():
    try:
        from gui_chatmock_auth import GUIAuthStore
        return GUIAuthStore().read() is not None
    except (ImportError, OSError):
        return False


@dataclass
class Progress:
    hh: bool = False
    chatgpt: bool = False
    resume: bool = False
    search: bool = False
    model: bool = False


# Reason shown next to a locked button: the first missing prerequisite wins.
REQUIREMENTS = (
    ("hh", "Сначала войди в HH (шаг 1)."),
    ("chatgpt", "Сначала войди в ChatGPT (шаг 1)."),
    ("resume", "Сначала загрузи и выбери резюме (шаг 2)."),
    ("search", "Сначала введи поисковый запрос (шаг 2)."),
    ("model", "Сначала выбери модель ChatGPT (шаг 3)."),
)
NEEDS = {
    "hh_login": (), "chatgpt_login": (),
    "resumes": ("hh",), "models": ("chatgpt",),
    "chats": ("hh", "chatgpt", "model"),
    "preview": ("hh", "chatgpt", "resume", "search", "model"),
    "apply": ("hh", "chatgpt", "resume", "search", "model"),
    "auto": ("hh", "chatgpt", "resume", "search", "model"),
}


def blocker(action, progress):
    """Plain reason why an action cannot start yet, or None when it can."""
    for key, reason in REQUIREMENTS:
        if key in NEEDS[action] and not getattr(progress, key):
            return reason
    return None


DONE, CURRENT, LOCKED, OPEN = "done", "current", "locked", "open"
MARKS = {DONE: "✓", CURRENT: "▶", LOCKED: "○", OPEN: "•"}


def step_states(progress):
    """States of steps 1-4. The first unfinished available step is CURRENT."""
    done = (progress.hh and progress.chatgpt, progress.resume and progress.search, progress.model, False)
    available = (True, progress.hh, progress.chatgpt, all(done[:3]))
    states = [DONE if finished else OPEN if ready else LOCKED for finished, ready in zip(done, available)]
    if OPEN in states:
        states[states.index(OPEN)] = CURRENT
    return states


def next_hint(progress):
    """One sentence for the status banner: what the person does next."""
    if not progress.hh:
        return "Шаг 1: нажми «Войти в HH»."
    if not progress.chatgpt:
        return "Шаг 1: нажми «Войти в ChatGPT»."
    if not progress.resume:
        return "Шаг 2: нажми «Загрузить мои резюме» и выбери резюме."
    if not progress.search:
        return "Шаг 2: введи поисковый запрос, например «Python разработчик»."
    if not progress.model:
        return "Шаг 3: нажми «Загрузить список моделей» и выбери модель."
    return "Всё готово. Шаг 4: нажми «Предпросмотр писем». Он ничего не отправляет."


FAILURE_HINTS = {
    "hh_login": "Вход в HH не выполнен. Проверь email или телефон и код, потом нажми «Войти в HH» снова.",
    "chatgpt_login": "Вход в ChatGPT не выполнен. Введи код на странице входа до истечения времени и попробуй снова.",
    "resumes": "Резюме не загружены. Проверь вход в HH. На HH нужно хотя бы одно опубликованное резюме.",
    "models": "Список моделей не загружен. Войди в ChatGPT заново и попробуй снова.",
    "preview": "Предпросмотр не завершён. Проверь вход в HH и ChatGPT и поисковый запрос.",
    "apply": "Отклики не завершены. Проверь вход в HH и ChatGPT.",
    "auto": "Автоотклики не завершены. Проверь вход в HH и ChatGPT.",
    "chats": "Ответы в чатах не завершены. Проверь вход в HH и ChatGPT.",
}


def parse_auto_settings(limit, minimum, maximum):
    """Validate the auto-send fields. Returns (values, None) or (None, reason)."""
    try:
        limit = int(str(limit).strip())
    except ValueError:
        return None, "Лимит откликов — целое число, например 5."
    try:
        minimum, maximum = float(str(minimum).strip().replace(",", ".")), float(str(maximum).strip().replace(",", "."))
    except ValueError:
        return None, "Пауза — число секунд, например 60 и 180."
    if limit < 1:
        return None, "Лимит откликов — целое число больше 0."
    if not 0 < minimum <= maximum <= 86400:
        return None, "Первая пауза должна быть больше 0 и не больше второй."
    return (limit, minimum, maximum), None
