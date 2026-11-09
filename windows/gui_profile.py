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
