#!/usr/bin/env python3
"""Safety guards, scope limits, truncation detection, language profiles, and verification helpers."""

from __future__ import annotations

import os
import re
import subprocess
import sys

FREE_CLOUD_PREFIXES = ("opencode/", "nvidia/", "openrouter/", "groq/", "github/")

CLOUD_FREE_DEFAULT_LIMITS = {
    "max_overwrite_lines": 200,
    "max_total_lines": 1200,
    "max_total_chars": 48000,
    "max_prompt_chars": 4000,
}

MODEL_LIMITS = {
    "qwen2.5:7b": {
        "max_overwrite_lines": 60,
        "max_total_lines": 300,
        "max_total_chars": 12000,
        "max_prompt_chars": 1500,
    },
    "qwen2.5-coder:14b": {
        "max_overwrite_lines": 150,
        "max_total_lines": 900,
        "max_total_chars": 36000,
        "max_prompt_chars": 3000,
    },
}

PLACEHOLDER_PATTERNS = [
    re.compile(r"//\s*\.\.\.\s*(existing|rest|remaining|unchanged)", re.IGNORECASE),
    re.compile(r"/\*\s*\.\.\.\s*(existing|rest|remaining|unchanged).*?\*/", re.IGNORECASE),
    re.compile(r"#\s*\.\.\.\s*(existing|rest|remaining|unchanged)", re.IGNORECASE),
]

SENSITIVE_FILENAME_PATTERNS = [
    re.compile(r"(^|/)\.env(\.|$)", re.IGNORECASE),
    re.compile(r"\.(pem|key|p12|pfx|keystore|p8|mobileprovision)$", re.IGNORECASE),
    re.compile(r"(^|/)(id_rsa|id_ed25519|credentials\.json|secrets\.json|GoogleService-Info\.plist)$", re.IGNORECASE),
]

SECRET_CONTENT_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\b(sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})\b"),
    re.compile(
        r"(?im)^\s*(?:export\s+)?[A-Z0-9_]*(API_KEY|SECRET|PASSWORD|PRIVATE_KEY|SEED_PHRASE|MNEMONIC|AUTH_TOKEN|ACCESS_TOKEN)\s*[:=]\s*['\"]?(?!\s*(?:['\"]|$|\$\{|process\.env|your_|changeme|example|dummy|test))[A-Za-z0-9_./+=-]{8,}"
    ),
]

DEFAULT_SYSTEM_PROMPT = (
    "You are an expert software engineer. Produce clean, strict, type-safe, "
    "production-ready code with no conversational wrapper. Handle edge cases explicitly "
    "and follow the target language's idiomatic conventions."
)

LANGUAGE_PROFILES: dict[str, dict[str, str]] = {
    "typescript": {
        "system_prompt": (
            "You are an expert TypeScript/software engineer. Produce clean, strict, type-safe, "
            "production-ready code with no conversational wrapper. Handle edge cases explicitly "
            "(empty/whitespace strings, NaN, floats, negative/unsafe numbers, null/undefined) "
            "and avoid loose coercions like Number() when strict format validation is required."
        ),
        "check_kind": "ts",
    },
    "javascript": {
        "system_prompt": (
            "You are an expert ES6+ JavaScript engineer. Produce clean, modular, "
            "production-ready code with no conversational wrapper. Handle edge cases explicitly "
            "(empty/whitespace strings, NaN, null/undefined) and use modern async/await patterns."
        ),
        "check_kind": "none",
    },
    "swift": {
        "system_prompt": (
            "You are an expert Swift and Apple platforms engineer (Swift 5.9+/6, SwiftUI, "
            "Concurrency, Foundation). Produce clean, strict, type-safe, idiomatic Swift code "
            "(value types, explicit optionals, proper throws/Result error handling, @MainActor/Sendable "
            "where appropriate) with no conversational wrapper. Handle empty/nil/boundary cases explicitly."
        ),
        "check_kind": "swift",
    },
    "php": {
        "system_prompt": (
            "You are an expert PHP 8+ engineer. Produce clean, PSR-12 compliant, strictly typed "
            "(declare(strict_types=1)) production-ready code with no conversational wrapper."
        ),
        "check_kind": "php",
    },
    "python": {
        "system_prompt": (
            "You are an expert Python 3.11+ engineer. Produce clean, type-annotated, "
            "production-ready code with no conversational wrapper. Handle edge cases explicitly."
        ),
        "check_kind": "python",
    },
    "web": {
        "system_prompt": (
            "You are an expert frontend engineer. Produce clean, accessible, semantic HTML/CSS/SCSS "
            "with no conversational wrapper."
        ),
        "check_kind": "none",
    },
}

