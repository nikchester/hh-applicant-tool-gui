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

    def test_completion_network_host_is_a_bare_host_on_failure_only(self):
        base = {"success": False, "cancelled": False, "unknown": False,
                "counts": {"attempts": 0, "http_success": 0, "captcha_rejections": 0}}
        valid = {**base, "network": "chatik.hh.ru"}
        self.assertEqual(validate_done(valid), valid)
        for invalid in ({**base, "network": None}, {**base, "network": ""},
                        {**base, "network": "https://auth.openai.com/x?token=1"},
                        {**base, "network": "user@host"}, {**base, "network": "a" * 254},
                        {**valid, "success": True}):
            with self.assertRaises(ProtocolError):
                validate_done(invalid)

    def test_worker_reports_unanswered_host_and_forgets_it_after_an_answer(self):
        def run(answers):
            reader, writer = Reader(), Writer()
            reader.put(frame(uuid4().hex, "start", {"action": "resumes", "settings": {}}))
            sender = Mock(side_effect=answers)

            def dispatch(_action, _settings, _channel):
                for host in ("chatik.hh.ru", "api.hh.ru")[:len(answers)]:
                    try:
                        requests.Session().send(requests.Request(
                            "GET", f"https://user:SECRET@{host.upper()}/x?token=SECRET").prepare())
                    except requests.RequestException:
                        pass
                raise RuntimeError("stop")
            with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)), \
                 patch.object(requests.sessions.Session, "send", sender), patch("gui_worker.dispatch", dispatch):
                self.assertEqual(gui_worker.worker_main(reader, writer), 1)
            reader.put("")
            self.assertNotIn("SECRET", json.dumps(writer.items))
            return writer.items[-1]["payload"]

        self.assertEqual(run([requests.ReadTimeout("x")])["network"], "chatik.hh.ru")
        self.assertEqual(run([requests.ConnectionError("x")])["network"], "chatik.hh.ru")
        self.assertNotIn("network", run([requests.ReadTimeout("x"), Mock(status_code=200)]))

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
            cli = Path(temp) / ".config/hh-applicant-tool-cli/config.toml"
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
            with patch.dict(os.environ, {"HH_APPLICANT_TOOL_SEARCH_FILE": str(gui_profile.search_path())}):
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
                self.assertEqual(gui_profile.expected_profile(), Path(temp) / ".config/hh-applicant-tool-cli")
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
        self.assertEqual(gui_runtime.MUTEX_NAME, "Local\\hh-applicant-tool-gui-run")

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

    def test_resumes_load_without_click_only_after_hh_sign_in(self):
        app = gui.Application.__new__(gui.Application)
        app.root, app.launch, app.resumes, app.session = Mock(), Mock(), [], None
        app.signed_hh = False
        app.auto_load_resumes()
        app.launch.assert_not_called()
        app.signed_hh, app.session = True, object()
        app.auto_load_resumes()
        app.launch.assert_not_called()
        app.root.after.assert_called_once_with(1000, app.auto_load_resumes)
        app.session = None
        app.auto_load_resumes()
        app.launch.assert_called_once_with("resumes")
        app.resumes = [{"id": "r", "title": "DevOps"}]
        app.auto_load_resumes()
        app.launch.assert_called_once()

    def test_worker_reports_own_stop_reason_but_hides_upstream_exit(self):
        import preview_hh
        def own(_action, _settings, _channel):
            preview_hh.check_letter_level("Готов на позицию миддл", {"name": "DevOps"})
        def upstream(_action, _settings, _channel):
            exec(compile("raise SystemExit('SECRET upstream')", "C:/elsewhere/upstream.py", "exec"))
        for dispatch, expected in ((own, "Письмо упоминает уровень джун/миддл для вакансии без Senior/Lead"),
                                   (upstream, None)):
            with self.subTest(expected=expected):
                reader, writer = Reader(), Writer()
                reader.put(frame(uuid4().hex, "start", {"action": "resumes", "settings": {}}))
                with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)),                      patch("gui_worker.dispatch", dispatch):
                    self.assertEqual(gui_worker.worker_main(reader, writer), 1)
                reader.put("")
                stopped = [item["payload"]["data"] for item in writer.items
                           if item["type"] == "event" and item["payload"]["name"] == "stopped"]
                self.assertEqual(stopped, [expected] if expected else [])
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


