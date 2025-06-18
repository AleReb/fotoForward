# ESP32–Raspberry Pi Image Transfer System

This project captures images on a Raspberry Pi and sends them via UART to an ESP32.  
The ESP32 stores each image on an SD card and then posts it to a server over LTE (SIM7600).

## Components

### Raspberry Pi (`capture_and_send.py`)
* Waits for `foto` on UART → captures a full‑res JPEG.
* Enhances the picture (Unsharp Mask + CLAHE).
* Saves RAW and enhanced versions.
* Resizes enhanced image and sends it to the ESP32 in 256‑byte chunks with `READY/ACK/DONE` handshakes.

### ESP32 (`Unified_SIM7600_SD_Image_Manager.ino`)
* Receives the chunks, writes them to SD.
* Optionally retries once on timeout.
* Posts the stored JPEG to `WEBHOOK_URL` via SIM7600.
* Displays modem status on a 128×128 OLED.

### Server (`server_handler.py`)
A minimal Flask endpoint that accepts `POST /agregarImagen` and writes the body to `uploads/<filename>`.

## Quick Start

```bash
# On Raspberry Pi
sudo apt install python3-picamera2 python3-opencv python3-pil python3-serial
python3 capture_and_send.py --port /dev/ttyAMA0 --baud 115200
```

Compile the `.ino` sketch in Arduino IDE, wire TX/RX between Pi and ESP32, and start sending `foto`.
