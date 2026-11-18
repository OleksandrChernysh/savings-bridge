# AGENTS.md — Savings Bridge Rules

Instructions for AI agents operating in `savings-bridge`.

## 1. Offloading (`local-llm` + `savings-bridge`)
- Follow `.agents/rules/local-llm.md` and use the global `savings-bridge` skill + `savings-bridge` CLI (`savings-bridge models --json` first) for local/free-cloud offloading and security (`secrets.env`, `exit 3`).

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
