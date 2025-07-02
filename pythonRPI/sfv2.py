#!/usr/bin/env python3
"""
Image Capture & Transfer Pipeline for Raspberry Pi ↔ ESP32
----------------------------------------------------------
This script listens on a UART interface for a 'foto' command (with optional width and quality) from an ESP32.
When invoked, it:
 1. Captures a full-resolution JPEG using the Pi Camera.
 2. Resizes the image to a specified width and quality in memory.
 3. Sends the resized JPEG over UART using a chunked handshake:
    - SEND header: "<timestamp>|<size>\n" → expect "READY"
    - For each chunk: send bytes → expect "ACK"
    - After all chunks: expect "DONE"
Added:
 - Debug output of selected serial port.
 - Filename sent over serial is only the timestamp.
 - After opening serial port, waits 1 s and sends "ready".
 - Saves both the raw full-resolution and the processed resized images to disk.
"""
import serial
import time
import os
import glob
import sys
import io
from picamera2 import Picamera2
from PIL import Image
from datetime import datetime
import argparse

# --- Configuration --------------------------------------------------------
BAUD_RATE       = 115200
DEFAULT_WIDTH   = 1024
DEFAULT_QUALITY = 5
ACK_TIMEOUT     = 10      # seconds
CHUNK_SIZE      = 256     # bytes per chunk
FULLRES_DIR     = 'fullres'
PROCESSED_DIR   = 'processed'

# Ensure directories exist
os.makedirs(FULLRES_DIR, exist_ok=True)
os.makedirs(PROCESSED_DIR, exist_ok=True)

# --- Utility Functions ---------------------------------------------------
def unique_filename(base: str, ext: str, folder: str) -> str:
    """Return a unique filepath in `folder` for base.ext."""
    counter = 0
    path = os.path.join(folder, f"{base}.{ext}")
    while os.path.exists(path):
        counter += 1
        path = os.path.join(folder, f"{base}_{counter}.{ext}")
    return path


def find_serial_port() -> str:
    """
    Detect available serial port: /dev/serial0, then /dev/ttyUSB*, then /dev/ttyACM*.
    Returns port or None.
    """
    candidates = []
    if os.path.exists('/dev/serial0'):
        candidates.append('/dev/serial0')
    candidates.extend(sorted(glob.glob('/dev/ttyUSB*')))
    candidates.extend(sorted(glob.glob('/dev/ttyACM*')))

    for port in candidates:
        try:
            ser = serial.Serial(port, BAUD_RATE, timeout=0.1)
            ser.close()
            print(f"[DEBUG] Selected serial port: {port}")
            return port
        except Exception:
            continue

    print("[ERROR] No available serial port found.")
    return None


def wait_for(ser: serial.Serial, expected: str, timeout: int) -> bool:
    """
    Block until `expected` appears (line) on serial or timeout.
    """
    deadline = time.time() + timeout
    ser.reset_input_buffer()
    while time.time() < deadline:
        try:
            line = ser.readline().decode(errors='ignore').strip()
        except Exception:
            continue
        if line:
            print(f"[DEBUG] Received '{line}'")
        if line == expected:
            return True
    return False


def send_data_via_serial(ser: serial.Serial, data: bytes, timestamp: str) -> bool:
    """
    Chunked transfer with handshake:
      header: "<timestamp>|<len>\n" → READY
      each CHUNK → ACK
      DONE at end
    """
    total = len(data)
    header = f"{timestamp}|{total}\n"
    ser.write(header.encode())
    print(f"[DEBUG] Sent header: {header.strip()}")

    if not wait_for(ser, 'READY', ACK_TIMEOUT):
        print("[ERROR] No READY")
        return False

    offset = 0
    while offset < total:
        end = min(offset + CHUNK_SIZE, total)
        ser.write(data[offset:end])
        if not wait_for(ser, 'ACK', ACK_TIMEOUT):
            print(f"[ERROR] No ACK at {offset}")
            return False
        offset = end
        print(f"[DEBUG] Sent {offset}/{total} bytes")

    if not wait_for(ser, 'DONE', ACK_TIMEOUT):
        print("[ERROR] No DONE")
        return False

    print("[INFO] Transfer complete")
    return True


