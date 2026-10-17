"""Offline safety tests: no HH or model network traffic is possible here."""

import os
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from hh_applicant_tool.ai.openai import OpenAIClient
from hh_applicant_tool.operations.apply_vacancies import Operation

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "windows"))
from send import ApprovalGate, arguments, run, salary_text, validate  # noqa: E402


RESUME = "9749b651ff056b6c970039ed1f5a63765a5178"
VACANCY = {"id": "123", "name": "Linux", "employer": {"name": "Test"},
           "salary": {"from": 100000, "currency": "RUR"}, "relations": []}
LETTER = "Привет! Я работаю с Linux. Созвонимся?"


def negotiation(vacancy_id="123", letter=LETTER, url="https://api.hh.ru/negotiations"):
    return requests.Request("POST", url, data={
        "resume_id": RESUME, "vacancy_id": vacancy_id, "message": letter,
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
            shutil.copyfile(ROOT / "termux/config.toml", profile / "config.toml")
            search = 'DevOps OR "Linux administrator"'
            argv = arguments(str(profile), RESUME, search)
            self.assertEqual(argv[-1], "--search=" + search)
            with patch.dict(os.environ, {"CONFIG_DIR": str(profile), "HH_PROFILE_ID": "."}), \
                 patch("send.Path.home", return_value=home), \
                 patch("requests.sessions.Session.send", side_effect=AssertionError("No network")):
                parsed = validate(argv)
                self.assertIs(parsed.dry_run, False)
                self.assertEqual(parsed.max_responses, 3)
                self.assertEqual(parsed.per_page, 3)
                for forbidden in ("--dry-run", "--send-email", "--ai-filter"):
                    with self.subTest(forbidden=forbidden):
                        with self.assertRaises(SystemExit):
                            validate(argv + [forbidden])
                changed = argv.copy()
                changed[changed.index("--no-handle-captcha")] = "--handle-captcha"
                with self.assertRaises(SystemExit):
                    validate(changed)

    def test_send_requires_exact_approval(self):
        for answer in ("no", "n", "", "да", "maybe"):
            self.gate.approve = lambda _, value=answer: value
            with self.subTest(answer=answer), redirect_stdout(StringIO()), \
                 self.assertRaisesRegex(SystemExit, "No approval"):
                self.gate.send(None, negotiation())
        self.sender.assert_not_called()
        self.assertEqual(self.gate.counts["attempts"], 0)

    def test_payload_and_context_must_match(self):
        for request in (negotiation("different"), negotiation(letter="different"),
                        negotiation(url="https://api.hh.ru/negotiations?x=1"),
                        requests.Request("POST", "https://api.hh.ru/vacancies/blacklisted/123").prepare()):
            with self.subTest(url=request.url, body=request.body):
                with self.assertRaises(SystemExit):
                    self.gate.send(None, request)
        self.sender.assert_not_called()

    def test_existing_response_or_test_prevents_send(self):
        for extra in ({"relations": ["response"]}, {"has_test": True},
                      {"archived": True}, {"response_url": "https://example.com"}):
            self.gate.current = VACANCY | extra
            with self.assertRaises(SystemExit):
                self.gate.send(None, negotiation())
        self.sender.assert_not_called()

    def test_exact_approval_sends_once_and_caps_attempts(self):
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
                         ("Отправить этот отклик? [yes/no]: ",))
        self.sender.assert_called_once()
        self.assertEqual(self.gate.counts["attempts"], 1)
        self.assertEqual(self.gate.counts["http_success"], 1)
        self.assertIsNone(self.gate.letter)
        with self.assertRaisesRegex(SystemExit, "without reviewing"):
            self.gate.send(None, negotiation())
        self.gate.counts["attempts"] = 3
        with self.assertRaisesRegex(SystemExit, "limit"):
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

    def test_skipped_vacancy_shows_reason(self):
        self.gate.original_vacancies = lambda *_a, **_kw: iter([VACANCY | {"relations": ["response"]}])
        output = StringIO()
        with redirect_stdout(output):
            list(self.gate.vacancies(None))
        self.assertIn("Компания: Test", output.getvalue())
        self.assertIn("Вакансия: Linux", output.getvalue())
        self.assertIn("пропускаем — уже есть отклик", output.getvalue())

    def test_uncertain_result_stops_without_retry(self):
        self.sender.side_effect = requests.ConnectionError("connection dropped")
        with redirect_stdout(StringIO()), self.assertRaisesRegex(SystemExit, "outcome uncertain"):
            self.gate.send(None, negotiation())
        self.sender.assert_called_once()
        self.assertEqual(self.gate.counts["attempts"], 1)
        self.assertEqual(self.gate.counts["http_success"], 0)

    def test_run_wires_approval_to_real_post_boundary_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            profile = home / ".config/pocketnode-hh"
            profile.mkdir(parents=True)
            shutil.copyfile(ROOT / "termux/config.toml", profile / "config.toml")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
