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
import bridge_guards  # noqa: E402
import local_bridge  # noqa: E402
import metrics_server  # noqa: E402
import model_discovery  # noqa: E402
import setup as sb_setup  # noqa: E402
import token_stats  # noqa: E402


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
                    with mock.patch.object(sb_setup.subprocess, "run"):
                        sb_setup.install_files(dry_run=False)
            bin_dir = fake_home / ".config" / "savings-bridge" / "bin"
            self.assertTrue((bin_dir / "savings-bridge.cmd").exists(), "Missing savings-bridge.cmd")
            for legacy in ("local-bridge.cmd", "savings-stats.cmd"):
                self.assertFalse((bin_dir / legacy).exists(), f"Legacy {legacy} should not be created")

    def test_configure_keys_merges_and_backs_up_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            secrets_file = Path(tmp) / "secrets.env"
            secrets_file.write_text('export OPENROUTER_API_KEY="old_or_key"\n', encoding="utf-8")
            answers = iter(["", "new_nv_key", "", "", ""])
            with mock.patch.object(sb_setup.getpass, "getpass", side_effect=lambda _: next(answers, "")):
                sb_setup.configure_keys(dry_run=False, secrets_path=secrets_file)
            self.assertTrue(secrets_file.with_suffix(".env.bak").exists())
            content = secrets_file.read_text(encoding="utf-8")
            self.assertIn("old_or_key", content)
            self.assertIn("new_nv_key", content)


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

    def test_local_aliases_never_map_to_cloud_and_exclude_embeddings(self):
        with mock.patch.object(model_discovery, "list_ollama_models", return_value=[]):
            with mock.patch.object(model_discovery, "list_lmstudio_models", return_value=[]):
                with mock.patch.object(model_discovery, "get_ram_gb", return_value=16.0):
                    profile = model_discovery.build_profile()
        self.assertIsNone(profile["aliases"]["coder"])
        self.assertIsNone(profile["aliases"]["fast"])

        models = [
            {"name": "nomic-embed-text:latest", "provider": "ollama", "size_gb": 0.3, "family": "bert"},
            {"name": "deepseek-cloud:latest", "provider": "ollama", "size_gb": 0.0, "family": ""},
            {"name": "qwen2.5:7b", "provider": "ollama", "size_gb": 4.7, "family": "qwen2"},
        ]
        self.assertEqual(model_discovery._pick_best(models, 16.0, prefer_coder=False), "qwen2.5:7b")

        with mock.patch.object(bridge_backends, "_get_dynamic_profile", return_value=profile):
            with self.assertRaises(bridge_backends.BackendError):
                bridge_backends.resolve_model("coder", bridge_backends.DEFAULT_CODER_MODEL, raise_on_error=True)

    def test_stale_or_cloud_cached_profile_triggers_refresh(self):
        cloud_cached = {
            "created_at": 9999999999.0,
            "aliases": {"coder": "opencode/deepseek-v4.1-flash-free", "fast": "qwen2.5:7b"},
        }
        self.assertTrue(bridge_backends._profile_needs_refresh(cloud_cached))
        old_cached = {"created_at": 1000.0, "aliases": {"coder": "qwen2.5-coder:7b", "fast": "qwen2.5:7b"}}
        self.assertTrue(bridge_backends._profile_needs_refresh(old_cached))


class TestTransactionalWritesAndRepair(unittest.TestCase):
    def _make_args(self, **kwargs):
        defaults = dict(
            prompt="add helper",
            file=None,
            out=None,
            context=None,
            model="qwen2.5-coder:7b",
            append=False,
            force=False,
            dry_run=False,
            no_auto_context=True,
            num_ctx=16384,
            timeout=30,
            tsc=False,
            check=True,
            verify=None,
            retries=1,
            no_rollback=False,
        )
        defaults.update(kwargs)
        return type("Args", (), defaults)()

    def test_mid_repair_backend_error_rolls_back_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "mod.py"
            orig = b"def ok():\r\n    return 1\r\n"
            target.write_bytes(orig)
            args = self._make_args(file=str(target))

            call_idx = 0

            def fake_query(**kwargs):
                nonlocal call_idx
                call_idx += 1
                if call_idx == 1:
                    return "def broken(:\n    pass\n"
                raise bridge_backends.BackendError("429 rate limit", retryable=False)

            with mock.patch.object(local_bridge, "resolve_model", return_value="qwen2.5-coder:7b"):
                with mock.patch.object(local_bridge, "query_backend", side_effect=fake_query):
                    with self.assertRaises(SystemExit) as cm:
                        local_bridge.handle_code(args)
            self.assertEqual(cm.exception.code, 1)
            self.assertEqual(target.read_bytes(), orig)
            self.assertTrue(Path(f"{target}.failed.tmp").exists())

    def test_append_repair_keeps_existing_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "big.py"
            existing = "\n".join(f"x_{i} = {i}" for i in range(50)) + "\n"
            target.write_text(existing, encoding="utf-8")
            args = self._make_args(file=str(target), append=True)

            responses = iter(["def bad(:\n    pass\n", "def good():\n    return 2\n"])
            with mock.patch.object(local_bridge, "resolve_model", return_value="qwen2.5-coder:7b"):
                with mock.patch.object(local_bridge, "query_backend", side_effect=lambda **kw: next(responses)):
                    local_bridge.handle_code(args)
            final = target.read_text(encoding="utf-8")
            self.assertIn("x_49 = 49", final)
            self.assertIn("def good():", final)

    def test_out_refuses_existing_without_force_and_missing_checker_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_file = Path(tmp) / "existing.py"
            out_file.write_text("keep me\n", encoding="utf-8")
            args = self._make_args(out=str(out_file))
            with self.assertRaises(SystemExit) as cm:
                local_bridge.handle_code(args)
            self.assertEqual(cm.exception.code, 2)
            self.assertEqual(out_file.read_text(encoding="utf-8"), "keep me\n")

        with mock.patch.object(bridge_guards.subprocess, "run", side_effect=FileNotFoundError("no npx")):
            errors: list[str] = []
            bridge_guards._run_subprocess_check(["npx", "tsc", "--noEmit"], None, "tsc", errors)
            self.assertEqual(errors, [])


