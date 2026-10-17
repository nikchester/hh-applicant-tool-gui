"""Offline checks for the Windows deployment and safe preview wrapper."""

from pathlib import Path
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import tomllib

from hh_applicant_tool.tool import HHApplicantTool


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'termux'))
from preview_hh import run  # noqa: E402
from preview import arguments  # noqa: E402


class WindowsChecks(unittest.TestCase):
    def test_local_config_and_profile(self):
        local = Path.home() / '.config/pocketnode-hh/config.toml'
        source = local if local.exists() else ROOT / 'termux/config.toml'
        config = tomllib.loads(source.read_text(encoding='utf-8'))
        self.assertEqual(config['openai']['default']['base_url'], 'http://127.0.0.1:8000/v1')
        self.assertEqual(config['general']['cover_letter_resume_id'], '9749b651ff056b6c970039ed1f5a63765a5178')
        target = Path.home() / '.config/pocketnode-hh'
        with patch.dict(os.environ, {'CONFIG_DIR': str(target), 'HH_PROFILE_ID': '.'}):
            with patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden')):
                self.assertEqual(HHApplicantTool().config_dir_path, target.resolve())

    def test_preview_flags_and_no_passthrough(self):
        source = (ROOT / 'windows/hh-preview.ps1').read_text(encoding='utf-8')
        self.assertIn("$env:CONFIG_DIR = $config", source)
        self.assertIn("$env:HH_PROFILE_ID = '.'", source)
        self.assertIn('$env:POCKETNODE_HH_SEARCH = $SearchText', source)
        self.assertIn("$env:POCKETNODE_HH_RESUME_ID = $ResumeId", source)
        self.assertIn("'windows\\preview.py'", source)
        self.assertIn("$env:PYTHONIOENCODING = 'utf-8'", source)
        self.assertIn('[Console]::OutputEncoding =', source)
        self.assertNotIn('@args', source.lower())
        phrase = 'DevOps OR "Linux administrator" OR "infrastructure engineer"'
        argv = arguments('C:/profile', 'resume-id', phrase)
        self.assertEqual(argv[-1], '--search=' + phrase)
        self.assertEqual(argv[-2], '--resume-id=resume-id')
        self.assertIn('--dry-run', argv)
        self.assertIn('--no-send-email', argv)
        self.assertIn('--no-ai-filter', argv)
        self.assertIn('--skip-tests', argv)
        self.assertIn('--no-handle-captcha', argv)
        self.assertIn('REMOTE', argv)
        self.assertIn('80000', argv)
        self.assertIn('3', argv)
        self.assertIn('1', argv)

    def test_authorization_selects_dedicated_profile(self):
        source = (ROOT / 'windows/hh-authorize.ps1').read_text(encoding='utf-8')
        self.assertIn('$env:CONFIG_DIR =', source)
        self.assertIn("$env:HH_PROFILE_ID = '.'", source)
        self.assertIn('--no-handle-captcha authorize', source)

    def test_preview_accepts_checked_config_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            target = home / '.config/pocketnode-hh'
            target.mkdir(parents=True)
            shutil.copyfile(ROOT / 'termux/config.toml', target / 'config.toml')
            args = [
                '--config-dir', str(target), '--profile-id', '.', '--no-handle-captcha',
                'apply', '--dry-run', '--no-send-email', '--no-ai-filter', '--skip-tests',
                '--per-page', '3', '--total-pages', '1', '--max-responses', '1',
                '--work-format', 'REMOTE', '--currency', 'RUR', '--salary', '80000',
                '--only-with-salary', '--search-field', 'name',
                '--resume-id=9749b651ff056b6c970039ed1f5a63765a5178',
                '--search=DevOps',
            ]
            with patch.dict(os.environ, {'CONFIG_DIR': str(target), 'HH_PROFILE_ID': '.'}), \
                 patch('preview_hh.Path.home', return_value=home), \
                 patch('preview_hh.main', return_value=0) as main, \
                 patch('requests.sessions.Session.send', side_effect=AssertionError('Network forbidden')):
                self.assertEqual(run(args), 0)
                main.assert_called_once_with(args)


if __name__ == '__main__':
    unittest.main(verbosity=2)
