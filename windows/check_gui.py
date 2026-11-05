"""Offline GUI boundary tests. Only disposable profiles; no account or network IO."""

from contextlib import contextmanager, nullcontext, redirect_stdout
from io import StringIO
import builtins
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import requests

import gui
import gui_controller
import gui_profile
import gui_runtime
import gui_worker
import gui_owned_process
from gui_recovery import RecoveryMarker
from gui_protocol import (ACTIONS, Cancelled, ProtocolError, ReviewAdapter, ReviewState,
                          WorkerChannel, decode, encode, frame, validate_done)
from send import ApprovalGate
from reply_chats import ChatGate


def setUpModule():
    global NETWORK_BLOCK
    NETWORK_BLOCK = patch.object(requests.sessions.Session, "send", side_effect=AssertionError("No network in GUI checks"))
    NETWORK_BLOCK.start()


def tearDownModule():
    NETWORK_BLOCK.stop()


def source_worker_command():
    """CI/build environments provide dependencies in this interpreter, not repo venvs."""
    return [sys.executable, "-B", str(Path(__file__).with_name("gui.py").resolve()), "--worker"]


def run_session(session):
    # Lifecycle checks fake process creation, so source runtime discovery is a
    # separate boundary (covered explicitly below), not a fixture prerequisite.
    with patch("gui_controller.worker_command", return_value=source_worker_command()):
        session._run()


class Reader:
    def __init__(self):
        self.lines = queue.Queue()

    def readline(self, _limit=None):
        return self.lines.get(timeout=5)

    def put(self, data):
        self.lines.put(encode(data) if isinstance(data, dict) else data)


class Writer:
    def __init__(self, callback=lambda _data: None):
        self.callback, self.items = callback, []

    def write(self, text):
        data = decode(text)
        self.items.append(data)
        self.callback(data)

    def flush(self):
        pass


class FakeChannel:
    def __init__(self, decisions=()):
        self.decisions, self.reviews, self.events = iter(decisions), [], []

    def ask(self, payload):
        self.reviews.append(payload)
        return next(self.decisions)

    def event(self, name, value):
        self.events.append((name, value))

    def check(self):
        pass

    def sleep(self, _seconds):
        pass


class ProtocolChecks(unittest.TestCase):
    def test_completion_counts_and_unknown_outcomes_must_be_consistent(self):
        valid = {"success": False, "cancelled": False, "unknown": True,
                 "counts": {"attempts": 1, "http_success": 0, "captcha_rejections": 0}}
        self.assertEqual(validate_done(valid), valid)
        for invalid in ({**valid, "counts": []}, {**valid, "unknown": False},
                        {**valid, "counts": {"attempts": True, "http_success": 0, "captcha_rejections": 0}},
                        {**valid, "counts": {"attempts": 0, "http_success": 1, "captcha_rejections": 0}}):
            with self.assertRaises(ProtocolError):
                validate_done(invalid)

    def test_strict_envelope_and_malformed_data(self):
        valid = frame(uuid4().hex, "response", {"action": "yes", "text": ""})
        self.assertEqual(decode(encode(valid)), valid)
        for line in ("", "yes\n", "{}", "[]", "null", "{" * 2000,
                     encode(valid).replace('"v":1', '"v":true'),
                     encode(valid).replace('"v":1', '"v":2'),
                     encode(valid).replace('"v":1', '"v":1,"v":1'),
                     encode(valid).replace('"text":""', '"text":NaN'), "x" * (1024 * 1024 + 1)):
            with self.subTest(line=line[:80]), self.assertRaises(ProtocolError):
                decode(line)

    def test_stale_duplicate_wrong_run_and_eof_cancel(self):
        for bad in ("stale", "wrong_run", "duplicate", "eof", "output"):
            with self.subTest(bad=bad):
                reader, writer, run = Reader(), Writer(), uuid4().hex
                channel = WorkerChannel(reader, writer, run)
                pending = uuid4().hex
                channel.pending = pending
                good = frame(run, "response", {"action": "yes", "text": ""}, pending)
                if bad == "duplicate":
                    reader.put(good)
                    reader.put(good)
                elif bad == "eof":
                    reader.put("")
                elif bad == "output":
                    reader.put("Отправить? yes\n")
                else:
                    reader.put(frame(uuid4().hex if bad == "wrong_run" else run, "response", {},
                                     uuid4().hex if bad == "stale" else pending))
                channel.listen()
                self.assertTrue(channel.cancelled.wait(2))
                with self.assertRaises(Cancelled):
                    channel.check()

    def test_request_ids_one_shot_and_cancellation(self):
        run = uuid4().hex
        state = ReviewState(run)
        review = frame(run, "review", {"text": "Exact\nletter"})
        state.accept(review)
        response = state.respond("manual", "Updated\ntext")
        self.assertEqual(response["id"], review["id"])
        self.assertEqual(response["payload"]["text"], "Updated\ntext")
        with self.assertRaises(ProtocolError):
            state.respond("yes")
        with self.assertRaises(ProtocolError):
            state.accept(review)
        fresh = frame(run, "review", {"text": "Updated\ntext"})
        state.accept(fresh)
        state.cancel()
        with self.assertRaises(ProtocolError):
            state.respond("yes")
        with self.assertRaises(ProtocolError):
            state.accept(frame(run, "review"))

    def test_channel_approval_not_terminal_text(self):
        run, reader = uuid4().hex, Reader()
        writer = Writer(lambda data: reader.put(frame(run, "response", {"action": "yes", "text": ""}, data["id"])))
        channel = WorkerChannel(reader, writer, run)
        channel.listen()
        result = channel.ask({"kind": "application", "text": "literal yes is data"})
        self.assertEqual(result["action"], "yes")
        self.assertIsNone(channel.pending)
        reader.put("")

    def test_cancellation_interrupts_review_and_pause(self):
        reader, writer = Reader(), Writer()
        channel = WorkerChannel(reader, writer, uuid4().hex)
        channel.cancelled.set()
        with self.assertRaises(Cancelled):
            channel.ask({})
        with self.assertRaises(Cancelled):
            channel.sleep(86400)
        self.assertEqual(writer.items, [])


