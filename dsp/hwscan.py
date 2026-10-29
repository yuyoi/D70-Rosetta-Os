"""Knockout scan: for each RCC word with a non-zero gain, set gain 0, play a note, measure body/tail vs stock, restore.
  py -3.10 dsp/hwscan.py dsp/data/hw_scan.txt"""
import sys, os, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dsplab import Lab, fmt
from hwab import take

lab = Lab('Focusrite USB MIDI')
words = {w: lab.rw(w) for w in range(256)}
static = set(int(x, 16) for x in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'hw_word_classes.txt')).read().split('static')[1].split())
todo = [w for w, v in words.items() if w in static and v != 0x000100]  # effect program only: voice mix words change on every note
base = []
def baseline():
    _, b, t = take(lab); base.append((b, t))
baseline(); baseline()
out = ['# %d static words (voice mix words skipped), baseline body/tail averaged around each block' % len(todo)]
for n, w in enumerate(todo):
    if n and n % 12 == 0: baseline()
    if n and n % 12 == 0:                                   # the stock firmware rewrites words: stop if the program moved
        moved = [x for x in static if lab.rw(x) != words[x]]
        if moved: out.append('!! program changed under the scan (%d words, e.g. W%02X) - stopped' % (len(moved), moved[0])); print(out[-1]); break
    v = lab.rw(w)                                           # restore what is there now, never a stale snapshot
    lab.ww(w, v & ~0xFF)
    _, b, t = take(lab)
    lab.ww(w, v)
    if lab.rw(w) != v: out.append('!! W%02X restore failed' % w)
    bb, bt = np.mean(base[-2:], 0)
    out.append('W%02X %s   body %+5.1f  tail %+5.1f dB' % (w, fmt(words[w]), b - bb, t - bt))
    print(out[-1], flush=True)
out.append('# baselines: ' + ' '.join('%.1f/%.1f' % x for x in base))
open(sys.argv[1], 'w').write('\n'.join(out) + '\n')
