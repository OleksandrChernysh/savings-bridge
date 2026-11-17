# Savings Bridge

[Українська версія](README_UA.md)

A simple assistant that **saves your paid limits (tokens) in Claude, Gemini, or ChatGPT/Codex**.

Instead of spending your expensive primary model on every small task (translations, short replies, text summaries, simple code, error checks), Savings Bridge automatically routes those tasks to **free cloud models** or **local models running on your computer** — and tracks how much you have saved.

---

## What Happens to Your Primary Model

- **It stays in place and works the same way.** Nothing is removed or disabled.
- **Complex work stays with your primary model.** Architecture, critical decisions, complex coding, and final verification are still handled by it.
- **Routine tasks go to the background assistant.** Everyday text chat, summaries, translations, drafts, simple code snippets, and Git commit messages run for free.
- **Your passwords and private data stay safe.** If a prompt or file contains passwords, keys, or `.env`, the system blocks sending it to external free services — such data is processed only locally on your machine or by your primary model.

---

## What Gets Installed on Your Computer

Running `python3 setup.py` installs only lightweight text scripts (no heavy apps or external Python packages):

1. **Assistant scripts** in `~/.gemini/bin/` (`local_bridge.py`, `token_stats.py`, `cli_meter.py`, `metrics_server.py`, `model_discovery.py`) and an agent instruction file in `~/.gemini/antigravity/skills/local-coder/`.
2. **Local key manager (`secrets.env`)** in `~/.config/savings-bridge/secrets.env` with access restricted to your user only (`chmod 600` on macOS/Linux). It stores API keys for any cloud providers you choose to connect (you can skip this step if you only use local models).
3. **Computer hardware profile** in `~/.config/savings-bridge/models.json` — the list of detected local models and settings automatically tuned to your RAM.
4. **Savings log** in `~/.gemini/antigravity/local_tokens.jsonl` — records every saved token.
5. **Optional (for offline work without internet):** if you do not have a local model runner yet (such as **Ollama** or **LM Studio**), the installer checks your RAM and tells you what to download. Ollama/LM Studio is never installed automatically — you install it only if you want to.

---

## What the System Does

| Module | What it does | Benefit | What it checks / reads | What it detects automatically |
|---|---|---|---|---|
| **Model Discovery** (`model_discovery.py`) | Checks your computer hardware and available local models | No need to guess technical model names or whether your laptop has enough memory | RAM size, CPU architecture, local Ollama (`:11434`) and LM Studio (`:1234`) ports | Operating system (macOS, Linux, Windows), best models for text vs code, or a pull suggestion if none are installed |
| **Setup Wizard** (`setup.py`) | Runs a quick 1–2 minute guided setup | Easy to set up on a new laptop or share with friends | `providers.json`, local ports, keys you enter | OS paths, key file permissions, and safe `.bak` backups before updating existing files |
| **Text Assistant** (`local_bridge.py ask`) | Answers text prompts, translates, summarizes, and writes drafts | Saves paid tokens on everyday chat and writing | Prompt text, attached context files, `models.json` profile | Which free or local model to use, and whether the text contains private keys |
| **Code Assistant** (`local_bridge.py code`, `review`, `git-commit`, `parse-json`) | Writes simple functions and tests, extracts JSON from text, drafts Git commits | Offloads routine work from your primary model | Target files, Git history, language config files | How to auto-check code for errors after writing (`tsc`, `swiftc`, `py_compile`, `php -l`) |
| **Secret Guard** (`Zero-Secrets Guard`) | Scans text and filenames before sending to external free services | Prevents accidental leaks of passwords and API keys | Filenames (`.env`, `.pem`, `.key`) and prompt contents | Tokens, keys, and passwords; blocks external free models if detected |
| **Savings Meter** (`token_stats.py`, `metrics_server.py`) | Counts spent vs saved tokens and serves stats to terminal, browser, or widgets | Clearly shows how many tokens you saved over the last day, week, or month | `local_tokens.jsonl` log and agent conversation transcripts | Savings % split between text/chat and code, plus live local runner status |

---

## How to Check Your Savings (Day, Week, Month)

