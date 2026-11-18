#!/usr/bin/env python3
"""
Universal CLI Token Meter for Antigravity Quota Bar.

Wraps any external CLI (via `cli_meter.py [opts] -- <cmd> ...` or transparent
symlinks in PATH like `~/.local/bin/coderabbit -> ~/.gemini/bin/cli_meter.py`),
streams output live, estimates output tokens from ANSI-stripped text, and
appends standard records to `~/.config/savings-bridge/local_tokens.jsonl`.
"""

import argparse
try:
    import fcntl
except ImportError:
    fcntl = None
import json
import os
try:
    import pty
except ImportError:
    pty = None
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
try:
    import termios
    import tty
except ImportError:
    termios = None
    tty = None
import time
from datetime import datetime, timezone

from bridge_common import STATS_LOG_PATH, append_usage_record

CONFIG_PATH = os.path.expanduser("~/.gemini/antigravity/cli_meter.json")
SELF_REALPATH = os.path.realpath(__file__)

ANSI_RE = re.compile(
    r"\x1B(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1B]*(?:\x07|\x1B\\)|[@-Z\\-_])"
)

SKIP_FLAGS = {"-h", "--help", "-v", "--version", "version", "help"}

DEFAULT_PROFILES: dict[str, dict] = {
    "coderabbit": {
        "model": "coderabbit/cli",
        "command": "review",
        "chars_per_token": 4.0,
        "local": False,
    },
    "opencode": {
        "model": "opencode/cli",
        "command": "cli",
        "chars_per_token": 4.0,
        "local": False,
    },
    "ollama": {
        "model": "ollama-cli",
        "command": "cli",
        "chars_per_token": 4.0,
        "local": True,
    },
}


def load_profiles() -> dict[str, dict]:
    profiles = {k: dict(v) for k, v in DEFAULT_PROFILES.items()}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            if isinstance(user_cfg, dict):
                for name, cfg in user_cfg.items():
                    if isinstance(cfg, dict):
                        base = profiles.get(name, {})
                        base.update(cfg)
                        profiles[name] = base
        except Exception:
            pass
    return profiles


def resolve_real_binary(tool_name: str) -> str | None:
    """Find `tool_name` in PATH, skipping symlinks/files pointing to this script."""
    if os.path.isabs(tool_name):
        return tool_name if os.path.realpath(tool_name) != SELF_REALPATH else None

    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = os.path.join(directory, tool_name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            if os.path.realpath(candidate) != SELF_REALPATH:
                return candidate
    return None


def infer_model_and_command(
    tool_name: str, args: list[str], profile: dict
) -> tuple[str, str]:
    model = profile.get("model") or (
        f"{tool_name}-cli" if profile.get("local") else f"{tool_name}/cli"
    )
    command = profile.get("command") or "cli"

    # Extract actual model name for `ollama run <model>` without '/' so it logs as local
    if tool_name == "ollama" and len(args) >= 2 and args[0] == "run":
        raw_model = args[1]
        model = raw_model.replace("/", "-") if "/" in raw_model else raw_model
        command = "run"
    elif tool_name == "coderabbit" and args:
        sub = next((a for a in args if not a.startswith("-")), None)
        if sub:
            command = sub

    return model, command


def estimate_tokens(raw_bytes: bytes, chars_per_token: float) -> int:
    text = raw_bytes.decode("utf-8", errors="replace")
    clean = ANSI_RE.sub("", text).strip()
    if not clean:
        return 0
    return max(1, int(round(len(clean) / max(1.0, chars_per_token))))


def log_usage(
    command: str,
    model: str,
    output_tokens: int,
    input_tokens: int,
    duration_sec: float,
) -> None:
    if output_tokens <= 0:
        return
    append_usage_record(
        command=command,
        model=model,
        output_tokens=output_tokens,
        input_tokens=input_tokens,
        duration_sec=duration_sec,
        log_path=STATS_LOG_PATH,
        dry_run=os.environ.get("CLI_METER_DRY_RUN") == "1",
        dry_run_prefix="[cli_meter]",
    )


def run_with_pty(cmd: list[str]) -> tuple[int, bytes]:
    master_fd, slave_fd = pty.openpty()

    def copy_winsize():
        try:
            ws = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\x00" * 8)
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, ws)
        except Exception:
            pass

    copy_winsize()
    prev_winch = signal.signal(signal.SIGWINCH, lambda *_: copy_winsize())

    proc = subprocess.Popen(
        cmd,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
    )
    os.close(slave_fd)

    stdin_fd = sys.stdin.fileno()
    old_tty = None
    try:
        old_tty = termios.tcgetattr(stdin_fd)
        tty.setraw(stdin_fd)
    except Exception:
        old_tty = None

    captured = bytearray()
    try:
        while True:
            watch = [master_fd]
            if old_tty is not None:
                watch.append(stdin_fd)
            rlist, _, _ = select.select(watch, [], [], 0.1)
            if master_fd in rlist:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                captured.extend(data)
                os.write(sys.stdout.fileno(), data)
            if stdin_fd in rlist:
                try:
                    inp = os.read(stdin_fd, 1024)
                except OSError:
                    inp = b""
                if inp:
                    os.write(master_fd, inp)
            if proc.poll() is not None and master_fd not in rlist:
                # Drain any remaining bytes
                while True:
                    try:
                        r2, _, _ = select.select([master_fd], [], [], 0.02)
                        if not r2:
                            break
                        data = os.read(master_fd, 4096)
                        if not data:
                            break
                        captured.extend(data)
                        os.write(sys.stdout.fileno(), data)
                    except OSError:
                        break
                break
    finally:
        if old_tty is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_tty)
        signal.signal(signal.SIGWINCH, prev_winch)
        try:
            os.close(master_fd)
        except OSError:
            pass

    return proc.wait(), bytes(captured)


