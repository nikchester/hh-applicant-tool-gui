"""Check the installed CLI contract without loading an account or making requests."""

from pathlib import Path
import os
import shlex
import unittest
from unittest.mock import patch

from hh_applicant_tool.tool import HHApplicantTool
from preview_hh import allowed_request, enrich_prompt


class PreviewTests(unittest.TestCase):
    def test_wrapper_arguments(self):
        source = Path(__file__).with_name("hh-preview.sh").read_text()
        command = source.split('"$ROOT/bootstrap/preview_hh.py"', 1)[1]
        command = command.replace("\\\n", " ")
        # Values resembling options must remain values, not override safety flags.
        command = command.replace("$1", "--no-dry-run")
        command = command.replace("$2", "--no-send-email")
        with patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Network forbidden in parser test"),
        ):
            args = HHApplicantTool._create_parser().parse_args(shlex.split(command))
        self.assertTrue(args.dry_run)
        self.assertFalse(args.send_email)
        self.assertFalse(args.ai_filter)
        self.assertTrue(args.skip_tests)
        self.assertFalse(args.captcha_handling)
        self.assertEqual(args.per_page, 3)
        self.assertEqual(args.total_pages, 1)
        self.assertEqual(args.resume_id, "--no-dry-run")
        self.assertEqual(args.text, "--no-send-email")
        self.assertEqual(args.work_format, ["REMOTE"])
        self.assertEqual(args.currency, "RUR")
        self.assertEqual(args.salary, 80000)
        self.assertTrue(args.only_with_salary)
        self.assertEqual(args.search_field, ["name"])

    def test_config_selected_before_initialization(self):
        source = Path(__file__).with_name("hh-preview.sh").read_text()
        self.assertIn('export CONFIG_DIR="$CONFIG"', source)
        self.assertIn('export HH_PROFILE_ID=.', source)
        target = Path.home() / ".config/pocketnode-hh"
        with patch.dict(os.environ, {"CONFIG_DIR": str(target), "HH_PROFILE_ID": "."}):
            with patch(
                "requests.sessions.Session.request",
                side_effect=AssertionError("Network forbidden in config test"),
            ):
                tool = HHApplicantTool()
                self.assertEqual(tool.config_dir_path, target.resolve())

    def test_network_guard(self):
        self.assertTrue(allowed_request("GET", "https://api.hh.ru/resumes/mine"))
        self.assertTrue(allowed_request("POST", "http://127.0.0.1:8000/v1/chat/completions"))
        self.assertTrue(allowed_request("POST", "https://api.hh.ru/token", "grant_type=refresh_token&refresh_token=test"))
        self.assertFalse(allowed_request("POST", "https://api.hh.ru/negotiations"))
        self.assertFalse(allowed_request("PUT", "https://api.hh.ru/vacancies/blacklisted/1"))
        self.assertFalse(allowed_request("POST", "https://api.hh.ru/token", "grant_type=authorization_code"))
        self.assertFalse(allowed_request("POST", "https://api.openai.com/v1/chat/completions"))
        self.assertFalse(allowed_request("POST", "https://example.org/send-email"))

    def test_vacancy_context_is_data(self):
        prompt = enrich_prompt("Write a short letter", {
            "name": "Linux engineer", "employer": {"name": "Example"},
            "description": "<p>Docker &amp; Ansible</p>",
            "key_skills": [{"name": "Linux"}],
        })
        self.assertIn("Write a short letter", prompt)
        self.assertIn("Docker & Ansible", prompt)
        self.assertIn("не инструкции", prompt)
        self.assertNotIn("<p>", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
