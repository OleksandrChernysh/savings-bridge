#!/usr/bin/env python3
"""
Savings Bridge Token Usage Analytics
Compares primary cloud token consumption vs local & free-cloud token generation.
"""

import os
import glob
import json
import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict

from bridge_common import STATS_LOG_PATH

TRANSCRIPT_GLOB = os.path.expanduser("~/.gemini/antigravity/brain/*/.system_generated/logs/transcript.jsonl")
LOCAL_LOG_PATH = STATS_LOG_PATH


def load_cloud_stats(days_limit: int | None = None):
    daily = defaultdict(lambda: {"inp": 0, "out": 0, "cache": 0, "steps": 0, "convs": set()})
    
    for path in glob.glob(TRANSCRIPT_GLOB):
        conv_id = Path(path).parts[-4]
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        created = obj.get("created_at", "")
                        if not created:
                            continue
                        day = created[:10]
                        inp = obj.get("input_tokens") or 0
                        out = obj.get("output_tokens") or 0
                        cache = obj.get("cache_read_tokens") or 0
                        if inp or out:
                            daily[day]["inp"] += inp
                            daily[day]["out"] += out
                            daily[day]["cache"] += cache
                            daily[day]["steps"] += 1
                            daily[day]["convs"].add(conv_id)
                    except (json.JSONDecodeError, ValueError, TypeError):
                        pass
        except OSError:
            pass

    return daily


load_gemini_stats = load_cloud_stats


def load_local_stats():
    daily_local = defaultdict(lambda: {"calls": 0, "local_out": 0, "local_inp": 0, "duration": 0.0, "commands": defaultdict(int), "cmd_out": defaultdict(int), "cmd_inp": defaultdict(int)})
    if os.path.exists(LOCAL_LOG_PATH):
        try:
            with open(LOCAL_LOG_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        ts = rec.get("timestamp", "")
                        day = ts[:10]
                        out = rec.get("output_tokens") or 0
                        inp = rec.get("input_tokens") or 0
                        dur = rec.get("duration_sec") or 0.0
                        cmd = rec.get("command", "unknown")
                        
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
    parser = argparse.ArgumentParser(description="Savings Bridge Token Savings & Usage Tracker")
    parser.add_argument("--days", "-d", type=int, default=None, help="Number of days to show (default: 10)")
    parser.add_argument(
        "--period",
        "-p",
        choices=["day", "week", "month"],
        help="Preset period: day (1 day), week (7 days), month (30 days)",
    )
    args = parser.parse_args()

    period_map = {"day": 1, "week": 7, "month": 30}
    days = period_map[args.period] if args.period else (args.days if args.days is not None else 10)

    cloud_stats = load_cloud_stats(days)
    local_stats = load_local_stats()

    all_days = sorted(set(list(cloud_stats.keys()) + list(local_stats.keys())))
    if days and len(all_days) > days:
        all_days = all_days[-days:]

    print("\n" + "=" * 98)
    print("                  SAVINGS BRIDGE TOKEN CONSUMPTION & LOCAL SAVINGS REPORT")
    print("=" * 98)
    
    header = (
        f"{'Date':<10} | "
        f"{'Cloud Inp':>12} | "
        f"{'Cloud Out':>11} | "
        f"{'Cloud Tot':>12} | "
        f"{'Local Calls':>11} | "
        f"{'Local Out (Free)':>16} | "
        f"{'Saved Out %':>11}"
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

        # Calculate percentage of output tokens offloaded locally
        total_out_needed = gout + lout
        saved_pct = (lout / total_out_needed * 100) if total_out_needed > 0 else 0.0

        print(
            f"{day:<10} | "
            f"{ginp:>12,} | "
            f"{gout:>11,} | "
            f"{gtot:>12,} | "
            f"{lcalls:>11,} | "
            f"{lout:>16,} | "
            f"{saved_pct:>10.1f}%"
        )

    print("-" * 98)
    tot_needed = total_gemini_out + total_local_out
    overall_saved = (total_local_out / tot_needed * 100) if tot_needed > 0 else 0.0

    print(
        f"{'TOTAL':<10} | "
        f"{total_gemini_inp:>12,} | "
        f"{total_gemini_out:>11,} | "
        f"{total_gemini_inp + total_gemini_out:>12,} | "
        f"{total_local_calls:>11,} | "
        f"{total_local_out:>16,} | "
        f"{overall_saved:>10.1f}%"
    )
    print("=" * 98 + "\n")

    if total_local_calls > 0:
        ask_calls = sum(local_stats[d]["commands"].get("ask", 0) for d in all_days)
        ask_out = sum(local_stats[d]["cmd_out"].get("ask", 0) for d in all_days)
        code_calls = total_local_calls - ask_calls
        code_out = total_local_out - ask_out
        print(f"🎉 Free tokens produced locally/free-cloud: {total_local_out:,} tokens across {total_local_calls} operations.")
        print(f"   💬 Text/Chat (ask): {ask_calls:,} calls / {ask_out:,} out tokens  |  ⚡ Code/Tools: {code_calls:,} calls / {code_out:,} out tokens")
    else:
        print("💡 Baseline recorded. As you run local commands, Local Out tokens and Savings % will populate.")


if __name__ == "__main__":
    main()
