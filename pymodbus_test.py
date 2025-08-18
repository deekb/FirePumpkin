#!/usr/bin/env python3
from pymodbus.client import ModbusTcpClient
import time

# PLC connection settings
PLC_IP = "192.168.1.2"   # Replace with your Micro850's IP address
PLC_PORT = 502            # Default Modbus TCP port

PUMPKIN_1 = 0
PUMPKIN_2 = 1
PUMPKIN_3 = 2
PUMPKIN_4 = 3

IGNITER_SHUTOFF = 4

client = ModbusTcpClient(PLC_IP, port=PLC_PORT)

if not client.connect():
    print("Failed to connect to PLC")
    exit()

def set_coil(coil, state):
    result = client.write_coil(coil, state)
    if result.isError():
        print(f"Error setting coil {coil} to {state}")
    else:
        print(f"Coil {coil} set to {state}")

def main():
    # set_coil(IGNITER_SHUTOFF, False)
    set_coil(PUMPKIN_1, True)
    set_coil(PUMPKIN_2, True)
    set_coil(PUMPKIN_3, True)
    set_coil(PUMPKIN_4, True)
    time.sleep(1)
    set_coil(PUMPKIN_1, False)
    set_coil(PUMPKIN_2, False)
    set_coil(PUMPKIN_3, False)
    set_coil(PUMPKIN_4, False)


if __name__ == "__main__":
    try:
        main()
    finally:
        client.close()