def run_with_pipes(cmd: list[str]) -> tuple[int, bytes, int]:
    stdin_bytes = b""
    stdin_arg = None
    if not sys.stdin.isatty():
        stdin_bytes = sys.stdin.buffer.read()
        stdin_arg = subprocess.PIPE

    proc = subprocess.Popen(
        cmd,
        stdin=stdin_arg,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if os.name == "nt":
        out_b, err_b = proc.communicate(input=stdin_bytes if stdin_arg is not None else None)
        if out_b:
            sys.stdout.buffer.write(out_b)
            sys.stdout.buffer.flush()
        if err_b:
            sys.stderr.buffer.write(err_b)
            sys.stderr.buffer.flush()
        return proc.returncode, (out_b or b"") + (err_b or b""), len(stdin_bytes)

    if stdin_arg is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_bytes)
            proc.stdin.close()
        except BrokenPipeError:
            pass

    captured = bytearray()
    streams = [s for s in (proc.stdout, proc.stderr) if s is not None]
    fd_map = {s.fileno(): s for s in streams}

    while fd_map:
        rlist, _, _ = select.select(list(fd_map.keys()), [], [])
        for fd in rlist:
            chunk = os.read(fd, 4096)
            if not chunk:
                fd_map[fd].close()
                del fd_map[fd]
                continue
            captured.extend(chunk)
            target = sys.stdout.buffer if fd_map[fd] is proc.stdout else sys.stderr.buffer
            target.write(chunk)
            target.flush()

    return proc.wait(), bytes(captured), len(stdin_bytes)


def parse_invocation() -> tuple[str, list[str], dict]:
    invoked_as = os.path.basename(sys.argv[0])
    profiles = load_profiles()

    if invoked_as not in ("cli_meter.py", "cli_meter"):
        # Transparent symlink mode
        profile = dict(profiles.get(invoked_as, {}))
        return invoked_as, sys.argv[1:], profile

    parser = argparse.ArgumentParser(
        description="Universal CLI token meter for Antigravity Quota Bar.",
        usage="cli_meter.py [--model MODEL] [--command CMD] [--local] [--chars-per-token N] -- <cli> [args...]",
    )
    parser.add_argument("--model", help="Model identifier (use '/' for free cloud, no '/' for local)")
    parser.add_argument("--command", help="Command label in local_tokens.jsonl")
    parser.add_argument("--local", action="store_true", help="Force local classification (no '/')")
    parser.add_argument("--chars-per-token", type=float, default=None, help="Estimator ratio (default: 4.0)")
    parser.add_argument("rest", nargs=argparse.REMAINDER, help="Wrapped command after '--'")

    ns = parser.parse_args()
    cmd_args = list(ns.rest)
    if cmd_args and cmd_args[0] == "--":
        cmd_args = cmd_args[1:]
    if not cmd_args:
        parser.print_help(sys.stderr)
        sys.exit(2)

    tool_name = os.path.basename(cmd_args[0])
    profile = dict(profiles.get(tool_name, {}))
    if ns.model:
        profile["model"] = ns.model
    if ns.command:
        profile["command"] = ns.command
    if ns.local:
        profile["local"] = True
    if ns.chars_per_token is not None:
        profile["chars_per_token"] = ns.chars_per_token

    return cmd_args[0], cmd_args[1:], profile


def main() -> int:
    tool, args, profile = parse_invocation()
    real_bin = resolve_real_binary(tool)
    if not real_bin:
        print(f"cli_meter: command not found: {tool}", file=sys.stderr)
        return 127

    if os.environ.get("CLI_METER_DISABLE") == "1" or (
        len(args) == 1 and args[0] in SKIP_FLAGS
    ):
        os.execv(real_bin, [tool] + args)

    tool_name = os.path.basename(tool)
    model, command = infer_model_and_command(tool_name, args, profile)
    chars_per_token = float(profile.get("chars_per_token", 4.0))

    start = time.monotonic()
    input_bytes = 0
    if sys.stdin.isatty() and sys.stdout.isatty() and pty is not None and termios is not None and tty is not None:
        rc, output_bytes = run_with_pty([real_bin] + args)
    else:
        rc, output_bytes, input_bytes = run_with_pipes([real_bin] + args)
    duration = time.monotonic() - start

    out_tokens = estimate_tokens(output_bytes, chars_per_token)
    in_tokens = max(1, int(round(input_bytes / chars_per_token))) if input_bytes > 0 else 0
    log_usage(command=command, model=model, output_tokens=out_tokens, input_tokens=in_tokens, duration_sec=duration)
    return rc


if __name__ == "__main__":
    sys.exit(main())