class ProfileChecks(unittest.TestCase):
    def test_round_robin_preserves_legacy_context_seam_and_forces_gui_facts_for_every_resume(self):
        from send_all import apply_round_robin
        contexts = []
        @contextmanager
        def legacy_context(resume_id, configured_id):
            contexts.append((resume_id, configured_id))
            yield
        tool = Mock()
        tool.get_resumes.return_value = [{"id": rid, "title": rid, "status": {"id": "published"}} for rid in ("r1", "r2")]
        tool.get_openai_settings.return_value = {}
        operation = SimpleNamespace(tool=tool)
        with patch("send_all.selected_resume_context", legacy_context), redirect_stdout(StringIO()):
            apply_round_robin(operation, "r1", {"r1": "Python", "r2": "Linux"}, lambda *_a, **_kw: iter(()))
        self.assertEqual(contexts, [("r1", "r1"), ("r2", "r1")])
        with patch("send_all.selected_resume_context", return_value=nullcontext()) as context, redirect_stdout(StringIO()):
            apply_round_robin(operation, "r1", {"r1": "Python", "r2": "Linux"}, lambda *_a, **_kw: iter(()), force_fetch=True)
        self.assertEqual([call.kwargs for call in context.call_args_list], [{"force_fetch": True}, {"force_fetch": True}])

    def test_preview_skipped_salary_and_senior_reasons_are_structured_without_generation(self):
        import preview
        import preview_hh
        from hh_applicant_tool.operations.apply_vacancies import Operation
        from hh_applicant_tool.ai.openai import OpenAIClient
        vacancies = [{"id": "1", "name": "Python", "salary": {"from": 400000, "currency": "RUR"}},
                     {"id": "2", "name": "Senior Python", "salary": {"from": 100000, "currency": "RUR"}}]
        observer = Mock()
        def main(_argv):
            self.assertEqual(list(Operation._get_vacancies(SimpleNamespace(api_client=None))), [])
            return 0
        with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)):
            gui_profile.initialize()
            gui_profile.set_model("offline")
            with patch.dict(os.environ, gui_worker.environment()), \
                 patch.object(Operation, "_get_vacancies", lambda *_a, **_kw: iter(vacancies)), \
                 patch.object(OpenAIClient, "complete") as complete, patch("preview_hh.main", main), \
                 patch("preview_hh.selected_resume_context", return_value=nullcontext()), redirect_stdout(StringIO()):
                preview_hh.run(preview.arguments(str(gui_profile.profile_dir()), "r", "Python"),
                               profile=gui_profile.profile_dir(), observer=observer)
            complete.assert_not_called()
        self.assertEqual([call.args[0] for call in observer.call_args_list], ["preview_skipped", "preview_skipped"])
        self.assertEqual([call.args[1]["vacancy"]["id"] for call in observer.call_args_list], ["1", "2"])
        self.assertTrue(all(call.args[1]["reason"] for call in observer.call_args_list))

    def test_gui_preview_reports_upstream_skip_flags_without_changing_terminal_path(self):
        import preview
        import preview_hh
        from hh_applicant_tool.operations.apply_vacancies import Operation
        for field, value in (("relations", ["applied"]),
                             ("archived", True), ("response_url", "https://example.invalid")):
            for structured in (False, True):
                with self.subTest(field=field, structured=structured), tempfile.TemporaryDirectory() as temp, \
                     patch("pathlib.Path.home", return_value=Path(temp)):
                    gui_profile.initialize()
                    vacancy = {"id": "1", "name": "Python", field: value}
                    observer = Mock() if structured else None
                    def main(_argv):
                        self.assertEqual(list(Operation._get_vacancies(SimpleNamespace(api_client=None))),
                                         [] if structured else [vacancy])
                        return 0
                    with patch.dict(os.environ, gui_worker.environment()), \
                         patch.object(Operation, "_get_vacancies", lambda *_a, **_kw: iter([vacancy])), \
                         patch("preview_hh.main", main), patch("preview_hh.selected_resume_context", return_value=nullcontext()), \
                         redirect_stdout(StringIO()):
                        preview_hh.run(preview.arguments(str(gui_profile.profile_dir()), "r", "Python"),
                                       profile=gui_profile.profile_dir(), observer=observer)
                    if structured:
                        self.assertEqual(observer.call_args.args[0], "preview_skipped")
                        self.assertTrue(observer.call_args.args[1]["reason"])

    def test_generic_profile_accepted_by_actual_send_validation_without_cli_redirection(self):
        import send
        with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)):
            gui_profile.initialize()
            gui_profile.set_model("offline-model")
            profile = gui_profile.profile_dir()
            argv = send.arguments(str(profile), "generic-resume", "Python")
            with patch.dict(os.environ, gui_worker.environment()):
                self.assertEqual(send.validate(argv, profile=profile).resume_id, "generic-resume")
                with self.assertRaises(SystemExit):
                    send.validate(argv)  # No silent change to the CLI's expected profile.

    def test_new_profile_is_generic_existing_cli_untouched(self):
        with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)):
            cli = Path(temp) / ".config/pocketnode-hh/config.toml"
            cli.parent.mkdir(parents=True)
            cli.write_text("sentinel", encoding="utf-8")
            path = gui_profile.initialize()
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text, Path(__file__).with_name("config.example.toml").read_text(encoding="utf-8"))
            self.assertNotIn("history =", text)
            self.assertNotIn("cover_letter_resume_id", text)
            gui_profile.set_model('model"quoted')
            import tomllib
            self.assertEqual(tomllib.loads(path.read_text(encoding="utf-8"))["openai"]["default"]["model"], 'model"quoted')
            gui_profile.initialize()
            self.assertEqual(cli.read_text(encoding="utf-8"), "sentinel")
            self.assertEqual(gui_profile.read_searches(), {})

    def test_atomic_per_user_search_and_optional_load_path(self):
        from send_all import load_searches
        with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)):
            gui_profile.save_search("r1", 'Linux OR "support engineer"')
            gui_profile.save_search("r2", "Python")
            mapping = gui_profile.read_searches()
            self.assertEqual(set(mapping), {"r1", "r2"})
            self.assertEqual(load_searches(gui_profile.search_path()), mapping)
            with patch.dict(os.environ, {"POCKETNODE_HH_SEARCH_FILE": str(gui_profile.search_path())}):
                self.assertEqual(load_searches(), mapping)
            self.assertEqual(list(gui_profile.search_path().parent.iterdir()), [gui_profile.search_path()])
            for invalid in ({}, {"": "x"}, {"r": " "}, {"r": 2}, []):
                with self.assertRaises(ValueError):
                    gui_profile.validate_searches(invalid)
            with patch("gui_profile.os.replace", side_effect=OSError):
                with self.assertRaises(OSError):
                    gui_profile.save_search("r3", "new")
            self.assertEqual(gui_profile.read_searches(), mapping)

    def test_default_cli_profile_cannot_be_redirected_by_env(self):
        with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)):
            with patch.dict(os.environ, {"CONFIG_DIR": "untrusted"}):
                self.assertEqual(gui_profile.expected_profile(), Path(temp) / ".config/pocketnode-hh")
                with self.assertRaises(SystemExit):
                    gui_profile.expected_profile(Path(temp) / "arbitrary")

    def test_generic_facts_fetched_even_for_configured_id(self):
        from resume_context import selected_resume_context
        tool = Mock()
        tool.get_resumes.return_value = [{"id": "r", "status": {"id": "published"}}]
        tool.api_client.get.return_value = {"id": "r", "title": "Python", "skill_set": ["Python"]}
        with patch("resume_context.HHApplicantTool", return_value=tool):
            with selected_resume_context("r", "r"):
                pass
            tool.get_resumes.assert_not_called()
            with selected_resume_context("r", "r", force_fetch=True):
                pass
            tool.api_client.get.assert_called_once_with("/resumes/r")


