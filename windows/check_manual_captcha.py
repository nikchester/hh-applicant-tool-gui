"""Offline tests of the original HH CAPTCHA handler and every guarded entry point."""

from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import requests
from hh_applicant_tool.tool import HHApplicantTool

from auto_send import AutoGate
from check_send import LETTER, RESUME, VACANCY, negotiation
from check_reply_chats import CHAT, TEXT, request as chat_request
from manual_captcha import ManualCaptcha, captcha_url
from reply_chats import ChatGate
from send import ApprovalGate
import list_resumes
import preview
import preview_hh


URL = "https://hh.ru/account/captcha?state=test"


def response(status=200, data=None, request=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(data or {}).encode()
    result.request = request
    result.url = request.url if request is not None else "https://api.hh.ru/me"
    return result


def challenge():
    return response(403, {"errors": [{"value": "captcha_required", "captcha_url": URL}]})


def manual_tool(**kwargs):
    return SimpleNamespace(captcha_manual=False, _can_use_tkinter=Mock(return_value=True),
                           handle_captcha=Mock(**(kwargs or {"return_value": True})))


@contextmanager
def profile():
    with tempfile.TemporaryDirectory() as temp:
        home = Path(temp)
        config = home / ".config/hh-applicant-tool-cli"
        config.mkdir(parents=True)
        shutil.copyfile(Path(__file__).with_name("config.toml"), config / "config.toml")
        with patch("pathlib.Path.home", return_value=home), \
             patch.dict(os.environ, {"CONFIG_DIR": str(config), "HH_PROFILE_ID": "."}):
            yield config


class CaptchaChecks(unittest.TestCase):
    def test_only_explicit_valid_hh_challenge_is_accepted(self):
        self.assertEqual(captcha_url(challenge()), URL)
        self.assertEqual(captcha_url(response(403, {"errors": [
            {"value": "captcha_required", "captcha_url": "https://spb.hh.ru/account/captcha"}]})),
            "https://spb.hh.ru/account/captcha")
        for url in ("https://evil.test/account/captcha", "https://hh.ru.evil.test/account/captcha",
                    "https://api.hh.ru/account/captcha", "https://hh.ru:444/account/captcha",
                    "https://user@hh.ru/account/captcha", "http://hh.ru/account/captcha",
                    "https://hh.ru/account/change-email", "https://hh.ru/account/captcha#x"):
            self.assertIsNone(captcha_url(response(403, {"errors": [
                {"value": "captcha_required", "captcha_url": url}]})))
        for status in (200, 429, 500):
            result = challenge()
            result.status_code = status
            self.assertIsNone(captcha_url(result))
        self.assertIsNone(captcha_url(response(403, {"errors": "not a list"})))

    def test_common_handler_retries_identical_request_once(self):
        captcha = ManualCaptcha()
        tool = manual_tool()
        sender = Mock(side_effect=[challenge(), response(200)])
        request = requests.Request("GET", "https://api.hh.ru/resumes/mine").prepare()
        with patch("manual_captcha.HHApplicantTool", return_value=tool), redirect_stderr(StringIO()):
            self.assertEqual(captcha.send(sender, None, request, timeout=15).status_code, 200)
        self.assertEqual(sender.call_count, 2)
        self.assertEqual(sender.call_args_list[0], sender.call_args_list[1])
        tool.handle_captcha.assert_called_once_with(URL)
        self.assertTrue(tool.captcha_manual)
        self.assertFalse(captcha.active)

    def test_repeated_challenge_cancel_wrong_answer_and_network_stop(self):
        for handler in ({"return_value": False}, {"side_effect": KeyboardInterrupt},
                        {"side_effect": RuntimeError("failed")}):
            sender = Mock(return_value=challenge())
            captcha = ManualCaptcha()
            with patch("manual_captcha.HHApplicantTool", return_value=manual_tool(**handler)), \
                 redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                captcha.send(sender, None, requests.Request("GET", "https://api.hh.ru/me").prepare())
            sender.assert_called_once()
            self.assertFalse(captcha.active)
        sender = Mock(return_value=challenge())
        tool = manual_tool()
        with patch("manual_captcha.HHApplicantTool", return_value=tool), redirect_stderr(StringIO()), \
             self.assertRaisesRegex(SystemExit, "Repeated"):
            ManualCaptcha().send(sender, None, requests.Request("GET", "https://api.hh.ru/me").prepare())
        self.assertEqual(sender.call_count, 2)
        tool.handle_captcha.assert_called_once()
        sender = Mock(side_effect=requests.Timeout())
        with self.assertRaises(requests.Timeout):
            ManualCaptcha().send(sender, None, requests.Request("GET", "https://api.hh.ru/me").prepare())
        sender.assert_called_once()

    def test_prompt_hook_alerts_before_both_captcha_kinds_and_cannot_break_them(self):
        events = []
        tool = Mock()
        tool._can_use_tkinter.return_value = True
        tool.handle_captcha.side_effect = lambda _url: events.append("window") or True
        tool.solve_captcha.side_effect = lambda *a, **k: events.append("window") or SimpleNamespace(
            text="abc", key="KEY")
        with patch.object(ManualCaptcha, "on_prompt", lambda: events.append("alert")), \
                redirect_stderr(StringIO()):
            ManualCaptcha().solve("https://hh.ru/account/captcha?state=x", tool)
            ManualCaptcha().solve_state("STATE", tool)
        self.assertEqual(events, ["alert", "window", "alert", "window"])
        broken = Mock(side_effect=RuntimeError("closed pipe"))
        with patch.object(ManualCaptcha, "on_prompt", broken), redirect_stderr(StringIO()):
            self.assertEqual(ManualCaptcha().solve_state("STATE", tool)["captchaText"], "abc")
        broken.assert_called_once()
        self.assertIsNone(ManualCaptcha.on_prompt)

    def test_non_hh_response_cannot_start_captcha(self):
        with patch("manual_captcha.HHApplicantTool") as tool:
            result = ManualCaptcha().send(Mock(return_value=challenge()), None,
                requests.Request("POST", "http://127.0.0.1:8000/v1/chat/completions").prepare())
        self.assertEqual(result.status_code, 403)
        tool.assert_not_called()

    def test_captcha_permissions_are_narrow_and_temporary(self):
        captcha = ManualCaptcha()
        good = [("GET", URL), ("GET", "https://hh.ru/captcha/picture?key=test"),
                ("POST", "https://hh.ru/captcha?lang=RU"), ("POST", URL)]
        for method, url in good:
            self.assertFalse(captcha.allowed_request(requests.Request(method, url).prepare()))
        captcha.active = True
        for method, url in good:
            self.assertTrue(captcha.allowed_request(requests.Request(method, url).prepare()))
        for method, url in [("POST", "https://hh.ru/account/change-email"),
                            ("POST", "https://api.hh.ru/captcha"),
                            ("POST", "https://api.hh.ru/negotiations"),
                            ("DELETE", URL), ("POST", "https://evil.test/captcha")]:
            self.assertFalse(captcha.allowed_request(requests.Request(method, url).prepare()))

    def test_original_solver_fetches_image_calls_gui_and_posts_human_answer(self):
        # Exercise the installed package, not a mock of handle_captcha/solve_captcha.
        tool = HHApplicantTool.__new__(HHApplicantTool)
        tool.config = {"openai": {"default": {}}}  # AI configured, but must never recognize the image.
        tool.use_ai = True
        tool.base_url = "https://hh.ru"
        tool.captcha_lang = "RU"
        tool.webview_session = requests.Session()
        tool.webview_session.trust_env = False
        tool.webview_session.cookies.set("_xsrf", "test-xsrf")
        tool._can_use_tkinter = Mock(return_value=True)
        tool.prompt_captcha_tkinter = Mock(return_value="human-text")
        tool.captcha_ai = Mock()
        captcha = ManualCaptcha()
        sent = []

        def fake_send(_session, request, **kwargs):
            self.assertTrue(captcha.allowed_request(request))
            sent.append(request)
            path = urlsplit(request.url).path
            if request.method == "GET" and path == "/account/captcha":
                result = response(request=request)
                # The original handler follows HH's regional CAPTCHA redirects.
                result.url = result.url.replace("hh.ru", "spb.hh.ru")
                return result
            if path == "/captcha":
                return response(data={"key": "image-key"}, request=request)
            if path == "/captcha/picture":
                result = response(request=request)
                result._content = b"test-image-bytes"
                return result
            if request.method == "POST" and path == "/account/captcha":
                params = parse_qs(urlsplit(request.url).query)
                self.assertEqual(params["captchaText"], ["human-text"])
                self.assertEqual(params["captchaState"], ["test"])
                self.assertEqual(params["captchaKey"], ["image-key"])
                self.assertFalse(kwargs["allow_redirects"])
                return response(302, request=request)
            self.fail("Unexpected request")

        with patch.object(requests.sessions.Session, "send", fake_send), redirect_stderr(StringIO()):
            captcha.solve(URL, tool)
        tool.prompt_captcha_tkinter.assert_called_once_with(b"test-image-bytes")
        tool.captcha_ai.recognize_text.assert_not_called()
        self.assertTrue(tool.captcha_manual)
        self.assertEqual(len(sent), 4)
        self.assertEqual(urlsplit(sent[-1].url).hostname, "spb.hh.ru")
        self.assertFalse(captcha.active)

    def test_missing_gui_never_uses_ai_or_sends_an_answer(self):
        tool = manual_tool()
        tool._can_use_tkinter.return_value = False
        with self.assertRaisesRegex(SystemExit, "Tkinter"):
            ManualCaptcha().solve(URL, tool)
        tool.handle_captcha.assert_not_called()
        self.assertFalse(tool.captcha_manual)

    def test_preview_captcha_on_read_keeps_application_posts_blocked(self):
        sender = Mock(side_effect=[challenge(), response()])
        tool = manual_tool()

        def main(_argv):
            with requests.Session() as session:
                session.get("https://api.hh.ru/resumes/mine")
                with self.assertRaisesRegex(SystemExit, "blocked"):
                    session.post("https://api.hh.ru/negotiations")
            return 0

        with profile() as config, patch.object(requests.sessions.Session, "send", sender), \
             patch("manual_captcha.HHApplicantTool", return_value=tool), \
             patch("preview_hh.selected_resume_context", return_value=nullcontext()), \
             patch("preview_hh.main", side_effect=main), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(preview_hh.run(preview.arguments(str(config), RESUME, "Linux")), 0)
        tool.handle_captcha.assert_called_once()
        self.assertEqual(sender.call_count, 2)

    def test_listing_captcha_keeps_stdout_valid_json(self):
        sender = Mock(side_effect=[challenge(), response(data={"items": []})])
        solver = manual_tool()

        def resumes(_tool):
            requests.Session().get("https://api.hh.ru/resumes/mine")
            return [{"id": RESUME, "title": "Linux", "status": {"id": "published"}}]

        output = StringIO()
        with profile(), patch.object(requests.sessions.Session, "send", sender), \
             patch("manual_captcha.HHApplicantTool", return_value=solver), \
             patch.object(HHApplicantTool, "get_resumes", resumes), \
             redirect_stdout(output), redirect_stderr(StringIO()):
            self.assertEqual(list_resumes.run(), 0)
        self.assertEqual(json.loads(output.getvalue()), [{"id": RESUME, "title": "Linux"}])
        solver.handle_captcha.assert_called_once()

    def test_manual_and_auto_reads_open_window_before_operation_is_bound(self):
        for gate_class, kwargs in ((ApprovalGate, {}), (AutoGate, {"max_responses": 2})):
            with self.subTest(gate=gate_class.__name__):
                sender = Mock(side_effect=[challenge(), response()])
                gate = gate_class(RESUME, sender, **kwargs)
                tool = manual_tool()
                with patch("manual_captcha.HHApplicantTool", return_value=tool), redirect_stderr(StringIO()):
                    self.assertEqual(gate.send(None,
                        requests.Request("GET", "https://api.hh.ru/me").prepare()).status_code, 200)
                tool.handle_captcha.assert_called_once()
                self.assertEqual(gate.counts["http_success"], 0)

    def test_auto_application_resumes_after_human_captcha_with_same_payload(self):
        sender = Mock(side_effect=[challenge(), response(201)])
        gate = AutoGate(RESUME, sender, max_responses=1)
        gate.current, gate.letter = VACANCY.copy(), LETTER
        tool = manual_tool()
        gate.operation = SimpleNamespace(tool=tool)
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(gate.send(None, negotiation()).status_code, 201)
        tool.handle_captcha.assert_called_once()
        self.assertEqual(sender.call_args_list[0], sender.call_args_list[1])
        self.assertEqual(gate.counts["attempts"], 2)
        self.assertEqual(gate.counts["captcha_rejections"], 1)
        self.assertEqual(gate.counts["http_success"], 1)

    def test_chat_reads_resume_but_approved_post_never_replays_after_captcha(self):
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                sender = Mock(side_effect=[challenge(), response()])
                gate = ChatGate(sender)
                gate.current = CHAT
                gate.operation = SimpleNamespace(chat_url="https://chatik.hh.ru")
                gate.pending = (CHAT.chat_id, TEXT) if method == "POST" else None
                request = chat_request() if method == "POST" else requests.Request(
                    "GET", "https://chatik.hh.ru/chatik/api/chats").prepare()
                tool = manual_tool()
                with patch("manual_captcha.HHApplicantTool", return_value=tool), redirect_stderr(StringIO()), \
                     redirect_stdout(StringIO()):
                    if method == "POST":
                        with patch.object(gate.captcha, "solve") as solve, \
                             self.assertRaisesRegex(SystemExit, "HTTP 403"):
                            gate.send(None, request)
                        solve.assert_not_called()
                    else:
                        self.assertEqual(gate.send(None, request).status_code, 200)
                if method == "POST":
                    tool.handle_captcha.assert_not_called()
                    sender.assert_called_once()
                    self.assertIsNone(gate.pending)
                    with self.assertRaisesRegex(SystemExit, "without approval"):
                        gate.send(None, request)
                    sender.assert_called_once()
                    self.assertEqual(gate.counts["attempts"], 1)
                else:
                    tool.handle_captcha.assert_called_once()
                    self.assertEqual(sender.call_count, 2)
                    self.assertEqual(sender.call_args_list[0], sender.call_args_list[1])
                self.assertEqual(gate.counts["http_success"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
