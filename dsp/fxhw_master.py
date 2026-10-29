"""Hardware run of the master FX box (fxbox v3): a short phrase on MIDI ch 1-5 + 10 per mode, recorded from the
Focusrite loop. First proves nothing bypasses the box (box output muted -> silence), then records every mode and
glues them into dsp/data/FX_MASTER_PREVIEW.wav. Stock is restored at the end.
  py -3.10 dsp/fxhw_master.py"""
import os, sys, time, wave
import numpy as np, sounddevice as sd, mido
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dsplab import Lab
import fxbox
from hwab import DEV, FS, db

D = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
CHORD = {0: [48, 60, 64], 1: [55, 67], 2: [52, 72], 3: [43, 59], 4: [60, 76], 9: [36, 38, 42]}


def phrase(lab, hold=1.4, tail=1.2):
    rec = sd.rec(int((0.2 + hold + tail) * FS), samplerate=FS, channels=2, device=DEV)
    time.sleep(0.2)
    for c, ns in CHORD.items():
        for n in ns: lab.o.send(mido.Message('note_on', channel=c, note=n, velocity=105))
    time.sleep(hold)
    for c, ns in CHORD.items():
        for n in ns: lab.o.send(mido.Message('note_off', channel=c, note=n))
    sd.wait()
    return rec


def apply(lab, st, words, cells):
    for a, v in words.items(): lab.ww(a, v)
    for c in fxbox.SCRATCH: lab.wc(c, cells.get(c, 0))
    bad = [a for a, v in words.items() if lab.rw(a) != v]
    assert not bad, 'readback mismatch ' + str(bad)


def save(path, x):
    y = (np.clip(x, -1, 1) * 32767).astype('<i2')
    with wave.open(path + '.tmp', 'wb') as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(FS); w.writeframes(y.tobytes())
    os.replace(path + '.tmp', path)


def main():
    lab = Lab('Focusrite USB MIDI')
    st = {a: lab.rw(a) for a in range(256)}
    open(fxbox.STOCK + '.tmp', 'w').write(''.join('%02X %06X\n' % kv for kv in st.items())); os.replace(fxbox.STOCK + '.tmp', fxbox.STOCK)
    stockw = {a: st[a] for a in fxbox.touched(st)}
    takes = []
    try:
        ref = phrase(lab); print('stock          body %6.1f dB' % db(ref[int(0.2 * FS):int(1.6 * FS)])); takes.append(('stock', ref))
        # bypass check, per channel: thru vs thru with both box outputs at gain 0
        tw, tc = fxbox.build(st, 'thru')
        mw = dict(tw); mw[0x1C] = fxbox.gword(tw[0x1C], 0); mw[0xDC] = fxbox.gword(tw[0xDC], 0)
        global CHORD
        full = CHORD
        for ch in full:
            CHORD = {ch: full[ch]}
            apply(lab, st, tw, tc); a = phrase(lab, 1.0, 0.6)
            apply(lab, st, mw, tc); b = phrase(lab, 1.0, 0.6)
            s = slice(int(0.2 * FS), int(1.2 * FS))
            print('ch%-2d bypass: thru %6.1f dB  box muted %6.1f dB  -> %.1f%% skips the box' % (
                ch + 1, db(a[s]), db(b[s]), 100 * 10 ** ((db(b[s]) - db(a[s])) / 20)))
        CHORD = full
        for mode, args in [('thru', ()), ('drive', (8,)), ('drive', (15.875,)), ('crush', (14,)), ('crush', (17,)),
                           ('filter', ('lp', 500, 4)), ('filter', ('bp', 1200, 6)), ('filter', ('hp', 2500, 2)),
                           ('decimate', (6,)), ('decimate', (20,)), ('rectify', (1,))]:
            apply(lab, st, *fxbox.build(st, mode, *args))
            r = phrase(lab)
            name = '_'.join([mode] + [str(x) for x in args])
            print('%-14s body %6.1f dB  peak %.2f' % (name, db(r[int(0.2 * FS):int(1.6 * FS)]), np.abs(r).max()))
            takes.append((name, r)); save(os.path.join(D, 'fxm_%02d_%s.wav' % (len(takes), name)), r)
    finally:
        for a, v in stockw.items(): lab.ww(a, v)
        for c in fxbox.SCRATCH: lab.wc(c, 0)
        bad = [a for a, v in stockw.items() if lab.rw(a) != v]
        print('stock restored' if not bad else 'RESTORE FAILED ' + str(bad))
        if not bad: os.remove(fxbox.STOCK)
    gap = np.zeros((int(0.4 * FS), 2))
    save(os.path.join(D, 'FX_MASTER_PREVIEW.wav'), np.concatenate([np.concatenate([r, gap]) for _, r in takes]))
    print('order:', ', '.join(n for n, _ in takes))


if __name__ == '__main__':
    main()
