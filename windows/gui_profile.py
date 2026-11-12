"""Isolated GUI settings; never import a repository's personal profile."""

import json
import os
from pathlib import Path
import tempfile
import tomllib


def profile_dir():
    return Path.home() / ".config/hh-applicant-tool-gui"


def state_dir():
    return Path.home() / ".local/state/hh-applicant-tool-gui"


def expected_profile(profile=None):
    # Callers must explicitly opt in; arbitrary environment paths cannot redirect CLI.
    expected = Path.home() / ".config/hh-applicant-tool-cli" if profile is None else Path(profile)
    if profile is not None and expected != profile_dir():
        raise SystemExit("Unexpected GUI profile")
    return expected


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         delete=False) as stream:
            name = stream.name
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)  # Only our incomplete temporary file.


def initialize():
    path = profile_dir() / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        template = Path(__file__).with_name("config.example.toml").read_text(encoding="utf-8")
        parsed = tomllib.loads(template)
        if (parsed.get("general", {}).get("cover_letter_resume_id")
                or any(section.get("history") for section in parsed["openai"].values())):
            raise ValueError("GUI template must not contain personal resume context")
        # Exclusive creation protects an existing profile even across app starts.
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(template)
        except FileExistsError:
            pass
    return path


def _set_default_value(name, value, missing):
    """Replace one `name = ...` line in [openai.default], keeping every other line."""
    path = initialize()
    lines, section, replaced = [], "", False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("["):
            section = line.strip()
        if section == "[openai.default]" and line.strip().startswith(name + " ="):
            line, replaced = f"{name} = " + json.dumps(value, ensure_ascii=False), True
        lines.append(line)
    if not replaced:
        raise ValueError(missing)
    atomic_write(path, "\n".join(lines) + "\n")


def set_model(model):
    if not isinstance(model, str) or not model.strip() or len(model) > 200:
        raise ValueError("Choose an available model")
    _set_default_value("model", model, "GUI model setting is missing")


def set_provider(base_url):
    """Switch the AI provider. The model of the old provider is cleared: it may not exist here."""
    import ai_provider
    provider = ai_provider.parse(base_url)
    _set_default_value("base_url", provider.base_url, "GUI provider setting is missing")
    _set_default_value("model", "CHOOSE_AFTER_LOGIN", "GUI model setting is missing")
    return provider


def read_provider():
    import ai_provider
    config = tomllib.loads(initialize().read_text(encoding="utf-8"))
    return ai_provider.parse(config["openai"]["default"]["base_url"])


def validate_searches(mapping, *, allow_empty=False):
    if (not isinstance(mapping, dict) or (not mapping and not allow_empty)
            or len(mapping) > 200 or any(
                not isinstance(key, str) or not key or len(key) > 200
                # "" means "all vacancies HH suggests for the resume"; blank text is a mistake.
                or not isinstance(value, str) or (value and not value.strip()) or len(value) > 4000
                for key, value in mapping.items())):
        raise ValueError("Invalid resume search map")
    return mapping


def search_path():
    return state_dir() / "searches.json"


def read_searches():
    path = search_path()
    return validate_searches(json.loads(path.read_text(encoding="utf-8")), allow_empty=True) if path.exists() else {}


def save_search(resume_id, search):
    mapping = read_searches()
    mapping[resume_id] = search
    validate_searches(mapping)
    atomic_write(search_path(), json.dumps(mapping, ensure_ascii=False, indent=2))


OPTIONS = {"no_letter": False}  # GUI switches kept between runs; only these names and bool values.


def options_path():
    return state_dir() / "options.json"