class AttentionChecks(unittest.TestCase):
    def test_alert_rule_covers_errors_and_long_runs_only(self):
        for action in ("resumes", "models", "hh_login", "auto", "apply", "chats", "preview"):
            with self.subTest(action=action):
                self.assertTrue(gui.needs_attention("error", action))
                self.assertTrue(gui.needs_attention("warning", action))
                self.assertFalse(gui.needs_attention("info", action))  # user pressed stop
        for action in ("auto", "apply", "chats", "preview"):
            self.assertTrue(gui.needs_attention("success", action))
        for action in ("resumes", "models", "hh_login", "chatgpt_login"):
            self.assertFalse(gui.needs_attention("success", action))

    def test_alert_picks_sound_by_severity_and_never_raises(self):
        import winsound
        window = Mock()
        window.winfo_id.side_effect = RuntimeError("no window")
        with patch("winsound.MessageBeep") as beep:
            for level, sound in (("error", winsound.MB_ICONHAND),
                                 ("warning", winsound.MB_ICONEXCLAMATION),
                                 ("success", winsound.MB_ICONASTERISK)):
                gui.attention(window, level)
                self.assertEqual(beep.call_args.args, (sound,))
            beep.side_effect = RuntimeError("no sound device")
            gui.attention(window, "error")

    def test_captcha_event_alerts_and_explains(self):
        session = SimpleNamespace(run="RUN", cancelled=threading.Event())
        app = SimpleNamespace(append_log=Mock(), say=Mock(), root=Mock(), session=session)
        frame = {"run": "RUN", "type": "event", "payload": {"name": "captcha", "data": True}}
        with patch("gui.attention") as alert:
            try:
                gui.Application.handle_frame(app, frame)
            except (AttributeError, KeyError, TypeError) as error:
                self.fail(f"captcha event needs more GUI state than expected: {error!r}")
        alert.assert_called_once_with(app.root, "warning")
        self.assertIn("Капча", app.append_log.call_args.args[0])
        self.assertEqual(app.say.call_args.args[1], "warning")

    def test_finish_run_alerts_through_the_rule(self):
        app = SimpleNamespace(
            close_device_window=Mock(), stop_chat_wait=Mock(), finish_message=Mock(return_value="msg"),
            append_log=Mock(), set_busy=Mock(), say=Mock(), next_hint=Mock(return_value=""),
            finish_severity=Mock(return_value="error"), root=Mock(), show_tab=Mock(),
            work=Mock(winfo_ismapped=Mock(return_value=False)))
        session = SimpleNamespace(action="auto")
        with patch("gui.attention") as alert:
            gui.Application.finish_run(app, session, {"success": False})
            alert.assert_called_once_with(app.root, "error")
            app.finish_severity.return_value = "info"
            gui.Application.finish_run(app, session, {"success": False})
            alert.assert_called_once()


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

    def test_own_stop_reason_replaces_the_login_hint(self):
        app = gui.Application.__new__(gui.Application)
        app.sending = False
        app.stop_reason = "Stopped: 3 letters in a row failed the letter check; check the cover letter prompt"
        data = {"done": None, "success": False, "cancelled": False, "forced": False}
        message = app.finish_message(data, "auto")
        self.assertIn("остановлен проверкой: Stopped: 3 letters", message)
        self.assertNotIn("Проверь вход в HH", message)

    def test_rejected_letter_is_logged_with_vacancy_and_text(self):
        from gui_protocol import LETTER_LEVEL_REJECTED
        app = gui.Application.__new__(gui.Application)
        app.session = SimpleNamespace(run="r", cancelled=threading.Event())
        app.append_log = Mock()

        def event(data):
            app.handle_frame({"run": "r", "type": "event",
                              "payload": {"name": "letter_rejected", "data": data}})
        event({"vacancy": {"name": "DevOps", "employer": {"name": "Example"}},
               "reason": LETTER_LEVEL_REJECTED, "text": "Готов на миддл."})
        logged = [call.args[0] for call in app.append_log.call_args_list]
        self.assertIn("Вакансия пропущена", logged[0])
        self.assertEqual(logged[1:], ["Вакансия: DevOps — Example", "Отклонённое письмо: Готов на миддл."])
        app.append_log.reset_mock()
        event({"vacancy": {"name": "DevOps"}, "reason": "free text", "text": "x"})
        app.append_log.assert_not_called()

    def test_chat_wait_countdown_survives_float_overshoot(self):
        app = gui.Application.__new__(gui.Application)
        app.session = SimpleNamespace(run="r", cancelled=threading.Event())
        app.root = Mock(after=Mock(return_value="job-1"))
        app.say = Mock()
        # For this monotonic value (t + 60) - t is slightly above 60.
        with patch("time.monotonic", return_value=4070.309673198955):
            app.start_chat_wait(60)
        self.assertIn("осталось 60 с", app.say.call_args.args[0])

    def test_chat_follow_up_wait_shows_countdown_and_its_end(self):
        app = gui.Application.__new__(gui.Application)
        app.session = SimpleNamespace(run="r", cancelled=threading.Event())
        app.root = Mock(after=Mock(return_value="job-1"))
        app.append_log, app.say = Mock(), Mock()

        def event(name, data):
            app.handle_frame({"run": "r", "type": "event", "payload": {"name": name, "data": data}})
        event("chat_wait", 60)
        app.append_log.assert_called_with("Жду следующий вопрос робота в этом чате (до 60 с).")
        self.assertIn("осталось 60 с", app.say.call_args.args[0])
        app.root.after.assert_called_once_with(1000, app.tick_chat_wait)
        event("chat_wait_end", "timeout")
        app.root.after_cancel.assert_called_once_with("job-1")
        self.assertIsNone(app.chat_wait_until)
        app.append_log.assert_called_with(gui.CHAT_WAIT_ENDS["timeout"])
        app.say.assert_called_with(gui.CHAT_WAIT_ENDS["timeout"])
        app.tick_chat_wait()  # A late tick after the end must not restart the countdown.
        app.root.after.assert_called_once()
        for bad in (0, 3601, "60", True):
            event("chat_wait", bad)
        event("chat_wait_end", "unknown")
        app.root.after.assert_called_once()

    def test_network_failure_message_names_the_host_instead_of_the_action_hint(self):
        app = gui.Application.__new__(gui.Application)
        app.sending = False
        done = {"success": False, "cancelled": False, "unknown": False, "network": "auth.openai.com",
                "counts": {"attempts": 0, "http_success": 0, "captcha_rejections": 0}}
        message = app.finish_message({"done": done, "success": False, "cancelled": False,
                                      "forced": False}, "chatgpt_login")
        self.assertIn("Нет связи с сервером auth.openai.com", message)
        self.assertNotIn("Введи код", message)

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


