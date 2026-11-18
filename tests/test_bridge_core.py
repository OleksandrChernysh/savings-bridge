#!/usr/bin/env python3
"""Unit and smoke tests for bridge_common, bridge_backends, cli_meter, and CLI entrypoints."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge_backends import load_secrets_env, normalize_ollama_url  # noqa: E402
from bridge_common import append_usage_record  # noqa: E402
from cli_meter import estimate_tokens, infer_model_and_command  # noqa: E402


class TestBridgeCommonAndBackends(unittest.TestCase):
    def test_normalize_ollama_url(self):
        self.assertEqual(normalize_ollama_url("127.0.0.1:11434/"), "http://127.0.0.1:11434")
        self.assertEqual(normalize_ollama_url("https://ollama.local/"), "https://ollama.local")

    def test_load_secrets_env_respects_existing_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "secrets.env"
            env_file.write_text(
                '# comment\nexport SB_TEST_NEW="abc123"\nSB_TEST_EXISTING=\'from_file\'\n',
                encoding="utf-8",
            )
            os.environ.pop("SB_TEST_NEW", None)
            os.environ["SB_TEST_EXISTING"] = "from_env"
            try:
                load_secrets_env(str(env_file))
                self.assertEqual(os.environ.get("SB_TEST_NEW"), "abc123")
                self.assertEqual(os.environ.get("SB_TEST_EXISTING"), "from_env")
            finally:
                os.environ.pop("SB_TEST_NEW", None)
                os.environ.pop("SB_TEST_EXISTING", None)

    def test_append_usage_record_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "local_tokens.jsonl"
            append_usage_record("ask", "qwen2.5:7b", 120, 45, 0.456, log_path=str(log_path))
            lines = log_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            rec = json.loads(lines[0])
            self.assertEqual(rec["command"], "ask")
            self.assertEqual(rec["model"], "qwen2.5:7b")
            self.assertEqual(rec["output_tokens"], 120)
            self.assertEqual(rec["input_tokens"], 45)
            self.assertEqual(rec["duration_sec"], 0.46)


class TestCliMeter(unittest.TestCase):
    def test_estimate_tokens_strips_ansi(self):
        raw = b"\x1b[32mhello world!\x1b[0m"
        self.assertEqual(estimate_tokens(raw, 4.0), 3)

    def test_infer_model_and_command(self):
        model, cmd = infer_model_and_command("ollama", ["run", "qwen2.5:7b"], {})
        self.assertEqual(model, "qwen2.5:7b")
        self.assertEqual(cmd, "run")

        model2, cmd2 = infer_model_and_command("opencode", ["run", "hi"], {})
        self.assertEqual(model2, "opencode/cli")
        self.assertEqual(cmd2, "cli")


class TestCliHelpSmoke(unittest.TestCase):
    def test_scripts_help_and_setup_dry_run(self):
        for script in ("local_bridge.py", "token_stats.py", "cli_meter.py", "metrics_server.py"):
            proc = subprocess.run(
                [sys.executable, str(ROOT / script), "--help"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, f"{script} --help failed: {proc.stderr}")

        setup_proc = subprocess.run(
            [sys.executable, str(ROOT / "setup.py"), "--dry-run", "--skip-keys"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(setup_proc.returncode, 0, setup_proc.stderr)
        self.assertIn("bridge_common.py", setup_proc.stdout)


if __name__ == "__main__":
    unittest.main()
