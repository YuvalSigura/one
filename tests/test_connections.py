import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from one_mcp.connections import configurations, emit
from one_mcp.safety import validate_paths


class ConnectionsTests(unittest.TestCase):
    def test_configurations_are_parseable_and_share_db(self):
        examples = configurations(python_path="C:/Example/Python/python.exe", db_path="C:/Example/one.db")
        claude = json.loads(examples["claude.mcp.json"])["mcpServers"]["one"]
        grok = tomllib.loads(examples["grok.config.toml"])["mcp_servers"]["one"]
        codex = tomllib.loads(examples["codex.config.toml"])["mcp_servers"]["one"]
        for conf in (claude, grok, codex):
            self.assertEqual(conf["args"], ["-m", "one_mcp.server"])
            self.assertTrue(conf["command"].endswith("python.exe"))
            self.assertTrue(conf["env"]["ONE_DB"].endswith("one.db"))

    def test_emit_never_overwrites(self):
        with tempfile.TemporaryDirectory() as root:
            output = emit(root)
            self.assertEqual(len(output["written"]), 3)
            with self.assertRaises(FileExistsError):
                emit(root)

    def test_protected_paths(self):
        for path in (".runtime", ".runtime/engagement", ".git/config", "configs/.env.local", "secrets/a"):
            with self.assertRaises(ValueError):
                validate_paths([path])


if __name__ == "__main__":
    unittest.main()