def read_option(name):
    """A stored GUI switch; a missing or damaged file gives the default."""
    try:
        data = json.loads(options_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return OPTIONS[name]
    value = data.get(name) if isinstance(data, dict) else None
    return value if isinstance(value, bool) else OPTIONS[name]


def save_option(name, value):
    if name not in OPTIONS or not isinstance(value, bool):
        raise ValueError("Unknown GUI option")
    try:
        data = json.loads(options_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data = {key: data[key] for key in OPTIONS if isinstance(data, dict) and isinstance(data.get(key), bool)}
    data[name] = value
    atomic_write(options_path(), json.dumps(data, indent=2))


# Placeholders HH 2.3.7 fills in the cover letter prompt (apply_vacancies.py).
PROMPT_PLACEHOLDERS = ("vacancy_name", "vacancy_url", "employer_name", "first_name", "last_name",
                       "email", "phone", "resume_hash", "resume_title", "resume_url")
MAX_PROMPT = 8000


def validate_prompt(prompt):
    """Reject a letter style that HH would fail to render, in plain language."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Стиль письма пуст. Опиши, каким должно быть письмо.")
    if len(prompt) > MAX_PROMPT:
        raise ValueError(f"Стиль письма слишком длинный: максимум {MAX_PROMPT} символов.")
    try:
        prompt % {name: "x" for name in PROMPT_PLACEHOLDERS}
    except KeyError as error:
        raise ValueError(f"Неизвестная подстановка %({error.args[0]})s. Доступные: "
                         + ", ".join(f"%({name})s" for name in PROMPT_PLACEHOLDERS) + ".") from None
    except (ValueError, TypeError):
        raise ValueError("Знак % в тексте пиши как %%. Подстановки пиши так: %(vacancy_name)s.") from None
    return prompt


def default_prompt():
    template = Path(__file__).with_name("config.example.toml").read_text(encoding="utf-8")
    return tomllib.loads(template)["general"]["cover_letter_prompt"].strip()


def read_prompt():
    config = tomllib.loads(initialize().read_text(encoding="utf-8"))
    return str(config.get("general", {}).get("cover_letter_prompt") or default_prompt()).strip()


def set_prompt(prompt):
    """Replace only [general].cover_letter_prompt, keeping every other line as is."""
    validate_prompt(prompt)
    path = initialize()
    lines, section, skipping, closing, written = [], "", False, None, False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if skipping:
            if closing in stripped:
                skipping = False
            continue
        if stripped.startswith("[") and not skipping:
            if section == "[general]" and not written:
                lines.append("cover_letter_prompt = " + json.dumps(prompt, ensure_ascii=False))
                written = True
            section = stripped
        if section == "[general]" and stripped.startswith("cover_letter_prompt"):
            value = stripped.split("=", 1)[1].strip()
            for quote in ('"""', "'''"):
                if value.startswith(quote) and value.count(quote) == 1:
                    skipping, closing = True, quote
            if not written:
                lines.append("cover_letter_prompt = " + json.dumps(prompt, ensure_ascii=False))
                written = True
            continue
        lines.append(line)
    if section == "[general]" and not written:
        lines.append("cover_letter_prompt = " + json.dumps(prompt, ensure_ascii=False))
        written = True
    if not written:
        raise ValueError("В профиле нет раздела [general]")
    text = "\n".join(lines) + "\n"
    if tomllib.loads(text).get("general", {}).get("cover_letter_prompt") != prompt:
        raise ValueError("Не удалось записать стиль письма")
    atomic_write(path, text)


# ---- letter styles (issue #4) ---------------------------------------------------------------
# Several named styles; the active one is written to [general].cover_letter_prompt, so
# letter generation itself does not change. "Исходный" comes from the shipped template:
# it is never stored and can be duplicated, but not changed or deleted.

BUILTIN_STYLE = "Исходный"
MIGRATED_STYLE = "Мой стиль"
MAX_STYLE_NAME = 60
MAX_STYLES = 50


def styles_path():
    return state_dir() / "letter-styles.json"


def validate_style_name(name, taken=()):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Введи название стиля.")
    name = name.strip()
    if len(name) > MAX_STYLE_NAME:
        raise ValueError(f"Название стиля слишком длинное: максимум {MAX_STYLE_NAME} символов.")
    if name.casefold() in {item.casefold() for item in (BUILTIN_STYLE, *taken)}:
        raise ValueError(f"Стиль «{name}» уже есть. Выбери другое название.")
    return name


def _valid_store(data):
    styles = data.get("styles") if isinstance(data, dict) else None
    if (data.get("v") != 1 or not isinstance(styles, dict) or len(styles) > MAX_STYLES
            or not isinstance(data.get("active"), str)):
        raise ValueError("Invalid letter styles")
    names = []
    for name, prompt in styles.items():
        validate_style_name(name, names)
        validate_prompt(prompt)
        names.append(name)
    if data["active"] != BUILTIN_STYLE and data["active"] not in styles:
        raise ValueError("Invalid active letter style")
    return data


def _write_styles(styles, active):
    data = _valid_store({"v": 1, "active": active, "styles": dict(styles)})
    path = styles_path()
    if path.exists():
        try:
            _valid_store(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, AttributeError):
            # Keep a damaged file for the person to inspect; never silently drop it.
            os.replace(path, path.with_name(path.name + ".damaged"))
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
    set_prompt(default_prompt() if active == BUILTIN_STYLE else styles[active])


def read_styles():
    """(styles, active, warning). styles: ordered {name: prompt}, "Исходный" first.

    A missing file migrates the profile's single style; a damaged file falls back to
    "Исходный" with a warning and stays untouched until the next save.
    """
    builtin = {BUILTIN_STYLE: default_prompt()}
    path = styles_path()
    if not path.exists():
        current = read_prompt()
        if current == default_prompt():
            _write_styles({}, BUILTIN_STYLE)
            return builtin, BUILTIN_STYLE, None
        _write_styles({MIGRATED_STYLE: current}, MIGRATED_STYLE)
        return {**builtin, MIGRATED_STYLE: current}, MIGRATED_STYLE, None
    try:
        data = _valid_store(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, AttributeError):
        return builtin, BUILTIN_STYLE, ("Файл стилей письма повреждён. Сейчас действует стиль «Исходный». "
                                        "Сохрани стиль заново; старый файл останется рядом с именем .damaged.")
    return {**builtin, **data["styles"]}, data["active"], None


def _stored():
    styles, active, _warning = read_styles()
    styles.pop(BUILTIN_STYLE)
    return styles, active


def save_style(name, prompt):
    """Create or change a style and make it active. Returns the saved name."""
    validate_prompt(prompt)
    styles, _active = _stored()
    if name == BUILTIN_STYLE:
        raise ValueError("Стиль «Исходный» нельзя изменить. Нажми «Дублировать» и измени копию.")
    if name not in styles:
        name = validate_style_name(name, styles)
        if len(styles) >= MAX_STYLES:
            raise ValueError(f"Стилей уже {MAX_STYLES}. Удали ненужный стиль.")
    styles[name] = prompt
    _write_styles(styles, name)
    return name


def free_style_name(base, taken):
    taken = {item.casefold() for item in (BUILTIN_STYLE, *taken)}
    if base.casefold() not in taken:
        return base
    number = 2
    while f"{base} {number}".casefold() in taken:
        number += 1
    return f"{base} {number}"


def duplicate_style(name, new_name=None):
    styles, _active = _stored()
    source = default_prompt() if name == BUILTIN_STYLE else styles[name]
    new_name = validate_style_name(new_name or free_style_name(name + " (копия)", styles), styles)
    if len(styles) >= MAX_STYLES:
        raise ValueError(f"Стилей уже {MAX_STYLES}. Удали ненужный стиль.")
    styles[new_name] = source
    _write_styles(styles, new_name)
    return new_name


def rename_style(name, new_name):
    styles, active = _stored()
    if name == BUILTIN_STYLE or name not in styles:
        raise ValueError("Стиль «Исходный» нельзя переименовать." if name == BUILTIN_STYLE
                         else "Такого стиля нет.")
    new_name = validate_style_name(new_name, [item for item in styles if item != name])
    renamed = {(new_name if item == name else item): prompt for item, prompt in styles.items()}
    _write_styles(renamed, new_name if active == name else active)
    return new_name


def delete_style(name):
    styles, active = _stored()
    if name == BUILTIN_STYLE:
        raise ValueError("Стиль «Исходный» нельзя удалить.")
    styles.pop(name)
    _write_styles(styles, BUILTIN_STYLE if active == name else active)


def activate_style(name):
    styles, _active = _stored()
    if name != BUILTIN_STYLE and name not in styles:
        raise ValueError("Такого стиля нет.")
    _write_styles(styles, name)
