"""Offline checks for the Windows deployment and safe preview wrapper."""

from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch, PropertyMock
import tomllib

from hh_applicant_tool.tool import HHApplicantTool


ROOT = Path(__file__).resolve().parents[1]
from preview_hh import run
from preview import arguments  # noqa: E402
from list_resumes import allowed_request as resume_request_allowed, published_resumes  # noqa: E402
import requests
from types import SimpleNamespace
from resume_context import facts_from_resume, selected_resume_context


# One-time move of pre-rename PocketNode profile folders, before any profile use.
MIGRATION = "& $python -I (Join-Path $PSScriptRoot 'profile_migration.py')"

class WindowsChecks(unittest.TestCase):
    def test_conditional_junior_prompt_preserves_closing(self):
        config = tomllib.loads((ROOT / 'windows/config.toml').read_text(encoding='utf-8'))
        template = tomllib.loads((ROOT / 'windows/config.example.toml').read_text(encoding='utf-8'))
        prompt = config['general']['cover_letter_prompt']
        self.assertTrue(prompt.startswith(template['general']['cover_letter_prompt'].rstrip('\n')))
        self.assertIn('Только если вакансия явно обозначена как Senior или Lead', prompt)
        self.assertIn('Если у тебя есть позиция джун/миддл, готов её рассмотреть', prompt)
        self.assertIn('включая вакансии без указанного уровня и Middle', prompt)
        self.assertIn('не упоминай позицию джун/миддл', prompt)
        self.assertIn('Отсутствие сведений в кратком контексте', prompt)
        self.assertIn('не пиши «нет коммерческого опыта»', prompt)
        self.assertTrue(prompt.rstrip().endswith(config['general']['cover_letter_closing']))
        rendered = prompt % {'vacancy_name': 'DevOps', 'employer_name': 'Example',
                             'resume_title': 'DevOps-инженер'}
        self.assertIn('Вакансия: DevOps. Компания: Example.', rendered)

    def test_bootstrap_uses_local_files(self):
        source = (ROOT / 'windows/bootstrap.ps1').read_text(encoding='utf-8')
        for name in ('requirements-hh.txt', 'requirements-chatmock.txt',
                     'config.toml', 'check_preview.py', 'check_chatmock.py',
                     'check_windows.py', 'check_send.py', 'check_reply_chats.py'):
            with self.subTest(name=name):
                self.assertTrue((ROOT / 'windows' / name).is_file())
                self.assertIn('windows\\' + name, source)
        self.assertNotIn('termux', source.lower())

    def test_hh_wrappers_manage_only_their_own_gateway(self):
        helper = (ROOT / 'windows/ensure-chatmock.ps1').read_text(encoding='utf-8')
        self.assertTrue((ROOT / 'windows/chatmock_server.py').is_file())
        self.assertIn("'http://127.0.0.1:8000/health'", helper)
        self.assertIn('if (Test-ChatMockReady)', helper)
        self.assertIn('return $null', helper)  # Pre-existing gateway is not owned.
        self.assertIn("'auth.json'", helper)
        self.assertIn("'127.0.0.1', '--port', '8000'", helper)
        self.assertIn('Stop-ChatMockForHH $gateway -KeepLogs', helper)
        self.assertIn("'-u'", helper)  # Flush diagnostics before the owned process is stopped.
        for name in ('hh-preview.ps1', 'hh-send.ps1', 'hh-reply-chats.ps1'):
            source = (ROOT / 'windows' / name).read_text(encoding='utf-8')
            with self.subTest(name=name):
                self.assertIn("'ensure-chatmock.ps1'", source)
                self.assertLess(source.index(MIGRATION), source.index('Start-ChatMockForHH'))
                run = source.replace(MIGRATION, '')  # Only the HH run must wait for the gateway.
                self.assertLess(run.index('Start-ChatMockForHH'), run.index('& $python'))
                self.assertIn('Stop-ChatMockForHH $ownedGateway', source)
                self.assertIn('$runSucceeded = $false', source)
                self.assertIn('$runSucceeded = $exitCode -eq 0', source)
                self.assertIn('-KeepLogs:(-not $runSucceeded)', source)
                self.assertIn('} finally {', source)
                self.assertIn("'Local\\hh-applicant-tool-gui-run'", source)
                self.assertIn('if ($runLocked) { $runMutex.ReleaseMutex() }', source)

    def test_gateway_log_cleanup_without_real_processes_or_network(self):
        powershell = shutil.which('powershell') or shutil.which('pwsh')
        if not powershell:
            self.skipTest('PowerShell is required for gateway lifecycle checks')
        script = r'''
$ErrorActionPreference = 'Stop'
. (Join-Path $env:HH_CHECK_ROOT 'windows\ensure-chatmock.ps1')
$script:stops = 0
function Stop-Process {
    param($InputObject, $ErrorAction)
    $script:stops++
    $InputObject.HasExited = $true
}
$process = [pscustomobject]@{ HasExited = $false; Disposed = $false }
$process | Add-Member ScriptMethod WaitForExit { param($timeout) return $true }
$process | Add-Member ScriptMethod Dispose { $this.Disposed = $true }
$logs = @((Join-Path $env:HH_CHECK_TEMP 'out.log'), (Join-Path $env:HH_CHECK_TEMP 'err.log'))
$gateway = [pscustomobject]@{ Process = $process; Logs = $logs }
foreach ($log in $logs) { Set-Content -LiteralPath $log -Value 'test diagnostic' }
Stop-ChatMockForHH $gateway -KeepLogs
if ($script:stops -ne 1 -or -not $process.Disposed) { throw 'Owned process was not stopped/disposed' }
foreach ($log in $logs) {
    if (-not (Test-Path -LiteralPath $log)) { throw 'Failure log was removed' }
    if ((Get-Content -LiteralPath $log) -ne 'test diagnostic') { throw 'Failure log changed' }
}
$process.Disposed = $false
Stop-ChatMockForHH $gateway
if ($script:stops -ne 1 -or -not $process.Disposed) { throw 'Exited process handling changed' }
foreach ($log in $logs) {
    if (Test-Path -LiteralPath $log) { throw 'Success log was not removed' }
}
Stop-ChatMockForHH $null -KeepLogs
if ($script:stops -ne 1) { throw 'Unowned process was touched' }
'''
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run(
                [powershell, '-NoProfile', '-NonInteractive', '-Command', script],
                env=dict(os.environ, HH_CHECK_ROOT=str(ROOT), HH_CHECK_TEMP=temp),
                capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count('ChatMock diagnostic log preserved:'), 2)


    def test_local_config_and_profile(self):
        local = Path.home() / '.config/hh-applicant-tool-cli/config.toml'
        source = local if local.exists() else ROOT / 'windows/config.toml'
        config = tomllib.loads(source.read_text(encoding='utf-8'))
        self.assertEqual(config['openai']['default']['base_url'], 'http://127.0.0.1:8000/v1')
        self.assertEqual(config['general']['cover_letter_resume_id'], '9749b651ff056b6c970039ed1f5a63765a5178')
        target = Path.home() / '.config/hh-applicant-tool-cli'
        with patch.dict(os.environ, {'CONFIG_DIR': str(target), 'HH_PROFILE_ID': '.'}):
            with patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden')):
                self.assertEqual(HHApplicantTool().config_dir_path, target.resolve())

    def test_preview_flags_and_no_passthrough(self):
        source = (ROOT / 'windows/hh-preview.ps1').read_text(encoding='utf-8')
        self.assertIn("$env:CONFIG_DIR = $config", source)
        self.assertIn("$env:HH_PROFILE_ID = '.'", source)
        self.assertIn('$env:HH_APPLICANT_TOOL_SEARCH = Resolve-HHSearchText', source)
        self.assertIn("$env:HH_APPLICANT_TOOL_RESUME_ID = $ResumeId", source)
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
        self.assertIn('--no-skip-tests', argv)
        self.assertIn('--no-only-with-salary', argv)
        self.assertNotIn('--only-with-salary', argv)
        self.assertIn('--no-handle-captcha', argv)
        self.assertIn('REMOTE', argv)
        self.assertIn('80000', argv)
        self.assertEqual(argv[argv.index('--per-page') + 1], '10')  # Up to 10 vacancies for 3 letters.
        self.assertIn('1', argv)

    def test_authorization_selects_dedicated_profile(self):
        source = (ROOT / 'windows/hh-authorize.ps1').read_text(encoding='utf-8')
        self.assertIn('$env:CONFIG_DIR =', source)
        self.assertIn("$env:HH_PROFILE_ID = '.'", source)
        self.assertIn('--no-handle-captcha --manual-captcha authorize', source)
        self.assertIn("& $python (Join-Path $repo 'windows\\authorize.py') --config-dir ", source)

    def test_wrappers_avoid_relocation_sensitive_console_launchers(self):
        # pip console-script launchers embed the venv's original absolute path.
        import re
        launcher = re.compile(r'Scripts[\\/]+([\w.-]+\.exe)', re.IGNORECASE)
        for path in sorted((ROOT / 'windows').glob('*.ps1')):
            names = launcher.findall(path.read_text(encoding='utf-8'))
            with self.subTest(path=path.name):
                self.assertEqual(
                    [name for name in names if name.lower() not in ('python.exe', 'pythonw.exe')],
                    [],
                )

    def test_chat_reply_wrapper_uses_shared_gateway_and_lock(self):
        source = (ROOT / 'windows/hh-reply-chats.ps1').read_text(encoding='utf-8')
        self.assertIn("'ensure-chatmock.ps1'", source)
        self.assertIn("'Local\\hh-applicant-tool-gui-run'", source)
        self.assertLess(source.index(MIGRATION), source.index('Start-ChatMockForHH'))
        run = source.replace(MIGRATION, '')  # Only the HH run must wait for the gateway.
        self.assertLess(run.index('Start-ChatMockForHH'), run.index('& $python'))
        self.assertIn('Stop-ChatMockForHH $ownedGateway', source)
        self.assertIn('if ($runLocked) { $runMutex.ReleaseMutex() }', source)
        self.assertIn("'windows\\reply_chats.py'", source)
        self.assertNotIn('ResumeId', source)
        wrapper = (ROOT / 'windows/hh-preview.ps1').read_text(encoding='utf-8')
        self.assertIn("'select-resume.ps1'", wrapper)
        self.assertIn('if (-not $ResumeId)', wrapper)
        self.assertLess(wrapper.index('Select-HHResume -Python'),
                        wrapper.index('Start-ChatMockForHH'))
        send_wrapper = (ROOT / 'windows/hh-send.ps1').read_text(encoding='utf-8')
        self.assertIn("'select-resume.ps1'", send_wrapper)
        self.assertIn("$env:HH_APPLICANT_TOOL_ALL = '1'", send_wrapper)
        self.assertLess(send_wrapper.index('Resolve-HHSearchText -Repo'),
                        send_wrapper.index('Start-ChatMockForHH'))
        chooser = (ROOT / 'windows/select-resume.ps1').read_text(encoding='utf-8')
        self.assertIn('windows\\list_resumes.py', chooser)
        self.assertIn("'Resume number'", chooser)
        self.assertIn("'^[1-9][0-9]*$'", chooser)

    def test_each_resume_has_distinct_search_text(self):
        searches = json.loads((ROOT / 'windows/resume-search.json').read_text(encoding='utf-8'))
        self.assertEqual(set(searches), {
            '9749b651ff056b6c970039ed1f5a63765a5178',
            '27f3daa7ff10eda2e50039ed1f68686c526532',
            'e7582ba1ff10eb7d2d0039ed1f537759493367',
            'bf283d89ff10eb7fc80039ed1f757134445075',
            '6032c534ff10f616b30039ed1f32437561744e',
        })
        self.assertEqual(len(set(searches.values())), len(searches))
        for resume_id, search in searches.items():
            with self.subTest(resume_id=resume_id):
                self.assertTrue(search.strip())
                self.assertEqual(search.count('"') % 2, 0)
                self.assertEqual(arguments('C:/profile', resume_id, search)[-1], '--search=' + search)

    def test_resume_list_read_only_and_published(self):
        records = [
            {'id': 'first', 'title': 'DevOps-инженер', 'status': {'id': 'published'}},
            {'id': 'second', 'title': 'Черновик', 'status': {'id': 'not_published'}},
            {'id': 'third', 'title': 'SRE-инженер', 'status': {'id': 'published'}},
        ]
        self.assertEqual(published_resumes(records), [
            {'id': 'first', 'title': 'DevOps-инженер'},
            {'id': 'third', 'title': 'SRE-инженер'},
        ])
        get = requests.Request('GET', 'https://api.hh.ru/resumes/mine').prepare()
        refresh = requests.Request('POST', 'https://api.hh.ru/token',
                                   data={'grant_type': 'refresh_token'}).prepare()
        self.assertTrue(resume_request_allowed(get))
        self.assertTrue(resume_request_allowed(refresh))
        for request in (
            requests.Request('GET', 'https://api.hh.ru/resumes/first').prepare(),
            requests.Request('POST', 'https://api.hh.ru/negotiations').prepare(),
            requests.Request('POST', 'https://api.hh.ru/token',
                             data={'grant_type': 'authorization_code'}).prepare(),
            requests.Request('GET', 'https://evil.example/resumes/mine').prepare(),
        ):
            with self.subTest(url=request.url):
                self.assertFalse(resume_request_allowed(request))

    def test_selected_resume_replaces_only_letter_facts_in_memory(self):
        resume_id = 'another-published-resume'
        resume = {
            'id': resume_id, 'title': 'SRE-инженер',
            'skill_set': ['Linux', 'me@example.org'],
            'experience': [{'position': 'Инженер мониторинга', 'start': '2025-01-01',
                            'end': None, 'description': 'Private details and contacts'}],
        }
        facts = facts_from_resume(resume)
        self.assertNotIn('me@example.org', str(facts))
        self.assertNotIn('Private details', str(facts))
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp)
            shutil.copyfile(ROOT / 'windows/config.toml', profile / 'config.toml')
            original = (profile / 'config.toml').read_bytes()
            with patch.dict(os.environ, {'CONFIG_DIR': str(profile), 'HH_PROFILE_ID': '.'}), \
                 patch.object(HHApplicantTool, 'get_resumes', return_value=[
                     {'id': resume_id, 'status': {'id': 'published'}}
                 ]), \
                 patch.object(HHApplicantTool, 'api_client', new_callable=PropertyMock,
                              return_value=SimpleNamespace(get=lambda _: resume)):
                with selected_resume_context(resume_id, 'configured-devops'):
                    settings = HHApplicantTool().get_openai_config('openai.cover_letter')
                    self.assertIn('SRE-инженер', str(settings['history']))
                    self.assertNotIn('170+ сайтов', str(settings['history']))
                    self.assertEqual(settings['base_url'], 'http://127.0.0.1:8000/v1')
                with self.assertRaisesRegex(SystemExit, 'not published'):
                    with patch.object(HHApplicantTool, 'get_resumes', return_value=[]):
                        with selected_resume_context(resume_id, 'configured-devops'):
                            pass
            self.assertEqual((profile / 'config.toml').read_bytes(), original)

    def test_preview_accepts_checked_config_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            target = home / '.config/hh-applicant-tool-cli'
            target.mkdir(parents=True)
            shutil.copyfile(ROOT / 'windows/config.toml', target / 'config.toml')
            args = [
                '--config-dir', str(target), '--profile-id', '.', '--no-handle-captcha',
                'apply', '--dry-run', '--no-send-email', '--no-ai-filter', '--no-skip-tests',
                '--per-page', '3', '--total-pages', '1', '--max-responses', '1',
                '--work-format', 'REMOTE', '--currency', 'RUR', '--salary', '80000',
                '--no-only-with-salary', '--search-field', 'name',
                '--order-by', 'publication_time',
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
