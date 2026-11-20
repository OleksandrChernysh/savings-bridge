#!/usr/bin/env python3
"""Safety guards, scope limits, truncation detection, language profiles, and verification helpers."""

from __future__ import annotations

import os
import re
import shutil
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
    re.compile(r"(^|/)(id_rsa|id_ed25519|credentials\.json|secrets\.json|secrets\.env|GoogleService-Info\.plist)$", re.IGNORECASE),
]

SECRET_CONTENT_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(
        r"\b(sk-(?:proj-|ant-api\d+-|or-v1-|live_|test_)?[A-Za-z0-9_-]{16,}"
        r"|AIza[0-9A-Za-z_-]{30,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}"
        r"|gsk_[A-Za-z0-9]{20,}|nvapi-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})\b"
    ),
    re.compile(
        r"(?im)(?:^|[+\-\s.;,\"'])(?:export\s+)?[A-Za-z0-9_.]*(?:API_?KEY|SECRET|PASSWORD|PASSWD|PRIVATE_KEY|SEED_PHRASE|MNEMONIC|AUTH_TOKEN|ACCESS_TOKEN)[A-Za-z0-9_]*[\"']?\s*[:=]\s*['\"]?(?!\s*(?:['\"]|$|\$\{|process\.env|os\.environ|your_|changeme|example|dummy|test))\S{8,}"
    ),
    re.compile(r"https?://[^\s:/@]+:[^\s/@]{4,}@[^\s/]+"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
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

_TS_IMPORT_RE = re.compile(
    r"""(?:(?:import|export)\s+(?:[^;]*?\s+from\s+)?|require\s*\(\s*)['"](\.\.?/[^'"]+)['"]""",
    re.DOTALL,
)
_PY_IMPORT_RE = re.compile(r"""^\s*from\s+(\.[A-Za-z0-9_.]*)\s+import\s+""", re.MULTILINE)


def strip_markdown_code(content: str) -> str:
    """Strip <think> blocks and outer markdown code fences, even when prose surrounds the block."""
    cleaned = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL | re.IGNORECASE).strip()

    lines = cleaned.splitlines()
    if lines and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
        inner = "\n".join(lines[1:-1]).strip()
        return (inner + "\n") if inner else ""

    fenced_blocks = re.findall(r"```[^\n]*\n(.*?)```", cleaned, flags=re.DOTALL)
    if fenced_blocks:
        best_block = max(fenced_blocks, key=len).strip()
        return (best_block + "\n") if best_block else ""

    return (cleaned + "\n") if cleaned else ""


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
    resolved_bin = shutil.which(cmd[0]) if not os.path.isabs(cmd[0]) else cmd[0]
    if not resolved_bin:
        print(f"Warning: {label} unavailable ({cmd[0]} not found); skipping check.", file=sys.stderr)
        return
    try:
        proc = subprocess.run(
            [resolved_bin, *cmd[1:]],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        print(f"Warning: {label} unavailable ({exc}); skipping check.", file=sys.stderr)
        return
    if proc.returncode != 0:
        err = "\n".join(p for p in [proc.stdout.strip(), proc.stderr.strip()] if p)
        print(f"{label} failed:", file=sys.stderr)
        if err:
            print(err, file=sys.stderr)
        errors.append(f"{label} failed:\n{err}")
    else:
        loc = f" ({cwd})" if cwd else ""
        print(f"{label} passed{loc}.")


def _scrubbed_child_env() -> dict[str, str]:
    secret_keys = {
        "OPENROUTER_API_KEY",
        "NVIDIA_API_KEY",
        "GROQ_API_KEY",
        "GITHUB_TOKEN",
        "OPENCODE_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
    }
    return {k: v for k, v in os.environ.items() if k not in secret_keys}


def get_tsc_baseline(target_file: str | None) -> set[str]:
    """Capture pre-existing tsc diagnostic lines before writing target_file."""
    ts_dir = find_project_root(target_file, "tsconfig.json")
    npx_bin = shutil.which("npx")
    if not ts_dir or not npx_bin:
        return set()
    try:
        proc = subprocess.run(
            [npx_bin, "--no-install", "tsc", "--noEmit"],
            cwd=ts_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return set()
    if proc.returncode == 0:
        return set()
    out = "\n".join(p for p in (proc.stdout, proc.stderr) if p)
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def run_verify(
    verify_cmd: str | None,
    tsc: bool,
    target_file: str | None,
    check: bool = False,
    baseline_tsc: set[str] | None = None,
) -> tuple[int, str]:
    """Run language-aware --check / --tsc and/or custom --verify command; return (exit_code, error_output)."""
    errors: list[str] = []
    _, check_kind = get_language_profile(target_file)

    if tsc or (check and check_kind == "ts"):
        ts_dir = find_project_root(target_file, "tsconfig.json")
        if not ts_dir:
            print("Warning: TypeScript check requested but no tsconfig.json found; skipping.", file=sys.stderr)
        else:
            tsc_errors: list[str] = []
            _run_subprocess_check(["npx", "--no-install", "tsc", "--noEmit"], ts_dir, "TypeScript check (npx tsc --noEmit)", tsc_errors)
            if tsc_errors and baseline_tsc:
                new_lines = [
                    ln for ln in tsc_errors[0].splitlines()[1:]
                    if ln.strip() and ln.strip() not in baseline_tsc
                ]
                if new_lines:
                    errors.append("TypeScript check (npx tsc --noEmit) failed:\n" + "\n".join(new_lines))
            else:
                errors.extend(tsc_errors)
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
        proc = subprocess.run(
            verify_cmd,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_scrubbed_child_env(),
        )
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
        norm_path = path.replace("\\", "/")
        basename = norm_path.rsplit("/", 1)[-1]
        for pat in SENSITIVE_FILENAME_PATTERNS:
            if pat.search(norm_path) or pat.search(basename):
                return f"sensitive file '{path}' cannot be sent to external cloud models"
    for text in texts:
        if not text:
            continue
        normalized_diff = re.sub(r"(?m)^[+-]", "", text)
        for candidate in (text, normalized_diff):
            for pat in SECRET_CONTENT_PATTERNS:
                if pat.search(candidate):
                    return f"detected potential secret/key matching {pat.pattern}"
    return None


def _resolve_scope_limits(model: str, tier: str | None = None) -> dict:
    """Resolve scope limits for explicit, cloud-free, or dynamically discovered local models."""
    if model.startswith(FREE_CLOUD_PREFIXES):
        return CLOUD_FREE_DEFAULT_LIMITS
    if model in MODEL_LIMITS:
        return MODEL_LIMITS[model]
    low = model.lower()
    if tier == "coder" or "coder" in low:
        return MODEL_LIMITS["qwen2.5-coder:14b"]
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
            stem, rel_ext = os.path.splitext(raw_path)
            ts_mapped = []
            if rel_ext in (".js", ".jsx", ".mjs", ".cjs"):
                ts_mapped = [stem + ".ts", stem + ".tsx", stem + ".mts", stem + ".cts"]
            probe_list = ts_mapped + [
                raw_path + ext for ext in (".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.tsx", "/index.js")
            ]
            for p in probe_list:
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
