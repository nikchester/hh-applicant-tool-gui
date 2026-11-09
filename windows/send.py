"""Manual, per-vacancy HH sending with an explicit decision for each letter."""

from contextlib import nullcontext
import ai_provider
import os
from pathlib import Path
import sys
import time
import tomllib
from unittest.mock import patch
from urllib.parse import parse_qs, parse_qsl, urlsplit

import requests
from hh_applicant_tool.ai.base import AIError
from hh_applicant_tool.api import Redirect
from hh_applicant_tool.ai.openai import OpenAIClient
from hh_applicant_tool.main import main
from hh_applicant_tool.operations.apply_vacancies import Operation
from hh_applicant_tool.tool import HHApplicantTool

from preview_hh import (allowed_request, check_letter_language, check_letter_level,
                        enrich_prompt, letter_kwargs)
from preview import arguments as preview_arguments  # noqa: E402
from resume_context import selected_resume_context
from vacancy_level import senior_title_reason
from vacancy_salary import salary_rejection_reason
from send_all import apply_round_robin, load_searches, single_round_vacancy
from manual_captcha import (ManualCaptcha, captcha_url, form_captcha_state, hh_url,
                            permits_token_refresh)
from gui_protocol import RETRY_NOTICES


class StopReview(BaseException):
    """Leave the upstream vacancy loop without another request."""


class SkippedTest(Exception):
    """User declined a test application; upstream logs it and moves on."""


# Upstream answers questions with links using joke text about hacking and theft.
URL_TASK_ANSWER = "Внешние ссылки из теста не открываю. Готов обсудить это задание в чате HH."
TEST_ANSWER_LIMIT = 2000

# The gateway reports a connect/TLS failure only before any model request byte
# left it, so a later round cannot duplicate a generation or an application.
GENERATION_ROUNDS = 3
GENERATION_RETRY_DELAY = 30
# Two vacancies in a row without a letter mean the provider is down: stop.
GENERATION_SKIP_LIMIT = 2
# HH may show a fresh application in the vacancy relations only after a delay.
SEND_CHECK_DELAY = 15


def _connect_failure(error):
    """Return the gateway's bounded connect-failure record, or None."""
    response = getattr(error.__cause__, "response", None)
    if not isinstance(response, requests.Response):
        return None
    try:
        failure = response.json()["error"]["generation_failure"]
        attempts = failure["attempts"]
        if (failure["kind"] in {"connect_timeout", "tls_handshake_timeout"}
                and type(attempts) is int and 2 <= attempts <= 10
                and failure["connect_retry_exhausted"] is True):
            return failure
    except (ValueError, KeyError, TypeError):
        pass
    return None


def generation_failure_detail(error):
    """Read only bounded metadata, never upstream exception text or model input."""
    cause = error.__cause__
    response = getattr(cause, "response", None)
    if isinstance(response, requests.Response):
        status = response.status_code
        try:
            failure = _connect_failure(error)
            if failure is not None:
                attempts = failure["attempts"]
                request_id = failure.get("request_id", "")
                safe_id = (request_id if isinstance(request_id, str) and len(request_id) == 32
                           and all(c in "0123456789abcdef" for c in request_id) else "unknown")
                stage = ("TLS handshake (connection establishment) timed out"
                         if failure["kind"] == "tls_handshake_timeout" else "connect timeout")
                return (f"ChatGPT {stage} after {attempts} attempts; request_id=" + safe_id
                        + "; check gateway logs and connectivity; proxy/VPN cause is not established")
        except (ValueError, KeyError, TypeError):
            pass
        return f"local model gateway HTTP {status}; check ChatMock logs; no automatic replay"
    return "model generation failed; check ChatMock logs; no automatic replay"


