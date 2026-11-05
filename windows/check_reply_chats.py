"""Offline tests for the reviewed HH chat reply boundary."""

import html
import json
import errno
import os
import socket
import ssl
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from email.message import Message
from io import BytesIO, StringIO
from pathlib import Path
import shutil
import sys
import tempfile
import time
import traceback
import unittest
import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch
from urllib.parse import parse_qs, urlsplit

import requests
from urllib3 import exceptions as u3
from hh_applicant_tool.ai.openai import OpenAIClient
from hh_applicant_tool.operations.reply_chats import BOT_RECRUITER_ANSWER, ChatToReply, Operation
from hh_applicant_tool.tool import HHApplicantTool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "windows"))
from reply_chats import (  # noqa: E402
    ChatGate, ChatSkipped, allowed_read, anonymous_web_session, arguments, chat_send_failure_detail,
    network_failure, request_target, run, startup_read, validate,
)
from hh_network_diagnostics import NetworkDiagnostics, transport_options  # noqa: E402
from urllib3.util import Timeout


RESUME = "9749b651ff056b6c970039ed1f5a63765a5178"
TEXT = "Привет! Давай созвонимся в пятницу."
CHAT = ChatToReply(
    chat_id=123, employer_name="Example", vacancy_name="DevOps",
    vacancy_url="https://hh.ru/vacancy/1", contact_name="Рекрутер",
    reply_to_message="Когда удобно созвониться?", reply_options=[],
    resume_id=RESUME, resume_title="DevOps-инженер", is_discard=False,
    vacancy_compensation="", employer_id="", applicant_id=1,
    resume_experience="experience", salary="salary", skills="skills",
)


def message(mid=1, text=CHAT.reply_to_message, sender="recruiter", bot=False, options=()):
    return {"id": mid, "text": text, "participantId": sender,
            "participantDisplay": {"name": CHAT.contact_name, "isBot": bot},
            "actions": {"text_buttons": [{"text": text} for text in options]}}


def event(mid, kind, sender="recruiter", bot=False):
    return {"id": mid, "type": kind, "text": "", "participantId": sender,
            "participantDisplay": {"name": "", "isBot": bot}}


def snapshot(*items, chat_id=123, allowed=True):
    return {"chat": {"id": chat_id, "currentParticipantId": "me",
                     "messages": {"items": list(items or [message()])}},
            "chatStates": {"writeMessageState": {"allowed": allowed}}}


def request(body=None, url="https://chatik.hh.ru/chatik/api/send"):
    if body is None:
        body = {"chatId": 123, "text": TEXT, "idempotencyKey": str(uuid.uuid4())}
    return requests.Request("POST", url, json=body).prepare()


class ConversationChecks(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.items = [message(bot=True)]
        self.allowed = True
        self.current_me = "me"
        self.on_sleep = lambda: None
        self.reads = 0
        self.drafts = []
        self.approvals = []
        self.sender = Mock(return_value=SimpleNamespace(status_code=200))
        self.gate = ChatGate(self.sender, approve=self.approve,
                             clock=lambda: self.now, sleep=self.sleep)
        self.operation = SimpleNamespace(chat_url="https://chatik.hh.ru",
                                         get_chat_data=self.read)
        self.gate.original_complete = lambda *_a: "Да"
        self.gate.original_process = self.process
        self.gate.original_message = self.send

    def read(self, chat_id):
        self.reads += 1
        data = snapshot(*self.items, chat_id=chat_id, allowed=self.allowed)
        data["chat"]["currentParticipantId"] = self.current_me
        return data

    def sleep(self, seconds):
        self.now += seconds
        self.on_sleep()

    def approve(self, prompt):
        self.approvals.append(prompt)
        return "yes"

    def process(self, operation, chat, blacklist):
        self.drafts.append(chat)
        text = self.gate.complete(None, "prompt")
        self.gate.message(operation, chat.chat_id, text)

    def send(self, operation, chat_id, text, refer_url=None):
        response = self.gate.send(None, request({
            "chatId": chat_id, "text": text, "idempotencyKey": str(uuid.uuid4())}))
        self.items.append(message(self.items[-1]["id"] + 1, text, "me"))
        return response

    def run_chat(self):
        with redirect_stdout(StringIO()):
            self.gate.process(self.operation, CHAT, set())

    def test_delayed_same_text_new_id_and_updated_buttons_preserve_resume(self):
        def advance():
            if self.now == 4:
                self.items.append(message(3, bot=True, options=["Да", "Нет"]))
            if len(self.drafts) == 2:
                self.items.append(message(5, BOT_RECRUITER_ANSWER, bot=True))
        self.on_sleep = advance
        self.run_chat()
        self.assertEqual(len(self.drafts), 2)
        self.assertEqual(len(self.approvals), 2)
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(self.drafts[1].reply_options, ["Да", "Нет"])
        self.assertEqual(self.drafts[1].reply_to_message, self.drafts[0].reply_to_message)
        for field in ("resume_id", "resume_title", "resume_experience", "salary", "skills"):
            self.assertEqual(getattr(self.drafts[1], field), getattr(CHAT, field))

    def test_stale_question_and_own_echo_never_replayed_timeout(self):
        self.run_chat()
        self.assertEqual(self.now, 60)
        self.assertEqual(len(self.drafts), 1)
        self.assertEqual(self.sender.call_count, 1)

    def test_initial_fresh_question_replaces_stale_chat_list_data(self):
        self.items = [message(7, "Свежий вопрос", options=["Да", "Нет"])]
        self.run_chat()
        self.assertEqual(self.drafts[0].reply_to_message, "Свежий вопрос")
        self.assertEqual(self.drafts[0].reply_options, ["Да", "Нет"])
        self.assertEqual(self.now, 0)

    def test_initial_terminal_own_echo_or_duplicate_ids_do_not_draft(self):
        for items in ([message(1, BOT_RECRUITER_ANSWER, bot=True)],
                      [message(1, TEXT, "me")], [message(), message()]):
            with self.subTest(items=items):
                self.setUp()
                self.items = items
                self.run_chat()
                self.assertEqual(self.drafts, [])
                self.assertEqual(self.approvals, [])
                self.sender.assert_not_called()

    def test_other_sender_with_same_name_stops(self):
        self.on_sleep = lambda: self.items.append(message(3, sender="another", bot=True))
        self.run_chat()
        self.assertEqual(len(self.drafts), 1)
        self.assertEqual(self.now, 2)

    def test_missing_or_wrong_echo_stops_before_next_draft(self):
        for wrong in (False, True):
            with self.subTest(wrong=wrong):
                self.setUp()
                original = self.send
                def send(*args, **kwargs):
                    result = original(*args, **kwargs)
                    if wrong:
                        self.items[-1]["text"] = "another answer"
                    else:
                        self.items.pop()
                    return result
                self.gate.original_message = send
                self.on_sleep = lambda: self.items.append(message(3, bot=True))
                self.run_chat()
                self.assertEqual(len(self.drafts), 1)

    def test_question_before_echo_is_not_new_turn(self):
        original = self.send
        def send(*args, **kwargs):
            self.items.append(message(2, bot=True))
            return original(*args, **kwargs)
        self.gate.original_message = send
        self.run_chat()
        self.assertEqual(len(self.drafts), 1)

    def test_terminal_disabled_and_discard_stop(self):
        for reason in ("terminal", "disabled", "discard"):
            with self.subTest(reason=reason):
                self.setUp()
                def advance():
                    if reason == "disabled":
                        self.allowed = False
                    else:
                        item = message(3, BOT_RECRUITER_ANSWER if reason == "terminal" else "Отказ", bot=True)
                        if reason == "discard":
                            item["workflowTransition"] = {"applicantState": "DISCARD"}
                        self.items.append(item)
                self.on_sleep = advance
                self.run_chat()
                self.assertEqual(len(self.drafts), 1)
                self.assertEqual(self.now, 2)

    def test_no_on_second_question_stops_without_second_post(self):
        self.on_sleep = lambda: self.items.append(message(3, bot=True))
        self.gate.approve = lambda _: "yes" if len(self.drafts) == 1 else "no"
        self.run_chat()
        self.assertEqual(len(self.drafts), 2)
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(self.now, 2)

    def test_changed_question_during_generation_or_approval_skips_stale_send(self):
        for when in ("generation", "approval"):
            with self.subTest(when=when):
                self.setUp()
                def changed(*_args):
                    self.items.append(message(2, "Новый вопрос", bot=True))
                    return "Да" if when == "generation" else "yes"
                if when == "generation":
                    self.gate.original_complete = changed
                else:
                    self.gate.approve = changed
                self.run_chat()
                self.sender.assert_not_called()
                self.assertEqual(self.now, 0)

    def test_current_participant_switch_during_generation_or_approval_never_posts(self):
        for when in ("generation", "approval"):
            with self.subTest(when=when):
                self.setUp()
                def changed(*_args):
                    self.current_me = "different-applicant"
                    return "Да" if when == "generation" else "yes"
                if when == "generation":
                    self.gate.original_complete = changed
                else:
                    self.gate.approve = changed
                self.run_chat()
                self.sender.assert_not_called()
                self.assertEqual(self.gate.counts["attempts"], 0)
                self.assertEqual(self.approvals, [])
                self.assertIsNone(self.gate.current_participant)

    def test_changed_question_or_identity_while_editing_never_posts(self):
        for change in ("question", "identity"):
            with self.subTest(change=change):
                self.setUp()
                answers = iter(("manual", "edited", "/done", "yes"))
                def approve(_):
                    answer = next(answers)
                    if answer == "edited":
                        if change == "question":
                            self.items.append(message(2, "Новый вопрос", bot=True))
                        else:
                            self.current_me = "different-applicant"
                    return answer
                self.gate.approve = approve
                self.run_chat()
                self.sender.assert_not_called()
                self.assertEqual(self.gate.counts["attempts"], 0)

    def test_sleep_reaching_deadline_does_not_start_get(self):
        self.gate.wait_seconds = 2
        def late_question():
            self.items.append(message(3, bot=True))
        self.on_sleep = late_question
        self.run_chat()
        # Initial read and two freshness checks, no poll after the sleep.
        self.assertEqual(self.reads, 3)
        self.assertEqual(len(self.drafts), 1)
        self.sender.assert_called_once()

    def test_poll_get_finishing_at_or_after_deadline_never_accepts_question(self):
        for finish_time in (60, 61):
            with self.subTest(finish_time=finish_time):
                self.setUp()
                original_read = self.read
                def late_read(chat_id):
                    if self.gate.poll_deadline is not None:
                        self.now = finish_time
                        self.items.append(message(3, bot=True))
                    return original_read(chat_id)
                self.operation.get_chat_data = late_read
                self.run_chat()
                self.assertEqual(self.reads, 4)
                self.assertEqual(len(self.drafts), 1)
                self.assertEqual(len(self.approvals), 1)
                self.sender.assert_called_once()
                self.assertIsNone(self.gate.poll_deadline)

    def test_no_actual_send_and_json_error_do_not_poll(self):
        self.gate.original_message = Mock(return_value=None)
        self.run_chat()
        self.assertEqual(self.now, 0)
        self.setUp()
        original = self.send
        def rejected(*args, **kwargs):
            original(*args, **kwargs)
            raise ValueError("HH JSON error")
        self.gate.original_message = rejected
        with self.assertRaisesRegex(ValueError, "JSON error"):
            self.run_chat()
        self.assertEqual(self.now, 0)
        self.assertIsNone(self.gate.sent_text)

    def test_uncertain_send_never_polls_or_retries(self):
        self.sender.side_effect = requests.ReadTimeout()
        with self.assertRaisesRegex(SystemExit, "uncertain"):
            self.run_chat()
        self.sender.assert_called_once()
        self.assertEqual(self.now, 0)

    def test_real_upstream_json_error_does_not_poll_after_http_success(self):
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"error": "rejected"}'
        self.sender.return_value = response
        self.gate.original_message = Operation.send_chat_message
        with requests.Session() as session:
            self.operation.tool = SimpleNamespace(session=session)
            self.operation._headers = lambda *_a, **_kw: {"Content-Type": "application/json"}
            # Installed upstream calls ApiError with an incompatible signature;
            # even that JSON-error failure must stop before any polling/retry.
            with patch.object(session, "send", side_effect=lambda req, **kw:
                              self.gate.send(session, req, **kw)), self.assertRaises(TypeError):
                self.run_chat()
        self.sender.assert_called_once()
        self.assertEqual(self.now, 0)
        self.assertIsNone(self.gate.sent_text)

    def test_real_upstream_processing_uses_fresh_question_and_options(self):
        self.gate.original_process = Operation.process_chat
        self.operation.build_placeholders = lambda chat, *args: Operation.build_placeholders(
            self.operation, chat, *args)
        self.operation.base_placeholders = {}
        self.operation.options_prompt = "%(reply_to_message)s %(options)s"
        self.operation.user_prompt = "%(reply_to_message)s %(history)s"
        self.operation.ai_init_args = {"system_prompt": "%(resume_title)s"}
        self.operation.dry_run = False
        self.operation.max_history = sys.maxsize
        self.operation.ignored_chats = set()
        self.operation.chat_page_url = "https://chatik.hh.ru/"
        self.operation.match_option = Operation.match_option
        self.operation.load_history = lambda chat: (["history"], 1, True)
        self.operation.send_chat_message = lambda *a, **kw: self.gate.message(self.operation, *a, **kw)
        prompts = []
        def complete(_client, prompt):
            prompts.append(prompt)
            return "Да"
        self.gate.original_complete = complete
        self.operation.tool = SimpleNamespace(get_ai_client=lambda **kw: SimpleNamespace(
            complete=lambda prompt: self.gate.complete(None, prompt)))
        def advance():
            if len(prompts) == 1:
                self.items.append(message(3, "Новый вопрос", bot=True, options=["Да", "Нет"]))
            else:
                self.items.append(message(5, BOT_RECRUITER_ANSWER, bot=True))
        self.on_sleep = advance
        self.run_chat()
        self.assertEqual(len(prompts), 2)
        self.assertIn("Новый вопрос", prompts[1])
        self.assertIn("- Нет", prompts[1])
        self.assertEqual(len(self.approvals), 2)

    def test_edited_a1_echo_and_q2_use_real_upstream_history(self):
        self.gate.original_process = Operation.process_chat
        self.operation.build_placeholders = lambda chat, *args: Operation.build_placeholders(
            self.operation, chat, *args)
        self.operation.base_placeholders = {}
        self.operation.options_prompt = "%(reply_to_message)s %(options)s"
        self.operation.user_prompt = "%(reply_to_message)s %(history)s"
        self.operation.ai_init_args = {"system_prompt": "%(resume_title)s"}
        self.operation.dry_run = False
        self.operation.max_history = sys.maxsize
        self.operation.ignored_chats = set()
        self.operation.chat_page_url = "https://chatik.hh.ru/"
        self.operation.match_option = Operation.match_option
        self.operation.load_history = lambda chat: ([m["text"] for m in self.items], 1, True)
        self.operation.send_chat_message = lambda *a, **kw: self.gate.message(self.operation, *a, **kw)
        prompts = []
        def complete(_client, prompt):
            prompts.append(prompt)
            return "AI A1" if len(prompts) == 1 else "A2"
        self.gate.original_complete = complete
        self.operation.tool = SimpleNamespace(get_ai_client=lambda **kw: SimpleNamespace(
            complete=lambda prompt: self.gate.complete(None, prompt)))
        answers = iter(("manual", "Edited A1", "/done", "yes", "yes"))
        self.gate.approve = lambda _: next(answers)
        def advance():
            if len(prompts) == 1:
                self.items.append(message(3, "Q2", bot=True))
            else:
                self.items.append(message(5, BOT_RECRUITER_ANSWER, bot=True))
        self.on_sleep = advance
        self.run_chat()
        self.assertEqual(len(prompts), 2)
        self.assertIn("Edited A1", prompts[1])
        self.assertIn("Q2", prompts[1])
        self.assertNotIn("AI A1", prompts[1])
        payloads = [json.loads(call.args[1].body)["text"] for call in self.sender.call_args_list]
        self.assertEqual(payloads, ["Edited A1", "A2"])

    def test_human_names_not_bot_substrings_and_explicit_labels_are_bots(self):
        for name, bot in (("Марии Ивановой", False), ("ИИ-помощник", True),
                          ("AI assistant", True), ("Робот-рекрутер", True),
                          ("Bot recruiter", True)):
            with self.subTest(name=name):
                self.setUp()
                self.items[0]["participantDisplay"] = {"name": name, "isBot": False}
                self.run_chat()
                self.assertEqual(self.now, 60 if bot else 0)
                self.sender.assert_called_once()

    def test_twenty_turn_limit(self):
        self.on_sleep = lambda: self.items.append(message(self.items[-1]["id"] + 1, bot=True))
        self.run_chat()
        self.assertEqual(len(self.drafts), 20)
        self.assertEqual(len(self.approvals), 20)
        self.assertEqual(self.sender.call_count, 20)
        self.assertEqual(self.now, 38)

    def run_chat_output(self):
        out = StringIO()
        with redirect_stdout(out):
            self.gate.process(self.operation, CHAT, set())
        return out.getvalue()

    def test_question_with_buttons_before_bot_left_is_offered_without_wait(self):
        self.items = [message(1, "Расскажите о себе " * 10, bot=True),
                      message(2, "Готовы к собеседованию?", bot=True, options=["Да", "Нет"]),
                      event(3, "PARTICIPANT_LEFT", sender="bot-service")]
        output = self.run_chat_output()
        self.assertEqual(len(self.drafts), 1)
        self.assertEqual(self.drafts[0].reply_to_message, "Готовы к собеседованию?")
        self.assertEqual(self.drafts[0].reply_options, ["Да", "Нет"])
        self.sender.assert_called_once()
        self.assertEqual(self.now, 0)
        self.assertIn("Робот вышел из чата", output)

    def test_question_after_own_message_before_bot_left_is_offered(self):
        self.items = [message(1, TEXT, "me"), message(2, "Описание " * 100, bot=True),
                      message(3, "Когда сможете выйти?", bot=True),
                      event(4, "PARTICIPANT_LEFT", bot=True)]
        self.run_chat()
        self.assertEqual(len(self.drafts), 1)
        self.assertEqual(self.drafts[0].reply_to_message, "Когда сможете выйти?")
        self.sender.assert_called_once()
        self.assertEqual(self.now, 0)

    def test_join_before_question_keeps_bot_follow_up_wait(self):
        self.items = [event(1, "PARTICIPANT_JOINED"), message(2, bot=True)]
        self.run_chat()
        self.assertEqual(len(self.drafts), 1)
        self.sender.assert_called_once()
        self.assertEqual(self.now, 60)

    def test_unknown_or_non_system_trailing_events_and_no_question_are_skipped(self):
        unknown = {"id": 2, "type": "SOMETHING_NEW", "text": "", "participantId": "recruiter"}
        left_with_buttons = event(2, "PARTICIPANT_LEFT")
        left_with_buttons["actions"] = {"text_buttons": [{"text": "Да"}]}
        discard = message(1, "Отказ", bot=True)
        discard["workflowTransition"] = {"applicantState": "DISCARD"}
        for items in ([message(1, bot=True), unknown],
                      [message(1, bot=True), left_with_buttons],
                      [event(1, "PARTICIPANT_LEFT")],
                      [event(1, "PARTICIPANT_JOINED"), event(2, "PARTICIPANT_LEFT")],
                      [message(1, bot=True), message(2, TEXT, "me"), event(3, "PARTICIPANT_LEFT")],
                      [discard, event(2, "PARTICIPANT_LEFT")],
                      [message(1, BOT_RECRUITER_ANSWER, bot=True), event(2, "PARTICIPANT_LEFT")]):
            with self.subTest(items=items):
                self.setUp()
                self.items = items
                self.run_chat()
                self.assertEqual(self.drafts, [])
                self.assertEqual(self.approvals, [])
                self.sender.assert_not_called()
                self.assertEqual(self.gate.counts["skipped"], 1)

    def test_system_event_during_generation_or_approval_keeps_approved_question(self):
        for when, kind in (("generation", "PARTICIPANT_JOINED"), ("approval", "PARTICIPANT_LEFT")):
            with self.subTest(when=when):
                self.setUp()
                def changed(*_args):
                    self.items.append(event(2, kind))
                    return "Да" if when == "generation" else "yes"
                if when == "generation":
                    self.gate.original_complete = changed
                else:
                    self.gate.approve = changed
                self.run_chat()
                self.sender.assert_called_once()
                self.assertEqual(self.gate.counts["skipped"], 0)

    def test_real_message_after_system_event_during_approval_skips_send(self):
        def changed(_prompt):
            self.items.extend([event(2, "PARTICIPANT_LEFT"), message(3, "Новый вопрос", bot=True)])
            return "yes"
        self.gate.approve = changed
        self.run_chat()
        self.sender.assert_not_called()
        self.assertEqual(self.gate.counts["skipped"], 1)

    def test_bot_left_during_follow_up_wait_stops_waiting(self):
        self.on_sleep = lambda: self.items.append(
            event(self.items[-1]["id"] + 1, "PARTICIPANT_LEFT", sender="bot-service"))
        output = self.run_chat_output()
        self.assertEqual(len(self.drafts), 1)
        self.sender.assert_called_once()
        self.assertEqual(self.now, 2)
        self.assertIn("Робот вышел из чата", output)
        self.assertEqual(self.gate.counts["skipped"], 0)

    def test_question_before_bot_left_in_one_poll_is_offered_then_no_wait(self):
        def advance():
            if self.now == 2:
                self.items.extend([
                    message(self.items[-1]["id"] + 1, "Второй вопрос", bot=True),
                    event(self.items[-1]["id"] + 2, "PARTICIPANT_LEFT", sender="bot-service")])
        self.on_sleep = advance
        output = self.run_chat_output()
        self.assertEqual(len(self.drafts), 2)
        self.assertEqual(self.drafts[1].reply_to_message, "Второй вопрос")
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(self.now, 2)
        self.assertIn("Робот вышел из чата", output)

    def test_join_during_follow_up_wait_is_ignored(self):
        def advance():
            if self.now == 2:
                self.items.append(event(self.items[-1]["id"] + 1, "PARTICIPANT_JOINED", sender="other"))
            elif self.now == 4:
                self.items.append(message(self.items[-1]["id"] + 1, "Второй вопрос", bot=True))
            elif len(self.drafts) == 2:
                self.items.append(message(self.items[-1]["id"] + 1, BOT_RECRUITER_ANSWER, bot=True))
        self.on_sleep = advance
        self.run_chat()
        self.assertEqual(len(self.drafts), 2)
        self.assertEqual(self.drafts[1].reply_to_message, "Второй вопрос")
        self.assertEqual(self.sender.call_count, 2)

    def test_upstream_history_skips_system_events_but_counts_them(self):
        lines, count = Operation.format_history_lines(
            snapshot(message(1, "Вопрос?", bot=True), event(2, "PARTICIPANT_LEFT")))
        self.assertEqual(len(lines), 1)
        self.assertIn("Вопрос?", lines[0])
        self.assertEqual(count, 2)


