#!/usr/bin/env python3
import json
import os
import signal
import shlex
import subprocess
import sys
import time
from typing import Any, Dict, Optional

import pygame
import paho.mqtt.client as mqtt

LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}

def log(cfg, level: str, *parts):
    want = str(cfg.get("logging", {}).get("level", "INFO")).upper()
    if LOG_LEVELS.get(level, 20) >= LOG_LEVELS.get(want, 20):
        print(f"[{level}]", *parts, flush=True)

CONFIG_PATH = "/home/pi/escape-sound-system/config/config.json"
STATUS_TOPIC = "escape/audio/status"
STATUS_INTERVAL_S = 5
DEFAULT_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}

running = True

def on_signal(signum, frame):
    global running
    running = False

signal.signal(signal.SIGTERM, on_signal)
signal.signal(signal.SIGINT, on_signal)

def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))

def safe_join(base: str, name: str) -> str:
    name = (name or "").strip().lstrip("/").replace("..", "")
    return os.path.join(base, name)

def now_ms() -> int:
    return int(time.time() * 1000)

def fade_music_to(target: float, duration_ms: int, steps: int = 20):
    """Software fade for music volume (pygame mixer.music has no smooth volume fade)."""
    target = clamp01(target)
    duration_ms = max(0, int(duration_ms))

    if duration_ms == 0:
        pygame.mixer.music.set_volume(target)
        return

    current = pygame.mixer.music.get_volume()
    steps = max(1, int(steps))
    dt = duration_ms / steps / 1000.0
    dv = (target - current) / steps

    for i in range(steps):
        pygame.mixer.music.set_volume(clamp01(current + dv * (i + 1)))
        time.sleep(dt)

def parse_payload(payload: bytes) -> Dict[str, Any]:
    s = payload.decode("utf-8", errors="ignore").strip()
    if not s:
        return {}
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            return {"raw": s}
    return {"raw": s}

