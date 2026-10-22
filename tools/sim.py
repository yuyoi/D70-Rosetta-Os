"""Check-only MCS-96 simulator: runs the player straight from the patched ROM image. Each instruction is decoded by
fw/mcs96.py and executed from that TEXT, so an encoding mistake in asm.py shows up as wrong behaviour here.
Models: registers, ROM pages (bank reg 0x0100), RAM1, a T6963C (address pointer, auto write, C5 read, display mode,
status always ready), TIMER1 at 0x0A (+1 per instruction). Records the LCD graphics each time hold_pc is reached."""
import os, sys, re
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from mcs96 import decode, SFR

NAME = {v: k for k, v in SFR.items()}


class LCD:
    def __init__(self):
        self.ram = bytearray(0x2000); self.adp = 0; self.args = []; self.auto = None; self.mode = None
        self.rd = 0; self.log = []; self.homes = {}

    def data_w(self, v):
        if self.auto == 'w':
            self.ram[self.adp & 0x1FFF] = v; self.adp += 1
        else:
            self.args.append(v)

    def cmd_w(self, c):
        if c == 0x24:
            self.adp = self.args[-2] | self.args[-1] << 8
        elif 0x40 <= c <= 0x43:
            self.homes[c] = self.args[-2] | self.args[-1] << 8
        elif c == 0xB0:
            self.auto = 'w'
        elif c == 0xB2:
            self.auto = None
        elif c == 0xC5:
            self.rd = self.ram[self.adp & 0x1FFF]; self.adp += 1
        elif c in (0xC0, 0xC4):                        # data write, + increment / address unchanged
            self.ram[self.adp & 0x1FFF] = self.args[-1]
            if c == 0xC0: self.adp += 1
        elif c & 0xF0 == 0x90:
            self.mode = c
        elif c & 0xF0 == 0xF0:                         # bit set / reset: the D-70's T6963C ignores it (hw + MAME)
            raise RuntimeError('LCD bit set/reset %02X: does nothing on the D-70 panel, use read-modify-write' % c)
        else:
            raise RuntimeError('unexpected LCD cmd %02X' % c)
        self.log.append(c); self.args = []


