import configparser
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.ini"

config = configparser.ConfigParser()
config.read(CONFIG_PATH)

ZIP_PATH = config.get("paths", "zip_path")
NON_MAP_DAT_FILES = config.get("paths", "non_map_dat_files")

PLC_IP = config.get("plc", "ip")
PLC_PORT = config.getint("plc", "port")

PUMPKIN_COILS = {
    0: config.getint("modbus_ports", "pumpkin_0"),
    1: config.getint("modbus_ports", "pumpkin_1"),
    2: config.getint("modbus_ports", "pumpkin_2"),
    3: config.getint("modbus_ports", "pumpkin_3"),
}
IGNITER_SHUTOFF = config.getint("modbus_ports", "igniter_shutoff")

NOTE_DURATION = config.getfloat("mapping", "note_duration")
IGNITER_ARM_DELAY = config.getfloat("mapping", "igniter_arm_delay")

LATENCY_COMPENSATION_OFFSET = config.getfloat("playback", "latency_compensation_offset")




