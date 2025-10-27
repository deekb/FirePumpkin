import time
from pymodbus.client import ModbusTcpClient
from FireBeat.constants import PLC_IP, PLC_PORT, PUMPKIN_COILS, IGNITER_SHUTOFF
from FireBeat.logger import logger
import threading
import pygame

class PLCController:
    def __init__(self, is_dryrun=False):
        logger.info(f"Connecting to PLC at {PLC_IP}:{PLC_PORT}")
        self.is_dryrun = is_dryrun
        if not self.is_dryrun:
            self.client = ModbusTcpClient(PLC_IP, port=PLC_PORT)
            if not self.client.connect():
                logger.critical("Failed to connect to PLC.")
                raise ConnectionError(f"Failed to connect to PLC at {PLC_IP}:{PLC_PORT}")
            logger.info("PLC connected successfully.")
        else:
            logger.warning("Not connecting to PLC, is_dryrun is True.")

    def set_coil(self, coil, state):
        if not self.is_dryrun:
            result = self.client.write_coil(coil, state)
            if result.isError():
                logger.warning(f"Error setting coil {coil} to {state}")
            else:
                logger.debug(f"Coil {coil} set to {state}")
        else:
            logger.debug(f"Coil {coil} set to {state}")

    def igniter_arm(self):
        self.set_coil(IGNITER_SHUTOFF, False)
        logger.warning("Arming igniter.")

    def igniter_disarm(self):
        self.set_coil(IGNITER_SHUTOFF, True)
        logger.warning("Disarming igniter.")

    def run_schedule(self, schedule, stream, start_time):
        logger.info("Starting PLC firing schedule.")
        for pulse in schedule:
            logger.debug(f"Pulse: {pulse}")
            coil = PUMPKIN_COILS.get(pulse["pumpkin"])
            if coil is None:
                logger.warning(f"Skipping pulse with invalid coil mapping: {pulse}")
                continue

            start_abs = start_time + pulse["start"]
            end_abs = start_time + pulse["end"]

            time.sleep(max(0, start_abs - stream.time))
            self.set_coil(coil, True)
            time.sleep(max(0, end_abs - stream.time))
            self.set_coil(coil, False)
        logger.info("Schedule complete.")

    def close(self):
        if not self.is_dryrun:
            self.client.close()
            logger.info("PLC connection closed.")
        else:
            logger.info("Not connected to PLC, is_dryrun is True.")

    def __exit__(self, exc_type, exc_value, traceback):
        logger.info("Shutting valves before disconnect.")
        for coil in PUMPKIN_COILS:
            self.set_coil(coil, False)
        self.close()

    def __enter__(self):
        return self


class PygamePLCController:
    """Simulated PLC controller using pygame to visualize four pumpkin states with overlapping notes."""

    def __init__(self):
        logger.info("Initializing PygamePLCController.")
        # Thread-safe coil states
        self.coil_states = {coil: False for coil in PUMPKIN_COILS.values()}
        self.lock = threading.Lock()
        self.running = True

    def set_coil(self, coil, state):
        with self.lock:
            if coil in self.coil_states:
                self.coil_states[coil] = state
                logger.debug(f"Set simulated coil {coil} to {state}")
            else:
                logger.warning(f"Attempted to set unknown coil {coil}")

    def igniter_disarm(self):
        self.set_coil(IGNITER_SHUTOFF, True)
        logger.warning("Disarming igniter.")

    def igniter_arm(self):
        self.set_coil(IGNITER_SHUTOFF, False)
        logger.warning("Arming igniter.")

    def run_schedule(self, schedule, stream, start_time):
        """Run the schedule in real-time, supporting overlapping notes."""
        logger.info("Starting simulated PLC schedule.")

        # Build a list of events: (time, coil, state)
        events = []
        for pulse in schedule:
            coil = PUMPKIN_COILS.get(pulse["pumpkin"])
            if coil is None:
                logger.warning(f"Skipping pulse with invalid coil mapping: {pulse}")
                continue
            events.append((start_time + pulse["start"], coil, True))   # turn on
            events.append((start_time + pulse["end"], coil, False))    # turn off

        # Sort events by time
        events.sort(key=lambda e: e[0])

        idx = 0
        while idx < len(events) and self.running:
            now = stream.time
            event_time, coil, state = events[idx]
            if now >= event_time:
                self.set_coil(coil, state)
                idx += 1

        logger.info("Simulated schedule complete.")

    def run_pygame_loop(self):
        """Run this method in the main thread to display the pumpkin states."""
        pygame.init()
        width, height = 400, 100
        screen = pygame.display.set_mode((width, height))
        pygame.display.set_caption("Pumpkin PLC Visualization")
        clock = pygame.time.Clock()
        block_width = width // 4
        block_height = height

        while self.running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False

            screen.fill((0, 0, 0))
            with self.lock:
                for i, coil in enumerate(sorted(self.coil_states.keys())):
                    color = (255, 255, 255) if self.coil_states[coil] else (0, 0, 0)
                    pygame.draw.rect(screen, color, (i*block_width, 0, block_width, block_height))
                    pygame.draw.rect(screen, (128,128,128), (i*block_width, 0, block_width, block_height), 2)  # border

            pygame.display.flip()
            clock.tick(60)

        pygame.quit()

    def close(self):
        """Stop the pygame loop."""
        logger.info("Closing PygamePLCController.")
        self.running = False
