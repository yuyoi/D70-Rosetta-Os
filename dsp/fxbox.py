"""FX box v3 for the D-70 effects chip (RCC, IC23), live over the Rosetta 0.8 DSP LAB: a MASTER insert. It sits on
the main L/R mix after the reverb, so every part goes through it whatever its Output Assign (Dry, Rev or Cho).
The chorus is lost (its steps do the work); Cho-assigned parts still reach the reverb dry, like CHORUS level 0.
(v2, the chorus-slot box, is kept as the local research folder.)

Signal chain on hw (docs/DSP.md): every bus ends in c24 (L) / c30 (R); output 0 reads c24 at
L261 then the seed resets it, output 6 reads c30 at L419 then resets it. Every mode:
  captures L  outputCapture1      dest W0B = c24 * -g W09   (src W08 -> c24; runs at L252, just before the read)
  captures R  chorusReturnMix1    dest WD6 = c30 * g WD2 + cell WD2 * g WD4   (src WD1 -> c30; L417, sums too)
  runs its own steps in the chorus / ramp words, scratch cells 3 5 6 12 13 22 23 (c1 = 0)
  outputs 0 / 6 read the result cells (W19 / WDB) at gain W1C / WDC (stock x2)
Voices that land in c24 between L252 and L262 (slots 1, 2) or in c30 between L417 and L421 (slot 26) are lost
for that side: one or two notes now and then. Voice words are never touched (checked against
dsp/data/hw_word_classes.txt). The patch lives in RCC RAM: changing the effect type or power-off brings stock back.
  py -3.10 dsp/fxbox.py thru               box on, no effect (the reference)
  py -3.10 dsp/fxbox.py drive 8            pre gain 1..16, hard clip at the 24-bit rail, level matched (stereo)
  py -3.10 dsp/fxbox.py crush 12           MONO bit crush, drop 7..17 of the 24 bits (14 = 8-bit, 17 = 5-bit)
  py -3.10 dsp/fxbox.py filter lp 800 4    resonant state-variable filter (mono sum), lp | bp | hp, Hz, Q
  py -3.10 dsp/fxbox.py decimate 6         sample and hold for n frames (stereo)
  py -3.10 dsp/fxbox.py rectify 1          MONO full-wave rectifier (octave-up fuzz) + DC blocker, input gain
  py -3.10 dsp/fxbox.py off                stock program back
"""
import math
import os
import sys

FS = 32000                    # RCC frame rate (filter cutoff maths); see FW_REWRITE_NOTES DSP DIG
GAIN, CELL, NIB = 0x1FF, 31 << 9, 15 << 14
ZERO = 1                      # c1 reads 0 on hw (mixBase cell)
JUNK = 0                      # c0: stock already dumps unused step results there
HERE = os.path.dirname(os.path.abspath(__file__))
STOCK = os.path.join(HERE, 'data', 'fx_stock_words.txt')
CLASSES = os.path.join(HERE, 'data', 'hw_word_classes.txt')
SCRATCH = (3, 5, 6, 12, 13, 22, 23)
OUT_GAIN = 2.0                # stock output gain (W1C / WDC)


def gword(w, x):
    """put gain x into word w: |x| < 1 fine /128 scale, else /8 (max 15.875)"""
    c, s4 = (round(x * 128), 0x100) if abs(x) < 1 else (round(x * 8), 0)
    assert -128 <= c <= 127, x
    return (w & ~GAIN) | s4 | (c & 255)


def gval(x):
    """the gain the chip really gets for x"""
    return round(x * 128) / 128 if abs(x) < 1 else round(x * 8) / 8


