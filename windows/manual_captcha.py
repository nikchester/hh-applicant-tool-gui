"""Shared HH authentication handling inside the Windows network guards."""

import sys
from urllib.parse import parse_qs, urlsplit

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
    def __init__(self):
        self.active = False

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
        print("HH запросил капчу. Введи текст с картинки в окне «Капча».", file=sys.stderr, flush=True)
        self.active = True
        try:
            accepted = tool.handle_captcha(url)
        except (Exception, KeyboardInterrupt):
            raise SystemExit("Manual HH captcha failed or cancelled; check HH before retrying") from None
        finally:
            self.active = False
        if not accepted:
            raise SystemExit("Captcha was not accepted; check HH before retrying")

    def send(self, sender, session, request, **kwargs):
        """One identical retry after an explicit HH rejection and human solution only."""
        response = sender(session, request, **kwargs)
        if not self.active and hh_url(request.url) and (url := captcha_url(response)):
            self.solve(url)
            response = sender(session, request, **kwargs)
            if captcha_url(response):
                raise SystemExit("Repeated HH captcha; stopped without another retry")
        return response