EXT_TO_LANGUAGE: dict[str, str] = {
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".swift": "swift",
    ".php": "php",
    ".py": "python",
    ".html": "web",
    ".css": "web",
    ".scss": "web",
}

_TS_IMPORT_RE = re.compile(r"""(?:import\s+.*?\s+from\s+|require\s*\(\s*)['"](\.\.?/[^'"]+)['"]""")
_PY_IMPORT_RE = re.compile(r"""^\s*from\s+(\.[A-Za-z0-9_.]*)\s+import\s+""", re.MULTILINE)


def strip_markdown_code(content: str) -> str:
    """Strip <think> blocks and outer markdown code fences, even when prose surrounds the block."""
    cleaned = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL | re.IGNORECASE).strip()

    lines = cleaned.splitlines()
    if lines and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip() + "\n"

    fenced_blocks = re.findall(r"```[^\n]*\n(.*?)```", cleaned, flags=re.DOTALL)
    if fenced_blocks:
        best_block = max(fenced_blocks, key=len)
        return best_block.strip() + "\n"

    return cleaned + "\n"


def detect_truncation_or_placeholders(existing: str, generated: str) -> str | None:
    """Detect lazy placeholder comments or severe file shrinkage before overwriting."""
    for pattern in PLACEHOLDER_PATTERNS:
        if pattern.search(generated):
            return f"output contains placeholder matching {pattern.pattern}"

    existing_lines = existing.splitlines()
    generated_lines = generated.splitlines()
    if len(existing_lines) > 40 and len(generated_lines) < int(len(existing_lines) * 0.45):
        return (
            f"output shrank from {len(existing_lines)} to {len(generated_lines)} lines "
            "(possible truncation)"
        )
    return None


def get_language_profile(target_file: str | None) -> tuple[str, str]:
    """Return (system_prompt, check_kind) based on target file extension."""
    if not target_file:
        return DEFAULT_SYSTEM_PROMPT, "none"
    ext = os.path.splitext(target_file)[1].lower()
    lang = EXT_TO_LANGUAGE.get(ext)
    if not lang:
        return DEFAULT_SYSTEM_PROMPT, "none"
    profile = LANGUAGE_PROFILES[lang]
    return profile["system_prompt"], profile["check_kind"]


def find_project_root(start_path: str | None, marker: str) -> str | None:
    """Walk up from start_path (or cwd) to find nearest directory containing marker."""
    cur = os.path.dirname(os.path.abspath(start_path)) if start_path else os.getcwd()
    while True:
        if os.path.exists(os.path.join(cur, marker)):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _run_subprocess_check(cmd: list[str], cwd: str | None, label: str, errors: list[str]) -> None:
    """Execute a compiler/linter check and record formatted error output on failure."""
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        err = "\n".join(p for p in [proc.stdout.strip(), proc.stderr.strip()] if p)
        print(f"{label} failed:", file=sys.stderr)
        if err:
            print(err, file=sys.stderr)
        errors.append(f"{label} failed:\n{err}")
    else:
        loc = f" ({cwd})" if cwd else ""
        print(f"{label} passed{loc}.")


def run_verify(
    verify_cmd: str | None,
    tsc: bool,
    target_file: str | None,
    check: bool = False,
) -> tuple[int, str]:
    """Run language-aware --check / --tsc and/or custom --verify command; return (exit_code, error_output)."""
    errors: list[str] = []
    _, check_kind = get_language_profile(target_file)

    if tsc or (check and check_kind == "ts"):
        ts_dir = find_project_root(target_file, "tsconfig.json")
        if not ts_dir:
            print("Warning: TypeScript check requested but no tsconfig.json found; skipping.", file=sys.stderr)
        else:
            _run_subprocess_check(["npx", "tsc", "--noEmit"], ts_dir, "TypeScript check (npx tsc --noEmit)", errors)
    elif check and check_kind == "swift":
        pkg_dir = find_project_root(target_file, "Package.swift")
        if pkg_dir:
            _run_subprocess_check(["swift", "build"], pkg_dir, "Swift build (swift build)", errors)
        elif target_file:
            _run_subprocess_check(["swiftc", "-parse", target_file], None, "Swift syntax check (swiftc -parse)", errors)
    elif check and check_kind == "php" and target_file:
        _run_subprocess_check(["php", "-l", target_file], None, "PHP syntax check (php -l)", errors)
    elif check and check_kind == "python" and target_file:
        _run_subprocess_check([sys.executable, "-m", "py_compile", target_file], None, "Python syntax check (py_compile)", errors)

    if verify_cmd and not errors:
        proc = subprocess.run(verify_cmd, shell=True, capture_output=True, text=True)
        if proc.returncode != 0:
            err = "\n".join(p for p in [proc.stdout.strip(), proc.stderr.strip()] if p)
            print(f"Verification failed ({verify_cmd}):", file=sys.stderr)
            if err:
                print(err, file=sys.stderr)
            errors.append(f"{verify_cmd} failed:\n{err}")
        else:
            print(f"Verification passed ({verify_cmd}).")

    if errors:
        return 1, "\n\n".join(errors)
    return 0, ""


