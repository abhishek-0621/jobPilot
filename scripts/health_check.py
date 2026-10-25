"""Print the running API's /health JSON; exit 1 if Ollama or models are unavailable,
2 (with a one-line message, no traceback) if the API cannot be reached."""

import json
import sys
import urllib.error
import urllib.request


def main(url: str = "http://127.0.0.1:8765/health") -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 - localhost only
            data = json.load(r)
    except (urllib.error.URLError, OSError, ValueError) as e:
        reason = getattr(e, "reason", e)
        print(f"Cannot reach JobPilot at {url} ({reason}). "
              "Is `uv run jobpilot start` running?", file=sys.stderr)
        return 2
    print(json.dumps(data, indent=2))
    return 0 if data.get("ollama") and not data.get("missing_models") else 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:2]))
