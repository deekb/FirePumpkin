import pygame
import time
import json
import soundfile as sf
import numpy as np

# --- SETTINGS ---
WIDTH, HEIGHT = 600, 200
BG_COLOR = (30, 30, 30)
FLASH_COLOR = (255, 0, 0)
FPS = 60
BEAT_FLASH_FRAMES = 5  # frames to flash after a beat

# --- AUDIO FILE ---
filename = "Somebody's Watching Me.mp3"  # Replace with your song
data, samplerate = sf.read(filename, dtype='float32')
if data.ndim > 1:
    data = data.mean(axis=1)  # Convert to mono

# --- PYGAME INIT ---
pygame.init()
screen = pygame.display.set_mode((WIDTH, HEIGHT))
pygame.display.set_caption("Manual Beat Recorder")
clock = pygame.time.Clock()
pygame.mixer.init()
sound = pygame.mixer.Sound(filename)

# --- RECORD BEATS ---
beats = []
recording = True
sound.play()
start_time = time.time()

print("Press SPACE to record beats. Press ESC to stop recording.")

while recording:
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            recording = False
        elif event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:
                recording = False
            elif event.key == pygame.K_SPACE:
                timestamp = time.time() - start_time
                beats.append(timestamp)
                print(f"Beat recorded at {timestamp:.3f}s")

    screen.fill(BG_COLOR)
    pygame.display.flip()
    clock.tick(FPS)

sound.stop()

# Save beats to a file
with open("beats.json", "w") as f:
    json.dump(beats, f)
print(f"Saved {len(beats)} beats to beats.json")

# --- PLAYBACK WITH FLASHING ---
print("Playing back song with beat flashes...")
sound.play()
start_time = time.time()
current_frame = 0
flash_counter = 0

while pygame.mixer.get_busy():
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            pygame.mixer.stop()
            pygame.quit()
            exit()

    elapsed = time.time() - start_time

    # Check if any beat should flash
    if current_frame < len(beats):
        if elapsed >= beats[current_frame]:
            flash_counter = BEAT_FLASH_FRAMES
            current_frame += 1

    # Draw screen
    if flash_counter > 0:
        screen.fill(FLASH_COLOR)
        flash_counter -= 1
    else:
        screen.fill(BG_COLOR)

    pygame.display.flip()
    clock.tick(FPS)

pygame.quit()
