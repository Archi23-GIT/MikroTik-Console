import builtins
import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import mikrotik_language


class ConsolePortabilityTests(unittest.TestCase):
    def load_without_readline(self):
        original_import = builtins.__import__

        def import_module(name, *args, **kwargs):
            if name == "readline":
                raise ModuleNotFoundError("No module named 'readline'")
            return original_import(name, *args, **kwargs)

        spec = importlib.util.spec_from_file_location(
            "console_without_readline", Path(__file__).with_name("mikrotik-console.py")
        )
        console = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {spec.name: console}), \
                patch.object(builtins, "__import__", import_module):
            spec.loader.exec_module(console)
        return console

    def test_import_help_and_completion_without_readline(self):
        console = self.load_without_readline()
        self.assertIsNone(console.readline)
        console.setup_completion(["router"])
        with patch.object(sys, "argv", ["mikrotik-console.py", "--help"]), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as result:
            console.main()
        self.assertEqual(result.exception.code, 0)
        self.assertEqual(console.parse_command("/system identity print"), ["/system/identity/print"])

    def test_completion_still_works_with_readline(self):
        console = self.load_without_readline()
        readline = MagicMock()
        readline.get_line_buffer.return_value = ":only rou"
        with patch.object(console, "readline", readline):
            console.setup_completion(["router"])
            complete = readline.set_completer.call_args.args[0]
            self.assertEqual(complete(":only rou", 0), ":only router")
            self.assertIsNone(complete(":only rou", 1))

    def test_missing_module_shows_windows_installation_hint(self):
        console = self.load_without_readline()
        for language in ("pl", "en"):
            with self.subTest(language=language), \
                    patch.object(console.sys, "platform", "win32"), \
                    patch.object(mikrotik_language, "LANGUAGE", language):
                output = io.StringIO()
                with contextlib.redirect_stderr(output):
                    console.setup_completion(["router"])
                self.assertIn("py -m pip install --upgrade pyreadline3", output.getvalue())
                self.assertIn("uruchom konsolę ponownie" if language == "pl" else "restart the console", output.getvalue())


if __name__ == "__main__":
    unittest.main()
