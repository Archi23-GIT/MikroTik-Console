import argparse
import asyncio
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import mikrotik_language


spec = importlib.util.spec_from_file_location(
    "builder", Path(__file__).with_name("mikrotik-config-builder.py")
)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class BuilderTests(unittest.TestCase):
    def test_windows_broadcast_interfaces_and_numeric_scope(self):
        result = SimpleNamespace(returncode=0, stdout=json.dumps([
            {"Address": "192.168.99.10", "Prefix": 24, "Scope": 12},
            {"Address": "127.0.0.1", "Prefix": 8, "Scope": 1},
        ]))
        with patch.object(builder.sys, "platform", "win32"), \
                patch.object(builder.subprocess, "run", return_value=result) as run:
            self.assertEqual(builder._local_ipv4_broadcast_interfaces(),
                             [("12", "192.168.99.10", "192.168.99.255")])
            self.assertEqual(run.call_args.args[0][0], "powershell.exe")

    def test_macos_hex_netmask(self):
        result = SimpleNamespace(returncode=0, stdout=(
            "en7: flags=123\n\tinet 192.168.99.10 netmask 0xffffff00 broadcast 192.168.99.255\n"
        ))
        with patch.object(builder.sys, "platform", "darwin"), \
                patch.object(builder.subprocess, "run", return_value=result):
            self.assertEqual(builder._local_ipv4_broadcast_interfaces(),
                             [("en7", "192.168.99.10", "192.168.99.255")])

    def scan(self, *, ipv6, network, replies, discovered=None, language="pl"):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(
                ipv6=ipv6, network=network, output=Path(directory) / "devices.json",
                force=True, username="admin", ssl=False, insecure=False,
                workers=4, max_hosts=20, port=8728, timeout=1,
            )
            args.output.write_text('{"previous": true}')
            output = io.StringIO()

            async def discover_one(address, *unused, **kwargs):
                identity = replies.get(address, "")
                return address, identity, "" if identity else "timeout", {}

            def discover_neighbors():
                self.assertIn("Przygotowanie" if language == "pl" else "Preparing", output.getvalue())
                return discovered or {}

            with patch.object(mikrotik_language, "LANGUAGE", language), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), \
                    patch.object(builder.getpass, "getpass", return_value="test"), \
                    patch.object(builder, "discover_one", discover_one), \
                    patch.object(builder, "discover_ipv6_neighbors", discover_neighbors):
                code = asyncio.run(builder.run(args))
            return code, json.loads(args.output.read_text()), output.getvalue()

    def test_ipv4_results_and_duplicate_identity(self):
        code, config, output = self.scan(
            ipv6=False, network="10.0.138.0/29",
            replies={"10.0.138.2": "S02-R1", "10.0.138.3": "S02-R1",
                     "10.0.138.4": "S03-R1"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(config["devices"], [
            {"name": "S02-R1", "host": "10.0.138.2"},
            {"name": "S03-R1", "host": "10.0.138.4"},
        ])
        self.assertIn("Pominięto 1", output)
        self.assertIn("3 z 6", output)
        self.assertNotIn("timeout", output)
        self.assertNotIn("[10.0.138.", output)

    def test_automatic_ipv6_excludes_ipv4_bootstrap(self):
        code, config, _ = self.scan(
            ipv6=True, network=None,
            discovered={"10.0.138.2": "en7", "fe80::2%en7": "en7",
                        "fe80::3%en7": "en7"},
            replies={"10.0.138.2": "S02-R1", "fe80::2%en7": "S02-R1",
                     "fe80::3%en7": "S02-R1"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(config["devices"], [{"name": "S02-R1", "host": "fe80::2%en7"}])

    def test_explicit_ipv6_network(self):
        code, config, output = self.scan(
            ipv6=True, network="fd00::/126", replies={"fd00::2": "S02-R1"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(config["devices"], [{"name": "S02-R1", "host": "fd00::2"}])
        self.assertIn("[fd00::1] timeout", output)

    def test_no_results_preserves_existing_config(self):
        code, config, output = self.scan(ipv6=False, network="10.0.138.0/30", replies={})
        self.assertEqual(code, 1)
        self.assertEqual(config, {"previous": True})
        self.assertNotIn("timeout", output)
        self.assertNotIn("[10.0.138.", output)

    def test_english_scan_keeps_same_configuration(self):
        code, config, output = self.scan(
            ipv6=False, network="10.0.138.0/30",
            replies={"10.0.138.1": "S01-R1"}, language="en",
        )
        self.assertEqual(code, 0)
        self.assertEqual(config["devices"], [{"name": "S01-R1", "host": "10.0.138.1"}])
        self.assertIn("Scanning 10.0.138.0/30", output)
        self.assertIn("Saved 1 devices", output)
        self.assertNotIn("Skanuję", output)
        self.assertIn("1 of 2 addresses checked", output)
        self.assertNotIn("timeout", output)


if __name__ == "__main__":
    unittest.main()
