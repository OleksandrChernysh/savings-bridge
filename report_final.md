# Savings Bridge: final review

- **Date:** 2026-11-19
- **Commit reviewed:** `f1db291` on `main`
- **Sources:** two independent static reviews run from the same prompt (`review/review_prompt.md`):
  - [`review/report_agent_a.md`](report_agent_a.md): 26 findings
  - [`review/report_agent_b.md`](report_agent_b.md): 20 findings

  Every disagreement between them was rechecked against the code. That meant reading the cited lines, plus one standalone Windows snippet (§3).
- **Method:** static only. No project code was run, imported or installed. Snippets copy a regex or reproduce stdlib behaviour; none of them import the project.

**Confidence labels used below:**

- **Both**: found independently by both agents.
- **Rechecked**: one agent found it, or they disagreed, and I confirmed it in the code.
- **Likely / Speculative**: as the agents defined them.

---

## 1. What the project is

Savings Bridge is a stdlib-only Python CLI (`savings-bridge`) plus an agent skill (`SKILL.md`). A paid coding agent (Claude Code, Codex, Gemini/Antigravity) reads the skill and hands routine work to the CLI:

- **Kinds of work:** small code modules, tests, summaries, translations, commit messages, JSON extraction, review pre-passes.
- **Where it goes:** cheaper models. These are free cloud tiers (OpenCode via the `opencode` CLI, OpenRouter, NVIDIA NIM, Groq, GitHub Models) or local runners (Ollama, LM Studio).

The CLI wraps each call in guards:

- a secret scanner in front of free cloud
- per-model scope limits
- a truncation/placeholder guard
- a compile check with one auto-repair pass and rollback

It logs every call's tokens and reports a "Saved %" figure.

**Intended flow:**

1. The agent runs `savings-bridge models --json`.
2. It calls `code|ask|review|git-commit|parse-json -m <alias>`.
3. The CLI resolves the alias, runs the secret and scope checks, and calls the backend (with fallback on 429).
4. It writes the file, then checks, repairs, and rolls back on failure.
5. It logs the usage, and `stats` compares that log with the agent's transcripts.

## 2. Verdict

**It partly does what it says.** Both reviewers reached the same verdict independently.

It works on a machine shaped like the author's:

- macOS or Linux
- Ollama running at install time, with exactly `qwen2.5:7b` and `qwen2.5-coder:14b` pulled
- 20 to 36 GB of RAM
- Antigravity as the primary agent

Off that path, the three promises the README sells as safety and value do not hold:

1. **"Private data stays local / is blocked from free cloud."**
   - The secret guard is a regex deny-list that misses most current key formats and every added line of a diff (C1).
   - The aliases documented as local can silently point to a cloud model (C2).
2. **"Broken writes are rolled back."**
   - Several crash paths skip rollback (C3).
   - The auto-repair step can silently replace a whole file with a partial one and report success (C4).
3. **"Tracks how much you saved."**
   - The spent side is read only from Antigravity transcripts, so Claude Code and Codex users see 100% (H3).
   - Every generated token counts as "saved", including output that was rejected or rolled back (M5).

Beyond those three promises:

- **Defaults are hard-wired.** The "dynamic" model selection is bypassed by every default invocation (H1). Three commands can only reach Ollama (H2).
- **Windows support is claimed but broken in several places.** Process launch (H4), text encoding (H5) and the installer's PATH advice (M6) all fail there.

The premise, that offloading saves net paid tokens, is plausible but unmeasured. Nothing the project records could show a net saving (§7).

## 3. How the two reviews compared

The two reviews converged strongly. All of the top findings were found by both agents, with matching line numbers and matching failure scenarios:

- the secret guard
- local aliases going to cloud
- rollback skipped on repair failure
- hard-coded defaults
- Ollama-only commands
- the Antigravity-only meter
- Windows `.cmd` shims

Where they differed, it was mostly severity or breadth. Each difference was resolved against the code:

