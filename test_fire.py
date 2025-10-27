import time

from FireBeat.constants import IGNITER_ARM_DELAY, PUMPKIN_COILS
from FireBeat.plc_controller import PLCController
from FireBeat.logger import logger


def main():
    plc = PLCController()

    plc.igniter_arm()
    logger.info(f"Waiting {IGNITER_ARM_DELAY}s for igniter arm delay...")
    time.sleep(IGNITER_ARM_DELAY)
    logger.info("Igniters are hot, testing fire")

    for coil in PUMPKIN_COILS:
        plc.set_coil(coil, True)
        time.sleep(0.25)
        plc.set_coil(coil, False)
        time.sleep(1)

    for coil in PUMPKIN_COILS:
        plc.set_coil(coil, True)

    time.sleep(0.5)

    for coil in PUMPKIN_COILS:
        plc.set_coil(coil, False)

    plc.igniter_disarm()
    logger.info("Igniters are disarmed")


if __name__ == "__main__":
    main()
