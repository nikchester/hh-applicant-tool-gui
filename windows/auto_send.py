"""Bounded automatic HH applications, pausing for HH's manual CAPTCHA window."""

import argparse
from functools import partial
import math
import os
from pathlib import Path
from random import SystemRandom
import sys
import time
import tomllib

from send import ApprovalGate, StopReview, all_resume_arguments, arguments, run


def validate_settings(max_responses, min_pause, max_pause):
    if type(max_responses) is not int or max_responses <= 0:
        raise ValueError("MaxResponses must be a positive integer")
    if (any(type(value) not in (int, float) or not math.isfinite(value)
            for value in (min_pause, max_pause))
            or not 0 < min_pause <= max_pause <= 86400):
        raise ValueError("Pause must satisfy 0 < MinPauseSeconds <= MaxPauseSeconds <= 86400")


class AutoGate(ApprovalGate):
    stop_message = "Заданный лимит успешных откликов достигнут; автоотправка завершена."
    captcha_retry_message = "КАПЧА ПРОЙДЕНА — ОДИН ПОВТОР ТОГО ЖЕ ОТКЛИКА В АВТОРЕЖИМЕ"

    def __init__(self, resume_id, sender, approve=None, *, max_responses,
                 min_pause=60, max_pause=180, sleep=time.sleep, uniform=None):
        validate_settings(max_responses, min_pause, max_pause)
        self.max_responses = max_responses
        self.min_pause, self.max_pause = min_pause, max_pause
        self.sleep = sleep
        self.uniform = uniform or SystemRandom().uniform
        self.mode_message = (
            f"АВТООТКЛИК: не более {max_responses} успешных откликов; "
            f"случайная пауза {min_pause}–{max_pause} с между отправками. "
            "Письма отправляются БЕЗ отдельного подтверждения. "
            "При капче вводи текст вручную в окне; после решения отправка продолжится. "
            "Остальные ошибки останавливают запуск без повторов. Ctrl+C — остановка."
        )

        super().__init__(resume_id, sender, approve=self.wait_before_send)

    def check_limit(self):
        if self.counts["http_success"] >= self.max_responses:
            raise StopReview

    def vacancies(self, operation, *args, **kwargs):
        self.check_limit()
        for vacancy in super().vacancies(operation, *args, **kwargs):
            self.check_limit()
            yield vacancy
            # Stop before reading another page or generating another letter,
            # including between resumes in round-robin mode.
            self.check_limit()

    def send(self, session, request, **kwargs):
        if self._negotiation(request) or self._test_response(request):
            self.check_limit()
        return super().send(session, request, **kwargs)

    def wait_before_send(self, _prompt):
        self.check_limit()
        if self.counts["http_success"]:
            delay = self.uniform(self.min_pause, self.max_pause)
            print(f"Пауза перед следующим откликом: {delay:.1f} с", flush=True)
            self.sleep(delay)
        return "yes"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-responses", type=int, required=True)
    parser.add_argument("--min-pause", type=float, default=60)
    parser.add_argument("--max-pause", type=float, default=180)
    options = parser.parse_args(argv)
    try:
        validate_settings(options.max_responses, options.min_pause, options.max_pause)
    except ValueError as error:
        parser.error(str(error))
    config_dir = os.environ.get("CONFIG_DIR", "")
    all_resumes = os.environ.get("POCKETNODE_HH_ALL") == "1"
    if all_resumes:
        config = tomllib.loads((Path(config_dir) / "config.toml").read_text(encoding="utf-8"))
        hh_args = all_resume_arguments(config_dir, config["general"]["cover_letter_resume_id"])
    else:
        hh_args = arguments(config_dir, os.environ.get("POCKETNODE_HH_RESUME_ID", ""),
                            os.environ.get("POCKETNODE_HH_SEARCH", ""))
    return run(hh_args, all_resumes=all_resumes, gate_factory=partial(
        AutoGate, max_responses=options.max_responses,
        min_pause=options.min_pause, max_pause=options.max_pause))


if __name__ == "__main__":
    sys.exit(main())
