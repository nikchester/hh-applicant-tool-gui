"""Saved vacancy filters (#15): a strict file, HH search arguments and local skip rules.

Only closed lists reach the HH query (period, labels, minimum salary, only with salary).
Everything else is a local rule that runs before a letter is generated and writes nothing
to the HH account: upstream --ai-filter and --excluded-filter blacklist vacancies there.
"""

from dataclasses import dataclass, field, fields
from functools import lru_cache
import json
import os
from pathlib import Path
import re

from vacancy_level import senior_title_reason
from vacancy_salary import MAXIMUM_RUB, MINIMUM_NET_RUB, salary_rejection_reason

# HH /dictionaries (experience): id -> label in the window, reason text when it is excluded.
EXPERIENCE = {
    "noExperience": ("нет опыта", "опыт не требуется"),
    "between1And3": ("1–3 года", "требуется опыт 1–3 года"),
    "between3And6": ("3–6 лет", "требуется опыт 3–6 лет"),
    "moreThan6": ("более 6 лет", "требуется опыт более 6 лет"),
}
PERIODS = (1, 3, 7, 30)
# HH /dictionaries (vacancy_label), in the order they are passed to --label.
AGENCY_LABEL, ACCREDITED_LABEL = "not_from_agency", "accredited_it"
MAX_SALARY = 10_000_000
MAX_STOP_WORDS, MAX_STOP_WORD_LENGTH = 200, 100
MAX_EMPLOYERS, MAX_EMPLOYER_NAME = 500, 100
MAX_FILE_BYTES = 1_000_000


@dataclass(frozen=True)
class Filters:
    """The defaults repeat the behaviour before filters existed."""
    salary_minimum: int = MINIMUM_NET_RUB
    salary_ceiling: int | None = MAXIMUM_RUB
    only_with_salary: bool = False
    exclude_senior: bool = True
    experience: tuple = tuple(EXPERIENCE)
    stop_words: tuple = ()
    hidden_employers: tuple = field(default=())  # ({"id": "1740", "name": "Яндекс"}, ...)
    period: int | None = None
    not_from_agency: bool = False
    accredited_it: bool = False

    def data(self):
        data = {item.name: getattr(self, item.name) for item in fields(self)}
        data["experience"] = list(self.experience)
        data["stop_words"] = list(self.stop_words)
        data["hidden_employers"] = [dict(item) for item in self.hidden_employers]
        return data


DEFAULTS = Filters()


def _integer(data, key, low, high, *, nullable=False):
    value = data[key]
    if value is None and nullable:
        return None
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"«{key}»: нужно целое число от {low} до {high}"
                         + (" или null" if nullable else ""))
    return value


def _flag(data, key):
    if type(data[key]) is not bool:
        raise ValueError(f"«{key}»: нужно true или false")
    return data[key]


def normalize_stop_word(word):
    """One stored form: single spaces, no edge spaces. Rejects what could never match."""
    if not isinstance(word, str):
        raise ValueError("стоп-слово должно быть строкой")
    word = " ".join(word.split())
    if not word or len(word) > MAX_STOP_WORD_LENGTH:
        raise ValueError(f"стоп-слово: от 1 до {MAX_STOP_WORD_LENGTH} символов")
    if not any(char.isalnum() for char in word) or any(not char.isprintable() for char in word):
        raise ValueError(f"стоп-слово «{word[:40]}» нужно записать буквами или цифрами")
    return word


def normalize_employer_id(value):
    if not (isinstance(value, str) and value.isascii() and value.isdecimal() and 0 < len(value) <= 12):
        raise ValueError("id работодателя — только цифры, до 12 знаков")
    return value