class GateChecks(unittest.TestCase):
    def test_application_exact_payload_preserved_via_injected_review(self):
        text = "Точное письмо\nwith spacing  "
        channel = FakeChannel([{"action": "yes", "text": "ignored, not a replacement"}])
        sender = Mock(return_value=SimpleNamespace(status_code=200))
        gate = ApprovalGate("r", sender, approve=Mock(side_effect=AssertionError("No terminal")), review=ReviewAdapter(channel))
        gate.current = {"id": "123", "name": "Python", "salary": {"from": 100000, "currency": "RUR"}}
        gate.letter = text
        request = requests.Request("POST", "https://api.hh.ru/negotiations",
                                   data={"resume_id": "r", "vacancy_id": "123", "message": text}).prepare()
        body, headers = request.body, dict(request.headers)
        with redirect_stdout(StringIO()):
            gate.send(None, request)
        self.assertEqual(request.body, body)
        self.assertEqual(dict(request.headers), headers)
        self.assertEqual(channel.reviews[0]["text"], text)
        self.assertEqual(sender.call_count, 1)
        with self.assertRaises(SystemExit), redirect_stdout(StringIO()):
            gate.send(None, request)

    def make_chat(self, decisions, *, options=()):
        chat = SimpleNamespace(chat_id=123, reply_options=list(options), employer_name="Test", vacancy_name="Python",
            vacancy_url="https://hh.ru/vacancy/1", resume_title="Python", resume_id="r",
            contact_name="Test", reply_to_message="Question")
        channel = FakeChannel(decisions)
        gate = ChatGate(Mock(), review=ReviewAdapter(channel))
        operation = SimpleNamespace()
        gate.current, gate.operation, gate.generated = chat, operation, "Original"
        gate.check_fresh = Mock(return_value=[])
        gate.original_message = Mock(return_value="ok")
        return gate, operation, channel

    def test_manual_chat_edit_requires_fresh_review(self):
        gate, operation, channel = self.make_chat([
            {"action": "manual", "text": "Updated\nexact  "}, {"action": "yes", "text": ""}])
        with redirect_stdout(StringIO()):
            gate.message(operation, 123, "Original")
        self.assertEqual([r["text"] for r in channel.reviews], ["Original", "Updated\nexact  "])
        gate.original_message.assert_called_once_with(operation, 123, "Updated\nexact  ", refer_url=None)

    def test_ai_instruction_is_not_sent_and_needs_fresh_approval(self):
        gate, operation, channel = self.make_chat([
            {"action": "edit", "text": "make shorter"}, {"action": "yes", "text": ""}])
        gate.refine_candidate = Mock(return_value="Revised")
        with redirect_stdout(StringIO()):
            gate.message(operation, 123, "Original")
        gate.refine_candidate.assert_called_once_with("Original", [], instruction="make shorter")
        gate.original_message.assert_called_once_with(operation, 123, "Revised", refer_url=None)
        self.assertEqual(channel.reviews[-1]["text"], "Revised")

    def test_manual_chat_invalid_option_cannot_send(self):
        gate, operation, _ = self.make_chat([{"action": "manual", "text": "unknown"}], options=["Original", " No "])
        with redirect_stdout(StringIO()), self.assertRaises(SystemExit):
            gate.message(operation, 123, "Original")
        gate.original_message.assert_not_called()

    def test_cancel_during_edit_never_sends(self):
        gate, operation, channel = self.make_chat([{"action": "manual", "text": "Changed"}])
        original = channel.ask
        def ask(payload):
            if len(channel.reviews) == 1:
                raise Cancelled
            return original(payload)
        channel.ask = ask
        with redirect_stdout(StringIO()), self.assertRaises(Cancelled):
            gate.message(operation, 123, "Original")
        gate.original_message.assert_not_called()


