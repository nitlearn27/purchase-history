"""
Flask web service wrapping the Flipkart scraper.
Render (and any cloud platform) needs an HTTP port — this provides it.

Endpoints:
  GET  /health   → liveness check
  POST /scrape   → start a scrape (runs in background thread)
  GET  /results  → return the latest scrape output
"""

import asyncio
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Startup: hydrate ephemeral files from environment variables.
# Render's filesystem resets on every deploy/restart, so credentials that
# were obtained locally are stored as env vars and written back here.
# ---------------------------------------------------------------------------

def _hydrate_file(env_key: str, file_path: Path) -> None:
    """Write env_key's value to file_path if the value is set and file is absent."""
    value = os.getenv(env_key, "").strip()
    if value and not file_path.exists():
        try:
            file_path.write_text(value)
            print(f"[init] {file_path.name} restored from {env_key}.")
        except Exception as exc:
            print(f"[init] WARNING: could not write {file_path.name}: {exc}")


_hydrate_file("GMAIL_TOKEN_JSON", Path("token.json"))
_hydrate_file("FLIPKART_AUTH_STATE", Path("auth_state.json"))

# ---------------------------------------------------------------------------
# Scrape state
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_state = {
    "running": False,
    "last_result": None,          # dict from orders_report.json
    "last_run_at": None,          # ISO timestamp
    "error": None,
}


def _run_scrape(num_orders: int) -> None:
    """Blocking function executed in a background thread."""
    global _state
    try:
        from scrape_flipkart_orders import run
        asyncio.run(run(num_orders=num_orders, headless=True))

        report_path = Path("orders_report.json")
        if report_path.exists():
            _state["last_result"] = json.loads(report_path.read_text())
            _state["error"] = None

            # Log auth_state.json content so the user can update FLIPKART_AUTH_STATE
            auth_path = Path("auth_state.json")
            if auth_path.exists():
                print("\n[deploy] Copy the value below into the FLIPKART_AUTH_STATE "
                      "environment variable on Render to persist the Flipkart session:\n")
                print(auth_path.read_text())
                print()
        else:
            _state["error"] = "Scrape finished but orders_report.json was not created."

    except Exception as exc:
        _state["error"] = str(exc)
        print(f"[scrape] Error: {exc}")
    finally:
        _state["running"] = False
        _state["last_run_at"] = datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now(tz=timezone.utc).isoformat()})


@app.route("/scrape", methods=["POST"])
def scrape():
    with _lock:
        if _state["running"]:
            return jsonify({"error": "A scrape is already in progress."}), 409

        body = request.get_json(silent=True) or {}
        num_orders = int(body.get("orders", 10))

        _state["running"] = True
        _state["error"] = None

    thread = threading.Thread(target=_run_scrape, args=(num_orders,), daemon=True)
    thread.start()

    return jsonify({
        "status": "started",
        "orders_requested": num_orders,
        "message": "Scrape started. Poll GET /results to get output.",
    }), 202


@app.route("/results", methods=["GET"])
def results():
    if _state["running"]:
        return jsonify({"status": "running", "message": "Scrape in progress…"}), 202

    if _state["error"]:
        return jsonify({"status": "error", "error": _state["error"],
                        "last_run_at": _state["last_run_at"]}), 500

    if _state["last_result"] is None:
        return jsonify({"status": "idle",
                        "message": "No scrape run yet. POST /scrape to start."}), 200

    return jsonify({"status": "ok", "last_run_at": _state["last_run_at"],
                    **_state["last_result"]}), 200


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    print(f"[server] Starting on port {port}")
    app.run(host="0.0.0.0", port=port)
