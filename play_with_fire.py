#!/usr/bin/env python3
import json
import time
import threading
from pymodbus.client import ModbusTcpClient
import sounddevice as sd
import soundfile as sf

# ---------------- PLC connection settings ----------------
PLC_IP = "192.168.1.2"   # Replace with your Micro850's IP address
PLC_PORT = 502           # Default Modbus TCP port

# Coils mapping
PUMPKIN_COILS = {
    0: 0,   # Pumpkin 1
    1: 1,   # Pumpkin 2
    2: 2,   # Pumpkin 3
    3: 3    # Pumpkin 4
}
IGNITER_SHUTOFF = 4

# ---------------- Input files ----------------
SCHEDULE_FILE = "Thunderstruck.json"
AUDIO_FILE = "Thunderstruck.ogg"   # must match what you exported

# ---------------- Modbus helpers ----------------
client = ModbusTcpClient(PLC_IP, port=PLC_PORT)

if not client.connect():
    print("Failed to connect to PLC")
    exit()

def set_coil(coil, state):
    """Write a coil and print result."""
    result = client.write_coil(coil, state)
    if result.isError():
        print(f"Error setting coil {coil} to {state}")
    else:
        print(f"Coil {coil} set to {state}")


# ---------------- Pulse worker ----------------
def pulse_worker(schedule, stream, stream_start_time):
    """Schedules coil pulses in sync with audio stream clock."""
    for pulse in schedule:
        coil = PUMPKIN_COILS.get(pulse["pumpkin"])
        if coil is None:
            continue

        start = pulse["start"]
        end = pulse["end"]

        # Convert to absolute audio clock times
        start_abs = stream_start_time + start
        end_abs = stream_start_time + end

        # Sleep until ON
        now = stream.time
        delay = max(0, start_abs - now)
        time.sleep(delay)
        set_coil(coil, True)

        # Sleep until OFF
        now = stream.time
        delay = max(0, end_abs - now)
        time.sleep(delay)
        set_coil(coil, False)



def main():
    # Load pulse schedule
    with open(SCHEDULE_FILE, "r") as f:
        schedule = json.load(f)

    data, samplerate = sf.read(AUDIO_FILE, dtype="float32")

    # Create an output stream so we can use its clock
    with sd.OutputStream(samplerate=samplerate, channels=data.shape[1] if data.ndim > 1 else 1) as stream:
        # Start time according to audio stream clock
        stream_start_time = stream.time

        # Start pulse thread
        t = threading.Thread(target=pulse_worker, args=(schedule, stream, stream_start_time))
        t.start()

        # Play audio through the same stream
        stream.write(data)

        t.join()
        print("Show complete.")


if __name__ == "__main__":
    try:
        main()
    finally:
        set_coil(0, False)
        set_coil(1, False)
        set_coil(2, False)
        set_coil(3, False)
        client.close()
