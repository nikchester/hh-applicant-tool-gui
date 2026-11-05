"""Offline ChatMock tests. No login, inference, or HH requests."""

import contextlib
import io
import json
import socket
import unittest
from unittest.mock import Mock, call, patch

from flask import Response, g, jsonify
import requests
from urllib3.exceptions import ProtocolError, ReadTimeoutError
from chatmock.config import CHATGPT_RESPONSES_URL

import chatmock_server
from chatmock_server import (begin_request_diagnostic, create_diagnostic_app, exception_details,
                             log_error_response, log_runtime, main, safe_error_message,
                             trace_upstream_send, upstream_diagnostics)


class FakeUpstream:
    status_code = 200
    headers = {}

    def iter_lines(self, **kwargs):
        events = [
            {"type": "response.output_text.delta", "delta": "Test letter."},
            {"type": "response.completed", "response": {"id": "test-response"}},
        ]
        for event in events:
            yield ("data: " + json.dumps(event)).encode()

    def close(self):
        pass


class FailedUpstream(FakeUpstream):
    def iter_lines(self, **kwargs):
        yield ("data: " + json.dumps({
            "type": "response.failed",
            "response": {"error": {"message": "Model temporarily unavailable."}},
        })).encode()


class FakeClock:
    """Replaces only chatmock_server's time module: per-attempt elapsed is scripted."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def attempts():
    return chatmock_server.CONNECT_ATTEMPTS


def backoffs():
    return list(chatmock_server.CONNECT_BACKOFFS)


def handshake_stall():
    # urllib3 2.x reports a TLS handshake stall as ReadTimeoutError wrapped by requests.
    return requests.ReadTimeout(ReadTimeoutError(None, "PRIVATE_URL", "PRIVATE_ERROR"))


def timed_sender(clock, outcomes):
    """Each outcome is (elapsed seconds, exception or response) for one attempt."""
    pending = list(outcomes)

    def send(*args, **kwargs):
        elapsed, result = pending.pop(0)
        clock.now += elapsed
        if isinstance(result, BaseException):
            raise result
        return result

    return Mock(side_effect=send)


class SmokeTests(unittest.TestCase):
    def setUp(self):
        network = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Network access forbidden in offline tests"),
        )
        network.start()
        self.addCleanup(network.stop)
        credentials = patch(
            "chatmock.utils.read_auth_file",
            side_effect=AssertionError("Credential reads forbidden in offline tests"),
        )
        credentials.start()
        self.addCleanup(credentials.stop)
        self.app = create_diagnostic_app(
            model_sync=False,
            reasoning_summary="none",
            reasoning_compat="legacy",
            default_web_search=False,
        )
        self.client = self.app.test_client()
        self.payload = {
            "model": "gpt-5.4-mini",
            "messages": [
                {"role": "system", "content": "Return only a short letter."},
                {"role": "user", "content": "Use only confirmed facts."},
            ],
            "temperature": 0.3,
            "max_completion_tokens": 500,
            "stream": False,
        }

    def test_health(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"status": "ok"})

    def test_invalid_json(self):
        response = self.client.post(
            "/v1/chat/completions", data="{broken", content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_missing_login(self):
        with patch("chatmock.upstream.get_effective_chatgpt_auth", return_value=(None, None)):
            response = self.client.post("/v1/chat/completions", json=self.payload)
        self.assertEqual(response.status_code, 401)

    def test_hh_payload_and_response_contract(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output), patch(
            "chatmock.routes_openai.start_upstream_request",
            return_value=(FakeUpstream(), None),
        ) as upstream:
            response = self.client.post("/v1/chat/completions", json=self.payload)
        self.assertEqual(response.status_code, 200)
        message = response.json["choices"][0]["message"]
        self.assertEqual(message["content"], "Test letter.")
        self.assertFalse(message.get("tool_calls"))
        self.assertFalse(upstream.call_args.kwargs["tools"])
        self.assertEqual(output.getvalue(), "")

    def test_failed_generation_logs_only_error_message_and_preserves_response(self):
        self.payload["messages"][1]["content"] = "PRIVATE_CHAT_HISTORY"
        output = io.StringIO()
        with contextlib.redirect_stderr(output), patch(
            "chatmock.routes_openai.start_upstream_request",
            return_value=(FailedUpstream(), None),
        ) as upstream:
            response = self.client.post(
                "/v1/chat/completions?secret=PRIVATE_QUERY", json=self.payload,
                headers={"Authorization": "Bearer PRIVATE_HEADER"},
            )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json, {"error": {"message": "Model temporarily unavailable."}})
        upstream.assert_called_once()
        line = output.getvalue().strip()
        self.assertTrue(line.startswith("[ChatMock error] "))
        diagnostic = json.loads(line.removeprefix("[ChatMock error] "))
        self.assertRegex(diagnostic.pop("request_id"), r"^[0-9a-f]{32}$")
        self.assertEqual(diagnostic, {
            "method": "POST", "path": "/v1/chat/completions", "status": 502,
            "error.message": "Model temporarily unavailable.",
        })
        for private in ("PRIVATE_CHAT_HISTORY", "PRIVATE_QUERY", "PRIVATE_HEADER"):
            self.assertNotIn(private, line)

    def test_diagnostic_ignores_other_fields_and_handles_missing_message(self):
        bodies = [
            {"error": {"message": "Failure.\n\x1b[31m", "detail": "PRIVATE_DETAIL"},
             "input": "PRIVATE_INPUT", "token": "PRIVATE_TOKEN"},
            {"error": {"message": {"private": "PRIVATE_NESTED"}}},
            {"error": "PRIVATE_ERROR"},
            ["PRIVATE_LIST"],
        ]
        for body in bodies:
            with self.subTest(body=body), self.app.test_request_context(
                "/v1/responses", method="POST"
            ):
                response = jsonify(body)
                response.status_code = 502
                output = io.StringIO()
                with contextlib.redirect_stderr(output):
                    self.assertIs(log_error_response(response), response)
                self.assertEqual(response.json, body)
                line = output.getvalue()
                self.assertEqual(len(line.splitlines()), 1)
                self.assertNotIn("PRIVATE_", line)
                self.assertNotIn("\x1b", line)

    def test_non_json_error_does_not_dump_response_body(self):
        with self.app.test_request_context("/v1/chat/completions", method="POST"):
            response = Response("<html>PRIVATE_BODY</html>", status=502, mimetype="text/html")
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                self.assertIs(log_error_response(response), response)
            self.assertIn("error.message unavailable", output.getvalue())
            self.assertNotIn("PRIVATE_BODY", output.getvalue())

    def test_unrelated_and_streamed_responses_are_not_read_or_logged(self):
        for path, response in (
            ("/health", Response("PRIVATE_BODY", status=500)),
            ("/v1/chat/completions", Response(iter([b"PRIVATE_STREAM"]), status=502)),
        ):
            with self.subTest(path=path), self.app.test_request_context(path, method="POST"):
                with patch.object(response, "get_json", side_effect=AssertionError("Do not read")):
                    output = io.StringIO()
                    with contextlib.redirect_stderr(output):
                        self.assertIs(log_error_response(response), response)
                    self.assertEqual(output.getvalue(), "")

    def test_diagnostic_masks_common_credentials_and_limits_message(self):
        message = (
            'Bearer bearer-secret sk-test-secret eyJfake.payload.signature '
            'access_token="access-secret" refresh_token=refresh-secret '
            "id_token='id-secret' api_key=key-secret"
        )
        cleaned = safe_error_message(message)
        self.assertNotIn("secret", cleaned)
        self.assertNotIn("eyJfake", cleaned)
        self.assertIn("[REDACTED]", cleaned)
        self.assertEqual(safe_error_message("x" * 2100), "x" * 2000 + " [truncated]")

    def test_log_write_failure_does_not_change_response(self):
        with self.app.test_request_context("/v1/chat/completions", method="POST"):
            response = jsonify({"error": {"message": "Failure"}})
            response.status_code = 502
            with patch("chatmock_server.print", side_effect=OSError("disk unavailable")):
                self.assertIs(log_error_response(response), response)
            self.assertEqual(response.status_code, 502)

    def test_entry_point_installs_factory_only_for_owned_server(self):
        from chatmock import cli
        original = cli.create_app
        original_send = requests.sessions.Session.send

        def fake_main():
            self.assertIs(cli.create_app, create_diagnostic_app)
            self.assertIsNot(requests.sessions.Session.send, original_send)

        with patch.object(cli, "main", side_effect=fake_main) as start, \
                contextlib.redirect_stderr(io.StringIO()):
            main()
        start.assert_called_once_with()
        self.assertIs(cli.create_app, original)
        self.assertIs(requests.sessions.Session.send, original_send)


class NetworkDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        network = patch("requests.adapters.HTTPAdapter.send",
                        side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.app = create_diagnostic_app(model_sync=False)
        self.request = requests.Request(
            "POST", "https://chatgpt.com/backend-api/codex/responses?private=PRIVATE_QUERY",
            json={"input": "PRIVATE_PROMPT"},
            headers={"Authorization": "Bearer PRIVATE_AUTH", "Cookie": "PRIVATE_COOKIE"},
        ).prepare()

    def records(self, output):
        return [json.loads(line.split("] ", 1)[1]) for line in output.getvalue().splitlines()]

    def response(self, body=b"PRIVATE_BODY\n"):
        response = requests.Response()
        response.status_code = 200
        response.url = self.request.url
        response.request = self.request
        response.raw = io.BytesIO(body)
        response.headers.update({
            "x-request-id": "req-test-123", "cf-ray": "abc123-FRA", "retry-after": "5",
            "Set-Cookie": "PRIVATE_RESPONSE_COOKIE", "Authorization": "PRIVATE_RESPONSE_AUTH",
        })
        return response

    def test_success_records_origin_proxy_headers_and_body_without_content(self):
        response = self.response()
        sender = Mock(return_value=response)
        output = io.StringIO()
        with self.app.test_request_context("/v1/chat/completions?private=PRIVATE_LOCAL_QUERY",
                                           method="POST"), contextlib.redirect_stderr(output):
            begin_request_diagnostic()
            result = trace_upstream_send(
                sender, None, self.request, stream=True, verify=True,
                proxies={"https": "http://PRIVATE_USER:PRIVATE_PASSWORD@proxy.example:8080/PRIVATE_PATH"},
            )
            self.assertIs(result, response)
            self.assertEqual(list(result.iter_lines()), [b"PRIVATE_BODY"])
            result.close()
        sender.assert_called_once()
        self.assertIs(sender.call_args.args[1], self.request)
        self.assertEqual(sender.call_args.kwargs["proxies"]["https"],
                         "http://PRIVATE_USER:PRIVATE_PASSWORD@proxy.example:8080/PRIVATE_PATH")
        records = self.records(output)
        self.assertEqual([r["event"] for r in records], [
            "request_started", "response_received", "body_read_complete",
        ])
        self.assertEqual(len({r["request_id"] for r in records}), 1)
        self.assertEqual(len({r["upstream_id"] for r in records}), 1)
        self.assertRegex(records[0]["request_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(records[0]["target"], "https://chatgpt.com")
        self.assertEqual(records[0]["proxy"], "http://proxy.example:8080")
        self.assertTrue(records[0]["tls_verification"])
        self.assertEqual(records[1]["headers"], {
            "x-request-id": "req-test-123", "cf-ray": "abc123-FRA", "retry-after": "5",
        })
        self.assertEqual(records[2]["phase"], "response_body")
        self.assertLessEqual(records[0]["elapsed_ms"], records[-1]["elapsed_ms"])
        self.assertIn("+00:00", records[0]["started_at"])
        self.assertNotIn("PRIVATE_", output.getvalue())

    def test_reset_before_headers_preserves_exception_without_retry(self):
        reset = ConnectionResetError(10054, "PRIVATE_ERROR")
        reset.winerror = 10054
        error = requests.ConnectionError(ProtocolError("PRIVATE_URL", reset))
        sender = Mock(side_effect=error)
        output = io.StringIO()
        with self.app.test_request_context("/v1/chat/completions", method="POST"), \
                contextlib.redirect_stderr(output):
            begin_request_diagnostic()
            with self.assertRaises(requests.ConnectionError) as raised:
                trace_upstream_send(sender, None, self.request, stream=True)
        self.assertIs(raised.exception, error)
        sender.assert_called_once()
        record = self.records(output)[-1]
        self.assertEqual(record["phase"], "connect_or_response_headers")
        self.assertEqual(record["event"], "request_failed")
        self.assertEqual(record["failure_kind"], "connection_reset")
        self.assertEqual(record["exception_chain"][-1]["winerror"], 10054)
        self.assertNotIn("PRIVATE_", output.getvalue())

    def test_body_read_failure_and_early_stop_are_distinct(self):
        error = requests.ConnectionError(ReadTimeoutError(None, "PRIVATE_URL", "PRIVATE_ERROR"))
        for fail in (True, False):
            with self.subTest(fail=fail):
                response = self.response()

                def content(*args, **kwargs):
                    yield b"PRIVATE_CHUNK"
                    if fail:
                        raise error
                    yield b"PRIVATE_NEXT_CHUNK"

                response.iter_content = content
                output = io.StringIO()
                with self.app.test_request_context("/v1/responses", method="POST"), \
                        contextlib.redirect_stderr(output):
                    begin_request_diagnostic()
                    trace_upstream_send(Mock(return_value=response), None,
                                        self.request, stream=True)
                    stream = response.iter_content()
                    self.assertEqual(next(stream), b"PRIVATE_CHUNK")
                    if fail:
                        with self.assertRaises(requests.ConnectionError) as raised:
                            next(stream)
                        self.assertIs(raised.exception, error)
                    else:
                        stream.close()
                record = self.records(output)[-1]
                self.assertEqual(record["phase"], "response_body")
                self.assertEqual(record["event"], "request_failed" if fail else "body_read_stopped")
                if fail:
                    self.assertEqual(record["failure_kind"], "read_timeout")
                self.assertNotIn("PRIVATE_", output.getvalue())

    def test_content_property_is_also_instrumented(self):
        response = self.response()
        output = io.StringIO()
        with self.app.test_request_context("/v1/completions", method="POST"), \
                contextlib.redirect_stderr(output):
            trace_upstream_send(Mock(return_value=response), None, self.request, stream=True)
            self.assertEqual(response.content, b"PRIVATE_BODY\n")
        self.assertEqual(self.records(output)[-1]["event"], "body_read_complete")
        self.assertNotIn("PRIVATE_", output.getvalue())

    def test_unrelated_requests_and_outside_context_pass_through(self):
        sender = Mock(return_value=object())
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertIs(trace_upstream_send(sender, None, self.request), sender.return_value)
            for path, url in (("/health", self.request.url),
                              ("/v1/chat/completions", "https://api.hh.ru/negotiations")):
                with self.app.test_request_context(path, method="POST"):
                    prepared = requests.Request("POST", url).prepare()
                    self.assertIs(trace_upstream_send(sender, None, prepared), sender.return_value)
        self.assertEqual(sender.call_count, 3)
        self.assertEqual(output.getvalue(), "")

    def test_exception_classification_and_cycles(self):
        for error, kind in (
            (requests.ConnectionError(socket.gaierror(11001, "PRIVATE_DNS")), "dns"),
            (requests.exceptions.SSLError("PRIVATE_TLS"), "tls"),
            (requests.ConnectTimeout("PRIVATE_TIMEOUT"), "connect_timeout"),
            (requests.ReadTimeout("PRIVATE_TIMEOUT"), "read_timeout"),
            (requests.exceptions.ProxyError("PRIVATE_PROXY"), "proxy"),
            (requests.ConnectionError("PRIVATE_ERROR"), "transport"),
        ):
            with self.subTest(kind=kind):
                self.assertEqual(exception_details(error)["failure_kind"], kind)
        error = requests.ConnectionError("PRIVATE_ERROR")
        error.__cause__ = error
        self.assertEqual(len(exception_details(error)["exception_chain"]), 1)

    def test_diagnostic_write_failure_does_not_change_network_or_body(self):
        response = self.response()
        sender = Mock(return_value=response)
        with self.app.test_request_context("/v1/chat/completions", method="POST"), \
                patch("chatmock_server.print", side_effect=OSError("disk unavailable")):
            result = trace_upstream_send(sender, None, self.request, stream=True)
            self.assertIs(result, response)
            self.assertEqual(result.content, b"PRIVATE_BODY\n")
        sender.assert_called_once()
        error = requests.ConnectTimeout("PRIVATE_ERROR")
        sender.side_effect = error
        with self.app.test_request_context("/v1/chat/completions", method="POST"), \
                patch("chatmock_server.print", side_effect=OSError("disk unavailable")):
            with self.assertRaises(requests.ConnectTimeout) as raised:
                trace_upstream_send(sender, None, self.request, stream=True)
        self.assertIs(raised.exception, error)

    def test_error_message_redacts_urls_including_proxy_credentials(self):
        cleaned = safe_error_message(
            "Failed at https://PRIVATE_USER:PRIVATE_PASS@chatgpt.com/path?token=PRIVATE_QUERY "
            "via socks5://PRIVATE_PROXY_USER:PRIVATE_PROXY_PASS@proxy.example:1080"
        )
        self.assertNotIn("PRIVATE_", cleaned)
        self.assertEqual(cleaned.count("[URL REDACTED]"), 2)

    def test_runtime_versions_only(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            log_runtime()
        record = self.records(output)[0]
        self.assertEqual(set(record), {"at", "versions"})
        self.assertEqual(set(record["versions"]), {"python", "chatmock", "requests", "urllib3"})

    def test_real_chatmock_maps_transport_reset_to_correlated_502(self):
        error = requests.ConnectionError(ProtocolError(
            "Failed at https://PRIVATE_USER:PRIVATE_PASSWORD@chatgpt.com/?token=PRIVATE_QUERY",
            ConnectionResetError(10054, "Connection reset"),
        ))
        output = io.StringIO()
        with contextlib.redirect_stderr(output), upstream_diagnostics(), \
                patch("chatmock.upstream.get_effective_chatgpt_auth",
                      return_value=("PRIVATE_TOKEN", "PRIVATE_ACCOUNT")), \
                patch("chatmock.upstream.resolve_installation_id", return_value="PRIVATE_INSTALL"), \
                patch("requests.adapters.HTTPAdapter.send", side_effect=error) as adapter:
            response = self.app.test_client().post("/v1/chat/completions", json={
                "model": "gpt-5.4-mini", "messages": [{"role": "user", "content": "PRIVATE_PROMPT"}],
                "stream": False,
            })
        self.assertEqual(response.status_code, 502)
        self.assertIn("ChatGPT transport failed", response.json["error"]["message"])
        self.assertNotIn("PRIVATE_", response.get_data(as_text=True))
        adapter.assert_called_once()
        records = self.records(output)
        self.assertEqual([r.get("event") for r in records], [
            "request_started", "request_failed", None,
        ])
        self.assertEqual(len({r["request_id"] for r in records}), 1)
        self.assertNotIn("PRIVATE_", output.getvalue())

    def test_real_chatmock_success_contract_is_unchanged(self):
        body = b'data: {"type":"response.output_text.delta","delta":"PRIVATE_LETTER"}\n\n'
        body += b'data: {"type":"response.completed","response":{"id":"test"}}\n\n'
        upstream = self.response(body)
        output = io.StringIO()
        with contextlib.redirect_stderr(output), upstream_diagnostics(), \
                patch("chatmock.upstream.get_effective_chatgpt_auth",
                      return_value=("PRIVATE_TOKEN", "PRIVATE_ACCOUNT")), \
                patch("chatmock.upstream.resolve_installation_id", return_value="PRIVATE_INSTALL"), \
                patch("requests.adapters.HTTPAdapter.send", return_value=upstream) as adapter:
            response = self.app.test_client().post("/v1/chat/completions", json={
                "model": "gpt-5.4-mini", "messages": [{"role": "user", "content": "PRIVATE_PROMPT"}],
                "stream": False,
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["choices"][0]["message"]["content"], "PRIVATE_LETTER")
        adapter.assert_called_once()
        events = [r["event"] for r in self.records(output)]
        self.assertEqual(events[:2], ["request_started", "response_received"])
        self.assertIn(events[-1], ("body_read_stopped", "body_read_complete"))
        self.assertNotIn("PRIVATE_", output.getvalue())

    def test_real_upstream_http_502_is_logged_as_received_response(self):
        upstream = self.response(b'{"error":{"message":"Temporarily unavailable"}}')
        upstream.status_code = 502
        output = io.StringIO()
        with contextlib.redirect_stderr(output), upstream_diagnostics(), \
                patch("chatmock.upstream.get_effective_chatgpt_auth",
                      return_value=("PRIVATE_TOKEN", "PRIVATE_ACCOUNT")), \
                patch("chatmock.upstream.resolve_installation_id", return_value="PRIVATE_INSTALL"), \
                patch("requests.adapters.HTTPAdapter.send", return_value=upstream) as adapter:
            response = self.app.test_client().post("/v1/chat/completions", json={
                "model": "gpt-5.4-mini", "messages": [{"role": "user", "content": "PRIVATE_PROMPT"}],
                "stream": False,
            })
        self.assertEqual(response.status_code, 502)
        adapter.assert_called_once()
        records = self.records(output)
        self.assertEqual(records[1]["event"], "response_received")
        self.assertEqual(records[1]["status"], 502)
        self.assertFalse(any(r.get("event") == "request_failed" for r in records))
        self.assertEqual(len({r["request_id"] for r in records}), 1)
        self.assertNotIn("PRIVATE_", output.getvalue())

    def test_existing_auth_refresh_has_separate_upstream_ids_and_same_request_id(self):
        rejected = self.response()
        rejected.status_code = 401
        accepted = self.response(b'data: {"type":"response.completed"}\n\n')
        output = io.StringIO()

        def auth(force_refresh=False):
            return ("PRIVATE_NEW" if force_refresh else "PRIVATE_OLD", "PRIVATE_ACCOUNT")

        with contextlib.redirect_stderr(output), upstream_diagnostics(), \
                patch("chatmock.upstream.get_effective_chatgpt_auth", side_effect=auth), \
                patch("chatmock.upstream.resolve_installation_id", return_value="PRIVATE_INSTALL"), \
                patch("requests.adapters.HTTPAdapter.send",
                      side_effect=[rejected, accepted]) as adapter:
            response = self.app.test_client().post("/v1/chat/completions", json={
                "model": "gpt-5.4-mini", "messages": [{"role": "user", "content": "PRIVATE_PROMPT"}],
                "stream": False,
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(adapter.call_count, 2)  # ChatMock's existing 401 refresh, not a new retry.
        records = self.records(output)
        self.assertEqual(len({r["request_id"] for r in records}), 1)
        self.assertEqual(len({r["upstream_id"] for r in records}), 2)
        self.assertEqual([r["status"] for r in records if r["event"] == "response_received"],
                         [401, 200])
        self.assertNotIn("PRIVATE_", output.getvalue())

    def test_connect_timeout_recovery_has_bounded_backoff_and_preserves_settings(self):
        prepared = requests.Request("POST", CHATGPT_RESPONSES_URL, json={"input": "PRIVATE"}).prepare()
        response = self.response()
        sender = Mock(side_effect=[requests.ConnectTimeout("PRIVATE"), response])
        output = io.StringIO()
        with self.app.test_request_context("/v1/chat/completions", method="POST"), \
                contextlib.redirect_stderr(output), patch("chatmock_server.time.sleep") as sleep:
            begin_request_diagnostic()
            self.assertIs(trace_upstream_send(sender, None, prepared, stream=True, timeout=600,
                                             proxies={"https": "http://proxy.invalid"}, verify=True), response)
        self.assertEqual(sender.call_count, 2)
        sleep.assert_called_once_with(backoffs()[0])
        for sent in sender.call_args_list:
            self.assertIs(sent.args[1], prepared)
            self.assertEqual(sent.kwargs["timeout"], (10, 600))
            self.assertEqual(sent.kwargs["proxies"], {"https": "http://proxy.invalid"})
            self.assertIs(sent.kwargs["verify"], True)
        records = self.records(output)
        self.assertEqual([r["event"] for r in records], [
            "request_started", "request_failed", "retry_scheduled", "response_received"])
        self.assertEqual(records[2]["backoff_seconds"], backoffs()[0])
        self.assertEqual(len({r["request_id"] for r in records}), 1)
        self.assertNotIn("PRIVATE", output.getvalue())

    def test_retry_budget_is_pinned(self):
        # Change these deliberately: send.py, HH's 120 s OpenAI timeout and docs depend on them.
        self.assertEqual(chatmock_server.CONNECT_ATTEMPTS, 4)
        self.assertEqual(chatmock_server.CONNECT_BACKOFFS, (2, 4, 8))
        self.assertEqual(chatmock_server.CONNECT_TIMEOUT, 10)
        self.assertEqual(len(chatmock_server.CONNECT_BACKOFFS), chatmock_server.CONNECT_ATTEMPTS - 1)
        self.assertEqual(sum(chatmock_server.CONNECT_BACKOFFS), 14)
        self.assertEqual(chatmock_server.CONNECT_RETRY_BUDGET, 60)

    def test_real_chatmock_connect_timeout_exhaustion_is_sanitized_and_bounded(self):
        clock = FakeClock()
        response, output, timeouts = self.post_through_real_chatmock(clock, [
            (10.0, requests.ConnectTimeout("PRIVATE https://user:password@host/?token=secret"))
            for _ in range(attempts())])
        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(timeouts), attempts())
        self.assertEqual(clock.sleeps, backoffs())
        self.assertEqual(sum(clock.sleeps), 14)  # Worst-case total backoff.
        failure = response.json["error"]["generation_failure"]
        self.assertEqual(failure["kind"], "connect_timeout")
        self.assertEqual(failure["attempts"], attempts())
        self.assertIs(failure["connect_retry_exhausted"], True)
        self.assertRegex(failure["request_id"], r"^[0-9a-f]{32}$")
        for text in (output.getvalue(), response.get_data(as_text=True)):
            self.assertNotIn("PRIVATE", text)
            self.assertNotIn("password", text)

    def test_real_chatmock_recovers_connect_timeout_then_generates_once(self):
        body = b'data: {"type":"response.output_text.delta","delta":"Test letter."}\n\n'
        body += b'data: {"type":"response.completed","response":{"id":"test"}}\n\n'
        clock = FakeClock()
        response, output, timeouts = self.post_through_real_chatmock(
            clock, [(10.0, requests.ConnectTimeout("PRIVATE")), (1.0, self.response(body))])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["choices"][0]["message"]["content"], "Test letter.")
        self.assertEqual(len(timeouts), 2)
        self.assertEqual(clock.sleeps, backoffs()[:1])
        self.assertFalse(any("generation_failure" in r for r in self.network_records(output)))
        self.assertNotIn("PRIVATE", output.getvalue())

    def test_only_connect_or_tls_handshake_timeout_can_retry_exact_generation_endpoint(self):
        # A ReadTimeout after the full read budget is a real read timeout. Every
        # other failure is not retried even when it fails instantly.
        errors = [(600.0, requests.ReadTimeout("PRIVATE")),
                  (0.0, requests.exceptions.SSLError("PRIVATE")),
                  (0.0, requests.ConnectionError("PRIVATE")),
                  (0.0, requests.exceptions.ProxyError("PRIVATE")),
                  (0.0, requests.exceptions.ChunkedEncodingError("PRIVATE")),
                  (0.0, requests.ConnectionError(socket.gaierror(11001, "PRIVATE")))]
        prepared = requests.Request("POST", CHATGPT_RESPONSES_URL).prepare()
        for elapsed, error in errors:
            clock = FakeClock()
            sender = timed_sender(clock, [(elapsed, error)])
            with self.subTest(error=type(error).__name__), \
                    self.app.test_request_context("/v1/chat/completions", method="POST"), \
                    contextlib.redirect_stderr(io.StringIO()), patch("chatmock_server.time", clock):
                with self.assertRaises(type(error)):
                    trace_upstream_send(sender, None, prepared, timeout=600, stream=True)
                sender.assert_called_once()
                self.assertEqual(clock.sleeps, [])
                self.assertNotEqual(g.generation_failure["kind"], "tls_handshake_timeout")
                self.assertIs(g.generation_failure["connect_retry_exhausted"], False)
        for method, url in (("POST", "https://api.hh.ru/negotiations"),
                            ("GET", CHATGPT_RESPONSES_URL),
                            ("POST", CHATGPT_RESPONSES_URL + "?other=true")):
            sender = Mock(side_effect=requests.ConnectTimeout("PRIVATE"))
            with self.subTest(method=method, url=url), \
                    self.app.test_request_context("/v1/chat/completions", method="POST"), \
                    contextlib.redirect_stderr(io.StringIO()), patch("chatmock_server.time.sleep") as sleep:
                with self.assertRaises(requests.ConnectTimeout):
                    trace_upstream_send(sender, None, requests.Request(method, url).prepare())
                sender.assert_called_once()
                sleep.assert_not_called()

    def post_through_real_chatmock(self, clock, outcomes):
        """Script only ChatGPT generation attempts; any other upstream call fails offline."""
        pending, timeouts = list(outcomes), []

        def adapter(prepared, **kwargs):
            if prepared.method != "POST" or prepared.url != CHATGPT_RESPONSES_URL:
                raise requests.ConnectionError("Unrelated upstream request is offline")
            timeouts.append(kwargs.get("timeout"))
            elapsed, result = pending.pop(0)
            clock.now += elapsed
            if isinstance(result, BaseException):
                raise result
            return result

        output = io.StringIO()
        with contextlib.redirect_stderr(output), upstream_diagnostics(), \
                patch("chatmock.upstream.get_effective_chatgpt_auth", return_value=("PRIVATE", "PRIVATE")), \
                patch("chatmock.upstream.resolve_installation_id", return_value="PRIVATE"), \
                patch("chatmock_server.time", clock), \
                patch("requests.adapters.HTTPAdapter.send", side_effect=adapter):
            response = self.app.test_client().post("/v1/chat/completions", json={
                "model": "gpt-5.4-mini", "messages": [{"role": "user", "content": "PRIVATE_PROMPT"}],
            })
        return response, output, timeouts

    def network_records(self, output):
        return [json.loads(line.split("] ", 1)[1]) for line in output.getvalue().splitlines()
                if line.startswith("[ChatMock network] ")]

    def test_real_chatmock_fast_read_timeout_exhaustion_is_tls_handshake_timeout(self):
        clock = FakeClock()
        response, output, timeouts = self.post_through_real_chatmock(
            clock, [(10.8, handshake_stall()) for _ in range(attempts())])
        self.assertEqual(response.status_code, 502)
        self.assertEqual(timeouts, [(10, 600)] * attempts())
        self.assertEqual(clock.sleeps, backoffs())
        error = response.json["error"]
        self.assertTrue(error["message"].startswith("ChatGPT connection establishment timed out"))
        failure = error["generation_failure"]
        self.assertEqual(failure["kind"], "tls_handshake_timeout")
        self.assertEqual(failure["attempts"], attempts())
        self.assertIs(failure["connect_retry_exhausted"], True)
        self.assertRegex(failure["request_id"], r"^[0-9a-f]{32}$")
        records = self.network_records(output)
        expected = [("request_started", None, None)]
        for attempt in range(1, attempts() + 1):
            expected.append(("request_failed", "tls_handshake_timeout", attempt))
            if attempt < attempts():
                expected.append(("retry_scheduled", None, attempt))
        self.assertEqual([(r["event"], r.get("failure_kind"), r.get("attempt")) for r in records],
                         expected)
        scheduled = [r for r in records if r["event"] == "retry_scheduled"]
        self.assertEqual([(r["next_attempt"], r["backoff_seconds"]) for r in scheduled],
                         list(zip(range(2, attempts() + 1), backoffs())))
        self.assertEqual({r["request_id"] for r in records}, {failure["request_id"]})
        self.assertIn("ReadTimeout", {item["type"].rsplit(".", 1)[-1]
                                      for item in records[1]["exception_chain"]})
        error_lines = [line for line in output.getvalue().splitlines()
                       if line.startswith("[ChatMock error] ")]
        self.assertEqual(len(error_lines), 1)
        self.assertIn(failure["request_id"], error_lines[0])
        for text in (output.getvalue(), response.get_data(as_text=True)):
            self.assertNotIn("PRIVATE", text)

    def test_real_chatmock_recovers_fast_read_timeout_then_generates_once(self):
        body = b'data: {"type":"response.output_text.delta","delta":"Test letter."}\n\n'
        body += b'data: {"type":"response.completed","response":{"id":"test"}}\n\n'
        clock = FakeClock()
        response, output, timeouts = self.post_through_real_chatmock(
            clock, [(10.8, handshake_stall()), (2.0, self.response(body))])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["choices"][0]["message"]["content"], "Test letter.")
        self.assertEqual(len(timeouts), 2)
        self.assertEqual(clock.sleeps, backoffs()[:1])
        events = [r["event"] for r in self.network_records(output)]
        self.assertEqual(events[:4], ["request_started", "request_failed",
                                      "retry_scheduled", "response_received"])
        self.assertNotIn("[ChatMock error]", output.getvalue())
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertNotIn("PRIVATE", response.get_data(as_text=True))

    def test_real_chatmock_slow_read_timeout_is_not_retried(self):
        clock = FakeClock()
        response, output, timeouts = self.post_through_real_chatmock(
            clock, [(600.0, handshake_stall())])
        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(timeouts), 1)
        self.assertEqual(clock.sleeps, [])
        error = response.json["error"]
        self.assertIn("ChatGPT transport failed", error["message"])
        self.assertEqual({key: error["generation_failure"][key] for key in
                          ("kind", "attempts", "connect_retry_exhausted")},
                         {"kind": "read_timeout", "attempts": 1, "connect_retry_exhausted": False})
        self.assertEqual([r["event"] for r in self.network_records(output)],
                         ["request_started", "request_failed"])
        for text in (output.getvalue(), response.get_data(as_text=True)):
            self.assertNotIn("PRIVATE", text)

    def trace_with_clock(self, outcomes, prepared=None, **kwargs):
        clock = FakeClock()
        sender = timed_sender(clock, outcomes)
        prepared = prepared or requests.Request("POST", CHATGPT_RESPONSES_URL,
                                                json={"input": "PRIVATE"}).prepare()
        output = io.StringIO()
        with self.app.test_request_context("/v1/chat/completions", method="POST"), \
                contextlib.redirect_stderr(output), patch("chatmock_server.time", clock):
            begin_request_diagnostic()
            try:
                result = trace_upstream_send(sender, None, prepared, stream=True, **kwargs)
            except requests.RequestException as error:
                result = error
            failure = g.get("generation_failure")
        self.assertNotIn("PRIVATE", output.getvalue())
        return result, sender, clock, self.records(output), failure

    def test_read_timeout_at_or_after_half_read_timeout_is_not_retried(self):
        # A small read timeout keeps the half-timeout boundary inside the retry budget.
        for timeout, elapsed in ((60, 30.0), (60, 30.5), (60, 45.0), (60, 60.0), (600, 600.0)):
            with self.subTest(timeout=timeout, elapsed=elapsed):
                error = handshake_stall()
                result, sender, clock, records, failure = self.trace_with_clock(
                    [(elapsed, error)], timeout=timeout)
                self.assertIs(result, error)
                sender.assert_called_once()
                self.assertEqual(sender.call_args.kwargs["timeout"], (10, timeout))
                self.assertEqual(clock.sleeps, [])
                self.assertEqual(records[-1]["failure_kind"], "read_timeout")
                self.assertNotIn("retry_budget_exhausted", records[-1])
                self.assertEqual(failure, {"kind": "read_timeout", "attempts": 1,
                                           "connect_retry_exhausted": False})
        # Just under the boundary is still a handshake stall and is retried once.
        response = self.response()
        result, sender, clock, records, failure = self.trace_with_clock(
            [(29.9, handshake_stall()), (1.0, response)], timeout=60)
        self.assertIs(result, response)
        self.assertEqual(sender.call_count, 2)
        self.assertEqual(clock.sleeps, backoffs()[:1])
        self.assertEqual(records[1]["failure_kind"], "tls_handshake_timeout")
        self.assertIsNone(failure)

    def test_handshake_classification_uses_each_attempt_elapsed_time(self):
        # Second attempt is slow on its own: a real read timeout, no third attempt.
        error = handshake_stall()
        result, sender, clock, records, failure = self.trace_with_clock(
            [(10.8, handshake_stall()), (400.0, error)], timeout=600)
        self.assertIs(result, error)
        self.assertEqual(sender.call_count, 2)
        self.assertEqual(clock.sleeps, backoffs()[:1])
        self.assertEqual([(r["event"], r.get("failure_kind")) for r in records], [
            ("request_started", None), ("request_failed", "tls_handshake_timeout"),
            ("retry_scheduled", None), ("request_failed", "read_timeout"),
        ])
        self.assertEqual(failure, {"kind": "read_timeout", "attempts": 2,
                                   "connect_retry_exhausted": False})
        # Read timeout 30 s: threshold 15 s per attempt. Four 12 s attempts plus
        # backoffs total 12+2+12+4+12+8+12, far above 15 s, yet each attempt is
        # fast. Budget check before attempt 4: 12+2+12+4+12+8+10 = 60, allowed.
        error = handshake_stall()
        outcomes = [(12.0, handshake_stall()) for _ in range(attempts() - 1)] + [(12.0, error)]
        result, sender, clock, records, failure = self.trace_with_clock(outcomes, timeout=30)
        self.assertIs(result, error)
        self.assertEqual(sender.call_count, attempts())
        self.assertEqual(clock.sleeps, backoffs())
        failed = [r for r in records if r["event"] == "request_failed"]
        self.assertEqual([r["failure_kind"] for r in failed], ["tls_handshake_timeout"] * attempts())
        self.assertFalse(any("retry_budget_exhausted" in r for r in failed))
        self.assertEqual(failure, {"kind": "tls_handshake_timeout", "attempts": attempts(),
                                   "connect_retry_exhausted": True})

    def test_mixed_connect_and_handshake_timeouts_share_one_budget(self):
        response = self.response()
        result, sender, clock, records, failure = self.trace_with_clock([
            (10.0, requests.ConnectTimeout("PRIVATE")), (10.8, handshake_stall()), (1.0, response),
        ], timeout=600)
        self.assertIs(result, response)
        self.assertEqual(sender.call_count, 3)
        self.assertEqual(clock.sleeps, [2, 4])
        self.assertEqual([(r["event"], r.get("failure_kind"), r.get("attempt")) for r in records], [
            ("request_started", None, None),
            ("request_failed", "connect_timeout", 1), ("retry_scheduled", None, 1),
            ("request_failed", "tls_handshake_timeout", 2), ("retry_scheduled", None, 2),
            ("response_received", None, None),
        ])
        self.assertIsNone(failure)

    def test_non_retryable_failure_on_third_attempt_stops_immediately(self):
        reset = ConnectionResetError(10054, "PRIVATE")
        for kind, error, elapsed in (
            ("read_timeout", handshake_stall(), 400.0),
            ("tls", requests.exceptions.SSLError("PRIVATE"), 0.5),
            ("connection_reset", requests.ConnectionError(ProtocolError("PRIVATE", reset)), 0.5),
            ("proxy", requests.exceptions.ProxyError("PRIVATE"), 0.5),
            ("transport", requests.ConnectionError("PRIVATE"), 0.5),
        ):
            with self.subTest(kind=kind):
                result, sender, clock, records, failure = self.trace_with_clock([
                    (10.0, requests.ConnectTimeout("PRIVATE")), (10.8, handshake_stall()),
                    (elapsed, error),
                ], timeout=600)
                self.assertIs(result, error)
                self.assertEqual(sender.call_count, 3)
                self.assertEqual(clock.sleeps, backoffs()[:2])
                self.assertEqual(records[-1]["event"], "request_failed")
                self.assertEqual(records[-1]["failure_kind"], kind)
                self.assertEqual(failure, {"kind": kind, "attempts": 3,
                                           "connect_retry_exhausted": False})

    def test_fast_read_timeout_outside_exact_endpoint_or_numeric_timeout_is_not_retried(self):
        exact = CHATGPT_RESPONSES_URL
        cases = (
            ("other URL", requests.Request("POST", exact + "?other=true").prepare(), {"timeout": 600}),
            ("other path", requests.Request(
                "POST", "https://chatgpt.com/backend-api/codex/other").prepare(), {"timeout": 600}),
            ("GET", requests.Request("GET", exact).prepare(), {"timeout": 600}),
            ("no timeout", None, {}),
            ("None timeout", None, {"timeout": None}),
            ("tuple timeout", None, {"timeout": (10, 600)}),
        )
        for name, prepared, kwargs in cases:
            with self.subTest(case=name):
                error = handshake_stall()
                result, sender, clock, records, failure = self.trace_with_clock(
                    [(0.5, error)], prepared=prepared, **kwargs)
                self.assertIs(result, error)
                sender.assert_called_once()
                self.assertEqual(clock.sleeps, [])
                self.assertEqual(sender.call_args.kwargs.get("timeout"), kwargs.get("timeout"))
                self.assertEqual(records[-1]["failure_kind"], "read_timeout")
                self.assertEqual(failure["kind"], "read_timeout")
                self.assertIs(failure["connect_retry_exhausted"], False)

    def test_four_short_stalls_fit_the_retry_budget(self):
        # Before attempt 4: 10.3*3 + 2 + 4 + 8 + 10 = 54.9 s, within 60 s.
        error = handshake_stall()
        result, sender, clock, records, failure = self.trace_with_clock(
            [(10.3, handshake_stall()) for _ in range(3)] + [(10.3, error)], timeout=600)
        self.assertIs(result, error)
        self.assertEqual(sender.call_count, 4)
        self.assertEqual(clock.sleeps, [2, 4, 8])
        self.assertFalse(any("retry_budget_exhausted" in r for r in records))
        self.assertEqual(failure, {"kind": "tls_handshake_timeout", "attempts": 4,
                                   "connect_retry_exhausted": True})

    def test_real_chatmock_slow_second_attempt_stops_at_retry_budget(self):
        # Before attempt 4: 10 + 2 + 25 + 4 + 10 = 51, plus 8 + 10 = 69 > 60.
        clock = FakeClock()
        response, output, timeouts = self.post_through_real_chatmock(
            clock, [(10.0, handshake_stall()), (25.0, handshake_stall()), (10.0, handshake_stall())])
        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(timeouts), 3)
        self.assertEqual(clock.sleeps, [2, 4])
        error = response.json["error"]
        self.assertTrue(error["message"].startswith("ChatGPT connection establishment timed out"))
        failure = error["generation_failure"]
        self.assertEqual({key: failure[key] for key in ("kind", "attempts", "connect_retry_exhausted")},
                         {"kind": "tls_handshake_timeout", "attempts": 3, "connect_retry_exhausted": True})
        records = self.network_records(output)
        failed = [r for r in records if r["event"] == "request_failed"]
        self.assertEqual([r.get("retry_budget_exhausted") for r in failed], [None, None, True])
        self.assertEqual(records[-1], failed[-1])  # No retry_scheduled after the budget stop.
        self.assertEqual(sum(r["event"] == "retry_scheduled" for r in records), 2)
        for text in (output.getvalue(), response.get_data(as_text=True)):
            self.assertNotIn("PRIVATE", text)

    def test_slow_first_attempt_stops_at_retry_budget_without_sleep(self):
        # 50 + 2 + 10 = 62 > 60: no second attempt can fail within the budget.
        for kind, error in (("tls_handshake_timeout", handshake_stall()),
                            ("connect_timeout", requests.ConnectTimeout("PRIVATE"))):
            with self.subTest(kind=kind):
                result, sender, clock, records, failure = self.trace_with_clock(
                    [(50.0, error)], timeout=600)
                self.assertIs(result, error)
                sender.assert_called_once()
                self.assertEqual(clock.sleeps, [])
                self.assertEqual([r["event"] for r in records], ["request_started", "request_failed"])
                self.assertEqual(records[-1]["failure_kind"], kind)
                self.assertIs(records[-1]["retry_budget_exhausted"], True)
                self.assertEqual(failure, {"kind": kind, "attempts": 1,
                                           "connect_retry_exhausted": True})

    def test_retry_budget_boundary_allows_exactly_sixty_seconds(self):
        # elapsed + backoff 2 + connect timeout 10 == 60 is allowed; 60.1 is not.
        response = self.response()
        result, sender, clock, records, failure = self.trace_with_clock(
            [(48.0, handshake_stall()), (1.0, response)], timeout=600)
        self.assertIs(result, response)
        self.assertEqual(sender.call_count, 2)
        self.assertEqual(clock.sleeps, [2])
        self.assertFalse(any("retry_budget_exhausted" in r for r in records))
        self.assertIsNone(failure)
        error = handshake_stall()
        result, sender, clock, records, failure = self.trace_with_clock(
            [(48.1, error)], timeout=600)
        self.assertIs(result, error)
        sender.assert_called_once()
        self.assertEqual(clock.sleeps, [])
        self.assertIs(records[-1]["retry_budget_exhausted"], True)
        self.assertEqual(failure, {"kind": "tls_handshake_timeout", "attempts": 1,
                                   "connect_retry_exhausted": True})

    def test_non_retryable_failures_never_get_retry_budget_flag(self):
        other = requests.Request("POST", CHATGPT_RESPONSES_URL + "?other=true").prepare()
        for name, outcomes, prepared in (
            ("slow read timeout", [(600.0, handshake_stall())], None),
            ("tls error", [(55.0, requests.exceptions.SSLError("PRIVATE"))], None),
            ("transport", [(55.0, requests.ConnectionError("PRIVATE"))], None),
            ("connect timeout on other URL", [(55.0, requests.ConnectTimeout("PRIVATE"))], other),
            ("third attempt slow read", [(10.0, requests.ConnectTimeout("PRIVATE")),
                                         (10.8, handshake_stall()), (400.0, handshake_stall())], None),
        ):
            with self.subTest(case=name):
                result, sender, clock, records, failure = self.trace_with_clock(
                    outcomes, prepared=prepared, timeout=600)
                self.assertIsInstance(result, requests.RequestException)
                self.assertEqual(sender.call_count, len(outcomes))
                self.assertFalse(any("retry_budget_exhausted" in r for r in records))
                self.assertIs(failure["connect_retry_exhausted"], False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
