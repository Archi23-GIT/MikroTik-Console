import contextlib
import importlib.util
import io
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import mikrotik_language as language


class LanguageTests(unittest.TestCase):
    def load_script(self, filename):
        spec = importlib.util.spec_from_file_location(
            filename.replace("-", "_").replace(".py", "") + "_cli_test",
            Path(__file__).with_name(filename),
        )
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {spec.name: module}):
            spec.loader.exec_module(module)
        return module

    def detect(self, platform, *, env=None, mac_languages=None, windows_id=None):
        result = SimpleNamespace(returncode=0, stdout=mac_languages or "")
        windows = SimpleNamespace(kernel32=SimpleNamespace(
            GetUserDefaultUILanguage=lambda: windows_id,
        ))
        with patch.object(language.sys, "platform", platform), \
                patch.dict(language.os.environ, env or {}, clear=True), \
                patch.object(language.locale, "getlocale", return_value=(None, None)), \
                patch.object(language.subprocess, "run", return_value=result), \
                patch.object(language.ctypes, "windll", windows, create=True):
            return language.detect_language()

    def test_macos_first_preferred_language(self):
        self.assertEqual(self.detect("darwin", mac_languages='(\n "pl-PL",\n "en-US"\n)'), "pl")
        self.assertEqual(self.detect("darwin", mac_languages='(\n "en-US",\n "pl-PL"\n)', env={"LANG": "pl_PL.UTF-8"}), "en")

    def test_windows_ui_language(self):
        self.assertEqual(self.detect("win32", windows_id=0x0415), "pl")
        self.assertEqual(self.detect("win32", windows_id=0x0409, env={"LANG": "pl_PL.UTF-8"}), "en")

    def test_linux_language_settings(self):
        for env in ({"LANG": "pl_PL.UTF-8"}, {"LANGUAGE": "pl:en"},
                    {"LC_MESSAGES": "pl.UTF-8"}, {"LC_ALL": "pl_PL.UTF-8", "LANG": "en_US.UTF-8"}):
            with self.subTest(env=env):
                self.assertEqual(self.detect("linux", env=env), "pl")
        self.assertEqual(self.detect("linux", env={"LANGUAGE": "en:pl", "LANG": "pl_PL.UTF-8"}), "en")
        self.assertEqual(self.detect("linux", env={"LC_ALL": "C", "LANGUAGE": "pl"}), "en")

    def test_unknown_language_defaults_to_english(self):
        for platform in ("darwin", "win32", "linux"):
            self.assertEqual(self.detect(platform), "en")
        self.assertEqual(self.detect("linux", env={"LANG": "de_DE.UTF-8"}), "en")

    def test_macos_detection_timeout(self):
        with patch.object(language.sys, "platform", "darwin"), \
                patch.dict(language.os.environ, {}, clear=True), \
                patch.object(language.locale, "getlocale", return_value=(None, None)), \
                patch.object(language.subprocess, "run", side_effect=subprocess.TimeoutExpired("defaults", 2)):
            self.assertEqual(language.detect_language(), "en")

    def test_locale_fallback(self):
        with patch.object(language.sys, "platform", "linux"), \
                patch.dict(language.os.environ, {}, clear=True), \
                patch.object(language.locale, "getlocale", return_value=("pl_PL", "UTF-8")):
            self.assertEqual(language.detect_language(), "pl")

    def test_console_help_and_errors_in_both_languages(self):
        spec = importlib.util.spec_from_file_location(
            "console_language_test", Path(__file__).with_name("mikrotik-console.py")
        )
        console = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {spec.name: console}):
            spec.loader.exec_module(console)
        for selected, heading, error in (
            ("pl", "Polecenia konsoli", "Niepoprawne cudzysłowy"),
            ("en", "Console commands", "Invalid quoting"),
        ):
            with self.subTest(language=selected), patch.object(language, "LANGUAGE", selected):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    console.print_help()
                self.assertIn(heading, output.getvalue())
                with self.assertRaisesRegex(ValueError, error):
                    console.parse_command('/system identity print name="')

    def test_language_override_applies_to_help_in_both_scripts(self):
        for filename in ("mikrotik-config-builder.py", "mikrotik-console.py"):
            script = self.load_script(filename)
            for selected, detected, expected in (
                ("en", "pl", "message language"),
                ("pl", "en", "język komunikatów"),
            ):
                with self.subTest(script=filename, language=selected), \
                        patch.object(language, "LANGUAGE", detected), \
                        patch.object(sys, "argv", [filename, "--help", "--lang", selected]):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as result:
                        script.main()
                    self.assertEqual(result.exception.code, 0)
                    self.assertIn(expected, output.getvalue())

    def test_exact_ipv6_command_with_english_override(self):
        script = self.load_script("mikrotik-config-builder.py")
        with patch.object(language, "LANGUAGE", "pl"), \
                patch.object(sys, "argv", ["mikrotik-config-builder.py", "--ipv6", "--lang", "en"]), \
                patch.object(script, "discover_ipv6_neighbors", return_value={}):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                self.assertEqual(script.main(), 1)
            self.assertIn("Preparing device scan", output.getvalue())
            self.assertIn("No IPv6 addresses found", output.getvalue())
            self.assertNotIn("unrecognized arguments", output.getvalue())

    def test_console_runtime_override(self):
        script = self.load_script("mikrotik-console.py")

        async def run(args):
            self.assertEqual(args.lang, "en")
            self.assertEqual(language.tr("polski", "English"), "English")
            return 0

        with patch.object(language, "LANGUAGE", "pl"), \
                patch.object(sys, "argv", ["mikrotik-console.py", "--lang", "en"]), \
                patch.object(script, "run", run):
            self.assertEqual(script.main(), 0)

    def test_omitted_override_keeps_detected_language(self):
        with patch.object(language, "LANGUAGE", "pl"), \
                patch.object(sys, "argv", ["mikrotik-console.py"]):
            language.configure_language()
            self.assertEqual(language.LANGUAGE, "pl")

    def test_invalid_language_is_rejected(self):
        with patch.object(sys, "argv", ["mikrotik-console.py", "--lang", "de"]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
            language.configure_language()
        self.assertEqual(result.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
