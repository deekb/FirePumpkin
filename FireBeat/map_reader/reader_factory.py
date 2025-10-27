import json
import logging
from .reader_v2 import BeatSaberReaderV2
from .reader_v3 import BeatSaberReaderV3

logger = logging.getLogger(__name__)


class BeatSaberReaderFactory:
    """Factory that selects the correct Beat Saber map reader based on the map file's internal version string."""

    @staticmethod
    def create_reader_from_mapfile(file_obj):
        """Read `_version` field from the given .dat file-like object and select appropriate reader."""
        try:
            file_obj.seek(0)
            data = json.load(file_obj)
            version = data.get("_version", None) or data.get("version", None)
            if version is None:
                raise Exception("Version field is missing from .dat file")
            logger.info(f"Detected Beat Saber map version {version}")
        except Exception as e:
            logger.exception("Failed to detect map version, defaulting to 2.0.0")
            version = "2.0.0"

        # Select reader class
        if version.startswith("3"):
            logger.info(f"Selecting reader for Beat Saber version {version} (using BeatSaberReaderV3)")
            reader = BeatSaberReaderV3(data)
        else:
            logger.info(f"Selecting reader for Beat Saber version {version} (using BeatSaberReaderV2)")
            reader = BeatSaberReaderV2(data)

        return reader
