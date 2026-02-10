#!/usr/bin/env python3
import json
import os
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("ESCAPE_WEB_HOST", "0.0.0.0")
PORT = int(os.environ.get("ESCAPE_WEB_PORT", "8080"))

HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Escape Sound System</title>
  <style>
    :root {
      --bg: #0b141d;
      --card: #132333;
      --text: #eaf2fb;
      --muted: #9db1c7;
      --danger: #d64045;
      --danger-hover: #bf3338;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: radial-gradient(circle at top, #1b334a, var(--bg) 60%);
      color: var(--text);
      font-family: "Segoe UI", Tahoma, sans-serif;
    }
    .panel {
      width: min(92vw, 420px);
      padding: 24px;
      border-radius: 16px;
      background: linear-gradient(180deg, #1a2e43, var(--card));
      border: 1px solid #2f4962;
      box-shadow: 0 16px 40px rgba(0, 0, 0, 0.35);
      text-align: center;
    }
    h1 {
      margin: 0 0 10px;
      font-size: 1.25rem;
      font-weight: 700;
    }
    p {
      margin: 0 0 20px;
      color: var(--muted);
      font-size: 0.95rem;
      line-height: 1.4;
    }
    button {
      border: 0;
      border-radius: 10px;
      padding: 12px 18px;
      background: var(--danger);
      color: #fff;
      font-size: 1rem;
      font-weight: 700;
      cursor: pointer;
      transition: background 140ms ease;
    }
    button:hover { background: var(--danger-hover); }
    button:disabled { opacity: 0.65; cursor: not-allowed; }
    #status {
      margin-top: 14px;
      min-height: 1.2em;
      font-size: 0.9rem;
      color: #c9d9ea;
    }
  </style>
</head>
<body>
  <div class="panel">
    <h1>Escape Sound System</h1>
    <p>Shut down this Raspberry Pi safely after confirming.</p>
    <button id="shutdownBtn" type="button">Shutdown Pi</button>
    <div id="status" aria-live="polite"></div>
  </div>

  <script>
    const button = document.getElementById("shutdownBtn");
    const status = document.getElementById("status");

    async function requestShutdown() {
      const yes = window.confirm("Are you sure you want to shut down this Raspberry Pi now?");
      if (!yes) {
        return;
      }

      button.disabled = true;
      status.textContent = "Sending shutdown request...";

      try {
        const response = await fetch("/api/shutdown", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ confirm: true })
        });

        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(data.error || "Shutdown failed");
        }

        status.textContent = data.message || "Shutdown started.";
      } catch (err) {
        status.textContent = "Error: " + err.message;
        button.disabled = false;
      }
    }

    button.addEventListener("click", requestShutdown);
  </script>
</body>
</html>
"""


def shutdown_host():
    # Give the HTTP response time to be sent before powering off.
    time.sleep(1.0)
    subprocess.run(["sync"], check=False)
    subprocess.run(["systemctl", "poweroff"], check=False)


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
            return

        data = HTML.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/api/shutdown":
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

        threading.Thread(target=shutdown_host, daemon=True).start()
        self._json(HTTPStatus.ACCEPTED, {"message": "Shutdown request accepted. Powering off..."})

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
