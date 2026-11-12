"""Isolated main-thread HH/ChatMock adapters, invoked only by gui --worker."""

from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from functools import partial
from importlib.metadata import version
import os
import sys
import tomllib
from types import SimpleNamespace
from unittest.mock import patch

from gui_profile import initialize, profile_dir, search_path, state_dir
from gui_protocol import ACTIONS, Cancelled, ProtocolError, ReviewAdapter, WorkerChannel, decode


def environment():
    return {"CONFIG_DIR": str(profile_dir()), "HH_PROFILE_ID": ".",
            "HH_APPLICANT_TOOL_SEARCH_FILE": str(search_path()),
            "CHATGPT_LOCAL_HOME": str(state_dir() / "chatmock"),
            "CODEX_HOME": str(state_dir() / "chatmock")}


@contextmanager
def hh_parser_modules():
    """HH 2.3.7 normally scans loose .py files, which frozen builds may not have."""
    if version("hh-applicant-tool") != "2.3.7":
        raise RuntimeError("Unsupported HH adapter version")
    from hh_applicant_tool import tool
    # Explicit imports make these dependencies discoverable to the packager too.
    from hh_applicant_tool.operations import apply_vacancies, authorize, reply_chats
    modules = (apply_vacancies, authorize, reply_chats)
    names = tuple((None, module.__name__.rsplit(".", 1)[-1], False) for module in modules)
    with patch.object(tool, "iter_modules", lambda _paths: iter(names)):
        yield


def validate_start(data):
    if data["type"] != "start" or set(data["payload"]) != {"action", "settings"}:
        raise ProtocolError("Expected start")
    action, settings = data["payload"]["action"], data["payload"]["settings"]
    if action not in ACTIONS or not isinstance(settings, dict):
        raise ProtocolError("Unknown action")
    expected = {"resume", "search", "all"} if action in {"preview", "apply", "auto"} else set()
    if action in {"apply", "auto"}:
        expected.add("no_letter")
    if action == "auto":
        expected |= {"limit", "min_pause", "max_pause", "consent"}
    if set(settings) != expected:
        raise ProtocolError("Unexpected settings")
    if expected:
        if (not isinstance(settings["resume"], str) or not settings["resume"]
                or len(settings["resume"]) > 200 or not isinstance(settings["search"], str)
                or (settings["search"] and not settings["search"].strip()) or len(settings["search"]) > 4000
                or type(settings["all"]) is not bool or (action == "preview" and settings["all"])
                or type(settings.get("no_letter", False)) is not bool):
            raise ProtocolError("Invalid resume/search")
    if action == "auto":
        from auto_send import validate_settings
        validate_settings(settings["limit"], settings["min_pause"], settings["max_pause"])
        if settings["consent"] is not True:
            raise ProtocolError("Automatic sending requires explicit consent")
    return action, settings


def hh_login(channel):
    if version("hh-applicant-tool") != "2.3.7":
        raise RuntimeError("Unsupported HH adapter version")
    from hh_applicant_tool.main import main
    from hh_applicant_tool.operations import authorize

    # Only this pinned module's input seam, not builtins.input; secrets travel
    # exclusively in the private pipe and are never put in argv, env, or logs.
    fields = iter(("Email или телефон от аккаунта HH", "Код из SMS или письма"))

    def credential(_prompt):
        result = channel.ask({"kind": "credential", "context": next(fields),
                              "text": "", "options": []})
        if (set(result) != {"action", "text"} or result["action"] != "submit"
                or not isinstance(result["text"], str) or not result["text"].strip()
                or len(result["text"]) > 500):
            raise Cancelled
        return result["text"]

    with patch.object(authorize, "input", credential, create=True), \
         patch.object(authorize, "MAX_RETRIES", 2), \
         patch.object(authorize, "time", SimpleNamespace(sleep=channel.sleep, time=__import__("time").time)):
        # One captcha challenge; upstream increments before testing MAX_RETRIES.
        return main(["--config-dir", str(profile_dir()), "--profile-id", ".",
                     "--no-handle-captcha", "--manual-captcha", "authorize"])


