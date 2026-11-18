# AGENTS.md — Savings Bridge Rules

Instructions for AI agents (Antigravity, Claude Code, Codex, Cursor) operating in or using `savings-bridge`.

## 1. Dynamic Model Discovery First
Before calling `local_bridge.py` on a new or unfamiliar machine, inspect host RAM, installed local models, and dynamic role defaults:
```bash
savings-bridge models --json
```
- Use dynamic role aliases instead of hardcoding model tags:
  - `-m fast` / `-m text` / `-m parser` for short text prompts, summarization, translation, JSON parsing, git commits.
  - `-m coder` for local code generation, helpers, and unit tests.
  - `-m free` / `-m glm-free` / `-m pickle` / `-m nim` for non-sensitive free cloud offloading (`cost: 0`).
- Never call `ollama run` or `http://127.0.0.1:11434` directly — all calls MUST go through `local_bridge.py` so token usage is logged to `~/.config/savings-bridge/local_tokens.jsonl`.

## 2. Routing: What Goes Where
- **Text / Chat / Drafting (`ask` / `chat` / `text`):** Offload summarization, translation, drafting replies, and general Q&A via:
  ```bash
  savings-bridge ask -m free -p "<prompt>" [-c <context_file>]
  ```
  Use `-m fast` for offline or sensitive text.
- **Code / Tests / Review / JSON / Git:** Offload atomic slices via `code`, `review`, `parse-json`, `git-commit`, `git-changelog`.
- **Keep in Primary Cloud Model:** Architecture, multi-file decomposition, newest SDK APIs, final verification, and sensitive logic requiring frontier reasoning. The user's primary model remains unchanged and handles all complex work.

## 3. Security & Key Manager (`exit 3`)
- API keys entered during onboarding are stored locally in `~/.config/savings-bridge/secrets.env` (`chmod 600` on POSIX).
- NEVER send `.env*`, `*.pem`/`*.key`, API keys, passwords, seed phrases, or auth tokens to external free cloud models (`opencode/*`, `nvidia/*`).
- Route secret-adjacent work exclusively to local Ollama (`-m fast` / `-m coder`) or the primary Cloud model.

## 4. Savings Reports & Widget Endpoint
- Show user savings by period:
  - Last day: `savings-stats --period day`
  - Last week: `savings-stats --period week`
  - Last month: `savings-stats --period month`
- HTTP widget endpoint (`metrics_server.py`):
  - `GET http://127.0.0.1:4040/metrics/summary?period=day|week|month`
  - `GET http://127.0.0.1:4040/health`

## 5. Cross-Platform & Safe File Changes
- All Python scripts use the Python standard library only and must stay compatible with macOS, Linux, and Windows.
- Preserve `.bak` backups and `--dry-run` support in `setup.py`.
