#!/usr/bin/env python3
"""
Savings Bridge Token Usage Analytics
Compares primary cloud token consumption vs local & free-cloud token generation.
"""

from __future__ import annotations

import os
import sys
import glob
import json
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

from bridge_common import STATS_LOG_PATH

TRANSCRIPT_GLOB = os.path.expanduser("~/.gemini/antigravity/brain/*/.system_generated/logs/transcript.jsonl")
CLAUDE_TRANSCRIPT_GLOB = os.path.expanduser("~/.claude/projects/**/*.jsonl")
CODEX_TRANSCRIPT_GLOB = os.path.expanduser("~/.codex/sessions/**/*.jsonl")
LOCAL_LOG_PATH = STATS_LOG_PATH


def get_cutoff_day(days_limit: int | None) -> str | None:
    """Return inclusive YYYY-MM-DD local date cutoff for the last N calendar days."""
    if not days_limit or days_limit <= 0:
        return None
    today_local = datetime.now().date()
    return (today_local - timedelta(days=days_limit - 1)).isoformat()


def _to_local_day(ts: str) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ts[:10]


def load_cloud_stats(days_limit: int | None = None):
    daily = defaultdict(lambda: {"inp": 0, "out": 0, "cache": 0, "steps": 0, "convs": set()})
    cutoff = get_cutoff_day(days_limit)

    def _record(day: str, inp: int, out: int, cache: int, conv_id: str) -> None:
        if not day or (cutoff and day < cutoff):
            return
        if inp or out:
            daily[day]["inp"] += inp
            daily[day]["out"] += out
            daily[day]["cache"] += cache
            daily[day]["steps"] += 1
            daily[day]["convs"].add(conv_id)

    for path in glob.glob(TRANSCRIPT_GLOB):
        conv_id = Path(path).parts[-4]
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        day = _to_local_day(obj.get("created_at", ""))
                        _record(
                            day,
                            obj.get("input_tokens") or 0,
                            obj.get("output_tokens") or 0,
                            obj.get("cache_read_tokens") or 0,
                            conv_id,
                        )
                    except (json.JSONDecodeError, ValueError, TypeError):
                        pass
        except OSError:
            pass

    for path in glob.glob(CLAUDE_TRANSCRIPT_GLOB, recursive=True):
        conv_id = Path(path).stem
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        usage = (obj.get("message") or {}).get("usage")
                        if not isinstance(usage, dict):
                            continue
                        day = _to_local_day(obj.get("timestamp", ""))
                        _record(
                            day,
                            usage.get("input_tokens") or 0,
                            usage.get("output_tokens") or 0,
                            usage.get("cache_read_input_tokens") or 0,
                            conv_id,
                        )
                    except (json.JSONDecodeError, ValueError, TypeError):
                        pass
        except OSError:
            pass

    for path in glob.glob(CODEX_TRANSCRIPT_GLOB, recursive=True):
        conv_id = Path(path).stem
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        payload = obj.get("payload") or {}
                        usage = (
                            (payload.get("info") or {}).get("last_token_usage")
                            or payload.get("usage")
                            or obj.get("usage")
                        )
                        if not isinstance(usage, dict):
                            continue
                        day = _to_local_day(obj.get("timestamp", ""))
                        _record(
                            day,
                            usage.get("input_tokens") or 0,
                            usage.get("output_tokens") or 0,
                            usage.get("cached_input_tokens") or 0,
                            conv_id,
                        )
                    except (json.JSONDecodeError, ValueError, TypeError):
                        pass
        except OSError:
            pass

    return daily


load_gemini_stats = load_cloud_stats