| # | Topic | Agent A | Agent B | Recheck result | Final |
|---|---|---|---|---|---|
| 1 | Local aliases resolve to cloud | Critical (C2) | High (H1) | Confirmed `model_discovery.py:133-134`, `bridge_backends.py:106-108`, `local_bridge.py:472`. It meets the prompt's Critical definition: secret-adjacent code leaves the machine against a documented promise. SKILL.md routes "**All secret/auth/key code**" to `-m coder`, and the guard's own block message (`local_bridge.py:106`) recommends these aliases. | **Critical (C2)** |
| 2 | Repair writes are unguarded | Medium (M6) | Critical (C2) | Confirmed `local_bridge.py:189-208`. The repair asks for "the complete corrected raw code for the file", even in `--append` mode, and writes it with no truncation or placeholder check. `py_compile` and `php -l` only check syntax, so a truncated file passes and the run reports success. This is data loss, so Critical. | **Critical (C4)** |
| 3 | `-o` target skips the overwrite guards | (not found) | part of C2 | Confirmed: `existing` is read only from `-f` (`:68-71`), while the write goes to `args.out or args.file` (`:128`). Downgraded, because with `-o` the model never sees the old file. It is a missing clobber protection, not a truncation. It still escapes the documented "never overwrite large files" limit. | **Medium (M2)** |
| 4 | `--check` type-checks the whole TS project | High (H4) | (not found) | Confirmed `bridge_guards.py:212-217`: `npx tsc --noEmit` runs at the `tsconfig.json` root, and any error anywhere triggers repair and then rollback. A's specific scenario is weaker than stated, because SKILL's order (types, then helpers, then services) is bottom-up. The general case holds: any pre-existing error anywhere in the project fails every check. | **Medium (M1), Likely** |
| 5 | Python 3.10+ required | High (H6) | Medium (M3) | Confirmed: `setup.py:26` imports `model_discovery` first, and `model_discovery.py:60` evaluates `str \| None` at definition time. `resolve_model` also imports it lazily (`bridge_backends.py:104`), so a hand-copied install fails on its first call too. It blocks the README's one-command install on the author's own primary platform. | **High (H6)** |
| 6 | `secrets.env` overwritten on re-run | Medium (M1) | High (H5) | Confirmed `setup.py:181-182`. Nuance neither agent stated: if no key is entered, the function returns early (`:173-175`) and the file is untouched. Loss needs at least one key typed. It is silent, it breaks the explicit ".bak backups" promise, and re-running setup is the only update path. | **High (H7)** |
| 7 | Windows text encoding | Medium (M8, stats emoji only) | Medium (M1, adds Cyrillic and `git diff`) | A snippet (below) confirmed B's wider scope and found more: non-ASCII output that doesn't crash is silently decoded as mojibake. In `code` mode that mojibake is written into the generated source. Raised one level. | **High (H5)** |
| 8 | Passwords containing symbols pass | (not found) | in C1 | Confirmed by reading: the value class `[A-Za-z0-9_./+=-]{8,}` (`bridge_guards.py:51`) needs 8 such characters straight after `=`, so `P@ssw0rd!2026` never matches. | **in C1** |
| 9 | Scope limits keyed on name substrings | Low (L6) | (not found) | Confirmed `bridge_guards.py:262-271`: `qwen2.5-coder:7b` contains `:7b`, so it gets the 60-line limit. SKILL.md documents 150 for the `coder` tier. | **Low (L5)** |
| 10 | OpenAI-compatible `content: null` crash | in C3 | (not found) | Confirmed: `.get("content", "")` returns `None` when the key is present but null (`bridge_backends.py:352`), and then `.strip()` raises (`:361`). | **in C3** |
| 11 | Ollama `-cloud` models picked as "local" | Speculative (M2) | (not found) | It depends on what `/api/tags` reports for these models. Size-0 models always "fit" (`model_discovery.py:112`), so it is plausible. | **Open question** |
| 12 | Ollama lock fails for a second user | (not found) | Low (L4) | Confirmed: `open(LOCK_PATH, "w")` in the shared temp directory (`bridge_backends.py:52`, `:137`). | **Low (L8)** |
| 13 | Fallback keeps the cloud scope limits | (not found) | Low (L4) | Confirmed: the limits are computed for the cloud `model` (`local_bridge.py:111`), then `build_fallback_chain` appends the local coder (`bridge_backends.py:376-382`). | **Low (L8)** |
| 14 | `opencode` prompt passed in argv | Medium (M9) | Low (L4) | On Windows a long prompt raises `OSError` (A verified by snippet). In the repair path that is a C3 trigger, so Medium. | **Medium (M9)** |
| 15 | TS auto-import gaps | Low (L5) | Medium (M6) | `_TS_IMPORT_RE` has no DOTALL (`bridge_guards.py:128`), so it misses Prettier-style multi-line imports, which are the norm. This is a headline README feature. | **Medium (M8)** |
| 16 | `git-commit` with nothing staged | Low (L7) | Medium (M7) | Confirmed `local_bridge.py:233-235`. `--apply` then fails harmlessly. The risk is an agent reusing a wrong message. | **Low (L6)** |
| 17 | `opencode run` is a tool-using agent | open question | Speculative (M8) | Cannot settle statically. It is kept because of its impact. | **Medium (M10), Speculative** |

**Windows encoding recheck.** The snippet is at `C:\Users\vladn\AppData\Local\Temp\claude\4dbf164a6ced2e38\77108aee-6093-4861-bbd0-7dc95903b5d2\scratchpad\final\win_decode3.py`. It mimics the project's own `subprocess.run([...], capture_output=True, text=True)` against a child that writes UTF-8 bytes, as `git diff` and `opencode` do. Results on Python 3.10.9, 3.12.10 and 3.14.0 (Windows 11, code page cp1252):

- **Text containing `с` (UTF-8 `D1 81`):**
  - the reader thread dies with `UnicodeDecodeError: ... byte 0x81`
  - `proc.stdout` is `None`
  - the caller's `.strip()` or `.splitlines()` raises `AttributeError`
  - Nearly any Ukrainian or Russian sentence contains `с`. Bytes 0x8D, 0x8F, 0x90 and 0x9D do the same.
- **Other non-ASCII text:** decodes without error but wrongly. `Переклад` becomes `ÐŸÐµÑ€ÐµÐºÐ»Ð°Ð´`, and `—` becomes `â€”`.
- **Printing Cyrillic to a piped stdout:** raises `UnicodeEncodeError`. My first test run crashed this way on its own label.

**Process notes.**

- **Report files.** Both agents' attempts to write their report files were refused by the harness ("Subagents should return findings as text"). I saved their returned text to `review/report_agent_*.md` without edits, apart from dropping the five-line chat summary above each report.
- **Agent A's grep.** One repo-wide grep accidentally matched five lines of the old `report.md` (Python version, Windows encoding). Agent B found both topics independently, so this does not affect any conclusion.

---

## 4. Claims vs reality

