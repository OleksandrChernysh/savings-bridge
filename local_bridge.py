#!/usr/bin/env python3
"""
Local & Free-Cloud LLM Bridge (Ollama, LM Studio, OpenRouter, NVIDIA NIM, OpenCode)
Supports:
  - ask: general text Q&A, drafting, summarization, translation
  - code: generate / implement / append code directly to file (with truncation guard & optional check/verify)
  - review: pre-review git diff or files for bugs and edge cases
  - git-commit: generate conventional commit message from git diff
  - git-changelog: generate release notes / changelog from git log
  - parse-json: extract structured JSON from unstructured text

Automatically logs local & free-cloud token usage to ~/.gemini/antigravity/local_tokens.jsonl
"""

import argparse
try:
    import fcntl
except ImportError:
    fcntl = None
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone

DEFAULT_CODER_MODEL = "qwen2.5-coder:14b"
DEFAULT_PARSER_MODEL = "qwen2.5:7b"
MODEL_ALIASES = {
    "7b": "qwen2.5:7b",
    "14b": "qwen2.5-coder:14b",
    "free": "opencode/deepseek-v4.1-flash-free",
    "deepseek": "opencode/deepseek-v4.1-flash-free",
    "pickle": "opencode/big-pickle",
    "glm-free": "opencode/glm-5.3-flash-free",
    "nim": "nvidia/nvidia/nemotron-3-ultra-550b-a55b",
    "openrouter": "openrouter/openrouter/free",
}
FREE_CLOUD_PREFIXES = ("opencode/", "nvidia/", "openrouter/", "groq/", "github/")
OPENAI_COMPAT_PROVIDERS = {
    "nvidia/": ("https://integrate.api.nvidia.com/v1/chat/completions", "NVIDIA_API_KEY", "NVIDIA NIM", "build.nvidia.com"),
    "openrouter/": ("https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY", "OpenRouter", "openrouter.ai/keys"),
    "groq/": ("https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY", "Groq", "console.groq.com/keys"),
    "github/": ("https://models.inference.ai.azure.com/chat/completions", "GITHUB_TOKEN", "GitHub Models", "github.com/marketplace/models"),
    "lmstudio/": (f"{os.environ.get('LM_STUDIO_HOST', 'http://127.0.0.1:1234').rstrip('/')}/v1/chat/completions", None, "LM Studio", "lmstudio.ai"),
}
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
STATS_LOG_PATH = os.path.expanduser("~/.gemini/antigravity/local_tokens.jsonl")
SECRETS_ENV_PATH = os.path.expanduser("~/.config/savings-bridge/secrets.env")
LOCK_PATH = os.path.join(tempfile.gettempdir(), "local_bridge_ollama.lock")


