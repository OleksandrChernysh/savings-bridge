#!/usr/bin/env python3
"""Shared stdlib helpers for Savings Bridge scripts."""

from __future__ import annotations

try:
    import fcntl
except ImportError:
    fcntl = None

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

STATS_LOG_PATH = os.path.expanduser("~/.config/savings-bridge/local_tokens.jsonl")
SECRETS_ENV_PATH = os.path.expanduser("~/.config/savings-bridge/secrets.env")


def append_usage_record(
    command: str,
    model: str,
    output_tokens: int,
    input_tokens: int,
    duration_sec: float,
    log_path: str = STATS_LOG_PATH,
    dry_run: bool = False,
    dry_run_prefix: str = "[savings-bridge]",
    outcome: str = "accepted",
    estimated: bool = False,
) -> None:
    """Append a single token-usage record to local_tokens.jsonl (flock-safe on POSIX)."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "command": command,
        "model": model,
        "output_tokens": output_tokens,
        "input_tokens": input_tokens,
        "duration_sec": round(duration_sec, 2),
        "outcome": outcome,
    }
    if estimated:
        record["estimated"] = True
    if dry_run:
        print(f"{dry_run_prefix} {json.dumps(record)}", file=sys.stderr)
        return
    try:
        parent = os.path.dirname(log_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            if fcntl is not None:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    f.write(json.dumps(record) + "\n")
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
            else:
                f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def mark_last_usage_outcome(outcome: str, log_path: str = STATS_LOG_PATH) -> None:
    """Update the outcome field of the most recent usage record (e.g. 'refused' or 'rolled_back')."""
    if not os.path.exists(log_path):
        return
    try:
        with open(log_path, "r+", encoding="utf-8") as f:
            if fcntl is not None:
                fcntl.flock(f, fcntl.LOCK_EX)
            try:
                lines = f.read().splitlines()
                if not lines:
                    return
                last = json.loads(lines[-1])
                last["outcome"] = outcome
                lines[-1] = json.dumps(last)
                f.seek(0)
                f.truncate()
                f.write("\n".join(lines) + "\n")
            finally:
                if fcntl is not None:
                    fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        pass


def normalize_http_url(raw: str) -> str:
    """Ensure a host URL has an HTTP(S) scheme and no trailing slash."""
    url = raw.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = f"http://{url}"
    return url


def get_ollama_host() -> str:
    """Return normalized OLLAMA_HOST (default http://127.0.0.1:11434)."""
    return normalize_http_url(os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"))


def get_lmstudio_host() -> str:
    """Return normalized LM_STUDIO_HOST (default http://127.0.0.1:1234)."""
    return normalize_http_url(os.environ.get("LM_STUDIO_HOST", "http://127.0.0.1:1234"))


def fetch_json(url: str, timeout: float = 1.5) -> dict | None:
    """Fetch and decode JSON from a URL, returning None on network/parse error."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
