"""List published HH resume titles and IDs through the dedicated read-only profile."""

import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.parse import urlsplit

import requests
from hh_applicant_tool.tool import HHApplicantTool
from manual_captcha import ManualCaptcha, permits_token_refresh, token_refresh_request


def allowed_request(request):
    if token_refresh_request(request.method, request.url, request.body):
        return True
    url = urlsplit(request.url)
    if url.username or url.password or url.fragment or url.query or url.port is not None:
        return False
    if url.scheme != "https" or url.hostname != "api.hh.ru":
        return False
    if request.method == "GET" and url.path == "/resumes/mine":
        return True
    return False


def published_resumes(resumes):
    return [
        {"id": item["id"], "title": item["title"]}
        for item in resumes
        if item.get("status", {}).get("id") == "published"
        and isinstance(item.get("id"), str) and item["id"]
        and isinstance(item.get("title"), str) and item["title"]
    ]


def run(*, profile=None, observer=None):
    from gui_profile import expected_profile
    expected = expected_profile(profile)
    if os.environ.get("CONFIG_DIR") != str(expected) or os.environ.get("HH_PROFILE_ID") != ".":
        raise SystemExit("Dedicated HH profile is required")
    send = requests.sessions.Session.send
    captcha = ManualCaptcha()

    def guarded_send(session, request, **kwargs):
        if not (allowed_request(request) or captcha.allowed_request(request)):
            raise SystemExit("Resume listing blocked an unexpected network request")
        try:
            response = captcha.send(send, session, request, **kwargs)
        except requests.RequestException:
            raise SystemExit("Resume listing stopped after a network failure") from None
        if (response.status_code >= 400 and not captcha.rejected_answer(request, response)
                and not permits_token_refresh(request, response)):
            raise SystemExit(f"Resume listing stopped after HTTP {response.status_code}")
        return response

    with patch.object(requests.sessions.Session, "send", guarded_send):
        tool = HHApplicantTool()
        tool.captcha_handling = False
        resumes = published_resumes(tool.get_resumes())
    print(json.dumps(resumes, ensure_ascii=False))
    if observer is not None:
        observer("resumes", resumes)
    return 0


if __name__ == "__main__":
    sys.exit(run())
