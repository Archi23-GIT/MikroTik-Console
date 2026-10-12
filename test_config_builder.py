import argparse
import asyncio
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mikrotik_language


spec = importlib.util.spec_from_file_location(
    "builder", Path(__file__).with_name("mikrotik-config-builder.py")
)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class BuilderTests(unittest.TestCase):
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
        code, config, _ = self.scan(
            ipv6=True, network="fd00::/126", replies={"fd00::2": "S02-R1"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(config["devices"], [{"name": "S02-R1", "host": "fd00::2"}])

    def test_no_results_preserves_existing_config(self):
        code, config, _ = self.scan(ipv6=False, network="10.0.138.0/30", replies={})
        self.assertEqual(code, 1)
        self.assertEqual(config, {"previous": True})

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


if __name__ == "__main__":
    unittest.main()
