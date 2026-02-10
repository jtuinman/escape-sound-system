#!/usr/bin/env python3
import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

running = True
DEFAULT_VIDEO_DIR = Path(__file__).resolve().parent.parent / "video"
DEFAULT_LOG = "/tmp/mpv.log"


def on_signal(signum, frame):
    global running
    running = False


signal.signal(signal.SIGTERM, on_signal)
signal.signal(signal.SIGINT, on_signal)


def safe_join(base: str, name: str) -> str:
    cleaned = (name or "").strip().lstrip("/").replace("..", "")
    return os.path.join(base, cleaned)


def read_text(path: Path) -> str:
    try:
        return path.read_text(errors="ignore").strip()
    except Exception:
        return ""


def detect_drm_connector(prefer_hdmi: bool = True) -> Optional[str]:
    """
    Detect the first connected DRM connector from /sys/class/drm.
    Prefers HDMI connectors if prefer_hdmi=True.
    Returns something like 'HDMI-A-1' or 'DP-1', or None if not found.
    """
    drm = Path("/sys/class/drm")
    if not drm.exists():
        return None

    candidates = []
    for entry in drm.iterdir():
        # connectors typically look like: card0-HDMI-A-1, card1-DP-1, etc.
        if "-" not in entry.name:
            continue
        status_file = entry / "status"
        status = read_text(status_file).lower()
        if status != "connected":
            continue

        name = entry.name
        # strip "cardX-" prefix
        if name.startswith("card"):
            parts = name.split("-", 1)
            if len(parts) == 2:
                name = parts[1]

        candidates.append(name)

    if not candidates:
        return None

    if prefer_hdmi:
        for c in candidates:
            if c.startswith("HDMI"):
                return c

    return candidates[0]


def build_cmd(
    video_path: str,
    connector: Optional[str],
    loop: bool,
    mute: bool,
    log_file: str,
    verbose: bool,
) -> List[str]:
    cmd = [
        "mpv",
        "--fs",
        "--vo=gpu",
        "--gpu-context=drm",
        "--hwdec=auto",
        f"--log-file={log_file}",
    ]

    # If you force a wrong connector, mpv often exits immediately.
    if connector:
        cmd.append(f"--drm-connector={connector}")

    if loop:
        cmd.append("--loop=inf")
    if mute:
        cmd.append("--mute=yes")

    # Silence only if NOT verbose (but keep log file regardless)
    if not verbose:
        cmd += ["--no-terminal", "--really-quiet"]
    else:
        cmd += ["--msg-level=all=info"]

    cmd.append(video_path)
    return cmd


def tail_file(path: str, max_lines: int = 80) -> str:
    try:
        with open(path, "r", errors="ignore") as f:
            lines = f.readlines()
        return "".join(lines[-max_lines:]).rstrip()
    except Exception:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Play a video on Raspberry Pi HDMI output using DRM/KMS (headless)."
    )
    parser.add_argument("file", help="Video filename or absolute path")
    parser.add_argument(
        "--base-path",
        default=str(DEFAULT_VIDEO_DIR),
        help="Base directory for relative video filenames",
    )
    parser.add_argument(
        "--connector",
        default="auto",
        help="DRM connector name (e.g. HDMI-A-1). Use 'auto' to detect (default).",
    )
    parser.add_argument("--loop", action="store_true", help="Loop video forever")
    parser.add_argument("--mute", action="store_true", help="Mute video audio")
    parser.add_argument(
        "--log-file",
        default=DEFAULT_LOG,
        help=f"mpv log file path (default {DEFAULT_LOG})",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show mpv output in terminal (also logs to --log-file).",
    )
    args = parser.parse_args()

    if os.path.isabs(args.file):
        video_path = args.file
    else:
        video_path = safe_join(args.base_path, args.file)

    if not os.path.isfile(video_path):
        print(f"[VIDEO] file not found: {video_path}", flush=True)
        return 1

    connector = None
    if args.connector.lower() == "auto":
        connector = detect_drm_connector(prefer_hdmi=True)
        if not connector:
            print(
                "[VIDEO] no connected DRM connector found in /sys/class/drm. "
                "Are you running with KMS/DRM available and an active display?",
                flush=True,
            )
            print(f"[VIDEO] mpv log (if any): {args.log_file}", flush=True)
            return 2
    else:
        connector = args.connector

    cmd = build_cmd(
        video_path=video_path,
        connector=connector,
        loop=args.loop,
        mute=args.mute,
        log_file=args.log_file,
        verbose=args.verbose,
    )

    print(f"[VIDEO] starting: {video_path}", flush=True)
    print(f"[VIDEO] connector: {connector}", flush=True)
    print(f"[VIDEO] log: {args.log_file}", flush=True)

    try:
        proc = subprocess.Popen(cmd)
    except FileNotFoundError:
        print("[VIDEO] mpv not found. Install it (e.g. sudo apt install mpv).", flush=True)
        return 1
    except Exception as exc:
        print(f"[VIDEO] failed to start mpv: {exc}", flush=True)
        return 1

    while running and proc.poll() is None:
        time.sleep(0.1)

    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    rc = proc.returncode if proc.returncode is not None else 0

    if rc != 0:
        print(f"[VIDEO] mpv exited with code {rc}", flush=True)
        last = tail_file(args.log_file)
        if last:
            print("[VIDEO] last mpv log lines:", flush=True)
            print(last, flush=True)
        else:
            print("[VIDEO] no mpv log available.", flush=True)

    return rc


if __name__ == "__main__":
    sys.exit(main())