class ConstructionChecks(unittest.TestCase):
    def test_every_actual_ui_launch_maps_to_the_intended_worker_action(self):
        for action in ACTIONS:
            with self.subTest(action=action):
                app = gui.Application.__new__(gui.Application)
                app.session = None
                app.model = SimpleNamespace(get=lambda: "offline-model")
                app.selected_id, app.save = Mock(return_value="r"), Mock(return_value=True)
                app.search = SimpleNamespace(get=lambda: "Python")
                app.all_resumes = SimpleNamespace(get=lambda: True)
                app.resumes, app.searches = [{"id": "r", "title": "Resume"}], {"r": "Python"}
                app.limit = SimpleNamespace(get=lambda: "2")
                app.minimum, app.maximum = SimpleNamespace(get=lambda: "60"), SimpleNamespace(get=lambda: "180")
                app.dialogs = Mock()
                app.check_recovery = Mock(return_value=True)
                app.dialogs.askyesno.return_value = True
                app.clear_review, app.replace_text = Mock(), Mock()
                app.context, app.letter, app.preview_box, app.status = Mock(), Mock(), Mock(), Mock()
                app.buttons, app.stop_button, app.force_button = [], Mock(), Mock()
                with patch("gui_profile.set_model"), patch("gui_controller.Session") as session:
                    app.launch(action)
                    self.assertEqual(session.call_args.args[0], action)
                    settings = session.call_args.args[1]
                    gui_worker.validate_start(frame(uuid4().hex, "start", {"action": action, "settings": settings}))
                    session.return_value.start.assert_called_once()
                    app.dialogs.showerror.assert_not_called()

    def test_all_resume_launch_refuses_before_session_when_a_search_is_missing(self):
        for action in ("apply", "auto"):
            with self.subTest(action=action):
                app = gui.Application.__new__(gui.Application)
                app.session = None
                app.model = SimpleNamespace(get=lambda: "offline-model")
                app.selected_id, app.save = Mock(return_value="r"), Mock(return_value=True)
                app.search = SimpleNamespace(get=lambda: "Python")
                app.all_resumes = SimpleNamespace(get=lambda: True)
                app.resumes = [{"id": "r", "title": "First"}, {"id": "s", "title": "Second"}]
                app.searches = {"r": "Python"}
                app.dialogs = Mock()
                app.check_recovery = Mock(return_value=True)
                with patch("gui_profile.set_model"), patch("gui_controller.Session") as session:
                    app.launch(action)
                    session.assert_not_called()
                message = app.dialogs.showerror.call_args.args[1]
                self.assertIn("Second", message)
                self.assertNotIn("First", message)

    def test_all_action_mapping_and_start_validation(self):
        for action in ACTIONS:
            settings = gui_controller.action_settings(action, resume="r", search='Python OR "support"', consent=True)
            self.assertEqual(gui_worker.validate_start(frame(uuid4().hex, "start", {"action": action, "settings": settings})), (action, settings))
        with self.assertRaises(ValueError):
            gui_controller.action_settings("upstream apply")
        for settings in ({}, {"resume": "r", "search": "x", "all": True, "limit": 2, "min_pause": 0, "max_pause": 1, "consent": True}):
            with self.assertRaises((ProtocolError, ValueError)):
                gui_worker.validate_start(frame(uuid4().hex, "start", {"action": "auto", "settings": settings}))
        settings = gui_controller.action_settings("auto", resume="r", search="x")
        with self.assertRaises(ProtocolError):
            gui_worker.validate_start(frame(uuid4().hex, "start", {"action": "auto", "settings": settings}))

    def test_frozen_and_source_invocations_no_credentials(self):
        with patch.object(sys, "frozen", True, create=True):
            self.assertEqual(gui_runtime.worker_command(), [sys.executable, "--worker"])
            self.assertEqual(gui_runtime.worker_command("--gateway-worker"), [sys.executable, "--gateway-worker"])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            module = root / "windows/gui_runtime.py"
            with patch.object(sys, "frozen", False, create=True), patch.object(gui_runtime, "__file__", str(module)), \
                 patch.object(Path, "is_file", autospec=True, return_value=True) as exists:
                for mode, action, runtime in (("--worker", None, "hh"),
                                             ("--worker", "chatgpt_login", "chatmock"),
                                             ("--gateway-worker", None, "chatmock")):
                    command = gui_runtime.worker_command(mode, action=action)
                    python = root / "venvs" / runtime / "Scripts/python.exe"
                    self.assertEqual(command, [str(python), "-u", str(root / "windows/gui.py"), mode])
                    self.assertEqual(exists.call_args.args, (python,))
        with self.assertRaises(ValueError):
            gui_runtime.worker_command("--apply")
        self.assertEqual(gui_runtime.MUTEX_NAME, "Local\\PocketNode-HH-Run")

    def test_entrypoint_routes_before_creating_tk(self):
        with patch("gui_worker.worker_main", return_value=0) as worker:
            self.assertEqual(gui.main(["--worker"]), 0)
            worker.assert_called_once_with()
        with patch("gui_runtime.gateway_main", return_value=0) as worker:
            self.assertEqual(gui.main(["--gateway-worker"]), 0)
            worker.assert_called_once_with()
        self.assertEqual(gui.main(["--unknown"]), 2)

    def test_worker_parser_commands_do_not_depend_on_loose_operation_files(self):
        from hh_applicant_tool import tool
        original = tool.iter_modules
        with gui_worker.hh_parser_modules():
            parser = tool.HHApplicantTool._create_parser()
            for command in ("authorize", "apply", "reply-chats"):
                self.assertTrue(callable(parser.parse_args([command]).operation_run))
        self.assertIs(tool.iter_modules, original)

    def test_dispatch_only_guarded_application_and_reply_routes(self):
        import send, reply_chats, preview, list_resumes
        channel = FakeChannel()
        cases = (("preview", preview, "run"), ("apply", send, "run"), ("auto", send, "run"),
                 ("chats", reply_chats, "run"), ("resumes", list_resumes, "run"))
        for action, module, name in cases:
            with self.subTest(action=action), patch.object(module, name, return_value=0) as run:
                settings = gui_controller.action_settings(action, resume="r", search="Python", all_resumes=False, consent=True)
                self.assertEqual(gui_worker.dispatch(action, settings, channel), 0)
                self.assertEqual(run.call_args.kwargs["profile"], gui_profile.profile_dir())
                if action == "apply":
                    self.assertIs(run.call_args.kwargs["gate_factory"].func, ApprovalGate)
                if action == "chats":
                    self.assertIsInstance(run.call_args.kwargs["review"], ReviewAdapter)

    def test_hh_auth_module_input_only_no_secrets_in_argv(self):
        original = builtins.input
        channel = FakeChannel([{"action": "submit", "text": "test@example.invalid"}, {"action": "submit", "text": "123456"}])
        def fake_main(argv):
            from hh_applicant_tool.operations import authorize
            self.assertEqual(authorize.input("ignored"), "test@example.invalid")
            self.assertEqual(authorize.input("ignored"), "123456")
            self.assertNotIn("123456", " ".join(argv))
            self.assertNotIn("test@example.invalid", " ".join(argv))
            self.assertIs(builtins.input, original)
            return 0
        with patch("gui_worker.version", return_value="2.3.7"), patch("hh_applicant_tool.main.main", fake_main):
            self.assertEqual(gui_worker.hh_login(channel), 0)
        self.assertIs(builtins.input, original)

    def test_worker_exception_is_sanitized_and_unknown_send_reported(self):
        reader, writer = Reader(), Writer()
        reader.put(frame(uuid4().hex, "start", {"action": "resumes", "settings": {}}))
        with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)), \
             patch("gui_worker.dispatch", side_effect=RuntimeError("SECRET must not leak")):
            self.assertEqual(gui_worker.worker_main(reader, writer), 1)
        reader.put("")
        self.assertEqual(writer.items[-1]["type"], "done")
        self.assertFalse(writer.items[-1]["payload"]["success"])
        self.assertNotIn("SECRET", json.dumps(writer.items))

    def test_worker_preserves_transport_payload_and_reports_uncertain_post_once(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                reader, writer = Reader(), Writer()
                reader.put(frame(uuid4().hex, "start", {"action": "resumes", "settings": {}}))
                request = requests.Request("POST", "https://api.hh.ru/negotiations",
                    data={"resume_id": "r", "vacancy_id": "1", "message": "Exact\n  letter"}).prepare()
                response = requests.Response()
                response.status_code = 201
                response._content = b"{}"
                sender = Mock(side_effect=requests.ReadTimeout("SECRET") if failure else None,
                              return_value=response)
                def dispatch(_action, _settings, _channel):
                    print("yes SECRET ordinary output is discarded")
                    requests.Session().send(request, timeout=(2, 7), allow_redirects=False, verify=True)
                    return 0
                with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)), \
                     patch.object(requests.sessions.Session, "send", sender), patch("gui_worker.dispatch", dispatch):
                    self.assertEqual(gui_worker.worker_main(reader, writer), 1 if failure else 0)
                reader.put("")
                sender.assert_called_once()
                self.assertIs(sender.call_args.args[1], request)
                self.assertEqual(sender.call_args.kwargs, {"timeout": (2, 7), "allow_redirects": False, "verify": True})
                done = writer.items[-1]["payload"]
                self.assertEqual(done["unknown"], failure)
                self.assertEqual(done["counts"]["attempts"], 1)
                self.assertEqual(done["counts"]["http_success"], 0 if failure else 1)
                self.assertNotIn("SECRET", json.dumps(writer.items))

    def test_worker_cancel_before_transport_cannot_start_network_request(self):
        reader, writer = Reader(), Writer()
        reader.put(frame(uuid4().hex, "start", {"action": "resumes", "settings": {}}))
        def dispatch(_action, _settings, channel):
            channel.cancelled.set()
            requests.Session().send(requests.Request("GET", "https://api.hh.ru/resumes/mine").prepare())
        with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)), \
             patch.object(requests.sessions.Session, "send") as sender, patch("gui_worker.dispatch", dispatch):
            self.assertEqual(gui_worker.worker_main(reader, writer), 1)
            sender.assert_not_called()
        reader.put("")
        self.assertTrue(writer.items[-1]["payload"]["cancelled"])

    def test_worker_requires_configured_model_before_generation(self):
        reader, writer = Reader(), Writer()
        settings = gui_controller.action_settings("preview", resume="r", search="Python")
        reader.put(frame(uuid4().hex, "start", {"action": "preview", "settings": settings}))
        with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)), \
             patch("gui_worker.dispatch") as dispatch:
            self.assertEqual(gui_worker.worker_main(reader, writer), 1)
            dispatch.assert_not_called()
        reader.put("")
        self.assertFalse(writer.items[-1]["payload"]["success"])

    def test_chatgpt_adapter_emits_structured_device_data_not_print_scraping(self):
        import sys
        from types import ModuleType
        channel = FakeChannel()
        response = SimpleNamespace(status_code=200, json=lambda: {"user_code": "offline-code"})
        oauth = SimpleNamespace(DEFAULT_ISSUER="https://auth.openai.com", requests=SimpleNamespace(post=Mock(return_value=response)),
                                time=SimpleNamespace())
        def login(client, *, verbose):
            self.assertEqual(client, "public-client-id")
            self.assertFalse(verbose)
            oauth.requests.post("https://auth.openai.com/api/accounts/deviceauth/usercode", json={"client_id": client})
            return True
        oauth.run_device_code_login = login
        package, config = ModuleType("chatmock"), ModuleType("chatmock.config")
        package.oauth, config.CLIENT_ID_DEFAULT = oauth, "public-client-id"
        with patch.dict(sys.modules, {"chatmock": package, "chatmock.config": config}), \
             patch("gui_worker.version", return_value="1.40"), patch("gui_chatmock_auth.install") as install:
            gui_worker.chatgpt_login(channel)
            install.return_value.require_auth.assert_called_once_with()
        self.assertEqual(channel.events, [("device", {"url": "https://auth.openai.com/codex/device", "code": "offline-code"})])

    def test_all_resume_generic_dispatch_does_not_require_personal_default_id(self):
        import send
        with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)):
            gui_profile.save_search("generic-resume", "Python")
            with patch.dict(os.environ, gui_worker.environment()), patch.object(send, "run", return_value=0) as run:
                gui_worker.dispatch("apply", gui_controller.action_settings("apply", resume="generic-resume", search="Python",
                                                                           all_resumes=True), FakeChannel())
            argv = run.call_args.args[0]
            self.assertIn("--resume-id=generic-resume", argv)
            self.assertTrue(run.call_args.kwargs["all_resumes"])

    def test_windowed_std_handles_attached_before_worker_import(self):
        with patch("gui_runtime.attach_standard_streams") as attach, patch("gui_worker.worker_main", return_value=0):
            self.assertEqual(gui.main(["--worker"]), 0)
            attach.assert_called_once_with()

    def test_real_source_worker_rejects_malformed_start_without_profile_or_network(self):
        result = subprocess.run(source_worker_command(), input="{}\n", capture_output=True,
            encoding="utf-8", timeout=15, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            **gui_runtime.process_options())
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_clean_checkout_without_repo_runtimes_keeps_offline_checks_independent(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(sys, "frozen", False, create=True), \
             patch.object(gui_runtime, "__file__", str(Path(temp) / "windows/gui_runtime.py")), \
             patch.object(Path, "is_file", autospec=True, return_value=False):
            for mode, action in (("--worker", None), ("--worker", "chatgpt_login"), ("--gateway-worker", None)):
                with self.assertRaisesRegex(RuntimeError, "runtime is missing"):
                    gui_runtime.worker_command(mode, action=action)
            # The production refusal above must not prevent real offline worker
            # boot or mocked controller lifecycle tests in a unified CI venv.
            self.test_real_source_worker_rejects_malformed_start_without_profile_or_network()
            LifecycleChecks().test_clean_completion_and_gateway_ownership()

    @unittest.skipUnless(os.name == "nt", "Windows inherited handles")
    def test_windowed_bootloader_missing_stdio_recovers_actual_private_pipes(self):
        import sys
        code = ("import sys; sys.path.insert(0, " + repr(str(Path(__file__).resolve().parent)) + "); "
                "sys.stdin = sys.stdout = sys.stderr = None; "
                "from gui_runtime import attach_standard_streams; attach_standard_streams(); "
                "sys.stdout.write('private pipe ready\\n'); sys.stdout.flush(); "
                "import gui; raise SystemExit(gui.main(['--worker']))")
        result = subprocess.run([sys.executable, "-B", "-c", code], input="{}\n", capture_output=True,
            encoding="utf-8", timeout=15, **gui_runtime.process_options())
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "private pipe ready\n")
        self.assertEqual(result.stderr, "")

    def test_real_tk_widgets_construct_with_only_a_disposable_profile(self):
        import tkinter as tk
        from tkinter import ttk
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest("Tk display unavailable: " + type(error).__name__)
        root.withdraw()
        try:
            with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)):
                app = gui.Application(root, tk, ttk, Mock())
                root.update_idletasks()
                self.assertEqual(str(app.yes["state"]), "disabled")
                self.assertEqual(str(app.submit["state"]), "disabled")
                self.assertEqual(app.previews, [])
                self.assertIsNone(app.session)
                app.session = gui_controller.Session("preview", {})
                app.handle_frame(frame(app.session.run, "event", {"name": "preview_skipped", "data": {
                    "vacancy": {"id": "1", "name": "Example"}, "reason": "salary ceiling", "number": 1}}))
                self.assertEqual(app.letter.get("1.0", "end-1c"), "Пропущена: salary ceiling")
                self.assertEqual(app.previews[0]["reason"], "salary ceiling")
                self.assertEqual(str(app.yes["state"]), "disabled")
                session = gui_controller.Session("chats", {})
                app.session = session
                review = frame(session.run, "review", {"kind": "chat", "context": "Question",
                    "text": "Original exact text", "options": []})
                app.handle_frame(review)
                self.assertEqual(app.letter.get("1.0", "end-1c"), "Original exact text")
                self.assertEqual(str(app.yes["state"]), "normal")
                app.manual.insert("1.0", "Updated\nexact  ")
                app.manual_edit()
                response = session.outgoing.get_nowait()
                self.assertEqual(response["id"], review["id"])
                self.assertEqual(response["payload"], {"action": "manual", "text": "Updated\nexact  "})
                self.assertEqual(str(app.yes["state"]), "disabled")
                self.assertIsNone(session.state.pending)
                fresh = frame(session.run, "review", {"kind": "chat", "context": "Question",
                    "text": "Updated\nexact  ", "options": []})
                app.handle_frame(fresh)
                self.assertTrue(session.outgoing.empty())  # The edit did not also approve.
                with self.assertRaises(ProtocolError):
                    app.handle_frame(frame(session.run, "event", {"name": "letter", "data": {
                        "vacancy": {}, "text": "Unreviewed replacement"}}))
                self.assertEqual(app.letter.get("1.0", "end-1c"), "Updated\nexact  ")
                app.respond("yes")
                response = session.outgoing.get_nowait()
                self.assertEqual(response["id"], fresh["id"])
                self.assertEqual(response["payload"]["action"], "yes")
        finally:
            root.destroy()