def load_secrets_env(path: str = SECRETS_ENV_PATH) -> None:
    """Load key=value or export key=value pairs from secrets.env without overriding existing env vars."""
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                    try:
                        val = json.loads(val) if val.startswith('"') else val[1:-1]
                    except Exception:
                        val = val[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


load_secrets_env()

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


def normalize_ollama_url(raw: str) -> str:
    """Ensure OLLAMA_HOST has an HTTP scheme and no trailing slash."""
    url = raw.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = f"http://{url}"
    return url


OLLAMA_API = normalize_ollama_url(os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"))


def _get_dynamic_profile() -> dict:
    from model_discovery import build_profile, load_profile

    cached = load_profile()
    if cached:
        return cached
    return build_profile(OLLAMA_API)


def resolve_model(name: str | None, default: str) -> str:
    """Resolve model alias dynamically via machine profile (models.json / Ollama) + static aliases."""
    profile = _get_dynamic_profile()
    dyn_aliases = profile.get("aliases", {})
    defaults = profile.get("defaults", {})

    if not name:
        if default == DEFAULT_CODER_MODEL and defaults.get("coder"):
            return defaults["coder"]
        if default == DEFAULT_PARSER_MODEL and defaults.get("parser"):
            return defaults["parser"]
        return default

    key = name.strip().lower()
    if key in dyn_aliases:
        return dyn_aliases[key]
    return MODEL_ALIASES.get(key, name.strip())


@contextmanager
def acquire_ollama_lock():
    """Serialize Ollama requests across processes to avoid RAM/GPU contention timeouts."""
    if fcntl is None:
        yield
        return
    with open(LOCK_PATH, "w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def log_local_usage(command: str, model: str, eval_count: int, prompt_eval_count: int, duration_sec: float):
    try:
        os.makedirs(os.path.dirname(STATS_LOG_PATH), exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "command": command,
            "model": model,
            "output_tokens": eval_count,
            "input_tokens": prompt_eval_count,
            "duration_sec": round(duration_sec, 2),
        }
        with open(STATS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def query_ollama(
    command: str,
    model: str,
    prompt: str,
    system: str = "",
    schema: dict | str | None = None,
    num_ctx: int = 16384,
    temperature: float = 0.2,
    timeout: int = 300,
) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload: dict = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": "15m",
        "options": {
            "num_ctx": num_ctx,
            "temperature": temperature,
        },
    }
    if isinstance(schema, dict):
        payload["format"] = schema
    elif isinstance(schema, str):
        payload["format"] = "json"

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_API}/api/chat",
        data=data,
        headers={"Content-Type": "application/json"},
    )

    try:
        with acquire_ollama_lock():
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                res = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"Error connecting to Ollama at {OLLAMA_API}: {e}", file=sys.stderr)
        sys.exit(1)
    except TimeoutError:
        print(f"Ollama request timed out after {timeout}s (model={model}).", file=sys.stderr)
        sys.exit(1)

    msg = res.get("message", {}).get("content", "")
    eval_count = res.get("eval_count", 0)
    prompt_eval_count = res.get("prompt_eval_count", 0)
    eval_duration_sec = (res.get("eval_duration") or 0) / 1e9

    if num_ctx > 0 and prompt_eval_count >= int(num_ctx * 0.9):
        print(
            f"Warning: prompt_eval_count ({prompt_eval_count}) >= 90% of num_ctx ({num_ctx}); context may truncate.",
            file=sys.stderr,
        )

    log_local_usage(
        command=command,
        model=model,
        eval_count=eval_count,
        prompt_eval_count=prompt_eval_count,
        duration_sec=eval_duration_sec,
    )
    return msg.strip()


