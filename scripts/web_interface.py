#!/usr/bin/env python3
import json
import os
import subprocess
import threading
import time
import math
import re
from pathlib import Path
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("ESCAPE_WEB_HOST", "0.0.0.0")
PORT = int(os.environ.get("ESCAPE_WEB_PORT", "8000"))
SERVICE_NAME = "escape-sound.service"
TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "index.html"
HTML = TEMPLATE_PATH.read_text(encoding="utf-8")

VOLUME_GAMMA = 0.35
MIXER_CONTROL = "Digital"


def clamp_percent(value: int) -> int:
    return max(0, min(100, int(value)))


def slider_to_mixer_percent(slider_percent: int) -> int:
    """
    Convert user-facing slider percent (perceptual scale) to hardware mixer percent.
    Lower gamma makes low slider values more audible.
    """
    slider_percent = clamp_percent(slider_percent)
    if slider_percent == 0:
        return 0

    normalized = slider_percent / 100.0
    mixer = round((normalized ** VOLUME_GAMMA) * 100.0)
    return clamp_percent(mixer)


def mixer_to_slider_percent(mixer_percent: int) -> int:
    """
    Convert hardware mixer percent back to user-facing slider percent.
    Inverse of slider_to_mixer_percent().
    """
    mixer_percent = clamp_percent(mixer_percent)
    if mixer_percent == 0:
        return 0

    normalized = mixer_percent / 100.0
    slider = round((normalized ** (1.0 / VOLUME_GAMMA)) * 100.0)
    return clamp_percent(slider)


def get_mixer_percent() -> int:
    try:
        result = subprocess.run(
            ["amixer", "get", MIXER_CONTROL],
            capture_output=True,
            text=True,
            check=False,
        )
        out = result.stdout or ""
        matches = re.findall(r"\[(\d+)%\]", out)
        if matches:
            return clamp_percent(int(matches[-1]))
    except Exception:
        pass
    return 0


def get_volume() -> int:
    mixer_percent = get_mixer_percent()
    return mixer_to_slider_percent(mixer_percent)


def set_volume(slider_percent: int) -> int:
    slider_percent = clamp_percent(slider_percent)
    mixer_percent = slider_to_mixer_percent(slider_percent)

    subprocess.run(
        ["amixer", "set", MIXER_CONTROL, f"{mixer_percent}%"],
        check=False,
    )
    return slider_percent

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

        if self.path == "/api/volume":
            self._json(HTTPStatus.OK, {"volume": get_volume()})
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
        if self.path == "/api/volume":
            try:
                raw_len = self.headers.get("Content-Length", "0")
                length = int(raw_len)
                body = self.rfile.read(length) if length > 0 else b"{}"
                payload = json.loads(body.decode("utf-8"))
                vol = int(payload.get("volume"))
            except Exception:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid volume"})
                return

            vol = set_volume(vol)
            self._json(HTTPStatus.OK, {"volume": vol})
            return

        if self.path == "/api/volume/bg":
            try:
                raw_len = self.headers.get("Content-Length", "0")
                length = int(raw_len)
                body = self.rfile.read(length) if length > 0 else b"{}"
                payload = json.loads(body.decode("utf-8"))
                vol = float(payload.get("volume"))
            except Exception:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid bg volume"})
                return

            subprocess.run([
                "mosquitto_pub",
                "-h", "localhost",
                "-t", "escape/audio/volume/bg",
                "-m", str(vol)
            ], check=False)

            self._json(HTTPStatus.OK, {"bg_volume": vol})
            return

        if self.path == "/api/volume/hint":
            try:
                raw_len = self.headers.get("Content-Length", "0")
                length = int(raw_len)
                body = self.rfile.read(length) if length > 0 else b"{}"
                payload = json.loads(body.decode("utf-8"))
                vol = float(payload.get("volume"))
            except Exception:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid hint volume"})
                return

            subprocess.run([
                "mosquitto_pub",
                "-h", "localhost",
                "-t", "escape/audio/volume/hint",
                "-m", str(vol)
            ], check=False)

            self._json(HTTPStatus.OK, {"hint_volume": vol})
            return

        if self.path == "/api/duck":
            try:
                raw_len = self.headers.get("Content-Length", "0")
                length = int(raw_len)
                body = self.rfile.read(length) if length > 0 else b"{}"
                payload = json.loads(body.decode("utf-8"))
                duck = float(payload.get("duck"))
            except Exception:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid duck value"})
                return
            

            subprocess.run([
                "mosquitto_pub",
                "-h", "localhost",
                "-t", "escape/audio/duck",
                "-m", str(duck)
            ], check=False)

            self._json(HTTPStatus.OK, {"duck": duck})
            return


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