class StepChecks(unittest.TestCase):
    def test_new_user_sees_one_current_step_and_locked_actions_explain_why(self):
        from gui_steps import CURRENT, DONE, LOCKED, Progress, blocker, next_hint, step_states
        fresh = Progress()
        self.assertEqual(step_states(fresh), [CURRENT, LOCKED, LOCKED, LOCKED])
        self.assertIn("Войти в HH", next_hint(fresh))
        self.assertIsNone(blocker("hh_login", fresh))
        self.assertIn("войди в HH", blocker("resumes", fresh))
        self.assertIn("войди в HH", blocker("preview", fresh))
        hh_only = Progress(hh=True)
        self.assertEqual(step_states(hh_only), [CURRENT, "open", LOCKED, LOCKED])
        self.assertIn("ChatGPT", blocker("models", hh_only))
        ready = Progress(hh=True, chatgpt=True, resume=True, search=True, model=True)
        self.assertEqual(step_states(ready), [DONE, DONE, DONE, CURRENT])
        for action in ("preview", "apply", "auto", "chats", "resumes", "models"):
            self.assertIsNone(blocker(action, ready))
        no_search = Progress(hh=True, chatgpt=True, resume=True, model=True)
        self.assertIn("поисковый запрос", blocker("apply", no_search))
        self.assertIsNone(blocker("chats", no_search))

    def test_auto_fields_are_validated_in_plain_language(self):
        from auto_send import validate_settings
        from gui_steps import parse_auto_settings
        values, problem = parse_auto_settings("3", "60", "90,5")
        self.assertIsNone(problem)
        validate_settings(*values)
        for fields in (("x", "60", "180"), ("0", "60", "180"), ("1", "0", "1"), ("1", "200", "100"), ("1", "a", "1")):
            values, problem = parse_auto_settings(*fields)
            self.assertIsNone(values)
            self.assertTrue(problem)

    def test_login_state_reads_only_key_presence_from_disposable_profile(self):
        from gui_steps import hh_signed_in
        with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)):
            self.assertFalse(hh_signed_in())
            path = gui_profile.profile_dir() / "token.json"
            path.parent.mkdir(parents=True)
            path.write_text("{broken", encoding="utf-8")
            self.assertFalse(hh_signed_in())
            path.write_text(json.dumps({"token": {"refresh_token": "offline"}}), encoding="utf-8")
            self.assertTrue(hh_signed_in())

    def test_failure_message_names_the_action_and_hides_unknown_send_warning_for_logins(self):
        app = gui.Application.__new__(gui.Application)
        app.sending = False
        data = {"done": None, "success": False, "cancelled": False, "forced": False}
        self.assertIn("Вход в HH не выполнен", app.finish_message(data, "hh_login"))
        self.assertNotIn("неизвестен", app.finish_message(data, "hh_login"))
        self.assertIn("неизвестен", app.finish_message(data, "apply"))

    def test_real_tk_first_run_locks_later_steps_and_shows_login_input_in_step_one(self):
        import tkinter as tk
        from tkinter import ttk
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest("Tk display unavailable: " + type(error).__name__)
        root.withdraw()
        try:
            with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)):
                app = gui.Application(root, tk, ttk, Mock())
                root.update_idletasks()
                self.assertEqual(str(app.action_buttons["hh_login"]["state"]), "normal")
                self.assertEqual(str(app.action_buttons["hh_login"]["style"]), "Accent.TButton")
                for action in ("resumes", "models", "preview", "apply", "auto", "chats"):
                    self.assertEqual(str(app.action_buttons[action]["state"]), "disabled", action)
                self.assertIn("Войти в HH", app.status.get())
                self.assertFalse(app.decision.winfo_manager())  # No disabled send button on first run.
                app.show_tab(1)  # The selected section must be unambiguous.
                self.assertEqual([str(button["style"]) for button, _line in app.selector_items],
                                 ["Selector.TButton", "SelectorOn.TButton"])
                self.assertEqual([str(line["style"]) for _button, line in app.selector_items],
                                 ["IndicatorOff.TFrame", "IndicatorOn.TFrame"])
                app.show_tab(0)
                app.session = gui_controller.Session("hh_login", {})
                app.handle_frame(frame(app.session.run, "review", {"kind": "credential",
                    "context": "Email или телефон от аккаунта HH", "text": "", "options": []}))
                self.assertEqual(app.credential_frame.winfo_manager(), "grid")
                self.assertEqual(str(app.credential["show"]), "")  # The person sees what they type.
                self.assertEqual(app.credential_label["text"], "Email или телефон от аккаунта HH")
                app.credential.insert(0, "test@example.invalid")
                app.submit_credential()
                self.assertEqual(app.session.outgoing.get_nowait()["payload"],
                                 {"action": "submit", "text": "test@example.invalid"})
        finally:
            root.destroy()


