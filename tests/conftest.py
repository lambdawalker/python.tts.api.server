import socket
import threading
import time
from contextlib import contextmanager

import pytest
import uvicorn


@pytest.fixture
def live_server():
    @contextmanager
    def run(app):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        try:
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    raise AssertionError("Test server failed to start")
                time.sleep(0.01)
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(5)
            sock.close()
            assert not thread.is_alive()

    return run