def send_failure_detail(error, vacancy_id):
    """Never stringify transport exceptions: they can contain credentials or URLs."""
    reasons = (
        (requests.ConnectTimeout, "connection establishment timed out"),
        (requests.ReadTimeout, "timed out waiting for HH response"),
        (requests.exceptions.SSLError, "TLS handshake or certificate failure; keep TLS verification enabled"),
        (requests.exceptions.ProxyError, "proxy connection failed; check proxy connectivity"),
        (requests.ConnectionError, "connection failed or dropped; check network connectivity"),
        (requests.Timeout, "network timeout"),
        (requests.exceptions.ChunkedEncodingError, "incomplete HH response"),
        (requests.RequestException, "request transport failed"),
    )
    kind, reason = next((kind.__name__, reason) for kind, reason in reasons
                        if isinstance(error, kind))
    # HH IDs are decimal; do not echo arbitrary upstream values into diagnostics.
    vacancy_id = str(vacancy_id)
    safe_id = (vacancy_id if vacancy_id.isascii() and vacancy_id.isdecimal()
               and len(vacancy_id) <= 20 else "unknown")
    return f"vacancy_id={safe_id}; {kind}: {reason}"


RESPONSE_MARKERS = (
    ("captcha", ("captcha", "капч")),
    ("xsrf", ("xsrf",)),
    ("login", ("/account/login", "account_login")),
    ("antibot", ("ddos-guard", "qrator", "cf-chl", "cloudflare")),
    ("forbidden", ("access denied", "forbidden", "доступ ограничен", "доступ запрещ")),
)


def _safe_token(value, limit=64, extra="-_./+"):
    return (value if isinstance(value, str) and 0 < len(value) <= limit
            and all(c.isascii() and (c.isalnum() or c in extra) for c in value) else None)


def response_failure_detail(response):
    """Summarize an HH error answer from allowlisted features only.

    The body may hold personal data, cookies or the _xsrf token, so neither it
    nor raw header values are echoed: only fixed marker names, safe tokens and sizes.
    """
    parts = []
    headers = getattr(response, "headers", None) or {}
    media = _safe_token(str(headers.get("Content-Type", "")).split(";")[0].strip().lower())
    parts.append("content_type=" + (media or "unknown"))
    location = headers.get("Location")
    if location:
        url = urlsplit(str(location))
        host = (url.hostname or "").lower()
        if host and host != "hh.ru" and not host.endswith(".hh.ru"):
            parts.append("location=external")
        else:
            parts.append("location=" + (_safe_token(url.path, 100) or "unknown"))
    request_id = _safe_token(str(headers.get("X-Request-Id", "")), extra="-")
    if request_id:
        parts.append("request_id=" + request_id)
    body = getattr(response, "content", b"") or b""
    parts.append(f"body_bytes={len(body)}")
    try:
        data = response.json() if callable(getattr(response, "json", None)) else None
    except ValueError:
        data = None
    if isinstance(data, dict):
        found = []
        errors = data.get("errors")
        for error in errors if isinstance(errors, list) else ():
            if isinstance(error, dict):
                found += [error.get("type"), error.get("value")]
        found.append(data.get("error"))
        safe = list(dict.fromkeys(t for t in map(_safe_token, found) if t))[:6]
        if safe:
            parts.append("json_errors=" + ",".join(safe))
        # Key names (never values) show the shape of unfamiliar HH web answers.
        keys = []
        for key, value in data.items():
            if _safe_token(key, 40, "-_"):
                keys.append(key)
                if isinstance(value, dict):
                    keys += [key + "." + k for k in value if _safe_token(k, 40, "-_")]
        if keys:
            parts.append("json_keys=" + ",".join(keys[:20]))
    text = body[:200_000].decode("utf-8", errors="ignore").casefold()
    markers = [name for name, needles in RESPONSE_MARKERS if any(n in text for n in needles)]
    if markers:
        parts.append("markers=" + ",".join(markers))
    return "; ".join(parts)


def arguments(config, resume_id, search):
    argv = preview_arguments(config, resume_id, search)
    argv[argv.index("--dry-run")] = "--no-dry-run"
    for option, value in (("--per-page", "20"), ("--total-pages", "100")):
        argv[argv.index(option) + 1] = value
    index = argv.index("--max-responses")
    del argv[index:index + 2]  # None means no application-count limit in HH 2.3.7.
    return argv


def all_resume_arguments(config, configured_id, *, generic=False):
    mapping = load_searches()
    if generic:
        configured_id = next(iter(mapping))
    if configured_id not in mapping:
        raise SystemExit("Configured resume has no SearchText; nothing was sent")
    return arguments(config, configured_id, mapping[configured_id])