class Sim:
    def __init__(self, img, ram_fill=0x5A, hold_pc=None, tick=1):
        self.hooks = {}; self.tick = tick; self.tx = bytearray()
        self.img = img; self.reg = bytearray(256); self.ram = bytearray([ram_fill]) * 0x4000
        self.bank = 2; self.lcd = LCD(); self.t1 = 0; self.c = self.z = self.lt = False
        self.frames = []; self.hold_pc = hold_pc
        self.ic6 = bytes((i * 7 + (i >> 8)) & 255 for i in range(0x8000))

    def rb(self, a):
        a &= 0xFFFF
        if a < 0x100:
            if a == 0x0A: return self.t1 & 255
            if a == 0x0B: return self.t1 >> 8 & 255
            return self.reg[a]
        if a == 0x0802: return 0xFF
        if a == 0x0800: return self.lcd.rd
        if 0x1000 <= a < 0x8000: return self.img[a]
        if 0x8000 <= a < 0xC000:
            if self.bank & 0x20: return self.ic6[(self.bank & 1) * 0x4000 + a - 0x8000]
            return self.img[(self.bank & 7) * 0x4000 + a - 0x8000]
        if a >= 0xC000: return self.ram[a - 0xC000]
        raise RuntimeError('read %04X' % a)

    def wb(self, a, v):
        a &= 0xFFFF; v &= 255
        if a < 0x100:
            self.reg[a] = v
            if a == 0x07: self.tx.append(v)
        elif a == 0x0100: self.bank = v
        elif a == 0x0800: self.lcd.data_w(v)
        elif a == 0x0802: self.lcd.cmd_w(v)
        elif a >= 0xC000: self.ram[a - 0xC000] = v
        else: raise RuntimeError('write %04X' % a)

    def rw(self, a): return self.rb(a) | self.rb(a + 1) << 8
    def ww(self, a, v): self.wb(a, v); self.wb(a + 1, v >> 8)
    def push(self, v): sp = (self.rw(0x18) - 2) & 0xFFFF; self.ww(0x18, sp); self.ww(sp, v)
    def pop(self): sp = self.rw(0x18); v = self.rw(sp); self.ww(0x18, sp + 2); return v

    def regno(self, s):
        return NAME[s] if s in NAME else int(s[1:], 16)

    def opnd(self, s, w):
        """-> (getter, setter) for one operand's text"""
        rd, wr = (self.rw, self.ww) if w else (self.rb, self.wb)
        m = re.fullmatch(r'#([0-9A-F]+)', s)
        if m:
            v = int(m.group(1), 16)
            return (lambda: v), None
        m = re.fullmatch(r'\[(\w+)\](\+?)', s)
        if m:
            r = self.regno(m.group(1)); inc = 2 if w else 1; post = bool(m.group(2))
            def g():
                ad = self.rw(r); assert not (w and ad & 1), 'word read at odd address %04X' % ad
                v = rd(ad)
                if post: self.ww(r, ad + inc)
                return v
            def st(v):
                ad = self.rw(r); wr(ad, v)
                if post: self.ww(r, ad + inc)
            return g, st
        m = re.fullmatch(r'([0-9A-F]+)\[(\w+)\]', s)
        if m:
            ad = (int(m.group(1), 16) + self.rw(self.regno(m.group(2)))) & 0xFFFF
            assert not (w and ad & 1), 'word access at odd address %04X' % ad
            return (lambda: rd(ad)), (lambda v: wr(ad, v))
        r = self.regno(s)
        return (lambda: rd(r)), (lambda v: wr(r, v))

    def run(self, pc, sp=0xFF00, limit=30_000_000):
        self.ww(0x18, sp)
        self.push(0xDEAD)                              # fake return address
        start_sp = self.rw(0x18) + 2
        for n in range(limit):
            self.t1 = (self.t1 + self.tick) & 0xFFFF
            if pc in self.hooks:
                if self.hooks[pc](self):
                    pc = self.pop()
                    if pc == 0xDEAD: return n
                    continue
            buf = bytes(self.rb(pc + i) for i in range(6))
            t, ln, kind, tg = decode(buf, 0, pc)
            self.last = (pc, t)
            mn = t.split()[0]
            ops = [x.strip() for x in t[len(mn):].split(',')] if len(t) > len(mn) else []
            nxt = (pc + ln) & 0xFFFF
            w = not (mn.endswith('B') and mn != 'SUB')      # SUB is a word op
            if pc == self.hold_pc:
                self.frames.append(bytes(self.lcd.ram[0:2560]))
            if mn in ('DI', 'EI', 'NOP'):
                pass
            elif mn in ('SETC', 'CLRC'):
                self.c = mn == 'SETC'
            elif mn == 'PUSHF':
                self.push(self.reg[0x08] << 8 | int(self.c)); self.reg[0x08] = 0
            elif mn == 'POPF':
                v = self.pop(); self.reg[0x08] = v >> 8
            elif mn == 'PUSH':
                self.push(self.opnd(ops[0], True)[0]())
            elif mn == 'POP':
                self.opnd(ops[0], True)[1](self.pop())
            elif mn in ('LD', 'LDB'):
                self.opnd(ops[0], w)[1](self.opnd(ops[1], w)[0]())
            elif mn == 'LDBZE':
                self.opnd(ops[0], True)[1](self.opnd(ops[1], False)[0]())
            elif mn in ('ST', 'STB'):
                self.opnd(ops[0], w)[1](self.opnd(ops[1], w)[0]())
            elif mn in ('MULU', 'MULUB', 'DIVUB'):
                if mn == 'MULU' and len(ops) == 2:          # long dst = dst word * src word
                    d = self.regno(ops[0]); r = self.rw(d) * self.opnd(ops[1], True)[0]()
                    assert d % 4 == 0, 'MULU long result at R%02X: the CPU needs a 4-aligned register' % d
                    self.ww(d, r & 0xFFFF); self.ww(d + 2, r >> 16)
                elif mn == 'MULUB' and len(ops) == 3:       # word dst = byte * byte
                    d = self.regno(ops[0]); r = self.opnd(ops[1], False)[0]() * self.opnd(ops[2], False)[0]()
                    self.ww(d, r)
                elif mn == 'MULUB':                         # word dst = dst byte * src byte
                    d = self.regno(ops[0]); r = self.rb(d) * self.opnd(ops[1], False)[0]()
                    self.ww(d, r)
                else:                                       # DIVUB: word dst / byte -> lo quotient, hi remainder
                    d = self.regno(ops[0]); n = self.rw(d); v = self.opnd(ops[1], False)[0]()
                    self.wb(d, n // v); self.wb(d + 1, n % v)
            elif mn in ('ADD', 'ADDB', 'SUB', 'SUBB', 'CMP', 'CMPB', 'ANDB', 'ORB', 'XORB'):
                msk = 0xFFFF if w else 0xFF
                g, st = self.opnd(ops[0], w)
                if len(ops) == 3:
                    a_, b_ = self.opnd(ops[1], w)[0](), self.opnd(ops[2], w)[0]()
                else:
                    b_ = self.opnd(ops[1], w)[0](); a_ = g()
                if mn.startswith('ADD'): r = a_ + b_; self.c = r > msk
                elif mn[:3] in ('SUB', 'CMP'):
                    r = a_ - b_; self.c = a_ >= b_
                    sb = 0x8000 if w else 0x80
                    self.lt = (a_ - (a_ & sb) * 2) < (b_ - (b_ & sb) * 2)
                elif mn == 'ANDB': r = a_ & b_
                elif mn == 'ORB': r = a_ | b_
                else: r = a_ ^ b_
                r &= msk; self.z = r == 0
                if not mn.startswith('CMP'): st(r)
            elif mn == 'DJNZ':
                r = self.regno(ops[0]); v = (self.reg[r] - 1) & 255; self.reg[r] = v
                if v: nxt = tg
            elif mn == 'BR':
                nxt = self.rw(self.regno(ops[0].strip('[]')))
            elif mn == 'NOTB':
                r = self.regno(ops[0]); v = ~self.reg[r] & 255; self.reg[r] = v; self.z = v == 0
            elif mn == 'NEGB':
                r = self.regno(ops[0]); a_ = self.reg[r]; v = -a_ & 255; self.reg[r] = v
                self.z = v == 0; self.c = a_ == 0
            elif mn in ('INC', 'DEC', 'DECB', 'CLRB', 'SHL', 'INCB', 'CLR', 'SHRB', 'SHR'):
                g, st = self.opnd(ops[0], w); msk = 0xFFFF if w else 0xFF
                v = g()
                if mn in ('SHL',): v <<= int(ops[1][1:])
                elif mn in ('SHR', 'SHRB'): v >>= int(ops[1][1:])
                else: v = {'INC': v + 1, 'DEC': v - 1, 'DECB': v - 1, 'CLRB': 0, 'INCB': v + 1, 'CLR': 0}[mn]
                v &= msk; st(v); self.z = v == 0
            elif mn in ('JE', 'JNE', 'JC', 'JNC', 'JH', 'JNH', 'JGT', 'JLE', 'JGE', 'JLT'):
                take = {'JE': self.z, 'JNE': not self.z, 'JC': self.c, 'JNC': not self.c,
                        'JH': self.c and not self.z, 'JNH': (not self.c) or self.z,
                        'JLT': self.lt, 'JGE': not self.lt, 'JGT': not self.lt and not self.z,
                        'JLE': self.lt or self.z}[mn]
                if take: nxt = tg
            elif mn in ('JBC', 'JBS'):
                r, bit = ops[0].split('.'); bit = int(bit)
                v = self.rb(self.regno(r)) >> bit & 1
                if (v == 0) == (mn == 'JBC'): nxt = tg
            elif mn in ('SJMP', 'LJMP'):
                nxt = tg
            elif mn in ('SCALL', 'LCALL'):
                self.push(nxt); nxt = tg
            elif mn == 'RET':
                nxt = self.pop()
                if nxt == 0xDEAD:
                    assert self.rw(0x18) == start_sp, 'stack not balanced'
                    return n
            else:
                raise RuntimeError('unhandled %04X %s' % (pc, t))
            pc = nxt
        raise RuntimeError('did not return')
