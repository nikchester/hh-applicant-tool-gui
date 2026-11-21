"""Bounded native cmd.exe fixture: no actual Python/GUI launch, profiles or network.

The actual launcher runs under cmd.exe with only its `start` command replaced
by a recorder. Real cmd.exe resolves disposable Unicode/space/ampersand paths.
No executable is created, installed or invoked as a fake pythonw.
"""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).with_name("start-gui.cmd")
START = 'start "" /D "%ROOT%" "%PYTHONW%" "%GUI%"'
# chcp makes the record UTF-8 from the next line on (cmd picks up a codepage change per line);
# echo then writes the start arguments exactly as the source has them.
RECORDER = 'chcp 65001 >nul\r\n>>"%ROOT%\\windows\\launcher-record.txt" echo START:"" /D "%ROOT%" "%PYTHONW%" "%GUI%"'
FAILURE = '"%ComSpec%" /d /c exit 1'


class LauncherChecks(unittest.TestCase):
    def test_source_is_crlf_batch_with_only_relative_prepared_runtime(self):
        data = SOURCE.read_bytes()
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"), "a BOM breaks the first batch line")
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"), "LF-only lines break batch labels")
        text = data.decode("utf-8")
        # cmd reads lines in the OEM codepage until :utf8 switches it, so the launch path stays ASCII.
        self.assertTrue(text.split(":no_python", 1)[0].isascii())
        self.assertIn('set "PYTHONW=%ROOT%\\venvs\\hh\\Scripts\\pythonw.exe"', text)
        self.assertEqual(text.count(START + " || goto launch_failed"), 1)
        lowered = text.lower()
        for forbidden in ("powershell", "wscript", "cscript", "mshta", "curl", "bitsadmin", "pip ",
                          "http://", "https://", "executionpolicy"):
            self.assertNotIn(forbidden, lowered)

    def fixture(self, *, runtime=True, gui=True, fail_run=False):
        if os.name != "nt":
            self.skipTest("cmd.exe fixture")
        comspec = os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Подготовленный проект 日本語 с пробелами & (x86)"
            windows = root / "windows"
            windows.mkdir(parents=True)
            pythonw = root / "venvs/hh/Scripts/pythonw.exe"
            if runtime:
                pythonw.parent.mkdir(parents=True)
                pythonw.write_bytes(b"inert fixture; never executed")
            if gui:
                (windows / "gui.py").write_text("# inert fixture; never executed\n", encoding="utf-8")
            text = SOURCE.read_bytes().decode("utf-8")
            self.assertEqual(text.count(START), 1)
            launcher = windows / "start-gui.cmd"
            launcher.write_bytes(text.replace(START, FAILURE if fail_run else RECORDER).encode("utf-8"))
            # The same quoting Explorer uses for a double-clicked .cmd file.
            result = subprocess.run(f'"{comspec}" /d /s /c ""{launcher}""',
                stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW)
            record = windows / "launcher-record.txt"
            output = record.read_text(encoding="utf-8") if record.exists() else ""
            # Russian messages come after "chcp 65001", so the console writes them as UTF-8.
            console = result.stdout.decode("utf-8", errors="replace")
            errors = result.stderr.decode("mbcs", errors="replace")
            return result.returncode, output, console, errors, str(root), str(pythonw), str(windows / "gui.py")

    def test_native_parse_resolves_and_quotes_unicode_paths_without_launching(self):
        code, output, console, errors, root, pythonw, gui = self.fixture()
        self.assertEqual(code, 0, console + errors)
        self.assertEqual(errors, "")
        self.assertEqual(console, "")
        self.assertEqual(output.strip(), f'START:"" /D "{root}" "{pythonw}" "{gui}"')

    def test_missing_runtime_has_russian_message_and_cannot_launch(self):
        code, output, console, errors, *_ = self.fixture(runtime=False)
        self.assertEqual(code, 1, console + errors)
        self.assertIn("Не найден подготовленный Python", console)
        self.assertIn("Загрузки и установка не выполняются", console)
        self.assertEqual(output, "")

    def test_missing_entrypoint_has_russian_message_and_cannot_launch(self):
        code, output, console, errors, *_ = self.fixture(gui=False)
        self.assertEqual(code, 1, console + errors)
        self.assertIn("Не найден windows\\gui.py", console)
        self.assertEqual(output, "")

    def test_launch_failure_has_fixed_russian_message(self):
        code, output, console, errors, *_ = self.fixture(fail_run=True)
        self.assertEqual(code, 1, console + errors)
        self.assertIn("Не удалось запустить GUI", console)
        self.assertEqual(output, "")


if __name__ == "__main__":
    unittest.main()
