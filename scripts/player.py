#!/usr/bin/env python3
import json
import os
import signal
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

        self.bg_default = float(audio["bg_default_volume"])
        self.hint_default = float(audio["hint_default_volume"])
        self.duck_volume = float(audio["duck_volume"])
        self.duck_fade_ms = int(audio["duck_fade_ms"])
        self.restore_fade_ms = int(audio["restore_fade_ms"])
        self.bg_fade_ms = int(audio["bg_fade_ms"])

        self.hint_channel: Optional[pygame.mixer.Channel] = None
        self.current_hint_sound = None
        self.hint_playing = False

    def init_audio(self):
        pygame.mixer.init()
        pygame.mixer.set_num_channels(8)
        self.hint_channel = pygame.mixer.Channel(1)

    def bg_start(self, filename: str):
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
        print("[BG] stop", flush=True)

    def bg_switch(self, filename: str):
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
        self.current_hint_sound = None
        self.hint_playing = False
        pygame.mixer.music.stop()
        pygame.mixer.music.set_volume(clamp01(self.bg_default))
        print("[PANIC] stopped hint + bg", flush=True)

    def hint_stop(self):
        if self.hint_channel:
            self.hint_channel.stop()
        self.current_hint_sound = None
        self.hint_playing = False
        fade_music_to(self.bg_default, self.restore_fade_ms)
        print("[HINT] stop (restore bg)", flush=True)

    def hint_play_interrupt(self, filename: str, volume: Optional[float] = None):
        if not self.hint_channel:
            print("[HINT] channel not ready", flush=True)
            return

        # interrupt: stop current hint
        self.hint_channel.stop()
        self.current_hint_sound = None
        self.hint_playing = False

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
        print(f"[HINT] play {filename} vol={vol} duck_to={self.duck_volume}", flush=True)

    def tick(self):
        # when hint finishes, restore bg volume
        if self.hint_channel and self.hint_playing and not self.hint_channel.get_busy():
            self.hint_playing = False
            self.current_hint_sound = None
            fade_music_to(self.bg_default, self.restore_fade_ms)
            print("[HINT] finished (restore bg)", flush=True)

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
        pygame.mixer.quit()

if __name__ == "__main__":
    sys.exit(main())