class SearchAllChecks(unittest.TestCase):
    def test_empty_search_uses_hh_suggestions_and_passes_send_validation(self):
        import preview
        import send
        argv = preview.arguments("cfg", "r", "")
        self.assertFalse(any(item.startswith("--search=") for item in argv))
        self.assertIn("--search=Python", preview.arguments("cfg", "r", "Python"))
        with self.assertRaises(SystemExit):
            preview.arguments("cfg", "", "Python")
        with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)):
            gui_profile.initialize()
            gui_profile.set_model("offline-model")
            profile = gui_profile.profile_dir()
            send_argv = send.arguments(str(profile), "generic-resume", "")
            with patch.dict(os.environ, gui_worker.environment()):
                args = send.validate(send_argv, profile=profile)
            self.assertIsNone(args.text)  # HH 2.3.7 then reads /resumes/{id}/similar_vacancies.

    def test_upstream_reads_similar_vacancies_without_text(self):
        from hh_applicant_tool.operations.apply_vacancies import Operation
        operation = Operation.__new__(Operation)
        operation.text, operation.total_pages = None, 1
        operation._get_search_params = lambda _page: {}
        client = Mock()
        client.get.return_value = {"items": [], "found": 0, "pages": 0}
        with patch.object(Operation, "api_client", property(lambda _self: client)):
            list(operation._get_vacancies("resume-1"))
        self.assertEqual(client.get.call_args.args[0], "/resumes/resume-1/similar_vacancies")

    def test_empty_search_is_stored_validated_and_accepted_by_worker(self):
        with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)):
            gui_profile.save_search("r1", "")
            self.assertEqual(gui_profile.read_searches(), {"r1": ""})
        with self.assertRaises(ValueError):
            gui_profile.validate_searches({"r": "  "})
        for action in ("preview", "apply"):
            settings = gui_controller.action_settings(action, resume="r", search="")
            gui_worker.validate_start(frame(uuid4().hex, "start", {"action": action, "settings": settings}))
            settings = gui_controller.action_settings(action, resume="r", search="   ")
            with self.assertRaises(ProtocolError):
                gui_worker.validate_start(frame(uuid4().hex, "start", {"action": action, "settings": settings}))

    def test_round_robin_turns_empty_search_into_suggestions(self):
        from send_all import apply_round_robin
        tool = Mock()
        tool.get_resumes.return_value = [{"id": "r1", "title": "r1", "status": {"id": "published"}}]
        tool.get_openai_settings.return_value = {}
        operation = SimpleNamespace(tool=tool, text="stale")
        seen = []

        def vacancies(op, resume_id):
            seen.append(op.text)  # Runs on first next(), after the loop set the search.
            yield from ()
        with patch("send_all.selected_resume_context", return_value=nullcontext()), redirect_stdout(StringIO()):
            apply_round_robin(operation, "r1", {"r1": ""}, vacancies)
        self.assertEqual(seen, [None])


