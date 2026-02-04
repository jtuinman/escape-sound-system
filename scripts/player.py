#!/usr/bin/env python3
import json
import os
import time
import pygame
import paho.mqtt.client as mqtt

BASE = "/home/pi/escape-sound-system"
AUDIO_DIR = os.path.join(BASE, "audio")

MQTT_HOST = "localhost"
MQTT_PORT = 1883

TOPIC_BG = "escape/audio/bg"      # background music (track 1)
TOPIC_HINT = "escape/audio/hint"  # spoken hints (track 2)

def audio_path(name: str) -> str:
    # allow "bg.mp3" etc; block path traversal
    name = name.strip().lstrip("/").replace("..", "")
    return os.path.join(AUDIO_DIR, name)

def load_bg(file_name: str, volume: float = 0.5, loop: bool = True):
    path = audio_path(file_name)
    if not os.path.isfile(path):
        print(f"[BG] file not found: {path}")
        return
    pygame.mixer.music.load(path)
    pygame.mixer.music.set_volume(max(0.0, min(1.0, volume)))
    pygame.mixer.music.play(-1 if loop else 0)
    print(f"[BG] playing: {file_name} vol={volume} loop={loop}")

def stop_bg(fade_ms: int = 0):
    if fade_ms > 0:
        pygame.mixer.music.fadeout(fade_ms)
    else:
        pygame.mixer.music.stop()
    print(f"[BG] stopped fade_ms={fade_ms}")

def play_hint(file_name: str, volume: float = 1.0):
    path = audio_path(file_name)
    if not os.path.isfile(path):
        print(f"[HINT] file not found: {path}")
        return
    snd = pygame.mixer.Sound(path)
    snd.set_volume(max(0.0, min(1.0, volume)))
    hint_channel.play(snd)
    print(f"[HINT] playing: {file_name} vol={volume}")

def stop_hint():
    hint_channel.stop()
    print("[HINT] stopped")

def parse_payload(payload: bytes):
    s = payload.decode("utf-8", errors="ignore").strip()
    if not s:
        return {}
    if s.startswith("{"):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return {"raw": s}
    # allow simple strings like: "start bg.mp3"
    return {"raw": s}

def on_connect(client, userdata, flags, rc):
    print("[MQTT] connected rc=", rc)
    client.subscribe([(TOPIC_BG, 0), (TOPIC_HINT, 0)])

def on_message(client, userdata, msg):
    data = parse_payload(msg.payload)
    topic = msg.topic

    # Simple string commands
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

    if topic == TOPIC_BG:
        if cmd in ("start", "play"):
            if not file_name:
                print("[BG] missing file")
                return
            load_bg(file_name, volume=volume, loop=loop)
        elif cmd in ("stop", "pause"):
            stop_bg(fade_ms=fade_ms)
        elif cmd == "volume":
            pygame.mixer.music.set_volume(max(0.0, min(1.0, volume)))
            print(f"[BG] volume set {volume}")
        else:
            print("[BG] unknown cmd:", cmd, data)

    elif topic == TOPIC_HINT:
        if cmd in ("play", "start"):
            if not file_name:
                print("[HINT] missing file")
                return
            play_hint(file_name, volume=volume)
        elif cmd == "stop":
            stop_hint()
        else:
            print("[HINT] unknown cmd:", cmd, data)

def main():
    global hint_channel

    pygame.mixer.init()
    pygame.mixer.set_num_channels(8)
    hint_channel = pygame.mixer.Channel(1)  # dedicated channel for hints

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
        client.loop_stop()
        pygame.mixer.quit()

if __name__ == "__main__":
    main()
