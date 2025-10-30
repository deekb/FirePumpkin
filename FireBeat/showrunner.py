import json
import time
import zipfile
import threading
from pathlib import Path
from typing import Callable, Optional

from FireBeat.map_reader.reader_factory import BeatSaberReaderFactory
from FireBeat.soundplayer import play_audio
from FireBeat.plc_controller import PLCController, PygamePLCController
from FireBeat.constants import (
    NOTE_DURATION,
    LATENCY_COMPENSATION_OFFSET,
    IGNITER_ARM_DELAY,
)
from FireBeat.logger import logger


def run_blocking_action(
    zip_path: Path,
    stop_event: threading.Event,
    set_status: Callable[[str, str], None],
    map_file: Optional[str],
    method: str = "normal",
    is_dryrun: bool = True,
):
    """
    Runs the FireBeat sequence for a given ZIP and map file.

    - map_file must be explicitly provided (no guessing).
    - Audio playback and PLC schedule are run in separate threads.
    - stop_event lets the Flask control page stop the run gracefully.
    - In dryrun, shows a pygame window via PygamePLCController.
    """
    try:
        set_status("running", f"Opening archive: {zip_path.name}")

        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()

            if not map_file or map_file not in names:
                raise FileNotFoundError(f"Map file '{map_file}' not found in archive")

            info_name = "Info.dat" if "Info.dat" in names else ("info.dat" if "info.dat" in names else None)
            if not info_name:
                raise FileNotFoundError("Info.dat/info.dat missing from archive")
            info_data = json.load(zf.open(info_name))

            bpm = info_data.get("_beatsPerMinute") or info_data.get("beatsPerMinute")
            song_file = info_data.get("_songFilename") or info_data.get("songFilename")
            if not song_file or song_file not in names:
                raise FileNotFoundError(f"Song file '{song_file}' not found in archive")

            logger.info("Selected map file: %s", map_file)

            reader = BeatSaberReaderFactory.create_reader_from_mapfile(zf.open(map_file))
            map_data = reader.load_map(zf, map_file)
            schedule = reader.extract_notes(map_data, bpm, NOTE_DURATION, method=method)
            if not schedule:
                raise RuntimeError("No notes detected — check map/difficulty")

            # -------- DRYRUN (pygame UI) --------
            if is_dryrun:
                plc = PygamePLCController()  # no context manager support
                pygame_thread = None
                try:
                    # Start pygame loop in a background thread so we can still monitor stop_event
                    if hasattr(plc, "run_pygame_loop"):
                        pygame_thread = threading.Thread(
                            target=plc.run_pygame_loop, name="pygame-ui", daemon=True
                        )
                        pygame_thread.start()

                    stream, start_time = play_audio(zf, song_file)

                    def _run_schedule():
                        plc.run_schedule(schedule, stream, start_time + LATENCY_COMPENSATION_OFFSET)

                    thr = threading.Thread(
                        target=_run_schedule, name=f"schedule-{zip_path.name}", daemon=True
                    )
                    thr.start()
                    set_status("running", f"Playback started — map: {map_file}")

                    while thr.is_alive():
                        if stop_event.is_set():
                            # cooperative stop
                            for method in ("request_stop", "stop", "abort", "close"):
                                m = getattr(plc, method, None)
                                if callable(m):
                                    try:
                                        m()
                                    except Exception:
                                        pass
                            try:
                                getattr(stream, "stop", lambda: None)()
                                getattr(stream, "close", lambda: None)()
                            except Exception:
                                pass
                            thr.join(timeout=2.0)
                            if pygame_thread and pygame_thread.is_alive():
                                try:
                                    pygame_thread.join(timeout=2.0)
                                except Exception:
                                    pass
                            set_status("stopped", "Stopped by user")
                            return
                        time.sleep(0.1)

                    set_status("done", "Completed successfully")
                    try:
                        getattr(stream, "close", lambda: None)()
                    except Exception:
                        pass
                    if pygame_thread and pygame_thread.is_alive():
                        for method in ("request_stop", "stop", "close"):
                            m = getattr(plc, method, None)
                            if callable(m):
                                try:
                                    m()
                                except Exception:
                                    pass
                        try:
                            pygame_thread.join(timeout=2.0)
                        except Exception:
                            pass
                finally:
                    # Best-effort cleanup for pygame PLC
                    try:
                        getattr(plc, "close", lambda: None)()
                    except Exception:
                        pass

            # -------- REAL HARDWARE (context-managed PLC) --------
            else:
                with PLCController(is_dryrun=False) as plc:
                    plc.igniter_arm()
                    time.sleep(IGNITER_ARM_DELAY)

                    stream, start_time = play_audio(zf, song_file)

                    def _run_schedule():
                        plc.run_schedule(schedule, stream, start_time + LATENCY_COMPENSATION_OFFSET)

                    thr = threading.Thread(
                        target=_run_schedule, name=f"schedule-{zip_path.name}", daemon=True
                    )
                    thr.start()
                    set_status("running", f"Playback started — map: {map_file}")

                    while thr.is_alive():
                        if stop_event.is_set():
                            for method in ("request_stop", "stop", "abort", "close"):
                                m = getattr(plc, method, None)
                                if callable(m):
                                    try:
                                        m()
                                    except Exception:
                                        pass
                            try:
                                getattr(stream, "stop", lambda: None)()
                                getattr(stream, "close", lambda: None)()
                            except Exception:
                                pass
                            thr.join(timeout=2.0)
                            set_status("stopped", "Stopped by user")
                            return
                        time.sleep(0.1)

                    set_status("done", "Completed successfully")
                    try:
                        getattr(stream, "close", lambda: None)()
                    except Exception:
                        pass

    except Exception as e:
        logger.exception("Worker failed for %s", zip_path)
        set_status("error", f"{type(e).__name__}: {e}")
