"""Round-robin search across published resumes, with one reviewed HH action at a time."""

from collections import deque
import json
import os
from pathlib import Path
from random import SystemRandom

from hh_applicant_tool.operations.apply_vacancies import DEFAULT_COVER_LETTER_SYSTEM_PROMPT

from resume_context import selected_resume_context


def load_searches(path=None):
    from gui_profile import validate_searches
    source = Path(path or os.environ.get("POCKETNODE_HH_SEARCH_FILE")
                  or Path(__file__).with_name("resume-search.json"))
    try:
        mapping = validate_searches(json.loads(source.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        raise SystemExit("Invalid resume search map; nothing was sent") from None
    return mapping


def apply_round_robin(operation, configured_id, mapping, original_get_vacancies, *, force_fetch=False):
    """Replace only the upstream sequential resume loop, not its per-vacancy checks."""
    resumes = [r for r in operation.tool.get_resumes()
               if (r.get("status") or {}).get("id") == "published"]
    ids = [r["id"] for r in resumes]
    if not ids or len(ids) != len(set(ids)) or any(rid not in mapping for rid in ids):
        raise SystemExit("Published resume list is empty, duplicated or missing a SearchText")

    # Build every AI client before the first application. A different resume's
    # saved DevOps history must never be used for an unrelated role.
    clients = {}
    test_clients = {}
    for resume in resumes:
        with selected_resume_context(resume["id"], configured_id,
                                     **({"force_fetch": True} if force_fetch else {})):
            settings = operation.tool.get_openai_settings("openai.cover_letter")
            settings.setdefault("system_prompt", DEFAULT_COVER_LETTER_SYSTEM_PROMPT)
            settings.setdefault("temperature", 0.3)
            clients[resume["id"]] = operation.tool.get_ai_client(**settings)
            # Test answers use the same resume facts as the letter.
            test_clients[resume["id"]] = operation.tool.get_ai_client(
                **operation.tool.get_openai_settings("openai.test_solver"))

    SystemRandom().shuffle(resumes)
    print("Порядок первого круга: " + " → ".join(r["title"] for r in resumes), flush=True)
    user = operation.tool.get_me()
    seen_employers = set()
    seen_vacancies = set()
    queue = deque((resume, original_get_vacancies(operation, resume_id=resume["id"]))
                  for resume in resumes)
    while queue:
        resume, vacancies = queue.popleft()
        # "" selects HH's suggested vacancies for this resume instead of a text search.
        operation.text = mapping[resume["id"]] or None
        vacancy = None
        while True:
            try:
                candidate = next(vacancies)
            except StopIteration:
                break
            if candidate["id"] in seen_vacancies:
                print(f"Пропущена повторяющаяся вакансия {candidate['id']}; "
                      "второй отклик на неё в этом запуске не предлагаем.", flush=True)
                continue
            vacancy = candidate
            break
        if vacancy is None:
            continue
        queue.append((resume, vacancies))
        seen_vacancies.add(vacancy["id"])
        print(f"\nРезюме: {resume['title']} | Поиск: {operation.text or 'все подходящие вакансии'}", flush=True)
        operation._round_resume = resume
        operation._round_vacancy = vacancy
        operation.cover_letter_ai = clients[resume["id"]]
        operation.test_ai = test_clients[resume["id"]]
        operation.resume_id = resume["id"]
        # The upstream method still checks existing responses, tests, employer,
        # letter content, and performs exactly one approved negotiation request.
        if operation._apply_resume(resume, user, seen_employers):
            print("HH сообщил об исчерпанном лимите откликов; остановлено.", flush=True)
            return


def single_round_vacancy(operation, resume_id):
    if resume_id != operation._round_resume["id"]:
        raise SystemExit("Round-robin resume mismatch; nothing was sent")
    yield operation._round_vacancy