def parse(data):
    """Strict check of a decoded filters file. Unknown keys and values outside the lists are errors."""
    if not isinstance(data, dict):
        raise ValueError("ожидался объект JSON")
    names = {item.name for item in fields(Filters)}
    unknown = sorted(str(key) for key in data if key not in names)
    if unknown:
        raise ValueError("неизвестный ключ: " + ", ".join(f"«{key}»" for key in unknown))
    data = {**DEFAULTS.data(), **data}
    minimum = _integer(data, "salary_minimum", 0, MAX_SALARY)
    ceiling = _integer(data, "salary_ceiling", 1, MAX_SALARY, nullable=True)
    if ceiling is not None and ceiling < minimum:
        raise ValueError("потолок зарплаты ниже минимума: подходящих вакансий не будет")
    experience = data["experience"]
    if (not isinstance(experience, list) or not experience
            or any(not isinstance(item, str) or item not in EXPERIENCE for item in experience)
            or len(set(experience)) != len(experience)):
        raise ValueError("«experience»: выбери хотя бы одно из " + ", ".join(EXPERIENCE))
    period = _integer(data, "period", 1, 30, nullable=True)
    if period is not None and period not in PERIODS:
        raise ValueError("«period»: " + ", ".join(map(str, PERIODS)) + " дней или null")
    words = data["stop_words"]
    if not isinstance(words, list) or len(words) > MAX_STOP_WORDS:
        raise ValueError(f"«stop_words»: список не длиннее {MAX_STOP_WORDS} слов")
    words = [normalize_stop_word(word) for word in words]
    if len({_fold(word) for word in words}) != len(words):
        raise ValueError("стоп-слова повторяются")
    employers = data["hidden_employers"]
    if not isinstance(employers, list) or len(employers) > MAX_EMPLOYERS:
        raise ValueError(f"«hidden_employers»: список не длиннее {MAX_EMPLOYERS} компаний")
    clean = []
    for item in employers:
        if not isinstance(item, dict) or set(item) != {"id", "name"}:
            raise ValueError("«hidden_employers»: у каждой компании ровно два поля, «id» и «name»")
        name = item["name"]
        if not isinstance(name, str) or len(name) > MAX_EMPLOYER_NAME or not name.isprintable():
            raise ValueError(f"«hidden_employers»: название — строка до {MAX_EMPLOYER_NAME} символов")
        clean.append({"id": normalize_employer_id(item["id"]), "name": name.strip()})
    if len({item["id"] for item in clean}) != len(clean):
        raise ValueError("компании в списке повторяются")
    return Filters(
        salary_minimum=minimum, salary_ceiling=ceiling,
        only_with_salary=_flag(data, "only_with_salary"), exclude_senior=_flag(data, "exclude_senior"),
        experience=tuple(item for item in EXPERIENCE if item in experience),
        stop_words=tuple(words), hidden_employers=tuple(clean), period=period,
        not_from_agency=_flag(data, "not_from_agency"), accredited_it=_flag(data, "accredited_it"),
    )


def filters_path():
    """HH_APPLICANT_TOOL_FILTERS_FILE is set by the GUI; the command line reads windows/filters.json."""
    return Path(os.environ.get("HH_APPLICANT_TOOL_FILTERS_FILE") or Path(__file__).with_name("filters.json"))


def read(path=None):
    """Saved filters, or the defaults when there is no file. A damaged file is a ValueError."""
    path = Path(path or filters_path())
    try:
        if not path.exists():
            return DEFAULTS
        with path.open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("файл слишком большой")
        return parse(json.loads(raw.decode("utf-8")))
    except OSError:
        raise ValueError("файл не читается") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("это не JSON") from None


def load(path=None):
    """Like read(), but a problem stops the run: filters are never reset silently."""
    try:
        return read(path)
    except ValueError as problem:
        raise SystemExit(
            f"Фильтры вакансий: файл filters.json не принят ({problem}). "
            "Исправь фильтры в «Настройки → Фильтры вакансий…» или удали файл, чтобы вернуть значения "
            "по умолчанию. Ничего не отправлено.") from None


def save(filters, path):
    from gui_profile import atomic_write
    parse(filters.data())  # Never write a file the next run would refuse.
    atomic_write(path, json.dumps(filters.data(), ensure_ascii=False, indent=2) + "\n")


def labels(filters):
    return ([AGENCY_LABEL] if filters.not_from_agency else []) + (
        [ACCREDITED_LABEL] if filters.accredited_it else [])


def search_arguments(filters):
    """The HH search part of argv. Default filters give the arguments from before #15, byte for byte."""
    return [
        "--work-format", "REMOTE", "--currency", "RUR", "--salary", str(filters.salary_minimum),
        "--only-with-salary" if filters.only_with_salary else "--no-only-with-salary",
        "--search-field", "name", "--order-by", "publication_time",
    ] + (["--period", str(filters.period)] if filters.period else []
         ) + (["--label", *labels(filters)] if labels(filters) else [])


def search_matches(args, filters):
    """Whether parsed upstream arguments carry exactly these filters and nothing else for the search."""
    return (args.work_format == ["REMOTE"] and args.currency == "RUR"
            and args.salary == filters.salary_minimum and args.only_with_salary is filters.only_with_salary
            and args.order_by == "publication_time" and args.search_field == ["name"]
            and args.period == (filters.period or None) and args.label == (labels(filters) or None)
            # Companies and experience are local rules; upstream's blacklist-writing options stay off.
            and args.experience is None and args.excluded_employer_id is None
            and args.excluded_filter is None and args.ai_filter is False)


# Cyrillic letters drawn like Latin ones: «1С» and «1C» are the same word to a reader.
_LOOKALIKES = str.maketrans("авекмнорстух", "abekmhopctyx")


def _fold(text):
    return " ".join(text.casefold().replace("ё", "е").translate(_LOOKALIKES).split())


