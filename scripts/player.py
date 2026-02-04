#!/usr/bin/env python3
import json
import os
import time
import queue
import signal

import pygame
import paho.mqtt.client as mqtt

BASE = "/home/pi/escape-sound-system"
AUDIO_DIR = os.path.join(BASE, "audio")

MQTT_HOST = "localhost"
MQTT_PORT = 1883

TOPIC_BG = "escape/audio/bg"
TOPIC_HINT = "escape/audio/hint"
TOPIC_STATE = "escape/state"

STATE_TO_FILE = {
    "state1": "state1.mp3",
    "state2": "state2.mp3",
    "state3": "state3.mp3",
}
current_state = None

# ======= VOLUMES (JOUW STANDAARD) =======
BG_DEFAULT_VOL = 0.70
BG_HINT_VOL = 0.30
# =======================================

running = True

def _handle_sigterm(signum, frame):
    global running
    running = False

signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT, _handle_sigterm)

def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))

def audio_path(name: str) -> str:
    name = (name or "").strip().lstrip("/").replace("..", "")
    return os.path.join(AUDIO_DIR, name)

def parse_payload(payload: bytes):
    s = payload.decode("utf-8", errors="ignore").strip()
    if not s:
        return {}
    if s.startswith("{"):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return {"raw": s}
    return {"raw": s}

# ---- Background (track 1) ----
def bg_start(file_name: str, volume: float = BG_DEFAULT_VOL, loop: bool = True, fade_ms: int = 0):
    path = audio_path(file_name)
    if not os.path.isfile(path):
        print(f"[BG] file not found: {path}")
        return
    pygame.mixer.music.load(path)
    pygame.mixer.music.set_volume(clamp01(volume))
    pygame.mixer.music.play(-1 if loop else 0, fade_ms=fade_ms)
    print(f"[BG] start file={file_name} vol={volume} loop={loop} fade_ms={fade_ms}")

def bg_stop(fade_ms: int = 0):
    if fade_ms > 0:
        pygame.mixer.music.fadeout(fade_ms)
    else:
        pygame.mixer.music.stop()
    print(f"[BG] stop fade_ms={fade_ms}")

def bg_set(volume: float):
    pygame.mixer.music.set_volume(clamp01(volume))
    print(f"[BG] volume set {volume}")

# ---- Hints (track 2) ----
hint_q: "queue.Queue[tuple[str,float,str]]" = queue.Queue()
current_hint_sound = None

def hint_play_now(file_name: str, volume: float = 1.0):
    global current_hint_sound
    path = audio_path(file_name)
    if not os.path.isfile(path):
        print(f"[HINT] file not found: {path}")
        return False

    # duck BG during hint
    bg_set(BG_HINT_VOL)

    current_hint_sound = pygame.mixer.Sound(path)  # keep reference alive
    current_hint_sound.set_volume(clamp01(volume))
    hint_channel.play(current_hint_sound)
    print(f"[HINT] play-now file={file_name} vol={volume}")
    return True

def hint_restore_bg_if_needed():
    # restore BG when hint finished
    if not hint_channel.get_busy():
        bg_set(BG_DEFAULT_VOL)

def hint_stop(clear_queue: bool = True):
    global current_hint_sound
    if clear_queue:
        while not hint_q.empty():
            try:
                hint_q.get_nowait()
            except queue.Empty:
                break
    hint_channel.stop()
    current_hint_sound = None
    bg_set(BG_DEFAULT_VOL)
    print("[HINT] stop (and cleared queue)")

# ---- MQTT ----
def on_connect(client, userdata, flags, rc):
    print("[MQTT] connected rc=", rc)
    client.subscribe([(TOPIC_BG, 0), (TOPIC_HINT, 0), (TOPIC_STATE, 0)])

def on_message(client, userdata, msg):
    data = parse_payload(msg.payload)
    topic = msg.topic
    global current_state

    if topic == TOPIC_STATE:
        s = msg.payload.decode("utf-8", errors="ignore").strip().lower()
        if not s:
            print("[STATE] empty payload")
            return

        # allow JSON too: {"state":"state1"}
        if s.startswith("{"):
            try:
                obj = json.loads(s)
                s = str(obj.get("state", "")).strip().lower()
            except Exception:
                pass

        if s not in STATE_TO_FILE:
            print("[STATE] unknown state:", s)
            return

        if s == current_state:
            print("[STATE] unchanged:", s)
            return

        current_state = s
        file_name = STATE_TO_FILE[s]
        bg_start(file_name, volume=BG_DEFAULT_VOL, loop=True, fade_ms=500)
        print("[STATE] set:", s, "->", file_name)
        return

    # Backwards compatible: "start state1.mp3"
    raw = data.get("raw")
    if raw:
        parts = raw.split()
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else None
        data = {"cmd": cmd, "file": arg}

    cmd = (data.get("cmd") or "").lower()
    file_name = data.get("file")
    volume = float(data.get("volume", 1.0))
    fade_ms = int(data.get("fade_ms", 0))
    loop = bool(data.get("loop", True))
    mode = (data.get("mode") or "interrupt").lower()  # interrupt|queue

    if topic == TOPIC_BG:
        if cmd in ("start", "play"):
            if not file_name:
                print("[BG] missing file")
                return
            # always use default unless you later decide otherwise
            bg_start(file_name, volume=BG_DEFAULT_VOL, loop=loop, fade_ms=fade_ms)
        elif cmd in ("stop", "pause"):
            bg_stop(fade_ms=fade_ms)
        elif cmd == "volume":
            bg_set(volume)
        else:
            print("[BG] unknown cmd:", cmd, data)

    elif topic == TOPIC_HINT:
        if cmd in ("play", "start"):
            if not file_name:
                print("[HINT] missing file")
                return
            hint_q.put((file_name, float(volume), mode))
            print(f"[HINT] request {mode}: {file_name}")
        elif cmd == "stop":
            hint_q.put(("", 0.0, "stop"))
            print("[HINT] stop request")
        else:
            print("[HINT] unknown cmd:", cmd, data)

def main():
    global hint_channel, running

    pygame.mixer.init()
    pygame.mixer.set_num_channels(8)
    hint_channel = pygame.mixer.Channel(1)

    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)

    print("[SYSTEM] ready. Listening for MQTT...")

    try:
        while running:
            # Process MQTT in THIS thread (geen loop_start thread gezeik)
            client.loop(timeout=0.05)

            # Restore BG if hint ended
            if hint_channel.get_busy() is False:
                # avoid spamming log: only restore if not already at default
                # pygame doesn't expose current music volume, so just set once in a while
                pass

            # Handle hint queue
            try:
                file_name, vol, mode = hint_q.get_nowait()
            except queue.Empty:
                time.sleep(0.02)
                continue

            if mode == "stop":
                hint_stop(clear_queue=True)

            elif mode == "interrupt":
                hint_stop(clear_queue=True)
                hint_play_now(file_name, vol)

            elif mode == "queue":
                if not hint_channel.get_busy():
                    hint_play_now(file_name, vol)
                else:
                    # still playing -> requeue
                    hint_q.put((file_name, vol, "queue"))
                    time.sleep(0.05)

            # If a hint finished, restore BG
            if not hint_channel.get_busy():
                bg_set(BG_DEFAULT_VOL)

    finally:
        try:
            client.disconnect()
        except Exception:
            pass
        pygame.mixer.quit()

if __name__ == "__main__":
    main()