class P:
    """patch builder on top of the stock words"""
    def __init__(self, stock):
        self.st, self.w, self.cells = stock, {}, {}

    def _cur(self, a):
        return self.w.get(a, self.st[a])

    def g(self, a, x):
        self.w[a] = gword(self._cur(a), x); return self

    def c(self, a, cell):
        self.w[a] = (self._cur(a) & ~CELL) | (cell & 31) << 9; return self

    def n(self, a, nib):
        self.w[a] = (self._cur(a) & ~NIB) | (nib & 15) << 14; return self

    def master(self):
        """chorus wet off, its dry (c20/21 -> reverb bus c18/19) kept; free the chorus steps"""
        self.g(0xC9, 0)                                   # chorus delay input (cis * g) -> DRAM stays silent
        self.g(0x03, 0).c(0x6D, ZERO)                     # previous-chorus returns L / R (dry gains stay)
        self.g(0xCB, 0)                                   # tap-1 return
        self.c(0x13, JUNK).c(0x6C, JUNK).c(0x94, JUNK)    # S1 / R capture / S2 dests: c5 c6 free
        self.c(0xCC, JUNK).c(0xD6, JUNK)                  # chorusReturnMix0/1 no longer add into c18/19
        return self

    def cap_l(self, dest, g):
        """dest = c24 * g at L252"""
        return self.c(0x08, 24).g(0x09, -g).c(0x0B, dest)

    def cap_r(self, dest, g, add=ZERO, ga=0.0):
        """dest = c30 * g + cell add * ga at L417"""
        return self.c(0xD1, 30).g(0xD2, g).c(0xD2, add).g(0xD4, ga).c(0xD6, dest)

    def out(self, cl, gl, cr, gr):
        return self.c(0x19, cl).g(0x1C, gl).c(0xDB, cr).g(0xDC, gr)


def ups(total, fixed):
    """gains for `fixed` stages whose product (after the chip rounds them) is `total`; the last one corrects"""
    each, out, prod = total ** (1.0 / (fixed + 1)), [], 1.0
    for _ in range(fixed):
        out.append(gval(each)); prod *= out[-1]
    last = total / prod
    assert last <= 15.875, total
    return out, last


# ---- modes: each returns (P, cells to preset) ----
def thru(st):
    """the box with no effect: L and R straight through"""
    p = P(st).master().cap_l(3, 1.0).cap_r(5, 1.0)
    return p.out(3, OUT_GAIN, 5, OUT_GAIN), {}


def drive(st, pre=8.0):
    pre = max(1.0, min(float(pre), 15.875))
    p = P(st).master().cap_l(3, pre).cap_r(5, pre)           # saturates at the rail
    return p.out(3, OUT_GAIN / pre, 5, OUT_GAIN / pre), {}


def crush(st, k=12):
    """mono bit crush: drop k of the 24 bits (7..17; the bus runs at 10-25 % FS, so 14 is about 8-bit, 17 about 5-bit).
         capL  c3  = L / 128                       (L252)
         capR  c22 = R / 128 + c3                  (L417, the first 7 bits go here)
         P4    c23 = c22 / 2^min(k-7, 7)           (L427)
         P6    c12 = c23 * a                       (L439; a = 2^(14-k) cuts the last bits for k > 14, else an up gain)
         then up to L + R: ramp1 c13 (two gains), S2 c6, chorusReturnMix0 c5, both outputs"""
    k = int(k); assert 7 <= k <= 17, 'crush 7..17'
    p = P(st).master().cap_l(3, 1 / 128).cap_r(22, 1 / 128, 3, 1.0)
    p.c(0xE5, ZERO).c(0xE4, 22).g(0xE5, 2.0 ** -min(k - 7, 7)).c(0xE7, 23)
    if k > 14:
        a = 2.0 ** (14 - k); u, last = ups(2.0 ** k, 4)
    else:
        (a, *u), last = ups(2.0 ** k, 5)
    p.c(0xE9, 23).g(0xE9, 0).c(0xEC, ZERO).g(0xEC, -a).c(0xEE, 12)     # P6, fold off
    p.c(0x82, ZERO).c(0x81, 12).g(0x82, -u[0])                          # ramp1 cand = c12 * u0
    p.c(0x83, 15).g(0x84, -15.875).c(0x86, ZERO).g(0x86, u[1] - 1).c(0x88, 13)
    p.c(0x92, ZERO).c(0x90, 13).g(0x92, u[2]).c(0x94, 6)                # S2 c6 = c13 * u2
    p.c(0xC7, 6).g(0xC8, u[3]).c(0xC9, ZERO).c(0xCC, 5)                 # c5 = c6 * u3 (chorus tap is silent)
    return p.out(5, last, 5, last), {}