class SoundSystem:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        audio = cfg["audio"]
        self.base_path = audio["base_path"]
        video = cfg.get("video", {})
        self.video_base_path = video.get("base_path", self.base_path)
        self.video_extensions = {
            str(ext).lower() for ext in video.get("extensions", sorted(DEFAULT_VIDEO_EXTENSIONS))
        }
        self.video_mode = str(video.get("mode", "auto")).lower()
        self.video_hdmi_connector = str(video.get("hdmi_connector", "HDMI-A-1")).strip()
        raw_video_cmd = video.get("player_cmd")
        if raw_video_cmd is None:
            raw_video_cmd = self.default_video_player_cmd()
        if isinstance(raw_video_cmd, str):
            self.video_player_cmd = shlex.split(raw_video_cmd)
        else:
            self.video_player_cmd = [str(part) for part in raw_video_cmd]

        self.bg_default = float(audio["bg_default_volume"])
        self.hint_default = float(audio["hint_default_volume"])
        self.duck_volume = float(audio["duck_volume"])
        self.duck_fade_ms = int(audio["duck_fade_ms"])
        self.restore_fade_ms = int(audio["restore_fade_ms"])
        self.bg_fade_ms = int(audio["bg_fade_ms"])

        self.hint_channel: Optional[pygame.mixer.Channel] = None
        self.current_hint_sound = None
        self.hint_playing = False
        self.hint_mode = "audio"
        self.bg_video_proc: Optional[subprocess.Popen] = None
        self.hint_video_proc: Optional[subprocess.Popen] = None

    def init_audio(self):
        pygame.mixer.init()
        pygame.mixer.set_num_channels(8)
        self.hint_channel = pygame.mixer.Channel(1)

    def default_video_player_cmd(self):
        has_graphical_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        force_drm = self.video_mode == "drm"
        use_drm = force_drm or (self.video_mode == "auto" and not has_graphical_display)

        cmd = ["mpv", "--fs", "--no-terminal", "--really-quiet"]
        if use_drm:
            cmd.extend(["--vo=gpu", "--gpu-context=drm"])
            if self.video_hdmi_connector:
                cmd.append(f"--drm-connector={self.video_hdmi_connector}")
        return cmd

    def is_video_file(self, filename: str) -> bool:
        _, ext = os.path.splitext((filename or "").strip().lower())
        return ext in self.video_extensions

    def stop_bg_video(self):
        if self.bg_video_proc and self.bg_video_proc.poll() is None:
            self.bg_video_proc.terminate()
            try:
                self.bg_video_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.bg_video_proc.kill()
        self.bg_video_proc = None

    def stop_hint_video(self):
        if self.hint_video_proc and self.hint_video_proc.poll() is None:
            self.hint_video_proc.terminate()
            try:
                self.hint_video_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.hint_video_proc.kill()
        self.hint_video_proc = None

    def play_video(self, filename: str, loop: bool = False, tag: str = "VIDEO") -> Optional[subprocess.Popen]:
        path = safe_join(self.video_base_path, filename)
        if not os.path.isfile(path):
            print(f"[{tag}] file not found: {path}", flush=True)
            return None
        if not self.video_player_cmd:
            print(f"[{tag}] missing player_cmd configuration", flush=True)
            return None

        cmd = list(self.video_player_cmd)
        if loop:
            cmd.append("--loop=inf")
        cmd.append(path)

        candidates = [cmd]
        if self.video_mode == "auto" and any(part in ("--gpu-context=drm", "--vo=gpu") for part in cmd):
            fallback = [part for part in cmd if part not in ("--gpu-context=drm", "--vo=gpu")]
            fallback = [part for part in fallback if not part.startswith("--drm-connector=")]
            candidates.append(fallback)

        for candidate in candidates:
            try:
                proc = subprocess.Popen(
                    candidate,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                print(f"[{tag}] play {filename} loop={loop}", flush=True)
                return proc
            except Exception as exc:
                print(f"[{tag}] failed to start player cmd={candidate!r}: {exc}", flush=True)

        return None

    def restore_bg_after_hint(self):
        self.hint_playing = False
        self.current_hint_sound = None
        self.hint_mode = "audio"
        self.hint_video_proc = None
        fade_music_to(self.bg_default, self.restore_fade_ms)
        print("[HINT] finished (restore bg)", flush=True)

    def bg_start(self, filename: str):
        if self.is_video_file(filename):
            self.stop_bg_video()
            pygame.mixer.music.stop()
            self.bg_video_proc = self.play_video(filename, loop=True, tag="BG-VIDEO")
            return

        self.stop_bg_video()
        path = safe_join(self.base_path, filename)
        if not os.path.isfile(path):
            print(f"[BG] file not found: {path}", flush=True)
            return
        pygame.mixer.music.load(path)
        pygame.mixer.music.set_volume(clamp01(self.bg_default))
        pygame.mixer.music.play(-1)
        print(f"[BG] start {filename} vol={self.bg_default}", flush=True)

    def bg_stop(self):
        pygame.mixer.music.stop()
        self.stop_bg_video()
        print("[BG] stop", flush=True)

    def bg_switch(self, filename: str):
        if self.is_video_file(filename) or self.bg_video_proc:
            self.bg_stop()
            self.bg_start(filename)
            print(f"[BG] switch -> {filename}", flush=True)
            return

        # no crossfade: fade out old, switch, fade in new
        fade_music_to(0.0, self.bg_fade_ms)
        self.bg_stop()
        self.bg_start(filename)
        fade_music_to(self.bg_default, self.bg_fade_ms)
        print(f"[BG] switch -> {filename} fade_ms={self.bg_fade_ms}", flush=True)

    def panic(self):
        # stop hint + bg and restore volumes to defaults
        if self.hint_channel:
            self.hint_channel.stop()
        self.stop_hint_video()
        self.stop_bg_video()
        self.current_hint_sound = None
        self.hint_playing = False
        self.hint_mode = "audio"
        pygame.mixer.music.stop()
        pygame.mixer.music.set_volume(clamp01(self.bg_default))
        print("[PANIC] stopped hint + bg", flush=True)

    def hint_stop(self):
        if self.hint_channel:
            self.hint_channel.stop()
        self.stop_hint_video()
        self.current_hint_sound = None
        self.hint_playing = False
        self.hint_mode = "audio"
        fade_music_to(self.bg_default, self.restore_fade_ms)
        print("[HINT] stop (restore bg)", flush=True)

    def hint_play_interrupt(self, filename: str, volume: Optional[float] = None):
        if not self.hint_channel:
            print("[HINT] channel not ready", flush=True)
            return

        # interrupt: stop current hint
        self.hint_channel.stop()
        self.stop_hint_video()
        self.current_hint_sound = None
        self.hint_playing = False
        self.hint_mode = "audio"

        if self.is_video_file(filename):
            fade_music_to(self.duck_volume, self.duck_fade_ms)
            proc = self.play_video(filename, loop=False, tag="HINT-VIDEO")
            if not proc:
                fade_music_to(self.bg_default, self.restore_fade_ms)
                return
            self.hint_video_proc = proc
            self.hint_playing = True
            self.hint_mode = "video"
            print(f"[HINT] play video {filename} duck_to={self.duck_volume}", flush=True)
            return

        path = safe_join(self.base_path, filename)
        if not os.path.isfile(path):
            print(f"[HINT] file not found: {path}", flush=True)
            return

        # duck bg then play hint
        fade_music_to(self.duck_volume, self.duck_fade_ms)

        vol = self.hint_default if volume is None else float(volume)
        self.current_hint_sound = pygame.mixer.Sound(path)  # keep reference alive
        self.current_hint_sound.set_volume(clamp01(vol))
        self.hint_channel.play(self.current_hint_sound)
        self.hint_playing = True
        self.hint_mode = "audio"
        print(f"[HINT] play {filename} vol={vol} duck_to={self.duck_volume}", flush=True)

    def tick(self):
        # when hint finishes, restore bg volume
        if not self.hint_playing:
            return

        if self.hint_mode == "video":
            if self.hint_video_proc and self.hint_video_proc.poll() is not None:
                self.restore_bg_after_hint()
            return

        if self.hint_channel and not self.hint_channel.get_busy():
            self.restore_bg_after_hint()

def main():
    cfg = load_config()

    topics = cfg["mqtt"]["topics"]
    topic_bg = topics["bg"]
    topic_hint = topics["hint"]
    topic_panic = topics["panic"]
    qos = int(cfg["mqtt"].get("qos", 0))

    ss = SoundSystem(cfg)
    ss.init_audio()

    client = mqtt.Client()
    client.connect(cfg["mqtt"]["host"], int(cfg["mqtt"]["port"]), keepalive=60)
    client.subscribe([(topic_bg, qos), (topic_hint, qos), (topic_panic, qos)])
    last_status = 0.0

    def publish_status():
        # retained so dashboards instantly know status after refresh
        client.publish(STATUS_TOPIC, json.dumps({"status": "ok"}), qos=0, retain=True)

    def on_message(client, userdata, msg):
        log(cfg, "DEBUG", f"recv topic={msg.topic} payload={msg.payload!r}")
        data = parse_payload(msg.payload)
        t = msg.topic

        # allow simple strings too
        raw = data.get("raw")
        if raw:
            parts = raw.split()
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else None
            data = {"cmd": cmd, "file": arg}

        cmd = (data.get("cmd") or "").lower()
        file_name = data.get("file")
        vol = data.get("volume")

        if t == topic_panic:
            ss.panic()
            return

        if t == topic_bg:
            if cmd == "start":
                if not file_name:
                    print("[BG] missing file", flush=True)
                    return
                ss.bg_start(file_name)
            elif cmd == "stop":
                ss.bg_stop()
            elif cmd in ("switch", "play"):
                if not file_name:
                    print("[BG] missing file", flush=True)
                    return
                ss.bg_switch(file_name)
            else:
                print("[BG] unknown cmd:", cmd, data, flush=True)
            return

        if t == topic_hint:
            if cmd == "play":
                if not file_name:
                    print("[HINT] missing file", flush=True)
                    return
                ss.hint_play_interrupt(file_name, volume=vol)
            elif cmd == "stop":
                ss.hint_stop()
            else:
                print("[HINT] unknown cmd:", cmd, data, flush=True)

    client.on_message = on_message

    print("[SYSTEM] ready", flush=True)

    global running
    try:
        while running:
            client.loop(timeout=0.05)
            now = time.time()
            if now - last_status >= STATUS_INTERVAL_S:
                publish_status()
                last_status = now
            ss.tick()
            time.sleep(0.02)
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
        ss.stop_hint_video()
        ss.stop_bg_video()
        pygame.mixer.quit()

if __name__ == "__main__":
    sys.exit(main())
