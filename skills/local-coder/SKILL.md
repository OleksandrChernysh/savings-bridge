---
name: local-coder
description: Offloads atomic code generation, tests, git commits, and JSON parsing to local Ollama models (7B for small functions/parsing, 14B for larger TS contexts) with hard scope limits.
---

# Local Coder & Parser Bridge (Ollama)

Use `~/.gemini/bin/local_bridge.py` to offload well-scoped, atomic tasks to local models instead of generating code in Cloud.
**NEVER call `ollama run` or `localhost:11434` directly** — all local calls MUST go through `local_bridge.py` so tokens are logged to `~/.gemini/antigravity/local_tokens.jsonl` (Antigravity Quota Bar).

## Dynamic Model Discovery (Run First on New Machines)
Run `python3 ~/.gemini/bin/local_bridge.py models --json` to inspect host RAM, installed local models, and dynamic aliases (`fast`, `coder`, `text`, `parser`). Prefer `-m coder` / `-m fast` (or explicit installed model tags) so selection adapts to the user's hardware.

## Model Routing & Hard Scope Limits

`local_bridge.py` enforces hard limits (`exit 2` if exceeded) so oversized tasks are always split first:

| Tier | Model | Use Cases | Max Overwrite | Max Context (`-f` + `-c`) | Max Prompt |
| :--- | :--- | :--- | :---: | :---: | :---: |
| **`-m 7b`** | `qwen2.5:7b` (~27 tok/s) | Pure helpers, validators (10–40 lines), `parse-json`, `git-commit`, small sensitive helpers | 60 lines | 300 lines / 12k chars | 1,500 chars |
| **`-m 14b`** (default) | `qwen2.5-coder:14b` (~13.6 tok/s) | **All secret/auth/key code**, offline work, fallback on 429/timeout | 150 lines | 900 lines / 36k chars | 3,000 chars |
| **`-m free`** / **`glm-free`** | `opencode/deepseek-v4.1-flash-free`, `glm-5.3-flash-free` (`cost: 0`) | Primary non-sensitive TS/React services, hooks, unit tests; small/medium Swift (`Codable` DTOs, helpers, `XCTest`) (40–150 lines) | 200 lines | 1,200 lines / 48k chars | 4,000 chars |
| **`-m pickle`** | `opencode/big-pickle` (`cost: 0`) | Complex non-sensitive TS/Swift modules, ViewModels, isolated SwiftUI subviews, edge-case logic (80–180 lines), default `review` pre-pass | 200 lines | 1,200 lines / 48k chars | 4,000 chars |
| **`-m nim`** | `nvidia/nvidia/nemotron-3-ultra-550b-a55b` (`NVIDIA_API_KEY`) | Backup on OpenCode 429 (slow 550B model; pass `--timeout 180`) | 200 lines | 1,200 lines / 48k chars | 4,000 chars |
| **Cloud** | Gemini (`Flash`/`Hydrogen`/`Argon`) | Architecture, decomposition, `types/*.ts` & `Models/*.swift`/`Protocols/*.swift`, wiring, micro-edits (1–10 lines), sensitive Swift, newest Apple SDK APIs, final verify | — | — | — |

## Security & Volume Rules for Free Cloud (`free`, `glm-free`, `pickle`, `nim`)
- **Volume:** Ideal for **40–200 line** non-sensitive TS modules/tests (`--append` for larger files, max 1–2 concurrent `opencode/*` calls to avoid 429; fallback chain: `free`/`glm-free` → `pickle` → `nim` → `-m 14b`).
- **Zero-Secrets Policy (`exit 3`):** NEVER send `.env*`, `*.pem`/`*.key`, API keys, crypto/exchange keys, seed phrases, passwords, or auth tokens to `opencode/*` or `nvidia/*` — keep all secret-adjacent code on local Ollama (`7b`/`14b`) or Cloud.

## Mandatory Workflow
1. **Decompose & keep types modular:** Split features >100 lines into small domain `types/*.ts` (≤200 lines each) → helpers (`7b`) → service slices (`14b` + `--append`) → tests (`14b`/`7b`).
2. **Batch by model tier:** Run `7b` tasks in one batch, then `14b` tasks in the next batch to avoid model reload delays (`keep_alive: 15m`).
3. **Pass targeted `-c` context:** Include only relevant domain `.ts` types/reference files within the model's limit.
4. **Prefer `--append`:** Never overwrite large existing files; append new functions/tests with `--append`.
5. **TDD-First & Auto-Repair:** For pure logic/validators, write edge-case unit tests first, then pass `-c <tests> --check --verify "<test_cmd>"` (`--check` auto-runs `tsc --noEmit` for TS, `swift build` / `swiftc -parse` for Swift, `php -l` for PHP, `py_compile` for Python). On failure, `local_bridge.py` auto-retries (`--retries 1`).
6. **Swift Rule:** Never route Swift code to `7b`/`14b`. Use `-m free`/`glm-free` for 40–150 line DTOs/helpers/tests, `-m pickle` for 80–180 line ViewModels/SwiftUI subviews/services, and **Cloud Gemini** for architecture, newest Apple SDK APIs, and any auth/Keychain/payment code.

## Commands

```bash
# 1. Small TS helper / validator (7B)
~/.gemini/bin/local_bridge.py code -m 7b -f path/to/helper.ts -c path/to/types.ts --tsc -p "Implement <single pure function>"

# 2. Larger TS module / service with context (14B)
~/.gemini/bin/local_bridge.py code -m 14b -f path/to/service.ts -c path/to/types.ts -c path/to/example.ts --tsc -p "Implement <focused slice>"

# 3. Append to existing file safely
~/.gemini/bin/local_bridge.py code -m 14b -f path/to/service.ts --append -c path/to/types.ts --tsc -p "Add <method>"

# 4. Write unit tests
~/.gemini/bin/local_bridge.py code -m 14b -o path/to/file.test.ts -c path/to/file.ts -p "Write deterministic unit tests"

# 4b. Swift DTO / ViewModel / SwiftUI subview (Free Cloud + --check)
~/.gemini/bin/local_bridge.py code -m free -f Sources/Models/UserDTO.swift -c Sources/Protocols/CodableTypes.swift --check -p "Implement Codable DTO with CodingKeys"
~/.gemini/bin/local_bridge.py code -m pickle -f Sources/Views/ProfileCardView.swift -c Sources/Models/UserDTO.swift --check --verify "swift test" -p "Implement SwiftUI card view"

# 5. Git commit (uses 7B by default) & changelog
~/.gemini/bin/local_bridge.py git-commit --apply
~/.gemini/bin/local_bridge.py git-changelog --range HEAD~10..HEAD [-o CHANGELOG.md]

# 6. Parse unstructured text to JSON (uses 7B by default)
~/.gemini/bin/local_bridge.py parse-json -i posts.txt -s schema.json -o result.json

# 7. Pre-review non-sensitive diff (uses pickle by default; sensitive diffs -> Cloud Gemini)
~/.gemini/bin/local_bridge.py review -m pickle

# 8. Text prompts, drafting, summarization, translation (saves cloud chat tokens)
~/.gemini/bin/local_bridge.py ask -m free -p "Summarize this spec in 5 bullets" -c docs/spec.md
~/.gemini/bin/local_bridge.py ask -m 7b -p "Draft a polite reply" -s "Be concise"
```
