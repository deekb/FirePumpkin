import json
import time
import zipfile
import threading
import os

from FireBeat.map_reader.reader_factory import BeatSaberReaderFactory
from FireBeat.constants import ZIP_DIR, NOTE_DURATION, LATENCY_COMPENSATION_OFFSET, NON_MAP_DAT_FILES, IGNITER_ARM_DELAY
from FireBeat.soundplayer import play_audio
from FireBeat.plc_controller import PLCController, PygamePLCController
from FireBeat.logger import logger, suppress_alsa_warnings
#from FireBeat.beatzip import BeatZipReader

is_dryrun=True

def main():
    print("Welcome to FireBeat!")

    print(f"Running in dryrun mode: {is_dryrun}")

    print("Select your song:")
    zip_files = [filename for filename in os.listdir(ZIP_DIR) if filename.endswith('.zip')]
    for i, zip_file in enumerate(zip_files, start=1):
        print(f"{i}. {zip_file}")
    choice = int(input("Enter the number for your song: ")) - 1
    ZIP_PATH = os.path.join(ZIP_DIR, zip_files[choice])


    logger.info(f"Opening Beat Saber archive: {ZIP_PATH}")

    with zipfile.ZipFile(ZIP_PATH, "r") as zf:
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


        if len(maps_dat) > 1:
            print("Multiple map files found:", maps_dat)
            for i, map_file in enumerate(maps_dat):
                print(f"{i}: {map_file}")
            print("Select a map file by index:")
            index = int(input())
            selected_map_file = maps_dat[index]
        elif len(maps_dat) == 0:
            logger.error("No .dat map files found in archive!")
            raise ValueError("No map files found!")
        else:
            print("Single map file found:", maps_dat[0])
            selected_map_file = maps_dat[0]

        print(f"Selected map file: {selected_map_file}")

        #selecting the map decoding method
        print("Which playback method would you like to use?")
        print("1. Modulus")
        print("2. Note position based")
        method = input("Which playback method would you like to use?")
        if method == "1":
            print("Using modulus playback method.")
            method = "modulus"
        elif method == "2":
            print("Using position-based playback method.")
            method = "position"

        # Passes the selected filename
        reader = BeatSaberReaderFactory.create_reader_from_mapfile(zf.open(selected_map_file))
        map_data = reader.load_map(zf, selected_map_file)
        logger.debug(f"Loaded map data keys: {list(map_data.keys())}")

        schedule = reader.extract_notes(map_data, bpm, NOTE_DURATION, method)
        logger.info(f"Extracted {len(schedule)} firing timings from map.")

        if len(schedule) == 0:
            raise RuntimeError("No notes detected — check map version or difficulty file!")
        if is_dryrun == False:
            plc = PLCController(is_dryrun=is_dryrun)
            plc.igniter_arm()
            logger.debug(f"Waiting {IGNITER_ARM_DELAY}s for igniter arm delay...")
            time.sleep(IGNITER_ARM_DELAY)
            logger.debug("Igniters are hot, commencing.")

            # Nonblocking playback
            stream, start_time = play_audio(zf, song_file) #start music

            t = threading.Thread(target=plc.run_schedule, args=(schedule, stream, start_time))
            t.start()
            t.join()
        elif is_dryrun == True:
            plc = PygamePLCController()

            logger.info("Dryrun mode enabled. Skipping Igniter Arm delay.")

            # Nonblocking playback
            stream, start_time = play_audio(zf, song_file) #start music

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
            with PLCController(is_dryrun=is_dryrun) as plc:
                plc.igniter_disarm() #disables igniters, and safetynet closes all the valves
                logger.info("Kill detected, shutting down safely")
            logger.error("User interrupted.")