class LifecycleChecks(unittest.TestCase):
    def make_session(self, action="resumes"):
        mutex, gateway = Mock(), Mock()
        gateway.close.return_value = True
        process = Mock()
        process.stdin = StringIO()
        process.poll.return_value = 0
        process.wait.return_value = 0
        session = gui_controller.Session(action, {}, popen=Mock(return_value=process),
            mutex_factory=lambda: mutex, gateway_factory=lambda *_: gateway,
            job_factory=lambda: SimpleNamespace(spawn=lambda command, *, popen, **kw: popen(command, **kw)),
            marker_factory=Mock)
        return session, process, mutex, gateway

    def test_clean_completion_and_gateway_ownership(self):
        session, process, mutex, gateway = self.make_session("models")
        process.stdout = StringIO(encode(frame(session.run, "done", {"success": True, "cancelled": False,
            "unknown": False, "counts": {"attempts": 0, "http_success": 0, "captcha_rejections": 0}})))
        run_session(session)
        self.assertTrue(session.finished.is_set())
        mutex.acquire.assert_called_once()
        mutex.release.assert_called_once()
        gateway.start.assert_called_once()
        gateway.close.assert_called_once_with(success=True)

    def test_eof_without_completion_is_never_success(self):
        session, process, _, gateway = self.make_session()
        process.stdout = StringIO("")
        run_session(session)
        gateway.close.assert_called_once_with(success=False)
        events = list(session.events.queue)
        self.assertFalse(events[-1][1]["success"])
        self.assertIsNone(events[-1][1]["done"])

    def test_busy_shared_mutex_starts_nothing_and_is_reported_as_busy(self):
        session, _, mutex, gateway = self.make_session("hh_login")
        mutex.acquire.side_effect = gui_runtime.RunBusy("busy")
        run_session(session)
        session.popen.assert_not_called()
        gateway.start.assert_not_called()
        events = list(session.events.queue)
        self.assertEqual(events[-1][0], "finished")
        self.assertTrue(events[-1][1]["busy"])
        self.assertFalse(events[-1][1]["success"])
        self.assertIn("другой запуск HH", events[-2][1])

    def test_cancel_before_spawn_cannot_start_new_requests(self):
        session, _, mutex, gateway = self.make_session("models")
        session.cancel()
        run_session(session)
        session.popen.assert_not_called()
        gateway.start.assert_not_called()
        mutex.release.assert_called_once()

    def test_malformed_or_wrong_run_output_cancels(self):
        for output in ("yes\n", encode(frame(uuid4().hex, "done", {}))):
            session, process, _, gateway = self.make_session()
            process.stdout = StringIO(output)
            run_session(session)
            self.assertTrue(session.cancelled.is_set())
            gateway.close.assert_called_once_with(success=False)

    def test_external_gateway_is_never_stopped(self):
        gateway = gui_runtime.Gateway(Mock(), threading.Event())
        with patch("gui_runtime.gateway_ready", return_value=True), patch("gui_runtime.subprocess.Popen") as popen:
            gateway.start()
            self.assertTrue(gateway.close(success=True))
            popen.assert_not_called()
            self.assertIsNone(gateway.process)

    def test_owned_gateway_invalid_gui_auth_cannot_start_or_search_another_profile(self):
        import gui_chatmock_auth
        with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)):
            store = gui_chatmock_auth.GUIAuthStore()
            store.home.mkdir(parents=True)
            store.path.write_text("synthetic invalid auth", encoding="utf-8")
            gateway = gui_runtime.Gateway(Mock(), threading.Event())
            with patch("gui_runtime.gateway_ready", return_value=False), \
                 patch("gui_owned_process.owned_job") as job, \
                 patch.object(Path, "open", autospec=True, wraps=Path.open) as opened:
                # A wrapped Path.open is not needed to deserialize credentials;
                # it records the only attempted auth path, then fails closed.
                opened.side_effect = PermissionError("synthetic unreadable GUI auth")
                with self.assertRaises(RuntimeError):
                    gateway.start()
                self.assertEqual([call.args[0] for call in opened.call_args_list], [store.path])
                job.assert_not_called()

    def test_gateway_entry_installs_guard_and_rejects_invalid_auth_before_server_start(self):
        import sys
        server = SimpleNamespace(main=Mock())
        guard = Mock()
        guard.require_auth.side_effect = RuntimeError("No account fallback")
        with patch("gui_chatmock_auth.install", return_value=guard) as install, \
             patch("importlib.metadata.version", return_value="1.40"), \
             patch.dict(sys.modules, {"chatmock": SimpleNamespace(utils=Mock()), "chatmock_server": server}), \
             patch.dict(os.environ, {}):
            with self.assertRaisesRegex(RuntimeError, "No account fallback"):
                gui_runtime.gateway_main()
            install.assert_called_once_with()
            guard.require_auth.assert_called_once_with()
            server.main.assert_not_called()

    def test_failed_owned_gateway_preserves_logs(self):
        with tempfile.TemporaryDirectory() as temp:
            gateway = gui_runtime.Gateway(Mock(), threading.Event())
            log = Path(temp) / "owned.log"
            log.write_text("metadata", encoding="utf-8")
            gateway.logs = [log]
            gateway.process = Mock()
            gateway.process.poll.return_value = None
            self.assertTrue(gateway.close(success=False))
            gateway.process.terminate.assert_called_once()
            self.assertTrue(log.exists())
            gateway.process.poll.return_value = 0
            gateway.close(success=True)
            self.assertFalse(log.exists())

    def test_close_during_review_requests_cancel_not_approval(self):
        app = gui.Application.__new__(gui.Application)
        app.session, app.root, app.dialogs = Mock(), Mock(), Mock()
        app.dialogs.askyesno.return_value = True
        app.stop = Mock()
        app.close()
        app.stop.assert_called_once()
        app.root.destroy.assert_not_called()
        app.session.respond.assert_not_called()


