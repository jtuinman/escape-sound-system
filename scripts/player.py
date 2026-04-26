#!/usr/bin/env python3
import json
import os
import signal
import sys
import time
import traceback
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
MQTT_RECONNECT_INTERVAL_S = 5
MQTT_UNHEALTHY_EXIT_S = 90
LANGUAGE_TOPIC = "escape/audio/language"
SUPPORTED_LANGUAGES = {"nl", "en"}
DEFAULT_LANGUAGE = "nl"
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

def mqtt_rc_name(rc: Any) -> str:
    try:
        return mqtt.error_string(int(rc))
    except Exception:
        return str(rc)

def reason_code_value(reason_code: Any) -> int:
    try:
        return int(reason_code)
    except Exception:
        value = getattr(reason_code, "value", None)
        if value is None:
            return 0 if str(reason_code).lower() == "success" else 1
        return int(value)

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
        self.language = DEFAULT_LANGUAGE

        self.bg_default = float(audio["bg_default_volume"])
        self.hint_default = float(audio["hint_default_volume"])
        self.bg_volume = clamp01(float(audio.get("bg_start_volume", self.bg_default)))
        self.hint_volume = clamp01(float(audio.get("hint_start_volume", self.hint_default)))
        self.duck_factor = clamp01(float(audio.get("duck_factor_percent", 30)) / 100.0)

        print(
            f"[CONFIG] bg_start={self.bg_volume} hint_start={self.hint_volume} duck_factor={self.duck_factor}",
            flush=True
        )
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

    def set_bg_volume(self, volume: float):
        self.bg_volume = clamp01(float(volume))
        if pygame.mixer.music.get_busy() and not self.hint_playing:
            pygame.mixer.music.set_volume(self.bg_volume)
        print(f"[BG] volume set to {self.bg_volume}", flush=True)

    def set_hint_volume(self, volume: float):
        self.hint_volume = clamp01(float(volume))
        if self.current_hint_sound is not None:
            self.current_hint_sound.set_volume(self.hint_volume)
        print(f"[HINT] volume set to {self.hint_volume}", flush=True)

    def set_duck_factor(self, percent: float):
        # verwacht 0–100 vanuit UI
        self.duck_factor = clamp01(float(percent) / 100.0)
        print(f"[DUCK] factor set to {self.duck_factor}", flush=True)        

    def set_language(self, language: str):
        language = (language or DEFAULT_LANGUAGE).strip().lower()
        if language not in SUPPORTED_LANGUAGES:
            language = DEFAULT_LANGUAGE
        self.language = language
        print(f"[LANG] set language to {self.language}", flush=True)

    def hint_base_path(self) -> str:
        return os.path.join(self.base_path, self.language.upper())

    def bg_start(self, filename: str):
        path = safe_join(self.base_path, filename)
        if not os.path.isfile(path):
            print(f"[BG] file not found: {path}", flush=True)
            return
        pygame.mixer.music.load(path)
        pygame.mixer.music.set_volume(self.bg_volume)
        pygame.mixer.music.play(-1)
        print(f"[BG] start {filename} vol={self.bg_volume}", flush=True)

    def bg_stop(self):
        pygame.mixer.music.stop()
        print("[BG] stop", flush=True)

    def bg_switch(self, filename: str):
        # no crossfade: fade out old, switch, fade in new
        fade_music_to(0.0, self.bg_fade_ms)
        self.bg_stop()
        self.bg_start(filename)
        fade_music_to(self.bg_volume, self.bg_fade_ms)
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
        fade_music_to(self.bg_volume, self.restore_fade_ms)
        print("[HINT] stop (restore bg)", flush=True)

    def hint_play_interrupt(self, filename: str, volume: Optional[float] = None):
        if not self.hint_channel:
            print("[HINT] channel not ready", flush=True)
            return

        # interrupt: stop current hint
        self.hint_channel.stop()
        self.current_hint_sound = None
        self.hint_playing = False

        path = safe_join(self.hint_base_path(), filename)
        if not os.path.isfile(path):
            print(f"[HINT] file not found: {path}", flush=True)
            return

        # duck bg then play hint
        ducked_volume = self.bg_volume * self.duck_factor
        fade_music_to(ducked_volume, self.duck_fade_ms)

        vol = self.hint_volume if volume is None else float(volume)
        self.current_hint_sound = pygame.mixer.Sound(path)  # keep reference alive
        self.current_hint_sound.set_volume(clamp01(vol))
        self.hint_channel.play(self.current_hint_sound)
        self.hint_playing = True
        print(f"[HINT] play {filename} vol={vol} duck_to={self.bg_volume * self.duck_factor}", flush=True)

    def tick(self):
        # when hint finishes, restore bg volume
        if self.hint_channel and self.hint_playing and not self.hint_channel.get_busy():
            self.hint_playing = False
            self.current_hint_sound = None
            fade_music_to(self.bg_volume, self.restore_fade_ms)
            print("[HINT] finished (restore bg)", flush=True)

