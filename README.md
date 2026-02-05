# Escape Room Hint System (Raspberry Pi)

## Hardware
- Raspberry Pi 4B
- Audio output: 3.5mm analog jack -> external amplifier -> speakers

## Features
- 2 audio tracks simultaneously (MP3):
  - Track 1: background music
  - Track 2: spoken hints (interrupts current hint)
- Ducking:
  - Default BG volume: 70%
  - Default Hint volume: 70%
  - During hint: BG fades to 30% (500ms)
  - After hint: BG fades back to 70% (500ms)
- Background switch:
  - Fade BG out (500ms), switch file, fade in (500ms)
  - No crossfade / no overlap
- MQTT control (broker runs on the sound-Pi)
- Auto-start on boot via systemd
- Config stored in repo: `config/config.json`
- Logs via `journalctl`

## Install
```bash
sudo apt update
sudo apt install -y mosquitto mosquitto-clients python3-pygame python3-paho-mqtt
sudo systemctl enable --now mosquitto