class LetterStyleChecks(unittest.TestCase):
    def test_prompt_validation_explains_mistakes(self):
        gui_profile.validate_prompt("Пиши коротко для %(vacancy_name)s, скидка 100%%.")
        for bad, hint in (("", "пуст"), ("%(unknown)s", "Неизвестная подстановка"), ("100% честно", "%%"),
                          ("x" * 8001, "длинный")):
            with self.assertRaises(ValueError) as caught:
                gui_profile.validate_prompt(bad)
            self.assertIn(hint, str(caught.exception))

    def test_set_prompt_replaces_single_and_multiline_values_and_keeps_other_settings(self):
        import tomllib
        templates = ('[general]\ncover_letter_prompt = "old"\nother = 1\n\n[openai.default]\nmodel = "m"\n',
                     '[general]\ncover_letter_prompt = """\nold\nmulti\n"""\nother = 1\n\n[openai.default]\nmodel = "m"\n',
                     '[openai.default]\nmodel = "m"\n\n[general]\nother = 1\n')
        for template in templates:
            with self.subTest(template=template[:40]), tempfile.TemporaryDirectory() as temp, \
                    patch("pathlib.Path.home", return_value=Path(temp)):
                path = gui_profile.profile_dir() / "config.toml"
                path.parent.mkdir(parents=True)
                path.write_text(template, encoding="utf-8")
                prompt = 'Привет "ты"\nстрока два, %(employer_name)s'
                gui_profile.set_prompt(prompt)
                config = tomllib.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(config["general"]["cover_letter_prompt"], prompt)
                self.assertEqual(config["general"]["other"], 1)
                self.assertEqual(config["openai"]["default"]["model"], "m")
                self.assertEqual(gui_profile.read_prompt(), prompt)
                with self.assertRaises(ValueError):
                    gui_profile.set_prompt("%(nope)s")
                self.assertEqual(gui_profile.read_prompt(), prompt)  # A rejected style changes nothing.

    def test_default_prompt_comes_from_the_shipped_template(self):
        self.assertTrue(gui_profile.default_prompt())
        gui_profile.validate_prompt(gui_profile.default_prompt())

    def test_real_tk_search_mode_and_letter_style_dialog(self):
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
                app.resumes = [{"id": "r1", "title": "Resume"}]
                app.resume_box.configure(values=["Resume"])
                app.resume_box.current(0)
                app.select_resume()
                self.assertFalse(app.progress_state().search)
                app.search_mode.set("all")
                app.select_search_mode()
                self.assertTrue(app.progress_state().search)
                self.assertEqual(str(app.search_entry["state"]), "disabled")
                self.assertEqual(gui_profile.read_searches(), {"r1": ""})
                self.assertTrue(app.save())
                self.assertEqual(app.search_value(), "")
                app.search_mode.set("query")
                app.select_resume()  # A saved "" restores the "all" mode.
                self.assertEqual(app.search_mode.get(), "all")

                app.edit_letter_style()
                editor, save_button, error, check = app.style_controls
                # Ctrl+A, Ctrl+C, Ctrl+V by Windows key codes, which the Russian layout keeps.
                editor.delete("1.0", "end")
                editor.insert("1.0", "Привет")
                # The dialog of a withdrawn root never gets focus, so Tk would drop
                # generated key events; call the bound handler with the event it reads.
                self.assertTrue(editor.bind("<Control-KeyPress>"))
                press = lambda code: editor.clipboard_keys(SimpleNamespace(keycode=code))
                press(65)
                press(67)
                self.assertEqual(root.clipboard_get(), "Привет")
                root.clipboard_clear()
                root.clipboard_append("%(broken)s")
                press(86)  # Paste replaces the selection.
                self.assertEqual(editor.get("1.0", "end-1c"), "%(broken)s")
                self.assertEqual(str(save_button["state"]), "disabled")  # Paste re-validates at once.
                editor.delete("1.0", "end")
                editor.insert("1.0", "%(broken)s")
                check()
                self.assertEqual(str(save_button["state"]), "disabled")
                self.assertIn("Неизвестная подстановка", error["text"])
                editor.delete("1.0", "end")
                editor.insert("1.0", "Коротко и на «ты» для %(vacancy_name)s.")
                check()
                self.assertEqual(str(save_button["state"]), "normal")
                save_button.invoke()
                self.assertEqual(gui_profile.read_prompt(), "Коротко и на «ты» для %(vacancy_name)s.")
                self.assertIsNone(app.style_window)
        finally:
            root.destroy()


