"""Offline checks for the redacted OTP rejection summary in authorize.py."""

import contextlib
import io
import unittest
from unittest.mock import patch

import requests
from hh_applicant_tool.operations import authorize as hh_authorize

import authorize

USERNAME = "secret.person@example.com"
CODE = "482913"
SECRETS = (USERNAME, CODE, "tok_SECRETvalue123", "79991234567", "https://hh.ru/back?x=1",
           "auto.login@example.com", "x482913x", "deep_reason_hidden", "csrf_SECRET_value")


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200
        self.url = "https://hh.ru/account/login/by_code"

    def json(self):
        return self.payload


def rejected_response():
    return {
        "availableAuthScenarios": ["APPLICANT_AUTH", "EMPLOYER_AUTH"],
        "success": False,
        "error": None,
        "backurl": "https://hh.ru/back?x=1",
        "autologinemail": "auto.login@example.com",
        "verification": {
            "success": False,
            "errorKey": "CODE_EXPIRED",
            "reason": "WRONG_CODE",
            "code": CODE,
            "codeStatus": "x482913x",
            "email": USERNAME,
            "phone": "79991234567",
            "token": "tok_SECRETvalue123",
            "csrfType": "csrf_SECRET_value",
            "details": {
                "status": "rejected",
                "accessToken": "tok_SECRETvalue123",
                "nested": {"type": "otp-mismatch",
                           "deep": {"reason": "deep_reason_hidden"}},
            },
        },
    }


