"""Run HH authorization and explain a rejected OTP confirmation safely.

Requests sent to HH are unchanged. Only the parsed /account/login/by_code
response is recorded; when the OTP confirmation fails a bounded, redacted one-line summary
goes to stderr. Personal data, codes, tokens and URLs are never printed.
"""

import re
import sys
from unittest.mock import patch
from urllib.parse import urlsplit

import requests
from hh_applicant_tool.main import main
from hh_applicant_tool.operations import authorize as hh_authorize

BY_CODE_PATH = "/account/login/by_code"
MAX_DEPTH = 3
MAX_ITEMS = 20
REDACTED = "<redacted>"
# Every printed key name and string value must pass printable().
PRINTABLE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,47}")
DIGIT_RUN = re.compile(r"[0-9]{3}")
SAFE_NAME = re.compile(r"(error|key|code|reason|status|type|success|scenario)", re.I)
# A denied fragment wins over SAFE_NAME, e.g. "code" itself may echo the OTP.
DENIED_NAME = re.compile(
    r"(user|mail|phone|token|backurl|url|cookie|password|secret|login|otp|"
    r"session|hash|captcha|value|csrf|xsrf|auth_?key|signature)", re.I)
DENIED_EXACT = {"code", "username", "autologinemail", "backurl"}


def normalized(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def windows4(digits):
    return {digits[i:i + 4] for i in range(len(digits) - 3)}


def secret_fragments(username="", code=""):
    """Normalized fragments that must never appear in printed text."""
    username, code = str(username or ""), str(code or "")
    fragments = {normalized(username), normalized(username.split("@", 1)[0]),
                 re.sub(r"\D", "", username), normalized(code)}
    fragments |= windows4(re.sub(r"\D", "", username)) | windows4(re.sub(r"\D", "", code))
    # Shorter fragments would redact almost everything without adding safety.
    return tuple(sorted(f for f in fragments if len(f) >= 2))


def printable(text, fragments):
    if not isinstance(text, str) or not PRINTABLE.fullmatch(text) or DIGIT_RUN.search(text):
        return False
    flat = normalized(text)
    return not any(fragment in flat for fragment in fragments)


class ByCodeRecorder:
    def __init__(self):
        self.response = None
        self.secrets = ()

    def reset(self):
        self.response, self.secrets = None, ()

    def record(self, url, kwargs, response):
        try:
            if urlsplit(str(url)).path != BY_CODE_PATH:
                return
        except ValueError:
            return
        data = kwargs.get("data")
        data = data if isinstance(data, dict) else {}
        self.secrets = secret_fragments(data.get("username"), data.get("code"))
        self.response = response


def safe_name(name, secrets):
    return name if printable(name, secrets) else REDACTED


def shape(value, secrets, depth=0):
    """Describe structure only: key names and value types, bounded depth."""
    if isinstance(value, dict):
        if depth >= MAX_DEPTH:
            return "dict"
        items = list(value.items())[:MAX_ITEMS]
        inner = ", ".join(f"{safe_name(k, secrets)}: {shape(v, secrets, depth + 1)}"
                          for k, v in items)
        more = ", ..." if len(value) > MAX_ITEMS else ""
        return "{" + inner + more + "}"
    if isinstance(value, list):
        if not value or depth >= MAX_DEPTH:
            return f"list[{len(value)}]"
        return f"list[{len(value)}]<{shape(value[0], secrets, depth + 1)}>"
    if value is None:
        return "null"
    return type(value).__name__


def safe_scalar(value, secrets):
    if isinstance(value, bool) or value is None:
        return repr(value)
    if isinstance(value, int):
        return repr(value) if abs(value) <= 999 else None
    if printable(value, secrets):
        return repr(value)
    return None


def key_allows_traversal(name, secrets):
    return (printable(name, secrets) and name.lower() not in DENIED_EXACT
            and not DENIED_NAME.search(name))


def safe_values(value, secrets, path="", depth=0, out=None):
    """Collect short scalar values under error-like keys only."""
    out = [] if out is None else out
    if depth > MAX_DEPTH or len(out) >= MAX_ITEMS:
        return out
    if isinstance(value, dict):
        for name, item in list(value.items())[:MAX_ITEMS]:
            if not key_allows_traversal(name, secrets):
                continue
            here = f"{path}.{name}" if path else name
            if isinstance(item, (dict, list)):
                safe_values(item, secrets, here, depth + 1, out)
            elif SAFE_NAME.search(name):
                text = safe_scalar(item, secrets)
                if text is not None:
                    out.append(f"{here}={text}")
    elif isinstance(value, list):
        for index, item in enumerate(value[:MAX_ITEMS]):
            if isinstance(item, (dict, list)):
                safe_values(item, secrets, f"{path}[{index}]", depth + 1, out)
    return out


def summary(res, secrets=()):
    """One line; secrets is the tuple from secret_fragments()."""
    if not isinstance(res, dict):
        return f"HH by_code diagnostics: response={shape(res, secrets)}"
    parts = [f"keys=[{', '.join(safe_name(k, secrets) for k in list(res)[:MAX_ITEMS])}]"]
    parts.append(f"verification={shape(res.get('verification'), secrets)}")
    values = safe_values(res, secrets)
    parts.append(f"values=[{', '.join(values)}]")
    scenarios = res.get("availableAuthScenarios")
    if isinstance(scenarios, list):
        texts = [repr(s) if printable(s, secrets) else REDACTED
                 for s in scenarios[:MAX_ITEMS]]
        more = ", ..." if len(scenarios) > MAX_ITEMS else ""
        parts.append(f"availableAuthScenarios=[{', '.join(texts)}{more}]")
    elif scenarios is not None:
        parts.append(f"availableAuthScenarios={shape(scenarios, secrets)}")
    return "HH by_code diagnostics: " + " ".join(parts)


def report(recorder):
    response = recorder.response
    if response is None:
        return
    try:
        res = response.json()
    except ValueError:
        print("HH by_code diagnostics: response is not JSON", file=sys.stderr)
        return
    print(summary(res, recorder.secrets), file=sys.stderr)


def run(argv):
    recorder = ByCodeRecorder()
    original_post = requests.Session.post
    original_confirm = hh_authorize.Operation._confirm_otp

    def post(self, url, *args, **kwargs):
        response = original_post(self, url, *args, **kwargs)
        recorder.record(url, kwargs, response)
        return response

    def confirm_otp(self, *args, **kwargs):
        recorder.reset()
        try:
            return original_confirm(self, *args, **kwargs)
        # HH 2.3.7 raises AttributeError, not OtpConfirmError, when HH sends
        # "error": null, so report any failure after a recorded response.
        except Exception:
            try:
                report(recorder)
            except Exception:  # Diagnostics must never mask the real error.
                print("HH by_code diagnostics: unavailable", file=sys.stderr)
            raise
        finally:
            recorder.reset()

    with patch.object(requests.Session, "post", post), \
            patch.object(hh_authorize.Operation, "_confirm_otp", confirm_otp):
        return main(argv)


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
