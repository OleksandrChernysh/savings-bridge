# AGENTS.md — Savings Bridge Rules

Instructions for AI agents operating in `savings-bridge`.

## 1. Offloading (`savings-bridge`)
- **Mandatory:** Follow `skills/savings-bridge/SKILL.md` (installed to `~/.gemini/antigravity/skills/savings-bridge/SKILL.md` / `~/.claude/skills/savings-bridge/SKILL.md`) to offload eligible work and preserve primary Cloud quota.
- Use `savings-bridge` CLI (`~/.local/bin/savings-bridge`): run `savings-bridge models --json` first, then route atomic `ask`, `code`, `review`, `parse-json`, `git-commit`, and `git-changelog` slices (`-m free` / `-m glm-free` / `-m pickle` for non-sensitive work; `-m fast` / `-m coder` / `-m 7b` / `-m 14b` for local/secret-adjacent work).
- Never call `ollama run` or `localhost:11434` directly; never send `.env*`, keys, tokens, or secrets to external free cloud models.
- Keep architecture, multi-file wiring, complex debugging, sensitive diff review, and final build/test verification in the primary Cloud model.

## 2. Savings Reports & Widget Endpoint
- Show user savings by period:
  - Last day: `savings-bridge stats --period day`
  - Last week: `savings-bridge stats --period week`
  - Last month: `savings-bridge stats --period month`
- HTTP widget endpoint (`savings-bridge metrics`):
  - `GET http://127.0.0.1:4040/metrics/summary?period=day|week|month`
  - `GET http://127.0.0.1:4040/health`

## 3. Cross-Platform & Safe File Changes
- All Python scripts use the Python standard library only and must stay compatible with macOS, Linux, and Windows.
- Preserve `.bak` backups and `--dry-run` support in `setup.py`.
