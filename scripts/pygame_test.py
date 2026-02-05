import pygame
import time

pygame.mixer.init()
pygame.mixer.music.load("/home/pi/escape-sound-system/audio/state1.mp3")
pygame.mixer.music.set_volume(0.7)
pygame.mixer.music.play()

print("playing...")
time.sleep(10)

pygame.mixer.quit()
