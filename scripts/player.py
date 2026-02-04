#!/usr/bin/env python3
import json
import os
import time
import queue
import threading
import pygame
import paho.mqtt.client as mqtt

BASE = "/home/pi/escape-sound-system"
AUDIO_DIR = os.path.join(BASE, "audio")

MQTT_HOST = "localhost"
MQTT_PORT = 1883

TOPIC_BG = "escape/audio/bg"       # track 1: state/background
TOPIC_HINT = "escape/audio/hint"   # track 2: spoken hints

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
def bg_start(file_name: str, volume: float = 0.5, loop: bool = True, fade_ms: int = 0):
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

def bg_volume(volume: float):
    pygame.mixer.music.set_volume(clamp01(volume))
    print(f"[BG] volume {volume}")

# ---- Hints (track 2) ----
hint_q: "queue.Queue[tuple[str,float]]" = queue.Queue()
hint_worker_running = True

def hint_play_now(file_name: str, volume: float = 1.0):
    path = audio_path(file_name)
    if not os.path.isfile(path):
        print(f"[HINT] file not found: {path}")
        return
    snd = pygame.mixer.Sound(path)
    snd.set_volume(clamp01(volume))
    hint_channel.play(snd)
    print(f"[HINT] play-now file={file_name} vol={volume}")

def hint_stop():
    # stop current + clear queue
    while not hint_q.empty():
        try:
            hint_q.get_nowait()
        except queue.Empty:
            break
    hint_channel.stop()
    print("[HINT] stop (and cleared queue)")

def hint_enqueue(file_name: str, volume: float = 1.0):
    hint_q.put((file_name, float(volume)))
    print(f"[HINT] queued file={file_name} vol={volume}")

def hint_worker():
    # plays queued hints sequentially
    global hint_worker_running
    while hint_worker_running:
        try:
            file_name, vol = hint_q.get(timeout=0.2)
        except queue.Empty:
            continue

        # Wait until channel is free
        while hint_worker_running and hint_channel.get_busy():
            time.sleep(0.05)

        if not hint_worker_running:
            break

        hint_play_now(file_name, vol)

        # Wait until it finishes
        while hint_worker_running and hint_channel.get_busy():
            time.sleep(0.05)

# ---- MQTT ----
def on_connect(client, userdata, flags, rc):
    print("[MQTT] connected rc=", rc)
    client.subscribe([(TOPIC_BG, 0), (TOPIC_HINT, 0)])

def on_message(client, userdata, msg):
    data = parse_payload(msg.payload)
    topic = msg.topic

    # Backward compatible string commands like: "start state1.mp3"
    raw = data.get("raw")
    if raw:
        parts = raw.split()
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else None
        data = {"cmd": cmd, "file": arg}

    cmd = (data.get("cmd") or "").lower()
    file_name = data.get("file")
    volume = float(data.get("volume", 0.5 if topic == TOPIC_BG else 1.0))
    fade_ms = int(data.get("fade_ms", 0))
    loop = bool(data.get("loop", True))
    mode = (data.get("mode") or "interrupt").lower()  # for hints: interrupt|queue

    if topic == TOPIC_BG:
        if cmd in ("start", "play"):
            if not file_name:
                print("[BG] missing file")
                return
            bg_start(file_name, volume=volume, loop=loop, fade_ms=fade_ms)
        elif cmd in ("stop", "pause"):
            bg_stop(fade_ms=fade_ms)
        elif cmd == "volume":
            bg_volume(volume)
        else:
            print("[BG] unknown cmd:", cmd, data)

    elif topic == TOPIC_HINT:
        if cmd in ("play", "start"):
            if not file_name:
                print("[HINT] missing file")
                return
            if mode == "queue":
                hint_enqueue(file_name, volume)
            else:
                # interrupt
                hint_channel.stop()
                hint_play_now(file_name, volume)
        elif cmd == "stop":
            hint_stop()
        else:
            print("[HINT] unknown cmd:", cmd, data)

def main():
    global hint_channel, hint_worker_running

    pygame.mixer.init()
    pygame.mixer.set_num_channels(8)
    hint_channel = pygame.mixer.Channel(1)

    t = threading.Thread(target=hint_worker, daemon=True)
    t.start()

    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)

    print("[SYSTEM] ready. Listening for MQTT...")
    client.loop_start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        hint_worker_running = False
        client.loop_stop()
        pygame.mixer.quit()

if __name__ == "__main__":
    main()
