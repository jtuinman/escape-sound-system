import pygame
import time

pygame.mixer.init()

pygame.mixer.music.load("/home/pi/escape-sound-system/audio/state1.mp3")
pygame.mixer.music.play(-1)   # loop

time.sleep(5)

hint = pygame.mixer.Sound("/home/pi/escape-sound-system/audio/hint1.mp3")
hint.play()

time.sleep(5)

pygame.quit()
