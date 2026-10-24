import threading
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_FORMS = Path(__file__).resolve().parents[1] / "fixtures" / "forms"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def form_server() -> Iterator[str]:
    """Serve tests/fixtures/forms on 127.0.0.1 (ephemeral port); yields the base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0),
                                 partial(_QuietHandler, directory=str(_FORMS)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
