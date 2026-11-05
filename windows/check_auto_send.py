"""Offline checks for bounded automatic sending; never touches HH accounts."""

from contextlib import nullcontext, redirect_stderr, redirect_stdout
from functools import partial
from io import StringIO
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests
from hh_applicant_tool.ai.openai import OpenAIClient
from hh_applicant_tool.operations.apply_vacancies import Operation

from auto_send import AutoGate, main, validate_settings
from check_send import LETTER, RESUME, VACANCY, negotiation
from send import StopReview, arguments, run
from send_all import apply_round_robin, single_round_vacancy


class AutoChecks(unittest.TestCase):
    def setUp(self):
        self.sender = Mock(return_value=SimpleNamespace(status_code=201))
        self.sleep = Mock()
        self.uniform = Mock(return_value=75)
        self.gate = AutoGate(RESUME, self.sender, max_responses=2,
                             sleep=self.sleep, uniform=self.uniform)
        self.gate.current = VACANCY.copy()
        self.gate.letter = LETTER

    def send(self):
        self.gate.letter = LETTER
        with redirect_stdout(StringIO()):
            return self.gate.send(None, negotiation())

    def test_count_limit_and_pause_between_sends_only(self):
        self.send()
        self.sleep.assert_not_called()
        self.send()
        self.uniform.assert_called_once_with(60, 180)
        self.sleep.assert_called_once_with(75)
        self.assertEqual(self.gate.counts['http_success'], 2)
        with self.assertRaises(StopReview):
            self.send()
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(self.gate.counts['attempts'], 2)

    def test_skips_do_not_count_or_sleep(self):
        overpaid = VACANCY | {'salary': {'from': 300001, 'currency': 'RUR'}}
        self.gate.original_vacancies = lambda *_a, **_kw: iter([overpaid, VACANCY])
        with redirect_stdout(StringIO()):
            self.assertEqual(list(self.gate.vacancies(None)), [VACANCY])
        self.assertEqual(self.gate.counts['http_success'], 0)
        self.sleep.assert_not_called()
        self.sender.assert_not_called()

    def test_failure_without_explicit_captcha_stops_without_retry_or_solver(self):
        tool = Mock()
        self.gate.operation = SimpleNamespace(tool=tool)
        for status in (403, 429, 500):
            self.sender.return_value = SimpleNamespace(status_code=status, json=lambda: {})
            with self.subTest(status=status), self.assertRaisesRegex(SystemExit, 'HTTP'):
                self.send()
        self.assertEqual(self.sender.call_count, 3)
        self.assertEqual(self.gate.counts['http_success'], 0)
        tool.handle_captcha.assert_not_called()
        self.sleep.assert_not_called()

    def test_network_failure_is_not_retried(self):
        self.sender.side_effect = requests.Timeout()
        with self.assertRaisesRegex(SystemExit, 'outcome uncertain'):
            self.send()
        self.sender.assert_called_once()
        self.assertEqual(self.gate.counts['http_success'], 0)

    def test_pause_precedes_pool_refresh_and_failed_post_has_safe_diagnostics(self):
        session = requests.Session()
        events = []
        self.gate.sleep = lambda delay: events.append(('pause', delay))
        self.gate.counts['http_success'] = 1
        self.sender.side_effect = requests.ConnectionError(
            'https://user:SECRET@proxy.invalid/?token=SECRET')
        with patch.object(session, 'close', side_effect=lambda: events.append('clear')), \
             redirect_stdout(StringIO()), self.assertRaises(SystemExit) as caught:
            self.gate.send(session, negotiation())
        self.assertEqual(events, [('pause', 75), 'clear'])
        self.assertIn('ConnectionError', str(caught.exception))
        self.assertIn('vacancy_id=123', str(caught.exception))
        self.assertNotIn('SECRET', str(caught.exception))
        self.assertNotIn('proxy.invalid', str(caught.exception))
        self.sender.assert_called_once_with(session, unittest.mock.ANY, timeout=(10, 60))
        self.assertEqual(self.gate.counts['attempts'], 1)
        self.assertEqual(self.gate.counts['http_success'], 1)
        self.assertIsNone(self.gate.letter)
        session.close()

    def test_interrupt_during_pause_does_not_send(self):
        self.send()
        self.sleep.side_effect = KeyboardInterrupt
        with self.assertRaises(SystemExit):
            self.send()
        self.sender.assert_called_once()
        self.assertEqual(self.gate.counts['attempts'], 1)

    def test_payload_and_filters_still_protect_network_boundary(self):
        with self.assertRaisesRegex(SystemExit, 'differs'):
            with redirect_stdout(StringIO()):
                self.gate.send(None, negotiation(letter='different letter'))
        self.gate.current = VACANCY | {'salary': {'from': 400000, 'currency': 'RUR'}}
        with self.assertRaisesRegex(SystemExit, 'Cannot send'):
            self.send()
        self.sender.assert_not_called()

    def test_non_negotiation_requests_have_no_pause(self):
        self.gate.counts['http_success'] = 1
        self.gate.send(None, requests.Request('GET', 'https://api.hh.ru/me').prepare())
        self.sleep.assert_not_called()
        with self.assertRaisesRegex(SystemExit, 'Blocked'):
            self.gate.send(None, requests.Request('DELETE', 'https://api.hh.ru/resumes/1').prepare())

    def test_settings_are_validated_before_any_network(self):
        for count, low, high in ((0, 60, 180), (-1, 60, 180), (True, 60, 180),
                                 (1, 0, 60), (1, 180, 60), (1, 1, float('inf')),
                                 (1, float('nan'), 60), (1, 1, 86401)):
            with self.subTest(values=(count, low, high)), self.assertRaises(ValueError):
                validate_settings(count, low, high)
        validate_settings(1, 1, 1)
        with patch('auto_send.run') as runner, redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                main(['--max-responses', '0'])
            runner.assert_not_called()

    def test_limit_is_shared_across_round_robin_resumes(self):
        other = 'other-resume'
        resumes = [{'id': rid, 'title': rid, 'status': {'id': 'published'}}
                   for rid in (RESUME, other)]
        tool = Mock()
        tool.get_resumes.return_value = resumes
        tool.get_openai_settings.return_value = {}
        operation = SimpleNamespace(tool=tool)
        self.gate.original_vacancies = single_round_vacancy
        fetched = []

        def search(_operation, resume_id):
            for number in range(3):
                vacancy = VACANCY | {'id': resume_id + str(number)}
                fetched.append(vacancy['id'])
                yield vacancy

        def apply(resume, _user, _employers):
            for vacancy in self.gate.vacancies(operation, resume_id=resume['id']):
                self.gate.letter = LETTER
                self.gate.send(None, negotiation(vacancy['id'], resume_id=resume['id']))
            return False

        operation._apply_resume = apply
        with patch('send_all.selected_resume_context', return_value=nullcontext()), \
             patch('send_all.SystemRandom') as random, redirect_stdout(StringIO()):
            random.return_value.shuffle.side_effect = lambda _items: None
            with self.assertRaises(StopReview):
                apply_round_robin(operation, RESUME, {RESUME: 'Linux', other: 'SRE'}, search)
        self.assertEqual(fetched, [RESUME + '0', other + '0'])
        self.assertEqual(self.gate.counts['http_success'], 2)
        self.assertEqual(self.sender.call_count, 2)
        self.sleep.assert_called_once_with(75)

    def test_run_stops_before_next_vacancy_or_letter(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            profile = home / '.config/pocketnode-hh'
            profile.mkdir(parents=True)
            shutil.copyfile(Path(__file__).with_name('config.toml'), profile / 'config.toml')
            fetched = []

            def vacancies(_operation, **_kwargs):
                for number in range(5):
                    fetched.append(number)
                    yield VACANCY | {'id': str(number)}

            def fake_main(_argv):
                operation = Operation()
                operation.tool = SimpleNamespace(api_client=SimpleNamespace(get=lambda path: (
                    VACANCY | {'id': path.rsplit('/', 1)[-1], 'description': 'Linux'})))
                for vacancy in operation._get_vacancies(resume_id=RESUME):
                    OpenAIClient.complete(None, 'prompt')
                    requests.Session().send(negotiation(vacancy['id']))
                return 0

            with patch.dict(os.environ, {'CONFIG_DIR': str(profile), 'HH_PROFILE_ID': '.'}), \
                 patch('send.Path.home', return_value=home), \
                 patch('send.selected_resume_context', return_value=nullcontext()), \
                 patch.object(Operation, '_get_vacancies', vacancies), \
                 patch.object(OpenAIClient, 'complete', return_value=LETTER) as model, \
                 patch.object(requests.sessions.Session, 'send', self.sender), \
                 patch('send.main', side_effect=fake_main), redirect_stdout(StringIO()):
                self.assertEqual(run(arguments(str(profile), RESUME, 'Linux'), gate_factory=partial(
                    AutoGate, max_responses=2, sleep=self.sleep, uniform=self.uniform)), 0)
            self.assertEqual(fetched, [0, 1])
            self.assertEqual(model.call_count, 2)
            self.assertEqual(self.sender.call_count, 2)
            self.sleep.assert_called_once_with(75)


if __name__ == '__main__':
    unittest.main()
