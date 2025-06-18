#!/usr/bin/env python3
import serial
import time
import os
import io
from picamera2 import Picamera2
from PIL import Image
from datetime import datetime

# --- Configuration --------------------------------------------------------
SERIAL_PORT        = '/dev/ttyAMA0'
BAUD_RATE          = 115200
FULLRES_DIR        = 'fullres'
DEFAULT_WIDTH      = 1024     # max width of resized image (px)
DEFAULT_QUALITY    = 5        # 1–10 scale => JPEG quality 10–100
ACK_TIMEOUT        = 10       # seconds to wait for each handshake
CHUNK_SIZE         = 256      # bytes per chunk

# Ensure full-resolution directory exists
os.makedirs(FULLRES_DIR, exist_ok=True)

# --- Utility Functions ---------------------------------------------------

def unique_filename(base: str, ext: str, folder: str = None) -> str:
    """Generate a unique filename, optionally inside a given folder."""
    counter = 0
    if folder:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{base}.{ext}")
    else:
        path = f"{base}.{ext}"
    while os.path.exists(path):
        counter += 1
        name = f"{base}_{counter}"
        path = os.path.join(folder, f"{name}.{ext}") if folder else f"{name}.{ext}"
    return path

def capture_and_prepare(width: int, quality_scale: int):
    """
    Capture full-res image, save to disk, then prepare resized JPEG in memory.
    Returns tuple (fullres_path, resized_bytes).
    """
    # Map quality_scale (1-10) to JPEG quality (10-100)
    jpeg_quality = max(1, min(quality_scale, 10)) * 10

    # Initialize camera
    picam2 = Picamera2()
    sensor_res = picam2.sensor_resolution
    config = picam2.create_still_configuration(main={"size": sensor_res})
    picam2.configure(config)
    picam2.start()
    time.sleep(2)  # allow sensor to warm up

    # Timestamp for filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = f"image_{timestamp}_fullres"
    fullres_path = unique_filename(base_name, "jpg", folder=FULLRES_DIR)
    
    # Capture and save full-resolution
    picam2.capture_file(fullres_path)
    picam2.close()
    print(f"Captured full-res: {fullres_path}")

    # Open and resize in memory
    img = Image.open(fullres_path)
    aspect = img.width / img.height if img.height else 1
    new_height = int(width / aspect)
    resized_img = img.resize((width, new_height), Image.LANCZOS)

    # Serialize JPEG into bytes buffer
    buffer = io.BytesIO()
    resized_img.save(buffer, format='JPEG', quality=jpeg_quality, optimize=True)
    data = buffer.getvalue()
    print(f"Prepared resized image: {width}×{new_height}, quality={jpeg_quality}, {len(data)} bytes")

    return fullres_path, data

def wait_for(ser: serial.Serial, expected: str, timeout: int) -> bool:
    """
    Block until `expected` line appears on serial or timeout expires.
    Lines are stripped of whitespace.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = ser.readline().decode(errors='ignore').strip()
        if line == expected:
            return True
    return False

def send_data_via_serial(ser: serial.Serial, data: bytes, dest_name: str) -> bool:
    """
    Send header and then data in chunks, performing handshakes:
      - Header: "<dest_name>|<len_bytes>\\n", expect "READY"
      - For each chunk: send block, expect "ACK"
      - After all: expect "DONE"
    Returns True on success.
    """
    total_len = len(data)
    header = f"{dest_name}|{total_len}\n"
    ser.write(header.encode())
    print(f"Sent header: '{header.strip()}'")

    # Wait for READY
    if not wait_for(ser, "READY", ACK_TIMEOUT):
        print("Error: no READY response from ESP32")
        return False

    # Send in chunks
    offset = 0
    while offset < total_len:
        end = min(offset + CHUNK_SIZE, total_len)
        ser.write(data[offset:end])
        if not wait_for(ser, "ACK", ACK_TIMEOUT):
            print(f"Error: no ACK for chunk starting at {offset}")
            return False
        offset = end
        print(f"Sent {offset}/{total_len} bytes")

    # Wait for final DONE
    if not wait_for(ser, "DONE", ACK_TIMEOUT):
        print("Error: no DONE response (incomplete transfer)")
        return False

    print("Transfer completed successfully")
    return True

# --- Main Loop -----------------------------------------------------------

def main():
    # Open serial port
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    print("Ready. Waiting for 'foto' commands...")

    while True:
        line = ser.readline().decode(errors='ignore').strip()
        if not line:
            continue

        parts = line.split()
        if parts[0].lower() != 'foto':
            continue

        # Parse optional width and quality
        width = DEFAULT_WIDTH
        quality = DEFAULT_QUALITY
        if len(parts) >= 2:
            try:
                width = int(parts[1])
            except ValueError:
                print(f"Invalid width '{parts[1]}', using default {DEFAULT_WIDTH}")
        if len(parts) >= 3:
            try:
                quality = int(parts[2])
            except ValueError:
                print(f"Invalid quality '{parts[2]}', using default {DEFAULT_QUALITY}")

        print(f"Command: foto → width={width}, quality={quality}")

        # Capture and prepare images
        fullres_path, resized_bytes = capture_and_prepare(width, quality)
        unix_ts = str(int(time.time()))

        # Send resized data
        success = send_data_via_serial(ser, resized_bytes, unix_ts)
        if not success:
            print("Error: image transfer failed.")
        else:
            print("Image sent to ESP32 as:", unix_ts + ".jpg")

        print("Awaiting next 'foto' command...\n")

if __name__ == '__main__':
    main()
