"""Bounded native WSH fixture: no actual Python/GUI launch, profiles or network.

The actual launcher runs under cscript with only WScript.Shell replaced by a
recorder. Real FileSystemObject resolves disposable Unicode/space-containing
paths. No executable is created, installed or invoked as a fake pythonw.
"""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

SOURCE = Path(__file__).with_name("start-gui.wsf")
RECORDER = '''function record(message) {
    var path = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "launcher-record.txt");
    var stream = fso.OpenTextFile(path, 8, true, -1);
    stream.WriteLine(message);
    stream.Close();
}
var shell = {
    CurrentDirectory: "",
    Popup: function(message, seconds, title, icon) {
        record("POPUP:" + message);
    },
    Run: function(command, style, wait) {
        record("CWD:" + this.CurrentDirectory);
        record("RUN:" + command);
        record("STYLE:" + style + ";WAIT:" + wait);
    }
};'''


class LauncherChecks(unittest.TestCase):
    def test_native_host_echo(self):
        if os.name != "nt":
            self.skipTest("Windows Script Host fixture")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "echo.wsf"
            path.write_text('<job><script language="JScript">WScript.Echo(123);</script></job>', encoding="utf-8")
            result = subprocess.run([os.environ["SystemRoot"] + r"\System32\cscript.exe", "//Nologo", str(path)],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"123\r\n", b""))

    def test_source_is_unicode_xml_with_only_relative_prepared_runtime(self):
        text = SOURCE.read_text(encoding="utf-8")
        self.assertEqual(ET.fromstring(text).find("script").attrib["language"], "JScript")
        self.assertIn("WScript.ScriptFullName", text)
        self.assertIn('"venvs\\\\hh\\\\Scripts\\\\pythonw.exe"', text)
        self.assertIn('shell.Run(quote(pythonw) + " " + quote(gui), 0, false)', text)
        for forbidden in ("powershell", "cmd.exe", "pip ", "http://", "https://", "ExecutionPolicy"):
            self.assertNotIn(forbidden, text)

    def fixture(self, *, runtime=True, gui=True, fail_run=False):
        if os.name != "nt":
            self.skipTest("Windows Script Host fixture")
        cscript = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/cscript.exe"
        if not cscript.is_file():
            self.skipTest("Windows Script Host unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Подготовленный проект 日本語 с пробелами"
            windows = root / "windows"
            windows.mkdir(parents=True)
            pythonw = root / "venvs/hh/Scripts/pythonw.exe"
            if runtime:
                pythonw.parent.mkdir(parents=True)
                pythonw.write_bytes(b"inert fixture; never executed")
            if gui:
                (windows / "gui.py").write_text("# inert fixture; never executed\n", encoding="utf-8")
            text = SOURCE.read_text(encoding="utf-8")
            recorder = RECORDER
            if fail_run:
                recorder = recorder.replace('record("RUN:" + command);',
                                            'throw new Error("synthetic failure, not shown");')
            original = 'var shell = new ActiveXObject("WScript.Shell");'
            self.assertEqual(text.count(original), 1)
            launcher = windows / "start-gui.wsf"
            launcher.write_text(text.replace(original, recorder), encoding="utf-8")
            result = subprocess.run([str(cscript), "//Nologo", str(launcher)],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW)
            record = windows / "launcher-record.txt"
            output = record.read_text(encoding="utf-16") if record.exists() else ""
            errors = (result.stdout + result.stderr).decode("mbcs", errors="replace")
            return result.returncode, output, errors, str(root), str(pythonw), str(windows / "gui.py")

    def test_native_parse_resolves_and_quotes_unicode_paths_without_launching(self):
        code, output, errors, root, pythonw, gui = self.fixture()
        self.assertEqual(code, 0, errors)
        self.assertEqual(errors, "")
        self.assertIn("CWD:" + root, output)
        self.assertIn('RUN:"' + pythonw + '" "' + gui + '"', output)
        self.assertIn("STYLE:0;WAIT:false", output)
        self.assertNotIn("POPUP:", output)

    def test_missing_runtime_has_russian_message_and_cannot_launch(self):
        code, output, errors, *_ = self.fixture(runtime=False)
        self.assertEqual(code, 1, errors)
        self.assertIn("Не найден подготовленный Python", output)
        self.assertIn("Загрузки и установка не выполняются", output)
        self.assertNotIn("RUN:", output)

    def test_missing_entrypoint_has_russian_message_and_cannot_launch(self):
        code, output, errors, *_ = self.fixture(gui=False)
        self.assertEqual(code, 1, errors)
        self.assertIn("Не найден windows", output)
        self.assertNotIn("RUN:", output)

    def test_launch_failure_has_fixed_russian_message(self):
        code, output, errors, *_ = self.fixture(fail_run=True)
        self.assertEqual(code, 1, errors)
        self.assertIn("Не удалось запустить GUI", output)
        self.assertNotIn("synthetic failure", output)


if __name__ == "__main__":
    unittest.main()