Open your terminal and run:

```bash
# Savings for the last day (today)
python3 ~/.gemini/bin/token_stats.py --period day

# Savings for the last week (7 days)
python3 ~/.gemini/bin/token_stats.py --period week

# Savings for the last month (30 days)
python3 ~/.gemini/bin/token_stats.py --period month
```

### Web Dashboard & Widget Endpoint

Start the local metrics server:
```bash
python3 ~/.gemini/bin/metrics_server.py --port 4040
```
Available endpoints:
- `http://127.0.0.1:4040/` — simple browser dashboard with your savings stats
- `http://127.0.0.1:4040/metrics/summary?period=day` (or `week`, `month`) — JSON data for widgets (SwiftBar, Waybar, Raycast, PowerShell)
- `http://127.0.0.1:4040/health` — checks whether local model runners are active

---

## Installation (1 Command for macOS, Linux, Windows)

```bash
git clone https://github.com/OleksandrChernysh/savings-bridge.git savings-bridge && cd savings-bridge
python3 setup.py
```

Extra options:
- `python3 setup.py --dry-run` — preview what the installer will do without writing files
- `python3 setup.py --skip-keys` — skip cloud API key setup (local models only)

`local_bridge.py` automatically reads `~/.config/savings-bridge/secrets.env` on macOS, Linux, and Windows. If you also want those keys available to other shell tools on macOS/Linux:
```bash
source ~/.config/savings-bridge/secrets.env
```

---

## Usage Examples

```bash
# 1. Text prompt via a free cloud model
python3 ~/.gemini/bin/local_bridge.py ask -p "Summarize this email into 3 bullet points" -c letter.txt

# 2. Text prompt locally on your computer (offline)
python3 ~/.gemini/bin/local_bridge.py ask -m fast -p "Translate this text to Spanish"

# 3. Simple code task (model is chosen automatically for your machine)
python3 ~/.gemini/bin/local_bridge.py code -m coder -f helper.ts -c types.ts --check -p "Add email validator"

# 4. Check which models were auto-selected for your computer
python3 ~/.gemini/bin/local_bridge.py models
```

---

## Supported Providers & Login Links

The full list is stored in `providers.json`. During `setup.py`, the installer shows these links automatically.

| Provider | Where it runs | Free tier | How realistic is free usage & caveats | Login / API key link |
|---|---|---|---|---|
| **Ollama** | On your computer | Yes | **Realistic & unlimited (verified).** No sign-up, no card, no rate limits. Only requires enough local RAM. | https://ollama.com/download |
| **OpenRouter** | Cloud | Yes (`:free` models) | **Realistic (verified).** Free key works without a card, but `:free` models have per-minute/day rate limits (`429` possible). | https://openrouter.ai/keys |
| **NVIDIA NIM** | Cloud | Yes (starter credits) | **Realistic (verified).** Free developer key after NVIDIA account sign-up; speed and limits vary by model. | https://build.nvidia.com |
| **LM Studio** | On your computer | Yes | **Realistic.** Free GUI alternative to Ollama without cards or sign-up. Requires local RAM. | https://lmstudio.ai |
| **OpenCode Free** | Cloud | Yes | **With caveats.** Offers free models, but requires the separate `opencode` CLI and frequently hits `429` rate limits. | https://opencode.ai |
| **Google AI Studio** | Cloud | Yes | **Restricted.** Strict RPM/TPM limits; some regions/accounts require billing; free-tier prompts may be logged. | https://aistudio.google.com/apikey |
| **Groq** | Cloud | Yes | **Restricted.** Very tight tokens-per-minute cap — quickly hits `429` on code or long prompts. | https://console.groq.com/keys |
| **GitHub Models** | Cloud | Yes | **Restricted.** Low daily request cap and small context window — suitable only for small tests. | https://github.com/marketplace/models |
| **Anthropic (Claude)** | Cloud | No | **Paid only.** Requires a credit card and prepaid balance. | https://console.anthropic.com/settings/keys |
| **OpenAI** | Cloud | No | **Paid only.** Requires a credit card and prepaid balance. | https://platform.openai.com/api-keys |
