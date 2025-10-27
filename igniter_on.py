import time

from FireBeat.constants import IGNITER_ARM_DELAY
from FireBeat.plc_controller import PLCController
from FireBeat.logger import logger


def main():
    plc = PLCController()

    plc.igniter_arm()
    logger.info(f"Waiting {IGNITER_ARM_DELAY}s for igniter arm delay...")
    time.sleep(IGNITER_ARM_DELAY)
    logger.info("Igniters are hot")


if __name__ == "__main__":
    main()
