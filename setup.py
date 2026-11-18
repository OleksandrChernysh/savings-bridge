#!/usr/bin/env python3
"""
Cross-platform onboarding & setup for Savings Bridge (macOS / Linux / Windows).
- Discovers local Ollama / LM Studio models
- Displays provider login / free-tier links from providers.json
- Installs scripts to ~/.gemini/bin/ (or ~/.local/bin/) and skill to ~/.gemini/antigravity/skills/
- Saves API keys securely (chmod 600 on POSIX) to ~/.config/savings-bridge/secrets.env
"""

import argparse
import getpass
import json
import os
import platform
import shutil
from pathlib import Path

from model_discovery import build_profile, list_lmstudio_models, list_ollama_models, save_profile

ROOT = Path(__file__).resolve().parent
PROVIDERS_FILE = ROOT / "providers.json"


def discover_local() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    ollama = [m["name"] for m in list_ollama_models() if m.get("name")]
    if ollama:
        found["ollama"] = ollama

    lms = [m["name"] for m in list_lmstudio_models() if m.get("name")]
    if lms:
        found["lmstudio"] = lms
    return found


def install_files(dry_run: bool = False) -> None:
    home = Path.home()
    bin_dir = home / ".config" / "savings-bridge" / "bin"
    skill_dirs = [home / ".gemini" / "antigravity" / "skills" / "savings-bridge"]
    if (home / ".claude").exists():
        skill_dirs.append(home / ".claude" / "skills" / "savings-bridge")
    if (home / ".codex").exists():
        skill_dirs.append(home / ".codex" / "skills" / "savings-bridge")

    scripts = [
        "bridge_common.py",
        "bridge_guards.py",
        "bridge_backends.py",
        "local_bridge.py",
        "token_stats.py",
        "cli_meter.py",
        "metrics_server.py",
        "model_discovery.py",
    ]
    if dry_run:
        print(f"[DRY-RUN] Would copy {scripts} -> {bin_dir}")
        for sdir in skill_dirs:
            print(f"[DRY-RUN] Would copy skills/savings-bridge/SKILL.md -> {sdir}")
        return

    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in scripts:
        src = ROOT / name
        dst = bin_dir / name
        if dst.exists():
            shutil.copy2(dst, dst.with_suffix(".py.bak"))
        shutil.copy2(src, dst)
        if platform.system() != "Windows":
            os.chmod(dst, 0o755)

    for sdir in skill_dirs:
        sdir.mkdir(parents=True, exist_ok=True)
        sdst = sdir / "SKILL.md"
        if sdst.exists():
            shutil.copy2(sdst, sdst.with_suffix(".md.bak"))
        shutil.copy2(ROOT / "skills" / "savings-bridge" / "SKILL.md", sdst)
    print(f"Installed scripts -> {bin_dir}")
    if platform.system() != "Windows":
        local_bin = home / ".local" / "bin"
        local_bin.mkdir(parents=True, exist_ok=True)
        for cmd_name, target_name in [
            ("savings-bridge", "local_bridge.py"),
            ("local-bridge", "local_bridge.py"),
            ("savings-stats", "token_stats.py"),
        ]:
            link = local_bin / cmd_name
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(bin_dir / target_name)
        print(f"Linked PATH cmds  -> {local_bin} (savings-bridge, local-bridge, savings-stats)")
    for sdir in skill_dirs:
        print(f"Installed skill   -> {sdir}")


def configure_keys(dry_run: bool = False) -> None:
    providers = json.loads(PROVIDERS_FILE.read_text(encoding="utf-8"))["providers"]
    secrets_path = Path.home() / ".config" / "savings-bridge" / "secrets.env"
    entries: list[str] = []

    print("\n--- Cloud Providers (press Enter to skip) ---")
    for p in providers:
        if p["type"] != "cloud":
            continue
        tag = "FREE TIER" if p.get("free_tier") else "PAID"
        print(f"\n• {p['name']} [{tag}]")
        print(f"  Reality:    {p.get('free_reality', p['notes'])}")
        print(f"  Login/Keys: {p['auth_url']}")
        val = getpass.getpass(f"  Enter {p['env_var']} (hidden, Enter to skip): ").strip()
        if val:
            entries.append(f"export {p['env_var']}={json.dumps(val)}")

    if not entries:
        print("\nNo cloud keys entered (local-only mode).")
        return

    if dry_run:
        print(f"[DRY-RUN] Would write {len(entries)} keys -> {secrets_path}")
        return

    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    secrets_path.write_text("\n".join(entries) + "\n", encoding="utf-8")
    if platform.system() != "Windows":
        os.chmod(secrets_path, 0o600)
    print(f"\nSaved {len(entries)} keys -> {secrets_path}")
    print("Note: local_bridge.py loads secrets.env automatically on all OSes.")
    if platform.system() != "Windows":
        print(f"Optional for shell tools: add `source {secrets_path}` to ~/.zshrc or ~/.bashrc")


def main() -> None:
    parser = argparse.ArgumentParser(description="Savings Bridge Onboarding & Setup")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing files")
    parser.add_argument("--skip-keys", action="store_true", help="Skip interactive API key setup")
    args = parser.parse_args()

    print(f"Savings Bridge Setup ({platform.system()} / {platform.machine()})")
    profile = build_profile()
    hw = profile["hardware"]
    print(f"\n--- Hardware & Local Models (RAM: {hw['ram_gb']} GB) ---")
    if profile["installed"]:
        for m in profile["installed"]:
            print(f"• {m['provider']}: {m['name']} ({m['size_gb']} GB)")
        print(f"Dynamic defaults -> coder: {profile['defaults']['coder']} | text/parser: {profile['defaults']['text']}")
    else:
        rec = profile["recommended_pull"]
        print("• None detected on :11434 (Ollama) or :1234 (LM Studio).")
        print(f"  Install Ollama: https://ollama.com/download")
        print(f"  Recommended for {hw['ram_gb']} GB RAM: ollama pull {rec['fast']} && ollama pull {rec['coder']}")

    if not args.dry_run:
        saved = save_profile(profile)
        print(f"Saved dynamic model profile -> {saved}")

    install_files(dry_run=args.dry_run)
    if not args.skip_keys:
        configure_keys(dry_run=args.dry_run)

    print("\nDone! Quick test:")
    print("  savings-bridge ask -m fast -p 'Say hello in 5 words'")
    print("  python3 ~/.config/savings-bridge/bin/metrics_server.py   # -> http://127.0.0.1:4040/metrics/summary")


if __name__ == "__main__":
    main()
