import json
import time
import zipfile

import sounddevice as sd
import soundfile as sf

# Path to the zip file
ZIP_PATH = "The_Summoning.zip"
MAP_FILETYPE = ".dat"
INFO_PATH = "Info.dat"
NOTE_DURATION = 0.1  # seconds
# Open the zip file
with zipfile.ZipFile(ZIP_PATH, "r") as zf:
    # List all files inside
    filenames = zf.namelist()
    maps_dat = [filename for filename in filenames if filename.endswith(MAP_FILETYPE) and filename != INFO_PATH]

    print("Files in zip:", filenames)
    assert INFO_PATH in filenames, "Info.dat file is missing!"

    if len(maps_dat) > 1:
        print("Multiple map files found:", maps_dat)
        for i, map_file in enumerate(maps_dat):
            print(f"{i}: {map_file}")
        print("Select a map file by index:")
        index = int(input())
        selected_map_file = maps_dat[index]
    elif len(maps_dat) == 0:
        raise ValueError("No map files found!")
    else:
        print("Single map file found:", maps_dat[0])
        selected_map_file = maps_dat[0]

    print(f"Selected map file: {selected_map_file}")

    # Open a specific file within the zip
    with zf.open(INFO_PATH) as file:
        data = json.load(file)

        bpm = (data["_beatsPerMinute"])
        song_file = (data["_songFilename"])
        assert data["_songFilename"] in filenames, "Song file is missing!"

    data, samplerate = sf.read(zf.open(song_file), dtype="float32")

    # Create an output stream so we can use its clock
    # with sd.OutputStream(samplerate=samplerate, channels=data.shape[1] if data.ndim > 1 else 1) as stream:
    #     # Start time according to audio stream clock
    #     stream_start_time = stream.time
    #
    #     # Play audio through the same stream
    #     stream.write(data)

    print("BPM:", bpm)
    print("Song file:", song_file)

    # Open the map file
    with zf.open(selected_map_file) as file:
        data = json.load(file)
        print(data.keys())
        notes = data["_notes"]
        firing_timings = []
        for i, note in enumerate(notes):
            # print(note)
            note_start_time = note["_time"] / (bpm / 60)
            firing_timings.append({
                "start": note_start_time,
                "end": note_start_time + NOTE_DURATION,
                "pumpkin": i % 4
            })

        with open("The_Summoning.json", "w") as file:
            json.dump(firing_timings, file, indent=4)
