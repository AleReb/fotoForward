#!/usr/bin/env python3
"""
Image Transfer Pipeline for Raspberry Pi & ESP32
------------------------------------------------
This script listens on a UART interface for a 'foto' command from an ESP32 module.
When invoked, it performs the following steps:

1. Capture a raw full-resolution image using the Pi Camera.
2. Enhance the image by applying:
   - Unsharp Mask for sharpening.
   - CLAHE (Contrast Limited Adaptive Histogram Equalization) for local contrast.
3. Save both the raw and enhanced full-resolution images to disk.
4. Resize the enhanced image to a configurable width.
5. Send the resized image over UART in fixed-size chunks with a handshake protocol:
   - SEND header "<filename>|<size>\n", wait for "READY"
   - Transmit each chunk, wait for "ACK"
   - After all chunks, wait for "DONE"
6. The ESP32 saves the incoming data as a JPEG file.

Usage:
    python3 capture_and_send.py --port /dev/ttyAMA0 --baud 115200
"""

import serial
import time
import os
import io
from picamera2 import Picamera2
from PIL import Image, ImageFilter
import numpy as np
import cv2
from datetime import datetime

# --- Configuration --------------------------------------------------------
SERIAL_PORT        = '/dev/ttyAMA0'
BAUD_RATE          = 115200
RAW_DIR            = 'fullres'
ENHANCED_DIR       = 'enhanced'
DEFAULT_WIDTH      = 1024     # resized max width (px)
DEFAULT_QUALITY    = 5        # 1–10 scale → JPEG quality 10–100
ACK_TIMEOUT        = 10       # seconds to wait per handshake
CHUNK_SIZE         = 256      # bytes per chunk

# Ensure directories exist
os.makedirs(RAW_DIR, exist_ok=True)
os.makedirs(ENHANCED_DIR, exist_ok=True)

# --- Utility Functions ---------------------------------------------------

def unique_filename(base: str, ext: str, folder: str) -> str:
    """Generate a unique filename in given folder."""
    counter = 0
    path = os.path.join(folder, f"{base}.{ext}")
    while os.path.exists(path):
        counter += 1
        path = os.path.join(folder, f"{base}_{counter}.{ext}")
    return path


def wait_for(ser: serial.Serial, expected: str, timeout: int) -> bool:
    """Block until `expected` line arrives on serial or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = ser.readline().decode(errors='ignore').strip()
        if line == expected:
            return True
    return False


def send_data_via_serial(ser: serial.Serial, data: bytes, dest_name: str) -> bool:
    """
    Send header and data in chunks with handshakes:
      - Header: "<dest_name>|<len>\n" → expect "READY"
      - Each chunk → expect "ACK"
      - Final → expect "DONE"
    """
    total_len = len(data)
    header = f"{dest_name}|{total_len}\n"
    ser.write(header.encode())
    print(f"Sent header: '{header.strip()}'")

    if not wait_for(ser, "READY", ACK_TIMEOUT):
        print("Error: no READY from ESP32")
        return False

    sent = 0
    while sent < total_len:
        end = min(sent + CHUNK_SIZE, total_len)
        ser.write(data[sent:end])
        if not wait_for(ser, "ACK", ACK_TIMEOUT):
            print(f"Error: no ACK for chunk at {sent}")
            return False
        sent = end
        print(f"Sent {sent}/{total_len} bytes")

    if not wait_for(ser, "DONE", ACK_TIMEOUT):
        print("Error: no DONE from ESP32")
        return False

    print("Transfer completed successfully")
    return True

# --- Image Processing Functions ------------------------------------------

def capture_raw_image() -> str:
    """Capture and save a raw full-resolution JPEG. Returns filepath."""
    picam2 = Picamera2()
    cfg = picam2.create_still_configuration(main={"size": picam2.sensor_resolution})
    picam2.configure(cfg)
    picam2.start()
    time.sleep(2)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{timestamp}_raw"
    path = unique_filename(base, "jpg", RAW_DIR)
    picam2.capture_file(path)
    picam2.close()
    print(f"Saved raw image: {path}")
    return path


def enhance_image(raw_path: str) -> (Image.Image, str):
    """Load raw image, apply unsharp mask and CLAHE, save enhanced full-res, return PIL and path."""
    img = Image.open(raw_path)
    # Unsharp Mask
    img = img.filter(ImageFilter.UnsharpMask(radius=1.5, percent=150, threshold=3))
    # CLAHE
    arr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    lab = cv2.cvtColor(arr, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8,8))
    cl = clahe.apply(l)
    merged = cv2.merge((cl, a, b))
    rgb = cv2.cvtColor(merged, cv2.COLOR_LAB2RGB)
    enhanced = Image.fromarray(rgb)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{timestamp}_en"
    path = unique_filename(base, "jpg", ENHANCED_DIR)
    enhanced.save(path, format='JPEG', quality=95, optimize=True)
    print(f"Saved enhanced image: {path}")
    return enhanced, path

# --- Main Loop -----------------------------------------------------------

def main():
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    print("Ready. Awaiting 'foto' command...")

    while True:
        line = ser.readline().decode(errors='ignore').strip()
        if not line:
            continue
        parts = line.split()
        if parts[0].lower() != 'foto':
            continue

        print("Received 'foto' command")
        raw_path = capture_raw_image()
        enhanced_img, enhanced_path = enhance_image(raw_path)

        # Resize for transfer
        aspect = enhanced_img.width / enhanced_img.height if enhanced_img.height else 1
        new_height = int(DEFAULT_WIDTH / aspect)
        resized = enhanced_img.resize((DEFAULT_WIDTH, new_height), Image.LANCZOS)
        buf = io.BytesIO()
        jpeg_quality = max(1, min(DEFAULT_QUALITY, 10)) * 10
        resized.save(buf, format='JPEG', quality=jpeg_quality, optimize=True)
        data = buf.getvalue()

        # Send over serial
        dest_name = os.path.splitext(os.path.basename(enhanced_path))[0]
        success = send_data_via_serial(ser, data, dest_name)
        if not success:
            print("Error: transfer failed.")
        print("Awaiting next 'foto' command...\n")

if __name__ == '__main__':
    main()
