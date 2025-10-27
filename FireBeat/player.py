import sounddevice as sd
import soundfile as sf
from FireBeat.logger import logger

def play_audio(zip_file, song_filename):
    """Read and play the audio file inside the zip in a nonblocking fashion."""
    logger.info(f"Playing audio file: {song_filename}")
    data, samplerate = sf.read(zip_file.open(song_filename), dtype="float32")
    channels = data.shape[1] if data.ndim > 1 else 1
    position = 0

    # Callback for nonblocking playback
    def callback(outdata, frames, time_info, status):
        nonlocal position
        if status:
            logger.warning(f"Playback status: {status}")
        chunk = data[position:position + frames]
        if len(chunk) < frames:
            outdata[:len(chunk)] = chunk
            outdata[len(chunk):] = 0
            raise sd.CallbackStop()
        else:
            outdata[:] = chunk
        position += frames

    stream = sd.OutputStream(samplerate=samplerate, channels=channels, callback=callback)
    stream.start()
    start_time = stream.time
    logger.info(f"Audio playback started at stream time {start_time:.3f}s")
    return stream, start_time