def validate(argv, *, profile=None):
    args = HHApplicantTool._create_parser().parse_args(argv)
    from gui_profile import expected_profile
    expected = expected_profile(profile)
    if os.environ.get("CONFIG_DIR") != str(expected) or os.environ.get("HH_PROFILE_ID") != ".":
        raise SystemExit("Dedicated profile environment is required")
    if not (
        args.dry_run is False and args.send_email is False and args.ai_filter is False
        and args.skip_tests is False and args.captcha_handling is False
        and args.total_pages == 100 and args.per_page == 20 and args.max_responses is None
        and args.resume_id and args.verbosity == 0
        and args.work_format == ["REMOTE"] and args.currency == "RUR"
        and args.salary == 80000 and args.only_with_salary is False
        and args.order_by == "publication_time"
        and args.search_field == ["name"] and args.force_cover_letter is True
        and args.use_ai is True and args.excluded_filter is None
    ):
        raise SystemExit("Unsafe send arguments; refusing to run")
    config = tomllib.loads((expected / "config.toml").read_text(encoding="utf-8"))
    if argv != arguments(str(expected), args.resume_id, args.text):
        raise SystemExit("Unexpected send arguments; refusing to run")
    ai_provider.activate(ai_provider.from_config(config, allow_external=profile is not None))
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
        f"ВАКАНСИЯ {number}",
        f"Компания: {((vacancy.get('employer') or {}).get('name') or 'не указана')}",
        f"Вакансия: {vacancy.get('name') or 'не указана'}",
        f"Зарплата: {salary_text(vacancy.get('salary'))}",
        f"Ссылка: {vacancy.get('alternate_url') or 'не указана'}",
    ]