class TestStatsRoutingAndTscBaseline(unittest.TestCase):
    def test_calendar_window_and_outcome_filtering(self):
        import json
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=20)).isoformat()
        today_ts = now.isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "local_tokens.jsonl"
            records = [
                {"timestamp": old, "command": "ask", "model": "qwen2.5:7b", "output_tokens": 500, "input_tokens": 50, "duration_sec": 1.0, "outcome": "accepted"},
                {"timestamp": today_ts, "command": "code", "model": "qwen2.5:7b", "output_tokens": 300, "input_tokens": 40, "duration_sec": 1.0, "outcome": "rolled_back"},
                {"timestamp": today_ts, "command": "ask", "model": "qwen2.5:7b", "output_tokens": 200, "input_tokens": 20, "duration_sec": 0.5, "outcome": "accepted"},
            ]
            log_file.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
            with mock.patch.object(token_stats, "LOCAL_LOG_PATH", str(log_file)):
                stats = token_stats.load_local_stats(days_limit=7)
            total_out = sum(v["local_out"] for v in stats.values())
            total_rejected = sum(v["rejected_calls"] for v in stats.values())
            self.assertEqual(total_out, 200)
            self.assertEqual(total_rejected, 1)

    def test_summary_returns_none_pct_without_primary_data_and_no_wildcard_cors(self):
        with mock.patch.object(metrics_server, "load_cloud_stats", return_value={}):
            with mock.patch.object(metrics_server, "load_local_stats", return_value={}):
                summary = metrics_server.build_summary(days=7)
        self.assertIsNone(summary["total"]["saved_output_pct"])
        self.assertNotIn("Access-Control-Allow-Origin", Path(metrics_server.__file__).read_text(encoding="utf-8"))

    def test_git_commit_requires_staged_changes_and_blocks_cloud_secrets(self):
        args = type("Args", (), {"model": "free", "timeout": 30, "apply": False})()
        empty_proc = type("Proc", (), {"stdout": "", "returncode": 0})()
        with mock.patch.object(local_bridge.subprocess, "run", return_value=empty_proc):
            with self.assertRaises(SystemExit) as cm:
                local_bridge.handle_git_commit(args)
        self.assertEqual(cm.exception.code, 1)

        diff_proc = type("Proc", (), {"stdout": "+API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz1234", "returncode": 0})()
        names_proc = type("Proc", (), {"stdout": "app.py\n", "returncode": 0})()
        with mock.patch.object(local_bridge.subprocess, "run", side_effect=[diff_proc, names_proc]):
            with mock.patch.object(local_bridge, "resolve_model", return_value="opencode/deepseek-v4.1-flash-free"):
                with self.assertRaises(SystemExit) as cm2:
                    local_bridge.handle_git_commit(args)
        self.assertEqual(cm2.exception.code, 3)

    def test_tsc_baseline_filters_preexisting_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tsconfig.json").write_text("{}", encoding="utf-8")
            target = root / "new.ts"
            target.write_text("export const x = 1;\n", encoding="utf-8")
            fake_proc = type("Proc", (), {"returncode": 1, "stdout": "old.ts(1,1): error TS1005: ';' expected.\n", "stderr": ""})()
            with mock.patch.object(bridge_guards.shutil, "which", return_value="/usr/local/bin/npx"):
                with mock.patch.object(bridge_guards.subprocess, "run", return_value=fake_proc):
                    baseline = bridge_guards.get_tsc_baseline(str(target))
                    rc, err = bridge_guards.run_verify(None, True, str(target), check=True, baseline_tsc=baseline)
            self.assertEqual(rc, 0)
            self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
