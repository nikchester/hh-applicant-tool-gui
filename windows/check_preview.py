"""Offline tests for the HH preview network and prompt boundaries."""

import json
import unittest
from contextlib import contextmanager, nullcontext, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hh_applicant_tool.api.client import ApiClient
from hh_applicant_tool.operations.apply_vacancies import Operation

from preview import arguments
from preview_hh import (allowed_request, check_letter_language, check_letter_level,
                        enrich_prompt, letter_kwargs, run, vacancy_is_english)
import vacancy_filters
from vacancy_filters import DEFAULTS, Filters
from vacancy_level import senior_title_reason
from vacancy_salary import salary_rejection_reason

# The arguments of the HH search before vacancy filters existed (#15): the defaults must repeat them.
LEGACY_SEARCH_ARGUMENTS = [
    "--work-format", "REMOTE", "--currency", "RUR", "--salary", "80000",
    "--no-only-with-salary", "--search-field", "name", "--order-by", "publication_time",
]
LEGACY_PREVIEW_ARGUMENTS = [
    "--config-dir", "config", "--profile-id", ".", "--no-handle-captcha",
    "apply", "--dry-run", "--no-send-email", "--no-ai-filter", "--no-skip-tests",
    "--per-page", "10", "--total-pages", "1", "--max-responses", "1",
] + LEGACY_SEARCH_ARGUMENTS + ["--resume-id=resume", "--search=Linux"]


@contextmanager
def saved_filters(data=None, raw=None):
    """A disposable filters.json chosen through the variable the GUI sets for its worker."""
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "filters.json"
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
        elif data is not None:
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with patch.dict(os.environ, {"HH_APPLICANT_TOOL_FILTERS_FILE": str(path)}):
            yield path


