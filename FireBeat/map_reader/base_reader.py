import abc
from FireBeat.logger import logger

class BaseMapReader(abc.ABC):
    """Abstract interface for Beat Saber map readers."""

    def __init__(self, zip_file):
        logger.debug(f"{self.__class__, __name__} initialized.")

    @abc.abstractmethod
    def load_info(self, zip_file):
        pass

    @abc.abstractmethod
    def load_map(self, zip_file, map_filename):
        pass

    @abc.abstractmethod
    def extract_notes(self, map_data, bpm, note_duration):
        pass
