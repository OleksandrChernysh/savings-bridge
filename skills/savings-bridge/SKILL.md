---
name: savings-bridge
description: Offloads atomic code generation, tests, git commits, and JSON parsing to local models (Ollama / LM Studio) or free cloud tiers with hard scope limits.
---

# Local & Free-Cloud Coder Bridge

Use `savings-bridge` to offload well-scoped, atomic tasks to local or free cloud models instead of spending your primary paid model tokens.
**NEVER call `ollama run` or `localhost:11434` directly** — all calls MUST go through `savings-bridge` so tokens are logged to `~/.config/savings-bridge/local_tokens.jsonl` (compatible with Antigravity Quota Bar & `token_stats.py`).

## Dynamic Model Discovery (Run First on New Machines)
Run `savings-bridge models --json` to inspect host RAM, installed local models (Ollama / LM Studio), and dynamic aliases (`fast`, `coder`, `text`, `parser`). Prefer `-m coder` / `-m fast` (or explicit model tags) so selection adapts to the user's hardware.

## Model Routing & Hard Scope Limits

`savings-bridge` enforces hard limits (`exit 2` if exceeded) so oversized tasks are always split first:

| Tier | Model / Alias | Use Cases | Max Overwrite | Max Context (`-f` + `-c`) | Max Prompt |
| :--- | :--- | :--- | :---: | :---: | :---: |
| **`-m fast`** (`7b` legacy) | Auto-selected small local model (e.g. `qwen2.5:3b`/`7b`) | Pure helpers, validators (10–40 lines), `parse-json`, `git-commit`, small sensitive helpers | 60 lines | 300 lines / 12k chars | 1,500 chars |
| **`-m coder`** (`14b` legacy) | Auto-selected local coder model (e.g. `qwen2.5-coder:7b`/`14b`, `lmstudio/*`) | **All secret/auth/key code**, offline work, fallback on 429/timeout | 150 lines | 900 lines / 36k chars | 3,000 chars |
| **`-m free`** / **`glm-free`** | `opencode/deepseek-v4.1-flash-free`, `glm-5.3-flash-free` (`cost: 0`) | Primary non-sensitive TS/React services, hooks, unit tests; small/medium Swift (`Codable` DTOs, helpers, `XCTest`) (40–150 lines) | 200 lines | 1,200 lines / 48k chars | 4,000 chars |
| **`-m pickle`** | `opencode/big-pickle` (`cost: 0`) | Complex non-sensitive TS/Swift modules, ViewModels, isolated SwiftUI subviews, edge-case logic (80–180 lines), default `review` pre-pass | 200 lines | 1,200 lines / 48k chars | 4,000 chars |
| **`-m openrouter`** / **`nim`** | `openrouter/openrouter/free`, `nvidia/nvidia/nemotron-3-ultra-550b-a55b`, `groq/*`, `github/*` | Free-tier OpenAI-compatible cloud alternatives & fallback on 429 (`--timeout 180` for large NIM models) | 200 lines | 1,200 lines / 48k chars | 4,000 chars |
| **Cloud (Primary)** | Your primary paid model (Claude / Gemini / GPT / Codex / etc.) | Architecture, decomposition, `types/*.ts` & `Models/*.swift`/`Protocols/*.swift`, wiring, micro-edits (1–10 lines), sensitive Swift, newest Apple SDK APIs, final verify | — | — | — |

## Security & Volume Rules for Free Cloud (`opencode/*`, `openrouter/*`, `nvidia/*`, `groq/*`, `github/*`)
- **Volume:** Ideal for **40–200 line** non-sensitive TS modules/tests (`--append` for larger files, max 1–2 concurrent `opencode/*` calls to avoid 429; fallback chain: `free`/`glm-free` → `pickle` → `openrouter`/`nim` → `-m coder`).
- **Zero-Secrets Policy (`exit 3`):** NEVER send `.env*`, `*.pem`/`*.key`, API keys, crypto/exchange keys, seed phrases, passwords, or auth tokens to external free cloud models — keep all secret-adjacent code on local Ollama/LM Studio (`-m fast` / `-m coder`) or your Primary Cloud model. `savings-bridge` auto-loads keys from `~/.config/savings-bridge/secrets.env`.

## Mandatory Workflow
1. **Decompose & keep types modular:** Split features >100 lines into small domain `types/*.ts` (≤200 lines each) → helpers (`-m fast`) → service slices (`-m coder` + `--append`) → tests (`-m coder`/`-m fast`).
2. **Batch by model tier:** Run `-m fast` tasks in one batch, then `-m coder` tasks in the next batch to avoid model reload delays (`keep_alive: 15m`).
3. **Pass targeted `-c` context:** Include only relevant domain `.ts` types/reference files within the model's limit.
4. **Prefer `--append`:** Never overwrite large existing files; append new functions/tests with `--append`.
5. **TDD-First & Auto-Repair:** For pure logic/validators, write edge-case unit tests first, then pass `-c <tests> --check --verify "<test_cmd>"` (`--check` auto-runs `tsc --noEmit` for TS, `swift build` / `swiftc -parse` for Swift, `php -l` for PHP, `py_compile` for Python). On failure, `savings-bridge` auto-retries (`--retries 1`).
6. **Swift Rule:** Never route Swift code to small local models (`7b`/`14b`). Use `-m free`/`glm-free` for 40–150 line DTOs/helpers/tests, `-m pickle` for 80–180 line ViewModels/SwiftUI subviews/services, and your **Primary Cloud model** for architecture, newest Apple SDK APIs, and any auth/Keychain/payment code.

## Commands

```bash
# 1. Small TS helper / validator (fast local model)
savings-bridge code -m fast -f path/to/helper.ts -c path/to/types.ts --check -p "Implement <single pure function>"

# 2. Larger TS module / service with context (coder local model)
savings-bridge code -m coder -f path/to/service.ts -c path/to/types.ts -c path/to/example.ts --check -p "Implement <focused slice>"

# 3. Append to existing file safely
savings-bridge code -m coder -f path/to/service.ts --append -c path/to/types.ts --check -p "Add <method>"

# 4. Write unit tests
savings-bridge code -m coder -o path/to/file.test.ts -c path/to/file.ts -p "Write deterministic unit tests"

# 4b. Swift DTO / ViewModel / SwiftUI subview (Free Cloud + --check)
savings-bridge code -m free -f Sources/Models/UserDTO.swift -c Sources/Protocols/CodableTypes.swift --check -p "Implement Codable DTO with CodingKeys"
savings-bridge code -m pickle -f Sources/Views/ProfileCardView.swift -c Sources/Models/UserDTO.swift --check --verify "swift test" -p "Implement SwiftUI card view"

# 5. Git commit (uses fast/parser by default) & changelog
savings-bridge git-commit --apply
savings-bridge git-changelog --range HEAD~10..HEAD [-o CHANGELOG.md]

# 6. Parse unstructured text to JSON (uses fast/parser by default)
savings-bridge parse-json -i posts.txt -s schema.json -o result.json

# 7. Pre-review non-sensitive diff (uses pickle by default; sensitive diffs -> Primary Cloud model)
savings-bridge review -m pickle

# 8. Text prompts, drafting, summarization, translation (saves cloud chat tokens)
savings-bridge ask -m free -p "Summarize this spec in 5 bullets" -c docs/spec.md
savings-bridge ask -m fast -p "Draft a polite reply" -s "Be concise"
```
