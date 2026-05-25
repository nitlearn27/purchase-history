"""PostToolUse hook: re-run the unit test suite whenever a logic file changes.

Triggered after Write|Edit. Reads the tool-call JSON on stdin, filters to the
files that affect behavior (scraper, salesforce sync, web app, or the tests
themselves), and runs `python -m unittest test_units.py`. Hook output is JSON
so Claude is informed of pass/fail.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

TRACKED = (
    "scrape_flipkart_orders.py",
    "salesforce_sync.py",
    "app.py",
    "test_units.py",
)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        # No stdin / malformed payload — silently no-op so we don't disturb
        # the user's normal flow on non-hook invocations.
        return 0

    file_path = (
        (data.get("tool_input") or {}).get("file_path")
        or (data.get("tool_response") or {}).get("filePath")
        or ""
    )
    if not file_path:
        return 0

    normalized = file_path.replace("\\", "/")
    if not any(normalized.endswith("/" + t) or normalized.endswith(t) for t in TRACKED):
        return 0

    py = os.path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe")
    if not os.path.exists(py):
        py = sys.executable

    try:
        result = subprocess.run(
            [py, "-m", "unittest", "test_units.py"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        _emit({
            "systemMessage": "Unit tests timed out (>120s) after edit — investigate.",
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "test_units.py did not finish within 120s.",
            },
        })
        return 0
    except Exception as exc:
        _emit({"systemMessage": f"Test runner hook failed to launch: {exc}"})
        return 0

    edited = os.path.basename(file_path)
    combined = (result.stderr or "") + (result.stdout or "")
    test_count_match = re.search(r"Ran (\d+) test", combined)
    test_count = test_count_match.group(1) if test_count_match else "?"

    if result.returncode == 0:
        _emit({
            "systemMessage": f"test_units.py: {test_count} tests passed after editing {edited}",
        })
    else:
        # Surface the failure tail back to Claude so it can react.
        tail = combined[-1500:] if combined else "(no output)"
        _emit({
            "systemMessage": f"test_units.py FAILED after editing {edited}",
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": (
                    f"Unit tests failed after edit to {edited}. "
                    f"Fix before continuing.\n\n--- test_units.py output (tail) ---\n{tail}"
                ),
            },
        })
    return 0


if __name__ == "__main__":
    sys.exit(main())
