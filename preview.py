#!/usr/bin/env python3
import json
import time
import pygame
import soundfile as sf
import sounddevice as sd
import numpy as np

# Constants
SCHEDULE_FILE = "Thunderstruck.json"  # JSON list of events
AUDIO_FILE = "Thunderstruck.ogg"         # Your audio file
PULSE_SECONDS = 0.2              # Coil pulse duration

# Colors
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
FIRE_COLOR = (255, 140, 0)
OFF_COLOR = (150, 150, 150)
WAVE_COLOR = (50, 200, 50)

def main():
    # Load schedule (list of events)
    with open(SCHEDULE_FILE, "r") as f:
        events_json = json.load(f)

    # Parse events
    events = [(float(e["time"]), int(e.get("coil", e.get("note", 0)))) for e in events_json]
    events.sort(key=lambda x: x[0])

    # Determine number of pumpkins
    num_pumpkins = max([ch for _, ch in events]) + 1

    # Load audio
    data, sr = sf.read(AUDIO_FILE, dtype="float32", always_2d=True)
    mono = data[:,0] if data.shape[1] > 1 else data[:,0]

    # Prepare waveform for first 10 seconds
    max_display_time = 10.0
    waveform_samples = int(min(max_display_time, mono.shape[0]/sr) * sr)
    waveform = mono[:waveform_samples]
    waveform = waveform / np.max(np.abs(waveform))

    # Initialize pygame
    pygame.init()
    WIDTH, HEIGHT = 800, 400
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    pygame.display.set_caption("Pumpkin Fire Show Visualizer")
    clock = pygame.time.Clock()
    font = pygame.font.SysFont(None, 28)

    # Position pumpkins evenly
    pumpkin_rects = []
    pumpkin_size = 80
    spacing = WIDTH // (num_pumpkins + 1)
    for i in range(num_pumpkins):
        x = spacing * (i + 1) - pumpkin_size // 2
        y = HEIGHT // 2 - pumpkin_size // 2
        pumpkin_rects.append(pygame.Rect(x, y, pumpkin_size, pumpkin_size))

    # Coil states
    coil_on_until = [0.0] * num_pumpkins
    event_idx = 0

    # Start timing
    t0 = time.perf_counter()

    # Start audio playback
    sd.stop()
    sd.play(data, sr, blocking=False)

    running = True
    while running:
        now = time.perf_counter() - t0

        # Handle quit
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False

        # Trigger events
        while event_idx < len(events) and events[event_idx][0] <= now:
            _, coil = events[event_idx]
            coil_on_until[coil] = now + PULSE_SECONDS
            event_idx += 1

        # Draw background
        screen.fill(BLACK)

        # Draw waveform
        if now < max_display_time:
            step = max(1, len(waveform) // WIDTH)
            for i in range(0, len(waveform), step):
                x = i // step
                y = int((waveform[i] * 0.4 + 0.5) * HEIGHT)
                pygame.draw.line(screen, WAVE_COLOR, (x, HEIGHT//2), (x, y))

        # Draw pumpkins
        for i, rect in enumerate(pumpkin_rects):
            color = FIRE_COLOR if now <= coil_on_until[i] else OFF_COLOR
            pygame.draw.ellipse(screen, color, rect)
            label = font.render(f"Pumpkin {i}", True, WHITE)
            screen.blit(label, (rect.x + rect.width//2 - label.get_width()//2, rect.y + rect.height + 5))

        # Time display
        time_text = font.render(f"Time: {now:.2f}s", True, WHITE)
        screen.blit(time_text, (10, 10))

        pygame.display.flip()
        clock.tick(60)

        # End after audio finishes
        if now > data.shape[0] / sr + 1:
            running = False

    pygame.quit()
    sd.stop()

if __name__ == "__main__":
    main()