class ProviderChecks(unittest.TestCase):
    API = "https://api.example.com/v1"

    def tearDown(self):
        import ai_provider
        ai_provider.activate(ai_provider.Provider(ai_provider.CHATMOCK, ai_provider.CHATMOCK_URL))

    def test_base_url_decides_kind_and_rejects_unsafe_addresses(self):
        import ai_provider
        self.assertEqual(ai_provider.parse("http://127.0.0.1:8000/v1/").kind, ai_provider.CHATMOCK)
        self.assertEqual(ai_provider.parse(self.API).kind, ai_provider.OPENAI)
        self.assertEqual(ai_provider.parse("http://localhost:11434/v1").kind, ai_provider.LOCAL)
        for bad in ("", "http://api.example.com/v1", "https://user:pw@api.example.com/v1",
                    "https://api.example.com/v1?x=1", "https://api.hh.ru/v1", "https://192.168.1.5/v1",
                    "http://127.0.0.1/v1", "http://127.0.0.1:8000/other", "ftp://api.example.com"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ai_provider.parse(bad)
        with self.assertRaises(ValueError):
            ai_provider.parse(self.API, allow_external=False)  # CLI profiles keep ChatMock.

    def test_only_active_provider_model_routes_pass_the_guards(self):
        import ai_provider
        import preview_hh
        import reply_chats
        ai_provider.activate(ai_provider.parse(self.API))
        allowed = (("GET", self.API + "/models"), ("POST", self.API + "/chat/completions"))
        blocked = (("POST", self.API + "/models"), ("GET", "https://api.example.com/v2/models"),
                   ("POST", "https://evil.example.com/v1/chat/completions"),
                   ("POST", self.API + "/chat/completions?x=1"),
                   ("POST", "http://127.0.0.1:8000/v1/chat/completions"),  # ChatMock is not active now.
                   ("GET", "http://127.0.0.1:8000/health"))
        for method, url in allowed:
            with self.subTest(url=url):
                self.assertTrue(preview_hh.allowed_request(method, url))
                self.assertTrue(reply_chats.allowed_read(requests.Request(method, url).prepare()))
        for method, url in blocked:
            with self.subTest(url=url):
                self.assertFalse(preview_hh.allowed_request(method, url))
                self.assertFalse(reply_chats.allowed_read(requests.Request(method, url).prepare()))
        self.assertTrue(preview_hh.allowed_request("GET", "https://api.hh.ru/resumes/mine"))
        self.assertEqual(reply_chats.request_target(requests.Request("POST", self.API + "/chat/completions").prepare()),
                         "OpenAI-совместимый API: completions")

    def test_key_is_added_only_for_the_provider_base_url(self):
        import ai_provider
        provider = ai_provider.parse(self.API)
        original = lambda _tool, section=None: {"base_url": self.API if section != "other" else "https://x.example/v1"}
        wrapped = ai_provider.keyed_config(original, provider, "sk-test")
        self.assertEqual(wrapped(None)["api_key"], "sk-test")
        self.assertNotIn("api_key", wrapped(None, "other"))
        self.assertNotIn("api_key", ai_provider.keyed_config(original, provider, None)(None))

    def test_send_validation_accepts_provider_only_in_the_gui_profile(self):
        import send
        with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)):
            gui_profile.initialize()
            gui_profile.set_provider(self.API)
            gui_profile.set_model("offline-model")
            profile = gui_profile.profile_dir()
            argv = send.arguments(str(profile), "r", "Python")
            with patch.dict(os.environ, gui_worker.environment()):
                send.validate(argv, profile=profile)
            import ai_provider
            self.assertEqual(ai_provider.active().kind, ai_provider.OPENAI)
            self.assertNotIn("api_key", (profile / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(gui_profile.read_provider().base_url, self.API)

    def test_switching_provider_clears_the_old_model(self):
        import tomllib
        with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)):
            gui_profile.set_model("gpt-5")
            gui_profile.set_provider("http://127.0.0.1:11434/v1")
            config = tomllib.loads((gui_profile.profile_dir() / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(config["openai"]["default"]["model"], "CHOOSE_AFTER_LOGIN")
            self.assertEqual(config["openai"]["default"]["base_url"], "http://127.0.0.1:11434/v1")

    def test_worker_lists_models_with_key_in_memory_only(self):
        import ai_provider
        ai_provider.activate(ai_provider.parse(self.API))
        channel = FakeChannel()
        response = Mock(status_code=200)
        response.json.return_value = {"data": [{"id": "model-a"}, {"id": 5}]}
        with patch("ai_provider.read_key", return_value="sk-secret"), \
             patch("requests.get", return_value=response) as get:
            self.assertEqual(gui_worker.dispatch("models", {}, channel), 0)
        self.assertEqual(get.call_args.args[0], self.API + "/models")
        self.assertEqual(get.call_args.kwargs["headers"], {"Authorization": "Bearer sk-secret"})
        self.assertEqual(channel.events, [("models", ["model-a"])])
        self.assertNotIn("sk-secret", json.dumps(dict(os.environ)))

    def test_worker_refuses_external_provider_without_key(self):
        reader, writer = Reader(), Writer()
        reader.put(frame(uuid4().hex, "start", {"action": "models", "settings": {}}))
        with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)), \
             patch("ai_provider.read_key", return_value=None), patch("gui_worker.dispatch") as dispatch:
            gui_profile.set_provider(self.API)
            self.assertEqual(gui_worker.worker_main(reader, writer), 1)
            dispatch.assert_not_called()
        reader.put("")
        self.assertFalse(writer.items[-1]["payload"]["success"])

    def test_chatmock_gateway_starts_only_for_the_chatmock_provider(self):
        with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)):
            self.assertTrue(gui_runtime.provider_uses_gateway())  # No profile yet: ChatMock.
            gui_profile.set_provider(self.API)
            self.assertFalse(gui_runtime.provider_uses_gateway())
            session, process, _, gateway = LifecycleChecks().make_session("models")
            process.stdout = StringIO(encode(frame(session.run, "done", {"success": True, "cancelled": False,
                "unknown": False, "counts": {"attempts": 0, "http_success": 0, "captcha_rejections": 0}})))
            run_session(session)
            gateway.start.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows Credential Manager")
    def test_real_credential_manager_round_trip_with_disposable_target(self):
        import ai_provider
        provider = ai_provider.parse(f"https://test-{uuid4().hex}.invalid/v1")
        try:
            self.assertIsNone(ai_provider.read_key(provider))
            ai_provider.save_key(provider, "  sk-round-trip  ")
            self.assertEqual(ai_provider.read_key(provider), "sk-round-trip")
            self.assertTrue(ai_provider.ready(provider))
        finally:
            ai_provider.delete_key(provider)
        self.assertIsNone(ai_provider.read_key(provider))
        ai_provider.delete_key(provider)  # Deleting a missing key is not an error.
        with self.assertRaises(ValueError):
            ai_provider.save_key(provider, "sk with space")

    def test_real_tk_provider_dialog_saves_url_and_key_but_never_writes_the_key_to_files(self):
        import tkinter as tk
        from tkinter import ttk
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest("Tk display unavailable: " + type(error).__name__)
        root.withdraw()
        store = {}
        try:
            with tempfile.TemporaryDirectory() as temp, patch("pathlib.Path.home", return_value=Path(temp)), \
                 patch("ai_provider.save_key", lambda provider, key, api=None: store.update({provider.base_url: key.strip()})), \
                 patch("ai_provider.read_key", lambda provider, api=None: store.get(provider.base_url)):
                app = gui.Application(root, tk, ttk, Mock())
                self.assertEqual(str(app.provider_state["text"]), "ChatGPT через ChatMock")
                app.edit_provider()
                controls = app.provider_controls
                controls["kind"].set("openai")
                controls["update"]()
                controls["address"].set("http://api.example.com/v1")
                controls["save"]()
                self.assertIn("https://", controls["error"]["text"])
                controls["address"].set(self.API)
                controls["save"]()
                self.assertIn("ключ", controls["error"]["text"])  # A new API needs a key.
                controls["key"].set("sk-from-dialog")
                controls["save"]()
                self.assertIsNone(app.provider_window)
                self.assertEqual(store, {self.API: "sk-from-dialog"})
                self.assertEqual(app.provider.base_url, self.API)
                self.assertTrue(app.ai_ready)
                self.assertFalse(app.chatgpt_button.winfo_manager())  # ChatGPT login is hidden.
                self.assertIn("ключ сохранён", str(app.provider_state["text"]))
                self.assertEqual(app.model.get(), "")
                text = (gui_profile.profile_dir() / "config.toml").read_text(encoding="utf-8")
                self.assertNotIn("sk-from-dialog", text)
                for path in Path(temp).rglob("*"):
                    if path.is_file():
                        self.assertNotIn("sk-from-dialog", path.read_text(encoding="utf-8", errors="ignore"))
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


