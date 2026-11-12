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
    chatgpt: bool = False  # The AI provider is ready: ChatGPT login, a stored key or a local model.
    ai_kind: str = "chatmock"
    resume: bool = False
    search: bool = False
    model: bool = False
    no_letter: bool = False  # #9: applications without a letter need no AI provider or model.


# Reason shown next to a locked button: the first missing prerequisite wins.
REQUIREMENTS = (
    ("hh", "Сначала войди в HH: меню «Настройки» → «Аккаунт HH…»."),
    ("chatgpt", "Сначала подключи AI: меню «Настройки» → «AI-провайдер и модель…»."),
    ("resume", "Сначала загрузи и выбери резюме в блоке «Резюме и поиск»."),
    ("search", "Сначала введи поисковый запрос или выбери «Все подходящие» в блоке «Резюме и поиск»."),
    ("model", "Сначала выбери модель: меню «Настройки» → «AI-провайдер и модель…»."),
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
    needs = NEEDS[action]
    if action in {"apply", "auto"} and progress.no_letter:
        needs = tuple(key for key in needs if key not in {"chatgpt", "model"})
    for key, reason in REQUIREMENTS:
        if key in needs and not getattr(progress, key):
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
        return "Нажми «Войти в HH»."
    if not progress.chatgpt:
        return ("Нажми «Войти в ChatGPT»." if progress.ai_kind == "chatmock" else
                "Нажми «AI-провайдер…» и сохрани ключ API.")
    if not progress.resume:
        return "Резюме загружаются сами после входа в HH. Если список пуст, нажми «Обновить список резюме»."
    if not progress.search:
        return "Введи поисковый запрос или выбери «Все подходящие для резюме»."
    if not progress.model:
        return "Нажми «Загрузить список моделей» и выбери модель."
    return "Всё готово. Нажми «Предпросмотр писем». Он ничего не отправляет."


FAILURE_HINTS = {
    "hh_login": "Вход в HH не выполнен. Проверь email или телефон и код, потом нажми «Войти в HH» снова.",
    "chatgpt_login": "Вход в ChatGPT не выполнен. Введи код на странице входа до истечения времени и попробуй снова.",
    "resumes": "Резюме не загружены. Проверь вход в HH. На HH нужно хотя бы одно опубликованное резюме.",
    "models": "Список моделей не загружен. Проверь адрес и ключ AI-провайдера или войди в ChatGPT заново.",
    "preview": "Предпросмотр не завершён. Проверь вход в HH, AI-провайдера и поисковый запрос.",
    "apply": "Отклики не завершены. Проверь вход в HH и AI-провайдера.",
    "auto": "Автоотклики не завершены. Проверь вход в HH и AI-провайдера.",
    "chats": "Ответы в чатах не завершены. Проверь вход в HH и AI-провайдера.",
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


def _reset_time(reason):
    """Local time of a provider limit reset found in the reason, or None."""
    import re
    import time
    match = re.search(r"reset_epoch=(\d{10})\b", reason)
    if not match:
        return None
    moment = time.localtime(int(match.group(1)))
    today = time.localtime()
    same_day = (moment.tm_year, moment.tm_yday) == (today.tm_year, today.tm_yday)
    return time.strftime("в %H:%M" if same_day else "%d.%m в %H:%M", moment)


# Internal stop reasons (English, for the journal) -> what the person reads (#11).
# Each pattern matches the text our own guards raise; the first match wins.
_REASONS = (
    (r"fallback list exhausted", lambda m, r: (
        "Модели из списка запасных закончились: у каждой кончился лимит или она недоступна."
        + (f" Ближайший сброс лимита {_reset_time(r)}." if _reset_time(r) else "")
        + " Добавь ещё бесплатную модель или повтори позже.")),
    (r"(?P<host>[\w.-]+): daily limit of free model requests", lambda m, r: (
        f"Закончился дневной лимит бесплатных запросов {m['host']}."
        + (f" Он обновится {_reset_time(r)}." if _reset_time(r) else " Он обновится завтра.")
        + " Можно выбрать другого AI-провайдера или модель.")),
    (r"(?P<host>[\w.-]+): rate limit \(HTTP 429\)", lambda m, r:
        f"AI-провайдер {m['host']} ограничил частоту запросов. Подожди минуту и повтори."),
    (r"(?P<host>[\w.-]+): HTTP (?P<code>40[13]); check the API key", lambda m, r:
        f"AI-провайдер {m['host']} отклонил запрос (HTTP {m['code']}). Проверь ключ API и доступ к модели."),
    (r"(?P<host>[a-z0-9-]+(?:\.[a-z0-9-]+)+): HTTP (?P<code>\d{3})", lambda m, r:
        f"AI-провайдер {m['host']} вернул ошибку HTTP {m['code']}. Повтори позже или выбери другую модель."),
    (r"AI provider request failed", lambda m, r:
        "AI-провайдер не ответил. Повтори позже или выбери другую модель."),
    (r"empty model response|empty test answer", lambda m, r:
        "AI-провайдер вернул пустой ответ. Повтори или выбери другую модель."),
    (r"rounds failed for \d+ vacancies in a row", lambda m, r:
        "AI-провайдер не отвечает несколько вакансий подряд. Повтори позже."),
    (r"ChatGPT (?:connect timeout|TLS handshake)|local model gateway HTTP|model generation failed"
     r"|Preview stopped after model failure", lambda m, r:
        "Модель не написала письмо. Повтори; если ошибка повторится, войди в ChatGPT заново или выбери другую модель."),
    (r"after HTTP 429", lambda m, r: "HH ограничил частоту запросов (HTTP 429). Подожди несколько минут и повтори."),
    (r"after HTTP 403", lambda m, r: "HH отказал в доступе (HTTP 403). Проверь вход в HH."),
    (r"after HTTP (?P<code>5\d\d)", lambda m, r: f"HH временно не отвечает (HTTP {m['code']}). Повтори позже."),
    (r"after HTTP (?P<code>\d{3})", lambda m, r: f"HH вернул ошибку HTTP {m['code']}."),
    (r"Send outcome uncertain|No confirmed send result", lambda m, r:
        "Неизвестно, дошёл ли отклик: HH не ответил. Проверь отклики на hh.ru, прежде чем повторять."),
    (r"(?:Test send|Send) returned HTTP (?P<code>\d{3})", lambda m, r:
        f"HH не принял отклик (HTTP {m['code']}). Проверь отклики на hh.ru, прежде чем повторять."),
    (r"HH did not accept the test application", lambda m, r:
        "HH не принял отклик с тестом. Проверь вакансию на hh.ru."),
    (r"network failure|Network failure", lambda m, r: "Нет связи с HH. Проверь интернет или VPN и повтори."),
    (r"Manual HH captcha failed or cancelled", lambda m, r:
        "Капча не введена: окно закрыто или ввод прерван."),
    (r"Captcha was not (?:accepted|entered)", lambda m, r: "HH не принял капчу."),
    (r"requires an available Tkinter window", lambda m, r: "Не удалось открыть окно капчи."),
    (r"Repeated HH captcha|Recursive HH captcha", lambda m, r:
        "HH снова запросил капчу. Попробуй позже."),
    (r"(?P<count>\d+) letters in a row failed the letter check", lambda m, r:
        f"Писем подряд не прошли проверку: {m['count']}. Проверь стиль письма или выбери другую модель."),
    (r"Vacancy salary changed", lambda m, r: "Зарплата в вакансии изменилась и вышла за лимит."),
    (r"vacancy-detail failure", lambda m, r: "Не удалось прочитать вакансию в HH. Повтори позже."),
    (r"No approval", lambda m, r: "Отклик не подтверждён."),
    (r"Invalid manual letter", lambda m, r: "Пустой текст письма не принят."),
    (r"[Bb]locked (?:an )?unexpected network request", lambda m, r:
        "Защита программы остановила неожиданный сетевой запрос."),
)


def readable_reason(reason):
    """The person-facing sentence for an internal stop reason; Russian reasons pass as they are."""
    import re
    if not isinstance(reason, str) or not reason.strip():
        return "Запуск остановлен защитой программы."
    if len(re.findall(r"[А-Яа-яЁё]", reason)) > len(re.findall(r"[A-Za-z]", reason)):
        return reason.strip()  # Already written for the person, e.g. "HH повторно запросил капчу…".
    for pattern, render in _REASONS:
        if match := re.search(pattern, reason):
            return render(match, reason)
    return "Запуск остановлен защитой программы."