class OwnedJobChecks(unittest.TestCase):
    def api(self):
        import ctypes
        from ctypes import wintypes
        api = SimpleNamespace()
        api.CreateJobObjectW = Mock(return_value=0x123456789)
        api.SetInformationJobObject = Mock(return_value=True)
        api.AssignProcessToJobObject = Mock(return_value=True)
        api.GetCurrentProcess = Mock(return_value=0xABCDEF123)
        api.CloseHandle = Mock(return_value=True)
        def member(_process, _job, output):
            ctypes.cast(output, ctypes.POINTER(wintypes.BOOL)).contents.value = True
            return True
        api.IsProcessInJob = Mock(side_effect=member)
        api.CreateToolhelp32Snapshot = Mock(return_value=101)
        def first(_snapshot, output):
            entry = ctypes.cast(output, ctypes.POINTER(gui_owned_process.ThreadEntry)).contents
            entry.th32OwnerProcessID, entry.th32ThreadID = 123, 456
            return True
        api.Thread32First, api.Thread32Next = Mock(side_effect=first), Mock(return_value=False)
        api.OpenThread, api.ResumeThread = Mock(return_value=102), Mock(return_value=1)
        api.GetProcessIdOfThread = Mock(return_value=123)
        return api

    def process(self):
        process = Mock(pid=123, _handle=0xA123456789)
        process.poll.return_value = None
        process.stdin, process.stdout, process.stderr = StringIO(), StringIO(), None
        return process

    def test_noninherited_gui_membership_precedes_suspended_creation_and_resume(self):
        import ctypes
        api = self.api()
        events = []
        api.AssignProcessToJobObject.side_effect = lambda *_args: events.append("parent-contained") or True
        api.ResumeThread.side_effect = lambda *_args: events.append("resume") or 1
        job = gui_owned_process.OwnedJob(api)
        process = self.process()
        def popen(_command, **options):
            self.assertEqual(options["creationflags"] & 4, 4)
            events.append("create-suspended")
            return process
        self.assertIs(job.spawn(["offline"], popen=popen, creationflags=0x08000000), process)
        self.assertEqual(events, ["parent-contained", "create-suspended", "resume"])
        api.CreateJobObjectW.assert_called_once_with(None, None)
        limits = ctypes.cast(api.SetInformationJobObject.call_args.args[2],
                             ctypes.POINTER(gui_owned_process.ExtendedLimits)).contents
        self.assertEqual(limits.BasicLimitInformation.LimitFlags, 0x2000)
        self.assertEqual(api.IsProcessInJob.call_args.args[:2], (process._handle, job.handle))
        self.assertNotIn(job.handle, [call.args[0] for call in api.CloseHandle.call_args_list])
        process.terminate.assert_not_called()

    def test_parent_job_failure_never_creates_child(self):
        for failure in ("CreateJobObjectW", "SetInformationJobObject", "AssignProcessToJobObject"):
            api = self.api()
            getattr(api, failure).side_effect = None
            getattr(api, failure).return_value = 0
            with self.subTest(failure=failure), self.assertRaises(RuntimeError):
                gui_owned_process.OwnedJob(api)

    def test_unverified_child_is_terminated_waited_and_pipes_closed_without_running(self):
        for failure in ("membership", "snapshot", "ambiguous", "thread", "identity", "resume", "missing_handle"):
            api, process = self.api(), self.process()
            job = gui_owned_process.OwnedJob(api)
            if failure == "membership":
                api.IsProcessInJob.side_effect = None
                api.IsProcessInJob.return_value = False
            elif failure == "snapshot":
                api.CreateToolhelp32Snapshot.return_value = 0
            elif failure == "ambiguous":
                api.Thread32First.side_effect = None
                api.Thread32First.return_value = False
            elif failure == "thread":
                api.OpenThread.return_value = 0
            elif failure == "identity":
                api.GetProcessIdOfThread.return_value = 999
            elif failure == "resume":
                api.ResumeThread.return_value = 0xffffffff
            else:
                process._handle = None
            with self.subTest(failure=failure), self.assertRaises(RuntimeError):
                job.spawn(["offline"], popen=Mock(return_value=process))
            process.terminate.assert_called_once()
            process.wait.assert_called_once()
            self.assertTrue(process.stdin.closed)
            self.assertTrue(process.stdout.closed)
            if failure != "resume":
                api.ResumeThread.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows ctypes signatures")
    def test_native_signatures_preserve_64bit_handles_and_size_t(self):
        import ctypes
        from ctypes import wintypes
        functions = {name: Mock() for name in ("CreateJobObjectW", "SetInformationJobObject", "AssignProcessToJobObject",
            "IsProcessInJob", "GetCurrentProcess", "CloseHandle", "CreateToolhelp32Snapshot", "Thread32First",
            "Thread32Next", "OpenThread", "GetProcessIdOfThread", "ResumeThread")}
        with patch("gui_owned_process.ctypes.WinDLL", return_value=SimpleNamespace(**functions)):
            api = gui_owned_process.windows_api()
        self.assertIs(api.CreateJobObjectW.restype, wintypes.HANDLE)
        self.assertEqual(api.AssignProcessToJobObject.argtypes, (wintypes.HANDLE, wintypes.HANDLE))
        self.assertIs(api.CreateToolhelp32Snapshot.restype, wintypes.HANDLE)
        self.assertEqual(ctypes.sizeof(gui_owned_process.ExtendedLimits), 144 if ctypes.sizeof(ctypes.c_void_p) == 8 else 112)

    @unittest.skipUnless(os.name == "nt", "Windows owned-process lifetime smoke")
    def test_actual_parent_normal_and_abrupt_exit_kill_only_owned_disposable_child(self):
        import ctypes
        from ctypes import wintypes
        import sys
        # Only the disposable helper joins the job, never this test process.
        code = ("import sys,subprocess; sys.path.insert(0," + repr(str(Path(__file__).resolve().parent)) + "); "
                "from gui_owned_process import owned_job; "
                "p=owned_job().spawn([sys.executable,'-B','-c','import time; time.sleep(30)'],"
                "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                "print(p.pid,flush=True); sys.stdin.readline()")
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes, api.OpenProcess.restype = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD), wintypes.HANDLE
        api.WaitForSingleObject.argtypes, api.WaitForSingleObject.restype = (wintypes.HANDLE, wintypes.DWORD), wintypes.DWORD
        api.CloseHandle.argtypes, api.CloseHandle.restype = (wintypes.HANDLE,), wintypes.BOOL
        for abrupt in (False, True):
            with self.subTest(abrupt=abrupt):
                external = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(30)"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    **gui_runtime.process_options())
                parent = subprocess.Popen([sys.executable, "-B", "-c", code], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **gui_runtime.process_options())
                handle = None
                try:
                    lines = queue.Queue()
                    threading.Thread(target=lambda: lines.put(parent.stdout.readline()), daemon=True).start()
                    line = lines.get(timeout=10)
                    self.assertTrue(line.strip().isdigit(), "Disposable contained child failed to start")
                    handle = api.OpenProcess(0x100000, False, int(line))  # SYNCHRONIZE only.
                    self.assertTrue(handle)
                    if abrupt:
                        parent.terminate()
                    else:
                        parent.stdin.write("exit\n")
                        parent.stdin.flush()
                    parent.wait(timeout=10)
                    self.assertEqual(api.WaitForSingleObject(handle, 10000), 0)
                    self.assertIsNone(external.poll())  # Sibling/external gateway analogue is untouched.
                finally:
                    if parent.poll() is None:
                        parent.terminate()
                        parent.wait(timeout=10)
                    if handle:
                        api.CloseHandle(handle)
                    for stream in (parent.stdin, parent.stdout, parent.stderr):
                        stream.close()
                    if external.poll() is None:
                        external.terminate()
                    external.wait(timeout=10)


