#!/usr/bin/env python3
"""
Cross-platform onboarding & setup for Savings Bridge (macOS / Linux / Windows).
- Discovers local Ollama / LM Studio models
- Displays provider login / free-tier links from providers.json
- Installs scripts to ~/.config/savings-bridge/bin/ (and PATH links in ~/.local/bin/) and skill to ~/.gemini/config/skills/
- Saves API keys securely (chmod 600 on POSIX) to ~/.config/savings-bridge/secrets.env
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

if sys.version_info < (3, 9):
    sys.exit("Python 3.9+ is required to run Savings Bridge.")

ROOT = Path(__file__).resolve().parent
SRC_DIR = ROOT / "src"
PROVIDERS_FILE = ROOT / "config" / "providers.json"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from model_discovery import build_profile, list_lmstudio_models, list_ollama_models, save_profile


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
    skill_dirs = [home / ".gemini" / "config" / "skills" / "savings-bridge"]
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
        src = SRC_DIR / name
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
        for legacy_cmd in ("local-bridge", "savings-stats"):
            legacy_link = local_bin / legacy_cmd
            if legacy_link.is_symlink():
                legacy_link.unlink()
        link = local_bin / "savings-bridge"
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            shutil.copy2(link, link.with_suffix(".bak"))
            link.unlink()
        link.symlink_to(bin_dir / "local_bridge.py")
        print(f"Linked PATH cmd   -> {local_bin / 'savings-bridge'}")
        path_entries = [os.path.realpath(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p.strip()]
        if os.path.realpath(str(local_bin)) not in path_entries:
            print(f'Add to PATH (~/.zshrc or ~/.bashrc): export PATH="$HOME/.local/bin:$PATH"')
    else:
        for legacy_cmd in ("local-bridge.cmd", "savings-stats.cmd"):
            legacy_path = bin_dir / legacy_cmd
            if legacy_path.exists():
                legacy_path.unlink()
        cmd_path = bin_dir / "savings-bridge.cmd"
        cmd_path.write_text('@echo off\r\npy -3 "%~dp0local_bridge.py" %*\r\n', encoding="utf-8")
        print(f"Created Windows .cmd wrapper -> {cmd_path}")
        path_entries = [p.strip().lower() for p in os.environ.get("PATH", "").split(os.pathsep) if p.strip()]
        if str(bin_dir).lower() not in path_entries:
            print(
                "Add to Windows User PATH (PowerShell):\n"
                f'  [Environment]::SetEnvironmentVariable("PATH", [Environment]::GetEnvironmentVariable("PATH", "User") + ";{bin_dir}", "User")'
            )
    for sdir in skill_dirs:
        print(f"Installed skill   -> {sdir}")

    hooks_dir = ROOT / ".githooks"
    if (ROOT / ".git").exists() and hooks_dir.exists():
        if platform.system() != "Windows":
            for hook in hooks_dir.iterdir():
                if hook.is_file():
                    os.chmod(hook, 0o755)
        subprocess.run(["git", "-C", str(ROOT), "config", "core.hooksPath", ".githooks"], check=False)
        print("Configured git hooksPath -> .githooks")


def migrate_legacy_logs(
    dry_run: bool = False,
    legacy_path: Path | None = None,
    target_path: Path | None = None,
) -> int:
    """One-time merge of legacy ~/.gemini/antigravity/local_tokens.jsonl into ~/.config/savings-bridge/local_tokens.jsonl."""
    home = Path.home()
    src = legacy_path or (home / ".gemini" / "antigravity" / "local_tokens.jsonl")
    dst = target_path or (home / ".config" / "savings-bridge" / "local_tokens.jsonl")
    if not src.exists():
        return 0

    src_lines = [ln.strip() for ln in src.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not src_lines:
        return 0

    dst_lines: list[str] = []
    if dst.exists():
        dst_lines = [ln.strip() for ln in dst.read_text(encoding="utf-8").splitlines() if ln.strip()]

    seen = set(dst_lines)
    added = [ln for ln in src_lines if ln not in seen]

    if dry_run:
        print(f"[DRY-RUN] Would merge {len(added)} legacy records from {src} -> {dst}")
        return len(added)

    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.copy2(dst, dst.with_suffix(".jsonl.bak"))
    merged = dst_lines + added
    dst.write_text("\n".join(merged) + "\n", encoding="utf-8")
    src.rename(src.with_suffix(".jsonl.migrated.bak"))
    print(f"Migrated {len(added)} legacy records -> {dst} (backup: {src.with_suffix('.jsonl.migrated.bak')})")
    return len(added)


SUPPORTED_CLOUD_IDS = {"openrouter", "nvidia", "groq", "github", "opencode"}


def _parse_existing_secrets(secrets_path: Path) -> dict[str, str]:
    existing: dict[str, str] = {}
    if not secrets_path.exists():
        return existing
    try:
        for raw in secrets_path.read_text(encoding="utf-8").splitlines():
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
            if key:
                existing[key] = val
    except Exception:
        pass
    return existing


def configure_keys(dry_run: bool = False, secrets_path: Path | None = None) -> None:
    providers = json.loads(PROVIDERS_FILE.read_text(encoding="utf-8"))["providers"]
    secrets_path = secrets_path or (Path.home() / ".config" / "savings-bridge" / "secrets.env")
    new_keys: dict[str, str] = {}

    print("\n--- Cloud Providers (press Enter to skip) ---")
    for p in providers:
        if p["type"] != "cloud" or p.get("id") not in SUPPORTED_CLOUD_IDS:
            continue
        tag = "FREE TIER" if p.get("free_tier") else "PAID"
        print(f"\n• {p['name']} [{tag}]")
        print(f"  Reality:    {p.get('free_reality', p['notes'])}")
        print(f"  Login/Keys: {p['auth_url']}")
        val = getpass.getpass(f"  Enter {p['env_var']} (hidden, Enter to skip): ").strip()
        if val:
            new_keys[p["env_var"]] = val

    if not new_keys:
        print("\nNo cloud keys entered (existing secrets unchanged).")
        return

    if dry_run:
        print(f"[DRY-RUN] Would merge {len(new_keys)} keys -> {secrets_path}")
        return

    merged = _parse_existing_secrets(secrets_path)
    merged.update(new_keys)
    lines = [f"export {k}={json.dumps(v)}" for k, v in merged.items()]

    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    if secrets_path.exists():
        shutil.copy2(secrets_path, secrets_path.with_suffix(".env.bak"))
    if platform.system() != "Windows":
        fd = os.open(str(secrets_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(secrets_path, 0o600)
    else:
        secrets_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\nSaved {len(merged)} keys -> {secrets_path}")
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
    migrate_legacy_logs(dry_run=args.dry_run)
    if not args.skip_keys:
        configure_keys(dry_run=args.dry_run)

    print("\nDone! Quick test:")
    print("  savings-bridge ask -m fast -p 'Say hello in 5 words'")
    print("  savings-bridge stats --period day")
    print("  savings-bridge metrics   # -> http://127.0.0.1:4040/metrics/summary")


if __name__ == "__main__":
    main()
