#!/usr/bin/env python3
"""Extended unit tests for fallback routing, host env vars, setup migration/Windows, metrics, and discovery."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "src"
for p in (str(ROOT), str(SRC_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import bridge_backends  # noqa: E402
import bridge_common  # noqa: E402
import metrics_server  # noqa: E402
import model_discovery  # noqa: E402
import setup as sb_setup  # noqa: E402


class TestHostHelpers(unittest.TestCase):
    def test_ollama_and_lmstudio_host_env(self):
        with mock.patch.dict(os.environ, {"OLLAMA_HOST": "10.0.0.5:11434/", "LM_STUDIO_HOST": "https://lms.local/"}):
            self.assertEqual(bridge_common.get_ollama_host(), "http://10.0.0.5:11434")
            self.assertEqual(bridge_common.get_lmstudio_host(), "https://lms.local")


class TestFallbackChain(unittest.TestCase):
    def test_fallback_from_opencode_to_local(self):
        calls: list[str] = []

        def fake_single(command, model, prompt, system, schema, num_ctx, temperature, timeout, raise_on_error):
            calls.append(model)
            if model.startswith("opencode/"):
                raise bridge_backends.BackendError("429 rate limit", retryable=True)
            return "ok from fallback"

        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "", "NVIDIA_API_KEY": ""}, clear=False):
            with mock.patch.object(bridge_backends, "resolve_model", return_value="qwen2.5:7b"):
                with mock.patch.object(bridge_backends, "_query_single_backend", side_effect=fake_single):
                    res = bridge_backends.query_backend("ask", "opencode/deepseek-v4.1-flash-free", "hi")

        self.assertEqual(res, "ok from fallback")
        self.assertEqual(calls, ["opencode/deepseek-v4.1-flash-free", "qwen2.5:7b"])


class TestSetupMigrationAndWindows(unittest.TestCase):
    def test_migrate_legacy_logs_merges_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            legacy = tdir / "legacy" / "local_tokens.jsonl"
            target = tdir / "new" / "local_tokens.jsonl"
            legacy.parent.mkdir(parents=True)
            target.parent.mkdir(parents=True)

            legacy.write_text('{"id": 1}\n{"id": 2}\n', encoding="utf-8")
            target.write_text('{"id": 2}\n{"id": 3}\n', encoding="utf-8")

            dry_count = sb_setup.migrate_legacy_logs(dry_run=True, legacy_path=legacy, target_path=target)
            self.assertEqual(dry_count, 1)
            self.assertTrue(legacy.exists())

            added = sb_setup.migrate_legacy_logs(dry_run=False, legacy_path=legacy, target_path=target)
            self.assertEqual(added, 1)
            self.assertFalse(legacy.exists())
            self.assertTrue(legacy.with_suffix(".jsonl.migrated.bak").exists())
            self.assertTrue(target.with_suffix(".jsonl.bak").exists())
            lines = target.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(lines, ['{"id": 2}', '{"id": 3}', '{"id": 1}'])

    def test_windows_install_creates_cmd_wrappers(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            with mock.patch.object(Path, "home", return_value=fake_home):
                with mock.patch("platform.system", return_value="Windows"):
                    sb_setup.install_files(dry_run=False)
            bin_dir = fake_home / ".config" / "savings-bridge" / "bin"
            self.assertTrue((bin_dir / "savings-bridge.cmd").exists(), "Missing savings-bridge.cmd")
            for legacy in ("local-bridge.cmd", "savings-stats.cmd"):
                self.assertFalse((bin_dir / legacy).exists(), f"Legacy {legacy} should not be created")


class TestMetricsAndDiscovery(unittest.TestCase):
    def test_build_summary_totals(self):
        cloud = {"2026-11-18": {"inp": 1000, "out": 200, "cache": 0, "steps": 1, "convs": {"c1"}}}
        local = {
            "2026-11-18": {
                "calls": 2,
                "local_out": 800,
                "local_inp": 100,
                "duration": 1.2,
                "commands": {"ask": 1, "code": 1},
                "cmd_out": {"ask": 300, "code": 500},
                "cmd_inp": {"ask": 40, "code": 60},
            }
        }
        with mock.patch.object(metrics_server, "load_cloud_stats", return_value=cloud):
            with mock.patch.object(metrics_server, "load_local_stats", return_value=local):
                summary = metrics_server.build_summary(days=10)

        self.assertEqual(summary["total"]["local_output_tokens"], 800)
        self.assertEqual(summary["total"]["saved_output_pct"], 80.0)
        self.assertEqual(summary["chat_savings"]["output_tokens"], 300)

    def test_recommend_for_ram_tiers(self):
        self.assertEqual(model_discovery.recommend_for_ram(8.0)["coder"], "qwen2.5-coder:3b")
        self.assertEqual(model_discovery.recommend_for_ram(16.0)["coder"], "qwen2.5-coder:7b")
        self.assertEqual(model_discovery.recommend_for_ram(32.0)["coder"], "qwen2.5-coder:14b")


if __name__ == "__main__":
    unittest.main()
