"""Manual, per-vacancy HH sending, capped at three reviewed attempts."""

import os
from pathlib import Path
import sys
import tomllib
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests
from hh_applicant_tool.ai.openai import OpenAIClient
from hh_applicant_tool.main import main
from hh_applicant_tool.operations.apply_vacancies import Operation
from hh_applicant_tool.tool import HHApplicantTool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "termux"))
from preview_hh import allowed_request, enrich_prompt  # noqa: E402
from preview import arguments as preview_arguments  # noqa: E402


def arguments(config, resume_id, search):
    argv = preview_arguments(config, resume_id, search)
    argv[argv.index("--dry-run")] = "--no-dry-run"
    argv[argv.index("--max-responses") + 1] = "3"
    return argv


def validate(argv):
    args = HHApplicantTool._create_parser().parse_args(argv)
    expected = Path.home() / ".config/pocketnode-hh"
    if os.environ.get("CONFIG_DIR") != str(expected) or os.environ.get("HH_PROFILE_ID") != ".":
        raise SystemExit("Dedicated profile environment is required")
    if not (
        args.dry_run is False and args.send_email is False and args.ai_filter is False
        and args.skip_tests is True and args.captcha_handling is False
        and args.total_pages == 1 and args.per_page == 3 and args.max_responses == 3
        and args.resume_id and args.text and args.verbosity == 0
        and args.work_format == ["REMOTE"] and args.currency == "RUR"
        and args.salary == 80000 and args.only_with_salary is True
        and args.search_field == ["name"] and args.force_cover_letter is True
        and args.use_ai is True and args.excluded_filter is None
    ):
        raise SystemExit("Unsafe send arguments; refusing to run")
    config = tomllib.loads((expected / "config.toml").read_text(encoding="utf-8"))
    if config["openai"]["default"]["base_url"] != "http://127.0.0.1:8000/v1":
        raise SystemExit("Only the local model gateway is allowed")
    if config.get("general", {}).get("cover_letter_resume_id") != args.resume_id:
        raise SystemExit("Candidate facts do not belong to the selected resume")
    return args


def salary_text(salary):
    if not salary:
        return "не указана"
    minimum, maximum = salary.get("from"), salary.get("to")
    currency = {"RUR": "₽", "EUR": "€", "USD": "$"}.get(
        salary.get("currency"), salary.get("currency") or ""
    )
    if minimum is not None and maximum is not None:
        amount = f"{minimum:,}–{maximum:,}"
    elif minimum is not None:
        amount = f"от {minimum:,}"
    elif maximum is not None:
        amount = f"до {maximum:,}"
    else:
        amount = "сумма не указана"
    tax = {True: "до налогов", False: "на руки"}.get(salary.get("gross"), "налоги не указаны")
    return f"{amount.replace(',', ' ')} {currency} ({tax})".strip()


def vacancy_lines(vacancy, number):
    return [
        f"ВАКАНСИЯ {number} ИЗ 3",
        f"Компания: {((vacancy.get('employer') or {}).get('name') or 'не указана')}",
        f"Вакансия: {vacancy.get('name') or 'не указана'}",
        f"Зарплата: {salary_text(vacancy.get('salary'))}",
        f"Ссылка: {vacancy.get('alternate_url') or 'не указана'}",
    ]


