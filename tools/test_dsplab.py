"""Offline check of the DSP LAB SysEx receiver on the built image (80C196 sim): bytes go into the MIDI RX ring,
the stock pump's call (LD R1A,#108C ; LCALL 1050) is replayed, RCC port writes and MIDI TX bytes are logged.
  py -3.10 test_dsplab.py build/D70_rosetta_v08_combined.bin"""
import sys
from sim import Sim
import dsplab as DL


class LabSim(Sim):
    def __init__(self, img):
        super().__init__(img)
        self.rcc = []; self.word = {}; self.cell = {}; self.data = [0, 0, 0]; self.out = bytearray()
        self.hooks[DL.TX] = lambda s: s.out.append(s.reg[0x1C]) or True    # stock send byte

    def rb(self, a):
        a &= 0xFFFF
        if 0x0A00 <= a <= 0x0A02: return self.cap[a - 0x0A00]
        return super().rb(a)

    def wb(self, a, v):
        a &= 0xFFFF; v &= 255
        if 0x0A00 <= a <= 0x0A0F:
            self.rcc.append((a & 15, v)); o = a & 15
            if o <= 2: self.data[o] = v
            val = self.data[0] << 16 | self.data[1] << 8 | self.data[2]
            if o == 6: self.word[v] = val
            if o == 4: self.cell[v] = val
            if o == 0x0A: self.cap = list(self.word.get(v, 0x123456).to_bytes(3, 'big'))
            if o == 0x08: self.cap = list(self.cell.get(v, 0xABCDEF).to_bytes(3, 'big'))
            return
        super().wb(a, v)

    def feed(self, bs):
        for b in bs:
            w = self.reg[0x40]; self.ram[w] = b; self.reg[0x40] = (w + 1) & 255

    def pump(self):
        """stock page-4 loop: call get-byte until no byte, collect what the stock parser would see"""
        seen = []
        for _ in range(200):
            self.reg[0x1A] = 0x8C; self.reg[0x1B] = 0x10
            self.c = False
            self.run(0x108C)                            # page 2 is in the window (stub 0x1050 did that)
            if not self.c: return seen
            seen.append(self.reg[0x1C])
        raise RuntimeError('pump did not stop')


def nib(*bs):
    out = []
    for b in bs: out += [b >> 4, b & 15]
    return out


def main():
    img = open(sys.argv[1] if len(sys.argv) > 1 else 'build/D70_rosetta_v08_combined.bin', 'rb').read()
    s = LabSim(img); s.bank = 2; s.ram[DL.LS_STATE - 0xC000] = 0; ok = True

    def check(name, cond):
        nonlocal ok; ok &= bool(cond); print('%-48s %s' % (name, 'OK' if cond else 'FAIL'))

    s.feed([0x90, 60, 100]); check('note on passes to stock', s.pump() == [0x90, 60, 100])
    s.feed([0xF0, 0x41, 0x10, 0x39, 0x12, 1, 2, 3, 0xF7])
    check('Roland SysEx passes untouched', s.pump() == [0xF0, 0x41, 0x10, 0x39, 0x12, 1, 2, 3, 0xF7])
    s.feed([0xF0, 0x7D, 0x4C, 1] + nib(0x9C, 0x12, 0x34, 0x56) + [0xF7])
    check('write word: stock sees F0 F7 only', s.pump() == [0xF0, 0xF7])
    check('write word 9C = 123456', s.word.get(0x9C) == 0x123456)
    s.feed([0xF0, 0x7D, 0x4C, 2] + nib(0x45, 0xFE, 0xDC, 0xBA) + [0xF7]); s.pump()
    check('write cell 5 (45 & 31) = FEDCBA', s.cell.get(5) == 0xFEDCBA)
    s.out.clear(); s.feed([0xF0, 0x7D, 0x4C, 3] + nib(0x9C) + [0xF7]); s.pump()
    check('read word 9C replies', bytes(s.out) == bytes([0xF0, 0x7D, 0x4C, 0x13] + nib(0x9C, 0x12, 0x34, 0x56) + [0xF7]))
    s.out.clear(); s.feed([0xF0, 0x7D, 0x4C, 4] + nib(5) + [0xF7]); s.pump()
    check('read cell 5 replies', bytes(s.out) == bytes([0xF0, 0x7D, 0x4C, 0x14] + nib(5, 0xFE, 0xDC, 0xBA) + [0xF7]))
    s.ram[0x0210] = 0x5A; s.ram[0x0211] = 0xA5; s.out.clear()
    s.feed([0xF0, 0x7D, 0x4C, 5] + nib(0x10, 0xC2) + [0xF7]); s.pump()
    check('read memory C210 replies its word', bytes(s.out) == bytes([0xF0, 0x7D, 0x4C, 0x15] + nib(0x10, 0xC2, 0x5A, 0xA5) + [0xF7]))
    s.feed([0xF0, 0x7D, 0x4C, 1, 3, 0x80, 64]); check('status inside ours aborts, goes to stock', s.pump() == [0xF0, 0xF7, 0x80, 64])
    s.feed([0x90, 61, 90]); check('notes still pass after abort', s.pump() == [0x90, 61, 90])
    check('state idle', s.ram[DL.LS_STATE - 0xC000] == 0)
    s.feed([0xF0, 0x7D] + [0x4C] * 40 + [0xF7, 0x80, 1, 2]); check('overlong message dropped safely', s.pump() == [0xF0, 0xF7, 0x80, 1, 2])
    print('ALL OK' if ok else 'FAILED'); sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
