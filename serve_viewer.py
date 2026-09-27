#!/usr/bin/env python3
"""Rebuild $Photos.html and open it via a local HTTP server.

OpenStreetMap and some other tile hosts return 403 when $Photos.html is
opened directly from File Explorer (file://). Serving over localhost avoids that.
"""

from __future__ import annotations

import argparse
import socketserver
import sys
import webbrowser
from pathlib import Path

import create_viewer


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create $Photos.html and open it in a browser over localhost."
    )
    parser.add_argument("directory", type=Path, help="Preprocessed picture directory")
    parser.add_argument("--port", type=int, default=8765, help="Local port (default 8765)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.directory.expanduser()
    code = create_viewer.main([str(root)])
    if code != 0:
        return code

    Handler = create_viewer.make_request_handler(root)
    url = f"http://127.0.0.1:{args.port}/$Photos.html"
    try:
        httpd = socketserver.ThreadingTCPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        print(f"Could not start server on port {args.port}: {exc}", file=sys.stderr)
        return 2
    print(f"Serving {root}")
    print(url)
    print("Press Ctrl+C to stop.")
    webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