def load_local_stats(days_limit: int | None = None):
    daily_local = defaultdict(
        lambda: {
            "calls": 0,
            "local_out": 0,
            "local_inp": 0,
            "estimated_out": 0,
            "rejected_calls": 0,
            "duration": 0.0,
            "commands": defaultdict(int),
            "cmd_out": defaultdict(int),
            "cmd_inp": defaultdict(int),
        }
    )
    cutoff = get_cutoff_day(days_limit)
    if os.path.exists(LOCAL_LOG_PATH):
        try:
            with open(LOCAL_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        day = _to_local_day(rec.get("timestamp", ""))
                        if not day or (cutoff and day < cutoff):
                            continue
                        if rec.get("outcome") in ("refused", "rolled_back"):
                            daily_local[day]["rejected_calls"] += 1
                            continue
                        out = rec.get("output_tokens") or 0
                        inp = rec.get("input_tokens") or 0
                        dur = rec.get("duration_sec") or 0.0
                        cmd = rec.get("command", "unknown")

                        if rec.get("estimated"):
                            daily_local[day]["estimated_out"] += out
                        daily_local[day]["calls"] += 1
                        daily_local[day]["local_out"] += out
                        daily_local[day]["local_inp"] += inp
                        daily_local[day]["duration"] += dur
                        daily_local[day]["commands"][cmd] += 1
                        daily_local[day]["cmd_out"][cmd] += out
                        daily_local[day]["cmd_inp"][cmd] += inp
                    except (json.JSONDecodeError, ValueError, TypeError):
                        pass
        except OSError:
            pass
    return daily_local


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    parser = argparse.ArgumentParser(description="Savings Bridge Token Savings & Usage Tracker")
    parser.add_argument("--days", "-d", type=int, default=None, help="Number of calendar days to show (default: 10)")
    parser.add_argument(
        "--period",
        "-p",
        choices=["day", "week", "month"],
        help="Preset calendar period: day (today), week (7 days), month (30 days)",
    )
    args = parser.parse_args()

    period_map = {"day": 1, "week": 7, "month": 30}
    days = period_map[args.period] if args.period else (args.days if args.days is not None else 10)
    cutoff = get_cutoff_day(days)

    cloud_stats = load_cloud_stats(days)
    local_stats = load_local_stats(days)

    all_days = sorted(d for d in set(list(cloud_stats.keys()) + list(local_stats.keys())) if not cutoff or d >= cutoff)

    print("\n" + "=" * 98)
    print("                  SAVINGS BRIDGE TOKEN CONSUMPTION & FREE SHARE REPORT")
    print("=" * 98)

    header = (
        f"{'Date':<10} | "
        f"{'Cloud Inp':>12} | "
        f"{'Cloud Out':>11} | "
        f"{'Cloud Tot':>12} | "
        f"{'Local Calls':>11} | "
        f"{'Local Out (Free)':>16} | "
        f"{'Free Share %':>12}"
    )
    print(header)
    print("-" * 98)

    total_gemini_inp = 0
    total_gemini_out = 0
    total_local_out = 0
    total_local_calls = 0

    for day in all_days:
        g = cloud_stats[day]
        l = local_stats[day]

        ginp = g["inp"]
        gout = g["out"]
        gtot = ginp + gout

        lcalls = l["calls"]
        lout = l["local_out"]

        total_gemini_inp += ginp
        total_gemini_out += gout
        total_local_out += lout
        total_local_calls += lcalls

        if gout > 0:
            saved_str = f"{(lout / (gout + lout) * 100):>11.1f}%"
        else:
            saved_str = f"{'n/a':>12}"

        print(
            f"{day:<10} | "
            f"{ginp:>12,} | "
            f"{gout:>11,} | "
            f"{gtot:>12,} | "
            f"{lcalls:>11,} | "
            f"{lout:>16,} | "
            f"{saved_str}"
        )

    print("-" * 98)
    if total_gemini_out > 0:
        overall_str = f"{(total_local_out / (total_gemini_out + total_local_out) * 100):>11.1f}%"
    else:
        overall_str = f"{'n/a':>12}"

    print(
        f"{'TOTAL':<10} | "
        f"{total_gemini_inp:>12,} | "
        f"{total_gemini_out:>11,} | "
        f"{total_gemini_inp + total_gemini_out:>12,} | "
        f"{total_local_calls:>11,} | "
        f"{total_local_out:>16,} | "
        f"{overall_str}"
    )
    print("=" * 98 + "\n")

    if total_local_calls > 0:
        ask_calls = sum(local_stats[d]["commands"].get("ask", 0) for d in all_days)
        ask_out = sum(local_stats[d]["cmd_out"].get("ask", 0) for d in all_days)
        code_calls = total_local_calls - ask_calls
        code_out = total_local_out - ask_out
        print(f"Free tokens produced locally/free-cloud: {total_local_out:,} tokens across {total_local_calls} operations.")
        print(f"  Text/Chat (ask): {ask_calls:,} calls / {ask_out:,} out tokens  |  Code/Tools: {code_calls:,} calls / {code_out:,} out tokens")
    else:
        print("Baseline recorded. As you run local commands, Local Out tokens and Savings % will populate.")


if __name__ == "__main__":
    main()
