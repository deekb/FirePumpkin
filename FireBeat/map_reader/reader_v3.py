import json
from FireBeat.logger import logger
from FireBeat.map_reader.base_reader import BaseMapReader

class BeatSaberReaderV3(BaseMapReader):
    """Reader for Beat Saber map version 3.x."""

    def load_info(self, zip_file):
        logger.info("Loading Info.dat (v3 format)")
        with zip_file.open("Info.dat") as f:
            info = json.load(f)
        logger.debug(f"Info.dat keys: {list(info.keys())}")
        return info

    def load_map(self, zip_file, map_filename):
        logger.info(f"Loading map file: {map_filename}")
        with zip_file.open(map_filename) as f:
            data = json.load(f)
        logger.debug(f"Loaded map with {len(data.get('colorNotes', []) or data.get('basicBeatmapEvents', []))} color notes.")
        return data

    def extract_notes(self, map_data, bpm, note_duration):
        logger.info("Extracting notes from v3 map.")
        notes = map_data.get("colorNotes", None) or map_data.get("basicBeatmapEvents", None)
        result = []
        for i, note in enumerate(notes):
            start_time = note["b"] / (bpm / 60)
            result.append({
                "start": start_time,
                "end": start_time + note_duration,
                "pumpkin": i % 4
            })
        logger.debug(f"Extracted {len(result)} firing timings.")
        logger.debug(f"first 100 timings: {result[:100]}")
        return result
