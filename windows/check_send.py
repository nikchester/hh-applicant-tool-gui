"""Offline safety tests: no HH or model network traffic is possible here."""

import os
from contextlib import contextmanager
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import requests
from hh_applicant_tool.ai.openai import OpenAIClient, OpenAIError
from hh_applicant_tool.api import Redirect
from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.operations.apply_vacancies import Operation

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "windows"))
from send import (ApprovalGate, SkippedTest, StopReview, URL_TASK_ANSWER,  # noqa: E402
                  all_resume_arguments, arguments, run, salary_text, validate)
from send_all import apply_round_robin, load_searches  # noqa: E402


RESUME = "9749b651ff056b6c970039ed1f5a63765a5178"
VACANCY = {"id": "123", "name": "Linux", "employer": {"name": "Test"},
           "salary": {"from": 100000, "currency": "RUR"}, "relations": []}
LETTER = "Привет! Я работаю с Linux. Созвонимся?"


def negotiation(vacancy_id="123", letter=LETTER, url="https://api.hh.ru/negotiations",
                resume_id=RESUME):
    return requests.Request("POST", url, data={
        "resume_id": resume_id, "vacancy_id": vacancy_id, "message": letter,
    }).prepare()


def test_response(vacancy_id="123", letter=LETTER, resume_id=RESUME,
                  url="https://hh.ru/applicant/vacancy_response/popup", **extra):
    return requests.Request("POST", url, data={
        "_xsrf": "x", "vacancy_id": vacancy_id, "resume_hash": resume_id,
        "withoutTest": "no", "letter": letter, "task_1_text": "Да, Ansible и Docker.",
        "task_2": "7", **extra,
    }).prepare()


