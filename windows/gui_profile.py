"""Isolated GUI settings; never import a repository's personal profile."""

import json
import os
from pathlib import Path
import tempfile
import tomllib


def profile_dir():
    return Path.home() / ".config/pocketnode-hh-gui"


def state_dir():
    return Path.home() / ".local/state/pocketnode-hh-gui"


def expected_profile(profile=None):
    # Callers must explicitly opt in; arbitrary environment paths cannot redirect CLI.
    expected = Path.home() / ".config/pocketnode-hh" if profile is None else Path(profile)
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


def set_model(model):
    if not isinstance(model, str) or not model.strip() or len(model) > 200:
        raise ValueError("Choose an available model")
    path = initialize()
    text = path.read_text(encoding="utf-8")
    lines, section, replaced = [], "", False
    for line in text.splitlines():
        if line.strip().startswith("["):
            section = line.strip()
        if section == "[openai.default]" and line.strip().startswith("model ="):
            line, replaced = "model = " + json.dumps(model, ensure_ascii=False), True
        lines.append(line)
    if not replaced:
        raise ValueError("GUI model setting is missing")
    atomic_write(path, "\n".join(lines) + "\n")


def validate_searches(mapping, *, allow_empty=False):
    if (not isinstance(mapping, dict) or (not mapping and not allow_empty)
            or len(mapping) > 200 or any(
                not isinstance(key, str) or not key or len(key) > 200
                or not isinstance(value, str) or not value.strip() or len(value) > 4000
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
