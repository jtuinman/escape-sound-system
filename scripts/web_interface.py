#!/usr/bin/env python3
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("ESCAPE_WEB_HOST", "0.0.0.0")
PORT = int(os.environ.get("ESCAPE_WEB_PORT", "8080"))
SERVICE_NAME = "escape-sound.service"
TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "index.html"
HTML = TEMPLATE_PATH.read_text(encoding="utf-8")


def shutdown_host():
    # Give the HTTP response time to be sent before powering off.
    time.sleep(1.0)
    subprocess.run(["sync"], check=False)
    subprocess.run(["systemctl", "poweroff"], check=False)


def reboot_host():
    # Give the HTTP response time to be sent before rebooting.
    time.sleep(1.0)
    subprocess.run(["sync"], check=False)
    subprocess.run(["systemctl", "reboot"], check=False)


def get_service_status() -> dict:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", SERVICE_NAME],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:
        return {"service": SERVICE_NAME, "status": "error", "detail": str(exc)}

    status = (result.stdout or result.stderr or "").strip() or "unknown"
    return {"service": SERVICE_NAME, "status": status}


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/service-status":
            self._json(HTTPStatus.OK, get_service_status())
            return

        if self.path not in ("/", "/index.html"):
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
            return

        try:
            data = HTML.encode("utf-8")
        except Exception:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Failed to load template: {TEMPLATE_PATH}"})
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path not in ("/api/shutdown", "/api/reboot"):
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
            return

        try:
            raw_len = self.headers.get("Content-Length", "0")
            length = int(raw_len)
            body = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body"})
            return

        if payload.get("confirm") is not True:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Missing confirm=true"})
            return

        if self.path == "/api/shutdown":
            threading.Thread(target=shutdown_host, daemon=True).start()
            self._json(HTTPStatus.ACCEPTED, {"message": "Shutdown request accepted. Powering off..."})
            return

        threading.Thread(target=reboot_host, daemon=True).start()
        self._json(HTTPStatus.ACCEPTED, {"message": "Reboot request accepted. Rebooting..."})

    def log_message(self, fmt, *args):
        return


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[WEB] listening on http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
