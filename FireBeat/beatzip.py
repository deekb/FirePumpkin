def BeatZipReader(zippath):
    import json
    import zipfile

    from FireBeat.map_reader.reader_factory import BeatSaberReaderFactory
    from FireBeat.constants import NON_MAP_DAT_FILES, NOTE_DURATION

    with zipfile.ZipFile(zippath, "r") as zf:
        filenames = zf.namelist()

        # Verify Info.dat exists
        if "Info.dat" in filenames:
            info_data = json.load(zf.open("Info.dat"))
        elif "info.dat" in filenames:
            info_data = json.load(zf.open("info.dat"))
        else:
            raise FileNotFoundError("Info.dat/info.dat not found in zip!")

        bpm = info_data.get("_beatsPerMinute")
        song_file = info_data.get("_songFilename")

        # Find all map files
        maps_dat = [f for f in filenames if f.endswith(".dat") and f.lower() not in NON_MAP_DAT_FILES]

        if not maps_dat:
            raise ValueError("No .dat map files found in zip.")

        selected_map = mapfile if mapfile else maps_dat[0]

        # Passes the selected filename
        reader = BeatSaberReaderFactory.create_reader_from_mapfile(zf.open(selected_map))
        map_data = reader.load_map(zf, selected_map)

        schedule = reader.extract_notes(map_data, bpm, NOTE_DURATION)

        if len(schedule) == 0:
            raise RuntimeError("No notes detected — check map version or difficulty file!")

        return schedule, song_file, bpm