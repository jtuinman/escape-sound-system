#!/usr/bin/env python3
import json
import os
import time
import queue

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
hint_q: "queue.Queue[tuple[str,float,str]]" = queue.Queue()
# tuple: (file_name, volume, mode) where mode is "queue" or "interrupt"

current_hint_sound = None  # keep reference alive while playing

def hint_play_now(file_name: str, volume: float = 1.0):
    global current_hint_sound
    path = audio_path(file_name)
    if not os.path.isfile(path):
        print(f"[HINT] file not found: {path}")
        return False

    current_hint_sound = pygame.mixer.Sound(path)  # keep reference alive
    current_hint_sound.set_volume(clamp01(volume))
    hint_channel.play(current_hint_sound)
    print(f"[HINT] play-now file={file_name} vol={volume}")
    return True

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
    print("[HINT] stop (and cleared queue)" if clear_queue else "[HINT] stop")

def hint_enqueue(file_name: str, volume: float = 1.0):
    hint_q.put((file_name, float(volume), "queue"))
    print(f"[HINT] queued file={file_name} vol={volume}")

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
                hint_q.put((file_name, float(volume), "queue"))
                print(f"[HINT] queued file={file_name} vol={volume}")
            else:
                # interrupt: stop current and clear queue, then play immediately (handled in main loop)
                hint_q.put((file_name, float(volume), "interrupt"))
                print(f"[HINT] interrupt request file={file_name} vol={volume}")
        elif cmd == "stop":
            # handled in main loop: stop + clear queue
            hint_q.put(("", 0.0, "stop"))
            print("[HINT] stop request")
        else:
            print("[HINT] unknown cmd:", cmd, data)

def main():
    global hint_channel

    pygame.mixer.init()
    pygame.mixer.set_num_channels(8)
    hint_channel = pygame.mixer.Channel(1)

    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)

    print("[SYSTEM] ready. Listening for MQTT...")
    client.loop_start()

    try:
        while True:
            # Process hint queue ONLY in main thread (pygame-friendly)
            try:
                file_name, vol, mode = hint_q.get_nowait()
            except queue.Empty:
                file_name = None

            if file_name is not None:
                if mode == "stop":
                    hint_stop(clear_queue=True)

                elif mode == "interrupt":
                    hint_stop(clear_queue=True)
                    hint_play_now(file_name, vol)

                elif mode == "queue":
                    # only start next queued hint when channel is free
                    if not hint_channel.get_busy():
                        hint_play_now(file_name, vol)
                    else:
                        # still playing: put it back and check later
                        hint_q.put((file_name, vol, "queue"))
                        time.sleep(0.05)

            time.sleep(0.02)

    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        pygame.mixer.quit()

if __name__ == "__main__":
    main()
