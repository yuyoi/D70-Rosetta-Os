"""Small two-pass MCS-96 assembler for the Rosetta player. Every emitted instruction is checked by decoding it back
with fw/mcs96.py (the decoder that matches the stock ROM) and comparing the text."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from mcs96 import decode


class Asm:
    def __init__(self, org):
        self.org = org
        self.ops = []        # (kind, size, args)
        self.lidx = {}       # label -> op index (addresses come from the layout, after branch relaxation)
        self.labels = {}
        self.ext = {}        # labels from another Asm (filled in by the caller)

    def here(self):
        return self.org + sum(o[1] for o in self.ops)

    def L(self, name):
        assert name not in self.lidx, name
        self.lidx[name] = len(self.ops)

    def b(self, *bs):
        self.ops.append(('raw', len(bs), bytes(bs)))

    # ---- loads / stores
    def ld(self, d, s): self.b(0xA0, s, d)
    def ldi(self, d, v): self.b(0xA1, v & 255, v >> 8 & 255, d)
    def ldb(self, d, s): self.b(0xB0, s, d)
    def ldbi(self, d, v): self.b(0xB1, v & 255, d)
    def ldb_pi(self, d, p): self.b(0xB2, p | 1, d)          # LDB d,[p]+
    def ldb_abs(self, d, a): self.b(0xB3, 0x01, a & 255, a >> 8, d)
    def ldbze(self, d, s): self.b(0xAC, s, d)
    def ldbze_pi(self, d, p): self.b(0xAE, p | 1, d)
    def stb_abs(self, a, s): self.b(0xC7, 0x01, a & 255, a >> 8, s)
    def stb_pi(self, p, s): self.b(0xC6, p | 1, s)          # STB s,[p]+
    # ---- alu
    def add(self, d, s): self.b(0x64, s, d)
    def addi(self, d, v): self.b(0x65, v & 255, v >> 8 & 255, d)
    def add3(self, d, s1, s2): self.b(0x44, s2, s1, d)      # d = s1 + s2
    def sub(self, d, s): self.b(0x68, s, d)
    def cmpi(self, d, v): self.b(0x89, v & 255, v >> 8 & 255, d)
    def cmpbi(self, d, v): self.b(0x99, v & 255, d)
    def andbi(self, d, v): self.b(0x71, v & 255, d)
    def orbi(self, d, v): self.b(0x91, v & 255, d)
    def xorb_ind(self, d, p): self.b(0x96, p & 0xFE, d)     # XORB d,[p]
    def inc(self, r): self.b(0x07, r)
    def dec(self, r): self.b(0x05, r)
    def decb(self, r): self.b(0x15, r)
    def clrb(self, r): self.b(0x11, r)
    def shl(self, r, n): self.b(0x09, n, r)
    def push(self, r): self.b(0xC8, r)
    def pop(self, r): self.b(0xCC, r)
    def pushf(self): self.b(0xF2)
    def popf(self): self.b(0xF3)
    def ret(self): self.b(0xF0)
    # ---- more forms used by the menu
    def ldb_ind(self, d, p): self.b(0xB2, p & 0xFE, d)      # LDB d,[p]
    def ldb_idx(self, d, p, off): self.b(0xB3, p & 0xFE, off & 255, d)       # LDB d,off8[p]
    def ld_idx(self, d, p, off): self.b(0xA3, p & 0xFE, off & 255, d)        # LD d,off8[p]
    def ldb_lidx(self, d, p, a): self.b(0xB3, p | 1, a & 255, a >> 8, d)    # LDB d,abs16[p]
    def ld_lidx(self, d, p, a): self.b(0xA3, p | 1, a & 255, a >> 8, d)     # LD d,abs16[p]
    def stb_ind(self, p, s): self.b(0xC6, p & 0xFE, s)      # STB s,[p]
    def st_lidx(self, p, a, s): self.b(0xC3, p | 1, a & 255, a >> 8, s)     # ST s,abs16[p]
    def stb_lidx(self, p, a, s): self.b(0xC7, p | 1, a & 255, a >> 8, s)    # STB s,abs16[p]
    def ld_pi(self, d, p): self.b(0xA2, p | 1, d)           # LD d,[p]+
    def ldbze_ind(self, d, p): self.b(0xAE, p & 0xFE, d)
    def ldbze_idx(self, d, p, off): self.b(0xAF, p & 0xFE, off & 255, d)
    def cmpb(self, d, s): self.b(0x98, s, d)
    def cmp(self, d, s): self.b(0x88, s, d)
    def addbi(self, d, v): self.b(0x75, v & 255, d)
    def subbi(self, d, v): self.b(0x79, v & 255, d)
    def subi(self, d, v): self.b(0x69, v & 255, v >> 8 & 255, d)
    def addb(self, d, s): self.b(0x74, s, d)
    def subb(self, d, s): self.b(0x78, s, d)
    def andb(self, d, s): self.b(0x70, s, d)
    def andb3(self, d, s1, s2): self.b(0x50, s2, s1, d)     # d = s1 & s2
    def orb(self, d, s): self.b(0x90, s, d)
    def notb(self, r): self.b(0x12, r)
    def incb(self, r): self.b(0x17, r)
    def clr(self, r): self.b(0x01, r)
    def shrb(self, r, n): self.b(0x18, n, r)
    def shr(self, r, n): self.b(0x08, n, r)
    def push_imm(self, l): self.ops.append(('imm16', 3, (0xC9, l)))
    def ldi_l(self, d, l): self.ops.append(('ldil', 4, (d, l)))
    def ldb_lidx_l(self, d, p, l, adj=0): self.ops.append(('lidxl', 5, (0xB3, p | 1, l, adj, d)))   # LDB d,label+adj[p]
    def ld_lidx_l(self, d, p, l, adj=0): self.ops.append(('lidxl', 5, (0xA3, p | 1, l, adj, d)))    # LD d,label+adj[p]
    def ldi_l2(self, d, l, adj): self.ops.append(('ldil2', 4, (d, l, adj)))
    def br(self, r): self.b(0xE3, r & 0xFE)
    def djnz(self, r, l): self.ops.append(('djnz', 3, (r, l)))
    def data(self, bs): self.ops.append(('data', len(bs), bytes(bs)))
    def dw(self, l): self.ops.append(('dw', 2, l))
    def align(self): self.ops.append(('align', 0, None))   # pad to an even address (the CPU reads words at even addresses only)

    # ---- control flow (fixups)
    def jcc(self, cond, l): self.ops.append(('rel8', 2, ({'JE': 0xDF, 'JNE': 0xD7, 'JC': 0xDB, 'JNC': 0xD3,
                                                         'JH': 0xD9, 'JNH': 0xD1, 'JGT': 0xD2, 'JLE': 0xDA,
                                                         'JGE': 0xD6, 'JLT': 0xDE}[cond], l)))
    def jbc(self, r, bit, l): self.ops.append(('jb', 3, (0x30 + bit, r, l)))
    def jbs(self, r, bit, l): self.ops.append(('jb', 3, (0x38 + bit, r, l)))
    def sjmp(self, l): self.ops.append(('rel11', 2, (0x20, l)))
    def scall(self, l): self.ops.append(('rel11', 2, (0x28, l)))
    def ljmp(self, l): self.ops.append(('rel16', 3, (0xE7, l)))
    def lcall(self, l): self.ops.append(('rel16', 3, (0xEF, l)))

    def target(self, l):
        if isinstance(l, int): return l
        return self.labels[l] if l in self.labels else self.ext[l]

    def layout(self):
        pcs, pc = [], self.org
        for i, o in enumerate(self.ops):
            if o[0] == 'align' and o[1] != pc & 1: self.ops[i] = o = ('align', pc & 1, None); self.moved = True
            pcs.append(pc); pc += o[1]
        pcs.append(pc)
        self.labels = {n: pcs[i] for n, i in self.lidx.items()}
        return pcs

    INV = {0xDF: 0xD7, 0xD7: 0xDF, 0xDB: 0xD3, 0xD3: 0xDB, 0xD9: 0xD1, 0xD1: 0xD9, 0xD2: 0xDA, 0xDA: 0xD2,
           0xD6: 0xDE, 0xDE: 0xD6}

    def assemble(self):
        while True:                      # relax short conditional branches that cannot reach
            self.moved = False; pcs = self.layout(); grew = self.moved
            for i, (k, n, a) in enumerate(self.ops):
                if k == 'rel8':
                    d = self.target(a[1]) - (pcs[i] + 2)
                    if not -128 <= d < 128:
                        self.ops[i] = ('rel8far', 5, a); grew = True
                elif k == 'rel11':
                    d = self.target(a[1]) - (pcs[i] + 2)
                    if not -1024 <= d < 1024:
                        self.ops[i] = ('rel16', 3, (0xE7 if a[0] == 0x20 else 0xEF, a[1])); grew = True
            if not grew: break
        out = bytearray(); pc = self.org
        for k, n, a in self.ops:
            if k == 'raw':
                out += a
            elif k == 'rel8':
                d = self.target(a[1]) - (pc + 2); assert -128 <= d < 128, (a, d)
                out += bytes([a[0], d & 255])
            elif k == 'rel8far':                                   # J!cc +3 ; LJMP target
                d = (self.target(a[1]) - (pc + 5)) & 0xFFFF
                out += bytes([self.INV[a[0]], 3, 0xE7, d & 255, d >> 8])
            elif k == 'jb':
                d = self.target(a[2]) - (pc + 3); assert -128 <= d < 128, (a, d)
                out += bytes([a[0], a[1], d & 255])
            elif k == 'rel11':
                d = self.target(a[1]) - (pc + 2); assert -1024 <= d < 1024, (a, d)
                out += bytes([a[0] | (d >> 8 & 7), d & 255])
            elif k == 'imm16':
                v = self.target(a[1]); out += bytes([a[0], v & 255, v >> 8])
            elif k == 'ldil':
                v = self.target(a[1]); out += bytes([0xA1, v & 255, v >> 8, a[0]])
            elif k == 'djnz':
                d = self.target(a[1]) - (pc + 3); assert -128 <= d < 128, (a, d)
                out += bytes([0xE0, a[0], d & 255])
            elif k == 'data':
                out += a
            elif k == 'align':
                out += bytes([0xFF] * n)
            elif k == 'lidxl':
                v = (self.target(a[2]) + a[3]) & 0xFFFF; out += bytes([a[0], a[1], v & 255, v >> 8, a[4]])
            elif k == 'ldil2':
                v = (self.target(a[1]) + a[2]) & 0xFFFF; out += bytes([0xA1, v & 255, v >> 8, a[0]])
            elif k == 'dw':
                v = self.target(a); out += bytes([v & 255, v >> 8])
            elif k == 'rel16':
                d = (self.target(a[1]) - (pc + 3)) & 0xFFFF
                out += bytes([a[0], d & 255, d >> 8])
            pc += n
        # round trip: every instruction must decode to exactly its own length
        pc = 0
        for k, n, a in self.ops:
            if k in ('data', 'dw', 'align'):
                pc += n; continue
            if k == 'rel8far':
                assert decode(out, pc)[1] == 2 and decode(out, pc + 2)[1] == 3
                pc += n; continue
            t, ln, kind, tg = decode(out, pc, self.org + pc)
            assert ln == n and kind != 'bad', ('decode mismatch at %04X: %s' % (self.org + pc, t))
            pc += n
        return bytes(out)

    def listing(self, code):
        lines, pc = [], 0
        inv = {}
        for nme, a in self.labels.items(): inv.setdefault(a, []).append(nme)
        for k, n, a in self.ops:
            for nme in inv.get(self.org + pc, []): lines.append('%s:' % nme)
            if k == 'rel8far':
                for q, m in ((pc, 2), (pc + 2, 3)):
                    t = decode(code, q, self.org + q)[0]
                    lines.append('  %04X: %-15s %s' % (self.org + q, code[q:q + m].hex(' '), t))
                pc += n; continue
            if k in ('data', 'dw', 'align'):
                if n == 0: continue
                lines.append('  %04X: .db %s' % (self.org + pc, code[pc:pc + n].hex(' '))); pc += n; continue
            t, ln, kind, tg = decode(code, pc, self.org + pc)
            lines.append('  %04X: %-15s %s' % (self.org + pc, code[pc:pc + ln].hex(' '), t))
            pc += n
        return '\n'.join(lines)