class SendChecks(unittest.TestCase):
    def setUp(self):
        self.sender = Mock()
        self.sender.return_value = SimpleNamespace(status_code=200)
        self.gate = ApprovalGate(RESUME, self.sender, approve=lambda _: "yes")
        self.gate.current = VACANCY.copy()
        self.gate.letter = LETTER

    def test_fixed_arguments_and_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            profile = home / ".config/pocketnode-hh"
            profile.mkdir(parents=True)
            shutil.copyfile(ROOT / "windows/config.toml", profile / "config.toml")
            search = 'DevOps OR "Linux administrator"'
            argv = arguments(str(profile), RESUME, search)
            self.assertEqual(argv[-1], "--search=" + search)
            with patch.dict(os.environ, {"CONFIG_DIR": str(profile), "HH_PROFILE_ID": "."}), \
                 patch("send.Path.home", return_value=home), \
                 patch("requests.sessions.Session.send", side_effect=AssertionError("No network")):
                parsed = validate(argv)
                self.assertIs(parsed.dry_run, False)
                self.assertIsNone(parsed.max_responses)
                self.assertEqual(parsed.per_page, 20)
                self.assertEqual(parsed.total_pages, 100)
                self.assertNotIn('--max-responses', argv)
                for forbidden in ("--dry-run", "--send-email", "--ai-filter"):
                    with self.subTest(forbidden=forbidden):
                        with self.assertRaises(SystemExit):
                            validate(argv + [forbidden])
                changed = argv.copy()
                changed[changed.index("--no-handle-captcha")] = "--handle-captcha"
                with self.assertRaises(SystemExit):
                    validate(changed)

    def test_send_requires_exact_approval(self):
        for answer in ("", "да", "maybe"):
            self.gate.approve = lambda _, value=answer: value
            with self.subTest(answer=answer), redirect_stdout(StringIO()), \
                 self.assertRaisesRegex(SystemExit, "No approval"):
                self.gate.send(None, negotiation())
        self.sender.assert_not_called()
        self.assertEqual(self.gate.counts["attempts"], 0)

    def test_no_skips_without_post_and_stop_exits(self):
        for answer in ("no", "n", " N "):
            self.gate.approve = lambda _, value=answer: value
            self.gate.letter = LETTER
            with redirect_stdout(StringIO()), self.assertRaises(Redirect):
                self.gate.send(None, negotiation())
        self.assertEqual(self.gate.counts["skipped"], 3)
        self.assertEqual(self.gate.counts["attempts"], 0)
        self.sender.assert_not_called()
        self.gate.approve = lambda _: "STOP "
        self.gate.letter = LETTER
        with redirect_stdout(StringIO()), self.assertRaises(StopReview):
            self.gate.send(None, negotiation())
        self.sender.assert_not_called()

    def test_payload_and_context_must_match(self):
        for request in (negotiation("different"), negotiation(letter="different"),
                        negotiation(url="https://api.hh.ru/negotiations?x=1"),
                        requests.Request("POST", "https://api.hh.ru/vacancies/blacklisted/123").prepare()):
            with self.subTest(url=request.url, body=request.body):
                with self.assertRaises(SystemExit):
                    self.gate.send(None, request)
        self.sender.assert_not_called()

    def test_test_vacancy_sends_letter_and_answers_once(self):
        self.gate.current = VACANCY | {"has_test": True}
        self.sender.return_value = Mock(status_code=200, json=Mock(return_value={"success": "true"}))
        self.gate.approve = Mock(return_value="yes")
        output = StringIO()
        with redirect_stdout(output):
            self.gate.send(None, test_response())
        rendered = output.getvalue()
        self.assertIn("ОТКЛИК НА ВАКАНСИЮ С ТЕСТОМ", rendered)
        self.assertIn(LETTER, rendered)
        self.assertIn("task_1_text: Да, Ansible и Docker.", rendered)
        self.sender.assert_called_once()
        self.assertEqual(self.gate.counts["http_success"], 1)
        self.assertIsNone(self.gate.letter)
        with self.assertRaises(SystemExit):
            self.gate.send(None, test_response())
        self.sender.assert_called_once()

    def test_test_payload_and_context_must_match(self):
        bad = (test_response("other"), test_response(letter="other"),
               test_response(resume_id="other"), test_response(withoutTest="yes"),
               test_response(url="https://hh.ru/applicant/vacancy_response/popup?x=1"))
        for request in bad:
            self.gate.current = VACANCY | {"has_test": True}
            self.gate.letter = LETTER
            with self.subTest(body=request.body, url=request.url), redirect_stdout(StringIO()),                  self.assertRaises(SystemExit):
                self.gate.send(None, request)
        for extra in ({}, {"has_test": True, "relations": ["response"]},
                      {"has_test": True, "name": "Senior DevOps"}):
            self.gate.current = VACANCY | extra
            self.gate.letter = LETTER
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                self.gate.send(None, test_response())
        self.sender.assert_not_called()

    def test_test_send_stops_when_hh_rejects(self):
        self.gate.current = VACANCY | {"has_test": True}
        self.sender.return_value = Mock(status_code=200, json=Mock(
            return_value={"success": "false", "error": "negotiations-limit-exceeded"}))
        with redirect_stdout(StringIO()),              self.assertRaisesRegex(SystemExit, "error=negotiations-limit-exceeded"):
            self.gate.send(None, test_response())
        self.assertEqual(self.gate.counts["http_success"], 0)

    def test_test_skip_and_stop(self):
        self.gate.current = VACANCY | {"has_test": True}
        self.gate.approve = lambda _: "no"
        with redirect_stdout(StringIO()), self.assertRaises(SkippedTest):
            self.gate.send(None, test_response())
        self.gate.letter = LETTER
        self.gate.approve = lambda _: "stop"
        with redirect_stdout(StringIO()), self.assertRaises(StopReview):
            self.gate.send(None, test_response())
        self.sender.assert_not_called()

    def test_test_answers_bypass_letter_checks_and_link_answer_is_neutral(self):
        test_ai = object()
        self.gate.current = VACANCY | {"has_test": True}
        self.gate.test_answers = []
        self.gate.operation = SimpleNamespace(test_ai=test_ai, api_client=None)
        self.gate.original_complete = Mock(return_value="7")
        self.assertEqual(self.gate.complete(test_ai, "Вопрос"), "7")
        self.assertEqual(self.gate.letter, LETTER)
        self.assertEqual(self.gate.test_answers, ["7"])
        self.gate.original_complete.return_value = " "
        with self.assertRaisesRegex(SystemExit, "empty test answer"):
            self.gate.complete(test_ai, "Вопрос")

        from hh_applicant_tool.operations import apply_vacancies
        def solver(_operation, **_kwargs):
            return apply_vacancies.rand_text("{шутка|про взлом}")
        self.gate.original_solve_test = solver
        self.assertEqual(self.gate.solve_test(None, vacancy_id="1"), URL_TASK_ANSWER)

    def test_existing_response_or_test_prevents_send(self):
        for extra in ({"relations": ["response"]}, {"has_test": True},
                      {"archived": True}, {"response_url": "https://example.com"}):
            self.gate.current = VACANCY | extra
            with self.assertRaises(SystemExit):
                self.gate.send(None, negotiation())
        self.sender.assert_not_called()

    def test_exact_approval_sends_once_without_reusing_letter(self):
        self.gate.approve = Mock(return_value="yes")
        output = StringIO()
        with redirect_stdout(output):
            self.gate.send(None, negotiation())
        rendered = output.getvalue()
        self.assertIn("Компания: Test", rendered)
        self.assertIn("Вакансия: Linux", rendered)
        self.assertIn("Сопроводительное письмо (будет отправлено дословно):", rendered)
        self.assertIn(LETTER, rendered)
        self.assertIn("Резюме: " + RESUME, rendered)
        self.assertEqual(self.gate.approve.call_args.args,
                         ("Отправить этот отклик? [yes/no/stop]: ",))
        self.sender.assert_called_once()
        self.assertEqual(self.gate.counts["attempts"], 1)
        self.assertEqual(self.gate.counts["http_success"], 1)
        self.assertIsNone(self.gate.letter)
        with self.assertRaisesRegex(SystemExit, "without reviewing"):
            self.gate.send(None, negotiation())
        self.sender.assert_called_once()

    def test_salary_boundaries_are_labeled(self):
        self.assertEqual(salary_text({"from": 80000, "to": None, "currency": "RUR", "gross": False}),
                         "от 80 000 ₽ (на руки)")
        self.assertEqual(salary_text({"from": None, "to": 200000, "currency": "RUR", "gross": True}),
                         "до 200 000 ₽ (до налогов)")
        self.assertEqual(salary_text({"from": None, "to": 4200, "currency": "EUR", "gross": True}),
                         "до 4 200 € (до налогов)")
        self.assertEqual(salary_text(None), "не указана")

    def test_gross_ceiling_is_skipped_before_letter_and_post(self):
        underpaid = VACANCY | {"salary": {"from": 40000, "to": 80000,
                                            "currency": "RUR", "gross": True}}
        self.gate.original_vacancies = lambda *_a, **_kw: iter([underpaid, VACANCY])
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(list(self.gate.vacancies(None)), [VACANCY])
        self.assertIn("40 000–80 000 ₽ (до налогов)", output.getvalue())
        self.assertIn("потолок зарплаты не достигает 80 000 ₽ на руки", output.getvalue())
        self.gate.current = underpaid
        with self.assertRaisesRegex(SystemExit, "Cannot send"):
            self.gate.send(None, negotiation())
        self.sender.assert_not_called()

    def test_changed_salary_in_detail_blocks_generation(self):
        self.gate.operation = SimpleNamespace(api_client=SimpleNamespace(get=lambda _: (
            VACANCY | {"salary": {"to": 80000, "currency": "RUR", "gross": True}}
        )))
        self.gate.original_complete = Mock(return_value=LETTER)
        with self.assertRaisesRegex(SystemExit, "Vacancy salary changed"):
            self.gate.complete(None, "prompt")
        self.gate.original_complete.assert_not_called()

    def test_generation_failure_reports_sanitized_gateway_metadata_without_submission(self):
        import json
        response = requests.Response()
        response.status_code = 502
        response._content = json.dumps({"error": {"message": "PRIVATE",
            "generation_failure": {"kind": "connect_timeout", "attempts": 2,
                "connect_retry_exhausted": True, "request_id": "a" * 32}}}).encode()
        transport = requests.HTTPError("PRIVATE", response=response)
        error = OpenAIError("PRIVATE")
        error.__cause__ = transport
        self.gate.operation = SimpleNamespace(api_client=Mock(get=Mock(return_value=VACANCY)))
        self.gate.original_complete = Mock(side_effect=error)
        self.gate.letter = None
        with self.assertRaises(SystemExit) as caught:
            self.gate.complete(None, "PRIVATE")
        message = str(caught.exception)
        self.assertIn("ChatGPT connect timeout after 2 attempts", message)
        self.assertIn("request_id=" + "a" * 32, message)
        self.assertIn("no application sent for this vacancy", message)
        self.assertNotIn("PRIVATE", message)
        self.gate.original_complete.assert_called_once()
        self.sender.assert_not_called()
        self.assertEqual(self.gate.counts["attempts"], 0)
        self.assertEqual(self.gate.counts["letters"], 0)

    def test_real_openai_client_does_not_replay_http_or_read_failures(self):
        for status in (400, 401, 429, 502):
            with self.subTest(status=status):
                client = OpenAIClient(base_url="http://127.0.0.1:8000/v1", model="test")
                response = requests.Response()
                response.status_code, response._content = status, b'{"error":{"message":"PRIVATE"}}'
                response.request = requests.Request("POST", client.base_url).prepare()
                self.gate.operation = SimpleNamespace(api_client=Mock(get=Mock(return_value=VACANCY)))
                self.gate.original_complete = OpenAIClient.complete
                expected = "model generation failed" if status == 429 else f"gateway HTTP {status}"
                with patch.object(client, "_request", return_value=response) as network, \
                        self.assertRaisesRegex(SystemExit, expected):
                    self.gate.complete(client, "PRIVATE")
                network.assert_called_once()
                self.assertEqual(client.max_retries, 3)
                client.session.close()
        client = OpenAIClient(base_url="http://127.0.0.1:8000/v1", model="test")
        with patch.object(client, "_request", side_effect=requests.ReadTimeout("PRIVATE")) as network, \
                self.assertRaisesRegex(SystemExit, "model generation failed") as caught:
            self.gate.complete(client, "PRIVATE")
        network.assert_called_once()
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual(client.max_retries, 3)
        self.sender.assert_not_called()
        client.session.close()

    def gateway_reply(self, status, body):
        import json

        def reply(_session, request, **_kwargs):
            response = requests.Response()
            response.status_code, response._content = status, json.dumps(body).encode()
            response.request, response.url = request, request.url
            return response
        return Mock(side_effect=reply)

    def test_gateway_http_error_reaches_generation_failure_message_through_real_client(self):
        failure = {"attempts": 2, "connect_retry_exhausted": True, "request_id": "b" * 32}
        cases = (
            (502, {"kind": "tls_handshake_timeout", **failure},
             "Stopped: ChatGPT TLS handshake (connection establishment) timed out after 2 attempts; "
             "request_id=" + "b" * 32),
            (502, {"kind": "tls_handshake_timeout", **failure, "request_id": "PRIVATE\nINJECTED"},
             "Stopped: ChatGPT TLS handshake (connection establishment) timed out after 2 attempts; "
             "request_id=unknown"),
            (502, {"kind": "connect_timeout", **failure},
             "Stopped: ChatGPT connect timeout after 2 attempts; request_id=" + "b" * 32),
            (502, {"kind": "tls_handshake_timeout", **failure, "attempts": 4},
             "Stopped: ChatGPT TLS handshake (connection establishment) timed out after 4 attempts; "
             "request_id=" + "b" * 32),
            (502, {"kind": "connect_timeout", **failure, "attempts": 4},
             "Stopped: ChatGPT connect timeout after 4 attempts; request_id=" + "b" * 32),
            (502, {"kind": "connect_timeout", **failure, "attempts": 10},
             "Stopped: ChatGPT connect timeout after 10 attempts; request_id=" + "b" * 32),
            *((502, {"kind": kind, **failure, "attempts": bad}, "Stopped: local model gateway HTTP 502")
              for kind in ("tls_handshake_timeout", "connect_timeout")
              for bad in (1, 11, True, "4", 4.0, None)),
            (502, {"kind": "tls_handshake_timeout", **failure, "attempts": 4,
                   "connect_retry_exhausted": "true"},
             "Stopped: local model gateway HTTP 502"),
            (502, {"kind": "read_timeout", "attempts": 1, "connect_retry_exhausted": False},
             "Stopped: local model gateway HTTP 502"),
            (502, None, "Stopped: local model gateway HTTP 502"),
            # HH's client raises its own rate-limit error without an HTTP cause.
            (429, None, "Stopped: model generation failed"),
        )
        for status, generation_failure, prefix in cases:
            with self.subTest(status=status, failure=generation_failure):
                body = {"error": {"message": "PRIVATE upstream text"}}
                if generation_failure is not None:
                    body["error"]["generation_failure"] = generation_failure
                reply = self.gateway_reply(status, body)
                gate = ApprovalGate(RESUME, None, approve=Mock(side_effect=AssertionError("No approval")))
                generating = []
                sender = Mock(side_effect=lambda *args, **kwargs: (
                    generating.append(gate._generating), reply(*args, **kwargs))[1])
                gate.sender = sender
                self.assertIs(gate._generating, False)
                gate.current = VACANCY.copy()
                gate.operation = SimpleNamespace(api_client=Mock(get=Mock(return_value=VACANCY)))
                gate.original_complete = OpenAIClient.complete
                client = OpenAIClient(base_url="http://127.0.0.1:8000/v1", model="test")
                with patch.object(requests.sessions.Session, "send",
                                  lambda session, request, **kwargs: gate.send(session, request, **kwargs)),                         patch("hh_applicant_tool.ai.openai.time.sleep",
                              side_effect=AssertionError("No replay delay")) as sleep,                         self.assertRaises(SystemExit) as caught:
                    gate.complete(client, "PRIVATE prompt")
                client.session.close()
                message = str(caught.exception)
                self.assertTrue(message.startswith(prefix), message)
                self.assertTrue(message.endswith("; no application sent for this vacancy"), message)
                self.assertNotIn("Stopped after HTTP", message)
                self.assertNotIn("PRIVATE", message)
                self.assertNotIn("INJECTED", message)
                sender.assert_called_once()  # No replay by HH's OpenAI client or the gate.
                sleep.assert_not_called()
                self.assertEqual(generating, [True])
                self.assertIs(gate._generating, False)
                self.assertEqual(client.max_retries, 3)
                request = sender.call_args.args[1]
                self.assertEqual((request.method, request.url),
                                 ("POST", "http://127.0.0.1:8000/v1/chat/completions"))
                gate.approve.assert_not_called()
                self.assertEqual(gate.counts["attempts"], 0)
                self.assertEqual(gate.counts["letters"], 0)
                self.assertIsNone(gate.letter)

    def test_generating_flag_is_reset_after_any_generation_outcome(self):
        self.gate.operation = SimpleNamespace(api_client=Mock(get=Mock(return_value=VACANCY)))
        seen = []

        def failing(*_args, **_kwargs):
            seen.append(self.gate._generating)
            raise RuntimeError("PRIVATE")

        for original, expected in ((failing, "Stopped: model generation failed"),
                                   (Mock(return_value=""), "empty model response")):
            with self.subTest(expected=expected):
                self.gate.original_complete = original
                with self.assertRaisesRegex(SystemExit, expected):
                    self.gate.complete(None, "prompt")
                self.assertIs(self.gate._generating, False)
        self.assertEqual(seen, [True])
        # A salary rejection stops before generation; the flag is never raised.
        self.gate.operation = SimpleNamespace(api_client=SimpleNamespace(get=lambda _: (
            VACANCY | {"salary": {"from": 300001, "currency": "RUR"}})))
        with self.assertRaisesRegex(SystemExit, "Vacancy salary changed"):
            self.gate.complete(None, "prompt")
        self.assertIs(self.gate._generating, False)

    def test_gateway_generation_response_passes_through_only_during_complete(self):
        request = requests.Request("POST", "http://127.0.0.1:8000/v1/chat/completions",
                                   json={"messages": []}).prepare()
        self.assertIs(self.gate._generating, False)
        for generating, status in ((True, 200), (True, 400), (True, 429), (True, 502),
                                   (False, 200)):
            with self.subTest(generating=generating, status=status):
                self.gate._generating = generating
                self.gate.sender = self.gateway_reply(status, {"error": {"message": "x"}})
                response = self.gate.send(None, request, timeout=(15, 90))
                self.assertEqual(response.status_code, status)
                self.gate.sender.assert_called_once()
                self.assertEqual(self.gate.sender.call_args.kwargs, {"timeout": (15, 90)})
        # Outside complete() the gateway POST gets the generic stop, never replay.
        self.gate._generating = False
        for status in (400, 429, 502):
            with self.subTest(generating=False, status=status):
                self.gate.sender = self.gateway_reply(status, {"error": {
                    "message": "PRIVATE", "generation_failure": {
                        "kind": "tls_handshake_timeout", "attempts": 2,
                        "connect_retry_exhausted": True, "request_id": "c" * 32}}})
                with self.assertRaises(SystemExit) as caught:
                    self.gate.send(None, request)
                self.assertEqual(str(caught.exception), f"Stopped after HTTP {status}")
                self.gate.sender.assert_called_once()

    def test_expired_token_get_refreshes_once_through_real_api_client(self):
        import json

        def reply(status, data):
            response = requests.Response()
            response.status_code = status
            response._content = json.dumps(data).encode()
            return response

        expired = {"errors": [{"type": "oauth", "value": "token_expired"}]}
        for repeated in (False, True):
            with self.subTest(repeated=repeated):
                persist = Mock()
                client = ApiClient(access_token="USERold", refresh_token="old-refresh",
                                   access_expires_at=1, client_id="test", client_secret="test",
                                   on_token_change=persist, delay=0.001)
                self.gate.sender = Mock(side_effect=[
                    reply(403, expired),
                    reply(200, {"access_token": "USERnew", "refresh_token": "new-refresh",
                                "expires_in": 3600}),
                    reply(403, expired) if repeated else reply(200, {"items": []}),
                ])
                try:
                    with patch.object(requests.sessions.Session, "send",
                                      lambda session, request, **kw: self.gate.send(session, request, **kw)):
                        if repeated:
                            from hh_applicant_tool.api.errors import Forbidden
                            with self.assertRaises(Forbidden):
                                client.get('/resumes/mine')
                        else:
                            self.assertEqual(client.get('/resumes/mine'), {"items": []})
                    self.assertEqual(self.gate.sender.call_count, 3)
                    methods = [(call.args[1].method, call.args[1].url)
                               for call in self.gate.sender.call_args_list]
                    self.assertEqual(methods, [
                        ('GET', 'https://api.hh.ru/resumes/mine'),
                        ('POST', 'https://hh.ru/oauth/token'),
                        ('GET', 'https://api.hh.ru/resumes/mine'),
                    ])
                    persist.assert_called_once()
                finally:
                    client.session.close()

    def test_token_refresh_exception_is_narrow_and_never_replays_a_send(self):
        from manual_captcha import permits_token_refresh
        expired = {"errors": [{"type": "oauth", "value": "token_expired"}]}
        for request in (
            negotiation(),
            requests.Request('POST', 'https://api.hh.ru/token',
                             data={'grant_type': 'refresh_token'}).prepare(),
            requests.Request('GET', 'https://hh.ru/').prepare(),
            requests.Request('GET', 'http://127.0.0.1:8000/health').prepare(),
        ):
            response = SimpleNamespace(status_code=403, json=lambda: expired)
            self.assertFalse(permits_token_refresh(request, response))
        request = requests.Request('GET', 'https://api.hh.ru/resumes/mine').prepare()
        for data in ({}, None, {'errors': 'invalid'},
                     {'errors': [{'type': 'oauth', 'value': 'token_revoked'}]},
                     {'errors': expired['errors'] + [{'value': 'captcha_required'}]}):
            self.gate.sender = Mock(return_value=SimpleNamespace(status_code=403, json=lambda: data))
            with self.assertRaisesRegex(SystemExit, 'HTTP 403'):
                self.gate.send(None, request)
            self.gate.sender.assert_called_once()
        self.gate.current = VACANCY.copy()
        self.gate.letter = LETTER
        self.gate.sender = Mock(return_value=SimpleNamespace(status_code=403, json=lambda: expired))
        with redirect_stdout(StringIO()), self.assertRaisesRegex(SystemExit, 'HTTP 403'):
            self.gate.send(None, negotiation())
        self.gate.sender.assert_called_once()

    def test_refresh_endpoint_allowlist_matches_client_without_broadening_writes(self):
        from list_resumes import allowed_request as resume_allowed
        from preview_hh import allowed_request as preview_allowed
        for url in ('https://hh.ru/oauth/token', 'https://api.hh.ru/token',
                    'https://hh.ru/token', 'https://hh.ru/oauth/token?x=1',
                    'https://user:pass@hh.ru/oauth/token', 'https://hh.ru:443/oauth/token',
                    'https://hh.ru/oauth/token#fragment', 'https://other.invalid/oauth/token'):
            for grant in ('refresh_token', 'authorization_code', 'password'):
                request = requests.Request('POST', url, data={'grant_type': grant}).prepare()
                expected = (url in ('https://hh.ru/oauth/token', 'https://api.hh.ru/token')
                            and grant == 'refresh_token')
                with self.subTest(url=url, grant=grant):
                    self.assertEqual(resume_allowed(request), expected)
                    self.assertEqual(preview_allowed(request.method, request.url, request.body), expected)

    def test_non_generation_http_errors_still_stop_after_http_status(self):
        cases = (
            requests.Request("GET", "https://api.hh.ru/vacancies/123").prepare(),
            requests.Request("GET", "https://api.hh.ru/resumes/mine").prepare(),
            requests.Request("POST", "https://api.hh.ru/token",
                             data={"grant_type": "refresh_token"}).prepare(),
            requests.Request("GET", "http://127.0.0.1:8000/health").prepare(),
            requests.Request("GET", "http://127.0.0.1:8000/v1/models").prepare(),
        )
        for request in cases:
            for status in (400, 404, 500, 502):
                with self.subTest(method=request.method, url=request.url, status=status):
                    self.gate.sender = self.gateway_reply(status, {"error": {
                        "message": "PRIVATE", "generation_failure": {
                            "kind": "tls_handshake_timeout", "attempts": 2,
                            "connect_retry_exhausted": True, "request_id": "c" * 32}}})
                    with self.assertRaises(SystemExit) as caught:
                        self.gate.send(None, request)
                    self.assertEqual(str(caught.exception), f"Stopped after HTTP {status}")
                    self.gate.sender.assert_called_once()
        self.gate.captcha.active = True
        for request in (requests.Request("GET", "https://hh.ru/account/captcha").prepare(),
                        requests.Request("GET", "https://hh.ru/captcha/picture").prepare(),
                        requests.Request("POST", "https://hh.ru/captcha").prepare()):
            with self.subTest(captcha=request.url):
                self.gate.sender = self.gateway_reply(500, {})
                with self.assertRaises(SystemExit) as caught:
                    self.gate.send(None, request)
                self.assertEqual(str(caught.exception), "Stopped after HTTP 500")
        self.gate.captcha.active = False
        for method, url in (("POST", "http://127.0.0.1:8000/v1/completions"),
                            ("POST", "http://127.0.0.1:8001/v1/chat/completions"),
                            ("POST", "https://127.0.0.1:8000/v1/chat/completions"),
                            ("POST", "http://user:pass@127.0.0.1:8000/v1/chat/completions"),
                            ("PUT", "http://127.0.0.1:8000/v1/chat/completions")):
            with self.subTest(blocked=(method, url)):
                self.gate.sender = self.gateway_reply(502, {})
                with self.assertRaisesRegex(SystemExit, "Blocked an unexpected network request"):
                    self.gate.send(None, requests.Request(method, url).prepare())
                self.gate.sender.assert_not_called()

    def test_above_maximum_is_skipped_before_letter_and_post(self):
        overpaid = VACANCY | {"salary": {"from": 100000, "to": 300001,
                                         "currency": "RUR", "gross": True}}
        self.gate.original_vacancies = lambda *_a, **_kw: iter([overpaid, VACANCY])
        with redirect_stdout(StringIO()):
            self.assertEqual(list(self.gate.vacancies(None)), [VACANCY])
        self.gate.current = overpaid
        with self.assertRaisesRegex(SystemExit, "Cannot send"):
            self.gate.send(None, negotiation())
        self.sender.assert_not_called()

    def test_above_maximum_in_detail_blocks_generation(self):
        self.gate.operation = SimpleNamespace(api_client=SimpleNamespace(get=lambda _: (
            VACANCY | {"salary": {"from": 300001, "currency": "RUR"}}
        )))
        self.gate.original_complete = Mock(return_value=LETTER)
        with self.assertRaisesRegex(SystemExit, "Vacancy salary changed"):
            self.gate.complete(None, "prompt")
        self.gate.original_complete.assert_not_called()

    def test_foreign_maximum_is_checked_with_operation_client(self):
        operation = Mock()
        operation.api_client = ApiClient(session=Mock())
        operation.api_client.get = Mock(return_value={"currency": [{"code": "USD", "rate": 0.01}]})
        # Mock has arbitrary attributes; a real single-resume operation has no round context.
        del operation._round_resume
        overpaid = VACANCY | {"salary": {"from": 3001, "currency": "USD"}}
        self.gate.original_vacancies = lambda *_a, **_kw: iter([overpaid, VACANCY])
        with redirect_stdout(StringIO()):
            self.assertEqual(list(self.gate.vacancies(operation)), [VACANCY])
        operation.api_client.get.assert_called_once_with("/dictionaries")

    def test_skipped_vacancy_shows_reason(self):
        self.gate.original_vacancies = lambda *_a, **_kw: iter([VACANCY | {"relations": ["response"]}])
        output = StringIO()
        with redirect_stdout(output):
            list(self.gate.vacancies(None))
        self.assertIn("Компания: Test", output.getvalue())
        self.assertIn("Вакансия: Linux", output.getvalue())
        self.assertIn("пропускаем — уже есть отклик", output.getvalue())

    def test_senior_title_is_skipped_before_letter_and_post(self):
        senior = VACANCY | {"name": "Ведущий инженер Linux"}
        self.gate.original_vacancies = lambda *_a, **_kw: iter([senior, VACANCY])
        output = StringIO()
        with redirect_stdout(output):
            shown = list(self.gate.vacancies(None))
        self.assertEqual(shown, [VACANCY])
        self.assertEqual(self.gate.counts["vacancies"], 2)
        self.assertIn("Компания: Test", output.getvalue())
        self.assertIn("пропускаем — уровень выше текущего опыта", output.getvalue())
        self.gate.current = senior
        with self.assertRaisesRegex(SystemExit, "Cannot send"):
            self.gate.send(None, negotiation())
        self.sender.assert_not_called()

    def test_english_letter_override_and_language_gate_before_approval(self):
        vacancy = VACANCY | {"description": (
            "We are looking for a Linux engineer to join our team. "
            "You will work with our infrastructure and improve automation. "
            "Experience with Linux and cloud tools is important for this role."
        )}
        self.gate.operation = SimpleNamespace(api_client=SimpleNamespace(get=lambda _: vacancy))
        generated = Mock(return_value="Hi! I work with Linux and automation. Let's talk about this role.")
        self.gate.original_complete = generated
        self.gate.letter = None
        letter = self.gate.complete(None, "Пиши по-русски. Telegram @nikchester")
        self.assertEqual(letter, generated.return_value)
        prompt = generated.call_args.args[1]
        self.assertIn("Write a short cover letter in English", prompt)
        self.assertIn("Telegram @nikchester", prompt)
        self.assertNotIn("Пиши по-русски", prompt)
        self.assertIn("system_prompt", generated.call_args.kwargs)

        self.gate.letter = None
        generated.return_value = "Привет! Я работаю с Linux и хочу обсудить вакансию."
        with self.assertRaisesRegex(SystemExit, "английском"):
            self.gate.complete(None, "Пиши по-русски")
        self.assertIsNone(self.gate.letter)
        self.assertEqual(self.gate.counts["letters"], 1)
        with self.assertRaisesRegex(SystemExit, "without reviewing"):
            self.gate.send(None, negotiation())
        self.sender.assert_not_called()

    def test_uncertain_result_stops_without_retry(self):
        self.sender.side_effect = requests.ConnectionError("connection dropped")
        with redirect_stdout(StringIO()), self.assertRaisesRegex(SystemExit, "outcome uncertain"):
            self.gate.send(None, negotiation())
        self.sender.assert_called_once()
        self.assertEqual(self.gate.counts["attempts"], 1)
        self.assertEqual(self.gate.counts["http_success"], 0)

    def test_transport_failure_diagnostics_are_allowlisted_and_redacted(self):
        secret = "Bearer SECRET https://user:password@proxy.invalid/?token=SECRET message=PRIVATE"
        cases = (
            (requests.ConnectTimeout, "connection establishment timed out"),
            (requests.ReadTimeout, "timed out waiting for HH response"),
            (requests.exceptions.SSLError, "TLS handshake or certificate failure"),
            (requests.exceptions.ProxyError, "proxy connection failed"),
            (requests.ConnectionError, "connection failed or dropped"),
            (requests.exceptions.ChunkedEncodingError, "incomplete HH response"),
            (requests.RequestException, "request transport failed"),
        )
        for kind, reason in cases:
            with self.subTest(kind=kind.__name__):
                self.gate.letter = LETTER
                self.sender.reset_mock()
                error = kind(secret, request=negotiation())
                error.__cause__ = RuntimeError(secret)
                self.sender.side_effect = error
                with redirect_stdout(StringIO()), self.assertRaises(SystemExit) as caught:
                    self.gate.send(None, negotiation())
                message = str(caught.exception)
                self.assertIn("vacancy_id=123", message)
                self.assertIn(kind.__name__, message)
                self.assertIn(reason, message)
                self.assertIn("check HH manually before any retry", message)
                for forbidden in ("SECRET", "password", "PRIVATE", "proxy.invalid", LETTER, RESUME):
                    self.assertNotIn(forbidden, message)
                self.assertTrue(caught.exception.__suppress_context__)
                self.sender.assert_called_once()
                self.assertIsNone(self.gate.letter)

    def test_diagnostic_vacancy_id_cannot_inject_text(self):
        from send import send_failure_detail
        self.assertIn("vacancy_id=unknown", send_failure_detail(
            requests.ConnectionError("private"), "123\nBearer SECRET"))

    def test_real_session_boundary_clears_pools_after_approval_and_preserves_settings(self):
        session = requests.Session()
        session.headers["X-Test"] = "disposable"
        session.cookies.set("test", "disposable")
        session.proxies = {"https": "http://proxy.invalid"}
        adapter = session.get_adapter("https://api.hh.ru")
        response = requests.Response()
        response.status_code, response._content = 201, b"{}"
        events = []
        self.gate.approve = lambda _: events.append("approve") or "yes"
        self.gate.sender = requests.sessions.Session.send

        def close():
            events.append("clear")

        def transport(request, **kwargs):
            events.append("send")
            self.assertEqual(kwargs["timeout"], (10, 60))
            self.assertIs(kwargs["verify"], True)
            self.assertEqual(request.body, negotiation().body)
            return response

        with patch.object(adapter, "close", side_effect=close), \
             patch.object(adapter, "send", side_effect=transport) as transport_mock, \
             redirect_stdout(StringIO()):
            self.assertIs(self.gate.send(session, negotiation(), timeout=None), response)
        self.assertEqual(events, ["approve", "clear", "send"])
        transport_mock.assert_called_once()
        self.assertEqual(session.headers["X-Test"], "disposable")
        self.assertEqual(session.cookies.get("test"), "disposable")
        self.assertEqual(session.proxies, {"https": "http://proxy.invalid"})
        self.assertEqual(adapter.max_retries.total, 0)
        self.assertEqual(self.gate.counts["http_success"], 1)
        session.close()

    def test_explicit_timeout_is_preserved(self):
        with redirect_stdout(StringIO()):
            self.gate.send(None, negotiation(), timeout=(5, 20), verify=True)
        self.assertEqual(self.sender.call_args.kwargs, {"timeout": (5, 20), "verify": True})

    def test_upstream_connection_retry_loop_cannot_repeat_uncertain_post(self):
        client = ApiClient()
        self.assertEqual(client.max_retries, 3)
        self.sender.side_effect = requests.ConnectionError("disposable failure")
        with patch.object(requests.sessions.Session, "send", new=lambda session, request, **kwargs: (
                self.gate.send(session, request, **kwargs))), \
             redirect_stdout(StringIO()), self.assertRaisesRegex(SystemExit, "outcome uncertain"):
            client.post("/negotiations", {
                "resume_id": RESUME, "vacancy_id": "123", "message": LETTER,
            })
        self.sender.assert_called_once()
        self.assertEqual(self.gate.counts["attempts"], 1)
        client.session.close()

    def test_explicit_captcha_needs_fresh_yes_for_one_identical_retry(self):
        response = SimpleNamespace(status_code=403, json=lambda: {
            "errors": [{"value": "captcha_required",
                        "captcha_url": "https://hh.ru/account/captcha?state=test"}],
        })
        tool = SimpleNamespace(captcha_manual=False, _can_use_tkinter=lambda: True)
        self.gate.operation = SimpleNamespace(tool=tool)
        calls = []

        def fake_sender(_session, request, **_kwargs):
            calls.append((request.method, request.url))
            if request.url == "https://api.hh.ru/negotiations":
                return response if sum(url == request.url for _, url in calls) == 1 else SimpleNamespace(status_code=200)
            if (request.method == "POST" and urlsplit(request.url).hostname == "hh.ru"
                    and urlsplit(request.url).path == "/account/captcha"):
                return SimpleNamespace(status_code=403)
            return SimpleNamespace(status_code=200)

        self.gate.sender = fake_sender
        self.gate.approve = Mock(side_effect=["yes", " y "])

        def manual_handler(_url):
            self.assertTrue(tool.captcha_manual)
            self.gate.send(None, requests.Request("GET", "https://hh.ru/account/captcha").prepare())
            self.gate.send(None, requests.Request("POST", "https://hh.ru/captcha?lang=ru").prepare())
            self.assertEqual(self.gate.send(None, requests.Request(
                "POST", "https://hh.ru/account/captcha", params={"captchaText": "test"}
            ).prepare()).status_code, 403)
            for bad in ("https://api.hh.ru/negotiations", "https://hh.ru/account/change-email",
                        "https://example.com/account/captcha"):
                with self.assertRaises(SystemExit):
                    self.gate.send(None, requests.Request("POST", bad).prepare())
            return True

        tool.handle_captcha = Mock(side_effect=manual_handler)
        output = StringIO()
        with redirect_stdout(output):
            result = self.gate.send(None, negotiation())
        self.assertEqual(result.status_code, 200)
        tool.handle_captcha.assert_called_once_with("https://hh.ru/account/captcha?state=test")
        self.assertFalse(self.gate.captcha_active)
        self.assertEqual([method for method, url in calls if url.endswith("/negotiations")], ["POST", "POST"])
        self.assertEqual(self.gate.approve.call_count, 2)
        self.assertEqual(output.getvalue().count(LETTER), 2)
        self.assertEqual(output.getvalue().count("Компания: Test"), 2)
        self.assertIn("КАПЧА ПРОЙДЕНА", output.getvalue())
        self.assertEqual(self.gate.counts["attempts"], 2)
        self.assertEqual(self.gate.counts["captcha_rejections"], 1)
        self.assertEqual(self.gate.counts["http_success"], 1)
        with self.assertRaisesRegex(SystemExit, "Blocked"):
            self.gate.send(None, requests.Request("POST", "https://hh.ru/captcha").prepare())

    def test_captcha_no_stop_and_missing_second_approval_never_retry(self):
        challenge = SimpleNamespace(status_code=403, json=lambda: {
            "errors": [{"value": "captcha_required", "captcha_url": "https://hh.ru/account/captcha"}],
        })
        for second in ("no", "n", "stop", "maybe", None, EOFError):
            with self.subTest(second=second):
                sender = Mock(return_value=challenge)
                gate = ApprovalGate(RESUME, sender, approve=Mock(side_effect=(
                    ["yes", EOFError()] if second is EOFError else ["yes", second]
                )))
                gate.current, gate.letter = VACANCY.copy(), LETTER
                tool = SimpleNamespace(captcha_manual=False, _can_use_tkinter=lambda: True,
                                       handle_captcha=Mock(return_value=True))
                gate.operation = SimpleNamespace(tool=tool)
                with redirect_stdout(StringIO()), self.assertRaises((Redirect, StopReview, SystemExit)):
                    gate.send(None, negotiation())
                sender.assert_called_once()
                tool.handle_captcha.assert_called_once()
                self.assertEqual(gate.counts["attempts"], 1)
                self.assertEqual(gate.counts["http_success"], 0)
                self.assertFalse(gate.captcha_active)

    def test_second_captcha_or_network_failure_stops_without_more_attempts(self):
        challenge = SimpleNamespace(status_code=403, json=lambda: {
            "errors": [{"value": "captcha_required", "captcha_url": "https://hh.ru/account/captcha"}],
        })
        for second in (challenge, requests.ConnectionError("lost response")):
            with self.subTest(second=second):
                sender = Mock(side_effect=[challenge, second])
                gate = ApprovalGate(RESUME, sender, approve=Mock(side_effect=["yes", "yes"]))
                gate.current, gate.letter = VACANCY.copy(), LETTER
                handler = Mock(return_value=True)
                gate.operation = SimpleNamespace(tool=SimpleNamespace(
                    captcha_manual=False, _can_use_tkinter=lambda: True, handle_captcha=handler
                ))
                with redirect_stdout(StringIO()), self.assertRaises(SystemExit):
                    gate.send(None, negotiation())
                self.assertEqual(sender.call_count, 2)
                handler.assert_called_once()
                self.assertEqual(gate.counts["attempts"], 2)
                self.assertEqual(gate.counts["http_success"], 0)

    def test_only_valid_hh_captcha_challenge_starts_manual_flow(self):
        tool = SimpleNamespace(_can_use_tkinter=Mock(return_value=True),
                               handle_captcha=Mock(return_value=True))
        self.gate.operation = SimpleNamespace(tool=tool)
        for error in (
            {"value": "access_denied", "captcha_url": "https://hh.ru/account/captcha"},
            {"value": "captcha_required", "captcha_url": "https://example.com/account/captcha"},
            {"value": "captcha_required", "captcha_url": "https://hh.ru/account/captcha/other"},
            {"value": "captcha_required", "captcha_url": "http://hh.ru/account/captcha"},
            {"value": "captcha_required", "captcha_url": "https://hh.ru:444/account/captcha"},
            {"value": "captcha_required", "captcha_url": "https://user@hh.ru/account/captcha"},
        ):
            with self.subTest(error=error):
                self.gate.letter = LETTER
                self.sender.return_value = SimpleNamespace(status_code=403, json=lambda: {"errors": [error]})
                with redirect_stdout(StringIO()), self.assertRaisesRegex(SystemExit, "HTTP 403"):
                    self.gate.send(None, negotiation())
        tool.handle_captcha.assert_not_called()
        tool._can_use_tkinter.assert_not_called()

    def test_captcha_requires_gui_and_failure_stops(self):
        self.sender.return_value = SimpleNamespace(status_code=403, json=lambda: {
            "errors": [{"value": "captcha_required", "captcha_url": "https://hh.ru/account/captcha"}],
        })
        tool = SimpleNamespace(captcha_manual=False, _can_use_tkinter=Mock(return_value=False),
                               handle_captcha=Mock(return_value=True))
        self.gate.operation = SimpleNamespace(tool=tool)
        with redirect_stdout(StringIO()), self.assertRaisesRegex(SystemExit, "Tkinter"):
            self.gate.send(None, negotiation())
        tool.handle_captcha.assert_not_called()
        self.assertFalse(tool.captcha_manual)
        self.gate.letter = LETTER
        tool._can_use_tkinter.return_value = True
        tool.handle_captcha.return_value = False
        with redirect_stdout(StringIO()), self.assertRaisesRegex(SystemExit, "not accepted"):
            self.gate.send(None, negotiation())
        self.assertTrue(tool.captcha_manual)
        self.assertFalse(self.gate.captcha_active)
        self.assertEqual(self.gate.counts["attempts"], 2)
        self.assertEqual(self.gate.counts["http_success"], 0)

    def test_run_wires_approval_to_real_post_boundary_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            profile = home / ".config/pocketnode-hh"
            profile.mkdir(parents=True)
            shutil.copyfile(ROOT / "windows/config.toml", profile / "config.toml")
            argv = arguments(str(profile), RESUME, "DevOps")

            def fake_vacancies(operation, *args, **kwargs):
                yield VACANCY.copy()

            def fake_main(_argv):
                operation = Operation()
                operation.tool = SimpleNamespace(api_client=SimpleNamespace(
                    get=lambda endpoint: {"id": "123", "name": "Linux", "description": "Linux"}
                ))
                for _ in operation._get_vacancies(resume_id=RESUME):
                    OpenAIClient.complete(None, "prompt")
                    requests.Session().send(negotiation())
                return 0

            with patch.dict(os.environ, {"CONFIG_DIR": str(profile), "HH_PROFILE_ID": "."}), \
                 patch("send.Path.home", return_value=home), \
                 patch.object(requests.sessions.Session, "send", self.sender), \
                 patch.object(Operation, "_get_vacancies", fake_vacancies), \
                 patch.object(OpenAIClient, "complete", return_value=LETTER), \
                 patch("send.main", side_effect=fake_main):
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(run(argv, approve=lambda _: "Y "), 0)
                self.assertIn("Компания: Test", output.getvalue())
                self.assertIn("Вакансия: Linux", output.getvalue())
                self.assertIn(LETTER, output.getvalue())
                self.assertIn("попыток отправки 1", output.getvalue())
            self.sender.assert_called_once()

    def test_run_passes_operation_to_test_solver_on_keyword_call(self):
        # Upstream calls self._solve_vacancy_test(vacancy_id=..., ...) with keywords only.
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            profile = home / ".config/pocketnode-hh"
            profile.mkdir(parents=True)
            shutil.copyfile(ROOT / "windows/config.toml", profile / "config.toml")
            argv = arguments(str(profile), RESUME, "DevOps")
            received = []

            def solver(operation, **kwargs):
                received.append((operation, kwargs))
                return {"success": "true"}

            def fake_main(_argv):
                operation = Operation()
                self.assertEqual(operation._solve_vacancy_test(vacancy_id="1", resume_hash=RESUME,
                                                               letter=LETTER), {"success": "true"})
                received.append(operation)
                return 0

            with patch.dict(os.environ, {"CONFIG_DIR": str(profile), "HH_PROFILE_ID": "."}),                  patch("send.Path.home", return_value=home),                  patch.object(Operation, "_solve_vacancy_test", solver),                  patch("send.main", side_effect=fake_main), redirect_stdout(StringIO()):
                self.assertEqual(run(argv, approve=lambda _: "no"), 0)
            self.assertIs(received[0][0], received[1])
            self.assertEqual(received[0][1]["vacancy_id"], "1")

    def test_more_than_three_vacancies_no_then_yes_then_stop(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            profile = home / ".config/pocketnode-hh"
            profile.mkdir(parents=True)
            shutil.copyfile(ROOT / "windows/config.toml", profile / "config.toml")
            argv = arguments(str(profile), RESUME, "DevOps")
            vacancies = [VACANCY | {"id": str(i), "name": f"Linux {i}"}
                         for i in range(1, 7)]
            decisions = iter(["n", "yes", "no", "yes", "stop"])
            sent = []

            def fake_main(_argv):
                operation = Operation()
                operation.tool = SimpleNamespace(api_client=SimpleNamespace(
                    get=lambda endpoint: {"id": endpoint.rsplit("/", 1)[-1],
                                          "name": "Linux", "description": "Linux"}
                ))
                for vacancy in operation._get_vacancies(resume_id=RESUME):
                    OpenAIClient.complete(None, "prompt")
                    try:
                        requests.Session().send(negotiation(vacancy["id"]))
                    except Redirect:
                        continue  # HH's own vacancy loop handles a skipped response this way.
                return 0

            def fake_sender(_session, request, **_kwargs):
                sent.append(request.body)
                return SimpleNamespace(status_code=200)

            with patch.dict(os.environ, {"CONFIG_DIR": str(profile), "HH_PROFILE_ID": "."}), \
                 patch("send.Path.home", return_value=home), \
                 patch.object(requests.sessions.Session, "send", fake_sender), \
                 patch.object(Operation, "_get_vacancies", return_value=iter(vacancies)), \
                 patch.object(OpenAIClient, "complete", return_value=LETTER), \
                 patch("send.main", side_effect=fake_main):
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(run(argv, approve=lambda _: next(decisions)), 0)
            self.assertEqual(len(sent), 2)
            self.assertIn("просмотрено 5", output.getvalue())
            self.assertIn("пропущено 2", output.getvalue())
            self.assertIn("попыток отправки 2", output.getvalue())
            self.assertIn("Просмотр завершён по команде stop", output.getvalue())

    def test_all_resumes_round_robin_keeps_search_facts_and_payload_together(self):
        other = "27f3daa7ff10eda2e50039ed1f68686c526532"
        mapping = load_searches()
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            profile = home / ".config/pocketnode-hh"
            profile.mkdir(parents=True)
            shutil.copyfile(ROOT / "windows/config.toml", profile / "config.toml")
            argv = all_resume_arguments(str(profile), RESUME)
            resumes = [
                {"id": RESUME, "title": "DevOps", "status": {"id": "published"},
                 "alternate_url": "https://hh.ru/resume/devops"},
                {"id": other, "title": "Sysadmin", "status": {"id": "published"},
                 "alternate_url": "https://hh.ru/resume/sysadmin"},
            ]
            dataset = {RESUME: ["a", "shared", "c"], other: ["b", "shared", "d"]}
            visits, contexts, sent = [], [], []
            active = [None]

            @contextmanager
            def context(resume_id, configured_id):
                self.assertEqual(configured_id, RESUME)
                contexts.append(resume_id)
                active[0] = resume_id
                try:
                    yield
                finally:
                    active[0] = None

            class Tool:
                api_client = SimpleNamespace(get=lambda endpoint: {
                    "id": endpoint.rsplit("/", 1)[-1], "name": "Linux", "description": "Linux"
                })

                def get_resumes(self):
                    return resumes

                def get_me(self):
                    return {}

                def get_openai_settings(self, section):
                    self_test.assertIn(section, ("openai.cover_letter", "openai.test_solver"))
                    return {"history": [active[0]]}

                def get_ai_client(self, **settings):
                    return SimpleNamespace(resume=settings["history"][0])

            self_test = self

            def search(operation, resume_id):
                for vacancy_id in dataset[resume_id]:
                    self.assertEqual(operation.text, mapping[resume_id])
                    yield VACANCY | {"id": vacancy_id, "name": f"Linux {vacancy_id}"}

            def fake_apply(operation, resume, _user, _employers):
                for vacancy in operation._get_vacancies(resume_id=resume["id"]):
                    self.assertEqual(operation.cover_letter_ai.resume, resume["id"])
                    visits.append((resume["id"], vacancy["id"]))
                    OpenAIClient.complete(None, "prompt")
                    requests.Session().send(negotiation(vacancy["id"], resume_id=resume["id"]))
                return False

            def fake_main(_argv):
                operation = Operation()
                operation.tool = Tool()
                operation.text = mapping[RESUME]
                operation._apply_vacancies()
                return 0

            def fake_sender(_session, request, **_kwargs):
                sent.append(request.body)
                return SimpleNamespace(status_code=200)

            with patch.dict(os.environ, {"CONFIG_DIR": str(profile), "HH_PROFILE_ID": "."}), \
                 patch("send.Path.home", return_value=home), \
                 patch("send_all.selected_resume_context", side_effect=context), \
                 patch("send_all.SystemRandom") as random_class, \
                 patch.object(requests.sessions.Session, "send", fake_sender), \
                 patch.object(Operation, "_get_vacancies", search), \
                 patch.object(Operation, "_apply_resume", fake_apply), \
                 patch.object(OpenAIClient, "complete", return_value=LETTER), \
                 patch("send.main", side_effect=fake_main):
                random_class.return_value.shuffle.side_effect = lambda items: items.reverse()
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(run(argv, approve=lambda _: "yes", all_resumes=True), 0)
            self.assertEqual(visits, [(other, "b"), (RESUME, "a"),
                                      (other, "shared"), (RESUME, "c"), (other, "d")])
            self.assertCountEqual(contexts, [RESUME, other])
            self.assertEqual(len(sent), 5)
            self.assertIn("Порядок первого круга: Sysadmin → DevOps", output.getvalue())
            self.assertIn("Пропущена повторяющаяся вакансия shared", output.getvalue())
            self.assertIn("ответов HH 2xx 5", output.getvalue())

    def test_all_resumes_missing_search_stops_before_any_apply(self):
        tool = SimpleNamespace(get_resumes=lambda: [{"id": "new", "title": "New",
                                                      "status": {"id": "published"}}])
        operation = SimpleNamespace(tool=tool)
        with self.assertRaisesRegex(SystemExit, "missing a SearchText"):
            apply_round_robin(operation, RESUME, load_searches(), None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
