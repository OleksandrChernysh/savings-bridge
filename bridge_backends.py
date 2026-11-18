#!/usr/bin/env python3
"""Backend clients, secrets loader, model alias resolution, and Ollama locking."""

from __future__ import annotations

try:
    import fcntl
except ImportError:
    fcntl = None

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

from bridge_common import SECRETS_ENV_PATH, STATS_LOG_PATH, append_usage_record

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

OPENAI_COMPAT_PROVIDERS = {
    "nvidia/": ("https://integrate.api.nvidia.com/v1/chat/completions", "NVIDIA_API_KEY", "NVIDIA NIM", "build.nvidia.com"),
    "openrouter/": ("https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY", "OpenRouter", "openrouter.ai/keys"),
    "groq/": ("https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY", "Groq", "console.groq.com/keys"),
    "github/": ("https://models.inference.ai.azure.com/chat/completions", "GITHUB_TOKEN", "GitHub Models", "github.com/marketplace/models"),
    "lmstudio/": (f"{os.environ.get('LM_STUDIO_HOST', 'http://127.0.0.1:1234').rstrip('/')}/v1/chat/completions", None, "LM Studio", "lmstudio.ai"),
}

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


def log_local_usage(command: str, model: str, eval_count: int, prompt_eval_count: int, duration_sec: float) -> None:
    """Log local or free-cloud token usage to STATS_LOG_PATH."""
    append_usage_record(
        command=command,
        model=model,
        output_tokens=eval_count,
        input_tokens=prompt_eval_count,
        duration_sec=duration_sec,
        log_path=STATS_LOG_PATH,
    )


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