| # | Claim (source) | Verdict | Findings |
|---|---|---|---|
| 1 | Routine tasks are "automatically" routed to free or local models (README:7) | Partly. The agent routes by following SKILL.md, and the CLI only picks among backends once called | §7, H1, H2 |
| 2 | Passwords, keys and `.env` are blocked from external free services (README:17, :41; SKILL:29) | Does not hold for common formats, diffs or Windows paths | C1 |
| 3 | Private data is processed only locally or by the primary (README:17); `-m fast`/`-m coder` are local and offline (README:98; SKILL:21, :29) | Does not hold when no local model was seen at setup time | C2 |
| 4 | Broken writes are rolled back (README:16, :40) | Does not hold on crash paths. Repair can silently truncate | C3, C4 |
| 5 | Repairs happen "locally with type context" (README:16) | Partly. The repair uses the same model, which may be cloud | C4, L1 |
| 6 | `--check` runs by default (README:16; SKILL:36) | On by default. It crashes on Windows for TS and when a checker is missing, and it checks the whole TS project | C3, H4, M1 |
| 7 | `code` auto-resolves imports into context (README:16, :40) | Partly. Only single-line TS imports and relative Python imports | M8 |
| 8 | The model is "chosen automatically for your machine" (README:101; SKILL:12) | Only with an explicit `-m fast`/`-m coder`. All defaults are hard-coded | H1 |
| 9 | `git-commit`/`parse-json` default to fast/parser and accept `-m free` (SKILL:58, :62; AGENTS:7) | Does not hold. Ollama only, with hard-coded `qwen2.5:7b` | H1, H2 |
| 10 | Fallback chain free → pickle → openrouter/nim → coder (SKILL:28) | Partly. Real chain is openrouter (if key) → nim (if key) → local. Never pickle; three commands have none | H2, L1 |
| 11 | Hard per-tier scope limits, exit 2 (SKILL:16-24) | Partly. Keyed on name substrings; `ask` has none; repair and `-o` bypass them | C4, M2, L5 |
| 12 | Meter counts spent vs saved from agent transcripts (README:42) | Antigravity only, and it counts every generated token as saved | H3, M5 |
| 13 | `--period day/week/month` = today / 7 / 30 days (README:51-58) | Does not hold. It is the last N days that have data | M4 |
| 14 | Setup makes `.bak` backups before updating files (README:38; AGENTS:22) | Holds for scripts and skills, not for `secrets.env` | H7 |
| 15 | One-command install on macOS, Linux, Windows (README:74-79; AGENTS:21) | Partly. Needs Python 3.10+ (undocumented); PATH advice is wrong on Windows and missing on macOS | H6, M6 |
| 16 | `setup.py --dry-run` writes nothing (README:82) | Holds | |
| 17 | Metrics server on 127.0.0.1:4040 (README:63-70) | Holds, with `Access-Control-Allow-Origin: *` | L3 |
| 18 | Stdlib only (AGENTS:21) | Holds | |

**Docs vs docs:**

- README_UA:26 drops README's "on macOS/Linux" qualifier for `chmod 600`.
- README:15 says micro-edits are ≤15 lines; SKILL:25 says 1-10.
- AGENTS.md:6 cites `.agents/rules/local-llm.md` (absent) and a Gemini skill path that setup does not use.

---

## 5. Findings

Each finding cites its sources from the agent reports. The full evidence and snippet output are in those reports.

### Critical

#### C1. The secret guard misses common secret formats, every added diff line, and Windows paths

**Both** (A C1, B C1; both verified by snippet).

**Where:**

- patterns: `src/bridge_guards.py:41-53`
- check function: `:245-259`
- callers: `src/local_bridge.py:101-109` (code), `:392-396` (review), `:438-445` (ask)
- cloud defaults: `ask` is `-m free` (`:576`), `review` is `-m pickle` (`:566`)

**What is wrong:**

- **Literal keys.** The pattern needs alphanumerics straight after `sk-`, so `sk-proj-`, `sk-ant-api03-` and `sk-or-v1-` never match. OpenRouter, which uses `sk-or-v1-`, is the project's own fallback provider. `gsk_`, `nvapi-`, `github_pat_` and `sk_live_` are absent from the pattern.
- **Assignment rule** (`:51`). It has four gaps:
  - It is anchored with `^\s*`, so diff `+` lines, `const API_KEY =`, `self.password =` and JSON `"password":` all pass.
  - The keyword must end the variable name, so `SECRET_KEY` and `AWS_SECRET_ACCESS_KEY` pass.
  - The value class excludes `@ ! # $ :`, so `DB_PASSWORD=P@ssw0rd!2026` passes.
  - URL credentials and `Bearer` headers are not covered.
- **Filenames.** `(^|/)\.env` only knows `/`, so `config\.env` and `C:\app\.env.local` pass. The project's own `secrets.env` does not match either.
- **`review`.** It checks filenames only for explicit `-f` paths, never the files a diff touches.
- **Repair prompt.** It carries `--verify` and `tsc` output and goes to the same cloud model without a re-scan.

**Scenario:**

1. A developer adds `const DB_PASSWORD = "Pr0d!Pass#2026";` to a config file.
2. The agent runs `savings-bridge review`, which SKILL.md calls the default pre-pass.
3. The diff goes to `opencode/big-pickle` unblocked.

This is exactly what README:17 says cannot happen.

**Fix:**

