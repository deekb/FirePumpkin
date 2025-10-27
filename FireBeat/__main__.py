import json
import time
import zipfile
import threading

from FireBeat.map_reader.reader_factory import BeatSaberReaderFactory
from FireBeat.constants import ZIP_PATH, NOTE_DURATION, LATENCY_COMPENSATION_OFFSET, NON_MAP_DAT_FILES, \
    IGNITER_ARM_DELAY
from FireBeat.player import play_audio
from FireBeat.plc_controller import PLCController, PygamePLCController
from FireBeat.logger import logger, suppress_alsa_warnings


def main():
    logger.info(f"Opening Beat Saber archive: {ZIP_PATH}")

    with zipfile.ZipFile(ZIP_PATH, "r") as zf, PLCController(is_dryrun=True) as plc:
        # with  as plc:
        filenames = zf.namelist()
        logger.debug(f"Files inside zip: {filenames}")

        # Verify Info.dat exists
        if "Info.dat" in filenames:
            info_data = json.load(zf.open("Info.dat"))
        elif "info.dat" in filenames:
            info_data = json.load(zf.open("info.dat"))
        else:
            logger.error("Info.dat/info.dat missing from archive!")
            raise FileNotFoundError("Info.dat/info.dat not found in zip!")

        logger.debug(f"Top-level keys in Info.dat: {list(info_data.keys())}")

        bpm = info_data.get("_beatsPerMinute")
        song_file = info_data.get("_songFilename")
        logger.debug(f"BPM: {bpm}, Song: {song_file}")

        if song_file not in filenames:
            logger.warning(f"Song file '{song_file}' not found in archive; available files: {filenames}")

        # Find all map files
        maps_dat = [f for f in filenames if f.endswith(".dat") and f.lower() not in NON_MAP_DAT_FILES]
        logger.debug(f"Detected map files: {maps_dat}")

        if not maps_dat:
            logger.error("No .dat map files found in archive!")
            raise ValueError("No .dat map files found in zip.")

        selected_map = maps_dat[0]

        reader = BeatSaberReaderFactory.create_reader_from_mapfile(zf.open(selected_map))
        map_data = reader.load_map(zf, selected_map)
        logger.debug(f"Loaded map data keys: {list(map_data.keys())}")

        schedule = reader.extract_notes(map_data, bpm, NOTE_DURATION)
        logger.info(f"Extracted {len(schedule)} firing timings from map.")

        if len(schedule) == 0:
            raise RuntimeError("No notes detected — check map version or difficulty file!")

        plc.igniter_arm()
        logger.debug(f"Waiting {IGNITER_ARM_DELAY}s for igniter arm delay...")
        time.sleep(IGNITER_ARM_DELAY)
        logger.debug("Igniters are hot, commencing.")

        # Nonblocking playback
        stream, start_time = play_audio(zf, song_file)

        # t = threading.Thread(target=plc.run_schedule, args=(schedule, stream, start_time))
        # t.start()
        # t.join()

        plc = PygamePLCController()

        # Start schedule in a background thread
        schedule_thread = threading.Thread(target=plc.run_schedule, args=(schedule, stream, start_time + LATENCY_COMPENSATION_OFFSET))
        schedule_thread.start()

        # Run pygame loop in the main thread (blocks until window closed)
        plc.run_pygame_loop()

        # Wait for schedule to finish
        schedule_thread.join()
        plc.close()

    logger.info("Beat Saber show finished successfully.")

if __name__ == "__main__":
    with suppress_alsa_warnings():
        try:
            main()
        except KeyboardInterrupt:
            logger.error("User interrupted.")
