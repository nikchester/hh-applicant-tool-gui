"""Fail CI if credential files or credential-shaped config enter the repository."""

from pathlib import Path
import re
import subprocess
import tomllib


def check():
    tracked = [Path(p.decode()) for p in subprocess.check_output(
        ["git", "ls-files", "-z", "--cached"]
    ).split(b"\0") if p]
    forbidden = {"auth.json", "token.json", "cookies.txt", "candidate-facts.json"}
    bad = [str(p) for p in tracked if p.name in forbidden or any(
        part in {"venvs", "__pycache__"} for part in p.parts
    ) or p.suffix in {".log", ".pid"}]
    if bad:
        raise SystemExit("Credential/runtime paths tracked: " + ", ".join(bad))

    config = Path("windows/config.toml")
    if config not in tracked:
        raise SystemExit("Reviewed runtime config is missing from Git")
    text = config.read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    if re.search(r"(?im)^\s*(?:api_key|access_token|refresh_token|id_token|password|secret|cookie|authorization)\s*=", text):
        raise SystemExit("Credential-shaped key in config.toml")
    if re.search(r"(?i)(?:gh[opsu]_[A-Za-z0-9]{20}|sk-[A-Za-z0-9]{20}|Bearer\s+[A-Za-z0-9._-]{20})", text):
        raise SystemExit("Credential-shaped value in config.toml")
    if parsed["openai"]["default"]["base_url"] != "http://127.0.0.1:8000/v1":
        raise SystemExit("Gateway must be loopback-only")
    print(f"Repository check passed ({len(tracked)} tracked files; no runtime credentials).")


if __name__ == "__main__":
    check()
