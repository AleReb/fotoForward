import serial
import threading
import sys

SERIAL_PORT = '/dev/ttyAMA0'  # Or '/dev/ttyUSB0' if needed
BAUD_RATE = 115200

def read_serial(ser):
    while True:
        if ser.in_waiting > 0:
            data = ser.read(ser.in_waiting)
            try:
                print(data.decode(errors='replace'), end='')
            except:
                print(data)

def write_serial(ser):
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break
            ser.write(line.encode())
        except KeyboardInterrupt:
            break

try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    print(f"Opened {SERIAL_PORT} at {BAUD_RATE} bps.")
except Exception as e:
    print(f"Could not open the port: {e}")
    exit(1)

read_thread = threading.Thread(target=read_serial, args=(ser,), daemon=True)
read_thread.start()

try:
    write_serial(ser)
except KeyboardInterrupt:
    print("\nExiting program.")
finally:
    ser.close()