- Normalise input first: strip `[+\- ]` from diff lines, and turn `\` into `/` while also matching on the basename.
- Add the modern key prefixes.
- Make the assignment rule unanchored, match the keyword anywhere in the identifier (`\w*(SECRET|PASSWORD|API_?KEY|TOKEN)\w*`), and accept any non-space value.
- Add URL-credential, `Bearer` and high-entropy rules.
- Scan `git diff --name-only` for `review`, and re-scan repair prompts.
- Turn the agents' snippet strings into regression tests.

**Structural fix:** a deny-list will always miss something. Make free cloud opt-in per call (`--allow-cloud`), or default `review` to a local model.

#### C2. "Local" aliases silently resolve to a free cloud model, and the cache is never refreshed

**Both** (A C2 Critical, B H1 High). Severity resolved in §3, row 1.

**Where:**

- `src/model_discovery.py:133-134`
- `setup.py:198`, `:205-213`
- `src/bridge_backends.py:103-109`, `:125-128`
- `src/local_bridge.py:472-476`
- `src/bridge_common.py:78` (1.5 s probe timeout)

**What is wrong:**

- With no local model detected, `coder` and `fast` (and so `text` and `parser`) fall back to `opencode/deepseek-v4.1-flash-free`.
- Setup saves this to `models.json`, even right after printing "Install Ollama ... ollama pull ...".
- `_get_dynamic_profile()` returns the cache forever. `models --json`, which SKILL.md tells agents to run first, does not refresh it. `--refresh` appears in no doc.

**Scenario:**

1. The user follows the README order: run `setup.py`, then install Ollama, which the README calls "optional, the installer tells you what to download". A cold Ollama slower than 1.5 s produces the same result.
2. From then on, `ask -m fast` ("locally ... offline", README:98) goes to OpenCode's cloud.
3. So does every `-m coder` call, which SKILL.md reserves for "all secret/auth/key code".
4. The only barrier left is C1's regex.
5. Nothing tells the user: stderr even says "Auto-repairing locally with opencode/..." (`local_bridge.py:186`).

**Fix:**

- Never map a local-tier alias to a cloud prefix. With no local model, fail with "no local model: `ollama pull X`, then `savings-bridge models --refresh`".
- Re-probe when the cache points at a cloud model or is older than a day.
- Print the backend host on every call.

#### C3. Several crash paths after the write skip rollback, so the original file is lost

**Both** (A C3, B C3). More triggers were added from the recheck.

**Where:**

- `src/local_bridge.py:160-176` (the original is kept only in memory, and the file is overwritten before verification)
- `:198` (repair call)
- `:210-226` (rollback block)
- `src/bridge_backends.py:469-470` (`sys.exit(1)`)
- `src/bridge_guards.py:190` (no `try`)

**What is wrong.** Rollback only runs if control reaches line 210. Any of these exits first and leaves the broken output in place:

- **Repair backend fails.** Every fallback candidate fails, for example a 429 with no keys, or a missing model. Then `query_backend` calls `sys.exit(1)`.
  - On a 16 GB machine, `-m 7b` repairs with the hard-coded `qwen2.5-coder:14b` (`:184`), which the installer never recommended there. Ollama returns 404, and the run exits.
- **Checker binary missing.** `npx`, `php`, `swiftc` or `swift` absent means `FileNotFoundError`. On Windows this is always the case for `npx` (H4).
- **Windows decoding** (found in the recheck). The opencode output contains `с` or a similar byte, so `proc.stdout` is `None` and `.splitlines()` raises `AttributeError` (`bridge_backends.py:261`).
- **OpenAI-compatible `content: null`.** Raises `AttributeError` (`bridge_backends.py:352`, `:361`).
- **Long prompt on Windows.** The argv limit raises `OSError` (M9).
- **Ctrl-C** during the 300 s wait.

**Scenario:**

1. `code -m free -f utils.py -p "add parse_date"` produces output that fails `py_compile`.
2. The repair hits a 429 with no OpenRouter or NIM key and no local model.
3. The process exits 1.
4. `utils.py` holds broken code, any uncommitted edits are gone, and no `.failed.tmp` is written.

**Fix:**

- Write the generated code to a temp file and verify it there. Replace the target only on success.
- Failing that, snapshot the original to disk and wrap the write/verify/repair steps in `try/except BaseException` that restores it.
- Call backends with `raise_on_error=True` inside the repair loop.
- Treat a missing checker as "check unavailable".
- Add tests that mock a failure mid-repair.

#### C4. Auto-repair rewrites the whole file with no guard, even in `--append` mode, and a truncated result that compiles is accepted

**Both, at different severities** (B C2 Critical, A M6 Medium). Resolved in §3, row 2.

**Where:**

- `src/local_bridge.py:189-208`
- `src/bridge_guards.py:148-161` (the guard that is skipped)
- `src/bridge_guards.py:226-227` (syntax-only checks)

**What is wrong:**

- The first write is protected by `detect_truncation_or_placeholders`, but only for non-append overwrites of files over 40 lines (`:147`).
- The repair sends the entire current file and asks for "the complete corrected raw code for the file". It writes the answer back with no placeholder check, no shrink check and no scope check.
- In `--append` mode, the entire file gets rewritten. SKILL.md prescribes `--append` exactly for files too large to overwrite.
- `py_compile` and `php -l` only check syntax, so a file reduced to the fixed function plus `# ... existing code` passes. The run then reports success.

**Scenario:**

1. A 400-line Python module.
2. `code -m coder -f svc.py --append -p "add retry helper"` appends a snippet that fails `py_compile`.
3. The repair prompt is the whole file plus up to 16k characters of context plus the errors. With `num_ctx=16384`, the model has little room left to echo the file back.
4. It returns only the fixed helper.
5. `py_compile` passes and `rc == 0`. The module silently lost about 390 lines.

Separately, empty model output becomes `"\n"` (`bridge_guards.py:145`). For targets of 40 lines or fewer it passes every guard, and an empty `.py` file compiles.

**Fix:**

- In `--append` mode, repair only the snippet (send the snippet plus the errors, then re-append).
- Run `detect_truncation_or_placeholders(orig_target, repaired)` before every repair write.
- Refuse empty or whitespace-only output.
- Treat `finish_reason == "length"` as a failure.

### High

#### H1. Default models are hard-coded, and the dynamic-default branch is dead code

**Both** (A H1, B H2). It has been dead since the first commit, per B.

**Where:**

- `src/local_bridge.py:519`, `:537`, `:545`, `:556` set `default=DEFAULT_CODER_MODEL` or `DEFAULT_PARSER_MODEL`.
- `src/bridge_backends.py:118-123` is the only reader of the profile defaults. It runs only when `name` is empty, which the CLI never allows.

**Effect.** The installer's own RAM tiers (`model_discovery.py:96-104`) say which models to pull. Here is what the no-`-m` defaults do with them:

| RAM | Installer recommends | `code` default works? | `git-commit` / `git-changelog` / `parse-json` default works? |
|---|---|---|---|
| under 10 GB | `qwen2.5:3b`, `qwen2.5-coder:3b` | no | no |
| 10 to 20 GB | `qwen2.5:7b`, `qwen2.5-coder:7b` | no | yes |
| 20 to 36 GB | `qwen2.5:7b`, `qwen2.5-coder:14b` | yes | yes |
| 36 GB and up | `qwen2.5:14b`, `qwen2.5-coder:32b` | no | no |