def capture_and_prepare(width: int, quality: int) -> (str, bytes):
    """
    Capture full-resolution JPEG, save to disk, then resize & re-encode in memory.
    Returns (timestamp, jpeg_bytes).
    """
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    full_base = f"{timestamp}_fullres"
    full_path = unique_filename(full_base, 'jpg', FULLRES_DIR)

    # Capture full-resolution
    picam2 = Picamera2()
    cfg = picam2.create_still_configuration(main={'size': picam2.sensor_resolution})
    picam2.configure(cfg)
    picam2.start()
    time.sleep(2)
    picam2.capture_file(full_path)
    picam2.close()
    print(f"[INFO] Captured full-resolution image: {full_path}")

    # Resize & encode
    img = Image.open(full_path)
    aspect = img.width / img.height if img.height else 1
    new_height = max(1, int(width / aspect))
    resized = img.resize((width, new_height), Image.LANCZOS)
    buf = io.BytesIO()
    q = max(1, min(quality, 10)) * 10
    resized.save(buf, format='JPEG', quality=q, optimize=True)
    size_bytes = buf.getbuffer().nbytes
    print(f"[DEBUG] Prepared resize: {width}×{new_height}, quality={q}, {size_bytes} bytes")

    # Save processed image
    proc_path = unique_filename(timestamp, 'jpg', PROCESSED_DIR)
    with open(proc_path, 'wb') as f:
        f.write(buf.getvalue())
    print(f"[INFO] Saved processed image: {proc_path}")

    return timestamp, buf.getvalue()

# --- Main Loop -----------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='Capture and send JPEG via UART')
    parser.add_argument('--port', help='Serial port override', default=None)
    parser.add_argument('--baud', help='Baud rate', type=int, default=BAUD_RATE)
    args = parser.parse_args()

    port = args.port or find_serial_port()
    if not port:
        sys.exit(1)

    try:
        ser = serial.Serial(port, args.baud, timeout=1)
        ser.reset_input_buffer(); ser.reset_output_buffer()
        print(f"[INFO] Opened serial port {port} at {args.baud} baud")

        # Notify ready after opening
        time.sleep(1)
        ser.write(b"ready\n")
        print("[DEBUG] Sent 'ready' on serial")

        print(f"[INFO] Listening on {port}@{args.baud}, awaiting 'foto'...")

        while True:
            line = ser.readline().decode(errors='ignore').strip()
            if not line:
                continue
            parts = line.split()
            if parts[0].lower() != 'foto':
                print(f"[WARN] Unknown cmd: '{line}'")
                continue

            # Parse optional width/quality
            w = DEFAULT_WIDTH
            q = DEFAULT_QUALITY
            if len(parts) >= 2:
                try:
                    w = int(parts[1])
                except ValueError:
                    print(f"[WARN] Invalid width '{parts[1]}'; using {DEFAULT_WIDTH}")
            if len(parts) >= 3:
                try:
                    q = int(parts[2])
                except ValueError:
                    print(f"[WARN] Invalid quality '{parts[2]}'; using {DEFAULT_QUALITY}")

            print(f"[INFO] Command: foto → width={w}, quality={q}")
            timestamp, data = capture_and_prepare(w, q)

            if not send_data_via_serial(ser, data, timestamp):
                print("[ERROR] Transfer failed.")
            else:
                print(f"[INFO] Sent as: {timestamp}.jpg")
            print("[INFO] Awaiting next 'foto'...\n")

    except serial.SerialException as e:
        print(f"[ERROR] Serial error: {e}")
        sys.exit(1)
    finally:
        if 'ser' in locals() and ser.is_open:
            ser.close()

if __name__ == '__main__':
    main()
