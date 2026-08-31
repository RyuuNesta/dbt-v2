"""
HTTP layer: stdlib ThreadingHTTPServer.

No web framework. dbt-core already pulls in a large dependency tree and this UI
adds nothing to it, which means the data team can run it on a locked-down
machine with no pip install and no Node toolchain.

Threading matters because a dbt build or a BigQuery profile takes seconds to
minutes; a single-threaded server would block the log polling that makes the run
console feel live.
"""

from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import socket
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from . import api, config

MAX_BODY_BYTES = 4 * 1024 * 1024  # generous for SQL, far below a DoS

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")


class Handler(BaseHTTPRequestHandler):
    server_version = "ASGdbtStudio/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        """Quiet by default; per-request noise makes the console unreadable."""
        if os.environ.get("DBT_UI_VERBOSE"):
            sys.stderr.write(
                f"[{self.log_date_time_string()}] {fmt % args}\n"
            )

    def _send(self, status: int, body: bytes, content_type: str,
              extra_headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Local tool: never let a stale asset or query result be cached.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, default=str, allow_nan=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _read_body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError(
                f"Request body of {length} bytes exceeds the "
                f"{MAX_BODY_BYTES} byte limit."
            )
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Request body is not valid JSON: {exc}") from exc
        return parsed if isinstance(parsed, dict) else {"value": parsed}

    def _guard_origin(self) -> bool:
        """
        Reject cross-origin state-changing requests.

        The server binds to loopback, but a page in another tab could still POST
        to it. Requiring the Origin header (when present) to be one of our own
        addresses stops that without needing tokens or cookies.
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True
        allowed = {
            f"http://127.0.0.1:{config.SETTINGS.port}",
            f"http://localhost:{config.SETTINGS.port}",
            f"http://{config.SETTINGS.host}:{config.SETTINGS.port}",
        }
        return origin in allowed

    # ------------------------------------------------------------------
    # verbs
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        self._handle("GET")

    def do_HEAD(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        if not self._guard_origin():
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "Cross-origin requests are not accepted."},
            )
            return
        self._handle("POST")

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        try:
            if path.startswith("/api/"):
                body = self._read_body() if method == "POST" else {}
                status, payload = api.handle(method, path, query, body)
                self._send_json(status, payload)
                return

            if path == "/dbt-docs" or path.startswith("/dbt-docs/"):
                self._serve_dbt_docs()
                return

            self._serve_static(path)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except BrokenPipeError:
            pass  # browser navigated away mid-response
        except ConnectionResetError:
            pass
        except Exception as exc:  # pragma: no cover
            import traceback
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": f"Server error: {exc}",
                    "traceback": traceback.format_exc(limit=6),
                },
            )

    # ------------------------------------------------------------------
    # static assets
    # ------------------------------------------------------------------

    def _resolve_static(self, path: str) -> Optional[Any]:
        """Map a URL path to a file under frontend/, refusing traversal."""
        clean = posixpath.normpath(path)
        if clean in ("/", ".", "/."):
            clean = "/index.html"

        root = config.FRONTEND_DIR.resolve()
        candidate = (root / clean.lstrip("/")).resolve()

        if candidate != root and root not in candidate.parents:
            return None
        if candidate.is_dir():
            candidate = candidate / "index.html"
        return candidate if candidate.is_file() else None

    def _serve_static(self, path: str) -> None:
        target = self._resolve_static(path)

        if target is None:
            # Unknown path that is not an asset request: hand back the SPA shell
            # so client-side routing works on a hard refresh.
            if "." not in posixpath.basename(path):
                target = self._resolve_static("/index.html")
            if target is None:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": f"Not found: {path}"},
                )
                return

        content_type, _ = mimetypes.guess_type(str(target))
        if content_type is None:
            content_type = "application/octet-stream"
        if content_type.startswith("text/") or content_type in (
            "application/javascript", "application/json", "image/svg+xml"
        ):
            content_type = f"{content_type}; charset=utf-8"

        self._send(HTTPStatus.OK, target.read_bytes(), content_type)

    def _serve_dbt_docs(self) -> None:
        """Serve the static dbt docs site if it has been generated."""
        static_index = config.TARGET_DIR / "static_index.html"
        if not static_index.is_file():
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {
                    "error": "dbt docs have not been generated yet.",
                    "hint": "Run 'Generate docs' from the Run console first.",
                },
            )
            return
        self._send(
            HTTPStatus.OK,
            static_index.read_bytes(),
            "text/html; charset=utf-8",
        )


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------

class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
            return True
        except OSError:
            return False


def find_port(host: str, preferred: int, attempts: int = 20) -> int:
    for offset in range(attempts):
        candidate = preferred + offset
        if _port_available(host, candidate):
            return candidate
    raise RuntimeError(
        f"No free port found in {preferred}-{preferred + attempts - 1}."
    )


def serve(host: Optional[str] = None, port: Optional[int] = None,
          open_browser: bool = True) -> None:
    settings = config.SETTINGS
    host = host or settings.host
    port = find_port(host, port or settings.port)
    settings.port = port  # keep the origin guard in sync

    config.ensure_runtime_dir()

    if not config.DBT_PROJECT_PATH.exists():
        raise SystemExit(
            f"No dbt_project.yml at {config.PROJECT_DIR}.\n"
            f"dbt Studio expects to live at <dbt project>/dbt_ui."
        )
    if not config.FRONTEND_DIR.is_dir():
        raise SystemExit(f"Frontend assets are missing: {config.FRONTEND_DIR}")

    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}"

    banner = f"""
  ASG dbt Studio
  {'-' * 58}
  project    {config.project_name()}  ({config.PROJECT_DIR})
  profile    {config.profile_name()}  ->  target '{config.default_target_name()}'
  manifest   {'found' if config.MANIFEST_PATH.exists() else 'missing - click Refresh manifest'}
  url        {url}
  {'-' * 58}
  Bound to {host} only. Nothing is exposed to the network.
  Press Ctrl+C to stop.
"""
    print(banner, flush=True)

    if host == "0.0.0.0":
        print(
            "  WARNING: host 0.0.0.0 makes this reachable from the network and\n"
            "  the UI has no authentication. It can query and write to\n"
            "  BigQuery with your credentials. Use 127.0.0.1 unless you have\n"
            "  put an authenticating proxy in front of it.\n",
            flush=True,
        )

    httpd = Server((host, port), Handler)

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down.", flush=True)
    finally:
        httpd.shutdown()
        httpd.server_close()