def filt(st, kind='lp', fc=800, q=2.0):
    """Chamberlin SVF on the mono sum x = (L + R) / 2 (c5, made by capR at L417):
         ramp1 (L353):  bp = bp + f * hp       c23
         P4    (L427):  lp = lp + f * bp       c22
         P5    (L435):  t  = x - q * bp        c13
         P6    (L439):  hp = t - lp            c12"""
    f = 2 * math.sin(math.pi * float(fc) / FS)
    qq = 1.0 / float(q)
    out = {'lp': 22, 'bp': 23, 'hp': 12}[kind]
    p = P(st).master().cap_l(3, 0.5).cap_r(5, 0.5, 3, 1.0)
    p.c(0x82, 23).c(0x81, 12).g(0x82, -f)                         # r1c = bp + f * hp
    p.c(0x83, 15).g(0x84, -15.875).g(0x86, 0).c(0x88, 23)
    p.c(0xE5, 22).c(0xE4, 23).g(0xE5, f).c(0xE7, 22)              # lp += f * bp
    p.g(0xE4, 0).c(0xEB, 5).g(0xEB, qq).c(0xED, 13)               # t = x - q * bp   (fold off: cp2 = bp)
    p.c(0xE9, 22).g(0xE9, 0).c(0xEC, 13).g(0xEC, 1.0).c(0xEE, 12) # hp = t - lp
    lvl = OUT_GAIN * (1.0 if kind != 'bp' else min(1.0, qq))
    return p.out(out, lvl, out, lvl), {}


def st_gain(st, a):
    c = st[a] & 255
    return (c - 256 if c & 128 else c) / (128 if st[a] & 0x100 else 8)


def decimate(st, n=6):
    """sample and hold through the chorus DRAM: R goes in by the chorus delay input (tap 53, offset 1, cis = c30 at
    L404), L by auxDelayTransfer2 (tap 44, moved to offset 128, from capL c5). ramp1 counts ST += 0x1000 per frame
    and resets at n; chorus tap 0 reads at 2 + ST (R, returned by chorusReturnMix0 -> c5, out 6 reads it at L419
    before capL overwrites c5), tap 1 at 129 + ST (L, chorusReturnMix1 -> c6 -> out 0)."""
    n = int(n); assert 1 <= n <= 255
    INC, LIM, ST, BL, BR = 12, 13, 22, 23, 3
    ret = st_gain(st, 0xC1)                                       # tap-0 return gain lives in a voice word
    p = P(st).master().cap_l(5, 1.0)
    p.c(0xC7, 30).g(0xC9, 1.0)                                    # chorus delay input = c30
    p.c(0xB0, 5).g(0xB0, 0).n(0xB0, 0).n(0xB1, 0).n(0xB2, 8)      # T44 <- c5 at offset 0x0080
    p.c(0x82, INC).c(0x81, ST).g(0x82, -1.0)                      # cand = INC + ST
    p.c(0x83, LIM).g(0x84, 1.0).c(0x86, ZERO).g(0x86, 0).c(0x88, ST)
    p.c(0xA1, ST).c(0xA2, BL).g(0xA2, 1.0)                        # phase0 = BL + ST
    p.c(0xB1, ST).g(0xB2, 1.0).g(0xB8, 0).c(0xBA, 0).c(0xB9, BR).g(0xBA, -1.0)   # phase1 = ST + BR
    p.g(0xC8, 0).c(0xC9, ZERO).c(0xCC, 5)                         # chorusReturnMix0 = tap0 * ret -> c5
    p.g(0xCB, ret).g(0xD2, 0).g(0xD4, 0).c(0xD6, 6)               # chorusReturnMix1 = tap1 * ret -> c6
    lvl = OUT_GAIN / ret
    return p.out(6, lvl, 5, lvl), {INC: 0x1000, LIM: n * 0x1000, ST: 0, BL: 0x2000, BR: 0x81000}


