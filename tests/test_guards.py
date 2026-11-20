#!/usr/bin/env python3
"""Unit tests for bridge_guards.py (stdlib unittest)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "src"
for p in (str(ROOT), str(SRC_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from bridge_guards import (  # noqa: E402
    check_scope_limits,
    detect_secrets,
    detect_truncation_or_placeholders,
    get_language_profile,
    resolve_local_imports,
    strip_markdown_code,
)


class TestStripMarkdownCode(unittest.TestCase):
    def test_strips_think_and_fences(self):
        raw = "<think>reasoning here</think>\nHere is code:\n```python\ndef add(a, b):\n    return a + b\n```\nDone."
        self.assertEqual(strip_markdown_code(raw), "def add(a, b):\n    return a + b\n")

    def test_plain_code_preserved(self):
        self.assertEqual(strip_markdown_code("x = 1"), "x = 1\n")


class TestTruncationAndPlaceholders(unittest.TestCase):
    def test_detects_placeholder_comments(self):
        self.assertIsNotNone(detect_truncation_or_placeholders("a\nb\n", "// ... existing code\nreturn 1;\n"))
        self.assertIsNotNone(detect_truncation_or_placeholders("a\nb\n", "# ... rest of file\n"))

    def test_detects_severe_shrinkage(self):
        existing = "\n".join(f"line {i}" for i in range(60))
        generated = "\n".join(f"line {i}" for i in range(10))
        err = detect_truncation_or_placeholders(existing, generated)
        self.assertIsNotNone(err)
        self.assertIn("shrank", err)

    def test_allows_normal_edit(self):
        existing = "\n".join(f"line {i}" for i in range(50))
        generated = "\n".join(f"line {i}" for i in range(45))
        self.assertIsNone(detect_truncation_or_placeholders(existing, generated))


class TestDetectSecrets(unittest.TestCase):
    def test_blocks_sensitive_filenames(self):
        self.assertIsNotNone(detect_secrets([".env"], ["hello"]))
        self.assertIsNotNone(detect_secrets(["/tmp/server.key"], ["hello"]))
        self.assertIsNotNone(detect_secrets(["config/id_rsa"], ["hello"]))
        self.assertIsNotNone(detect_secrets([r"config\.env"], ["hello"]))
        self.assertIsNotNone(detect_secrets([r"C:\app\.env.local"], ["hello"]))
        self.assertIsNotNone(detect_secrets(["~/.config/savings-bridge/secrets.env"], ["hello"]))

    def test_blocks_secret_literals(self):
        self.assertIsNotNone(detect_secrets(["app.py"], ["OPENAI_API_KEY = 'sk-1234567890abcdefghij1234'"]))
        self.assertIsNotNone(detect_secrets(["app.py"], ["-----BEGIN RSA PRIVATE KEY-----"]))
        self.assertIsNotNone(detect_secrets(["app.py"], ["sk-proj-abcdefghijklmnopqrstuvwxyz1234"]))
        self.assertIsNotNone(detect_secrets(["app.py"], ["sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234"]))
        self.assertIsNotNone(detect_secrets(["app.py"], ["sk-or-v1-abcdefghijklmnopqrstuvwxyz1234"]))
        self.assertIsNotNone(detect_secrets(["app.py"], ["gsk_abcdefghijklmnopqrstuvwxyz1234"]))
        self.assertIsNotNone(detect_secrets(["app.py"], ["nvapi-abcdefghijklmnopqrstuvwxyz1234"]))
        self.assertIsNotNone(detect_secrets(["app.py"], ["github_pat_abcdefghijklmnopqrstuvwxyz1234"]))
        self.assertIsNotNone(detect_secrets(["app.ts"], ['+const DB_PASSWORD = "Pr0d!Pass#2026";']))
        self.assertIsNotNone(detect_secrets(["app.py"], ["+self.password = 'P@ssw0rd!2026'"]))
        self.assertIsNotNone(detect_secrets(["app.json"], ['"password": "SuperSecret!99"']))
        self.assertIsNotNone(detect_secrets(["app.env"], ["AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCY"]))
        self.assertIsNotNone(detect_secrets(["app.py"], ["https://user:s3cr3tpass@db.example.com/app"]))
        self.assertIsNotNone(detect_secrets(["app.py"], ["Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"]))

    def test_allows_safe_placeholders(self):
        self.assertIsNone(detect_secrets(["app.py"], ["API_KEY = process.env.API_KEY"]))
        self.assertIsNone(detect_secrets(["app.py"], ["API_KEY = 'your_api_key_here'"]))


class TestScopeLimits(unittest.TestCase):
    def test_prompt_limit_7b(self):
        err = check_scope_limits("qwen2.5:7b", "x" * 1600, "", [], append=False)
        self.assertIsNotNone(err)
        self.assertIn("prompt too long", err)

    def test_overwrite_limit_bypassed_on_append(self):
        big_file = "\n".join(f"line {i}" for i in range(100))
        self.assertIsNotNone(check_scope_limits("qwen2.5:7b", "add fn", big_file, [], append=False))
        self.assertIsNone(check_scope_limits("qwen2.5:7b", "add fn", big_file, [], append=True))
        self.assertIsNone(check_scope_limits("qwen2.5-coder:7b", "add fn", big_file, [], append=False))


class TestLanguageAndImports(unittest.TestCase):
    def test_language_profiles(self):
        _, kind_ts = get_language_profile("foo.tsx")
        _, kind_py = get_language_profile("foo.py")
        _, kind_unk = get_language_profile("foo.txt")
        self.assertEqual(kind_ts, "ts")
        self.assertEqual(kind_py, "python")
        self.assertEqual(kind_unk, "none")

    def test_resolve_local_python_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp)
            helper = pkg / "helper.py"
            helper.write_text("VALUE = 42\n", encoding="utf-8")
            main_py = pkg / "main.py"
            src = "from .helper import VALUE\n"
            main_py.write_text(src, encoding="utf-8")

            resolved = resolve_local_imports(str(main_py), src, set(), max_extra_chars=1000)
            self.assertEqual(len(resolved), 1)
            self.assertEqual(os.path.realpath(resolved[0][0]), str(helper.resolve()))
            self.assertEqual(resolved[0][1], "VALUE = 42\n")

    def test_resolve_local_ts_multiline_reexport_and_js_ext(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp)
            (pkg / "types.ts").write_text("export type Foo = string;\n", encoding="utf-8")
            (pkg / "bar.ts").write_text("export const BAR = 1;\n", encoding="utf-8")
            main_ts = pkg / "index.ts"
            src = (
                "import {\n  Foo,\n} from './types.js';\n"
                "export { BAR } from './bar';\n"
            )
            main_ts.write_text(src, encoding="utf-8")
            resolved = resolve_local_imports(str(main_ts), src, set(), max_extra_chars=1000)
            names = {Path(p).name for p, _ in resolved}
            self.assertEqual(names, {"types.ts", "bar.ts"})


if __name__ == "__main__":
    unittest.main()