def detect_secrets(file_paths: list[str], texts: list[str]) -> str | None:
    """Block sensitive files or secret literals from being sent to external OpenCode free models."""
    for path in file_paths:
        if not path:
            continue
        for pat in SENSITIVE_FILENAME_PATTERNS:
            if pat.search(path):
                return f"sensitive file '{path}' cannot be sent to external OpenCode models"
    for text in texts:
        if not text:
            continue
        for pat in SECRET_CONTENT_PATTERNS:
            if pat.search(text):
                return f"detected potential secret/key matching {pat.pattern}"
    return None


def _resolve_scope_limits(model: str) -> dict:
    """Resolve scope limits for explicit, cloud-free, or dynamically discovered local models."""
    if model.startswith(FREE_CLOUD_PREFIXES):
        return CLOUD_FREE_DEFAULT_LIMITS
    if model in MODEL_LIMITS:
        return MODEL_LIMITS[model]
    low = model.lower()
    if any(tag in low for tag in (":1b", ":3b", ":7b", ":8b", "3b-", "7b-", "8b-")):
        return MODEL_LIMITS["qwen2.5:7b"]
    return MODEL_LIMITS["qwen2.5-coder:14b"]


def check_scope_limits(model: str, prompt: str, existing: str, context_contents: list[str], append: bool) -> str | None:
    """Enforce hard per-model task and context size limits to prevent oversized local calls."""
    limits = _resolve_scope_limits(model)

    if len(prompt) > limits["max_prompt_chars"]:
        return (
            f"prompt too long for {model} ({len(prompt)} > {limits['max_prompt_chars']} chars). "
            "Split into smaller atomic tasks."
        )

    if not append and existing:
        existing_lines = len(existing.splitlines())
        if existing_lines > limits["max_overwrite_lines"]:
            return (
                f"target file has {existing_lines} lines (> {limits['max_overwrite_lines']} limit for {model} overwrite). "
                "Use --append or split into smaller modules."
            )

    all_texts = ([existing] if existing else []) + context_contents
    total_lines = sum(len(t.splitlines()) for t in all_texts)
    total_chars = sum(len(t) for t in all_texts)
    if total_lines > limits["max_total_lines"] or total_chars > limits["max_total_chars"]:
        return (
            f"input context too large for {model} ({total_lines} lines / {total_chars} chars; "
            f"limit {limits['max_total_lines']} lines / {limits['max_total_chars']} chars). "
            "Pass only relevant types/interfaces in -c or upgrade to 14b."
        )
    return None


def resolve_local_imports(
    base_file: str,
    source_text: str,
    already_included: set[str],
    max_extra_chars: int,
) -> list[tuple[str, str]]:
    """Auto-resolve relative local imports from source_text within remaining char budget."""
    if not base_file or not source_text or max_extra_chars <= 0:
        return []
    base_dir = os.path.dirname(os.path.abspath(base_file))
    candidates: list[str] = []

    for rel in _TS_IMPORT_RE.findall(source_text):
        raw_path = os.path.normpath(os.path.join(base_dir, rel))
        if os.path.isfile(raw_path):
            candidates.append(raw_path)
        else:
            for ext in (".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.tsx", "/index.js"):
                p = raw_path + ext
                if os.path.isfile(p):
                    candidates.append(p)
                    break

    for mod in _PY_IMPORT_RE.findall(source_text):
        dots = len(mod) - len(mod.lstrip("."))
        rel_mod = mod[dots:].replace(".", os.sep)
        parent = base_dir
        for _ in range(max(0, dots - 1)):
            parent = os.path.dirname(parent)
        base_mod = os.path.join(parent, rel_mod) if rel_mod else parent
        for p in (base_mod + ".py", os.path.join(base_mod, "__init__.py")):
            if os.path.isfile(p):
                candidates.append(p)
                break

    resolved: list[tuple[str, str]] = []
    used_chars = 0
    for cand in candidates:
        abs_p = os.path.abspath(cand)
        if abs_p in already_included or any(pat.search(abs_p) for pat in SENSITIVE_FILENAME_PATTERNS):
            continue
        try:
            with open(abs_p, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:
            continue
        if used_chars + len(text) > max_extra_chars:
            continue
        already_included.add(abs_p)
        used_chars += len(text)
        resolved.append((cand, text))
    return resolved