def main():
    cfg = load_config()

    topics = cfg["mqtt"]["topics"]
    topic_volume_bg = "escape/audio/volume/bg"
    topic_volume_hint = "escape/audio/volume/hint"
    topic_duck = "escape/audio/duck"    
    topic_bg = topics["bg"]
    topic_hint = topics["hint"]
    topic_panic = topics["panic"]
    qos = int(cfg["mqtt"].get("qos", 0))
    subscriptions = [
        (topic_bg, qos),
        (topic_hint, qos),
        (topic_panic, qos),
        (LANGUAGE_TOPIC, qos),
        (topic_volume_bg, qos),
        (topic_volume_hint, qos),
        (topic_duck, qos),
    ]

    ss = SoundSystem(cfg)
    ss.init_audio()

    client = mqtt.Client()
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    mqtt_state = {
        "connected": False,
        "subscriptions_active": False,
        "pending_subscribe_mid": None,
        "last_healthy": time.time(),
        "last_reconnect_attempt": 0.0,
    }
    last_status = 0.0

    def publish_status():
        # retained so dashboards instantly know status after refresh
        if not mqtt_state["connected"]:
            return

        if mqtt_state["subscriptions_active"]:
            payload = {"status": "ok", "mqtt": "connected", "subscriptions": "active"}
        else:
            payload = {
                "status": "error",
                "mqtt": "connected",
                "subscriptions": "inactive",
            }
        client.publish(STATUS_TOPIC, json.dumps(payload), qos=0, retain=True)

    def on_connect(client, userdata, flags, rc, properties=None):
        if reason_code_value(rc) != 0:
            mqtt_state["connected"] = False
            mqtt_state["subscriptions_active"] = False
            print(f"[MQTT] connect failed rc={rc} ({mqtt_rc_name(rc)})", flush=True)
            return

        mqtt_state["connected"] = True
        mqtt_state["subscriptions_active"] = False
        print(f"[MQTT] connected rc={rc} host={cfg['mqtt']['host']} port={cfg['mqtt']['port']}", flush=True)

        result, mid = client.subscribe(subscriptions)
        mqtt_state["pending_subscribe_mid"] = mid
        if result != mqtt.MQTT_ERR_SUCCESS:
            print(f"[MQTT] subscribe request failed rc={result} ({mqtt_rc_name(result)})", flush=True)
            return

        topic_names = ", ".join(topic for topic, _ in subscriptions)
        print(f"[MQTT] subscribe requested mid={mid} topics={topic_names}", flush=True)

    def on_disconnect(client, userdata, *args):
        reason = args[0] if args else 0
        if len(args) >= 2:
            reason = args[1]

        mqtt_state["connected"] = False
        mqtt_state["subscriptions_active"] = False
        mqtt_state["pending_subscribe_mid"] = None
        print(f"[MQTT] disconnected rc={reason} ({mqtt_rc_name(reason)})", flush=True)

    def on_subscribe(client, userdata, mid, granted_qos, properties=None):
        mqtt_state["subscriptions_active"] = True
        mqtt_state["last_healthy"] = time.time()
        print(f"[MQTT] subscribed mid={mid} granted_qos={granted_qos}", flush=True)

    def on_message(client, userdata, msg):
        try:
            log(cfg, "DEBUG", f"recv topic={msg.topic} payload={msg.payload!r}")
            data = parse_payload(msg.payload)
            t = msg.topic

            if t == LANGUAGE_TOPIC:
                lang = data.get("language") or data.get("raw")
                ss.set_language(str(lang or DEFAULT_LANGUAGE))
                return

            if t == topic_volume_bg:
                try:
                    val = float(data.get("volume") if data.get("volume") is not None else data.get("raw"))
                    ss.set_bg_volume(val)
                except Exception:
                    print("[BG] invalid volume", data, flush=True)
                return

            if t == topic_volume_hint:
                try:
                    val = float(data.get("volume") if data.get("volume") is not None else data.get("raw"))
                    ss.set_hint_volume(val)
                except Exception:
                    print("[HINT] invalid volume", data, flush=True)
                return

            if t == topic_duck:
                try:
                    val = float(data.get("duck") if data.get("duck") is not None else data.get("raw"))
                    ss.set_duck_factor(val)
                except Exception:
                    print("[DUCK] invalid value", data, flush=True)
                return

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
        except Exception:
            print(f"[MQTT] on_message exception topic={getattr(msg, 'topic', None)}", flush=True)
            traceback.print_exc()

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    client.will_set(
        STATUS_TOPIC,
        json.dumps({"status": "error", "mqtt": "disconnected", "subscriptions": "inactive"}),
        qos=0,
        retain=True,
    )
    client.connect(cfg["mqtt"]["host"], int(cfg["mqtt"]["port"]), keepalive=60)

    print("[SYSTEM] ready", flush=True)

    global running
    try:
        while running:
            now = time.time()
            try:
                rc = client.loop(timeout=0.05)
            except Exception:
                rc = mqtt.MQTT_ERR_CONN_LOST
                print("[MQTT] loop exception", flush=True)
                traceback.print_exc()

            if rc == mqtt.MQTT_ERR_SUCCESS:
                if mqtt_state["connected"] and mqtt_state["subscriptions_active"]:
                    mqtt_state["last_healthy"] = now
            else:
                mqtt_state["connected"] = False
                mqtt_state["subscriptions_active"] = False
                if now - mqtt_state["last_reconnect_attempt"] >= MQTT_RECONNECT_INTERVAL_S:
                    mqtt_state["last_reconnect_attempt"] = now
                    print(f"[MQTT] loop rc={rc} ({mqtt_rc_name(rc)}), reconnecting", flush=True)
                    try:
                        client.reconnect()
                    except Exception:
                        print("[MQTT] reconnect failed", flush=True)
                        traceback.print_exc()

            if mqtt_state["last_healthy"] and now - mqtt_state["last_healthy"] > MQTT_UNHEALTHY_EXIT_S:
                print(
                    f"[MQTT] unhealthy for >{MQTT_UNHEALTHY_EXIT_S}s; exiting for service restart",
                    flush=True,
                )
                return 1

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