def rectify(st, gain=1.0):
    """mono full-wave rectifier with a 40 Hz DC blocker:
         x = c5 = (L + R) / 2 (capL + capR), phase1 c23 = -|x * gain|,
         ramp1 c13 = c23 - lp, P4 lp (c22) += c13 / 128, both outputs read c13 * -2"""
    gain = float(gain)
    p = P(st).master().cap_l(3, 0.5).cap_r(5, 0.5, 3, 1.0)
    p.c(0xB1, ZERO).g(0xB2, 0).c(0xB7, 5).g(0xB8, gain).c(0xBA, 23)
    p.c(0x82, 23).c(0x81, 22).g(0x82, 1.0)                        # r1c = c23 - lp
    p.c(0x83, 15).g(0x84, -15.875).c(0x86, ZERO).g(0x86, 0).c(0x88, 13)
    p.c(0xE5, 22).c(0xE4, 13).g(0xE5, 1 / 128).c(0xE7, 22)        # lp += c13 / 128
    return p.out(13, -OUT_GAIN, 13, -OUT_GAIN), {}


MODES = {'thru': thru, 'drive': drive, 'crush': crush, 'filter': filt, 'decimate': decimate, 'rectify': rectify}


def touched(st):
    """every word any mode writes (switching modes puts the others back to stock)"""
    out = set()
    for fn, args in ((thru, ()), (drive, ()), (crush, ()), (crush, (17,)), (filt, ()), (decimate, ()), (rectify, ())):
        out |= set(fn(st, *args)[0].w)
    return out


def build(st, mode, *args):
    p, cells = MODES[mode](st, *args)
    words = {a: st[a] for a in touched(st)}
    words.update(p.w)
    if os.path.exists(CLASSES):
        voice = set(int(x, 16) for x in open(CLASSES).read().split('static')[0].split()[1:])
        bad = sorted(voice & set(p.w))
        assert not bad, 'patch touches voice words ' + ' '.join('%02X' % a for a in bad)
    return words, cells


def main():
    sys.path.insert(0, HERE)
    from dsplab import Lab
    if len(sys.argv) < 2: print(__doc__); return
    lab = Lab('Focusrite USB MIDI')
    if os.path.exists(STOCK):
        st = {int(a, 16): int(v, 16) for a, v in (l.split() for l in open(STOCK))}
    else:
        st = {a: lab.rw(a) for a in range(256)}
        open(STOCK + '.tmp', 'w').write(''.join('%02X %06X\n' % kv for kv in st.items())); os.replace(STOCK + '.tmp', STOCK)
    if sys.argv[1] == 'off':
        words = {a: st[a] for a in touched(st)}
        for a, v in words.items(): lab.ww(a, v)
        for c in SCRATCH: lab.wc(c, 0)
        bad = [a for a, v in words.items() if lab.rw(a) != v]
        print('stock restored' if not bad else 'RESTORE FAILED at ' + ' '.join('W%02X' % a for a in bad))
        if not bad: os.remove(STOCK)
        return
    words, cells = build(st, sys.argv[1], *sys.argv[2:])
    for a, v in words.items(): lab.ww(a, v)
    for c in SCRATCH: lab.wc(c, cells.get(c, 0))
    bad = [a for a, v in words.items() if lab.rw(a) != v]
    print('%s %s on (%d words%s)' % (sys.argv[1], ' '.join(sys.argv[2:]), len(words), ', ok' if not bad else ', MISMATCH ' + str(bad)))


if __name__ == '__main__':
    main()