class ApprovalGate:
    mode_message = ("РЕЖИМ ОТПРАВКИ: страницы HH до конца результатов; "
                    "каждый отклик требует yes/no, stop завершает просмотр.")
    stop_message = "Просмотр завершён по команде stop."
    captcha_retry_message = "КАПЧА ПРОЙДЕНА — ПОВТОР ОТКЛИКА ТОЛЬКО ПО НОВОМУ ПОДТВЕРЖДЕНИЮ"
    # The GUI worker counts sends at the transport level; this hook tells it how
    # a POST without an answer was resolved: "sent" or "absent".
    on_send_resolved = None

    def __init__(self, resume_id, sender, approve=input, *, review=None, sleep=time.sleep):
        self.resume_id = resume_id
        self.sender = sender
        self.approve = approve
        self.review = review
        self.sleep = sleep
        self.current = None
        self.letter = None
        self._generating = False
        self.generation_skips = 0
        self.captcha = ManualCaptcha()
        self.counts = {"vacancies": 0, "letters": 0, "skipped": 0,
                       "attempts": 0, "captcha_rejections": 0, "http_success": 0}

    def vacancies(self, operation, *args, **kwargs):
        for vacancy in self.original_vacancies(operation, *args, **kwargs):
            if hasattr(operation, "_round_resume"):
                self.resume_id = operation._round_resume["id"]
                self.resume_title = operation._round_resume["title"]
            self.counts["vacancies"] += 1
            self.current = vacancy
            self.operation = operation
            self.letter = None
            self.test_answers = []
            reason = senior_title_reason(vacancy)
            if not reason:
                reason = salary_rejection_reason(vacancy, getattr(operation, "api_client", None)) or (
                    "уже есть отклик или отказ в HH" if vacancy.get("relations") else
                    "вакансия в архиве" if vacancy.get("archived") else
                    "отклик ведёт на внешний сайт" if vacancy.get("response_url") else None
                )
            if reason:
                print("\n" + "=" * 60, flush=True)
                print("\n".join(vacancy_lines(vacancy, self.counts["vacancies"])), flush=True)
                print(f"Статус: пропускаем — {reason}", flush=True)
                if senior_title_reason(vacancy) or salary_rejection_reason(
                        vacancy, getattr(operation, "api_client", None)):
                    continue
            else:
                company = (vacancy.get("employer") or {}).get("name") or "не указана"
                print(f"\nНайдена вакансия {self.counts['vacancies']}: "
                      f"{vacancy.get('name') or 'не указана'} — {company}. "
                      "Готовим письмо для проверки...", flush=True)
            yield vacancy

    def complete(self, client, prompt, *args, **kwargs):
        if self.current is None:
            raise SystemExit("Unexpected letter generation")
        test_ai = getattr(getattr(self, "operation", None), "test_ai", None)
        if test_ai is not None and client is test_ai:
            return self._test_answer(client, prompt, *args, **kwargs)
        self.letter = None
        try:
            vacancy = self.operation.api_client.get("/vacancies/" + self.current["id"])
            if reason := salary_rejection_reason(vacancy, self.operation.api_client):
                raise SystemExit(f"Vacancy salary changed: {reason}")
        except Exception:
            raise SystemExit("Stopped after vacancy-detail failure; no application sent for this vacancy") from None
        letter = self._generate(client, enrich_prompt(prompt, vacancy), args,
                                letter_kwargs(kwargs, vacancy), "application")
        if not isinstance(letter, str) or not letter.strip():
            raise SystemExit("Stopped after empty model response")
        check_letter_level(letter, self.current)
        check_letter_language(letter, vacancy)
        self.letter = letter
        self.counts["letters"] += 1
        return letter

    def _test_answer(self, client, prompt, *args, **kwargs):
        if not self.current.get("has_test") or self.letter is None:
            raise SystemExit("Unexpected test answer generation")
        answer = self._generate(client, prompt, args, kwargs, "test application")
        if not isinstance(answer, str) or not answer.strip():
            raise SystemExit("Stopped after empty test answer")
        self.test_answers.append(answer)
        return answer

    def _notice(self, code):
        print(RETRY_NOTICES[code], flush=True)
        if getattr(self, "observer", None) is not None:
            self.observer("retry", code)

    def _generate(self, client, prompt, args, kwargs, what):
        """Repeat only a gateway connect failure; then skip the vacancy or stop."""
        for round_ in range(GENERATION_ROUNDS):
            try:
                # Do not let HH's broad OpenAI transport/429 retry loop multiply the
                # gateway's narrowly scoped connection retry or replay read failures.
                self._generating = True
                with (patch.object(client, "max_retries", 0)
                      if isinstance(client, OpenAIClient) else nullcontext()):
                    result = self.original_complete(client, prompt, *args, **kwargs)
                self.generation_skips = 0
                return result
            except Exception as error:
                detail = generation_failure_detail(error)
                if _connect_failure(error) is None:
                    raise SystemExit(f"Stopped: {detail}; no {what} sent for this vacancy") from None
            finally:
                self._generating = False
            if round_ + 1 < GENERATION_ROUNDS:
                self._notice("generation_wait")
                self.sleep(GENERATION_RETRY_DELAY)
        self.generation_skips += 1
        if self.generation_skips >= GENERATION_SKIP_LIMIT:
            raise SystemExit(f"Stopped: {detail}; {GENERATION_ROUNDS} rounds failed for "
                             f"{self.generation_skips} vacancies in a row; no {what} sent for this vacancy")
        self.letter = None
        self._notice("generation_skip")
        # Upstream logs AIError and moves on to the next vacancy without a request to HH.
        raise AIError("model gateway unreachable; vacancy skipped")

    def solve_test(self, operation, *args, **kwargs):
        """Run upstream test solving with a neutral answer for link questions."""
        def url_answer(_template):
            return URL_TASK_ANSWER

        try:
            with patch("hh_applicant_tool.operations.apply_vacancies.rand_text", url_answer):
                return self.original_solve_test(operation, *args, **kwargs)
        except SkippedTest:
            raise
        except Exception as error:
            # Upstream catches this and continues silently; make the skip visible.
            print(f"Отклик с тестом не отправлен ({type(error).__name__}); "
                  "переходим к следующей вакансии.", flush=True)
            raise

    @staticmethod
    def _test_response(request):
        url = urlsplit(request.url)
        return (request.method == "POST" and url.scheme == "https"
                and url.hostname == "hh.ru" and url.port is None
                and url.path == "/applicant/vacancy_response/popup" and not url.query
                and not url.fragment and not url.username and not url.password)

    def _send_test(self, session, request, **kwargs):
        if (self.current is None or not self.current.get("has_test") or self.current.get("relations")
                or self.current.get("archived") or self.current.get("response_url")
                or senior_title_reason(self.current)
                or salary_rejection_reason(
                    self.current, getattr(getattr(self, "operation", None), "api_client", None))
                or self.captcha_active):
            raise SystemExit("Cannot send this test application")
        if self.letter is None:
            raise SystemExit("Cannot send a test application without a generated letter")
        if not isinstance(request.body, (bytes, str)):
            raise SystemExit("Unexpected HH test request body")
        body = request.body.decode("utf-8") if isinstance(request.body, bytes) else request.body
        params = parse_qs(body, keep_blank_values=True, strict_parsing=True)
        if any(len(values) != 1 for values in params.values()):
            raise SystemExit("HH test payload has repeated fields")
        fields = {key: values[0] for key, values in params.items()}
        if (fields.get("vacancy_id") != str(self.current["id"])
                or fields.get("resume_hash") != self.resume_id
                or fields.get("letter") != self.letter
                or fields.get("withoutTest") != "no" or not fields.get("_xsrf")):
            raise SystemExit("HH test payload differs from the vacancy, resume or letter")
        answers = [(key, value) for key, value in fields.items() if key.startswith("task_")]
        if not answers or any(len(value) > TEST_ANSWER_LIMIT for _, value in answers):
            raise SystemExit("HH test payload has no answers or an oversized answer")
        if request.headers.get("Content-Type", "").split(";")[0] != "application/x-www-form-urlencoded":
            raise SystemExit("Unexpected HH content type")
        if any(key.startswith("captcha") for key in fields):
            raise SystemExit("HH test payload already carries captcha fields")

        letter = self.letter
        for attempt in range(2):  # One retry after the user solves an in-form HH CAPTCHA.
            print("\n" + "-" * 60, flush=True)
            if attempt:
                print(self.captcha_retry_message, flush=True)
            print("ОТКЛИК НА ВАКАНСИЮ С ТЕСТОМ\n" + "\n".join(
                vacancy_lines(self.current, self.counts["vacancies"])[1:]
            ), flush=True)
            print("Резюме: " + getattr(self, "resume_title", self.resume_id)
                  + " (" + self.resume_id + ")", flush=True)
            print("\nСопроводительное письмо (будет отправлено дословно):\n", flush=True)
            print(letter, flush=True)
            print("\nОтветы на тест (ИИ или выбранный вариант, отправляются без проверки):", flush=True)
            for key, value in answers:
                print(f"  {key}: {value}", flush=True)
            print("-" * 60, flush=True)
            if getattr(self, "observer", None) is not None:
                self.observer("application", {"vacancy": self.current, "text": letter,
                                               "resume": self.resume_id, "retry": bool(attempt),
                                               "test_answers": dict(answers)})
            try:
                answer = (self.review.application(self, letter, retry=bool(attempt))
                          if self.review is not None else
                          self.approve("Отправить этот отклик с тестом? [yes/no/stop]: "))
            except (EOFError, KeyboardInterrupt):
                raise SystemExit("No approval; stopped") from None
            answer = answer.strip().casefold() if isinstance(answer, str) else ""
            if answer == "stop":
                raise StopReview
            if answer in {"no", "n"}:
                self.counts["skipped"] += 1
                self.letter = None
                print("Пропускаем этот отклик; переходим к следующей вакансии.", flush=True)
                raise SkippedTest("Skipped by user before network request")
            if answer not in {"yes", "y"}:
                raise SystemExit("No approval; stopped")
            if isinstance(session, requests.Session):
                session.close()
            if kwargs.get("timeout") is None:
                kwargs["timeout"] = (10, 60)
            self.counts["attempts"] += 1
            self.letter = None
            try:
                response = self.sender(session, request, **kwargs)
            except requests.RequestException as error:
                detail = send_failure_detail(error, self.current["id"])
                raise SystemExit(
                    f"Send outcome uncertain; {detail}; check HH manually before any retry"
                ) from None
            state = form_captcha_state(response)
            if state and attempt:
                raise SystemExit("Repeated HH captcha on the test application; stopped without another retry")
            if state:
                # 403 with hhcaptcha: HH rejected the form before accepting it.
                self.counts["captcha_rejections"] += 1
                referer = request.headers.get("Referer")
                solution = self.captcha.solve_state(
                    state, self.operation.tool, referer if hh_url(str(referer or "")) else None)
                request = request.copy()
                request.prepare_body(
                    data=parse_qsl(body, keep_blank_values=True, strict_parsing=True)
                    + list(solution.items()), files=None)
                continue
            break
        if not 200 <= response.status_code < 300:
            raise SystemExit(
                f"Test send returned HTTP {response.status_code}; {response_failure_detail(response)}; "
                "check HH manually before any retry"
            )
        try:
            result = response.json()
        except ValueError:
            raise SystemExit("Test send returned a non-JSON answer; check HH manually before any retry") from None
        if not isinstance(result, dict) or result.get("success") != "true":
            error = result.get("error") if isinstance(result, dict) else None
            safe = (error if isinstance(error, str) and len(error) <= 64
                    and all(c.isascii() and (c.isalnum() or c in "-_") for c in error) else "unknown")
            raise SystemExit(f"HH did not accept the test application: error={safe}; check HH manually")
        self.counts["http_success"] += 1
        print(f"HH принял отклик с тестом на вакансию {self.current['id']}", flush=True)
        return response

    def send(self, session, request, **kwargs):
        if self._test_response(request):
            return self._send_test(session, request, **kwargs)
        if not self._negotiation(request):
            captcha_request = self._captcha_request(request)
            if not (allowed_request(request.method, request.url, request.body) or captcha_request):
                raise SystemExit("Blocked an unexpected network request")
            try:
                response = self.captcha.send(self.sender, session, request, **kwargs)
            except requests.RequestException:
                raise SystemExit("Network failure; stopped without automatic retry") from None
            # complete() reports gateway errors from their bounded metadata; only
            # there is OpenAIClient's 429 replay loop disabled.
            if self._gateway_generation(request) and getattr(self, "_generating", False):
                return response
            # HH's manual solver interprets 403 on the final captcha form as
            # "incorrect answer". Do not mask it before it reaches the solver.
            if (response.status_code >= 400 and not self.captcha.rejected_answer(request, response)
                    and not permits_token_refresh(request, response)):
                raise SystemExit(f"Stopped after HTTP {response.status_code}")
            return response

        if (self.current is None or self.current.get("relations")
                or self.current.get("has_test") or self.current.get("archived")
                 or self.current.get("response_url") or senior_title_reason(self.current)
                  or salary_rejection_reason(
                      self.current, getattr(getattr(self, "operation", None), "api_client", None))
                 or self.captcha_active):
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

        for attempt in range(2):  # One retry after the user solves an explicit HH CAPTCHA.
            print("\n" + "-" * 60, flush=True)
            if attempt:
                print(self.captcha_retry_message, flush=True)
            print("ОТКЛИК НА ВАКАНСИЮ\n" + "\n".join(
                vacancy_lines(self.current, self.counts["vacancies"])[1:]
            ), flush=True)
            print("Резюме: " + getattr(self, "resume_title", self.resume_id)
                  + " (" + self.resume_id + ")", flush=True)
            print("\nСопроводительное письмо (будет отправлено дословно):\n", flush=True)
            print(params["message"][0], flush=True)
            print("-" * 60, flush=True)
            if getattr(self, "observer", None) is not None:
                self.observer("application", {"vacancy": self.current, "text": params["message"][0],
                                               "resume": self.resume_id, "retry": bool(attempt)})
            try:
                answer = (self.review.application(self, params["message"][0], retry=bool(attempt))
                          if self.review is not None else
                          self.approve("Отправить этот отклик? [yes/no/stop]: "))
            except (EOFError, KeyboardInterrupt):
                raise SystemExit("No approval; stopped") from None
            answer = answer.strip().casefold() if isinstance(answer, str) else ""
            if answer == "stop":
                raise StopReview
            if answer in {"no", "n"}:
                self.counts["skipped"] += 1
                self.letter = None
                print("Пропускаем этот отклик; переходим к следующей вакансии.", flush=True)
                # HH 2.3.7 catches Redirect around this exact API call and continues
                # to the next vacancy without treating the synthetic result as sent.
                raise Redirect(None, {"description": "Skipped by user before network request"})
            if answer not in {"yes", "y"}:
                raise SystemExit("No approval; stopped")
            # Approval (including AutoGate's pause) may leave a pooled socket idle
            # for minutes. Clear pools before the POST, not after a failed POST.
            # Session.close retains cookies, headers, proxies and TLS settings.
            if isinstance(session, requests.Session):
                session.close()
            if kwargs.get("timeout") is None:
                kwargs["timeout"] = (10, 60)  # connect/read, not an overall deadline
            self.counts["attempts"] += 1  # Count before network: a lost answer needs a check.
            self.letter = None  # The original approval cannot authorize a second attempt.
            response = self._post_negotiation(session, request, kwargs)
            if response.status_code == 403 and not attempt:
                captcha_url = self._captcha_url(response)
                if captcha_url:
                    self.counts["captcha_rejections"] += 1
                    self.captcha.solve(captcha_url, self.operation.tool)
                    continue
            if not 200 <= response.status_code < 300:
                raise SystemExit(
                    f"Send returned HTTP {response.status_code}; {response_failure_detail(response)}; "
                    "check HH manually before any retry"
                )
            self.counts["http_success"] += 1
            print(f"HH вернул HTTP {response.status_code} для вакансии {self.current['id']}", flush=True)
            return response
        raise SystemExit("No confirmed send result; check HH manually")

    def _post_negotiation(self, session, request, kwargs):
        """Send the POST; after a lost answer, ask HH before one identical repeat.

        HH may have created the application before the answer was lost, so a
        repeat happens only when the vacancy shows no application in HH.
        """
        for repeat in range(2):
            try:
                return self.sender(session, request, **kwargs)
            except requests.RequestException as error:
                detail = send_failure_detail(error, self.current["id"])
            if repeat:
                raise SystemExit(f"Send outcome uncertain after one checked repeat; {detail}; "
                                 "check HH manually before any retry")
            if self._application_exists(session, request, kwargs, detail):
                self._resolved("sent")
                self._notice("send_confirmed")
                confirmed = requests.Response()
                # Upstream expects the empty body HH returns for a created application.
                confirmed.status_code, confirmed._content = 201, b""
                confirmed.request, confirmed.url = request, request.url
                return confirmed
            # This attempt provably created nothing, so it is not counted.
            self.counts["attempts"] -= 1
            self._resolved("absent")
            self._notice("send_retry")
            if isinstance(session, requests.Session):
                session.close()
            self.counts["attempts"] += 1

    def _resolved(self, kind):
        hook = type(self).on_send_resolved  # Read from the class: a plain function, not a method.
        if hook is not None:
            hook(kind)

    def _application_exists(self, session, request, kwargs, detail):
        """Read the vacancy relations directly: the upstream client lock is held here."""
        self._notice("send_check")
        self.sleep(SEND_CHECK_DELAY)
        vacancy_id = str(self.current["id"])
        failed = (f"Send outcome uncertain; {detail}; status check failed; "
                  "check HH manually before any retry")
        if not (vacancy_id.isascii() and vacancy_id.isdecimal() and len(vacancy_id) <= 20):
            raise SystemExit(failed)
        headers = {name: request.headers[name] for name in (
            "Authorization", "User-Agent", "Accept", "X-HH-App-Active", "X-Force-App-Access")
            if name in request.headers}
        check = requests.Request("GET", "https://api.hh.ru/vacancies/" + vacancy_id,
                                 headers=headers).prepare()
        try:
            response = self.sender(session, check, **{**kwargs, "timeout": (10, 30)})
            relations = response.json()["relations"] if response.status_code == 200 else None
        except (requests.RequestException, ValueError, KeyError, TypeError):
            raise SystemExit(failed) from None
        if not isinstance(relations, list):
            raise SystemExit(failed)
        return bool(relations)

    @staticmethod
    def _negotiation(request):
        url = urlsplit(request.url)
        return (request.method == "POST" and url.scheme == "https"
                and url.hostname == "api.hh.ru" and url.port is None
                and url.path == "/negotiations" and not url.query
                and not url.fragment and not url.username and not url.password)

    @staticmethod
    def _gateway_generation(request):
        return (ai_provider.generation(request.method, request.url)
                and allowed_request(request.method, request.url, request.body))

    @staticmethod
    def _captcha_url(response):
        return captcha_url(response)

    @property
    def captcha_active(self):
        return self.captcha.active

    def _captcha_request(self, request):
        return self.captcha.allowed_request(request)


