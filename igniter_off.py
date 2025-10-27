from FireBeat.plc_controller import PLCController
from FireBeat.logger import logger


def main():
    plc = PLCController()

    plc.igniter_disarm()
    logger.info("Igniters are disarmed")


if __name__ == "__main__":
    main()
