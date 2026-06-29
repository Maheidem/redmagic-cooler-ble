"""Static web UI server."""

from __future__ import annotations

import functools
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files


def serve_web_ui(host: str = "127.0.0.1", port: int = 8766, open_browser: bool = False) -> str:
    """Serve the packaged Web Bluetooth UI and block until interrupted."""

    root = files("redmagic_cooler_ble").joinpath("web")
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(root))
    server = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{server.server_port}/ui.html"

    if open_browser:
        webbrowser.open(url)

    print(f"Serving RedMagic Cooler BLE UI at {url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()

    return url