class RefinementChecks(unittest.TestCase):
    # Real upstream process_chat -> complete -> wire payload, with every HTTP
    # send intercepted locally. No gateway or HH service is contacted.
    read = ConversationChecks.read
    sleep = ConversationChecks.sleep
    approve = ConversationChecks.approve
    send = ConversationChecks.send
    process = ConversationChecks.process

    def setUp(self):
        ConversationChecks.setUp(self)
        self.gate.original_process = Operation.process_chat
        self.gate.original_complete = OpenAIClient.complete
        self.operation.build_placeholders = lambda chat, *args: Operation.build_placeholders(
            self.operation, chat, *args)
        self.operation.base_placeholders = {}
        self.operation.options_prompt = "%(reply_to_message)s %(options)s"
        self.operation.user_prompt = "%(reply_to_message)s %(history)s"
        self.operation.ai_init_args = {"system_prompt": "RESUME %(resume_title)s %(resume_skills)s"}
        self.operation.dry_run = False
        self.operation.max_history = sys.maxsize
        self.operation.ignored_chats = set()
        self.operation.chat_page_url = "https://chatik.hh.ru/"
        self.operation.match_option = Operation.match_option
        self.operation.load_history = lambda chat: ([m["text"] for m in self.items], 1, True)
        self.operation.send_chat_message = lambda *a, **kw: self.gate.message(self.operation, *a, **kw)
        self.operation.tool = SimpleNamespace(get_ai_client=self.client)
        self.payloads, self.clients = [], []
        self.outputs = iter(("Original", "Revised"))
        self.on_model = lambda: None
        self.sender.side_effect = self.http

    def client(self, **kwargs):
        client = OpenAIClient("http://127.0.0.1:8000/v1", "offline", delay=1e-12, **kwargs)
        self.clients.append(client)
        # Use keyword message too, and exercise real complete's keyword-only
        # history/settings. These must never become arbitrary payload fields.
        return SimpleNamespace(complete=lambda prompt: self.gate.complete(
            client, message=prompt, history=[{"role": "user", "content": "CALL HISTORY"}],
            temperature=0.25, max_completion_tokens=77, seed=42))

    def http(self, session, prepared, **kwargs):
        if prepared.url.endswith("/v1/chat/completions"):
            payload = json.loads(prepared.body)
            self.payloads.append(payload)
            self.on_model()
            output = next(self.outputs)
            if isinstance(output, Exception):
                raise output
            response = requests.Response()
            response.status_code = 200
            response.request = prepared
            response.url = prepared.url
            response._content = json.dumps({"choices": [{"message": {"content": output}}]}).encode()
            return response
        return SimpleNamespace(status_code=200)

    def run_chat(self, chat=CHAT):
        output = StringIO()
        with patch.object(requests.sessions.Session, "send", lambda session, req, **kw:
                          self.gate.send(session, req, **kw)), redirect_stdout(output):
            self.gate.process(self.operation, chat, set())
        return output.getvalue()

    def answers(self, *values):
        self.gate.approve = Mock(side_effect=values)

    def posted(self):
        return [json.loads(c.args[1].body)["text"] for c in self.sender.call_args_list
                if c.args[1].url.endswith("/chatik/api/send")]

    def revision(self, index=1):
        return json.loads(self.payloads[index]["messages"][-1]["content"])

    def test_instruction_revises_wire_context_and_exact_post_only_after_yes(self):
        revised = "  Короткий ответ\nбез приветствия  "
        self.outputs = iter(("Original", revised))
        self.answers("edit", "сделай короче, убери приветствие", "yes")
        self.on_model = lambda: self.assertEqual(self.posted(), [])
        output = self.run_chat()
        self.assertEqual(self.posted(), [revised])
        self.assertIn(f"дословно):\n{revised}\n", output)
        self.assertEqual(len(self.clients), 1)
        original, revision = self.payloads
        self.assertEqual(set(original), set(revision))
        self.assertEqual(revision["temperature"], 0.25)
        self.assertEqual(revision["max_completion_tokens"], 77)
        self.assertEqual(revision["seed"], 42)
        self.assertEqual(original["messages"][1:-1], revision["messages"][1:-1])
        self.assertTrue(revision["messages"][0]["content"].startswith(
            original["messages"][0]["content"]))
        self.assertIn("untrusted employer", revision["messages"][0]["content"])
        self.assertEqual(self.revision()["original_prompt"], original["messages"][-1]["content"])
        self.assertEqual(self.revision()["current_candidate"], "Original")
        self.assertEqual(self.revision()["user_feedback"], "сделай короче, убери приветствие")
        self.assertEqual(self.gate.counts["drafts"], 2)
        self.assertEqual(self.gate.approve.call_count, 3)
        self.assertIsNone(self.gate.completion_context)
        self.assertEqual(self.gate.feedback, [])

    def test_repeated_refinements_use_latest_candidate_and_previous_feedback(self):
        self.outputs = iter(("Original", "First revision", "Final revision"))
        self.answers("e", "shorter", "edit", "more polite", "yes")
        self.run_chat()
        self.assertEqual(self.revision(2)["current_candidate"], "First revision")
        self.assertEqual(self.revision(2)["previous_user_feedback"], ["shorter"])
        self.assertEqual(self.revision(2)["user_feedback"], "more polite")
        self.assertEqual(self.revision(2)["original_prompt"], self.revision()["original_prompt"])
        self.assertEqual(self.posted(), ["Final revision"])
        self.assertEqual(self.gate.counts["drafts"], 3)

    def test_positional_complete_preserves_explicit_system_history_and_same_client(self):
        factory = self.client
        history = [{"role": "assistant", "content": "EXPLICIT HISTORY"}]
        def client(**kwargs):
            factory(**kwargs)
            actual_client = self.clients[-1]
            return SimpleNamespace(complete=lambda prompt: self.gate.complete(
                actual_client, prompt, system_prompt="EXPLICIT SYSTEM",
                history=history, model="override-model"))
        self.operation.tool.get_ai_client = client
        self.items = [message(bot=False)]
        self.answers("e", "shorter", "yes")
        self.run_chat()
        original, revision = self.payloads
        self.assertEqual(original["messages"][0]["content"], "EXPLICIT SYSTEM")
        self.assertTrue(revision["messages"][0]["content"].startswith("EXPLICIT SYSTEM\n\n"))
        self.assertEqual(revision["messages"][1], history[0])
        self.assertEqual(history, [{"role": "assistant", "content": "EXPLICIT HISTORY"}])
        self.assertEqual(revision["model"], "override-model")
        for call in self.sender.call_args_list:
            if call.args[1].url.endswith("/v1/chat/completions"):
                self.assertIs(call.args[0], self.clients[0].session)
        self.assertNotIn("Revise the candidate", self.clients[0].system_prompt)

    def test_cancel_blank_and_interruption_never_call_revision_model(self):
        for instruction in ("/cancel", "", "  ", EOFError(), KeyboardInterrupt()):
            with self.subTest(instruction=instruction):
                self.setUp()
                self.answers("edit", instruction, "yes")
                if isinstance(instruction, BaseException):
                    with self.assertRaises(SystemExit):
                        self.run_chat()
                    self.assertEqual(self.posted(), [])
                else:
                    self.run_chat()
                    self.assertEqual(self.posted(), ["Original"])
                self.assertEqual(len(self.payloads), 1)
                self.assertIsNone(self.gate.completion_context)

    def test_revision_error_or_empty_never_posts(self):
        for result in (ValueError("private-error"), "", "  ", None):
            with self.subTest(result=result):
                self.setUp()
                self.outputs = iter(("Original", result))
                self.answers("edit", "shorter", "yes")
                with self.assertRaises(SystemExit):
                    self.run_chat()
                self.assertEqual(self.posted(), [])
                self.assertEqual(self.gate.counts["drafts"], 1)
                self.assertIsNone(self.gate.completion_context)

    def test_options_exact_allowed_string_or_invalid_keeps_candidate(self):
        for result, expected, drafts in (("- Нет.", "  Нет \t", 2),
                                         ("invented", "Да", 1)):
            with self.subTest(result=result):
                self.setUp()
                self.items = [message(options=["Да", "  Нет \t"])]
                self.outputs = iter(("Да", result))
                self.answers("edit", "выбери нет", "yes")
                output = self.run_chat()
                self.assertEqual(self.posted(), [expected])
                self.assertEqual(self.gate.counts["drafts"], drafts)
                self.assertEqual(self.revision()["allowed_options"], ["Да", "  Нет \t"])
                if result == "invented":
                    self.assertIn("недопустимый вариант", output)

    def test_changed_question_or_identity_before_during_revision_or_approval_never_posts(self):
        for change in ("question", "identity"):
            for when in ("instruction", "revision", "approval"):
                with self.subTest(change=change, when=when):
                    self.setUp()
                    def mutate():
                        if change == "question":
                            self.items.append(message(2, "NEW QUESTION", bot=True))
                        else:
                            self.current_me = "other-applicant"
                    def model():
                        if when == "revision" and len(self.payloads) == 2:
                            mutate()
                    self.on_model = model
                    answers = iter(("edit", "shorter", "yes"))
                    def approve(_):
                        answer = next(answers)
                        if (when == "instruction" and answer == "shorter"
                                or when == "approval" and answer == "yes"):
                            mutate()
                        return answer
                    self.gate.approve = approve
                    self.run_chat()
                    self.assertEqual(self.posted(), [])
                    self.assertEqual(len(self.payloads), 1 if when == "instruction" else 2)

    def test_refined_bot_answer_then_q2_has_new_client_and_no_feedback_leak(self):
        self.outputs = iter(("Original", "Refined A1", "A2", "Refined A2"))
        self.answers("edit", "private Q1 feedback", "yes", "edit", "Q2 feedback", "yes")
        def advance():
            if len(self.payloads) == 2:
                self.items.append(message(3, "Q2", bot=True))
            else:
                self.items.append(message(5, BOT_RECRUITER_ANSWER, bot=True))
        self.on_sleep = advance
        self.run_chat()
        self.assertEqual(self.posted(), ["Refined A1", "Refined A2"])
        self.assertEqual(len(self.clients), 2)
        q2 = self.payloads[2]["messages"][-1]["content"]
        self.assertIn("Refined A1", q2)
        self.assertIn("Q2", q2)
        self.assertNotIn("private Q1 feedback", json.dumps(self.payloads[2:]))
        self.assertEqual(self.revision(3)["current_candidate"], "A2")
        self.assertEqual(self.revision(3)["previous_user_feedback"], [])

    def test_next_chat_has_own_resume_client_prompt_and_no_previous_feedback(self):
        self.items = [message(bot=False)]
        self.outputs = iter(("Original", "First revised", "Second", "Second revised"))
        self.answers("edit", "private first feedback", "yes", "edit", "second feedback", "yes")
        self.run_chat()
        self.items = [message(10, "SECOND CHAT")]
        other = replace(CHAT, chat_id=456, resume_title="OTHER RESUME", skills="OTHER SKILLS")
        self.run_chat(other)
        self.assertEqual(len(self.clients), 2)
        self.assertNotIn("private first feedback", json.dumps(self.payloads[2:]))
        self.assertNotIn("First revised", json.dumps(self.payloads[2:]))
        self.assertIn("OTHER RESUME", self.payloads[3]["messages"][0]["content"])
        self.assertEqual(self.revision(3)["previous_user_feedback"], [])
        self.assertEqual(self.revision(3)["current_candidate"], "Second")

    def test_button_revision_reads_actual_chat_history_without_private_feedback_or_other_chat(self):
        self.outputs = iter(("Original", "Actual sent A1", "Да", "Нет", "Да", "Нет"))
        self.answers("edit", "private Q1 feedback", "yes",
                     "edit", "как в предыдущем ответе", "yes",
                     "edit", "учти переписку", "yes")
        def advance():
            if len(self.payloads) == 2:
                self.items.append(message(3, "Q2 buttons", bot=True, options=["Да", "Нет"]))
            else:
                self.items.append(message(5, BOT_RECRUITER_ANSWER, bot=True))
        self.on_sleep = advance
        self.run_chat()
        # Upstream's initial button prompt omits history; the refinement must
        # independently include the actual sent answer from the fresh snapshot.
        self.assertNotIn("Actual sent A1", self.payloads[2]["messages"][-1]["content"])
        revision = self.revision(3)
        self.assertEqual(revision["untrusted_chat_history"], [
            {"id": 1, "sender": "other", "text": CHAT.reply_to_message},
            {"id": 2, "sender": "applicant", "text": "Actual sent A1"},
            {"id": 3, "sender": "other", "text": "Q2 buttons"},
        ])
        self.assertEqual(revision["previous_user_feedback"], [])
        self.assertNotIn("private Q1 feedback", json.dumps(self.payloads[2:]))
        self.assertIn("untrusted source data", self.payloads[3]["messages"][0]["content"])
        self.assertEqual(self.posted(), ["Actual sent A1", "Нет"])

        self.items = [message(10, "Other chat answer", sender="me"),
                      message(11, "Other chat buttons", options=["Да", "Нет"])]
        self.run_chat(replace(CHAT, chat_id=456, resume_title="OTHER RESUME"))
        other = self.revision(5)
        self.assertEqual(other["untrusted_chat_history"], [
            {"id": 10, "sender": "applicant", "text": "Other chat answer"},
            {"id": 11, "sender": "other", "text": "Other chat buttons"},
        ])
        self.assertEqual(other["previous_user_feedback"], [])
        for private in ("Actual sent A1", "Q2 buttons", "private Q1 feedback", "как в предыдущем ответе"):
            self.assertNotIn(private, json.dumps(self.payloads[4:], ensure_ascii=False))
        self.assertEqual(self.posted(), ["Actual sent A1", "Нет", "Нет"])

    def test_revision_is_not_approval_and_original_origin_remains_unchanged(self):
        self.answers("edit", "shorter", "no")
        original_message = self.gate.message
        origins = []
        def message_with_origin(*args, **kwargs):
            try:
                return original_message(*args, **kwargs)
            finally:
                origins.append(self.gate.generated)
        self.gate.message = message_with_origin
        self.run_chat()
        self.assertEqual(origins, ["Original"])
        self.assertEqual(self.posted(), [])