On the failing tiers the user sees the misleading message "Error connecting to Ollama". `HTTPError` is caught as `URLError` (`bridge_backends.py:199`), so Ollama's "model not found" body is never shown.

**Fix:**

- Use `default=None` on every `--model`.
- Catch `HTTPError` separately and show its body.

#### H2. `git-commit`, `git-changelog` and `parse-json` can only reach Ollama

**Both** (A H2, B H2).

**Where:** `src/local_bridge.py:257`, `:295`, `:337` call `query_ollama` directly.

**Effect:**

- `-m free`, `-m pickle` (which AGENTS.md:7 tells agents to use), LM Studio-only setups, and the no-local-model case all post to the Ollama port and fail.
- There is no fallback chain.

**Fix:** route these commands through `query_backend`, and add `detect_secrets` on the diff or text **in the same change**. Today, the Ollama-only path is the accidental reason staged secrets cannot leave through `git-commit`.

#### H3. "Saved %" reads only Antigravity transcripts, so Claude Code and Codex users see 100%

**Both** (A H3, B H3).

**Where:**

- `src/token_stats.py:17` (`TRANSCRIPT_GLOB` = `~/.gemini/antigravity/brain/*/...`)
- `:150` (`saved_pct = lout / (gout + lout)`)
- `src/metrics_server.py` (same logic)

**Effect.** README:5 names Claude first among the supported agents. A Claude Code user makes one bridge call and sees "Saved Out % 100.0", however much paid usage they spent.

**Fix:**

- Read `~/.claude/projects/**/*.jsonl` (`message.usage`) and Codex session logs.
- Until then, show "n/a" when there is no primary-side data.

#### H4. Windows: list-form `subprocess` cannot launch `.cmd` shims (`npx`, npm-installed `opencode`)

**Both** (A H5, B H4; both verified by snippet).

**Where:** `src/bridge_guards.py:217` (`["npx", "tsc", "--noEmit"]`), `src/bridge_backends.py:237-245` (`["opencode", ...]`).

**Effect:**

- Every TypeScript `--check` on Windows raises `FileNotFoundError` after the write, with no rollback (C3).
- With an npm-installed `opencode`, the default `ask`/`review` backend reports "not found". That last part is Likely and depends on the install method.

**Fix:**

- Resolve every executable with `shutil.which()` and pass the full path.
- Catch `OSError` around every external tool call.
- Add a Windows CI job; the tests only mock `platform.system()`.

#### H5. Windows text encoding: crashes on Cyrillic and emoji, and silent mojibake in generated code

**Both, widened by the recheck** (A M8, B M1, snippet in §3).

**Where:**

- No `encoding=` on any `subprocess.run(..., text=True)`: `src/local_bridge.py:230-238`, `:373-380`; `src/bridge_backends.py:237-243`; `src/bridge_guards.py:190`, `:230`
- No stdout reconfigure anywhere
- Emoji at `src/token_stats.py:182`, `:185`

**Effect on Windows, Python 3.10 to 3.14, with output piped (as agents always run it):**

- `stats` prints its table, then dies on the closing emoji with exit 1. The agent sees a failed command.
- `review` or `git-commit` on a diff containing `с` (nearly any Ukrainian text) crash with `AttributeError`.
- `ask` returning Ukrainian text through opencode crashes the same way. When the answer arrives intact, printing it raises `UnicodeEncodeError`.
- **Silent corruption (new in the recheck).** Non-ASCII text that does not crash is decoded as cp1252. In `code` mode an em dash or curly quote in a generated comment or string is written to the file as `â€”` or `â€™`, and `--check` passes.

README_UA exists, so Ukrainian-speaking users are an intended audience. That is why this is High rather than Medium.

**Fix:**

- Pass `encoding="utf-8", errors="replace"` to every `subprocess.run(..., text=True)`.
- Call `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` (and the same for stderr) at CLI start.
- Drop the emoji.

#### H6. Python 3.10+ is required but stated nowhere, so setup crashes on stock macOS `python3`

**Both** (A H6 High, B M3 Medium). Resolved in §3, row 5.

**Where:**

- `src/model_discovery.py:60`, `:84`, `:107`, `:125`, `:170`
- `setup.py:121-122`
- `src/cli_meter.py:88`
- `src/token_stats.py:21`

All of them use PEP 604 `X | None` in signatures without `from __future__ import annotations`. Only the three `bridge_*` modules have that import.

**Effect.** On Python 3.9, `setup.py` dies at import (`:26`), and the bridge dies on its first `resolve_model` call. The Xcode Command Line Tools ship 3.9 as `/usr/bin/python3` (Likely; neither agent had a 3.9 interpreter).

**Fix:**

- Add `from __future__ import annotations` to the four modules.
- Add a `sys.version_info` check in setup.
- State the minimum Python version in both READMEs.

#### H7. Re-running setup with any key entered replaces `secrets.env` and drops the stored keys, with no backup

**Both** (A M1 Medium, B H5 High). Resolved in §3, row 6.