def chatgpt_login(channel):
    if version("chatmock") != "1.40":
        raise RuntimeError("Unsupported ChatMock adapter version")
    from gui_chatmock_auth import install
    guard = install()
    from chatmock import oauth
    from chatmock.config import CLIENT_ID_DEFAULT
    sender = oauth.requests.post
    issuer = oauth.DEFAULT_ISSUER.rstrip("/")

    def post(url, *args, **kwargs):
        channel.check()
        response = sender(url, *args, **kwargs)
        channel.check()
        if url == issuer + "/api/accounts/deviceauth/usercode" and response.status_code < 400:
            data = response.json()
            code = data.get("user_code") or data.get("usercode")
            if not isinstance(code, str) or not code or len(code) > 128:
                raise ProtocolError("Invalid device code")
            channel.event("device", {"url": issuer + "/codex/device", "code": code})
        return response

    import time
    with patch.object(oauth.requests, "post", post), \
         patch.object(oauth, "time", SimpleNamespace(sleep=channel.sleep, monotonic=time.monotonic)):
        if not oauth.run_device_code_login(CLIENT_ID_DEFAULT, verbose=False):
            raise RuntimeError("ChatGPT login failed")
        guard.require_auth()  # A successful return must have persisted valid GUI-only auth.


def dispatch(action, settings, channel):
    """Every apply/reply route must use the local gate, never upstream directly."""
    review = ReviewAdapter(channel)
    profile = profile_dir()
    if action == "hh_login":
        return hh_login(channel)
    if action == "chatgpt_login":
        return chatgpt_login(channel)
    if action == "models":
        import requests
        import ai_provider
        provider = ai_provider.active()
        key = ai_provider.read_key(provider) if provider.needs_key else None
        response = requests.get(provider.base_url + "/models", timeout=(5, 20),
                                headers={"Authorization": "Bearer " + key} if key else {})
        response.raise_for_status()
        models = ai_provider.model_ids(provider, response.json()["data"])
        channel.event("models", models)
        return 0
    if action == "resumes":
        import list_resumes
        return list_resumes.run(profile=profile, observer=channel.event)
    if action == "chats":
        import reply_chats
        return reply_chats.run(reply_chats.arguments(str(profile)), profile=profile,
                               review=review, diagnostics_directory=state_dir() / "run", observer=channel.event)
    if action == "preview":
        import preview
        return preview.run(preview.arguments(str(profile), settings["resume"], settings["search"]),
                           profile=profile, observer=channel.event)
    import send
    no_letter = settings["no_letter"]
    argv = (send.all_resume_arguments(str(profile), None, generic=True, no_letter=no_letter) if settings["all"] else
            send.arguments(str(profile), settings["resume"], settings["search"], no_letter=no_letter))
    if action == "apply":
        factory = partial(send.ApprovalGate, review=review, sleep=channel.sleep)
    elif action == "auto":
        from auto_send import AutoGate
        factory = partial(AutoGate, max_responses=settings["limit"], min_pause=settings["min_pause"],
                          max_pause=settings["max_pause"], sleep=channel.sleep)
    else:
        raise ProtocolError("Unknown send action")
    return send.run(argv, all_resumes=settings["all"], gate_factory=factory, profile=profile,
                    observer=channel.event)


def own_stop_reason(error):
    """Return the SystemExit text only when a module of this app raised it."""
    if not isinstance(error.code, str) or error.__traceback__ is None:
        return None
    tb = error.__traceback__
    while tb.tb_next is not None:
        tb = tb.tb_next
    from pathlib import Path
    try:
        origin = Path(tb.tb_frame.f_code.co_filename).resolve().parent
    except (OSError, ValueError):
        return None
    return error.code[:500] if origin == Path(__file__).resolve().parent else None