@lru_cache(maxsize=512)
def _stop_pattern(word):
    folded = _fold(word)
    tokens = [re.escape(token) for token in folded.split()]
    # A boundary only where the word starts or ends with a letter or digit: ".net" is found in "ASP.NET".
    before = r"(?<!\w)" if folded[0].isalnum() or folded[0] == "_" else ""
    after = r"(?!\w)" if folded[-1].isalnum() or folded[-1] == "_" else ""
    return re.compile(before + r"\s+".join(tokens) + after)


def stop_word_in(text, words):
    """The first stop word found in the text as a whole word or phrase, ignoring case; else None."""
    text = _fold(text or "")
    return next((word for word in words if _stop_pattern(word).search(text)), None)


def experience_check(vacancy, filters):
    """(reason, note): HH shows no experience field for some cards, then the vacancy stays."""
    if set(filters.experience) == set(EXPERIENCE):
        return None, None
    card = vacancy.get("experience")
    identifier = card.get("id") if isinstance(card, dict) else None
    if identifier in filters.experience:
        return None, None
    if identifier in EXPERIENCE:
        return EXPERIENCE[identifier][1], None
    return None, "в выдаче HH нет требуемого опыта у этой вакансии, проверка опыта пропущена"


def check(vacancy, filters, api_client=None):
    """(reason, notes): every local rule on a search result, in one place. A reason skips the vacancy."""
    if filters.exclude_senior and (reason := senior_title_reason(vacancy)):
        return reason, []
    employer = vacancy.get("employer") or {}
    if str(employer.get("id")) in {item["id"] for item in filters.hidden_employers}:
        return "компания скрыта", []
    if word := stop_word_in(vacancy.get("name"), filters.stop_words):
        return f"стоп-слово «{word}» в названии вакансии", []
    if word := stop_word_in(employer.get("name"), filters.stop_words):
        return f"стоп-слово «{word}» в названии компании", []
    reason, note = experience_check(vacancy, filters)
    if reason:
        return reason, []
    reason = salary_rejection_reason(vacancy, api_client, minimum=filters.salary_minimum,
                                     ceiling=filters.salary_ceiling)
    return reason, [note] if note else []


def skip_reason(vacancy, filters, api_client=None):
    return check(vacancy, filters, api_client)[0]


def salary_reason(vacancy, filters, api_client=None):
    """The salary rule alone, for the full vacancy card read before a letter is written."""
    return salary_rejection_reason(vacancy, api_client, minimum=filters.salary_minimum,
                                   ceiling=filters.salary_ceiling)


def money(amount):
    return f"{amount:,}".replace(",", " ")


def summary(filters):
    """One line for the main screen, e.g. «удалённо · от 80 000 ₽ · без Senior/Lead · стоп-слов: 3»."""
    parts = ["удалённо", f"от {money(filters.salary_minimum)} ₽"]
    if filters.salary_ceiling != DEFAULTS.salary_ceiling:
        parts.append("без потолка" if filters.salary_ceiling is None
                     else f"до {money(filters.salary_ceiling)} ₽")
    if filters.exclude_senior:
        parts.append("без Senior/Lead")
    if filters.only_with_salary:
        parts.append("только с зарплатой")
    if set(filters.experience) != set(EXPERIENCE):
        parts.append("опыт: " + ", ".join(EXPERIENCE[item][0] for item in filters.experience))
    if filters.stop_words:
        parts.append(f"стоп-слов: {len(filters.stop_words)}")
    if filters.hidden_employers:
        parts.append(f"скрыто компаний: {len(filters.hidden_employers)}")
    if filters.period:
        parts.append(f"за {filters.period} дн.")
    if filters.not_from_agency:
        parts.append("без агентств")
    if filters.accredited_it:
        parts.append("аккредитованные ИТ")
    return " · ".join(parts)


EMPLOYER_LINK = re.compile(r"(?:^|/)employer/(\d{1,12})$")


def parse_employer_line(line):
    """«1740 Яндекс», «https://hh.ru/employer/1740» or a bare id -> {"id", "name"}; else a ValueError."""
    head, name = (line.split(None, 1) + [""])[:2] if line.strip() else ("", "")
    link = EMPLOYER_LINK.search(head.split("?")[0].rstrip("/"))
    try:
        identifier = normalize_employer_id(link.group(1) if link else head)
    except ValueError:
        raise ValueError(f"не понял строку «{line.strip()[:50]}»: нужен id компании (цифры) "
                         "или ссылка hh.ru/employer/…") from None
    return {"id": identifier, "name": " ".join(name.split())[:MAX_EMPLOYER_NAME]}


def employer_line(item):
    return (item["id"] + " " + item["name"]).strip()