def run(argv, approve=input, *, all_resumes=False, gate_factory=ApprovalGate, profile=None, observer=None):
    from gui_profile import expected_profile
    expected = expected_profile(profile)
    args = validate(argv, profile=profile)
    config = tomllib.loads((expected / "config.toml").read_text(encoding="utf-8"))
    mapping = load_searches() if all_resumes else None
    if all_resumes and argv != all_resume_arguments(str(expected),
                                                   config.get("general", {}).get("cover_letter_resume_id"),
                                                   generic=profile is not None):
        raise SystemExit("Unexpected all-resume arguments; refusing to run")
    gate = gate_factory(args.resume_id, requests.sessions.Session.send, approve)
    gate.observer = observer
    gate.operation = None
    original_get_vacancies = Operation._get_vacancies
    gate.original_vacancies = single_round_vacancy if all_resumes else original_get_vacancies
    gate.original_complete = OpenAIClient.complete
    gate.original_solve_test = Operation._solve_vacancy_test
    def guarded_send(session, request, **kwargs):
        return gate.send(session, request, **kwargs)

    def visible_vacancies(operation, *args, **kwargs):
        return gate.vacancies(operation, *args, **kwargs)

    def visible_letter(client, *args, **kwargs):
        return gate.complete(client, *args, **kwargs)

    # A bound method on the class is not re-bound, so upstream's keyword-only
    # call would lose the operation; wrap it like the other patched methods.
    def visible_test(operation, *args, **kwargs):
        return gate.solve_test(operation, *args, **kwargs)

    print(gate.mode_message, flush=True)
    try:
        with patch.object(requests.sessions.Session, "send", guarded_send), \
             (selected_resume_context(args.resume_id, config.get("general", {}).get("cover_letter_resume_id"),
                                      **({"force_fetch": True} if profile is not None else {}))
              if not all_resumes else nullcontext()), \
             patch.object(Operation, "_get_vacancies", visible_vacancies), \
             (patch.object(Operation, "_apply_vacancies", lambda operation: apply_round_robin(
                  operation, config.get("general", {}).get("cover_letter_resume_id"), mapping,
                  original_get_vacancies, **({"force_fetch": True} if profile is not None else {})))
              if all_resumes else nullcontext()), \
             patch.object(OpenAIClient, "complete", visible_letter), \
             patch.object(Operation, "_solve_vacancy_test", visible_test):
            try:
                return main(argv)
            except StopReview:
                print(gate.stop_message, flush=True)
                return 0
    finally:
        counts = gate.counts
        print("\n" + "=" * 60, flush=True)
        print("ИТОГ: просмотрено {vacancies}, подготовлено писем {letters}, пропущено {skipped}, "
              "попыток отправки {attempts}, ответов HH 2xx {http_success}".format(**counts), flush=True)
        if gate.counts["attempts"] - gate.counts["captcha_rejections"] != gate.counts["http_success"]:
            print("Результат отправки может быть неопределённым. Перед повтором проверь отклики в HH.", flush=True)


if __name__ == "__main__":
    if os.environ.get("HH_APPLICANT_TOOL_ALL") == "1":
        config_path = Path(os.environ.get("CONFIG_DIR", "")) / "config.toml"
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        sys.exit(run(all_resume_arguments(os.environ.get("CONFIG_DIR", ""),
                                          config["general"]["cover_letter_resume_id"]), all_resumes=True))
    sys.exit(run(arguments(os.environ.get("CONFIG_DIR", ""),
                           os.environ.get("HH_APPLICANT_TOOL_RESUME_ID", ""),
                           os.environ.get("HH_APPLICANT_TOOL_SEARCH", ""))))