class ApprovalGate:
    def __init__(self, resume_id, sender, approve=input):
        self.resume_id = resume_id
        self.sender = sender
        self.approve = approve
        self.current = None
        self.letter = None
        self.counts = {"vacancies": 0, "letters": 0, "attempts": 0, "http_success": 0}

    def vacancies(self, operation, *args, **kwargs):
        for vacancy in self.original_vacancies(operation, *args, **kwargs):
            self.counts["vacancies"] += 1
            if self.counts["vacancies"] > 3:
                raise SystemExit("Three-vacancy limit exceeded")
            self.current = vacancy
            self.operation = operation
            self.letter = None
            reason = (
                "уже есть отклик или отказ в HH" if vacancy.get("relations") else
                "вакансия в архиве" if vacancy.get("archived") else
                "есть тестовое задание" if vacancy.get("has_test") else
                "отклик ведёт на внешний сайт" if vacancy.get("response_url") else None
            )
            if reason:
                print("\n" + "=" * 60, flush=True)
                print("\n".join(vacancy_lines(vacancy, self.counts["vacancies"])), flush=True)
                print(f"Статус: пропускаем — {reason}", flush=True)
            else:
                company = (vacancy.get("employer") or {}).get("name") or "не указана"
                print(f"\nНайдена вакансия {self.counts['vacancies']} из 3: "
                      f"{vacancy.get('name') or 'не указана'} — {company}. "
                      "Готовим письмо для проверки...", flush=True)
            yield vacancy

    def complete(self, client, prompt, *args, **kwargs):
        if self.current is None or self.counts["letters"] >= 3:
            raise SystemExit("Unexpected letter generation")
        try:
            vacancy = self.operation.api_client.get("/vacancies/" + self.current["id"])
            letter = self.original_complete(client, enrich_prompt(prompt, vacancy), *args, **kwargs)
        except Exception:
            raise SystemExit("Stopped after model or vacancy-detail failure") from None
        if not isinstance(letter, str) or not letter.strip():
            raise SystemExit("Stopped after empty model response")
        self.letter = letter
        self.counts["letters"] += 1
        return letter

    def send(self, session, request, **kwargs):
        if not self._negotiation(request):
            if not allowed_request(request.method, request.url, request.body):
                raise SystemExit("Blocked an unexpected network request")
            try:
                response = self.sender(session, request, **kwargs)
            except requests.RequestException:
                raise SystemExit("Network failure; stopped without automatic retry") from None
            if response.status_code >= 400:
                raise SystemExit(f"Stopped after HTTP {response.status_code}")
            return response

        if self.counts["attempts"] >= 3:
            raise SystemExit("Three-send limit reached")
        if (self.current is None or self.current.get("relations")
                or self.current.get("has_test") or self.current.get("archived")
                or self.current.get("response_url")):
            raise SystemExit("Cannot send to a responded or test vacancy")
        if self.letter is None:
            raise SystemExit("Cannot send without reviewing a generated letter")
        if not isinstance(request.body, (bytes, str)):
            raise SystemExit("Unexpected HH request body")
        body = request.body.decode("utf-8") if isinstance(request.body, bytes) else request.body
        params = parse_qs(body, keep_blank_values=True, strict_parsing=True)
        if params != {
            "resume_id": [self.resume_id],
            "vacancy_id": [self.current["id"]],
            "message": [self.letter],
        }:
            raise SystemExit("HH payload differs from the reviewed vacancy or letter")
        if request.headers.get("Content-Type", "").split(";")[0] != "application/x-www-form-urlencoded":
            raise SystemExit("Unexpected HH content type")

        print("\n" + "-" * 60, flush=True)
        print("ОТКЛИК НА ВАКАНСИЮ\n" + "\n".join(
            vacancy_lines(self.current, self.counts["vacancies"])[1:]
        ), flush=True)
        print("Резюме: " + self.resume_id, flush=True)
        print("\nСопроводительное письмо (будет отправлено дословно):\n", flush=True)
        print(params["message"][0], flush=True)
        print("-" * 60, flush=True)
        try:
            answer = self.approve("Отправить этот отклик? [yes/no]: ")
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("No approval; stopped") from None
        if not isinstance(answer, str) or answer.strip().casefold() not in {"yes", "y"}:
            raise SystemExit("No approval; stopped")
        self.counts["attempts"] += 1  # Count before network: an uncertain result cannot be retried.
        self.letter = None  # One approval per attempt.
        try:
            response = self.sender(session, request, **kwargs)
        except requests.RequestException:
            raise SystemExit("Send outcome uncertain; check HH manually before any retry") from None
        if not 200 <= response.status_code < 300:
            raise SystemExit(
                f"Send returned HTTP {response.status_code}; check HH manually before any retry"
            )
        self.counts["http_success"] += 1
        print(f"HH вернул HTTP {response.status_code} для вакансии {self.current['id']}", flush=True)
        return response

    @staticmethod
    def _negotiation(request):
        url = urlsplit(request.url)
        return (request.method == "POST" and url.scheme == "https"
                and url.hostname == "api.hh.ru" and url.port is None
                and url.path == "/negotiations" and not url.query
                and not url.fragment and not url.username and not url.password)


def run(argv, approve=input):
    args = validate(argv)
    gate = ApprovalGate(args.resume_id, requests.sessions.Session.send, approve)
    gate.original_vacancies = Operation._get_vacancies
    gate.original_complete = OpenAIClient.complete
    def guarded_send(session, request, **kwargs):
        return gate.send(session, request, **kwargs)

    def visible_vacancies(operation, *args, **kwargs):
        return gate.vacancies(operation, *args, **kwargs)

    def visible_letter(client, *args, **kwargs):
        return gate.complete(client, *args, **kwargs)

    print("РЕЖИМ ОТПРАВКИ: до трёх вакансий; каждый отклик требует отдельного подтверждения yes/no.", flush=True)
    try:
        with patch.object(requests.sessions.Session, "send", guarded_send), \
             patch.object(Operation, "_get_vacancies", visible_vacancies), \
             patch.object(OpenAIClient, "complete", visible_letter):
            return main(argv)
    finally:
        counts = gate.counts
        print("\n" + "=" * 60, flush=True)
        print("ИТОГ: просмотрено {vacancies}, подготовлено писем {letters}, "
              "попыток отправки {attempts}, ответов HH 2xx {http_success}".format(**counts), flush=True)
        if gate.counts["attempts"] != gate.counts["http_success"]:
            print("Результат отправки может быть неопределённым. Перед повтором проверь отклики в HH.", flush=True)


if __name__ == "__main__":
    sys.exit(run(arguments(
        os.environ.get("CONFIG_DIR", ""),
        os.environ.get("POCKETNODE_HH_RESUME_ID", ""),
        os.environ.get("POCKETNODE_HH_SEARCH", ""),
    )))