def worker_main(reader=None, writer=None):
    reader, writer = reader or sys.stdin, writer or sys.stdout
    channel = None
    counts = {"attempts": 0, "http_success": 0, "captcha_rejections": 0}
    success, cancelled = False, False
    network = {"host": None}  # Host of the last unanswered request; a later answer clears it.
    try:
        start = decode(reader.readline(1024 * 1024 + 1))
        action, settings = validate_start(start)
        channel = WorkerChannel(reader, writer, start["run"])
        channel.listen()
        import manual_captcha
        # One worker process serves one run, so a class-level hook is safe here.
        manual_captcha.ManualCaptcha.on_prompt = lambda: channel.event("captcha", True)
        import requests
        original = requests.sessions.Session.send

        def guarded_transport(session, request, **kwargs):
            channel.check()  # Covers reads, redirects, captcha and the final POST.
            from urllib.parse import urlsplit
            path = urlsplit(request.url).path
            sending = request.method == "POST" and path in {
                "/negotiations", "/chatik/api/send", "/applicant/vacancy_response/popup"}
            if sending:
                counts["attempts"] += 1
                channel.event("sending", True)
            if kwargs.get("timeout") is None:
                kwargs["timeout"] = (10, 60)
            try:
                try:
                    response = original(session, request, **kwargs)
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                    network["host"] = (urlsplit(request.url).hostname or "").lower() or None
                    raise
                network["host"] = None
                if sending and 200 <= response.status_code < 300:
                    counts["http_success"] += 1
                elif sending and path == "/negotiations" and response.status_code == 403:
                    from manual_captcha import captcha_url
                    if captcha_url(response):
                        counts["captcha_rejections"] += 1
                elif sending and path == "/applicant/vacancy_response/popup":
                    from manual_captcha import form_captcha_state
                    if form_captcha_state(response):
                        counts["captcha_rejections"] += 1
                return response
            finally:
                if sending:
                    channel.event("sending", False)
                    # The GUI counts what HH accepted, never the attempts (#8).
                    channel.event("counts", dict(counts))

        # Suppress all upstream stdout/stderr, including credentials, prompts,
        # traceback/URL logging. Structured events are the only UI output.
        with open(os.devnull, "w", encoding="utf-8") as sink, ExitStack() as stack:
            stack.enter_context(redirect_stdout(sink))
            stack.enter_context(redirect_stderr(sink))
            stack.enter_context(patch.dict(os.environ, environment()))
            stack.enter_context(patch.object(requests.sessions.Session, "send", guarded_transport))
            if action not in {"chatgpt_login", "models"}:
                stack.enter_context(hh_parser_modules())
            # No upstream file handlers (HH normally writes verbose hh.log).
            import logging
            previous = logging.root.manager.disable
            logging.disable(logging.CRITICAL)
            stack.callback(logging.disable, previous)
            initialize()
            config = tomllib.loads((profile_dir() / "config.toml").read_text(encoding="utf-8"))
            if action in {"models", "preview", "apply", "auto", "chats"}:
                import ai_provider
                from hh_applicant_tool.tool import HHApplicantTool
                provider = ai_provider.from_config(config)
                ai_provider.activate(provider)
                key = ai_provider.read_key(provider) if provider.needs_key else None
                # #9: applications without a letter never call the model.
                if provider.needs_key and not key and not settings.get("no_letter"):
                    raise ProtocolError("AI provider key is missing")
                # The key stays in this process's memory: never argv, environment or files.
                stack.enter_context(patch.object(HHApplicantTool, "get_openai_config", ai_provider.keyed_config(
                    HHApplicantTool.get_openai_config, provider, key)))
                if provider.kind != ai_provider.CHATMOCK:
                    from hh_applicant_tool.ai.openai import OpenAIClient
                    stack.enter_context(patch.object(OpenAIClient, "_request", ai_provider.tuned_request(
                        OpenAIClient._request, provider)))
            if action in {"preview", "apply", "auto", "chats"} and not settings.get("no_letter"):
                model = config.get("openai", {}).get("default", {}).get("model")
                if not isinstance(model, str) or not model.strip() or model == "CHOOSE_AFTER_LOGIN":
                    raise ProtocolError("Choose an account model before generation")
            if action in {"apply", "auto"}:
                import send

                def resolved(kind):
                    # A POST without an answer, settled by a status check in HH.
                    if kind == "sent":
                        counts["http_success"] += 1
                    elif kind == "absent" and counts["attempts"] > 0:
                        counts["attempts"] -= 1  # It provably created nothing in HH.
                    channel.event("counts", dict(counts))
                send.ApprovalGate.on_send_resolved = staticmethod(resolved)
            channel.check()
            result = dispatch(action, settings, channel)
            channel.check()
            success = result in (0, None)
    except Cancelled:
        cancelled = True
    except SystemExit as error:
        # Our own guards stop with fixed, sanitized texts; upstream exits stay hidden.
        reason = own_stop_reason(error)
        if channel is not None and reason:
            try:
                channel.event("stopped", reason)
            except (OSError, ValueError):
                pass
    except Exception:
        pass  # Never serialize an upstream exception or credential-bearing URL.
    finally:
        if channel is not None:
            from gui_protocol import valid_network_host
            done = {"success": success, "cancelled": cancelled,
                    "unknown": counts["attempts"] - counts["captcha_rejections"] != counts["http_success"],
                    "counts": counts}
            if not success and not cancelled and valid_network_host(network["host"]):
                done["network"] = network["host"]
            try:
                channel.emit("done", done)
            except (OSError, ValueError):
                pass
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(worker_main())
