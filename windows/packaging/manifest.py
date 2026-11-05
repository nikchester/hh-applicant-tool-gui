"""Closed source allowlist. Never walk the repository to assemble a release."""

APP_NAME = "PocketNodeHH"
APP_VERSION = "0.1.0"
APP_MODULES = (
    "gui", "gui_chatmock_auth", "gui_controller", "gui_owned_process", "gui_profile", "gui_protocol",
    "gui_recovery", "gui_runtime", "gui_steps", "gui_worker",
    "auto_send", "chatmock_server", "hh_network_diagnostics", "list_resumes",
    "manual_captcha", "preview", "preview_hh", "reply_chats", "resume_context",
    "send", "send_all", "vacancy_level", "vacancy_salary",
)
ENTRY_FILES = ("entrypoint.py", "smoke.py", "manifest.py")
APPLICATION_PINS = {"hh-applicant-tool": "2.3.7", "chatmock": "1.40"}
BUILD_PINS = {
    "pyinstaller": "6.16.0", "pyinstaller-hooks-contrib": "2025.9",
    "packaging": "25.0", "altgraph": "0.17.4", "pefile": "2023.2.7",
    "pywin32-ctypes": "0.2.3", "setuptools": "80.9.0",
}
PRIVATE_NAMES = frozenset({
    "config.toml", "resume-search.json", "searches.json", "auth.json", "token.json",
    "cookies.txt", "candidate-facts.json", "direct_url.json", "mutation-state.json",
})