def query_opencode(
    command: str,
    model: str,
    prompt: str,
    system: str = "",
    timeout: int = 180,
) -> str:
    """Query an OpenCode free model via `opencode run --format json` (runs in parallel with Ollama)."""
    combined_prompt = f"{system}\n\n{prompt}".strip() if system else prompt
    start = time.monotonic()
    try:
        proc = subprocess.run(
            ["opencode", "run", "-m", model, "--format", "json", combined_prompt],
            cwd=tempfile.gettempdir(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        print("Error: `opencode` CLI not found in PATH.", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print(f"OpenCode request timed out after {timeout}s (model={model}).", file=sys.stderr)
        sys.exit(1)

    duration_sec = time.monotonic() - start
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip()
        print(f"OpenCode error (exit {proc.returncode}, model={model}): {err}", file=sys.stderr)
        sys.exit(1)

    text_parts: list[str] = []
    input_tokens = 0
    output_tokens = 0
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = evt.get("type")
        part = evt.get("part") or {}
        if etype == "text" and isinstance(part.get("text"), str):
            text_parts.append(part["text"])
        elif etype == "step_finish":
            tokens = part.get("tokens") or {}
            input_tokens += tokens.get("input") or 0
            output_tokens += tokens.get("output") or 0

    log_local_usage(
        command=command,
        model=model,
        eval_count=output_tokens,
        prompt_eval_count=input_tokens,
        duration_sec=duration_sec,
    )
    return "\n".join(text_parts).strip()


def query_openai_compat(
    command: str,
    model: str,
    prefix: str,
    prompt: str,
    system: str = "",
    temperature: float = 0.2,
    timeout: int = 180,
) -> str:
    """Query any OpenAI-compatible endpoint (NVIDIA NIM, OpenRouter, Groq, GitHub Models, LM Studio)."""
    api_url, env_var, display_name, setup_url = OPENAI_COMPAT_PROVIDERS[prefix]
    headers = {"Content-Type": "application/json"}
    if env_var:
        api_key = os.environ.get(env_var, "").strip()
        if not api_key:
            print(
                f"Error: {env_var} is not set. Add it to {SECRETS_ENV_PATH} or environment (get key at {setup_url}).",
                file=sys.stderr,
            )
            sys.exit(1)
        headers["Authorization"] = f"Bearer {api_key}"

    remote_model = model[len(prefix):]
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": remote_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 4096,
        "stream": False,
    }
    req = urllib.request.Request(
        api_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )

    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            res = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"{display_name} HTTP {e.code} (model={remote_model}): {body[:500]}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Error connecting to {display_name}: {e}", file=sys.stderr)
        sys.exit(1)
    except TimeoutError:
        print(f"{display_name} request timed out after {timeout}s (model={remote_model}).", file=sys.stderr)
        sys.exit(1)

    duration_sec = time.monotonic() - start
    choices = res.get("choices") or []
    content = (choices[0].get("message") or {}).get("content", "") if choices else ""
    usage = res.get("usage") or {}
    log_local_usage(
        command=command,
        model=model,
        eval_count=usage.get("completion_tokens") or 0,
        prompt_eval_count=usage.get("prompt_tokens") or 0,
        duration_sec=duration_sec,
    )
    return content.strip()


def query_backend(
    command: str,
    model: str,
    prompt: str,
    system: str = "",
    schema: dict | str | None = None,
    num_ctx: int = 16384,
    temperature: float = 0.2,
    timeout: int = 300,
) -> str:
    """Route to OpenCode CLI (`opencode/*`), OpenAI-compatible APIs (`nvidia/*`, `openrouter/*`, `groq/*`, `github/*`, `lmstudio/*`), or Ollama."""
    if model.startswith("opencode/"):
        return query_opencode(command=command, model=model, prompt=prompt, system=system, timeout=timeout)
    for prefix in OPENAI_COMPAT_PROVIDERS:
        if model.startswith(prefix):
            return query_openai_compat(
                command=command,
                model=model,
                prefix=prefix,
                prompt=prompt,
                system=system,
                temperature=temperature,
                timeout=timeout,
            )
    return query_ollama(
        command=command,
        model=model,
        prompt=prompt,
        system=system,
        schema=schema,
        num_ctx=num_ctx,
        temperature=temperature,
        timeout=timeout,
    )


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
        _run_subprocess_check(["python3", "-m", "py_compile", target_file], None, "Python syntax check (py_compile)", errors)

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


_TS_IMPORT_RE = re.compile(r"""(?:import\s+.*?\s+from\s+|require\s*\(\s*)['"](\.\.?/[^'"]+)['"]""")
_PY_IMPORT_RE = re.compile(r"""^\s*from\s+(\.[A-Za-z0-9_.]*)\s+import\s+""", re.MULTILINE)


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


def handle_code(args):
    prompt_parts = []
    existing = ""
    context_contents: list[str] = []
    context_blocks: list[str] = []
    included_abs: set[str] = set()

    if args.file and os.path.exists(args.file):
        included_abs.add(os.path.abspath(args.file))
        with open(args.file, "r", encoding="utf-8") as f:
            existing = f.read()
        label = "Existing file to append to" if args.append else "Existing file content"
        prompt_parts.append(f"{label} ({args.file}):\n```\n{existing}\n```")

    if args.context:
        for ctx_file in args.context:
            if os.path.exists(ctx_file):
                included_abs.add(os.path.abspath(ctx_file))
                with open(ctx_file, "r", encoding="utf-8") as f:
                    ctx_text = f.read()
                    context_contents.append(ctx_text)
                    block = f"Context from {ctx_file}:\n```\n{ctx_text}\n```"
                    context_blocks.append(block)
                    prompt_parts.append(block)
            else:
                print(f"Warning: context file not found: {ctx_file}", file=sys.stderr)

    model = resolve_model(args.model, DEFAULT_CODER_MODEL)
    auto_ctx_paths: list[str] = []
    if not getattr(args, "no_auto_context", False) and existing and args.file:
        limits = _resolve_scope_limits(model)
        used = len(existing) + sum(len(c) for c in context_contents)
        budget = max(0, int(limits["max_total_chars"] * 0.85) - used)
        for imp_path, imp_text in resolve_local_imports(args.file, existing, included_abs, budget):
            auto_ctx_paths.append(imp_path)
            context_contents.append(imp_text)
            block = f"Auto-imported context ({imp_path}):\n```\n{imp_text}\n```"
            context_blocks.append(block)
            prompt_parts.append(block)

    if model.startswith(FREE_CLOUD_PREFIXES):
        checked_paths = [p for p in ([args.file, args.out] + (args.context or []) + auto_ctx_paths) if p]
        secret_err = detect_secrets(checked_paths, [args.prompt, existing] + context_contents)
        if secret_err:
            print(
                f"Security block (exit 3): {secret_err}. Use local Ollama (-m fast / -m coder) or Primary Cloud model instead.",
                file=sys.stderr,
            )
            sys.exit(3)

    scope_error = check_scope_limits(model, args.prompt, existing, context_contents, args.append)
    if scope_error:
        print(f"Scope limit exceeded: {scope_error}", file=sys.stderr)
        sys.exit(2)

    prompt_parts.append(f"Task / Instruction:\n{args.prompt}")
    if args.append:
        prompt_parts.append(
            "IMPORTANT: Output ONLY the new code snippet to append to the end of the file. "
            "Do NOT repeat existing code. No conversational filler, no markdown explanations."
        )
    else:
        prompt_parts.append(
            "IMPORTANT: Output only the exact raw code for the file. "
            "No conversational filler, no markdown explanations before or after."
        )

    target_file = args.out or args.file
    system_prompt, _ = get_language_profile(target_file)

    full_prompt = "\n\n".join(prompt_parts)

    raw_code = query_backend(
        command="code",
        model=model,
        prompt=full_prompt,
        system=system_prompt,
        num_ctx=args.num_ctx,
        timeout=args.timeout,
    )
    clean_code = strip_markdown_code(raw_code)

    if args.dry_run or not target_file:
        sys.stdout.write(clean_code)
        return

    if not args.append and existing and not args.force:
        guard_reason = detect_truncation_or_placeholders(existing, clean_code)
        if guard_reason:
            tmp_path = f"{target_file}.local.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(clean_code)
            print(
                f"Refused to overwrite {target_file}: {guard_reason}. "
                f"Saved output to {tmp_path} (use --force to overwrite).",
                file=sys.stderr,
            )
            sys.exit(2)

    had_target = os.path.exists(target_file)
    orig_target = ""
    if had_target:
        with open(target_file, "r", encoding="utf-8") as f:
            orig_target = f.read()

    os.makedirs(os.path.dirname(os.path.abspath(target_file)), exist_ok=True)
    if args.append and had_target:
        current = orig_target.rstrip()
        combined = f"{current}\n\n{clean_code}" if current else clean_code
        with open(target_file, "w", encoding="utf-8") as f:
            f.write(combined)
        print(f"Appended generated code to: {target_file}")
    else:
        with open(target_file, "w", encoding="utf-8") as f:
            f.write(clean_code)
        print(f"Saved generated code to: {target_file}")

    if args.tsc or args.check or args.verify:
        rc, err_out = run_verify(args.verify, args.tsc, target_file, check=args.check)
        retries_left = args.retries
        ctx_section = ("\n\n".join(context_blocks)[:16000] + "\n\n") if context_blocks else ""
        while rc != 0 and retries_left > 0:
            retries_left -= 1
            repair_model = DEFAULT_CODER_MODEL if model == DEFAULT_PARSER_MODEL else model
            print(
                f"Auto-repairing locally with {repair_model} (retries left: {retries_left})...",
                file=sys.stderr,
            )
            with open(target_file, "r", encoding="utf-8") as f:
                broken_content = f.read()
            repair_prompt = (
                f"Original task:\n{args.prompt}\n\n"
                f"{ctx_section}"
                f"Current file content ({target_file}):\n```\n{broken_content}\n```\n\n"
                f"Verification errors:\n{err_out[:4000]}\n\n"
                "Fix all errors and output ONLY the complete corrected raw code for the file."
            )
            repaired_raw = query_backend(
                command="code-repair",
                model=repair_model,
                prompt=repair_prompt,
                system=system_prompt,
                num_ctx=args.num_ctx,
                timeout=args.timeout,
            )
            repaired_clean = strip_markdown_code(repaired_raw)
            with open(target_file, "w", encoding="utf-8") as f:
                f.write(repaired_clean)
            rc, err_out = run_verify(args.verify, args.tsc, target_file, check=args.check)
        if rc != 0:
            if not getattr(args, "no_rollback", False):
                failed_tmp = f"{target_file}.failed.tmp"
                with open(target_file, "r", encoding="utf-8") as f:
                    failed_content = f.read()
                with open(failed_tmp, "w", encoding="utf-8") as f:
                    f.write(failed_content)
                if had_target:
                    with open(target_file, "w", encoding="utf-8") as f:
                        f.write(orig_target)
                elif os.path.exists(target_file):
                    os.remove(target_file)
                print(
                    f"Verification failed after retries; rolled back {target_file} (failed output saved to {failed_tmp}).",
                    file=sys.stderr,
                )
            sys.exit(rc)


def handle_git_commit(args):
    diff_proc = subprocess.run(["git", "diff", "--cached"], capture_output=True, text=True)
    diff = diff_proc.stdout.strip()

    if not diff:
        diff_proc = subprocess.run(["git", "diff", "HEAD~1"], capture_output=True, text=True)
        diff = diff_proc.stdout.strip()

    if not diff:
        diff_proc = subprocess.run(["git", "diff"], capture_output=True, text=True)
        diff = diff_proc.stdout.strip()

    if not diff:
        print("No git diff detected to summarize.", file=sys.stderr)
        sys.exit(1)

    if len(diff) > 20000:
        diff = diff[:20000] + "\n\n...[diff truncated for length]..."

    prompt = (
        "Generate a concise, conventional commit message based on the following git diff.\n"
        "Format: <type>(<scope>): <summary>\n"
        "[optional bullet points if needed]\n"
        "Output ONLY the commit message. No quotes, no markdown fences.\n\n"
        f"Diff:\n{diff}"
    )

    model = resolve_model(args.model, DEFAULT_PARSER_MODEL)
    commit_msg = query_ollama(
        command="git-commit", model=model, prompt=prompt, num_ctx=8192, temperature=0.1, timeout=args.timeout
    )
    commit_msg = commit_msg.strip("`'\"\n ")

    if args.apply:
        res = subprocess.run(["git", "commit", "-m", commit_msg])
        if res.returncode == 0:
            print(f"Committed successfully:\n{commit_msg}")
        else:
            print("Git commit failed.", file=sys.stderr)
            sys.exit(res.returncode)
    else:
        print(commit_msg)


def handle_git_changelog(args):
    git_range = args.range or "HEAD~10..HEAD"
    log_proc = subprocess.run(
        ["git", "log", git_range, "--pretty=format:%h - %s (%an)"],
        capture_output=True,
        text=True,
    )
    commits = log_proc.stdout.strip()

    if not commits:
        print(f"No commits found for range {git_range}", file=sys.stderr)
        sys.exit(1)

    prompt = (
        "Summarize these commits into clear release notes / changelog grouped by category "
        "(Features, Bug Fixes, Refactoring, Maintenance).\n"
        "Commits:\n"
        f"{commits}\n\n"
        "Format as clean GitHub Flavored Markdown."
    )

    model = resolve_model(args.model, DEFAULT_PARSER_MODEL)
    changelog = query_ollama(
        command="git-changelog", model=model, prompt=prompt, num_ctx=8192, temperature=0.2, timeout=args.timeout
    )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(changelog + "\n")
        print(f"Saved changelog to: {args.out}")
    else:
        print(changelog)


def handle_parse_json(args):
    if args.input and os.path.exists(args.input):
        with open(args.input, "r", encoding="utf-8") as f:
            raw_text = f.read()
    elif args.text:
        raw_text = args.text
    else:
        raw_text = sys.stdin.read()

    if args.schema:
        if os.path.exists(args.schema):
            with open(args.schema, "r", encoding="utf-8") as f:
                schema_obj = json.load(f)
        else:
            schema_obj = json.loads(args.schema)
    else:
        schema_obj = "json"

    system_prompt = (
        "You are an accurate data extraction and parsing model. "
        "Extract the requested structured information from the provided raw text and return strictly valid JSON. "
        "Do not include explanations or markdown fences."
    )

    prompt = (
        f"Raw text:\n{raw_text}\n\n"
        f"Extraction instruction:\n{args.instruction or 'Extract key information into JSON.'}"
    )

    model = resolve_model(args.model, DEFAULT_PARSER_MODEL)
    result = query_ollama(
        command="parse-json",
        model=model,
        prompt=prompt,
        system=system_prompt,
        schema=schema_obj,
        num_ctx=args.num_ctx,
        temperature=0.0,
        timeout=args.timeout,
    )

    clean_json = strip_markdown_code(result)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(clean_json)
        print(f"Saved parsed JSON to: {args.out}")
    else:
        print(clean_json)


def handle_review(args):
    model = resolve_model(args.model, MODEL_ALIASES["pickle"])
    file_paths = args.file or []
    parts = []

    if file_paths:
        for path in file_paths:
            if not os.path.exists(path):
                print(f"Review target file not found: {path}", file=sys.stderr)
                sys.exit(1)
            with open(path, "r", encoding="utf-8") as f:
                parts.append(f"--- FILE: {path} ---\n{f.read()}")

    if not file_paths or args.diff:
        diff_target = args.diff or "HEAD"
        diff_proc = subprocess.run(
            ["git", "diff", diff_target],
            capture_output=True,
            text=True,
        )
        diff_text = diff_proc.stdout.strip()
        if not diff_text and not args.diff:
            diff_proc = subprocess.run(["git", "diff", "--cached"], capture_output=True, text=True)
            diff_text = diff_proc.stdout.strip()
        if diff_text:
            parts.append(f"--- GIT DIFF ({diff_target}) ---\n{diff_text}")

    if not parts:
        print("No changes or files to review.", file=sys.stderr)
        sys.exit(1)

    payload = "\n\n".join(parts)
    focus = args.prompt or "Find bugs, edge cases, null/undefined issues, type coercion bugs, and security flaws."

    if model.startswith(FREE_CLOUD_PREFIXES):
        secret_err = detect_secrets(file_paths, [focus, payload])
        if secret_err:
            print(f"SECURITY BLOCK ({secret_err}). Sensitive diffs must be reviewed in Primary Cloud model.", file=sys.stderr)
            sys.exit(3)

    scope_err = check_scope_limits(model, focus, "", [payload], append=True)
    if scope_err:
        print(f"SCOPE LIMIT EXCEEDED: {scope_err}", file=sys.stderr)
        sys.exit(2)

    system_prompt = (
        "You are a senior code reviewer. Be terse and concrete. "
        "Report only real bugs, edge cases, type/null hazards, or security issues with file/line references. "
        "If none found, reply: NO_ISSUES."
    )
    prompt = f"Focus: {focus}\n\n{payload}"
    result = query_backend(
        command="review",
        model=model,
        prompt=prompt,
        system=system_prompt,
        num_ctx=16384,
        temperature=0.1,
        timeout=args.timeout,
    )
    print(result)


def handle_ask(args):
    """Send a general text prompt (Q&A, drafting, summarization, translation) to local or free cloud models."""
    prompt_text = args.prompt if args.prompt is not None else sys.stdin.read().strip()
    if not prompt_text:
        print("Prompt is empty. Pass --prompt or pipe text via stdin.", file=sys.stderr)
        sys.exit(1)

    model = resolve_model(args.model, MODEL_ALIASES["free"])
    context_files = args.context or []
    context_parts = []
    for path in context_files:
        if not os.path.exists(path):
            print(f"Context file not found: {path}", file=sys.stderr)
            sys.exit(1)
        with open(path, "r", encoding="utf-8") as f:
            context_parts.append(f"--- CONTEXT: {path} ---\n{f.read()}")

    if model.startswith(FREE_CLOUD_PREFIXES):
        secret_err = detect_secrets(context_files, [args.system or "", prompt_text, *context_parts])
        if secret_err:
            print(
                f"SECURITY BLOCK ({secret_err}). Use local Ollama (-m fast / -m coder) for sensitive text.",
                file=sys.stderr,
            )
            sys.exit(3)

    full_prompt = "\n\n".join(context_parts + [prompt_text]) if context_parts else prompt_text
    system_prompt = args.system or "You are a concise, helpful assistant. Answer directly without filler."

    result = query_backend(
        command="ask",
        model=model,
        prompt=full_prompt,
        system=system_prompt,
        num_ctx=args.num_ctx,
        temperature=args.temperature,
        timeout=args.timeout,
    )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(result.rstrip() + "\n")
        print(f"Saved response to: {args.out}")
    else:
        print(result)


def handle_models(args):
    """Discover hardware specs, installed local models, and dynamic role defaults."""
    from model_discovery import build_profile, load_profile, save_profile

    profile = None if args.refresh else load_profile()
    if not profile:
        profile = build_profile(OLLAMA_API)
        if args.save or args.refresh:
            save_profile(profile)

    if args.json:
        print(json.dumps(profile, indent=2))
        return

    hw = profile["hardware"]
    print(f"Hardware: {hw['os']} ({hw['arch']}), RAM: {hw['ram_gb']} GB")
    print(f"Defaults: coder={profile['defaults']['coder']} | text={profile['defaults']['text']}")
    print("Installed local models:")
    if profile["installed"]:
        for m in profile["installed"]:
            print(f"  - {m['name']} ({m['provider']}, {m['size_gb']} GB)")
    else:
        rec = profile["recommended_pull"]
        print(f"  (none) Recommended for your RAM: ollama pull {rec['fast']} && ollama pull {rec['coder']}")


def main():
    parser = argparse.ArgumentParser(description="Antigravity Local LLM Bridge (Ollama)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: code
    p_code = subparsers.add_parser("code", help="Generate code or implement files locally")
    p_code.add_argument("--prompt", "-p", required=True, help="Instruction / task description")
    p_code.add_argument("--file", "-f", help="Target file to implement / edit")
    p_code.add_argument("--out", "-o", help="Output file path (defaults to --file)")
    p_code.add_argument("--context", "-c", action="append", help="Additional context file(s)")
    p_code.add_argument("--model", "-m", default=DEFAULT_CODER_MODEL, help=f"Model name or alias 7b/14b/free/pickle/glm-free/nim (default: {DEFAULT_CODER_MODEL})")
    p_code.add_argument("--num-ctx", type=int, default=16384, help="Context size (default: 16384)")
    p_code.add_argument("--timeout", type=int, default=300, help="Request timeout in seconds (default: 300)")
    p_code.add_argument("--append", action="store_true", help="Append generated snippet to target file instead of overwriting")
    p_code.add_argument("--dry-run", action="store_true", help="Print generated code to stdout without writing file")
    p_code.add_argument("--force", action="store_true", help="Bypass truncation/placeholder guard on overwrite")
    p_code.add_argument("--tsc", action="store_true", help="Run npx tsc --noEmit from nearest tsconfig.json after writing")
    p_code.add_argument("--check", dest="check", action="store_true", default=True, help="Run language-appropriate compiler/syntax check (default: enabled)")
    p_code.add_argument("--no-check", dest="check", action="store_false", help="Skip automatic compiler/syntax check")
    p_code.add_argument("--no-auto-context", action="store_true", help="Disable auto-resolving relative imports from --file")
    p_code.add_argument("--no-rollback", action="store_true", help="Keep failed output in target file instead of rolling back on verify failure")
    p_code.add_argument("--verify", help="Custom verification command to run after writing")
    p_code.add_argument("--retries", type=int, default=1, help="Auto-repair retries on --tsc/--verify failure (default: 1, escalates 7b->14b)")
    p_code.set_defaults(func=handle_code)

    # Subcommand: git-commit
    p_commit = subparsers.add_parser("git-commit", help="Generate conventional commit message from git diff")
    p_commit.add_argument("--apply", "-a", action="store_true", help="Automatically run git commit with generated message")
    p_commit.add_argument("--model", "-m", default=DEFAULT_PARSER_MODEL, help=f"Model name or alias (default: {DEFAULT_PARSER_MODEL})")
    p_commit.add_argument("--timeout", type=int, default=180, help="Request timeout in seconds (default: 180)")
    p_commit.set_defaults(func=handle_git_commit)

    # Subcommand: git-changelog
    p_changelog = subparsers.add_parser("git-changelog", help="Generate changelog / release notes from git log")
    p_changelog.add_argument("--range", "-r", help="Git revision range (e.g. HEAD~15..HEAD)")
    p_changelog.add_argument("--out", "-o", help="Output markdown file")
    p_changelog.add_argument("--model", "-m", default=DEFAULT_PARSER_MODEL, help=f"Model name or alias (default: {DEFAULT_PARSER_MODEL})")
    p_changelog.add_argument("--timeout", type=int, default=180, help="Request timeout in seconds (default: 180)")
    p_changelog.set_defaults(func=handle_git_changelog)

    # Subcommand: parse-json
    p_parse = subparsers.add_parser("parse-json", help="Extract structured JSON from text (Telegram, logs, etc.)")
    p_parse.add_argument("--text", "-t", help="Raw input text")
    p_parse.add_argument("--input", "-i", help="Input file path")
    p_parse.add_argument("--instruction", help="Custom extraction instruction")
    p_parse.add_argument("--schema", "-s", help="Path to JSON Schema file or JSON schema string")
    p_parse.add_argument("--out", "-o", help="Output JSON file")
    p_parse.add_argument("--model", "-m", default=DEFAULT_PARSER_MODEL, help=f"Model name or alias (default: {DEFAULT_PARSER_MODEL})")
    p_parse.add_argument("--num-ctx", type=int, default=8192, help="Context size (default: 8192)")
    p_parse.add_argument("--timeout", type=int, default=180, help="Request timeout in seconds (default: 180)")
    p_parse.set_defaults(func=handle_parse_json)

    # Subcommand: review
    p_review = subparsers.add_parser("review", help="Pre-review git diff or files for bugs and edge cases")
    p_review.add_argument("--diff", "-d", help="Git diff target/range (default: HEAD)")
    p_review.add_argument("--file", "-f", action="append", help="Specific file(s) to review")
    p_review.add_argument("--prompt", "-p", help="Custom review focus")
    p_review.add_argument("--model", "-m", default="pickle", help="Model name or alias (default: pickle; sensitive diffs must be reviewed in Primary Cloud model)")
    p_review.add_argument("--timeout", type=int, default=180, help="Request timeout in seconds (default: 180)")
    p_review.set_defaults(func=handle_review)

    # Subcommand: ask (text/chat/summarization/translation)
    p_ask = subparsers.add_parser("ask", aliases=["chat", "text"], help="Send a text prompt to local or free cloud models")
    p_ask.add_argument("--prompt", "-p", help="Prompt text (reads stdin if omitted)")
    p_ask.add_argument("--system", "-s", help="Optional system instruction")
    p_ask.add_argument("--context", "-c", action="append", help="Optional context file(s)")
    p_ask.add_argument("--out", "-o", help="Optional output file path")
    p_ask.add_argument("--model", "-m", default="free", help="Model name or alias (default: free; also fast/coder/openrouter/glm-free/pickle/nim)")
    p_ask.add_argument("--temperature", "-t", type=float, default=0.3, help="Sampling temperature (default: 0.3)")
    p_ask.add_argument("--num-ctx", type=int, default=8192, help="Context size for Ollama (default: 8192)")
    p_ask.add_argument("--timeout", type=int, default=180, help="Request timeout in seconds (default: 180)")
    p_ask.set_defaults(func=handle_ask)

    # Subcommand: models (dynamic discovery)
    p_models = subparsers.add_parser("models", help="Inspect hardware, installed local models, and dynamic defaults")
    p_models.add_argument("--json", action="store_true", help="Output JSON for agent consumption")
    p_models.add_argument("--refresh", action="store_true", help="Re-scan local runtimes and update models.json")
    p_models.add_argument("--save", action="store_true", help="Save discovered profile to models.json")
    p_models.set_defaults(func=handle_models)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
