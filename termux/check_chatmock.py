"""Offline native-Termux smoke tests. No login, inference, or HH requests."""

import json
import unittest
from unittest.mock import patch

from chatmock.app import create_app


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
        self.app = create_app(
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
        with patch(
            "chatmock.routes_openai.start_upstream_request",
            return_value=(FakeUpstream(), None),
        ) as upstream:
            response = self.client.post("/v1/chat/completions", json=self.payload)
        self.assertEqual(response.status_code, 200)
        message = response.json["choices"][0]["message"]
        self.assertEqual(message["content"], "Test letter.")
        self.assertFalse(message.get("tool_calls"))
        self.assertFalse(upstream.call_args.kwargs["tools"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
