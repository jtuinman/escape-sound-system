#!/usr/bin/env python3
import subprocess
import socket
import time
import sys

CHECK_INTERVAL = 1.0   # seconds
TIMEOUT = 120          # seconds max wait

def wait_for_network():
    # simpele check: kan localhost resolven + socket openen
    try:
        socket.gethostbyname("localhost")
        return True
    except Exception:
        return False

def wait_for_mosquitto():
    try:
        subprocess.check_call(
            ["systemctl", "is-active", "--quiet", "mosquitto"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except subprocess.CalledProcessError:
        return False

def wait_for_audio():
    try:
        out = subprocess.check_output(["aplay", "-l"], stderr=subprocess.DEVNULL).decode()
        return "card" in out.lower()
    except Exception:
        return False

def main():
    start = time.time()
    print("[WAIT] waiting for system readiness...")

    while True:
        net_ok = wait_for_network()
        mosq_ok = wait_for_mosquitto()
        audio_ok = wait_for_audio()

        if net_ok and mosq_ok and audio_ok:
            print("[WAIT] network, mosquitto and audio are ready")
            return 0

        if time.time() - start > TIMEOUT:
            print("[WAIT] timeout waiting for system readiness", file=sys.stderr)
            return 1

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    sys.exit(main())
