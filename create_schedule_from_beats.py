#!/usr/bin/env python3
import json

# Input and output files
LABELS_FILE = "Thunderstruck.txt"
OUTPUT_JSON = "Thunderstruck.json"

# Number of pumpkins
NUM_PUMPKINS = 1

# Default pulse length if start == end
DEFAULT_DELAY = 0.200  # seconds


def read_labels(file_path):
    """Read a labels.txt file from Audacity export and return a list of (start, end) times."""
    pulses = []
    with open(file_path, "r") as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) >= 2:
                try:
                    start_time = float(parts[0])
                    end_time = float(parts[1])
                    # If no length (start == end), extend by DEFAULT_DELAY
                    if end_time == start_time:
                        end_time = start_time + DEFAULT_DELAY
                    pulses.append((start_time, end_time))
                except ValueError:
                    continue
    return pulses


def assign_pumpkins(pulses, num_pumpkins):
    """Assign pulses to pumpkins in round-robin order."""
    pumpkin_pulses = []
    for i, (start, end) in enumerate(pulses):
        pumpkin = i % num_pumpkins
        pumpkin_pulses.append({
            "start": start,
            "end": end,
            "pumpkin": pumpkin
        })
    return pumpkin_pulses


def main():
    pulses = read_labels(LABELS_FILE)
    pumpkin_schedule = assign_pumpkins(pulses, NUM_PUMPKINS)

    with open(OUTPUT_JSON, "w") as f:
        json.dump(pumpkin_schedule, f, indent=4)

    print(f"Converted {len(pulses)} pulses into JSON for {NUM_PUMPKINS} pumpkins.")


if __name__ == "__main__":
    main()
