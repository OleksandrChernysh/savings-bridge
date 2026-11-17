#!/usr/bin/env python3
"""
Lightweight HTTP Metrics & Health Server for Savings Bridge.
Exposes:
  - GET /metrics/summary?days=10  (JSON for SwiftBar / Waybar / Raycast / PowerShell widgets)
  - GET /health                   (Ollama / LM Studio status)
  - GET /                         (Minimal HTML dashboard)
"""

import argparse
import json
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from token_stats import load_gemini_stats, load_local_stats


def check_url(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except Exception:
        return False


def build_summary(days: int = 10) -> dict:
    gemini_stats = load_gemini_stats(days)
    local_stats = load_local_stats()

    all_days = sorted(set(list(gemini_stats.keys()) + list(local_stats.keys())))
    if days > 0 and len(all_days) > days:
        all_days = all_days[-days:]

    by_day = []
    by_command: dict[str, int] = {}
    tot_g_inp = tot_g_out = tot_l_inp = tot_l_out = tot_l_calls = 0
    chat_calls = chat_inp = chat_out = 0

    for day in all_days:
        g = gemini_stats[day]
        l = local_stats[day]
        ginp, gout = g["inp"], g["out"]
        linp, lout, lcalls = l["local_inp"], l["local_out"], l["calls"]

        tot_g_inp += ginp
        tot_g_out += gout
        tot_l_inp += linp
        tot_l_out += lout
        tot_l_calls += lcalls
        chat_calls += l["commands"].get("ask", 0)
        chat_inp += l["cmd_inp"].get("ask", 0)
        chat_out += l["cmd_out"].get("ask", 0)

        for cmd, cnt in l["commands"].items():
            by_command[cmd] = by_command.get(cmd, 0) + cnt

        denom = gout + lout
        by_day.append(
            {
                "date": day,
                "cloud_input_tokens": ginp,
                "cloud_output_tokens": gout,
                "local_calls": lcalls,
                "local_input_tokens": linp,
                "local_output_tokens": lout,
                "saved_output_pct": round((lout / denom * 100) if denom > 0 else 0.0, 1),
            }
        )

    tot_denom = tot_g_out + tot_l_out
    today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_entry = next((d for d in by_day if d["date"] == today_key), None)

    return {
        "days": days,
        "today": today_entry,
        "total": {
            "cloud_input_tokens": tot_g_inp,
            "cloud_output_tokens": tot_g_out,
            "local_calls": tot_l_calls,
            "local_input_tokens": tot_l_inp,
            "local_output_tokens": tot_l_out,
            "saved_output_pct": round((tot_l_out / tot_denom * 100) if tot_denom > 0 else 0.0, 1),
        },
        "chat_savings": {
            "calls": chat_calls,
            "input_tokens": chat_inp,
            "output_tokens": chat_out,
        },
        "by_command": by_command,
        "by_day": by_day,
    }


DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Savings Bridge Metrics</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 760px; margin: 2rem auto; padding: 0 1rem; background: #0f1117; color: #e6edf3; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 1rem; margin: 1.5rem 0; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 1rem; }
  .val { font-size: 1.5rem; font-weight: 700; color: #58a6ff; }
  table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
  th, td { text-align: right; padding: .5rem; border-bottom: 1px solid #21262d; }
  th:first-child, td:first-child { text-align: left; }
</style></head><body>
  <h2>Savings Bridge — Token Meter</h2>
  <div class="cards" id="cards">Loading...</div>
  <table><thead><tr><th>Date</th><th>Cloud Out</th><th>Local Calls</th><th>Local Out</th><th>Saved %</th></tr></thead><tbody id="rows"></tbody></table>
  <script>
    async function load() {
      const r = await fetch('/metrics/summary?days=10').then(x => x.json());
      const t = r.total;
      document.getElementById('cards').innerHTML = `
        <div class="card"><div>Saved Output</div><div class="val">${t.saved_output_pct}%</div></div>
        <div class="card"><div>Chat / Text Saved</div><div class="val">${r.chat_savings.output_tokens.toLocaleString()}</div></div>
        <div class="card"><div>Local Calls</div><div class="val">${t.local_calls.toLocaleString()}</div></div>
        <div class="card"><div>Local Output</div><div class="val">${t.local_output_tokens.toLocaleString()}</div></div>
        <div class="card"><div>Cloud Output</div><div class="val">${t.cloud_output_tokens.toLocaleString()}</div></div>`;
      document.getElementById('rows').innerHTML = r.by_day.slice().reverse().map(d =>
        `<tr><td>${d.date}</td><td>${d.cloud_output_tokens.toLocaleString()}</td><td>${d.local_calls}</td><td>${d.local_output_tokens.toLocaleString()}</td><td>${d.saved_output_pct}%</td></tr>`
      ).join('');
    }
    load(); setInterval(load, 15000);
  </script>
</body></html>"""


class MetricsHandler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/metrics/summary":
            qs = parse_qs(parsed.query)
            period = qs.get("period", [""])[0].lower()
            period_map = {"day": 1, "week": 7, "month": 30}
            if period in period_map:
                days = period_map[period]
            else:
                try:
                    days = int(qs.get("days", ["10"])[0])
                except ValueError:
                    days = 10
            self._send_json(200, build_summary(days))
            return
        if parsed.path == "/health":
            self._send_json(
                200,
                {
                    "status": "ok",
                    "ollama": check_url("http://127.0.0.1:11434/api/tags"),
                    "lm_studio": check_url("http://127.0.0.1:1234/v1/models"),
                },
            )
            return
        if parsed.path == "/":
            body = DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_json(404, {"error": "not_found"})

    def log_message(self, format: str, *args) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="HTTP Metrics Server for Savings Bridge")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=4040, help="Bind port (default: 4040)")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), MetricsHandler)
    print(f"Savings Bridge metrics listening on http://{args.host}:{args.port}")
    print(f"  JSON endpoint: http://{args.host}:{args.port}/metrics/summary")
    print(f"  Health check:  http://{args.host}:{args.port}/health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
