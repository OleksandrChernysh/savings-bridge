#!/usr/bin/env python3
"""
Dynamic hardware & local model discovery for Savings Bridge.
Inspects system RAM, OS/arch, and installed Ollama / LM Studio models,
then selects optimal defaults for 'coder', 'text', and 'parser' roles.
"""

import json
import os
import platform
import subprocess
import urllib.request
from pathlib import Path

MODELS_CONFIG_PATH = Path.home() / ".config" / "savings-bridge" / "models.json"


def get_ram_gb() -> float:
    """Return total system RAM in GB across macOS, Linux, and Windows."""
    system = platform.system()
    try:
        if system == "Darwin":
            out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip()
            return round(int(out) / (1024**3), 1)
        if system == "Linux":
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return round(kb / (1024**2), 1)
        if system == "Windows":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return round(stat.ullTotalPhys / (1024**3), 1)
    except Exception:
        pass
    return 0.0


def _fetch_json(url: str, timeout: float = 1.5) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def list_ollama_models(ollama_host: str = "http://127.0.0.1:11434") -> list[dict]:
    data = _fetch_json(f"{ollama_host.rstrip('/')}/api/tags")
    if not data or not isinstance(data.get("models"), list):
        return []
    models = []
    for m in data["models"]:
        name = m.get("name", "")
        if not name:
            continue
        size_gb = round((m.get("size") or 0) / (1024**3), 2)
        details = m.get("details") or {}
        models.append(
            {
                "name": name,
                "provider": "ollama",
                "size_gb": size_gb,
                "param_size": details.get("parameter_size", ""),
                "family": details.get("family", ""),
            }
        )
    return models


def list_lmstudio_models(lms_host: str = "http://127.0.0.1:1234") -> list[dict]:
    data = _fetch_json(f"{lms_host.rstrip('/')}/v1/models")
    if not data or not isinstance(data.get("data"), list):
        return []
    return [
        {"name": m["id"], "provider": "lmstudio", "size_gb": 0.0, "param_size": "", "family": ""}
        for m in data["data"]
        if m.get("id")
    ]


def recommend_for_ram(ram_gb: float) -> dict[str, str]:
    """Suggest Ollama pull targets based on machine RAM."""
    if ram_gb and ram_gb < 10:
        return {"fast": "qwen2.5:3b", "coder": "qwen2.5-coder:3b"}
    if ram_gb and ram_gb < 20:
        return {"fast": "qwen2.5:7b", "coder": "qwen2.5-coder:7b"}
    if ram_gb and ram_gb < 36:
        return {"fast": "qwen2.5:7b", "coder": "qwen2.5-coder:14b"}
    return {"fast": "qwen2.5:14b", "coder": "qwen2.5-coder:32b"}


def _pick_best(models: list[dict], ram_gb: float, prefer_coder: bool) -> str | None:
    if not models:
        return None
    # Leave ~35% RAM headroom for OS/IDE; if RAM unknown, allow all
    max_gb = (ram_gb * 0.65) if ram_gb > 0 else 999.0
    fitting = [m for m in models if m["size_gb"] == 0.0 or m["size_gb"] <= max_gb] or models

    def score(m: dict) -> tuple[int, float]:
        n = m["name"].lower()
        is_coder = int("coder" in n or "code" in n or "deepseek" in n)
        if prefer_coder:
            return (is_coder, m["size_gb"])
        # For fast/text/parser: prefer smaller non-coder or general instruct model
        return (1 - is_coder, -m["size_gb"] if m["size_gb"] > 0 else 0.0)

    return max(fitting, key=score)["name"]


def build_profile(ollama_host: str = "http://127.0.0.1:11434") -> dict:
    ram_gb = get_ram_gb()
    ollama_models = list_ollama_models(ollama_host)
    lms_models = list_lmstudio_models()
    installed = ollama_models + lms_models
    lms_prefixed = [{**m, "name": f"lmstudio/{m['name']}"} for m in lms_models]
    local_pool = ollama_models or lms_prefixed

    coder = _pick_best(local_pool, ram_gb, prefer_coder=True) or "opencode/deepseek-v4.1-flash-free"
    fast = _pick_best(local_pool, ram_gb, prefer_coder=False) or "opencode/deepseek-v4.1-flash-free"

    aliases = {
        "fast": fast,
        "text": fast,
        "parser": fast,
        "coder": coder,
    }
    # Keep legacy 7b/14b aliases mapped to best fitting local models if installed
    if local_pool:
        aliases["7b"] = fast
        aliases["14b"] = coder

    return {
        "hardware": {
            "os": platform.system(),
            "arch": platform.machine(),
            "ram_gb": ram_gb,
        },
        "defaults": {
            "coder": coder,
            "text": fast,
            "parser": fast,
        },
        "aliases": aliases,
        "installed": installed,
        "recommended_pull": recommend_for_ram(ram_gb),
    }


def save_profile(profile: dict, path: Path = MODELS_CONFIG_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    return path


def load_profile(path: Path = MODELS_CONFIG_PATH) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