class LegacyProfileMigrationTest(unittest.TestCase):
    """Pre-rename PocketNode profiles move once to the new names; nothing is merged or lost."""

    def test_old_folders_move_to_new_names(self):
        import profile_migration
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            for parent, old, _ in profile_migration.FOLDERS:
                (home / parent / old).mkdir(parents=True)
                (home / parent / old / "marker.txt").write_text(parent + old, encoding="utf-8")
            moved = profile_migration.migrate_folders(home)
            self.assertEqual(len(moved), 4)
            for parent, old, new in profile_migration.FOLDERS:
                self.assertFalse((home / parent / old).exists())
                self.assertEqual((home / parent / new / "marker.txt").read_text(encoding="utf-8"), parent + old)
            self.assertEqual(profile_migration.migrate_folders(home), [])

    def test_existing_new_folder_wins_and_old_is_kept(self):
        import profile_migration
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / ".config/pocketnode-hh-gui").mkdir(parents=True)
            (home / ".config/hh-applicant-tool-gui").mkdir(parents=True)
            self.assertEqual(profile_migration.migrate_folders(home), [])
            self.assertTrue((home / ".config/pocketnode-hh-gui").is_dir())

    def test_failed_rename_stops_with_plain_message(self):
        import profile_migration
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / ".config/pocketnode-hh").mkdir(parents=True)
            with patch.object(profile_migration.os, "rename", side_effect=PermissionError("busy")):
                with self.assertRaisesRegex(profile_migration.MigrationError, "Закройте старую версию"):
                    profile_migration.migrate_folders(home)

    def test_gui_profile_uses_new_names(self):
        self.assertEqual(gui_profile.profile_dir().name, "hh-applicant-tool-gui")
        self.assertEqual(gui_profile.state_dir().name, "hh-applicant-tool-gui")
        self.assertEqual(gui_profile.expected_profile().name, "hh-applicant-tool-cli")


if __name__ == "__main__":
    unittest.main()
