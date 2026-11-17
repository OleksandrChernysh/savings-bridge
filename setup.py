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
import urllib.request
from pathlib import Path

from model_discovery import build_profile, save_profile

ROOT = Path(__file__).resolve().parent
PROVIDERS_FILE = ROOT / "providers.json"


def probe_json(url: str, timeout: float = 1.5) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def discover_local() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    ollama = probe_json("http://127.0.0.1:11434/api/tags")
    if ollama and isinstance(ollama.get("models"), list):
        found["ollama"] = [m.get("name", "") for m in ollama["models"] if m.get("name")]

    lms = probe_json("http://127.0.0.1:1234/v1/models")
    if lms and isinstance(lms.get("data"), list):
        found["lmstudio"] = [m.get("id", "") for m in lms["data"] if m.get("id")]
    return found


def install_files(dry_run: bool = False) -> None:
    home = Path.home()
    bin_dir = home / ".gemini" / "bin"
    skill_dirs = [home / ".gemini" / "antigravity" / "skills" / "local-coder"]
    if (home / ".claude").exists():
        skill_dirs.append(home / ".claude" / "skills" / "local-coder")

    scripts = ["local_bridge.py", "token_stats.py", "cli_meter.py", "metrics_server.py", "model_discovery.py"]
    if dry_run:
        print(f"[DRY-RUN] Would copy {scripts} -> {bin_dir}")
        for sdir in skill_dirs:
            print(f"[DRY-RUN] Would copy skills/local-coder/SKILL.md -> {sdir}")
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
        shutil.copy2(ROOT / "skills" / "local-coder" / "SKILL.md", sdst)
    print(f"Installed scripts -> {bin_dir}")
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
    print("  python3 ~/.gemini/bin/local_bridge.py ask -m fast -p 'Say hello in 5 words'")
    print("  python3 ~/.gemini/bin/metrics_server.py   # -> http://127.0.0.1:4040/metrics/summary")


if __name__ == "__main__":
    main()
