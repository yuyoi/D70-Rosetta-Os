#!/usr/bin/env python3
"""PC side of the Rosetta DSP LAB (v0.8+): read / write the D-70 effects chip (RCC, IC23) live over MIDI SysEx.
  py -3.10 tools\\dsplab.py --list                               MIDI ports
  py -3.10 tools\\dsplab.py --port "MIDI (OCTA" ping                 read word 0 (proves the round trip)
  py -3.10 tools\\dsplab.py --port "MIDI (OCTA" dump  [out.txt]      all 256 parameter words + 32 cells
  py -3.10 tools\\dsplab.py --port "MIDI (OCTA" rw ADDR              read word (hex)
  py -3.10 tools\\dsplab.py --port "MIDI (OCTA" ww ADDR VALUE        write word (hex)
  py -3.10 tools\\dsplab.py --port "MIDI (OCTA" rc CELL / wc CELL VALUE
Messages: F0 7D 4C cmd <nibbles> F7; cmd 1 write word, 2 write cell, 3 read word, 4 read cell; replies cmd + 0x10.
Word format (FW_REWRITE_NOTES DSP DIG): bits 0-7 gain, bit 8 fine scale, 9-13 RAM A cell, 14-17 delay nibble.
The stock firmware rewrites effect words when the effect type changes: set the type first, then poke.
"""
import argparse, sys, time
import mido


def nib(*bs):
    out = []
    for b in bs: out += [b >> 4 & 15, b & 15]
    return out


def unnib(ns):
    return [ns[i] << 4 | ns[i + 1] for i in range(0, len(ns), 2)]


class Lab:
    def __init__(self, port):
        self.o = mido.open_output(next(n for n in mido.get_output_names() if port.lower() in n.lower()))
        self.i = mido.open_input(next(n for n in mido.get_input_names() if port.lower() in n.lower()))

    def send(self, cmd, *bs):
        self.o.send(mido.Message('sysex', data=[0x7D, 0x4C, cmd] + nib(*bs)))

    def _read(self, cmd, idx, timeout=1.0):
        for _ in self.i.iter_pending(): pass
        self.send(cmd, idx)
        t = time.time()
        while time.time() - t < timeout:
            for m in self.i.iter_pending():
                d = list(m.data) if m.type == 'sysex' else []
                if d[:3] == [0x7D, 0x4C, cmd + 0x10]:
                    a, h, mi, lo = unnib(d[3:11])
                    if a == idx: return h << 16 | mi << 8 | lo
            time.sleep(0.002)
        raise TimeoutError('no reply from the D-70 (MIDI OUT -> PC connected? Rosetta 0.8 on the chips?)')

    def mem(self, addr, timeout=1.0):
        """the CPU word at an even addr -> [lo byte, hi byte] (Rosetta DSP LAB cmd 5).
        Careful: reading SP_STAT 0x11 / IOS1 0x16 clears flags, 0x0608 / 0x0609 takes a pending key"""
        for _ in self.i.iter_pending(): pass
        self.send(5, addr & 255, addr >> 8)
        t = time.time()
        while time.time() - t < timeout:
            for m in self.i.iter_pending():
                d = list(m.data) if m.type == 'sysex' else []          # mido drops F0 / F7
                if d[:3] == [0x7D, 0x4C, 0x15]:
                    b = unnib(d[3:11])
                    if b[0] | b[1] << 8 == addr: return b[2:]
            time.sleep(0.002)
        raise TimeoutError('no reply to cmd 5 (Rosetta with the memory reader on the chips?)')

    def rw(self, a): return self._read(3, a)
    def rc(self, c): return self._read(4, c)
    def ww(self, a, v): self.send(1, a, v >> 16 & 255, v >> 8 & 255, v & 255)
    def wc(self, c, v): self.send(2, c, v >> 16 & 255, v >> 8 & 255, v & 255)


def fmt(w):
    g = w & 255; g = g - 256 if g & 128 else g
    return '%06X  gain %+4d%s  cell %2d  nib %X' % (w, g, '/128' if w & 0x100 else '/8  ', w >> 9 & 31, w >> 14 & 15)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--list', action='store_true'); ap.add_argument('--port', default='MIDI (OCTA-CAPTURE)')
    ap.add_argument('cmd', nargs='?'); ap.add_argument('args', nargs='*')
    a = ap.parse_args()
    if a.list:
        print('out:', mido.get_output_names()); print('in: ', mido.get_input_names()); return
    lab = Lab(a.port); x = [int(v, 16) for v in a.args if not v.endswith('.txt')]
    if a.cmd == 'ping':
        print('word 00 =', fmt(lab.rw(0)))
    elif a.cmd == 'dump':
        lines = ['W%02X %s' % (n, fmt(lab.rw(n))) for n in range(256)] + ['C%02d %06X' % (c, lab.rc(c)) for c in range(32)]
        print('\n'.join(lines))
        if a.args and a.args[-1].endswith('.txt'): open(a.args[-1], 'w').write('\n'.join(lines) + '\n')
    elif a.cmd == 'rw': print(fmt(lab.rw(x[0])))
    elif a.cmd == 'rc': print('%06X' % lab.rc(x[0]))
    elif a.cmd == 'ww': lab.ww(x[0], x[1]); print('W%02X <- %s  (read back %s)' % (x[0], fmt(x[1]), fmt(lab.rw(x[0]))))
    elif a.cmd == 'wc': lab.wc(x[0], x[1])
    elif a.cmd == 'mem': print('%04X: %02X %02X' % ((x[0] & ~1,) + tuple(lab.mem(x[0] & ~1))))
    elif a.cmd == 'keys':                       # dead-keys check (only side-effect-free registers are read)
        im, ip = lab.mem(0x08); ip1, im1 = lab.mem(0x12); w46 = lab.mem(0x46); r48 = lab.mem(0x48); r4e = lab.mem(0x4E)
        print('INT_MASK %02X INT_PEND %02X | INT_PEND1 %02X INT_MASK1 %02X  (keyboard INT13 = 0x20, MIDI = 0x03)' % (
              im, ip, ip1, im1))
        print('queue write R46 %02X%02X  read R48 %02X%02X  R4E %02X%02X  (high bytes must be 00)' % (
              w46[1], w46[0], r48[1], r48[0], r4e[1], r4e[0]))
    else: ap.print_help()


if __name__ == '__main__':
    main()
