"""Shared HH authentication handling inside the Windows network guards."""

import sys
from urllib.parse import parse_qs, urlsplit

import requests

from hh_applicant_tool.tool import HHApplicantTool


def hh_url(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme == "https" and parsed.hostname
                and (parsed.hostname == "hh.ru" or parsed.hostname.endswith(".hh.ru"))
                and parsed.port is None and not parsed.username and not parsed.password
                and not parsed.fragment):
            return parsed
    except ValueError:
        pass
    return None


def captcha_url(response):
    if response.status_code != 403:
        return None
    try:
        data = response.json()
        errors = data.get("errors") if isinstance(data, dict) else None
        if isinstance(errors, list):
            for error in errors:
                if isinstance(error, dict) and error.get("value") == "captcha_required":
                    candidate = error.get("captcha_url")
                    url = hh_url(candidate) if isinstance(candidate, str) else None
                    if url and url.hostname != "api.hh.ru" and url.path == "/account/captcha":
                        return candidate
    except ValueError:
        pass
    return None


def form_captcha_state(response):
    """HH site forms answer 403 {"hhcaptcha": {"captchaState": ...}} instead of a captcha URL."""
    if response.status_code != 403:
        return None
    try:
        data = response.json() if callable(getattr(response, "json", None)) else None
    except ValueError:
        return None
    captcha = data.get("hhcaptcha") if isinstance(data, dict) else None
    state = captcha.get("captchaState") if isinstance(captcha, dict) else None
    if (isinstance(state, str) and 0 < len(state) <= 4096
            and all(c.isascii() and c.isprintable() and not c.isspace() for c in state)):
        return state
    return None


def form_captcha_error(response):
    """HH's own verdict on the previous captcha text of a form: True wrong, False accepted, None unknown."""
    try:
        data = response.json() if callable(getattr(response, "json", None)) else None
    except ValueError:
        return None
    captcha = data.get("hhcaptcha") if isinstance(data, dict) else None
    error = captcha.get("captchaError") if isinstance(captcha, dict) else None
    return error if isinstance(error, bool) else None


def token_refresh_request(method, url, body=None):
    """Only the two HH OAuth endpoints and the refresh-token grant."""
    if method != "POST" or url not in ("https://api.hh.ru/token", "https://hh.ru/oauth/token"):
        return False
    if isinstance(body, bytes):
        body = body.decode("ascii", errors="replace")
    return parse_qs(body or "").get("grant_type") == ["refresh_token"]


def permits_token_refresh(request, response):
    """Let ApiClient refresh once, only after an explicit expired-token GET.

    Never pass through a failed mutation: its outcome must not be replayed.
    ApiClient owns the refresh limit and token persistence, not this guard.
    """
    url = hh_url(request.url)
    if (request.method != "GET" or not url or url.hostname != "api.hh.ru"
            or response.status_code != 403):
        return False
    try:
        data = response.json()
        errors = data.get("errors") if isinstance(data, dict) else None
        return (isinstance(errors, list) and len(errors) == 1
                and isinstance(errors[0], dict)
                and errors[0].get("type") == "oauth"
                and errors[0].get("value") == "token_expired")
    except (ValueError, TypeError, AttributeError):
        return False


class ManualCaptcha:
    # Optional callback without arguments: the GUI worker uses it to alert the user.
    on_prompt = None

    def __init__(self):
        self.active = False

    def _prompt(self):
        print("HH запросил капчу. Введи текст с картинки в окне «Капча».", file=sys.stderr, flush=True)
        callback = type(self).on_prompt
        if callback is not None:
            try:
                callback()
            except Exception:  # An alert must never block the captcha itself.
                pass

    def allowed_request(self, request):
        """Only the CAPTCHA page, image, and forms, only while solving a challenge."""
        url = hh_url(request.url)
        if not self.active or not url or url.hostname == "api.hh.ru":
            return False
        return (
            request.method == "GET" and url.path in ("/account/captcha", "/captcha/picture")
            or request.method == "POST" and url.path in ("/captcha", "/account/captcha")
        )

    def rejected_answer(self, request, response):
        return (self.allowed_request(request) and request.method == "POST"
                and urlsplit(request.url).path == "/account/captcha" and response.status_code == 403)

    def solve(self, url, tool=None):
        if self.active:
            raise SystemExit("Recursive HH captcha challenge; stopped")
        tool = tool if tool is not None else HHApplicantTool()
        if not tool._can_use_tkinter():
            raise SystemExit("Manual HH captcha requires an available Tkinter window")
        tool.captcha_manual = True  # Reuse the original window, never the AI recognizer.
        # Resume listing's stdout is a JSON protocol consumed by PowerShell.
        self._prompt()
        self.active = True
        # HH 2.3.7 calls session.get(url, params) positionally, which requests'
        # Session.get(url, **kwargs) rejects. Adapt only this session, only now.
        session = getattr(tool, "webview_session", None)
        adapted = isinstance(session, requests.Session)
        if adapted:
            original_get = session.__dict__.get("get")
            session.get = lambda target, params=None, **kwargs: type(session).get(
                session, target, params=params, **kwargs)
        try:
            accepted = tool.handle_captcha(url)
        except (Exception, KeyboardInterrupt):
            raise SystemExit("Manual HH captcha failed or cancelled; check HH before retrying") from None
        finally:
            self.active = False
            if adapted:
                if original_get is None:
                    del session.get
                else:
                    session.get = original_get
        if not accepted:
            raise SystemExit("Captcha was not accepted; check HH before retrying")

    def solve_state(self, state, tool, referer_url=None):
        """Show the in-form HH captcha; the caller resends its form with the solution."""
        if self.active:
            raise SystemExit("Recursive HH captcha challenge; stopped")
        if not tool._can_use_tkinter():
            raise SystemExit("Manual HH captcha requires an available Tkinter window")
        tool.captcha_manual = True  # Human input only, never the AI recognizer.
        self._prompt()
        self.active = True
        try:
            solution = tool.solve_captcha(state, referer_url=referer_url, session=tool.session)
        except (Exception, KeyboardInterrupt):
            raise SystemExit("Manual HH captcha failed or cancelled; check HH before retrying") from None
        finally:
            self.active = False
        text, key = getattr(solution, "text", None), getattr(solution, "key", None)
        if not (isinstance(text, str) and text.strip() and isinstance(key, str) and key):
            raise SystemExit("Captcha was not entered; check HH before retrying")
        return {"captchaText": text.strip(), "captchaKey": key, "captchaState": state}

    def send(self, sender, session, request, **kwargs):
        """One identical retry after an explicit HH rejection and human solution only."""
        response = sender(session, request, **kwargs)
        if not self.active and hh_url(request.url) and (url := captcha_url(response)):
            self.solve(url)
            response = sender(session, request, **kwargs)
            if captcha_url(response):
                raise SystemExit("Repeated HH captcha; stopped without another retry")
        return response