class ReplyChecks(unittest.TestCase):
    def setUp(self):
        self.sender = Mock(return_value=SimpleNamespace(status_code=200))
        self.gate = ChatGate(self.sender, approve=lambda _: "yes")
        self.gate.current = CHAT
        self.gate.operation = SimpleNamespace(chat_url="https://chatik.hh.ru",
            get_chat_data=lambda chat_id: snapshot(chat_id=chat_id))
        self.gate.generated = TEXT

    def test_fixed_args_and_config(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            profile = home / ".config/pocketnode-hh"
            profile.mkdir(parents=True)
            shutil.copyfile(ROOT / "windows/config.toml", profile / "config.toml")
            argv = arguments(str(profile))
            self.assertEqual(int(argv[argv.index("--max-pages") + 1]), sys.maxsize)
            self.assertEqual(int(argv[argv.index("--max-history") + 1]), sys.maxsize)
            with patch.dict(os.environ, {"CONFIG_DIR": str(profile), "HH_PROFILE_ID": "."}), \
                 patch("reply_chats.Path.home", return_value=home), \
                 patch("requests.sessions.Session.send", side_effect=AssertionError("No network")):
                self.assertIsNone(validate(argv).resume_id)
                for extra in (["--loop"], ["--max-pages", "10"], ["--dry-run"]):
                    with self.subTest(extra=extra), self.assertRaises(SystemExit):
                        validate(argv + extra)
                with self.assertRaises(SystemExit):
                    validate(argv + ["--resume-id", "another-resume"])
                config_path = profile / "config.toml"
                changed = config_path.read_text(encoding="utf-8").replace(
                    '[openai.default]',
                    '[openai.default]\nhistory = [{role = "user", content = "Other resume"}]',
                )
                config_path.write_text(changed, encoding="utf-8")
                with self.assertRaisesRegex(SystemExit, "Cross-resume"):
                    validate(argv)

    def test_only_read_and_local_model_traffic_without_approval(self):
        for method, url in (
            ("GET", "https://chatik.hh.ru/chatik/api/chats"),
            ("GET", "https://hh.ru/applicant/my_resumes"),
            ("POST", "http://127.0.0.1:8000/v1/chat/completions"),
        ):
            self.assertTrue(allowed_read(requests.Request(method, url).prepare()))
        for method, url in (
            ("POST", "https://chatik.hh.ru/chatik/api/mark_read"),
            ("POST", "https://chatik.hh.ru/chatik/api/leave"),
            ("POST", "https://evil.hh.ru.evil.test/chatik/api/send"),
            ("GET", "https://example.com/chatik/api/chats"),
        ):
            with self.subTest(url=url):
                with self.assertRaises(SystemExit):
                    self.gate.send(None, requests.Request(method, url).prepare())
        self.sender.assert_not_called()

    def test_request_target_strips_credentials_query_and_fragment(self):
        prepared = requests.Request(
            "GET", "https://user:password@chatik.hh.ru/chatik/api/chat_data"
            "?chatId=123&token=private#secret",
            headers={"Cookie": "private-cookie", "Authorization": "Bearer private-token"},
        ).prepare()
        self.assertEqual(request_target(prepared),
                          "GET https://chatik.hh.ru/chatik/api/chat_data")
        for url, expected in (
            ("https://hh.ru/resumes/PRIVATE_RESUME?PRIVATE_QUERY=secret", "GET https://hh.ru/resumes/{id}"),
            ("https://PRIVATE_SUBDOMAIN.hh.ru/PRIVATE_PATH", "GET other/other"),
            ("https://chatik.hh.ru/chat/123", "GET https://chatik.hh.ru/chat/{id}"),
        ):
            self.assertEqual(request_target(requests.Request("GET", url).prepare()), expected)

    def test_http_failure_identifies_hh_or_gateway_without_exposing_payload(self):
        self.sender.return_value = SimpleNamespace(status_code=502, text="private-response")
        for method, url, target in (
            ("GET", "https://chatik.hh.ru/chatik/api/chat_data?chatId=123&token=private",
             "GET https://chatik.hh.ru/chatik/api/chat_data"),
            ("POST", "http://127.0.0.1:8000/v1/chat/completions",
             "POST http://127.0.0.1:8000/v1/chat/completions"),
        ):
            self.sender.reset_mock()
            prepared = requests.Request(method, url, json={"text": TEXT}, headers={
                "Authorization": "Bearer private-token", "Cookie": "private-cookie",
            }).prepare()
            with self.subTest(url=url), self.assertRaises(SystemExit) as error:
                self.gate.send(None, prepared)
            self.assertEqual(str(error.exception), f"Stopped after HTTP 502 for {target}")
            self.sender.assert_called_once()
            self.assertEqual(self.gate.counts["attempts"], 0)

    def test_anonymous_resumes_403_asks_for_web_login_without_body(self):
        target = "GET https://hh.ru/applicant/my_resumes"
        state = '{"userType": "anonymous", "private": "private-response"}'
        anonymous = f'<template id="HH-Lux-InitialState">{state}</template>'
        login = (f"HH web session is not logged in (HTTP 403 for {target}); "
                 "run windows/hh-authorize.ps1, then retry")
        for label, status, body, expected in (
            ("plain", 403, anonymous, login),
            ("escaped", 403,
             f'<template id="HH-Lux-InitialState">{html.escape(state)}</template>', login),
            ("applicant", 403,
             '<template id="HH-Lux-InitialState">{"userType": "applicant"}</template>', None),
            ("truncated", 403,
             '<template id="HH-Lux-InitialState">{"userType": "anonym</template>', None),
            ("no-state", 403, "private-response", None),
            ("list", 403, '<template id="HH-Lux-InitialState">[]</template>', None),
            ("deep", 403, '<template id="HH-Lux-InitialState">'
             + "[" * 100000 + "]" * 100000 + "</template>", None),
            ("repeated-marker", 403, '<template id="HH-Lux-InitialState">a' * 20000, None),
            ("status-500", 500, anonymous, f"Stopped after HTTP 500 for {target}"),
        ):
            response = requests.Response()
            response.status_code, response._content = status, body.encode()
            self.sender.reset_mock(return_value=True)
            self.sender.return_value = response
            self.gate.counts["failures"] = 0
            prepared = requests.Request("GET", "https://hh.ru/applicant/my_resumes",
                                        headers={"Cookie": "private-cookie"}).prepare()
            started = time.monotonic()
            with self.subTest(label), self.assertRaises(SystemExit) as error:
                self.gate.send(None, prepared)
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(str(error.exception), expected or f"Stopped after HTTP 403 for {target}")
            self.sender.assert_called_once()
            self.assertEqual(self.gate.counts["failures"], 1)
        response.status_code, response._content = 403, anonymous.encode()
        post = requests.Request("POST", "https://hh.ru/applicant/my_resumes").prepare()
        self.assertFalse(anonymous_web_session(post, response))
        prepared = requests.Request("GET", "https://chatik.hh.ru/chatik/api/chats").prepare()
        response._content = f'<template id="HH-Lux-InitialState">{state}</template>'.encode()
        with self.assertRaises(SystemExit) as error:
            self.gate.send(None, prepared)
        self.assertEqual(str(error.exception),
                         "Stopped after HTTP 403 for GET https://chatik.hh.ru/chatik/api/chats")

    def test_network_failure_identifies_endpoint_without_exception_secrets(self):
        self.sender.side_effect = requests.ConnectionError("private-token private-response")
        prepared = requests.Request(
            "GET", "https://chatik.hh.ru/chatik/api/chat_data?token=private",
        ).prepare()
        with self.assertRaises(SystemExit) as error:
            self.gate.send(None, prepared)
        self.assertEqual(str(error.exception),
                          "Network failure for GET https://chatik.hh.ru/chatik/api/chat_data; "
                          "category=unknown; types=ConnectionError; codes=none; "
                          "transport_attempts=1; stopped")
        self.sender.assert_called_once()

    def test_successful_read_is_unchanged(self):
        prepared = requests.Request("GET", "https://chatik.hh.ru/chatik/api/chat_data").prepare()
        self.assertIs(self.gate.send(None, prepared), self.sender.return_value)
        self.sender.assert_called_once_with(None, prepared, timeout=(10, 15))

    def test_poll_get_timeout_is_capped_to_remaining_budget(self):
        self.gate.clock = lambda: 58
        self.gate.poll_deadline = 60
        prepared = requests.Request("GET", "https://chatik.hh.ru/chatik/api/chat_data").prepare()
        for supplied, expected in ((None, (2, 2)), (9, (2, 2)),
                                   ((1, 8), (1, 2)), ((None, 1), (2, 1))):
            with self.subTest(timeout=supplied):
                self.sender.reset_mock()
                self.gate.send(None, prepared, timeout=supplied)
                self.sender.assert_called_once_with(None, prepared, timeout=expected)
        self.sender.reset_mock()
        self.gate.clock = lambda: 60
        with self.assertRaises(ChatSkipped):
            self.gate.send(None, prepared)
        self.sender.assert_not_called()

    def test_failed_chat_post_identifies_endpoint_without_retry(self):
        self.gate.pending = (123, TEXT)
        self.sender.return_value = SimpleNamespace(status_code=502, text="private-response")
        with self.assertRaises(SystemExit) as error:
            self.gate.send(None, request())
        self.assertEqual(str(error.exception),
                          "Chat send returned HTTP 502 for POST https://chatik.hh.ru/chatik/api/send; "
                          "chat_id=123; check HH before retrying")
        self.sender.assert_called_once()
        self.assertEqual(self.gate.counts["attempts"], 1)
        self.assertEqual(self.gate.counts["http_success"], 0)

    def test_chats_filter_rejected_without_count_limit(self):
        rejected = SimpleNamespace(is_discard=True)
        other = ChatToReply(**(vars(CHAT) | {
            'chat_id': 456, 'resume_id': 'other-published-resume',
            'resume_title': 'SRE-инженер',
        }))
        self.gate.original_chats = lambda *_a, **_kw: [rejected, CHAT, other, CHAT, CHAT]
        selected = self.gate.chats(None)
        self.assertEqual(len(selected), 4)
        self.assertIs(selected[1], other)
        self.assertEqual(self.gate.counts["chats"], 4)
        self.gate.original_process = lambda _operation, chat, _blacklist: chat.resume_id
        with patch.object(self.gate, "original_process", return_value=None) as process:
            self.gate.process(self.gate.operation, other, set())
        self.assertEqual(process.call_args.args[1].resume_id, 'other-published-resume')

    def test_no_skips_current_chat_without_post(self):
        self.gate.original_message = Mock()
        for answer in ("no", "n", " N "):
            self.gate.approve = lambda _, value=answer: value
            with self.subTest(answer=answer), redirect_stdout(StringIO()), \
                 self.assertRaises(ChatSkipped):
                self.gate.message(self.gate.operation, 123, TEXT)
        self.gate.original_message.assert_not_called()
        self.sender.assert_not_called()
        self.gate.original_complete = lambda *_a, **_kw: TEXT
        self.gate.original_process = lambda operation, chat, _: self.gate.message(
            operation, chat.chat_id, self.gate.complete(None, 'prompt'))
        output = StringIO()
        with redirect_stdout(output):
            self.assertIsNone(self.gate.process(self.gate.operation, CHAT, set()))
        self.assertIn('Пропущен чат', output.getvalue())
        self.assertEqual(self.gate.counts['skipped'], 1)

    def test_invalid_or_missing_approval_stops_before_post(self):
        self.gate.original_message = Mock()
        for answer in ('', 'maybe', None):
            self.gate.approve = lambda _, value=answer: value
            with self.subTest(answer=answer), redirect_stdout(StringIO()), \
                 self.assertRaisesRegex(SystemExit, 'No approval'):
                self.gate.message(self.gate.operation, 123, TEXT)
        self.gate.approve = Mock(side_effect=EOFError)
        with redirect_stdout(StringIO()), self.assertRaisesRegex(SystemExit, 'No approval'):
            self.gate.message(self.gate.operation, 123, TEXT)
        self.gate.original_message.assert_not_called()
        self.sender.assert_not_called()

    def test_skipped_chat_continues_to_next_without_false_sent_message(self):
        second = replace(CHAT, chat_id=456)
        operation = self.gate.operation
        answers = iter(('n', 'yes'))
        self.gate.approve = lambda _: next(answers)
        self.gate.original_complete = lambda *_a, **_kw: TEXT

        def send_message(operation, chat_id, text, refer_url=None):
            return self.gate.send(None, request({
                'chatId': chat_id, 'text': text, 'idempotencyKey': str(uuid.uuid4()),
            }))

        def process(operation, chat, blacklist):
            draft = self.gate.complete(None, 'prompt')
            self.gate.message(operation, chat.chat_id, draft)
            print('Upstream reports sent')

        self.gate.original_message = send_message
        self.gate.original_process = process
        output = StringIO()
        with redirect_stdout(output):
            for chat in (CHAT, second):
                self.gate.process(operation, chat, set())
        self.assertEqual(self.gate.counts['skipped'], 1)
        self.assertEqual(self.gate.counts['drafts'], 2)
        self.assertEqual(self.gate.counts['attempts'], 1)
        self.assertEqual(self.gate.counts['http_success'], 1)
        self.assertEqual(output.getvalue().count('Upstream reports sent'), 1)
        self.sender.assert_called_once()
        self.assertEqual(json.loads(self.sender.call_args.args[1].body)['chatId'], 456)

    def test_more_than_three_approved_replies(self):
        operation = self.gate.operation
        self.gate.original_complete = lambda *_a, **_kw: TEXT

        def send_message(_operation, chat_id, text, refer_url=None):
            return self.gate.send(None, request({
                "chatId": chat_id, "text": text, "idempotencyKey": str(uuid.uuid4()),
            }))

        def process(_operation, chat, _blacklist):
            draft = self.gate.complete(None, "prompt")
            self.gate.message(operation, chat.chat_id, draft)

        self.gate.original_message = send_message
        self.gate.original_process = process
        with redirect_stdout(StringIO()):
            for chat_id in range(123, 127):
                chat = replace(CHAT, chat_id=chat_id)
                self.gate.process(operation, chat, set())
        self.assertEqual(self.gate.counts["drafts"], 4)
        self.assertEqual(self.gate.counts["attempts"], 4)
        self.assertEqual(self.gate.counts["http_success"], 4)
        self.assertEqual(self.sender.call_count, 4)

    def test_exact_review_and_payload(self):
        self.gate.original_message = Mock()
        self.gate.approve = Mock(return_value=" Y ")
        output = StringIO()
        with redirect_stdout(output):
            self.gate.message(self.gate.operation, 123, TEXT)
        for fragment in ("Компания: Example", "Вакансия: DevOps", "Когда удобно", TEXT):
            self.assertIn(fragment, output.getvalue())
        self.gate.approve.assert_called_once_with("Отправить этот ответ? [yes/no/edit/manual]: ")
        self.gate.pending = (123, TEXT)
        with redirect_stdout(StringIO()):
            self.gate.send(None, request())
        self.sender.assert_called_once()
        self.assertEqual(self.gate.counts["attempts"], 1)
        self.assertEqual(self.gate.counts["http_success"], 1)
        self.assertIsNone(self.gate.pending)
        with self.assertRaisesRegex(SystemExit, "without approval"):
            self.gate.send(None, request())

    def test_payload_mismatch_and_destination_rejected(self):
        for bad in (
            request({"chatId": 444, "text": TEXT, "idempotencyKey": str(uuid.uuid4())}),
            request({"chatId": 123, "text": "changed", "idempotencyKey": str(uuid.uuid4())}),
            request({"chatId": 123, "text": TEXT, "idempotencyKey": "not-uuid"}),
            request(url="https://chatik.hh.ru/chatik/api/send?x=1"),
            request(url="https://other.hh.ru/chatik/api/send"),
        ):
            self.gate.pending = (123, TEXT)
            with self.subTest(url=bad.url, body=bad.body), self.assertRaises(SystemExit):
                self.gate.send(None, bad)
        self.sender.assert_not_called()

    def test_button_answer_must_be_exact_option(self):
        option_chat = SimpleNamespace(**(vars(CHAT) | {"reply_options": ["Да", "Нет"]}))
        self.gate.current = option_chat
        self.gate.generated = "- Да."
        self.gate.original_message = Mock()
        with redirect_stdout(StringIO()):
            self.gate.message(self.gate.operation, 123, "Да")
        self.gate.original_message.assert_called_once()
        with self.assertRaisesRegex(SystemExit, "reviewed option"):
            self.gate.message(self.gate.operation, 123, "Нет")

    def edited_reply(self, answers, initial=TEXT):
        self.gate.approve = Mock(side_effect=answers)
        def send(operation, chat_id, text, refer_url=None):
            self.assertEqual(self.gate.pending, (chat_id, text))
            return self.gate.send(None, request({
                "chatId": chat_id, "text": text, "idempotencyKey": str(uuid.uuid4())}))
        self.gate.original_message = Mock(side_effect=send)
        output = StringIO()
        with redirect_stdout(output):
            self.gate.message(self.gate.operation, 123, initial)
        return output.getvalue()

    def test_multiline_edit_is_exact_separately_reviewed_and_sent(self):
        edited = "  Первая строка  \n\nВторая строка "
        output = self.edited_reply(["manual", "  Первая строка  ", "", "Вторая строка ", "/done", "yes"])
        self.assertIn(f"дословно):\n{edited}\n", output)
        self.assertEqual(json.loads(self.sender.call_args.args[1].body)["text"], edited)
        self.assertEqual(self.gate.generated, TEXT)
        self.assertEqual(self.gate.sent_text, edited)
        self.assertIsNone(self.gate.pending)
        self.assertEqual(self.gate.approve.call_count, 6)

    def test_repeated_edits_cancel_and_empty_retain_current_candidate(self):
        output = self.edited_reply([
            "manual", "first", "/done", "manual", "discard", "/cancel",
            "manual", "  ", "", "/done", "manual", "final", "/done", "yes",
        ])
        self.assertEqual(output.count("дословно):\nfirst\n"), 3)
        self.assertIn("Пустой ответ", output)
        self.assertEqual(json.loads(self.sender.call_args.args[1].body)["text"], "final")
        self.assertEqual(self.gate.generated, TEXT)

    def test_no_or_interruption_after_edit_never_sends(self):
        for answers, error in (
            (["manual", "new", "/done", "no"], ChatSkipped),
            (["manual", "new", EOFError()], SystemExit),
            (["manual", KeyboardInterrupt()], SystemExit),
            (["manual", "new", "/done", EOFError()], SystemExit),
        ):
            with self.subTest(answers=answers):
                self.setUp()
                with self.assertRaises(error):
                    self.edited_reply(answers)
                self.gate.original_message.assert_not_called()
                self.sender.assert_not_called()
                self.assertIsNone(self.gate.pending)

    def test_button_edit_only_number_preserves_exact_option(self):
        options = ["Да", "  Нет \t"]
        self.gate.current = replace(CHAT, reply_options=options)
        self.gate.generated = "Да"
        output = self.edited_reply([
            "manual", "arbitrary", "manual", "0", "manual", "3", "manual", "Нет",
            "manual", "/cancel", "manual", "2", "yes",
        ], initial="Да")
        self.assertIn("2:   Нет \t", output)
        self.assertEqual(output.count("Неверный номер"), 4)
        self.assertEqual(json.loads(self.sender.call_args.args[1].body)["text"], options[1])
        self.assertEqual(self.gate.generated, "Да")

    def test_button_edit_interruption_never_sends(self):
        self.gate.current = replace(CHAT, reply_options=["Да", "Нет"])
        self.gate.generated = "Да"
        with self.assertRaises(SystemExit):
            self.edited_reply(["manual", EOFError()], initial="Да")
        self.sender.assert_not_called()

    def test_origin_tampering_rejected_before_edit_prompt(self):
        for buttons in (False, True):
            with self.subTest(buttons=buttons):
                self.setUp()
                if buttons:
                    self.gate.current = replace(CHAT, reply_options=["Да", "Нет"])
                    self.gate.generated = "Да"
                self.gate.approve = Mock(return_value="edit")
                with self.assertRaisesRegex(SystemExit, "differs"):
                    self.gate.message(self.gate.operation, 123, "Нет" if buttons else "tampered")
                self.gate.approve.assert_not_called()
                self.sender.assert_not_called()

    def test_uncertain_send_does_not_retry(self):
        self.gate.pending = (123, TEXT)
        self.sender.side_effect = requests.ConnectionError("lost response")
        with self.assertRaisesRegex(SystemExit, "uncertain"):
            self.gate.send(None, request())
        self.sender.assert_called_once()
        self.assertEqual(self.gate.counts["attempts"], 1)
        self.assertEqual(self.gate.counts["http_success"], 0)

    def test_transport_diagnostics_are_allowlisted_and_approval_consumed(self):
        for kind in (requests.ConnectTimeout, requests.ReadTimeout,
                     requests.exceptions.SSLError, requests.exceptions.ProxyError,
                     requests.ConnectionError, requests.Timeout,
                     requests.exceptions.ChunkedEncodingError, requests.RequestException):
            self.sender.reset_mock()
            self.gate.pending = (123, TEXT)
            secret = "private-token private-cookie private-response " + TEXT
            error = kind(secret, request=request(), response=Mock(text=secret))
            self.sender.side_effect = error
            with self.subTest(kind=kind), self.assertRaises(SystemExit) as caught:
                self.gate.send(None, request())
            message = str(caught.exception)
            self.assertIn(f"chat_id=123; {kind.__name__}:", message)
            self.assertIn("check HH manually before any retry", message)
            for private in ("private-token", "private-cookie", "private-response", TEXT):
                self.assertNotIn(private, message)
            self.assertIsNone(caught.exception.__cause__)
            self.assertTrue(caught.exception.__suppress_context__)
            with self.assertRaisesRegex(SystemExit, "without approval"):
                self.gate.send(None, request())
            self.sender.assert_called_once()
        self.assertIn("chat_id=unknown", chat_send_failure_detail(error, "private-token"))

    def test_fresh_pools_only_after_approval_preserve_payload_and_settings(self):
        events = []
        prepared = request()
        with requests.Session() as session:
            session.cookies.set("test-cookie", "offline-cookie")
            session.headers["X-Test"] = "offline-header"
            session.proxies = {"https": "http://offline-proxy.test:8080"}
            session.verify = "offline-ca.pem"
            session.cert = "offline-cert.pem"
            adapter = session.get_adapter(prepared.url)
            self.assertEqual(adapter.max_retries.total, 0)

            def approve(_):
                self.assertEqual(events, [])
                events.append("approved")
                return "yes"

            def send(actual_session, actual_request, **kwargs):
                events.append("sent")
                self.assertEqual(events, ["approved", "closed", "sent"])
                self.assertIs(actual_session, session)
                self.assertIs(actual_request, prepared)
                self.assertEqual(json.loads(actual_request.body)["text"], TEXT)
                self.assertEqual(session.cookies.get("test-cookie"), "offline-cookie")
                self.assertEqual(session.headers["X-Test"], "offline-header")
                self.assertEqual(session.proxies, {"https": "http://offline-proxy.test:8080"})
                self.assertEqual(session.verify, "offline-ca.pem")
                self.assertEqual(session.cert, "offline-cert.pem")
                self.assertIs(session.get_adapter(prepared.url), adapter)
                self.assertEqual(kwargs, {"timeout": (10, 60), "allow_redirects": False})
                return SimpleNamespace(status_code=200)

            self.gate.approve = approve
            self.gate.sender = Mock(side_effect=send)
            self.gate.original_message = lambda *_a, **_kw: self.gate.send(
                session, prepared, timeout=None)
            close = session.close

            def close_pools():
                events.append("closed")
                close()

            with patch.object(session, "close", side_effect=close_pools) as closed, \
                 redirect_stdout(StringIO()):
                self.gate.message(self.gate.operation, 123, TEXT)
            closed.assert_called_once()
            self.gate.sender.assert_called_once()

    def test_explicit_timeout_and_transport_options_are_preserved(self):
        for timeout in (5, (2, 9)):
            self.gate.pending = (123, TEXT)
            with redirect_stdout(StringIO()):
                self.gate.send(None, request(), timeout=timeout, verify=True,
                               proxies={"https": "http://offline-proxy.test"})
            self.assertEqual(self.sender.call_args.kwargs, {
                "timeout": timeout, "verify": True,
                "proxies": {"https": "http://offline-proxy.test"}, "allow_redirects": False,
            })

    def test_success_output_does_not_expose_response_or_transport_secrets(self):
        self.gate.pending = (123, TEXT)
        prepared = request()
        prepared.headers.update({"Authorization": "Bearer private-token",
                                 "Cookie": "private-cookie"})
        self.sender.return_value = SimpleNamespace(status_code=200, text="private-response")
        output = StringIO()
        with redirect_stdout(output):
            self.gate.send(None, prepared)
        self.assertEqual(output.getvalue(), "HH вернул HTTP 200 для чата 123\n")
        self.assertEqual(self.gate.counts["http_success"], 1)

    def test_chat_captcha_rejection_never_solves_or_replays_post(self):
        response = Mock(status_code=403)
        response.json.return_value = {"errors": [{"value": "captcha_required",
            "captcha_url": "https://hh.ru/account/captcha?token=private"}]}
        self.sender.return_value = response
        self.gate.pending = (123, TEXT)
        with patch.object(self.gate.captcha, "solve") as solve, \
             self.assertRaisesRegex(SystemExit, "HTTP 403.*chat_id=123"):
            self.gate.send(None, request())
        solve.assert_not_called()
        self.sender.assert_called_once()
        with self.assertRaisesRegex(SystemExit, "without approval"):
            self.gate.send(None, request())

    def test_real_upstream_send_stops_without_retry_on_transport_failure(self):
        operation = Operation()
        operation.chat_url = "https://chatik.hh.ru"
        with requests.Session() as session:
            operation.tool = SimpleNamespace(session=session)
            self.gate.operation = operation
            self.gate.original_message = Operation.send_chat_message
            self.sender.side_effect = requests.ReadTimeout("private-token")
            with patch.object(operation, "_headers", return_value={
                "Content-Type": "application/json"}), \
                 patch.object(session, "send", side_effect=lambda req, **kw:
                              self.gate.send(session, req, **kw)), \
                 redirect_stdout(StringIO()), \
                 self.assertRaisesRegex(SystemExit, "ReadTimeout"):
                self.gate.message(operation, 123, TEXT)
        self.sender.assert_called_once()
        payload = json.loads(self.sender.call_args.args[1].body)
        self.assertEqual(payload["chatId"], 123)
        self.assertEqual(payload["text"], TEXT)
        uuid.UUID(payload["idempotencyKey"])
        self.assertEqual(self.gate.counts["attempts"], 1)
        self.assertEqual(self.gate.counts["http_success"], 0)

    def test_redirect_is_not_followed_or_counted_as_send(self):
        self.gate.pending = (123, TEXT)
        self.sender.return_value = SimpleNamespace(status_code=302)
        with self.assertRaisesRegex(SystemExit, "HTTP 302"):
            self.gate.send(None, request())
        self.assertIs(self.sender.call_args.kwargs["allow_redirects"], False)
        self.assertEqual(self.gate.counts["attempts"], 1)
        self.assertEqual(self.gate.counts["http_success"], 0)

    def test_wrapper_patches_chat_effects_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            profile = home / ".config/pocketnode-hh"
            profile.mkdir(parents=True)
            shutil.copyfile(ROOT / "windows/config.toml", profile / "config.toml")
            argv = arguments(str(profile))
            sender = Mock(return_value=SimpleNamespace(status_code=200))

            def fake_process(operation, chat, blacklist):
                operation.mark_chat_read(123, 111)
                draft = OpenAIClient.complete(None, "prompt")
                operation.send_chat_message(chat.chat_id, draft)

            def fake_message(operation, chat_id, text, refer_url=None):
                return requests.Session().send(request({
                    "chatId": chat_id, "text": text, "idempotencyKey": str(uuid.uuid4()),
                }))

            def fake_main(_argv):
                operation = Operation()
                operation.chat_url = "https://chatik.hh.ru"
                for chat in operation.get_chats_awaiting_reply(1):
                    operation.process_chat(chat, set())
                return 0

            with patch.dict(os.environ, {"CONFIG_DIR": str(profile), "HH_PROFILE_ID": "."}), \
                 patch("reply_chats.Path.home", return_value=home), \
                 patch.object(requests.sessions.Session, "send", sender), \
                  patch.object(Operation, "get_chats_awaiting_reply", return_value=[CHAT]), \
                  patch.object(Operation, "get_chat_data", return_value=snapshot()), \
                 patch.object(Operation, "process_chat", fake_process), \
                 patch.object(Operation, "send_chat_message", fake_message), \
                 patch.object(OpenAIClient, "complete", return_value=TEXT), \
                 patch("reply_chats.main", side_effect=fake_main):
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(run(argv, approve=lambda _: "yes"), 0)
                self.assertIn("попыток отправки 1", output.getvalue())
            sender.assert_called_once()


class StartupNetworkChecks(unittest.TestCase):
    def setUp(self):
        self.response = SimpleNamespace(status_code=200)
        self.sender = Mock(return_value=self.response)
        self.sleep = Mock()
        self.gate = ChatGate(self.sender, sleep=self.sleep)
        self.request = requests.Request("GET", "https://chatik.hh.ru/").prepare()

    def test_sanitized_nested_categories_and_cycles(self):
        secret = "https://user:password@proxy.test/?token=secret Cookie: private"
        cases = [
            (socket.gaierror(-2, secret), "dns"),
            (ssl.SSLError(1, secret), "tls"),
            (requests.exceptions.ProxyError(secret), "proxy"),
            (ConnectionResetError(errno.ECONNRESET, secret), "reset"),
            (ConnectionAbortedError(errno.ECONNABORTED, secret), "reset"),
            (ConnectionRefusedError(errno.ECONNREFUSED, secret), "refusal"),
            (requests.ReadTimeout(secret), "timeout"),
            (u3.NewConnectionError(None, secret), "unknown"),
            (ValueError(secret), "unknown"),
        ]
        for inner, expected in cases:
            with self.subTest(expected=expected):
                middle = u3.MaxRetryError(None, secret, reason=inner)
                outer = requests.ConnectionError(middle, secret)
                inner.__context__ = outer  # Cycle through reason and args.
                category, detail = network_failure(outer)
                self.assertEqual(category, expected)
                for word in ("password", "proxy.test", "token", "Cookie", "private", "secret"):
                    self.assertNotIn(word, detail)
        cause = requests.ConnectionError(secret)
        cause.__cause__ = OSError(10054, secret)
        self.assertEqual(network_failure(cause)[0], "reset")
        self.assertIn("errno=10054", network_failure(cause)[1])
        cause.__context__ = requests.exceptions.SSLError(secret)
        self.assertEqual(network_failure(cause)[0], "tls")  # Never retry TLS + reset.
        chain = requests.ConnectionError(secret)
        for _ in range(100):
            chain = requests.ConnectionError(chain)
        self.assertEqual(network_failure(chain)[0], "unknown")

    def test_never_formats_unknown_types_args_or_nested_containers(self):
        class PrivateSecretError(Exception):
            def __str__(self):
                raise AssertionError("Must not stringify exceptions")
            def __repr__(self):
                raise AssertionError("Must not repr exceptions")
        nested = []
        nested.append(nested)
        reset = OSError("secret")
        reset.winerror = 10053
        nested.append(PrivateSecretError(reset))
        error = requests.ConnectionError(nested)
        category, detail = network_failure(error)
        self.assertEqual(category, "reset")
        self.assertNotIn("PrivateSecretError", detail)
        self.assertIn("winerror=10053", detail)

    def test_startup_transient_success_retains_session_options(self):
        for error in (requests.ConnectTimeout("secret"),
                      requests.ConnectionError(ConnectionResetError(10054, "secret"))):
            with self.subTest(error=type(error).__name__):
                self.sender.reset_mock()
                self.sender.side_effect = [error, self.response]
                with requests.Session() as session, patch.object(session, "close") as close:
                    session.cookies.set("test", "disposable")
                    result = self.gate.send(session, self.request, verify="test-ca", proxies={"https": "test"})
                    self.assertIs(result, self.response)
                    close.assert_called_once()
                    self.assertEqual(session.cookies.get("test"), "disposable")
                    self.assertEqual(session.verify, True)
                self.assertEqual(self.sender.call_count, 2)
                self.assertEqual(self.sender.call_args.kwargs, {
                    "timeout": (10, 15), "verify": "test-ca", "proxies": {"https": "test"}})
        self.assertEqual(self.sleep.call_count, 2)

    def test_repeated_failure_two_attempts_and_safe_diagnostic(self):
        self.sender.side_effect = requests.ReadTimeout("private-token")
        with self.assertRaises(SystemExit) as caught:
            self.gate.send(None, self.request)
        self.assertIn("category=timeout", str(caught.exception))
        self.assertIn("transport_attempts=2", str(caught.exception))
        self.assertNotIn("private-token", str(caught.exception))
        self.assertEqual(self.sender.call_count, 2)
        self.sleep.assert_called_once_with(0.25)
        self.assertEqual(self.gate.counts["failures"], 1)

    def test_nonretryable_categories_and_statuses(self):
        for error in (requests.exceptions.ProxyError("secret"),
                      requests.exceptions.SSLError("secret"),
                      requests.ConnectionError(socket.gaierror(-2, "secret")),
                      requests.ConnectionError(ConnectionRefusedError(10061, "secret")),
                      requests.ConnectionError("Connection reset secret")):
            with self.subTest(error=type(error).__name__):
                self.sender.reset_mock()
                self.sender.side_effect = error
                with self.assertRaises(SystemExit):
                    self.gate.send(None, self.request)
                self.sender.assert_called_once()
        self.sender.side_effect = None
        for status in (302, 403, 429, 500, 502):
            self.sender.reset_mock()
            self.sender.return_value = SimpleNamespace(status_code=status, json=lambda: {})
            if status >= 400:
                with self.assertRaises(SystemExit):
                    self.gate.send(None, self.request)
            else:
                self.gate.send(None, self.request)
            self.sender.assert_called_once()
        self.sleep.assert_not_called()

    def test_exact_scope_and_no_other_request_retries(self):
        for query in ("", "?platform=xhh&dest=iframe", "?dest=iframe&platform=xhh"):
            self.assertTrue(startup_read(requests.Request("GET", "https://chatik.hh.ru/" + query).prepare()))
        targets = [
            ("GET", "https://chatik.hh.ru/?token=secret", None),
            ("GET", "https://chatik.hh.ru/?platform=xhh&dest=iframe&extra=1", None),
            ("GET", "https://chatik.hh.ru/?platform=xhh&dest=iframe&dest=iframe", None),
            ("GET", "https://user:pass@chatik.hh.ru/", None),
            ("GET", "https://chatik.hh.ru/#fragment", None),
            ("GET", "https://chatik.hh.ru:443/", None),
            ("GET", "https://chatik.hh.ru/", "body"),
            ("GET", "https://chatik.hh.ru/chatik/api/chat_data", None),
            ("GET", "https://hh.ru/", None),
            ("POST", "https://chatik.hh.ru/", None),
            ("POST", "http://127.0.0.1:8000/v1/chat/completions", None),
            ("POST", "https://api.hh.ru/token", "grant_type=refresh_token"),
        ]
        for method, url, body in targets:
            with self.subTest(method=method, url=url):
                prepared = requests.Request(method, url, data=body).prepare()
                self.assertFalse(startup_read(prepared))
                self.sender.reset_mock()
                self.sender.side_effect = requests.ReadTimeout("secret")
                with self.assertRaises(SystemExit):
                    self.gate.send(None, prepared)
                self.assertLessEqual(self.sender.call_count, 1)
        self.sleep.assert_not_called()

    def test_all_hh_gets_finite_defaults_explicit_options_unchanged(self):
        for url in ("https://chatik.hh.ru/", "https://hh.ru/redirect", "https://api.hh.ru/me"):
            for options, expected in (({}, (10, 15)), ({"timeout": None}, (10, 15)),
                                      ({"timeout": 7}, 7), ({"timeout": (2, 8)}, (2, 8))):
                self.gate.send(None, requests.Request("GET", url).prepare(), **options)
                self.assertEqual(self.sender.call_args.kwargs["timeout"], expected)

    def test_retry_preserves_explicit_timeout_and_does_not_close_fake_sessions(self):
        for timeout in (4, (1, 8)):
            self.sender.reset_mock()
            self.sender.side_effect = [requests.ReadTimeout(), self.response]
            session = SimpleNamespace(close=Mock())
            self.gate.send(session, self.request, timeout=timeout, cert="test-cert", stream=True)
            self.assertEqual(self.sender.call_count, 2)
            for call in self.sender.call_args_list:
                self.assertEqual(call.kwargs, {"timeout": timeout, "cert": "test-cert", "stream": True})
            session.close.assert_not_called()

    def test_real_requests_redirects_are_guarded_with_finite_timeouts(self):
        def response(status, location=None):
            result = requests.Response()
            result.status_code, result._content = status, b""
            if location:
                result.headers["Location"] = location
            return result
        gate = ChatGate(requests.sessions.Session.send, sleep=self.sleep)
        with requests.Session() as session:
            session.trust_env = False
            adapter = session.get_adapter(self.request.url)
            responses = iter([response(302, "https://chatik.hh.ru/redirect"), response(200)])
            def adapter_send(prepared, **kwargs):
                result = next(responses)
                result.request, result.url = prepared, prepared.url
                return result
            with patch.object(adapter, "send", side_effect=adapter_send) as send, \
                 patch.object(session, "send", side_effect=lambda req, **kw: gate.send(session, req, **kw)):
                self.assertEqual(session.get(self.request.url).status_code, 200)
            self.assertEqual(send.call_count, 2)
            for call in send.call_args_list:
                self.assertEqual(call.kwargs["timeout"], (10, 15))
        self.sleep.assert_not_called()

    def test_failed_startup_run_reports_incomplete_not_zero_inspected_success(self):
        def main(_argv):
            requests.Session().get(self.request.url)
        output = StringIO()
        with tempfile.TemporaryDirectory() as temp, \
             patch("reply_chats.validate"), patch("reply_chats.main", side_effect=main), \
             patch.object(requests.sessions.Session, "send", side_effect=requests.ConnectionError("secret")), \
             redirect_stdout(output), self.assertRaises(SystemExit):
            run([], diagnostics_directory=temp)
        self.assertIn("не завершена", output.getvalue())
        self.assertIn("начата проверка 0", output.getvalue())
        self.assertIn("ошибок 1", output.getvalue())
        self.assertNotIn("просмотрено 0", output.getvalue())

    def test_retry_below_captcha_one_workflow_and_shared_budget(self):
        challenge = requests.Response()
        challenge.status_code = 403
        challenge._content = json.dumps({"errors": [{"value": "captcha_required",
            "captcha_url": "https://hh.ru/account/captcha"}]}).encode()
        for results, attempts, solves, success in (
            ([requests.ReadTimeout(), self.response], 2, 0, True),
            ([challenge, requests.ReadTimeout()], 2, 1, False),
            ([requests.ReadTimeout(), challenge], 2, 1, False),
            ([challenge, self.response], 2, 1, True),
        ):
            with self.subTest(attempts=attempts, solves=solves, success=success):
                self.sender.reset_mock()
                self.sender.side_effect = results
                with patch.object(self.gate.captcha, "solve") as solve, patch.object(
                        self.gate.captcha, "send", wraps=self.gate.captcha.send) as workflow:
                    if success:
                        self.gate.send(None, self.request)
                    else:
                        with self.assertRaises(SystemExit):
                            self.gate.send(None, self.request)
                    self.assertEqual(solve.call_count, solves)
                    workflow.assert_called_once()
                self.assertEqual(self.sender.call_count, attempts)

    def test_no_retry_during_captcha_or_pending_send(self):
        for active, pending, sending in ((True, None, False), (False, (123, TEXT), False),
                                          (False, None, True)):
            self.gate.captcha.active, self.gate.pending = active, pending
            self.gate.sending = sending
            self.sender.reset_mock()
            self.sender.side_effect = requests.ReadTimeout()
            with self.assertRaises(SystemExit):
                self.gate.send(None, self.request)
            self.sender.assert_called_once()
        self.sleep.assert_not_called()


class ChatPaginationChecks(unittest.TestCase):
    """HH ignores `page`; the wrapper must follow the `from=<nextFrom>` cursor."""

    BASE = "https://chatik.hh.ru"

    @staticmethod
    def item(chat_id):
        return {"id": chat_id, "currentParticipantId": "me",
                "resources": {"RESUME": ["r1"], "VACANCY": ["v1"]},
                "lastMessage": {"text": "Когда удобно?", "participantId": "recruiter"}}

    @classmethod
    def page(cls, ids, cursor=None):
        chats = {"found": 47, "items": [cls.item(chat_id) for chat_id in ids]}
        if cursor is not None:
            chats["nextFrom"] = cursor
        return {"chats": chats, "resources": {
            "resumeHashById": {"r1": RESUME}, "resumes": {"r1": {"userId": 1}},
            "vacancies": {"v1": {"name": "DevOps", "company": {"id": 5, "name": "Example"}}}}}

    def operation(self):
        operation = Operation()
        operation.tool = SimpleNamespace(
            session=requests.Session(), xsrf_token="xsrf",
            get_resumes=lambda: [{"id": RESUME, "status": {"id": "published"}}])
        operation.resume_id, operation.ignored_chats, operation.user = None, set(), {"id": 1}
        operation.__dict__.update(chat_url=self.BASE, chat_page_url=f"{self.BASE}/chats")
        return operation

    def collect(self, responder):
        """Run the real upstream loop through the gate against a fake HH."""
        requests_seen = []

        def fake_hh(_session, prepared, **_kwargs):
            url = urlsplit(prepared.url)
            assert url.path == "/chatik/api/chats", url.path
            query = parse_qs(url.query)
            requests_seen.append(query)
            response = requests.Response()
            response.status_code, response.url, response.request = 200, prepared.url, prepared
            response.headers["Content-Type"] = "application/json"
            response._content = json.dumps(responder(query.get("from", [None])[0])).encode()
            return response

        gate = ChatGate(fake_hh, approve=lambda _: "no")
        gate.original_chats = Operation.get_chats_awaiting_reply
        with patch.object(requests.sessions.Session, "send",
                          lambda session, prepared, **kw: gate.send(session, prepared, **kw)), \
             patch.object(Operation, "get_chats",
                          lambda op, page, refer_url=None, unread=False:
                          gate.chats_page(op, page, refer_url, unread)), \
             patch.object(Operation, "parse_chat_item",
                          lambda op, *a, **kw: gate.parse_chat_item(op, *a, **kw)), \
             patch.object(Operation, "is_chat_awaiting_reply",
                          lambda op, chat: gate.is_chat_awaiting_reply(op, chat)):
            chats = gate.chats(self.operation(), sys.maxsize)
        return gate, chats, requests_seen

    def listed_with_last(self, last):
        def responder(_cursor):
            body = self.page([1])
            body["chats"]["items"][0]["lastMessage"] = last
            return body
        _gate, chats, _seen = self.collect(responder)
        return chats

    def test_system_event_as_list_last_message_reaches_gate_only_when_not_mine(self):
        left = event(9, "PARTICIPANT_LEFT", sender="bot-service")
        self.assertEqual([chat.chat_id for chat in self.listed_with_last(left)], [1])
        joined = event(9, "PARTICIPANT_JOINED")
        self.assertEqual([chat.chat_id for chat in self.listed_with_last(joined)], [1])
        unknown = {"id": 9, "type": "SOMETHING_NEW", "text": "", "participantId": "recruiter"}
        own_left = event(9, "PARTICIPANT_LEFT", sender="me")
        with_buttons = event(9, "PARTICIPANT_LEFT")
        with_buttons["actions"] = {"text_buttons": [{"text": "Да"}]}
        for last in (unknown, own_left, with_buttons,
                     {"text": TEXT, "participantId": "me"},
                     {"text": BOT_RECRUITER_ANSWER, "participantId": "recruiter"}):
            with self.subTest(last=last):
                self.assertEqual(self.listed_with_last(last), [])

    def test_ignored_chat_with_system_last_message_is_not_listed(self):
        gate = ChatGate(Mock(), approve=lambda _: "no")
        operation = self.operation()
        chat = replace(CHAT, chat_id=1, reply_to_message="", last_is_mine=False)
        gate.system_last_chats.add(1)
        self.assertTrue(gate.is_chat_awaiting_reply(operation, chat))
        operation.ignored_chats = {1}
        self.assertFalse(gate.is_chat_awaiting_reply(operation, chat))
        # Unmarked chats keep the upstream decision.
        self.assertFalse(gate.is_chat_awaiting_reply(self.operation(), replace(chat, chat_id=2)))

    def process_listed(self, chat, items):
        sender = Mock(return_value=SimpleNamespace(status_code=200))
        gate = ChatGate(sender, approve=lambda _: "yes", clock=lambda: 0,
                        sleep=Mock(side_effect=AssertionError("must not wait")))
        drafts = []
        def process(operation, current, _blacklist):
            drafts.append(current)
            gate.message(operation, current.chat_id, gate.complete(None, "prompt"))
        def send(operation, chat_id, text, refer_url=None):
            return gate.send(None, request({"chatId": chat_id, "text": text,
                                            "idempotencyKey": str(uuid.uuid4())}))
        gate.original_complete = lambda *_a: "Да"
        gate.original_process, gate.original_message = process, send
        operation = SimpleNamespace(chat_url=self.BASE, get_chat_data=lambda chat_id: snapshot(
            *items, chat_id=chat_id))
        with redirect_stdout(StringIO()):
            gate.process(operation, chat, set())
        return gate, drafts, sender

    def test_listed_bot_left_chat_offers_question_or_skips_own_last_message(self):
        [chat] = self.listed_with_last(event(9, "PARTICIPANT_LEFT", sender="bot-service"))
        gate, drafts, sender = self.process_listed(chat, [
            message(1, "Расскажите о себе", bot=True),
            message(2, "Готовы к собеседованию?", bot=True, options=["Да", "Нет"]),
            event(3, "PARTICIPANT_LEFT", sender="bot-service")])
        self.assertEqual(drafts[0].reply_to_message, "Готовы к собеседованию?")
        self.assertEqual(drafts[0].reply_options, ["Да", "Нет"])
        sender.assert_called_once()
        gate, drafts, sender = self.process_listed(chat, [
            message(1, "Вопрос?", bot=True), message(2, TEXT, "me"),
            event(3, "PARTICIPANT_LEFT", sender="bot-service")])
        self.assertEqual(drafts, [])
        sender.assert_not_called()
        self.assertEqual(gate.counts["skipped"], 1)

    def test_follows_cursor_through_all_unread_chats(self):
        pages = {None: (range(1, 21), "100_1"), "100_1": (range(21, 41), "100_2"),
                 "100_2": (range(41, 48), None)}
        gate, chats, seen = self.collect(lambda cursor: self.page(*pages[cursor]))
        self.assertEqual([chat.chat_id for chat in chats], list(range(1, 48)))
        self.assertEqual(gate.counts["chats"], 47)
        self.assertFalse(gate.chat_pages_incomplete)
        self.assertEqual([query.get("from") for query in seen], [None, ["100_1"], ["100_2"]])
        for query in seen:
            self.assertNotIn("page", query)
            self.assertEqual(query["filterUnread"], ["true"])
            self.assertEqual(query["filterHasTextMessage"], ["false"])
            self.assertEqual(query["do_not_track_session_events"], ["true"])

    def test_cursor_ignored_stops_without_duplicates(self):
        gate, chats, seen = self.collect(lambda _cursor: self.page(range(1, 21), "100_1"))
        self.assertEqual([chat.chat_id for chat in chats], list(range(1, 21)))
        self.assertEqual(len(seen), 2)
        self.assertTrue(gate.chat_pages_incomplete)

    def test_in_page_duplicates_are_dropped(self):
        gate, chats, seen = self.collect(lambda _cursor: self.page([1, 2, 1]))
        self.assertEqual([chat.chat_id for chat in chats], [1, 2])
        self.assertFalse(gate.chat_pages_incomplete)

    def test_partial_overlap_keeps_paging(self):
        pages = {None: (range(1, 21), "100_1"), "100_1": (range(15, 35), "100_2"),
                 "100_2": (range(30, 48), None)}
        gate, chats, seen = self.collect(lambda cursor: self.page(*pages[cursor]))
        self.assertEqual([chat.chat_id for chat in chats], list(range(1, 48)))
        self.assertEqual(len(seen), 3)
        self.assertFalse(gate.chat_pages_incomplete)

    def test_malformed_structure_is_incomplete(self):
        for body in ({}, [], {"chats": None}, {"chats": []}, {"chats": [1]}, {"chats": {"items": {}}},
                     {"chats": {"found": 3}}, {"chats": {"items": "1"}}):
            with self.subTest(body=body):
                gate, _chats, seen = self.collect(lambda _cursor: body)
                self.assertEqual(len(seen), 1)
                self.assertTrue(gate.chat_pages_incomplete)
        for body in ({"chats": {"found": 0}}, {"chats": {"found": 0, "items": []}}):
            with self.subTest(body=body):
                gate, chats, _seen = self.collect(lambda _cursor: body)
                self.assertEqual((chats, gate.chat_pages_incomplete), ([], False))

    def test_repeated_cursor_stops(self):
        pages = {None: (range(1, 21), "100_1"), "100_1": (range(21, 41), "100_1")}
        gate, chats, seen = self.collect(lambda cursor: self.page(*pages[cursor]))
        self.assertEqual(len(chats), 40)
        self.assertEqual(len(seen), 2)
        self.assertTrue(gate.chat_pages_incomplete)

    def test_missing_next_from_is_single_page(self):
        gate, chats, seen = self.collect(lambda _cursor: self.page(range(1, 21)))
        self.assertEqual(len(chats), 20)
        self.assertEqual(len(seen), 1)
        self.assertFalse(gate.chat_pages_incomplete)

    def test_malformed_next_from_is_not_followed(self):
        gate, chats, seen = self.collect(lambda _cursor: self.page(range(1, 21), "1_2&page=3"))
        self.assertEqual((len(chats), len(seen)), (20, 1))
        self.assertTrue(gate.chat_pages_incomplete)

    def test_page_cap(self):
        def endless(cursor):
            number = 0 if cursor is None else int(cursor.split("_")[0])
            return self.page([number + 1], f"{number + 1}_1")
        gate, chats, seen = self.collect(endless)
        self.assertEqual(len(seen), 100)
        self.assertEqual(len(chats), 100)
        self.assertTrue(gate.chat_pages_incomplete)

    def test_page_without_cursor_is_refused(self):
        gate = ChatGate(Mock(side_effect=AssertionError("No network")))
        with self.assertRaisesRegex(SystemExit, "without a cursor"):
            gate.chats_page(self.operation(), 1)

    def test_allowed_read_checks_cursor(self):
        url = f"{self.BASE}/chatik/api/chats?filterUnread=true&from="
        self.assertTrue(allowed_read(requests.Request("GET", url + "123_456").prepare()))
        for value in ("", "abc", "1_2x", "12", "1_2&from=3_4", "%0A1_2", "1" * 21 + "_1", "-1_2"):
            with self.subTest(value=value):
                self.assertFalse(allowed_read(requests.Request("GET", url + value).prepare()))

    def test_run_patches_get_chats_and_reports_incomplete(self):
        def main(_argv):
            self.assertEqual(Operation.get_chats.__name__, "cursor_chats")
            Operation().get_chats_awaiting_reply(1)
            return 0
        for incomplete in (True, False):
            def chats(gate, *_a, **_kw):
                gate.chat_pages_incomplete, gate.listed = incomplete, True
                return []
            with self.subTest(incomplete=incomplete), tempfile.TemporaryDirectory() as directory, \
                 patch("reply_chats.validate"), patch("reply_chats.main", side_effect=main), \
                 patch.object(ChatGate, "chats", chats), \
                 patch.object(NetworkDiagnostics, "finish", autospec=True,
                              side_effect=NetworkDiagnostics.finish) as finish, \
                 redirect_stdout(StringIO()) as output:
                self.assertEqual(run([], diagnostics_directory=directory), 0)
            finish.assert_called_once_with(ANY, successful=True, complete=not incomplete)
            self.assertEqual("проверены не все чаты" in output.getvalue(), incomplete)
            self.assertEqual("Проверка чатов не завершена" in output.getvalue(), incomplete)


class NetworkDiagnosticChecks(unittest.TestCase):
    """Real Session redirects/body consumption, offline HTTPAdapter boundary only."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = NetworkDiagnostics(network_failure, self.temp.name)
        self.addCleanup(self.finish_log)
        self.session = requests.Session()
        self.session.trust_env = False
        self.addCleanup(self.session.close)
        self.sleep = Mock()
        self.gate = ChatGate(requests.sessions.Session.send, sleep=self.sleep,
                             diagnostics=self.log)

    def finish_log(self):
        with redirect_stdout(StringIO()):
            self.log.finish(successful=False, complete=False)

    def records(self):
        return [json.loads(line) for line in self.log.path.read_text().splitlines()]

    @staticmethod
    def response(prepared, status=200, location=None, body_error=None):
        response = requests.Response()
        response.request, response.url, response.status_code = prepared, prepared.url, status
        if location is not None:
            response.headers["Location"] = location

        def stream(*a, **kw):
            yield b"PRIVATE_RESPONSE_BODY"
            if body_error is not None:
                raise body_error
        response.raw = SimpleNamespace(stream=stream, close=lambda: None, release_conn=lambda: None)
        return response

    def get(self, transport, url="https://chatik.hh.ru/", **kwargs):
        with patch.object(requests.adapters.HTTPAdapter, "send", transport), \
             self.log.installed(), patch.object(requests.sessions.Session, "send",
                 lambda session, req, **kw: self.gate.send(session, req, **kw)):
            return self.session.get(url, **kwargs)

    def test_redirect_child_failure_route_correlation_and_no_double_count(self):
        seen = []
        def transport(adapter, prepared, **kwargs):
            seen.append(prepared.url)
            if len(seen) == 1:
                return self.response(prepared, 302,
                    "https://hh.ru/chatik/api/chat_data?PRIVATE_QUERY=PRIVATE_TOKEN")
            raise requests.ConnectionError("PRIVATE_EXCEPTION")
        with self.assertRaises(SystemExit):
            self.get(transport)
        events = self.records()
        hops = [e for e in events if e["event"] == "hop_started"]
        self.assertEqual([h["route"] for h in hops], ["/", "/chatik/api/chat_data"])
        self.assertEqual([h["phase"] for h in hops], ["bootstrap", "chat-read"])
        self.assertEqual(hops[1]["parent_request_id"], hops[0]["request_id"])
        self.assertEqual(hops[1]["parent_hop_id"], hops[0]["hop_id"])
        self.assertEqual(hops[1]["logical_request_id"], hops[0]["logical_request_id"])
        failures = [e for e in events if e["event"] == "hop_failed"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["hop_id"], hops[1]["hop_id"])
        self.assertEqual(failures[0]["stage"], "response_headers")
        self.assertEqual([e["stage"] for e in events if e["event"] == "send_aborted"],
                         ["observed_failure", "child_abort"])
        self.assertEqual(self.gate.counts["failures"], 1)  # The child converts to SystemExit.
        self.sleep.assert_not_called()

    def test_headers_vs_body_failure_and_stream_remains_lazy(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                baseline = len(self.records())
                error = u3.ProtocolError("PRIVATE_BODY_ERROR")
                def transport(adapter, prepared, **kwargs):
                    return self.response(prepared, body_error=error)
                if stream:
                    response = self.get(transport, "https://hh.ru/chatik/api/chat_data", stream=True)
                    current = self.records()[baseline:]
                    self.assertFalse(any(e["event"] == "body_started" for e in current))
                    with self.assertRaises(requests.exceptions.ChunkedEncodingError):
                        list(response.iter_content(chunk_size=3))
                else:
                    with self.assertRaises(SystemExit):
                        self.get(transport, "https://hh.ru/chatik/api/chat_data")
                current = self.records()[baseline:]
                self.assertEqual(len([e for e in current if e["event"] == "headers_received"]), 1)
                failures = [e for e in current if e["event"] == "hop_failed"]
                self.assertEqual([e["stage"] for e in failures], ["response_body"])

    def test_iter_content_preserves_validation_chunks_and_cached_reads(self):
        def transport(adapter, prepared, **kwargs):
            return self.response(prepared)
        response = self.get(transport, "https://hh.ru/chatik/api/chat_data", stream=True)
        with self.assertRaises(TypeError):
            response.iter_content(chunk_size="invalid")
        self.assertFalse(any(e["event"] == "body_started" for e in self.records()))
        response.encoding = "ascii"
        self.assertEqual(list(response.iter_content(4, decode_unicode=True)), ["PRIVATE_RESPONSE_BODY"])
        with self.assertRaises(requests.exceptions.StreamConsumedError):
            response.iter_content(4)
        self.assertEqual(len([e for e in self.records() if e["event"] == "body_completed"]), 1)

    def test_partial_body_close_then_content_failure_is_observed_without_changing_reads(self):
        outcomes = []
        for enabled in (False, True):
            error = requests.exceptions.ChunkedEncodingError("PRIVATE_REENTERED_BODY")
            chunk = b"PRIVATE_FIRST_CHUNK"
            first = True
            def stream(*args, **kwargs):
                nonlocal first
                if first:
                    first = False
                    yield chunk
                raise error
            def transport(adapter, prepared, **kwargs):
                response = self.response(prepared)
                response.raw.stream = stream
                return response
            gate = self.gate if enabled else ChatGate(requests.sessions.Session.send)
            with patch.object(requests.adapters.HTTPAdapter, "send", transport), \
                 self.log.installed() if enabled else nullcontext(), \
                 patch.object(requests.sessions.Session, "send", lambda session, req, **kw:
                              gate.send(session, req, **kw)):
                response = self.session.get("https://hh.ru/chatik/api/chat_data", stream=True)
            partial = response.iter_content(1)
            self.assertIs(next(partial), chunk)
            partial.close()
            for _ in range(2):
                with self.assertRaises(requests.exceptions.ChunkedEncodingError) as caught:
                    response.content
                self.assertIs(caught.exception, error)
            outcomes.append((response._content, response._content_consumed, first))
        self.assertEqual(outcomes, [(False, False, False)] * 2)
        events = self.records()
        self.assertEqual(len([e for e in events if e["event"] == "body_started"]), 1)
        self.assertFalse(any(e["event"] == "body_completed" for e in events))
        failures = [e for e in events if e["event"] == "hop_failed"]
        self.assertEqual([e["stage"] for e in failures], ["response_body"])
        self.assertNotIn("PRIVATE_", self.log.path.read_text())

    def test_partial_body_close_then_content_success_deduplicates_lifecycle(self):
        chunks = iter((b"first", b"second"))
        def transport(adapter, prepared, **kwargs):
            response = self.response(prepared)
            response.raw.stream = lambda *a, **kw: chunks
            return response
        response = self.get(transport, "https://hh.ru/chatik/api/chat_data", stream=True)
        partial = response.iter_content(1)
        self.assertEqual(next(partial), b"first")
        partial.close()
        self.assertEqual(response.content, b"second")
        self.assertEqual(list(response.iter_content(3)), [b"sec", b"ond"])
        events = self.records()
        self.assertEqual(len([e for e in events if e["event"] == "body_started"]), 1)
        self.assertEqual(len([e for e in events if e["event"] == "body_completed"]), 1)
        self.assertFalse(any(e["event"] == "hop_failed" for e in events))

    def test_all_explicit_phases_and_generated_timestamps(self):
        from hh_network_diagnostics import target
        for url, phase, route in (
            ("https://hh.ru/applicant/my_resumes", "discovery", "/applicant/my_resumes"),
            ("https://chatik.hh.ru/", "bootstrap", "/"),
            ("https://chatik.hh.ru/chatik/api/unread", "chat-list", "/chatik/api/unread"),
            ("https://chatik.hh.ru/chatik/api/chat_data", "chat-read", "/chatik/api/chat_data"),
            ("https://chatik.hh.ru/chatik/api/send", "chat-send", "/chatik/api/send"),
            ("https://api.hh.ru/token", "token-refresh", "/token"),
            ("https://spb.hh.ru/account/captcha?PRIVATE_TEXT=answer", "captcha", "/account/captcha"),
        ):
            facts = target(requests.Request("POST", url).prepare())
            self.assertEqual((facts["phase"], facts["route"]), (phase, route))
        first = self.records()[0]
        self.assertEqual(first["event"], "run_started")
        self.assertTrue({"python", "requests", "urllib3"} <= first.keys())
        uuid.UUID(first["run_id"])
        self.assertTrue(first["utc"].endswith("+00:00"))
        self.assertGreaterEqual(first["monotonic_ms"], 0)

    def test_redirect_processing_failure_not_misattributed_to_transport(self):
        def transport(adapter, prepared, **kwargs):
            # Requests decodes Location before dispatching the next guarded send.
            return self.response(prepared, 302, "https://hh.ru/\xffPRIVATE_PATH")
        with self.assertRaisesRegex(SystemExit, "^Invalid network URL or redirect; stopped$"):
            self.get(transport)
        events = self.records()
        self.assertFalse(any(e["event"] == "hop_failed" for e in events))
        self.assertEqual([e["stage"] for e in events if e["event"] == "send_aborted"],
                         ["redirect_processing"])

    def test_blocked_non_hh_redirect_is_child_abort_without_external_url(self):
        def transport(adapter, prepared, **kwargs):
            return self.response(prepared, 302, "https://PRIVATE_EXTERNAL.test/PRIVATE_PATH")
        with self.assertRaisesRegex(SystemExit, "Blocked"):
            self.get(transport)
        events = self.records()
        self.assertEqual([e["stage"] for e in events if e["event"] == "send_aborted"], ["child_abort"])
        self.assertNotIn("PRIVATE_", self.log.path.read_text())

    def test_malformed_redirects_stop_before_upstream_can_log_private_urls(self):
        for location in ("https://hh.ru:PRIVATE_TOKEN/PRIVATE_PATH",
                         "https://chatik.hh.ru:PRIVATE_TOKEN/PRIVATE_PATH",
                         "https://[PRIVATE_IPV6]/PRIVATE_PATH"):
            with self.subTest(location=location):
                calls = []
                def transport(adapter, prepared, **kwargs):
                    calls.append(prepared)
                    return self.response(prepared, 302, location)
                # Exercise the real upstream error logger without initialization,
                # profile access, cookie persistence or signal-handler changes.
                tool = HHApplicantTool.__new__(HHApplicantTool)
                tool._parser = SimpleNamespace(parse_args=lambda *a, **kw: SimpleNamespace())
                tool._assign_args = Mock()
                tool.create_config_dir = tool.setup_logging = tool.save_cookies = Mock()
                tool.config_dir_path = Path(self.temp.name)
                tool.operation_run = lambda *_: self.get(transport)
                stdout, stderr = StringIO(), StringIO()
                with patch("hh_applicant_tool.tool.is_docker", return_value=False), \
                     patch("hh_applicant_tool.tool.signal.signal"), redirect_stdout(stdout), \
                     redirect_stderr(stderr), self.assertLogs("hh_applicant_tool", level="DEBUG") as logs:
                    try:
                        tool.run([])
                    except SystemExit as error:
                        rendered = "".join(traceback.format_exception(error))
                        self.assertEqual(str(error), "Invalid network URL or redirect; stopped")
                    else:
                        self.fail("Malformed redirect reached upstream's raw exception logger")
                self.assertEqual(len(calls), 1)
                self.assertTrue(all(record.levelname != "ERROR" for record in logs.records))
                for text in (rendered, stdout.getvalue(), stderr.getvalue(),
                             "\n".join(logs.output), self.log.path.read_text()):
                    self.assertNotIn("PRIVATE_", text)
        self.sleep.assert_not_called()

    def test_malformed_request_guard_and_endpoint_rendering_are_safe(self):
        for url in ("https://hh.ru:PRIVATE_TOKEN/", "https://[PRIVATE_IPV6]/PRIVATE_PATH",
                    "http://127.0.0.1:PRIVATE_TOKEN/health"):
            prepared = requests.Request("GET", "https://hh.ru/").prepare()
            prepared.url = url  # Deliberately bypass Request.prepare_url validation.
            self.assertEqual(request_target(prepared), "other")
            for operation in (lambda: allowed_read(prepared),
                              lambda: self.gate.send(self.session, prepared)):
                with self.assertRaises(SystemExit) as caught:
                    operation()
                self.assertEqual(str(caught.exception), "Invalid network URL or redirect; stopped")
                self.assertNotIn("PRIVATE_", "".join(traceback.format_exception(caught.exception)))
        self.assertNotIn("PRIVATE_", self.log.path.read_text())

    def test_malformed_redirect_with_no_proxy_is_sanitized_before_upstream_logging(self):
        self.session.trust_env = True
        calls = []
        def transport(adapter, prepared, **kwargs):
            calls.append(prepared)
            return self.response(prepared, 302, "https://hh.ru:PRIVATE_TOKEN/PRIVATE_PATH")
        tool = HHApplicantTool.__new__(HHApplicantTool)
        tool._parser = SimpleNamespace(parse_args=lambda *a, **kw: SimpleNamespace())
        tool._assign_args = Mock()
        tool.create_config_dir = tool.setup_logging = tool.save_cookies = Mock()
        tool.config_dir_path = Path(self.temp.name)
        tool.operation_run = lambda *_: self.get(transport)
        stdout, stderr = StringIO(), StringIO()
        with patch.dict(os.environ, {"NO_PROXY": "PRIVATE_NO_PROXY.invalid"}, clear=True), \
             patch("requests.sessions.get_netrc_auth", return_value=None), \
             patch("requests.utils.getproxies", return_value={}), \
             patch("requests.utils.proxy_bypass", return_value=False), \
             patch("hh_applicant_tool.tool.is_docker", return_value=False), \
             patch("hh_applicant_tool.tool.signal.signal"), redirect_stdout(stdout), \
             redirect_stderr(stderr), self.assertLogs("hh_applicant_tool", level="DEBUG") as logs:
            try:
                tool.run([])
            except SystemExit as error:
                self.assertEqual(str(error), "Invalid network URL or redirect; stopped")
                rendered = "".join(traceback.format_exception(error))
                trace, codes = error.__context__.__traceback__, []
                while trace is not None:
                    codes.append(trace.tb_frame.f_code)
                    trace = trace.tb_next
                self.assertIn(requests.utils.should_bypass_proxies.__code__, codes)
            else:
                self.fail("Proxy URL parsing reached upstream's raw exception logger")
        self.assertEqual(len(calls), 1)
        self.sleep.assert_not_called()
        self.assertTrue(all(record.levelname != "ERROR" for record in logs.records))
        for text in (rendered, stdout.getvalue(), stderr.getvalue(),
                     "\n".join(logs.output), self.log.path.read_text()):
            self.assertNotIn("PRIVATE_", text)

    def test_proxy_parser_in_unrelated_child_hook_is_not_reclassified(self):
        errors, calls = [], []
        def hook(response, **kwargs):
            if response.status_code == 200:
                try:
                    requests.utils.should_bypass_proxies(
                        "https://hh.ru:PRIVATE_HOOK_PORT/", no_proxy="PRIVATE_NO_PROXY.invalid")
                except ValueError as error:
                    errors.append(error)
                    raise
        def transport(adapter, prepared, **kwargs):
            calls.append(prepared)
            return self.response(prepared, 302 if len(calls) == 1 else 200,
                                 "https://hh.ru/chatik/api/chats" if len(calls) == 1 else None)
        with self.assertRaises(ValueError) as caught:
            self.get(transport, hooks={"response": hook})
        self.assertIs(caught.exception, errors[0])
        self.assertEqual(len(calls), 2)
        self.sleep.assert_not_called()
        self.assertNotIn("PRIVATE_", self.log.path.read_text())

    def test_unrelated_valueerror_from_response_hook_is_not_reclassified(self):
        class PrivateError(ValueError):
            def __str__(self):
                raise AssertionError("exception str forbidden")
            def __repr__(self):
                raise AssertionError("exception repr forbidden")
        error = PrivateError("PRIVATE_UNRELATED_HOOK")
        def hook(*a, **kw):
            raise error
        def transport(adapter, prepared, **kwargs):
            return self.response(prepared)
        with self.assertRaises(ValueError) as caught:
            self.get(transport, hooks={"response": hook})
        self.assertIs(caught.exception, error)
        self.sleep.assert_not_called()
        self.assertNotIn("PRIVATE_", self.log.path.read_text())

    def test_urlparse_in_unrelated_child_hook_is_not_reclassified(self):
        from urllib.parse import urlsplit
        errors, calls = [], []
        def hook(response, **kwargs):
            if response.status_code == 200:
                try:
                    urlsplit("https://hh.ru:PRIVATE_HOOK_PORT/").port
                except ValueError as error:
                    errors.append(error)
                    raise
        def transport(adapter, prepared, **kwargs):
            calls.append(prepared)
            return self.response(prepared, 302 if len(calls) == 1 else 200,
                                 "https://hh.ru/chatik/api/chats" if len(calls) == 1 else None)
        with self.assertRaises(ValueError) as caught:
            self.get(transport, hooks={"response": hook})
        self.assertIs(caught.exception, errors[0])
        self.assertEqual(len(calls), 2)
        self.sleep.assert_not_called()
        self.assertNotIn("PRIVATE_", self.log.path.read_text())

    def test_multiple_redirects_are_identical_with_diagnostics_enabled_or_disabled(self):
        snapshots = []
        real_send = requests.sessions.Session.send
        urls = ["https://api.hh.ru/token", "https://api.hh.ru/token?PRIVATE_QUERY=second",
                "https://hh.ru/applicant/my_resumes?PRIVATE_QUERY=third",
                "https://chatik.hh.ru/chatik/api/chats?PRIVATE_QUERY=fourth"]
        for enabled in (False, True):
            calls, prepared_requests, responses, reads = [], [], [], []
            def transport(adapter, prepared, **kwargs):
                self.assertIs(adapter, session.get_adapter(prepared.url))
                index = len(calls)
                prepared_requests.append(prepared)
                calls.append({"method": prepared.method, "url": prepared.url,
                              "body": prepared.body, "headers": dict(prepared.headers),
                              "options": dict(kwargs)})
                response = self.response(prepared, (307, 302, 308, 200)[index],
                                         urls[index + 1] if index < 3 else None)
                headers = Message()
                if index == 0:
                    headers.add_header("Set-Cookie", "api_cookie=PRIVATE_API_COOKIE; Path=/; Secure")
                    headers.add_header("Set-Cookie", "wide_cookie=PRIVATE_WIDE_COOKIE; Domain=.hh.ru; Path=/; Secure")
                elif index == 2:
                    headers.add_header("Set-Cookie", "scoped_cookie=PRIVATE_SCOPED_COOKIE; Path=/applicant; Secure")
                response.raw._original_response = SimpleNamespace(msg=headers)
                def stream(*a, **kw):
                    reads.append(index)
                    yield b"PRIVATE_RESPONSE_BODY"
                response.raw.stream = stream
                responses.append(response)
                return response
            gate = ChatGate(real_send, sleep=self.sleep, diagnostics=self.log if enabled else None)
            with requests.Session() as session:
                session.trust_env = False
                session.headers["Authorization"] = "Bearer PRIVATE_AUTH"
                session.cookies.set("seed", "PRIVATE_SEED", domain="api.hh.ru", path="/")
                session.proxies = {"https": "http://PRIVATE_USER:PRIVATE_PASS@PRIVATE_PROXY:8123"}
                session.verify = "PRIVATE_CA"
                session.cert = ("PRIVATE_CERT", "PRIVATE_KEY")
                with patch.object(requests.adapters.HTTPAdapter, "send", transport), \
                     self.log.installed() if enabled else nullcontext(), \
                     patch.object(requests.sessions.Session, "send", lambda actual, req, **kw:
                                  gate.send(actual, req, **kw)):
                    result = session.post(urls[0], data={"grant_type": "refresh_token",
                        "refresh_token": "PRIVATE_REFRESH"}, timeout=(2, 9))
                self.assertIs(result, responses[-1])
                self.assertEqual(result.history, responses[:-1])
                for response, prepared in zip((*result.history, result), prepared_requests):
                    self.assertIs(response.request, prepared)
                cookies = sorted((c.name, c.value, c.domain, c.path, c.secure) for c in session.cookies)
                self.assertEqual([cookie[0] for cookie in cookies],
                                 ["api_cookie", "scoped_cookie", "seed", "wide_cookie"])
                snapshots.append((calls, cookies, reads, result.content,
                                  [(r.status_code, r.url, r.request.method, r.content) for r in result.history]))
            self.assertEqual(len(calls), 4)
            self.assertEqual([c["url"] for c in calls], urls)
            for call in calls:
                self.assertEqual(call["options"], {
                    "stream": False, "timeout": (2, 9), "verify": "PRIVATE_CA",
                    "cert": ("PRIVATE_CERT", "PRIVATE_KEY"),
                    "proxies": {"https": "http://PRIVATE_USER:PRIVATE_PASS@PRIVATE_PROXY:8123"},
                })
            self.assertEqual([c["method"] for c in calls], ["POST", "POST", "GET", "GET"])
            self.assertEqual(calls[0]["body"], calls[1]["body"])
            self.assertIsNone(calls[2]["body"])
            self.assertIsNone(calls[3]["body"])
            self.assertIn("Content-Type", calls[1]["headers"])
            self.assertNotIn("Content-Type", calls[2]["headers"])
            self.assertEqual([c["headers"].get("Authorization") for c in calls],
                             ["Bearer PRIVATE_AUTH", "Bearer PRIVATE_AUTH", None, None])
            self.assertIn("api_cookie=PRIVATE_API_COOKIE", calls[1]["headers"]["Cookie"])
            self.assertNotIn("api_cookie", calls[2]["headers"]["Cookie"])
            self.assertNotIn("seed=", calls[2]["headers"]["Cookie"])
            self.assertEqual(calls[3]["headers"]["Cookie"], "wide_cookie=PRIVATE_WIDE_COOKIE")
            self.assertEqual(reads, [0, 1, 2, 3])
            self.assertEqual([r.status_code for r in result.history], [307, 302, 308])
        self.assertEqual(snapshots[0], snapshots[1])
        self.sleep.assert_not_called()
        self.assertNotIn("PRIVATE_", self.log.path.read_text())

    def test_effective_options_preserved_and_strict_proxy_privacy(self):
        timeout = Timeout(connect=1.5, read=4, total=8)
        proxy = {"https": "http://PRIVATE_USER:PRIVATE_PASSWORD@PRIVATE_PROXY:8123/PRIVATE_PROXY_PATH"}
        seen = []
        def transport(adapter, *args, **kwargs):
            seen.append((args, kwargs))
            return self.response(args[0])
        response = self.get(transport, "https://hh.ru/chatik/api/chat_data?PRIVATE_QUERY=secret",
                            proxies=proxy, verify="PRIVATE_CA", cert=("PRIVATE_CERT", "PRIVATE_KEY"),
                            timeout=timeout, stream=True)
        self.assertFalse(response._content_consumed)
        self.assertEqual(len(seen), 1)
        options = seen[0][1]
        self.assertIs(options["timeout"], timeout)
        self.assertEqual(options["proxies"], proxy)
        self.assertEqual(options["verify"], "PRIVATE_CA")
        self.assertEqual(options["cert"], ("PRIVATE_CERT", "PRIVATE_KEY"))
        self.assertTrue(options["stream"])
        hop = next(e for e in self.records() if e["event"] == "hop_started")
        self.assertEqual(hop["proxy"], {"mode": "proxied", "scheme": "http", "port": 8123, "loopback": False})
        self.assertEqual(hop["timeout"], {"kind": "urllib3", "connect": 1.5, "read": 4, "total": 8})
        self.assertEqual((hop["tls"], hop["client_cert"], hop["stream"]), ("custom_ca", True, True))
        self.assertNotIn("PRIVATE_", self.log.path.read_text())

    def test_all_canaries_absent_and_exception_never_formatted(self):
        class SecretError(requests.ConnectionError):
            def __str__(self):
                raise AssertionError("exception str forbidden")
            def __repr__(self):
                raise AssertionError("exception repr forbidden")
        calls = []
        def transport(adapter, prepared, **kwargs):
            calls.append(prepared)
            if len(calls) == 1:
                return self.response(prepared, 302,
                    "https://PRIVATE_SUBDOMAIN.hh.ru/PRIVATE_PATH?PRIVATE_QUERY=PRIVATE_TOKEN")
            raise SecretError("PRIVATE_EXCEPTION", "https://PRIVATE_RAW_URL")
        with self.assertRaises(SystemExit):
            self.get(transport, "https://chatik.hh.ru/?PRIVATE_QUERY=PRIVATE_TOKEN", headers={
                "Authorization": "Bearer PRIVATE_AUTH", "Cookie": "PRIVATE_COOKIE",
                "X-Private": "PRIVATE_HEADER"})
        data = self.log.path.read_text()
        self.assertNotIn("PRIVATE_", data)
        second = [e for e in self.records() if e["event"] == "hop_started"][1]
        self.assertEqual((second["origin"], second["route"]), ("other", "other"))
        self.assertNotIn("SecretError", data)

    def test_positional_forwarding_unmodified_and_local_gateway_excluded(self):
        prepared = requests.Request("GET", "https://hh.ru/resumes/PRIVATE_RESUME").prepare()
        proxies = {"https": "socks5://PRIVATE_CREDENTIALS@127.0.0.1:9050/PRIVATE_PATH"}
        original = Mock(return_value=self.response(prepared))
        adapter = self.session.get_adapter(prepared.url)
        with self.log.guard(prepared), self.log.sending(prepared):
            result = self.log.adapter_send(original, adapter, prepared, True, (2, 3), False, None, proxies)
        original.assert_called_once_with(adapter, prepared, True, (2, 3), False, None, proxies)
        self.assertIs(result, original.return_value)
        hop = next(e for e in self.records() if e["event"] == "hop_started")
        self.assertEqual(hop["route"], "/resumes/{id}")
        self.assertEqual(hop["tls"], "disabled")
        self.assertTrue(hop["proxy"]["loopback"])
        before = self.log.path.read_bytes()
        def transport(adapter, prepared, **kwargs):
            return self.response(prepared)
        self.get(transport, "http://127.0.0.1:8000/health")
        self.assertEqual(self.log.path.read_bytes(), before)

    def test_nonfinite_timeout_and_proxy_metadata_never_dump_objects(self):
        prepared = requests.Request("GET", "https://hh.ru/").prepare()
        for timeout in (float("inf"), float("nan"), (None, float("inf")), Timeout.DEFAULT_TIMEOUT):
            facts = transport_options(prepared, {"timeout": timeout})
            self.assertIsNone(facts["timeout"]["connect"])
            json.dumps(facts, allow_nan=False)
        for proxy, loopback in (("http://localhost:8000", True), ("http://[::1]:8080", True),
                                ("http://PRIVATE_PROXY:bad", False)):
            facts = transport_options(prepared, {"proxies": {"https": proxy}})
            self.assertEqual(facts["proxy"]["loopback"], loopback)
            self.assertNotIn("PRIVATE_", json.dumps(facts))

    def test_real_adapter_transport_hook_and_response_consumption(self):
        from urllib3.response import HTTPResponse
        raw = HTTPResponse(body=BytesIO(b"PRIVATE_RESPONSE_BODY"), status=200, preload_content=False)
        pool = SimpleNamespace(urlopen=Mock(return_value=raw))
        adapter = self.session.get_adapter("https://hh.ru/")
        with patch.object(adapter, "get_connection_with_tls_context", return_value=pool), \
             patch.object(adapter, "cert_verify") as verify, self.log.installed(), \
             patch.object(requests.sessions.Session, "send", lambda session, req, **kw:
                          self.gate.send(session, req, **kw)):
            response = self.session.get("https://hh.ru/chatik/api/chats?PRIVATE_QUERY=value")
        self.assertEqual(response.content, b"PRIVATE_RESPONSE_BODY")
        self.assertEqual(pool.urlopen.call_count, 1)
        self.assertFalse(pool.urlopen.call_args.kwargs["preload_content"])
        self.assertFalse(pool.urlopen.call_args.kwargs["redirect"])
        self.assertIs(pool.urlopen.call_args.kwargs["retries"], adapter.max_retries)
        self.assertEqual(adapter.max_retries.total, 0)
        self.assertEqual(verify.call_args.args[2:], (True, None))
        events = self.records()
        self.assertEqual([e["event"] for e in events if e["event"] in
                         {"hop_started", "headers_received", "body_started", "body_completed"}],
                         ["hop_started", "headers_received", "body_started", "body_completed"])
        self.assertNotIn("PRIVATE_", self.log.path.read_text())

    def test_diagnostic_post_still_one_shot_even_with_captcha_or_transport_error(self):
        for result in ("captcha", "timeout"):
            with self.subTest(result=result):
                self.gate.current = CHAT
                self.gate.operation = SimpleNamespace(chat_url="https://chatik.hh.ru")
                self.gate.pending = (CHAT.chat_id, TEXT)
                sent = []
                def transport(adapter, prepared, **kwargs):
                    sent.append((prepared, kwargs))
                    if result == "timeout":
                        raise requests.ReadTimeout("PRIVATE_EXCEPTION")
                    response = self.response(prepared, 403)
                    response._content = json.dumps({"errors": [{"value": "captcha_required",
                        "captcha_url": "https://hh.ru/account/captcha?PRIVATE_TOKEN=value"}]}).encode()
                    return response
                with patch.object(requests.adapters.HTTPAdapter, "send", transport), \
                     self.log.installed(), patch.object(self.gate.captcha, "solve") as solve, \
                     patch.object(requests.sessions.Session, "send", lambda session, req, **kw:
                                  self.gate.send(session, req, **kw)), self.assertRaises(SystemExit):
                    self.session.send(request())
                solve.assert_not_called()
                self.assertEqual(len(sent), 1)
                self.assertEqual(json.loads(sent[0][0].body)["text"], TEXT)
                self.assertEqual(sent[0][1]["timeout"], (10, 60))
                self.assertIsNone(self.gate.pending)
        self.sleep.assert_not_called()
        self.assertNotIn(TEXT, self.log.path.read_text())
        self.assertNotIn("PRIVATE_", self.log.path.read_text())

    def test_diagnostic_captcha_replay_keeps_existing_shared_startup_budget(self):
        calls = []
        def transport(adapter, prepared, **kwargs):
            calls.append(prepared)
            if len(calls) == 1:
                raise requests.ReadTimeout("PRIVATE_EXCEPTION")
            response = self.response(prepared, 403)
            response._content = json.dumps({"errors": [{"value": "captcha_required",
                "captcha_url": "https://hh.ru/account/captcha?PRIVATE_TOKEN=value"}]}).encode()
            return response
        with patch.object(self.gate.captcha, "solve") as solve, self.assertRaisesRegex(
                SystemExit, "budget exhausted"):
            self.get(transport)
        self.assertEqual(len(calls), 2)
        solve.assert_called_once()
        self.assertEqual(len([e for e in self.records() if e["event"] == "startup_budget_exhausted"]), 1)

    def test_retry_decisions_and_budget_semantics_unchanged(self):
        transport = Mock(side_effect=requests.ReadTimeout("PRIVATE_EXCEPTION"))
        with self.assertRaises(SystemExit):
            self.get(transport)
        self.assertEqual(transport.call_count, 2)
        self.sleep.assert_called_once_with(0.25)
        events = self.records()
        self.assertEqual(len([e for e in events if e["event"] == "hop_failed"]), 2)
        self.assertEqual(len([e for e in events if e["event"] == "startup_retry_scheduled"]), 1)
        self.assertEqual(len([e for e in events if e["event"] == "startup_budget_exhausted"]), 1)

    def test_adapter_gap_explicit_not_fabricated_headers(self):
        class CustomAdapter(requests.adapters.BaseAdapter):
            def send(adapter, prepared, **kwargs):
                return self.response(prepared)
            def close(adapter):
                pass
        self.session.mount("https://", CustomAdapter())
        self.get(Mock(side_effect=AssertionError("must not call HTTPAdapter")))
        events = self.records()
        self.assertEqual(len([e for e in events if e["event"] == "adapter_gap"]), 1)
        self.assertFalse(any(e["event"] == "headers_received" for e in events))

    def test_cap_reserves_final_marker_and_disk_failures_do_not_mask(self):
        log = NetworkDiagnostics(network_failure, self.temp.name, max_bytes=4096)
        for _ in range(100):
            log.event("safe_test_event")
        with redirect_stdout(StringIO()):
            log.finish(successful=False, complete=False)
        events = [json.loads(line) for line in log.path.read_text().splitlines()]
        self.assertEqual(events[-1]["event"], "run_finished")
        self.assertEqual(len([e for e in events if e["event"] == "log_capped"]), 1)
        self.assertLessEqual(log.path.stat().st_size, 4096)
        stderr = StringIO()
        with patch.object(self.log.file, "write", side_effect=OSError("PRIVATE_DISK_ERROR")), \
             redirect_stderr(stderr):
            self.log.event("safe_test_event")
            self.log.event("safe_test_event")
            transport = Mock(side_effect=requests.ConnectionError("PRIVATE_EXCEPTION"))
            with self.assertRaises(SystemExit):
                self.get(transport)
        self.assertEqual(stderr.getvalue().count("HH network diagnostics unavailable"), 1)
        self.assertNotIn("PRIVATE_", stderr.getvalue())
        transport.assert_called_once()
        def success(adapter, prepared, **kwargs):
            return self.response(prepared)
        self.assertEqual(self.get(success).status_code, 200)

    def test_flush_and_cleanup_failures_retain_own_log_and_warn_once(self):
        stderr = StringIO()
        with patch.object(self.log.file, "flush", side_effect=OSError("PRIVATE_FLUSH_ERROR")), \
             redirect_stderr(stderr):
            self.log.event("safe_test_event")
            self.log.event("safe_test_event")
        with redirect_stdout(StringIO()):
            self.log.finish(successful=True, complete=True)
        self.assertTrue(self.log.path.exists())
        self.assertEqual(stderr.getvalue().count("HH network diagnostics unavailable"), 1)
        second = NetworkDiagnostics(network_failure, self.temp.name)
        with patch.object(Path, "unlink", side_effect=PermissionError("PRIVATE_UNLINK_ERROR")), \
             redirect_stderr(stderr), redirect_stdout(StringIO()) as output:
            second.finish(successful=True, complete=True)
        self.assertTrue(second.path.exists())
        self.assertIn(str(second.path), output.getvalue())
        self.assertEqual(stderr.getvalue().count("HH network diagnostics unavailable"), 2)
        self.assertNotIn("PRIVATE_", stderr.getvalue())

    def test_exclusive_creation_and_io_failure_warn_without_masking(self):
        original = self.log.path.read_bytes()
        with patch("hh_network_diagnostics.uuid.uuid4", return_value=SimpleNamespace(hex=self.log.run_id)), \
             redirect_stderr(StringIO()) as stderr:
            collision = NetworkDiagnostics(network_failure, self.temp.name)
        self.assertIsNone(collision.path)
        collision.finish(successful=True, complete=True)
        self.assertEqual(self.log.path.read_bytes(), original)
        self.assertEqual(stderr.getvalue().count("HH network diagnostics unavailable"), 1)
        with patch.object(Path, "mkdir", side_effect=PermissionError("PRIVATE_DISK_ERROR")), \
             redirect_stderr(StringIO()) as stderr:
            denied = NetworkDiagnostics(network_failure, self.temp.name)
            denied.finish(successful=False, complete=False)
        self.assertEqual(stderr.getvalue().count("HH network diagnostics unavailable"), 1)
        self.assertNotIn("PRIVATE_", stderr.getvalue())

    def test_run_lifecycle_success_failure_interrupt_nonzero_incomplete(self):
        previous = Path(self.temp.name) / "hh-chats-previous.jsonl"
        previous.write_text("previous run must remain")
        for outcome in ("success", "failure", "interrupt", "nonzero", "incomplete"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                def main(_argv):
                    if outcome != "incomplete":
                        Operation().get_chats_awaiting_reply(1)
                    if outcome == "failure":
                        raise RuntimeError("PRIVATE_EXCEPTION")
                    if outcome == "interrupt":
                        raise KeyboardInterrupt()
                    return 7 if outcome == "nonzero" else 0
                output = StringIO()
                with patch("reply_chats.validate"), patch("reply_chats.main", side_effect=main), \
                     patch.object(Operation, "get_chats_awaiting_reply", return_value=[]), \
                     redirect_stdout(output):
                    if outcome in {"failure", "interrupt"}:
                        with self.assertRaises(RuntimeError if outcome == "failure" else KeyboardInterrupt):
                            run([], diagnostics_directory=directory)
                    else:
                        self.assertEqual(run([], diagnostics_directory=directory), 7 if outcome == "nonzero" else 0)
                logs = list(Path(directory).glob("*.jsonl"))
                self.assertEqual(len(logs), 0 if outcome == "success" else 1)
                if logs:
                    final = json.loads(logs[0].read_text().splitlines()[-1])
                    self.assertEqual(final["event"], "run_finished")
                    self.assertFalse(final["complete"])
                    self.assertIn(str(logs[0]), output.getvalue())
                    self.assertNotIn("PRIVATE_", logs[0].read_text())
        self.log.finish(successful=True, complete=True)
        self.assertFalse(self.log.path.exists())
        self.assertEqual(previous.read_text(), "previous run must remain")


if __name__ == "__main__":
    unittest.main(verbosity=2)
