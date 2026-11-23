"""All logs of the app in one piece for a developer: a ZIP file or one text for the clipboard.

Only the journal (log/gui.log*) and the diagnostic logs of runs (run/chatmock-*.log,
run/hh-chats-*.jsonl) go in: never tokens, keys, cookies or settings. The texts pass
scrub() first: the home folder becomes ~, letter texts and anything shaped like a token
are hidden, because a bundle may end up in a public issue.

No Tk here, so every rule is checked offline.
"""

from pathlib import Path
import re
import time
import zipfile

# The names our own code writes; any other file in these folders stays out.
LOG_NAME = re.compile(r"^gui\.log(?:\.\d)?$")
RUN_NAME = re.compile(r"^(?:chatmock-[0-9a-f]{32}\.(?:out|err)\.log|hh-chats-[0-9a-f]{32}\.jsonl)$")
# One file larger than this keeps only its end: the latest events are the useful ones.
MAX_FILE = 5 * 1024 * 1024
# The ZIP takes the newest files up to this many bytes of text.
MAX_ZIP_TEXT = 60 * 1024 * 1024
# A messenger or an issue field takes a few megabytes at most.
CLIPBOARD_LIMIT = 2 * 1024 * 1024
PRIVATE_LINES = ("Отклонённое письмо: ",)
_SECRETS = (
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [скрыто]"),
    (re.compile(r'(?i)("?(?:access_token|refresh_token|id_token|api_key|apikey|password|secret|cookie)"?'
                r"\s*[:=]\s*\"?)[^\s\",;&]{4,}"), r"\1[скрыто]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "[скрыто]"),
)


def state_dir():
    from gui_profile import state_dir as profile_state_dir
    return profile_state_dir()


def scrub(text, home=None):
    """Hide what may identify the person: the home folder, letter texts, token-shaped values."""
    lines = []
    for line in text.split("\n"):
        for prefix in PRIVATE_LINES:
            at = line.find(prefix)
            if at >= 0:
                line = line[:at + len(prefix)] + "[скрыто]"
        lines.append(line)
    text = "\n".join(lines)
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    if home:
        # Paths name the Windows user: show them as ~ in both slash styles, JSON escapes too.
        home = str(home)
        variants = {home, home.replace("\\", "/"), home.replace("\\", "\\\\")}
        for variant in sorted(variants, key=len, reverse=True):
            if len(variant) > 3:
                text = re.sub(re.escape(variant), "~", text, flags=re.IGNORECASE)
    return text


def log_files(root=None):
    """(name in the bundle, path) of every log, the newest first."""
    root = Path(root or state_dir())
    found = []
    for folder, pattern in (("log", LOG_NAME), ("run", RUN_NAME)):
        try:
            entries = list((root / folder).iterdir())
        except OSError:
            continue
        for path in entries:
            try:
                if pattern.match(path.name) and path.is_file() and not path.is_symlink():
                    found.append((path.stat().st_mtime, f"{folder}/{path.name}", path))
            except OSError:
                continue
    found.sort(key=lambda item: item[0], reverse=True)
    return [(name, path) for _mtime, name, path in found]


def _read(path, limit=None):
    """The text of one log; a longer file keeps its last `limit` bytes with a note."""
    limit = limit or MAX_FILE
    try:
        with path.open("rb") as stream:
            size = path.stat().st_size
            if size > limit:
                stream.seek(size - limit)
            data = stream.read(limit)
    except OSError:
        return None
    text = data.decode("utf-8", errors="replace")
    if size > limit:
        text = f"[начало файла обрезано: показаны последние {limit // 1024} КБ из {size // 1024} КБ]\n" + text
    return text


def write_zip(target, report, root=None, home=None):
    """Write report.txt and every log into a ZIP; returns (files written, files left out)."""
    target = Path(target)
    written, left_out, total = 0, [], 0
    partial = target.with_name(target.name + ".part")
    try:
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("report.txt", scrub(report, home))
            for name, path in log_files(root):
                text = _read(path)
                if text is None:
                    left_out.append(name)
                    continue
                if total + len(text) > MAX_ZIP_TEXT:
                    left_out.append(name)
                    continue
                total += len(text)
                archive.writestr(name, scrub(text, home))
                written += 1
            if left_out:
                archive.writestr("left-out.txt", "Не вошли в архив (слишком много данных или нет доступа):\n"
                                 + "\n".join(left_out) + "\n")
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return written, left_out


def clipboard_text(report, root=None, home=None, limit=CLIPBOARD_LIMIT):
    """The report and the logs as one text: the journal first, then run logs, newest first, up to `limit`."""
    parts = [report.rstrip("\n"), ""]
    used = sum(len(part) for part in parts)
    files = log_files(root)
    # The journal leads: it tells what happened; run logs follow from the newest.
    files.sort(key=lambda item: not item[0].startswith("log/"))
    left_out = []
    for name, path in files:
        text = _read(path, min(MAX_FILE, limit))
        if text is None:
            left_out.append(name)
            continue
        block = f"===== {name} =====\n{text.rstrip(chr(10))}\n"
        if used + len(block) > limit:
            left_out.append(name)
            continue
        parts.append(block)
        used += len(block)
    if left_out:
        parts.append("===== не поместились =====\n" + "\n".join(left_out)
                     + "\nВсе логи целиком: «Журнал» → «Сохранить логи в файл…».\n")
    return scrub("\n".join(parts), home)


def default_name():
    return time.strftime("hh-logs-%Y%m%d-%H%M%S.zip")


# «Копировать все логи» keeps its ZIP files here; older ones beyond this count are removed.
KEEP_COPIES = 3


def copy_zip(report, root=None, home=None):
    """A fresh ZIP of every log under <state>/reports for the clipboard; returns its path."""
    folder = Path(root or state_dir()) / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / default_name()
    write_zip(target, report, root=root, home=home)
    copies = sorted((path for path in folder.glob("hh-logs-*.zip") if path.is_file()),
                    key=lambda path: path.name, reverse=True)
    for old in copies[KEEP_COPIES:]:
        old.unlink(missing_ok=True)
    return target


def copy_file_to_clipboard(path, run=None):
    """Put the file itself on the Windows clipboard: Ctrl+V attaches it in a messenger or a mail.

    PowerShell's Set-Clipboard does the file-list format; the path travels in the environment,
    never in the command text. Raises OSError or subprocess.SubprocessError on failure.
    """
    import os
    import subprocess
    run = run or subprocess.run
    run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "Set-Clipboard -LiteralPath $env:HH_LOG_ZIP"],
        env={**os.environ, "HH_LOG_ZIP": str(path)}, check=True, timeout=30,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=0x08000000)  # CREATE_NO_WINDOW
