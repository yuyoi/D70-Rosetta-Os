"""A/B helper: play a note over MIDI, record the Focusrite loop, measure body + tail (wet) energy."""
import sys, time, numpy as np, sounddevice as sd, mido
import os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dsplab import Lab

DEV = [i for i, d in enumerate(sd.query_devices()) if 'Analogue 1 + 2 (Focusrite' in d['name']
       and sd.query_hostapis(d['hostapi'])['name'] == 'Windows WASAPI'][0]
FS = 48000
db = lambda x: 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9)


def take(lab, note=60, hold=1.0, tail=1.5):
    rec = sd.rec(int((0.2 + hold + tail) * FS), samplerate=FS, channels=2, device=DEV)
    time.sleep(0.2)
    for c in range(16): lab.o.send(mido.Message('note_on', channel=c, note=note, velocity=110))
    time.sleep(hold)
    for c in range(16): lab.o.send(mido.Message('note_off', channel=c, note=note))
    sd.wait()
    n0, n1 = int(0.2 * FS), int((0.2 + hold) * FS)
    return rec, db(rec[n0:n1]), db(rec[n1 + int(0.3 * FS):])      # body, tail (after release settles)
