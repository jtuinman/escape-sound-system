#!/usr/bin/env python3
import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import List


running = True
DEFAULT_VIDEO_DIR = Path(__file__).resolve().parent.parent / "video"


def on_signal(signum, frame):
    global running
    running = False


signal.signal(signal.SIGTERM, on_signal)
signal.signal(signal.SIGINT, on_signal)


def safe_join(base: str, name: str) -> str:
    cleaned = (name or "").strip().lstrip("/").replace("..", "")
    return os.path.join(base, cleaned)


def build_cmd(video_path: str, connector: str, loop: bool, mute: bool) -> List[str]:
    cmd = [
        "mpv",
        "--fs",
        "--no-terminal",
        "--really-quiet",
        "--vo=gpu",
        "--gpu-context=drm",
        f"--drm-connector={connector}",
    ]
    if loop:
        cmd.append("--loop=inf")
    if mute:
        cmd.append("--mute=yes")
    cmd.append(video_path)
    return cmd


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Play a video on Raspberry Pi HDMI output using DRM/KMS."
    )
    parser.add_argument("file", help="Video filename or absolute path")
    parser.add_argument(
        "--base-path",
        default=str(DEFAULT_VIDEO_DIR),
        help="Base directory for relative video filenames",
    )
    parser.add_argument(
        "--connector",
        default="HDMI-A-2",
        help="DRM connector name (for example: HDMI-A-1 or HDMI-A-2)",
    )
    parser.add_argument("--loop", action="store_true", help="Loop video forever")
    parser.add_argument("--mute", action="store_true", help="Mute video audio")
    args = parser.parse_args()

    if os.path.isabs(args.file):
        video_path = args.file
    else:
        video_path = safe_join(args.base_path, args.file)

    if not os.path.isfile(video_path):
        print(f"[VIDEO] file not found: {video_path}", flush=True)
        return 1

    cmd = build_cmd(video_path, args.connector, args.loop, args.mute)
    print(f"[VIDEO] starting: {video_path}", flush=True)

    try:
        proc = subprocess.Popen(cmd)
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

    return proc.returncode if proc.returncode is not None else 0


if __name__ == "__main__":
    sys.exit(main())
