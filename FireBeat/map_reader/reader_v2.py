import json
from FireBeat.logger import logger
from .base_reader import BaseMapReader

class BeatSaberReaderV2(BaseMapReader):
    """Reader for Beat Saber map version 2.x."""

    def load_info(self, zip_file):
        logger.info("Loading Info.dat (v2 format)")
        with zip_file.open("Info.dat") as f:
            info = json.load(f)
        logger.debug(f"Info.dat keys: {list(info.keys())}")
        return info

    def load_map(self, zip_file, map_filename):
        logger.info(f"Loading map file: {map_filename}")
        with zip_file.open(map_filename) as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError as e:
                logger.exception(f"Failed to parse map file {map_filename}: {e}")
                raise
        note_count = len(data.get("_notes", []))
        logger.debug(f"Loaded map with {note_count} notes. Keys: {list(data.keys())}")
        if note_count == 0:
            logger.debug(f"Map contents sample (first 500 chars): {str(data)[:500]}")
        return data

    def extract_notes(self, map_data, bpm, note_duration):
        logger.info("Extracting notes from v2 map.")
        notes = map_data.get("_notes", [])
        if not notes:
            logger.warning("Map has zero notes! Possibly wrong difficulty or format.")
        else:
            logger.debug(f"First note sample: {notes[0]}")

        result = []
        for i, note in enumerate(notes):
            try:
                start_time = note["_time"] / (bpm / 60)
                result.append({
                    "start": start_time,
                    "end": start_time + note_duration,
                    "pumpkin": i % 4
                })
            except KeyError as e:
                logger.error(f"Malformed note entry missing key {e}: {note}")
        logger.debug(f"Extracted {len(result)} firing timings.")
        return result
