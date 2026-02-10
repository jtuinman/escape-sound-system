# Escape Room Hint System (Raspberry Pi)

## Hardware
- Raspberry Pi 4B
- Audio output: 3.5mm analog jack -> external amplifier -> speakers

## Features
- 2 audio tracks simultaneously (MP3):
  - Track 1: background music
  - Track 2: spoken hints (interrupts current hint)
- Video playback support using the same MQTT trigger format (`cmd` + `file`):
  - If `file` has a video extension (`.mp4`, `.mkv`, `.mov`, `.avi`, `.webm`, `.m4v`), it is played with an external video player.
  - BG video loops until `stop`/`switch`.
  - Hint video plays once and then BG volume is restored.
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
sudo apt install -y mosquitto mosquitto-clients python3-pygame python3-paho-mqtt mpv
sudo systemctl enable --now mosquitto
```

Optional video section in `config/config.json`:
```json
"video": {
  "base_path": "/home/pi/escape-sound-system/video",
  "extensions": [".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"],
  "mode": "auto",
  "hdmi_connector": "HDMI-A-1",
  "player_cmd": ["mpv", "--fs", "--no-terminal", "--really-quiet"]
}
```

HDMI output behavior:
- In `mode: "auto"`, the player uses desktop display output when a graphical session exists.
- In `mode: "auto"` without a graphical session, it targets HDMI via DRM/KMS (`--gpu-context=drm`), default connector `HDMI-A-1`.
- Set `mode: "drm"` to force HDMI/DRM mode.
- Set `mode: "x11"` to force desktop-session output.
- If your monitor is on a different connector, set `hdmi_connector` (for example `HDMI-A-2`).

## Web Interface (Shutdown)
- Script: `scripts/web_interface.py`
- Serves a simple page with a `Shutdown Pi` button.
- Uses browser confirmation popup before sending shutdown request.
- Endpoint: `POST /api/shutdown` with body `{"confirm": true}`
- Shutdown command is executed after HTTP response for graceful poweroff.

Run manually:
```bash
python3 /home/pi/escape-sound-system/scripts/web_interface.py
```

Optional environment variables:
- `ESCAPE_WEB_HOST` (default: `0.0.0.0`)
- `ESCAPE_WEB_PORT` (default: `8000`)

Note:
- The process must have permission to run `systemctl poweroff` (typically run as root or with suitable sudo/systemd configuration).

### Auto-start on boot (systemd)
Unit file in repo:
- `config/systemd/escape-web-interface.service`

Install and enable on Pi:
```bash
sudo cp /home/pi/escape-sound-system/config/systemd/escape-web-interface.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now escape-web-interface.service
sudo systemctl status escape-web-interface.service
```
