"""Print the running API's /health JSON; exit 1 if Ollama or models are unavailable."""

import json
import sys
import urllib.request


def main(url: str = "http://127.0.0.1:8765/health") -> int:
    with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 - localhost only
        data = json.load(r)
    print(json.dumps(data, indent=2))
    return 0 if data["ollama"] and not data["missing_models"] else 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:2]))
