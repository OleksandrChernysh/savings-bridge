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

STATS_LOG_PATH = os.path.expanduser("~/.gemini/antigravity/local_tokens.jsonl")
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
) -> None:
    """Append a single token-usage record to local_tokens.jsonl (flock-safe on POSIX)."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "command": command,
        "model": model,
        "output_tokens": output_tokens,
        "input_tokens": input_tokens,
        "duration_sec": round(duration_sec, 2),
    }
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


def fetch_json(url: str, timeout: float = 1.5) -> dict | None:
    """Fetch and decode JSON from a URL, returning None on network/parse error."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