**Where:** `setup.py:181-182` (`write_text` of only this run's entries); `:173-175` (the early return when nothing is entered).

**Effect.**

1. A user stored OpenRouter and NVIDIA keys earlier.
2. They re-run setup to update the scripts (the only update path) and add a Groq key.
3. The two earlier keys are gone, and the fallback chain silently loses its cloud steps.

This contradicts README:38 and AGENTS.md:22. Related: the file is created under the default umask and only then set to `chmod 600` (`:183-184`).

**Fix:**

- Parse the existing file and update only the keys entered.
- Copy it to `secrets.env.bak` first.
- Create it with `os.open(..., 0o600)`.

### Medium

#### M1. `--check` type-checks the whole TypeScript project

**Rechecked, Likely** (A H4).

**Where:** `src/bridge_guards.py:212-217`.

**What happens.** `npx tsc --noEmit` runs at the `tsconfig.json` root. Any pre-existing error anywhere in the project then:

- fails the check
- sends up to 4000 characters of unrelated errors, which C1 never scans, to the model
- rolls back correct output

On large projects every call also pays for a full type-check.

**Fix:**

- Take a baseline of diagnostics before the write and fail only on new ones that mention the target.
- Use `npx --no-install`.

#### M2. A `-o` target that already exists is overwritten with no guard

**Rechecked** (B, part of C2).

**Where:** `src/local_bridge.py:68-71` (guards read `-f`), `:128` (the write goes to `-o or -f`).

**What happens.** SKILL's example `code -o tests/x.test.ts -c src/x.ts` against an existing test file replaces it. No overwrite-size limit or shrink check applies, and a passing `tsc` means no rollback. With both `-f a.ts -o b.ts`, the shrink guard compares the output against the wrong file.

**Fix:**

- Read `existing` from the actual target.
- Refuse to replace an existing `-o` file without `--force`.

#### M3. The model picker can choose an embedding model as `fast`

**Both** (A M2, B M4).

**Where:** `src/model_discovery.py:107-122`.

**What happens:**

- `fast`, `text` and `parser` all become the smallest non-coder model. With `nomic-embed-text` installed, that is the embedding model, which cannot chat.
- Size-0 models always "fit" in RAM.
- `deepseek` in a name counts as a coder.

**Fix:**

- Exclude embedding families (`embed`, `bert`).
- Do not treat an unknown size as fitting.
- Prefer a mid-size instruct model over the absolute smallest.

#### M4. `--period day/week/month` means the last N days that have data, not calendar windows

**Both** (A M3, B M2).

**Where:** `src/token_stats.py:107-109`; `src/metrics_server.py:33-35`. `load_cloud_stats(days_limit)` ignores its argument.

**What happens.** If the last call was three weeks ago, `--period day` shows that day's numbers. Days are also UTC dates.

**Fix:** filter by `today - (N-1) days` in local time.

#### M5. Every backend call counts as "saved", including refused, rolled-back and repair output

**Both** (A M4, B §6).

**Where:**

- Logging happens inside `query_*` (`src/bridge_backends.py:215`, `:278`, `:354`), before any guard, check or rollback.
- `cli_meter` adds chars/4 estimates for wrapped CLIs.

**Fix:**

- Log an `outcome` per call (accepted, refused, rolled_back, repaired, fallback) and count only accepted output.
- Keep `cli_meter` estimates in their own category.

#### M6. Install PATH handling is harmful on Windows and missing on macOS

**Both** (A M5, B M3).

**Where:** `setup.py:103-105`, `:83-94`; `.githooks/pre-push:5`.

**What happens:**

- **Windows `setx`.** `setx PATH "%PATH%;..."` folds the machine PATH into the user PATH and truncates the result at 1024 characters. In PowerShell, `%PATH%` is not expanded at all.
- **macOS PATH.** `~/.local/bin` is not on macOS's default PATH, and setup does not warn about it.
- **Windows `python3`** (B checked this machine). `python3` is the Microsoft Store stub, so the README's `python3 setup.py` fails here, and so does the pre-push hook.

**Fix:**

- On Windows, use PowerShell's user-scope `SetEnvironmentVariable` and document `py setup.py`.
- On macOS and Linux, warn when `~/.local/bin` is missing from PATH.

#### M7. Every stored key is exported to every child process, and unused paid keys are collected

**Both** (A M7, B M5).

**Where:** `src/bridge_backends.py:55-83` (`os.environ[key] = val` at import); `src/bridge_guards.py:230` (`--verify` with `shell=True`); `setup.py:162-171`.

**What happens:**

- Code generated by a free cloud model runs under `--verify` with `OPENROUTER_API_KEY`, `GITHUB_TOKEN` and the rest in its environment.
- Setup asks for Gemini, Anthropic and OpenAI keys that no backend uses.

**Fix:**

- Keep keys in a dict that only the HTTP client reads.
- Pass a scrubbed `env=` to children.
- Stop prompting for unused providers.

#### M8. Auto-import misses multi-line imports, re-exports and `.js` specifiers

**Both** (A L5, B M6; both verified by snippet).

**Where:** `src/bridge_guards.py:128`, `:316-325`.

**What happens.** Prettier-formatted multi-symbol imports, `export ... from`, side-effect imports, and `./types.js` pointing at a `.ts` file are all silently skipped. That is the "ping-pong" README:16 says this feature prevents.

**Fix:**

- Use DOTALL with a bounded `[^;]*?`.
- Match `export ... from`.
- Map `.js` specifiers to `.ts`.

#### M9. The opencode prompt travels as one argv element

**Both** (A M9, B L4).

**Where:** `src/bridge_backends.py:237-238`.

**What happens:**

- On Windows, about 32k characters raises `OSError` (A verified this at 40k). The code-scope limit allows 48k.
- Linux caps one argument at 128 KiB.
- The prompt is visible in the process list.
- In the repair path, the `OSError` is a C3 trigger.

**Fix:**

- Use stdin or a temp file if `opencode run` supports it.
- Otherwise cap the length per OS and treat `OSError` as retryable.

#### M10. `opencode run` is an agent with tools, fed untrusted file and diff content

**Speculative** (B M8; an open question in A).

**Where:** `src/bridge_backends.py:237-243`. No permission flags; `cwd` is the temp directory, which does not confine a shell tool.

**Risk.** A prompt injection in a context file or diff could make the free model run bash or edit files on the user's machine, if opencode allows tools by default in `run` mode.

**Settle it.** Read opencode's `permission` defaults, then test a harmless canary in a VM. If tools are allowed, deny `bash`, `edit` and `webfetch`.

### Low

- **L1. Documentation drift** (both).
  - AGENTS.md:6 has two wrong paths.
  - README says micro-edits are ≤15 lines; SKILL says 1-10.
  - SKILL's fallback chain is not the real one.
  - "Auto-repairing locally" is printed for cloud models.
  - `--retries` help says "escalates 7b->14b", which only happens for the literal `qwen2.5:7b`.
  - Google AI Studio, Anthropic and OpenAI are listed as providers with no backend.
  - `.js`/`.jsx` get no check at all (`bridge_guards.py:77`).
  - `cli_meter` config still lives under `~/.gemini/antigravity/`.
- **L2. Undocumented installer and test side effects** (both).
  - Setup sets `core.hooksPath` (`setup.py:109-116`), and the hook runs any `agy-check-savings` it finds on PATH.
  - `tests/test_extended.py:81` runs `git config` on the real repo.
  - `~/.gemini/...` is always created (`setup.py:44`).
  - `~/.local/bin/{local-bridge,savings-stats,savings-bridge}` are deleted without checking their targets (`:86-92`).
- **L3. `Access-Control-Allow-Origin: *`** on the metrics server (`metrics_server.py:136`). Any open web page can read the usage history.
- **L4. `LM_STUDIO_HOST` in `secrets.env` is ignored for requests.** The URL is built at import time (`bridge_backends.py:49`), before `load_secrets_env()` runs (`:83`).
- **L5. Scope limits are keyed on name substrings** (A only, rechecked). `qwen2.5-coder:7b` gets the 60-line limit, not the documented 150 for `coder` (`bridge_guards.py:262-271`).
- **L6. `git-commit` with nothing staged describes the last commit plus unstaged edits** (`git diff HEAD~1`, `local_bridge.py:234`). Fix: fail with "stage changes first".
- **L7. Writes do not preserve bytes** (both). Text-mode I/O converts LF to CRLF on Windows on every append, and rollback is not byte-exact. Fix: `newline=""`, and append with mode `"a"`.
- **L8. Small robustness items:**
  - `finish_reason` is ignored, with `max_tokens: 4096` (`bridge_backends.py:321`).
  - `ask` has no scope check.
  - Cloud-to-local fallback keeps the cloud's 48k-character scope limits.
  - The Ollama lock does nothing on Windows and raises `PermissionError` for a second POSIX user (`:52`, `:137`).
  - `cli_meter.resolve_real_binary` ignores `PATHEXT`.

---

## 6. Finding map (final ↔ agent IDs)

| Final | Agent A | Agent B | A sev | B sev | Final sev |
|---|---|---|---|---|---|
| C1 | C1 | C1 | Critical | Critical | Critical |
| C2 | C2 | H1 | Critical | High | **Critical** |
| C3 | C3 (+H5, M9 triggers) | C3 | Critical | Critical | Critical |
| C4 | M6 | C2 (repair part) | Medium | Critical | **Critical** |
| H1 | H1 | H2 (defaults part) | High | High | High |
| H2 | H2 | H2 (routing part) | High | High | High |
| H3 | H3 | H3 | High | High | High |
| H4 | H5 | H4 | High | High | High |
| H5 | M8 | M1 | Medium | Medium | **High** (widened in recheck) |
| H6 | H6 | M3 (Python part) | High | Medium | **High** |
| H7 | M1 | H5 | Medium | High | **High** |
| M1 | H4 | — | High | — | **Medium** |
| M2 | — | C2 (`-o` part) | — | Critical | **Medium** |
| M3 | M2 | M4 | Medium | Medium | Medium |
| M4 | M3 | M2 | Medium | Medium | Medium |
| M5 | M4 | §6 | Medium | (premise) | Medium |
| M6 | M5 | M3 (PATH part) | Medium | Medium | Medium |
| M7 | M7 | M5 | Medium | Medium | Medium |
| M8 | L5 | M6 | Low | Medium | **Medium** |
| M9 | M9 | L4 (part) | Medium | Low | Medium |
| M10 | §9 question | M8 | — | Medium (Spec.) | Medium (Spec.) |
| L1 | L2 | L1 | Low | Low | Low |
| L2 | L1 | L3 | Low | Low | Low |
| L3 | L3 | L4 (part) | Low | Low | Low |
| L4 | L4 | L4 (part) | Low | Low | Low |
| L5 | L6 | — | Low | — | Low |
| L6 | L7 | M7 | Low | Medium | **Low** |
| L7 | L8 | L2 | Low | Low | Low |
| L8 | parts of L4, L8 | parts of L4 | Low | Low | Low |

**Totals:** 4 Critical, 7 High, 10 Medium, 8 Low.

---

## 7. The premise

Both reviews agree on this, and I agree with them.

**The direction is sound.** Moving output-heavy, low-judgement work (boilerplate, tests, summaries, commit messages) off a paid model can cut paid output tokens, which are the expensive ones.

**The number the project reports is not savings.** "Saved Out %" is `local_output / (primary_output + local_output)`. That is the share of output produced by free models. It assumes every free token replaced exactly one paid token and that every one was accepted. It leaves out:

- **Orchestration cost on the primary:** reading SKILL.md and `models --json`, writing prompts of up to 4000 characters, reading the result back (which then stays in context on later turns), and splitting work into slices.
- **Rework:** the primary redoing a task after a refusal or rollback. C3, C4, H1, H4 and M1 make this common.
- **Failed and thinking tokens logged as saved:** `eval_count` includes `<think>` tokens that `strip_markdown_code` throws away.
- **Cache reads:** these dominate input in an agent loop. They are loaded (`cache_read_tokens`) and then ignored.
- **The spent side** for two of the three named agents (H3).

**What would settle it:**

1. **Per-call outcome logging** (M5). Acceptance rate per tier is the first number that matters.
2. **A paired benchmark.** Take 20 to 30 fixed, realistic tasks. Run each with the skill installed and without it, on the same primary model. Compare the primary's own billed usage (input, output, cache read and write), the pass rate (tests or human acceptance), rework turns and wall time. The net saving is the difference in primary usage at an equal pass rate.

Until then, label the figure "share of output produced by free models".

**Secondary risks:**

- The defaults depend on promotional free-tier model IDs (`deepseek-v4.1-flash-free`, `glm-5.3-flash-free`, `big-pickle`, `openrouter/free`, `nemotron-3-ultra-550b-a55b`) that change often.
- Free tiers commonly log or train on prompts, which the README's privacy line does not mention.

## 8. What works well

Both reviewers agree on all of these:

- **Easy to audit.** The codebase is small, stdlib-only and clearly split into common, guards, backends, discovery and stats modules.
- **The secret check is in the right place**, even though its patterns are weak:
  - it runs on the *resolved* model, before any network call
  - auto-imported files are filtered by sensitive filename
  - a local primary never falls back to the cloud (`build_fallback_chain` returns `[]`)
- **Refused output is kept.** The first-pass truncation and placeholder guard saves it to `.local.tmp`, and the normal rollback keeps `.failed.tmp`.
- **The fallback chain is classified.** 429, 5xx, timeouts and a missing CLI are treated as retryable, and a test covers it.
- **Setup is careful in several places:**
  - `setup.py --dry-run` really writes nothing.
  - Scripts and skills are backed up before being replaced.
  - The legacy-log migration de-duplicates and is tested.
  - Keys are read with `getpass`.
  - The secrets loader never overrides existing environment variables.
- **The metrics server binds to `127.0.0.1`.**
- **The provider table is candid** about free-tier limits.
- **Tests exist and run on pre-push.** They miss the risky paths: write/repair/rollback, realistic secret formats, default model resolution and period filtering.

## 9. Prioritized recommendations

The order is by value per unit of effort.

1. **C2: stop mapping local aliases to cloud.**
   - Never resolve `fast`, `coder`, `text` or `parser` to a cloud prefix.
   - Re-probe a stale cache or one that points at the cloud.
   - Print the backend host on every call.

   This is a few lines of code, and it closes the largest silent leak.
2. **C3, C4, M2: make writes transactional.**
   - Write to a temp file, verify it, then replace the target.
   - Restore in `try/except BaseException`.
   - Guard every repair output, and repair only the snippet in `--append` mode.
   - Refuse empty output.
   - Read `existing` from the real target.
   - Add tests that mock a backend failure mid-repair and a missing checker.

   These are the only paths that destroy user work.
3. **C1: harden the secret guard and make cloud opt-in.**
   - Apply the pattern fixes listed under C1.
   - Scan diff filenames and repair prompts.
   - Default `review` to a local model, or require `--allow-cloud`.
   - Add the agents' snippet strings as regression tests.
4. **H1, H2: make the defaults real.**
   - Use `default=None` on every `--model`.
   - Route all commands through `query_backend`, adding the secret check in the same change.
   - Take the repair model from the profile.
   - Show Ollama's error body.
5. **H4, H5, M6, L7: Windows.**
   - Use `shutil.which` for executables.
   - Use UTF-8 for subprocess output and for stdout and stderr.
   - Drop the emoji.
   - Fix the PATH advice and document `py`.
   - Use `newline=""`.
   - Add a Windows CI job.
6. **H6, H7: install basics.**
   - Add `__future__` annotations and a stated minimum Python version.
   - Merge `secrets.env` with a backup, and create it as 0600.
7. **H3, M4, M5, then §7: meter honesty.**
   - Read Claude Code and Codex logs, or show "n/a".
   - Use calendar windows.
   - Log outcomes.
   - Run the paired benchmark before quoting any percentage.
8. **The rest:**
   - M1: run a diagnostic baseline before `tsc`.
   - M3: filter out embedding models.
   - M7: scrub the environment passed to child processes.
   - M8: fix the import regex.
   - M9: move the prompt off argv.
   - M10: confirm opencode's permission defaults.
   - The Low items.

## 10. Open questions (static reading cannot settle these)

| Question | Step that settles it |
|---|---|
| Does `opencode run --format json` emit `{"type":"text","part":{"text":...}}` and `step_finish` with `part.tokens.input/output`? If not, every OpenCode call returns `""` and logs 0 tokens. | `opencode run -m opencode/big-pickle --format json "say hi"` once; inspect the lines. |
| Do the hard-coded free model IDs exist today? | `opencode models`; `GET https://openrouter.ai/api/v1/models`; the NVIDIA build catalog. |
| Does `opencode run` allow its bash, edit and webfetch tools by default (M10)? | Read the opencode `permission` docs; test a canary instruction in a VM. |
| Does opencode pick up `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` from the environment for side calls (M7)? | One `ask` with a deliberately invalid key exported; look for a 401 in opencode's logs. |
| How do Ollama `*-cloud` models appear in `/api/tags` (size, family)? If their size is 0, the picker treats them as local and the secret guard is skipped. | `curl http://127.0.0.1:11434/api/tags` with a cloud model pulled. |
| Ollama's status codes for a missing model and for `/api/chat` on an embedding model. | Two `curl` calls against a local Ollama. |
| What does Ollama do when prompt plus answer exceed `num_ctx` (C4 likelihood)? | A 14k-token prompt with `num_ctx=16384` asking for a 9k-token echo. |
| Antigravity transcript path, field names and timezone. | Inspect one real `transcript.jsonl`. |
| Stock macOS `/usr/bin/python3` version (H6). | `python3 --version` on a clean Mac with only the Command Line Tools. |
| Does `opencode` on Windows install as `.exe` or as a `.cmd` shim (H4)? | `where opencode` after each official install method. |
| Do the free tiers log or train on prompts? | The current data-use terms for OpenCode Zen, OpenRouter (per endpoint) and NVIDIA build. |
| Is the test suite green on Windows? | `py -3.12 -m unittest discover -s tests` in a throwaway copy (`test_extended.py:81` touches git config). |