class AuthorizeDiagnostics(unittest.TestCase):
    def confirm(self, payload, username=USERNAME, otp=CODE):
        """Run authorize.run with a fake CLI that performs one OTP confirmation."""
        sent = []

        def fake_post(session, url, *args, **kwargs):
            sent.append((url, args, kwargs))
            return FakeResponse(payload)

        def fake_main(argv):
            op = hh_authorize.Operation.__new__(hh_authorize.Operation)
            op.is_email, op.redirect_url = "@" in username, "https://hh.ru/account/login"
            op.tool = type("Tool", (), {"session": requests.Session(), "xsrf_token": "t"})()
            try:
                op._confirm_otp(username)
            except hh_authorize.OtpConfirmError:
                return 7
            except AttributeError:  # HH 2.3.7 with "error": null
                return 1
            return 0

        stderr = io.StringIO()
        with patch.object(requests.Session, "post", fake_post), \
                patch.object(authorize, "main", fake_main), \
                patch.object(hh_authorize.Operation, "_login_trust_flags", lambda *a: "flags"), \
                patch.object(hh_authorize.Operation, "_log_response", staticmethod(lambda r: None)), \
                patch("builtins.input", lambda prompt="": otp), \
                contextlib.redirect_stderr(stderr):
            code = authorize.run(["authorize"])
        return code, stderr.getvalue(), sent

    def test_rejection_prints_safe_fields_only(self):
        code, output, sent = self.confirm(rejected_response())
        self.assertEqual(code, 1)
        self.assertEqual(output.count("\n"), 1)
        self.assertIn("HH by_code diagnostics:", output)
        for safe in ("verification.success=False", "verification.errorKey='CODE_EXPIRED'",
                     "verification.reason='WRONG_CODE'", "verification.details.status='rejected'",
                     "verification.details.nested.type='otp-mismatch'", "error=None",
                     "availableAuthScenarios=['APPLICANT_AUTH', 'EMPLOYER_AUTH']",
                     "success=False", "email: str", "token: str"):
            self.assertIn(safe, output)
        for secret in SECRETS:
            self.assertNotIn(secret, output)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0][0].endswith("/account/login/by_code"))

    def test_request_is_unchanged(self):
        _, _, wrapped = self.confirm(rejected_response())
        sent = []

        def fake_post(session, url, *args, **kwargs):
            sent.append((url, args, kwargs))
            return FakeResponse(rejected_response())

        op = hh_authorize.Operation.__new__(hh_authorize.Operation)
        op.is_email, op.redirect_url = True, "https://hh.ru/account/login"
        op.tool = type("Tool", (), {"session": requests.Session(), "xsrf_token": "t"})()
        with patch.object(requests.Session, "post", fake_post), \
                patch.object(hh_authorize.Operation, "_login_trust_flags", lambda *a: "flags"), \
                patch.object(hh_authorize.Operation, "_log_response", staticmethod(lambda r: None)), \
                patch("builtins.input", lambda prompt="": CODE):
            with self.assertRaises(AttributeError):
                op._confirm_otp(USERNAME)
        self.assertEqual(wrapped, sent)

    def test_error_key_rejection_is_reported(self):
        res = rejected_response()
        res["error"] = {"key": "CODE_INVALID", "token": "tok_SECRETvalue123"}
        code, output, _ = self.confirm(res)
        self.assertEqual(code, 7)
        self.assertIn("error.key='CODE_INVALID'", output)
        for secret in SECRETS:
            self.assertNotIn(secret, output)

    def test_success_prints_nothing(self):
        payload = {"success": True, "verification": {"success": True, "token": "tok_SECRETvalue123"}}
        code, output, _ = self.confirm(payload)
        self.assertEqual(code, 0)
        self.assertEqual(output, "")

    def test_non_list_scenarios_and_non_json_are_safe(self):
        res = rejected_response()
        res["availableAuthScenarios"] = [USERNAME]
        line = authorize.summary(res, authorize.secret_fragments(USERNAME, CODE))
        self.assertIn("availableAuthScenarios=[<redacted>]", line)
        self.assertNotIn(USERNAME, line)

        class NotJson:
            def json(self):
                raise ValueError("bad")

        recorder = authorize.ByCodeRecorder()
        recorder.response = NotJson()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            authorize.report(recorder)
        self.assertEqual(stderr.getvalue(), "HH by_code diagnostics: response is not JSON\n")

    def leaks(self, verification, username=USERNAME, otp=CODE, scenarios=None):
        res = {"success": False, "error": None, "verification": verification}
        if scenarios is not None:
            res["availableAuthScenarios"] = scenarios
        code, output, _ = self.confirm(res, username=username, otp=otp)
        self.assertEqual(code, 1)
        self.assertEqual(output.count("\n"), 1)
        self.assertTrue(output.endswith("\n"))
        return output

    def test_phone_parts_are_redacted(self):
        phone = "8 (900) 123-45-67"
        for verification in ({"reason": "900-123-45-67"}, {"type": "sent_to_4567"}):
            with self.subTest(verification=verification):
                output = self.leaks(verification, username=phone)
                self.assertNotIn(next(iter(verification.values())), output)
                self.assertNotIn("4567", output)
                self.assertNotIn("900", output)

    def test_code_parts_are_redacted(self):
        output = self.leaks({"status": "x4829"}, otp="482913")
        self.assertNotIn("4829", output)
        self.assertIn("status: str", output)

    def test_email_local_part_is_redacted(self):
        output = self.leaks({"type": "Secret.Person"}, username="Secret.Person@Example.com",
                            scenarios=["Secret.Person", "APPLICANT_AUTH"])
        self.assertNotIn("secret.person", output.lower())
        self.assertIn("availableAuthScenarios=[<redacted>, 'APPLICANT_AUTH']", output)

    def test_key_names_are_redacted(self):
        output = self.leaks({"secret.person": True, "id_9001234567": True},
                            username="secret.person@example.com")
        self.assertNotIn("secret.person", output)
        self.assertNotIn("9001234567", output)
        self.assertIn("verification={<redacted>: bool, <redacted>: bool}", output)

    def test_newline_in_key_keeps_one_line(self):
        output = self.leaks({"type\n": "multi", "reason\nx": "line"})
        self.assertNotIn("multi", output)
        self.assertNotIn("'line'", output)
        self.assertIn("verification={<redacted>: str, <redacted>: str}", output)

    def test_other_urls_are_not_recorded(self):
        recorder = authorize.ByCodeRecorder()
        recorder.record("https://hh.ru/account/otp_generate", {}, FakeResponse({}))
        self.assertIsNone(recorder.response)
        recorder.record("http://[bad", {}, FakeResponse({}))
        self.assertIsNone(recorder.response)

    def test_patches_are_removed_after_run(self):
        post, confirm = requests.Session.post, hh_authorize.Operation._confirm_otp
        with patch.object(authorize, "main", lambda argv: 3):
            self.assertEqual(authorize.run([]), 3)
        self.assertIs(requests.Session.post, post)
        self.assertIs(hh_authorize.Operation._confirm_otp, confirm)


if __name__ == "__main__":
    unittest.main()