class PreviewTests(unittest.TestCase):
    def test_newest_first_arguments(self):
        from hh_applicant_tool.tool import HHApplicantTool
        args = HHApplicantTool._create_parser().parse_args(arguments("config", "resume", "Linux"))
        self.assertEqual(args.order_by, "publication_time")

    def test_salary_maximum_checks_both_bounds_without_tax_deduction(self):
        for gross in (True, False, None):
            for bounds, rejected in (
                ({"from": 300000}, False), ({"to": 300000}, False),
                ({"from": 300001}, True), ({"to": 300001}, True),
                ({"from": 100000, "to": 400000}, True),
                ({"from": 100000, "to": 300000}, False),
            ):
                with self.subTest(gross=gross, bounds=bounds):
                    reason = salary_rejection_reason({"salary": bounds | {
                        "currency": "RUR", "gross": gross}})
                    self.assertEqual(reason is not None, rejected)

    def test_foreign_salary_uses_cached_hh_rates(self):
        client = ApiClient(session=Mock())
        client.get = Mock()
        client.get.return_value = {"currency": [
            {"code": "USD", "rate": 0.01}, {"code": "EUR", "rate": 0.009},
            {"code": "KZT", "rate": 5},
        ]}
        for currency, limit in (("USD", 3000), ("EUR", 2700), ("KZT", 1500000)):
            for field in ("from", "to"):
                with self.subTest(currency=currency, field=field):
                    self.assertIsNone(salary_rejection_reason(
                        {"salary": {field: limit, "currency": currency}}, client))
                    self.assertIn("300 000", salary_rejection_reason(
                        {"salary": {field: limit + 1, "currency": currency}}, client))
        self.assertIsNotNone(salary_rejection_reason(
            {"salary": {"from": 100, "currency": "UNKNOWN"}}, client))
        client.get.assert_called_once_with("/dictionaries")

    def test_currency_cache_is_isolated_per_real_client(self):
        first = ApiClient(session=Mock())
        second = ApiClient(session=Mock())
        first.get = Mock(return_value={"currency": [{"code": "USD", "rate": 0.01}]})
        second.get = Mock(return_value={"currency": [{"code": "USD", "rate": 0.005}]})
        vacancy = {"salary": {"from": 2000, "currency": "USD"}}
        for _ in range(2):
            self.assertIsNone(salary_rejection_reason(vacancy, first))
            self.assertIn("300 000", salary_rejection_reason(vacancy, second))
        first.get.assert_called_once_with("/dictionaries")
        second.get.assert_called_once_with("/dictionaries")

    def test_currency_load_failure_is_not_cached(self):
        client = ApiClient(session=Mock())
        client.get = Mock(side_effect=[RuntimeError("offline"), {
            "currency": [{"code": "USD", "rate": 0.01}]}])
        vacancy = {"salary": {"from": 2000, "currency": "USD"}}
        with self.assertRaisesRegex(SystemExit, "Cannot load HH currency rates"):
            salary_rejection_reason(vacancy, client)
        # A later explicit check may reload; the failed check never passes.
        self.assertIsNone(salary_rejection_reason(vacancy, client))
        self.assertIsNone(salary_rejection_reason(vacancy, client))
        self.assertEqual(client.get.call_count, 2)

    def test_ruble_salary_does_not_load_currency_rates(self):
        client = ApiClient(session=Mock())
        client.get = Mock(side_effect=AssertionError("Unexpected request"))
        for currency in ("RUR", "RUB"):
            self.assertIsNone(salary_rejection_reason(
                {"salary": {"from": 200000, "currency": currency}}, client))
        client.get.assert_not_called()

    def test_missing_or_invalid_exchange_rate_never_passes(self):
        vacancy = {"salary": {"from": 3000, "currency": "USD"}}
        self.assertIsNotNone(salary_rejection_reason(vacancy))
        for rate in (0, -1, None, "0.01", float("nan"), float("inf")):
            client = Mock()
            client.get.return_value = {"currency": [{"code": "USD", "rate": rate}]}
            self.assertIsNotNone(salary_rejection_reason(vacancy, client))
        client = Mock()
        client.get.side_effect = RuntimeError("offline")
        with self.assertRaisesRegex(SystemExit, "Cannot load HH currency rates"):
            salary_rejection_reason(vacancy, client)

    def test_salary_ceiling_is_compared_after_tax(self):
        def vacancy(ceiling, gross, currency="RUR"):
            return {"salary": {"from": 40000, "to": ceiling,
                               "currency": currency, "gross": gross}}

        self.assertIsNotNone(salary_rejection_reason(vacancy(80000, True)))
        self.assertIsNotNone(salary_rejection_reason(vacancy(79999, False)))
        self.assertIsNone(salary_rejection_reason(vacancy(80000, False)))
        self.assertIsNone(salary_rejection_reason(vacancy(80000, None)))
        self.assertIsNone(salary_rejection_reason(vacancy(92000, True)))
        self.assertIsNone(salary_rejection_reason(vacancy(None, True)))
        self.assertIsNotNone(salary_rejection_reason(vacancy(4200, True, "EUR")))

    def test_preview_skips_gross_ceiling_before_letter(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            profile = home / ".config/hh-applicant-tool-cli"
            profile.mkdir(parents=True)
            shutil.copyfile(Path(__file__).with_name("config.toml"), profile / "config.toml")
            vacancy = {"id": "123", "name": "Специалист технической поддержки",
                       "salary": {"from": 40000, "to": 80000, "gross": True,
                                  "currency": "RUR"}}

            def fake_main(_argv):
                self.assertEqual(list(Operation()._get_vacancies(resume_id="any")), [])
                return 0

            output = StringIO()
            with patch.dict(os.environ, {"CONFIG_DIR": str(profile), "HH_PROFILE_ID": "."}), \
                 patch("preview_hh.Path.home", return_value=home), \
                 patch("preview_hh.selected_resume_context", return_value=nullcontext()), \
                 patch.object(Operation, "_get_vacancies", return_value=iter([vacancy])), \
                 patch("preview_hh.main", side_effect=fake_main), \
                 redirect_stdout(output):
                self.assertEqual(run(arguments(str(profile), "resume", "Linux")), 0)
            self.assertIn("потолок зарплаты", output.getvalue())
            self.assertIn('"letters": 0', output.getvalue())

    def test_preview_skips_senior_before_upstream_letter(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            profile = home / ".config/hh-applicant-tool-cli"
            profile.mkdir(parents=True)
            shutil.copyfile(Path(__file__).with_name("config.toml"), profile / "config.toml")
            vacancy = {"id": "123", "name": "Senior DevOps", "employer": {"name": "Test"}}

            def fake_main(_argv):
                self.assertEqual(list(Operation()._get_vacancies(resume_id="any")), [])
                return 0

            output = StringIO()
            with patch.dict(os.environ, {"CONFIG_DIR": str(profile), "HH_PROFILE_ID": "."}), \
                 patch("preview_hh.Path.home", return_value=home), \
                 patch("preview_hh.selected_resume_context", return_value=nullcontext()), \
                 patch.object(Operation, "_get_vacancies", return_value=iter([vacancy])), \
                 patch("preview_hh.main", side_effect=fake_main), \
                 redirect_stdout(output):
                self.assertEqual(run(arguments(str(profile), "resume", "DevOps")), 0)
            self.assertIn("SKIPPED", output.getvalue())
            self.assertIn('"letters": 0', output.getvalue())

    def test_skipped_vacancies_do_not_use_up_the_three_letters(self):
        from hh_applicant_tool.ai.openai import OpenAIClient
        from preview_hh import PREVIEW_LETTERS, PREVIEW_VACANCIES
        self.assertIn(str(PREVIEW_VACANCIES), arguments("config", "resume", "Linux"))
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            profile = home / ".config/hh-applicant-tool-cli"
            profile.mkdir(parents=True)
            shutil.copyfile(Path(__file__).with_name("config.toml"), profile / "config.toml")
            vacancies = ([{"id": str(n), "name": "Senior DevOps"} for n in (1, 2)]
                         + [{"id": str(n), "name": "DevOps-инженер"} for n in range(3, 11)])
            seen = []

            def fake_main(_argv):
                operation = SimpleNamespace(api_client=Mock(
                    get=lambda path: {"id": path.rsplit("/", 1)[-1], "name": "DevOps"}))
                for vacancy in Operation._get_vacancies(operation, resume_id="any"):
                    seen.append(vacancy["id"])
                    OpenAIClient.complete(Mock(), "Напиши письмо")
                return 0

            output = StringIO()
            with patch.dict(os.environ, {"CONFIG_DIR": str(profile), "HH_PROFILE_ID": "."}), \
                 patch("preview_hh.Path.home", return_value=home), \
                 patch("preview_hh.selected_resume_context", return_value=nullcontext()), \
                 patch.object(Operation, "_get_vacancies", return_value=iter(vacancies)), \
                 patch.object(OpenAIClient, "complete", Mock(return_value="Здравствуйте! Готов обсудить вакансию.")), \
                 patch("preview_hh.main", side_effect=fake_main), \
                 redirect_stdout(output):
                self.assertEqual(run(arguments(str(profile), "resume", "DevOps")), 0)
            # Two Senior vacancies are skipped; three letters come from the next ones, and no more is read.
            self.assertEqual(seen, ["3", "4", "5"])
            self.assertEqual(output.getvalue().count("LETTER "), PREVIEW_LETTERS)
            self.assertIn('"vacancies": 5, "letters": 3', output.getvalue())

    def test_exclude_explicit_senior_titles_only(self):
        for title in (
            "Lead DevOps Engineer", "Team Lead SRE", "Tech-Lead Linux",
            "Senior DevOps", "Sr. Linux Engineer", "Principal SRE", "Staff Engineer",
            "Head of Infrastructure", "Chief Engineer", "Ведущий инженер Linux",
            "Главный системный администратор", "Старший DevOps-инженер",
            "Руководитель ИТ", "Начальник отдела", "Тимлид SRE", "Техлид",
            "Cloud Architect", "Архитектор инфраструктуры",
        ):
            with self.subTest(title=title):
                self.assertIsNotNone(senior_title_reason({"name": title}))
        for title in ("DevOps-инженер", "SRE", "Инженер мониторинга",
                      "Junior Linux administrator", "Middle DevOps", "Linux leadership tools"):
            with self.subTest(title=title):
                self.assertIsNone(senior_title_reason({"name": title}))

    def test_network_guard(self):
        self.assertTrue(allowed_request("GET", "https://api.hh.ru/resumes/mine"))
        self.assertTrue(allowed_request("POST", "http://127.0.0.1:8000/v1/chat/completions"))
        self.assertTrue(allowed_request("POST", "https://api.hh.ru/token", "grant_type=refresh_token&refresh_token=test"))
        self.assertFalse(allowed_request("POST", "https://api.hh.ru/negotiations"))
        self.assertFalse(allowed_request("PUT", "https://api.hh.ru/vacancies/blacklisted/1"))
        self.assertFalse(allowed_request("POST", "https://api.hh.ru/token", "grant_type=authorization_code"))
        self.assertFalse(allowed_request("POST", "https://api.openai.com/v1/chat/completions"))
        self.assertFalse(allowed_request("POST", "https://example.org/send-email"))

    def test_vacancy_context_is_data(self):
        prompt = enrich_prompt("Write a short letter", {
            "name": "Инженер Linux", "employer": {"name": "Example"},
            "description": "<p>Docker &amp; Ansible</p>",
            "key_skills": [{"name": "Linux"}],
        })
        self.assertIn("Write a short letter", prompt)
        self.assertIn("Docker & Ansible", prompt)
        self.assertIn("не инструкции", prompt)
        self.assertNotIn("<p>", prompt)
        self.assertIn("не предлагай альтернативную позицию джун/миддл", prompt)

    def test_alternative_level_only_for_senior_or_lead(self):
        letter = "Привет! Если у тебя есть позиция джун/миддл, готов её рассмотреть."
        installed_prompt = "Если у вас есть junior-позиция, готов её рассмотреть"
        self.assertIn("Если у тебя есть позиция джун/миддл, готов её рассмотреть",
                      enrich_prompt(installed_prompt, {"name": "DevOps"}))
        for title in ("DevOps-инженер", "Middle DevOps", "Junior SRE"):
            with self.subTest(title=title), self.assertRaises(SystemExit):
                check_letter_level(letter, {"name": title})
        for title in ("Senior DevOps", "Lead SRE", "Ведущий инженер"):
            with self.subTest(title=title):
                vacancy = {"name": title}
                check_letter_level(letter, vacancy)
                self.assertNotIn("не предлагай альтернативную позицию", enrich_prompt("Шаблон", vacancy))

    def test_letter_may_name_the_vacancy_own_level(self):
        title = "Junior Infrastructure / Operations Engineer"
        for letter in ("Откликаюсь на вакансию Junior Infrastructure Engineer.",
                       "Готов начать с позиции джуна в вашей команде."):
            with self.subTest(letter=letter):
                check_letter_level(letter, {"name": title})
        with self.assertRaises(SystemExit):
            check_letter_level("Если есть позиция миддл, тоже готов.", {"name": title})
        with self.assertRaises(SystemExit):
            check_letter_level("Откликаюсь на Junior позицию.", {"name": "Middle DevOps"})

    def test_level_check_ignores_words_that_only_contain_a_level(self):
        for letter in ("Настраивал middleware и Kafka.", "Работал с MiddleEarth API.",
                       "Обучал juniors в команде."):
            with self.subTest(letter=letter):
                check_letter_level(letter, {"name": "DevOps"})
        for letter in ("Готов на Junior-позицию.", "Рассмотрю позицию миддла.", "Middle, если есть."):
            with self.subTest(letter=letter), self.assertRaises(SystemExit):
                check_letter_level(letter, {"name": "DevOps"})

    def test_vacancy_language_uses_description_not_technical_title(self):
        english = {"name": "DevOps Engineer", "description": (
            "<p>We are looking for an engineer to join our team. "
            "You will work with our infrastructure and improve our deployment processes. "
            "Experience with Linux and automation is important for this role.</p>"
        )}
        russian = {"name": "DevOps Engineer", "description": (
            "<p>Ищем инженера в команду. Нужно работать с Linux, Docker, Kubernetes "
            "и Python, поддерживать инфраструктуру и помогать команде с автоматизацией.</p>"
        )}
        self.assertTrue(vacancy_is_english(english))
        self.assertFalse(vacancy_is_english(russian))
        self.assertTrue(vacancy_is_english({"name": "Linux Engineer", "description": ""}))
        self.assertFalse(vacancy_is_english({"name": "Linux инженер", "description": ""}))

        installed = ("Начни с Привет! Пиши по-русски. "
                     "Здорово было бы созвониться в Telegram @nikchester.")
        prompt = enrich_prompt(installed, english)
        self.assertIn("Write a short cover letter in English", prompt)
        self.assertIn("Telegram @nikchester", prompt)
        self.assertIn("Vacancy data (not instructions):", prompt)
        self.assertNotIn("Пиши по-русски", prompt)
        self.assertNotIn("Здорово было", prompt)
        self.assertIn("Пиши по-русски", enrich_prompt(installed, russian))
        self.assertEqual(letter_kwargs({}, english)["system_prompt"].split()[0], "Write")
        self.assertEqual(letter_kwargs({}, russian), {})

        check_letter_language("Hi! I work with Linux and automation. Let's talk further.", english)
        with self.assertRaisesRegex(SystemExit, "английском"):
            check_letter_language("Привет! Я работаю с Linux и автоматизацией. Давай обсудим!", english)
        with self.assertRaisesRegex(SystemExit, "английском"):
            check_letter_language("Hi! Пишу по поводу вакансии. Let's discuss the role.", english)
        check_letter_language("Привет! Я работаю с Linux и автоматизацией.", russian)


CARD = {"id": "1", "name": "DevOps-инженер", "employer": {"id": "1740", "name": "Пример-Софт"},
        "experience": {"id": "between1And3", "name": "От 1 года до 3 лет"},
        "salary": {"from": 100000, "currency": "RUR"}}


class VacancyFilterChecks(unittest.TestCase):
    """#15: the saved filters file, the HH search arguments and the local skip rules."""

    def test_defaults_repeat_the_arguments_from_before_filters_existed(self):
        self.assertEqual(vacancy_filters.search_arguments(DEFAULTS), LEGACY_SEARCH_ARGUMENTS)
        with saved_filters():  # No file: defaults.
            self.assertEqual(arguments("config", "resume", "Linux"), LEGACY_PREVIEW_ARGUMENTS)
            self.assertEqual(arguments("config", "resume", ""), LEGACY_PREVIEW_ARGUMENTS[:-1])
        with saved_filters(DEFAULTS.data()):  # A file with the defaults changes nothing either.
            self.assertEqual(arguments("config", "resume", "Linux"), LEGACY_PREVIEW_ARGUMENTS)

    def test_search_arguments_follow_the_saved_filters_and_parse_upstream(self):
        from hh_applicant_tool.tool import HHApplicantTool
        data = {"salary_minimum": 120000, "only_with_salary": True, "period": 7,
                "not_from_agency": True, "accredited_it": True}
        with saved_filters(data):
            argv = arguments("config", "resume", "Linux")
            filters = vacancy_filters.load()
        self.assertEqual(argv[argv.index("--salary") + 1], "120000")
        self.assertIn("--only-with-salary", argv)
        self.assertEqual(argv[argv.index("--period") + 1], "7")
        self.assertEqual(argv[argv.index("--label") + 1:argv.index("--label") + 3],
                         ["not_from_agency", "accredited_it"])
        args = HHApplicantTool._create_parser().parse_args(argv)
        self.assertEqual((args.salary, args.only_with_salary, args.period, args.label),
                         (120000, True, 7, ["not_from_agency", "accredited_it"]))
        self.assertTrue(vacancy_filters.search_matches(args, filters))
        # The same argv no longer matches once a filter changes.
        self.assertFalse(vacancy_filters.search_matches(args, Filters(**{**filters.__dict__, "period": 3})))
        self.assertFalse(vacancy_filters.search_matches(args, DEFAULTS))

    def test_the_file_is_checked_strictly(self):
        good = DEFAULTS.data()
        bad = {
            "unknown key": {"stop_word": []},
            "minimum as text": {"salary_minimum": "80000"},
            "minimum as boolean": {"salary_minimum": True},
            "negative minimum": {"salary_minimum": -1},
            "ceiling below minimum": {"salary_minimum": 100000, "salary_ceiling": 90000},
            "flag as number": {"only_with_salary": 1},
            "experience outside the dictionary": {"experience": ["noExperience", "senior"]},
            "experience empty": {"experience": []},
            "experience repeated": {"experience": ["noExperience", "noExperience"]},
            "period outside the dictionary": {"period": 5},
            "period as text": {"period": "7"},
            "stop words as text": {"stop_words": "1С"},
            "empty stop word": {"stop_words": ["  "]},
            "stop word without letters": {"stop_words": ["..."]},
            "stop words repeat": {"stop_words": ["1С", "1с"]},
            "employer id not digits": {"hidden_employers": [{"id": "17a0", "name": ""}]},
            "employer id a link": {"hidden_employers": [{"id": "hh.ru/employer/1740", "name": ""}]},
            "employer id as number": {"hidden_employers": [{"id": 1740, "name": ""}]},
            "employer extra field": {"hidden_employers": [{"id": "1740", "name": "", "x": 1}]},
            "employers repeat": {"hidden_employers": [{"id": "1", "name": ""}, {"id": "1", "name": "x"}]},
        }
        for title, change in bad.items():
            with self.subTest(title), saved_filters(good | change):
                with self.assertRaisesRegex(SystemExit, "Фильтры вакансий.*Ничего не отправлено"):
                    vacancy_filters.load()
        for raw in ("{", "[]", "null", '{"salary_minimum": NaN}', "\xff"):
            with self.subTest(raw=raw), saved_filters(raw=raw):
                with self.assertRaisesRegex(SystemExit, "Фильтры вакансий.*Ничего не отправлено"):
                    vacancy_filters.load()
        with saved_filters(good | {"salary_ceiling": None, "period": 30, "experience": ["moreThan6"],
                                   "hidden_employers": [{"id": "1740", "name": "Пример-Софт"}]}):
            filters = vacancy_filters.load()
        self.assertEqual((filters.salary_ceiling, filters.period, filters.experience),
                         (None, 30, ("moreThan6",)))
        # A partial file keeps the defaults for the missing keys.
        with saved_filters({"stop_words": ["1С"]}):
            self.assertEqual(vacancy_filters.load(), Filters(stop_words=("1С",)))

    def test_a_damaged_file_stops_a_preview_before_any_request(self):
        with saved_filters(raw="{"), patch("requests.sessions.Session.send", side_effect=AssertionError("No network")):
            with self.assertRaisesRegex(SystemExit, "Фильтры вакансий"):
                arguments("config", "resume", "Linux")

    def test_saved_file_round_trips(self):
        filters = Filters(salary_minimum=90000, salary_ceiling=None, only_with_salary=True,
                          exclude_senior=False, experience=("noExperience", "between1And3"),
                          stop_words=("1С", "дружный коллектив"), period=3, accredited_it=True,
                          hidden_employers=({"id": "1740", "name": "Пример-Софт"},))
        with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)):
            path = Path(temp) / "state" / "filters.json"
            vacancy_filters.save(filters, path)
            self.assertEqual(vacancy_filters.read(path), filters)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), filters.data())
        self.assertEqual(vacancy_filters.parse(DEFAULTS.data()), DEFAULTS)

    def test_stop_words_are_whole_words_and_phrases_without_case(self):
        words = ("1С", "Bitrix", "дружный коллектив", "C++", "ёлка")
        for text, found in (
            ("Программист 1С", "1С"), ("программист 1с-разработчик", "1С"), ("BITRIX разработчик", "Bitrix"),
            ("Ищем в Дружный  коллектив", "дружный коллектив"), ("C++ developer", "C++"),
            ("Ёлка", "ёлка"), ("ЕЛКА", "ёлка"),
        ):
            with self.subTest(text):
                self.assertEqual(vacancy_filters.stop_word_in(text, words), found)
        for text in ("Инженер 1СО", "Bitrixa", "Недружный коллектив", "дружный  и коллектив",
                     "C+", "", None):
            with self.subTest(text):
                self.assertIsNone(vacancy_filters.stop_word_in(text, words))
        # No regular expressions: a dot or a star is an ordinary character.
        self.assertIsNone(vacancy_filters.stop_word_in("Linux", ("L.nux",)))
        self.assertIsNone(vacancy_filters.stop_word_in("Linux", (".*",)))
        self.assertEqual(vacancy_filters.stop_word_in("Node.js", ("node.js",)), "node.js")

    def test_each_local_rule_gives_its_reason(self):
        def reason(vacancy=None, **filters):
            return vacancy_filters.check(CARD | (vacancy or {}), Filters(**filters))

        self.assertEqual(reason(), (None, []))
        self.assertIn("уровень выше", reason({"name": "Senior DevOps"})[0])
        self.assertIsNone(reason({"name": "Senior DevOps"}, exclude_senior=False)[0])
        self.assertEqual(reason(hidden_employers=({"id": "1740", "name": ""},))[0], "компания скрыта")
        self.assertIsNone(reason(hidden_employers=({"id": "17400", "name": ""},))[0])
        self.assertIsNone(reason({"employer": {"name": "Без номера"}}, hidden_employers=({"id": "1", "name": ""},))[0])
        self.assertEqual(reason({"name": "Программист 1С"}, stop_words=("1С",))[0],
                         "стоп-слово «1С» в названии вакансии")
        self.assertEqual(reason({"employer": {"id": "5", "name": "Битрикс Групп"}}, stop_words=("битрикс",))[0],
                         "стоп-слово «битрикс» в названии компании")
        self.assertEqual(reason({"experience": {"id": "moreThan6"}}, experience=("noExperience",))[0],
                         "требуется опыт более 6 лет")
        self.assertEqual(reason(experience=("noExperience",))[0], "требуется опыт 1–3 года")
        self.assertIsNone(reason(experience=("noExperience", "between1And3"))[0])
        self.assertIn("не достигает 100 000", reason({"salary": {"to": 90000, "currency": "RUR"}},
                                                        salary_minimum=100000)[0])
        self.assertIn("выше 150 000", reason({"salary": {"from": 160000, "currency": "RUR"}},
                                              salary_ceiling=150000)[0])
        self.assertIsNone(reason({"salary": {"from": 900000, "currency": "RUR"}}, salary_ceiling=None)[0])
        self.assertIn("выше 300 000", reason({"salary": {"from": 900000, "currency": "RUR"}})[0])

    def test_missing_experience_keeps_the_vacancy_with_a_note(self):
        subset = Filters(experience=("noExperience",))
        for card in ({"experience": None}, {"experience": {}}, {"experience": {"id": "newValue"}},
                     {"experience": "between1And3"}):
            with self.subTest(card):
                reason, notes = vacancy_filters.check({k: v for k, v in CARD.items() if k != "experience"} | card, subset)
                self.assertIsNone(reason)
                self.assertEqual(len(notes), 1)
                self.assertIn("проверка опыта пропущена", notes[0])
        reason, notes = vacancy_filters.check({k: v for k, v in CARD.items() if k != "experience"}, subset)
        self.assertEqual((reason, len(notes)), (None, 1))
        # With every experience allowed there is nothing to check and nothing to note.
        self.assertEqual(vacancy_filters.check({"name": "Linux"}, DEFAULTS), (None, []))

    def test_employer_lines_and_summary(self):
        for line, expected in (("1740", ("1740", "")), ("1740 Пример  Софт", ("1740", "Пример Софт")),
                               ("https://hh.ru/employer/1740?hhtmFrom=vacancy", ("1740", "")),
                               ("hh.ru/employer/1740/ Пример", ("1740", "Пример"))):
            with self.subTest(line):
                item = vacancy_filters.parse_employer_line(line)
                self.assertEqual((item["id"], item["name"]), expected)
        for line in ("abc", "Пример 1740", "employer/12x", "hh.ru/vacancy/1740", ""):
            with self.subTest(line), self.assertRaises(ValueError):
                vacancy_filters.parse_employer_line(line)
        self.assertEqual(vacancy_filters.summary(DEFAULTS), "удалённо · от 80 000 ₽ · без Senior/Lead")
        self.assertEqual(vacancy_filters.summary(Filters(
            exclude_senior=True, stop_words=("а", "б", "в"), salary_ceiling=None)),
            "удалённо · от 80 000 ₽ · без потолка · без Senior/Lead · стоп-слов: 3")

    def preview(self, vacancies, **filters):
        """Run a preview over these vacancies; return (output, letters asked of the model)."""
        from hh_applicant_tool.ai.openai import OpenAIClient
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            profile = home / ".config/hh-applicant-tool-cli"
            profile.mkdir(parents=True)
            shutil.copyfile(Path(__file__).with_name("config.toml"), profile / "config.toml")
            asked = []

            def fake_main(_argv):
                operation = SimpleNamespace(api_client=Mock(get=lambda path: {
                    "id": path.rsplit("/", 1)[-1], "name": "DevOps",
                    "salary": {"from": 100000, "currency": "RUR"}}))
                for vacancy in Operation._get_vacancies(operation, resume_id="any"):
                    asked.append(vacancy["id"])
                    OpenAIClient.complete(Mock(), "Напиши письмо")
                return 0

            output = StringIO()
            with saved_filters(DEFAULTS.data() | filters), \
                 patch.dict(os.environ, {"CONFIG_DIR": str(profile), "HH_PROFILE_ID": "."}), \
                 patch("preview_hh.Path.home", return_value=home), \
                 patch("preview_hh.selected_resume_context", return_value=nullcontext()), \
                 patch.object(Operation, "_get_vacancies", return_value=iter(vacancies)), \
                 patch.object(OpenAIClient, "complete", Mock(return_value="Здравствуйте! Готов обсудить вакансию.")), \
                 patch("preview_hh.main", side_effect=fake_main), redirect_stdout(output):
                self.assertEqual(run(arguments(str(profile), "resume", "DevOps")), 0)
        return output.getvalue(), asked

    def test_preview_skips_by_local_rules_before_the_model(self):
        cards = [CARD | {"id": "1", "name": "Инженер 1С"},
                 CARD | {"id": "2", "employer": {"id": "999", "name": "Скрытая"}},
                 CARD | {"id": "3", "experience": {"id": "moreThan6"}},
                 CARD | {"id": "4", "employer": {"id": "5", "name": "Дружный Коллектив ООО"}},
                 CARD | {"id": "5", "salary": {"to": 95000, "currency": "RUR"}},
                 CARD | {"id": "6"}]
        output, asked = self.preview(
            cards, stop_words=["1с", "дружный коллектив"], hidden_employers=[{"id": "999", "name": ""}],
            experience=["noExperience", "between1And3", "between3And6"], salary_minimum=96000,
            salary_ceiling=150000)
        self.assertEqual(asked, ["6"])
        self.assertEqual(output.count("SKIPPED"), 5)
        for reason in ("стоп-слово «1с» в названии вакансии", "компания скрыта", "требуется опыт более 6 лет",
                       "стоп-слово «дружный коллектив» в названии компании", "не достигает 96 000"):
            self.assertIn(reason, output)
        self.assertIn('"vacancies": 6, "letters": 1', output)

    def test_preview_notes_a_missing_experience_and_changes_nothing_by_default(self):
        bare = {k: v for k, v in CARD.items() if k != "experience"}
        output, asked = self.preview([bare], experience=["noExperience"])
        self.assertEqual(asked, ["1"])
        self.assertIn("Фильтры: в выдаче HH нет требуемого опыта", output)
        output, asked = self.preview([bare, CARD | {"id": "2", "name": "Senior DevOps"}])
        self.assertEqual(asked, ["1"])
        self.assertNotIn("Фильтры:", output)

    def test_preview_refuses_arguments_that_differ_from_the_saved_filters(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            profile = home / ".config/hh-applicant-tool-cli"
            profile.mkdir(parents=True)
            shutil.copyfile(Path(__file__).with_name("config.toml"), profile / "config.toml")
            with saved_filters({"period": 7}), \
                 patch.dict(os.environ, {"CONFIG_DIR": str(profile), "HH_PROFILE_ID": "."}), \
                 patch("preview_hh.Path.home", return_value=home), \
                 patch("preview_hh.main", side_effect=AssertionError("Must not start")):
                argv = arguments(str(profile), "resume", "DevOps")
                for extra in (["--excluded-filter", "junior"], ["--excluded-employer-id", "1740"],
                              ["--experience", "noExperience"], ["--ai-filter"]):
                    with self.subTest(extra=extra), self.assertRaisesRegex(SystemExit, "Unsafe preview"):
                        run(argv + extra)
                changed = [arg for arg in argv if arg not in ("--period", "7")]
                with self.assertRaisesRegex(SystemExit, "Unsafe preview"):
                    run(changed)
                with self.assertRaisesRegex(SystemExit, "Unsafe preview"):
                    run(argv[:argv.index("--period") + 1] + ["30"] + argv[argv.index("--period") + 2:])


if __name__ == "__main__":
    unittest.main(verbosity=2)