class RecoveryChecks(unittest.TestCase):
    def test_dirty_marker_is_private_text_free_and_requires_explicit_fresh_acknowledgement(self):
        with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)):
            marker = RecoveryMarker()
            self.assertIsNone(marker.pending())
            marker.begin()
            token = marker.pending()
            self.assertIsNotNone(token)
            self.assertEqual(json.loads(marker.path.read_text()), {"v": 1, "unresolved": True})
            next_run = RecoveryMarker()
            with self.assertRaises(RuntimeError):
                next_run.begin()
            with self.assertRaises(RuntimeError):
                next_run.begin((0, 0, 0))
            next_run.begin(token)
            next_run.complete()
            self.assertIsNone(next_run.pending())

    def test_marker_io_failure_blocks_start_and_malformed_marker_requires_ack(self):
        with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)):
            marker = RecoveryMarker()
            marker.path.parent.mkdir(parents=True)
            marker.path.write_text("truncated", encoding="utf-8")
            self.assertIsNotNone(marker.pending())
            with self.assertRaises(RuntimeError):
                marker.begin()
            with patch("gui_recovery.atomic_write", side_effect=OSError), self.assertRaises(OSError):
                marker.begin(marker.pending())

    def test_ui_declining_recovery_cannot_authorize_mutating_launch(self):
        app = gui.Application.__new__(gui.Application)
        app.session, app.status, app.dialogs = None, Mock(), Mock()
        app.dialogs.askyesno.return_value = False
        with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)):
            RecoveryMarker().begin()
            with patch("gui_controller.Session") as session:
                app.launch("apply")
                session.assert_not_called()
            self.assertIsNotNone(RecoveryMarker().pending())

    def test_controller_marker_failure_precedes_all_child_creation(self):
        mutex, gateway, job, marker = Mock(), Mock(), Mock(), Mock()
        marker.begin.side_effect = OSError("offline marker failure")
        session = gui_controller.Session("apply", {}, mutex_factory=lambda: mutex,
            gateway_factory=lambda *_: gateway, job_factory=Mock(return_value=job), marker_factory=lambda: marker)
        run_session(session)
        job.spawn.assert_not_called()
        gateway.start.assert_not_called()
        mutex.release.assert_called_once()

    def test_controller_clears_marker_only_after_confirmed_success_and_cleanup(self):
        for success, stopped in ((False, True), (True, True), (True, False)):
            fixture = LifecycleChecks()
            session, process, _, gateway = fixture.make_session("apply")
            marker = Mock()
            session.marker_factory = lambda: marker
            gateway.close.return_value = stopped
            process.stdout = StringIO(encode(frame(session.run, "done", {"success": success, "cancelled": False,
                "unknown": False, "counts": {"attempts": 0, "http_success": 0, "captcha_rejections": 0}})))
            run_session(session)
            marker.begin.assert_called_once_with(None)
            self.assertEqual(marker.complete.call_count, int(success and stopped))


if __name__ == "__main__":
    unittest.main()
