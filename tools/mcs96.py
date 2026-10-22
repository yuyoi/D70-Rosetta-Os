#!/usr/bin/env python3
"""mcs96.py - MCS-96 (8xC196KB) disassembler, written from the Intel opcode map.
Decoder only; verification is done by recursive-descent consistency on the D-70 ROM (see trace.py).
Operand byte order in the stream: [mode operand(s)] [source reg(s)] [dest reg]; for ST the memory operand comes first.
"""

SFR = {0x00: 'ZERO', 0x02: 'AD_CMD/RES', 0x03: 'AD_RES_H', 0x07: 'SBUF', 0x08: 'INT_MASK', 0x09: 'INT_PEND',
       0x0E: 'PORT0/BAUD', 0x0F: 'PORT1', 0x10: 'PORT2', 0x11: 'SP_STAT/CON', 0x18: 'SP'}


def reg(n):
    return SFR.get(n, 'R%02X' % n)


def s8(b):
    return b - 256 if b > 127 else b


def s16(v):
    return v - 65536 if v > 32767 else v


COND = {0xD0: 'JNST', 0xD1: 'JNH', 0xD2: 'JGT', 0xD3: 'JNC', 0xD4: 'JNVT', 0xD5: 'JNV', 0xD6: 'JGE', 0xD7: 'JNE',
        0xD8: 'JST', 0xD9: 'JH', 0xDA: 'JLE', 0xDB: 'JC', 0xDC: 'JVT', 0xDD: 'JV', 0xDE: 'JLT', 0xDF: 'JE'}

# (name, base opcode, nops, size)  size 'w' or 'b'; nops 3 or 2.  4 addressing modes at base+0..3
ALU = [('AND', 0x40, 3, 'w'), ('ADD', 0x44, 3, 'w'), ('SUB', 0x48, 3, 'w'), ('MULU', 0x4C, 3, 'w'),
       ('ANDB', 0x50, 3, 'b'), ('ADDB', 0x54, 3, 'b'), ('SUBB', 0x58, 3, 'b'), ('MULUB', 0x5C, 3, 'b'),
       ('AND', 0x60, 2, 'w'), ('ADD', 0x64, 2, 'w'), ('SUB', 0x68, 2, 'w'), ('MULU', 0x6C, 2, 'w'),
       ('ANDB', 0x70, 2, 'b'), ('ADDB', 0x74, 2, 'b'), ('SUBB', 0x78, 2, 'b'), ('MULUB', 0x7C, 2, 'b'),
       ('OR', 0x80, 2, 'w'), ('XOR', 0x84, 2, 'w'), ('CMP', 0x88, 2, 'w'), ('DIVU', 0x8C, 2, 'w'),
       ('ORB', 0x90, 2, 'b'), ('XORB', 0x94, 2, 'b'), ('CMPB', 0x98, 2, 'b'), ('DIVUB', 0x9C, 2, 'b'),
       ('LD', 0xA0, 2, 'w'), ('ADDC', 0xA4, 2, 'w'), ('SUBC', 0xA8, 2, 'w'), ('LDBZE', 0xAC, 2, 'wb'),
       ('LDB', 0xB0, 2, 'b'), ('ADDCB', 0xB4, 2, 'b'), ('SUBCB', 0xB8, 2, 'b'), ('LDBSE', 0xBC, 2, 'wb')]
ALUMAP = {}
for n, b, k, sz in ALU:
    for m in range(4):
        ALUMAP[b + m] = (n, m, k, sz)

ONEREG = {0x01: 'CLR', 0x02: 'NOT', 0x03: 'NEG', 0x05: 'DEC', 0x06: 'EXT', 0x07: 'INC',
          0x11: 'CLRB', 0x12: 'NOTB', 0x13: 'NEGB', 0x15: 'DECB', 0x16: 'EXTB', 0x17: 'INCB'}
SHIFT = {0x08: 'SHR', 0x09: 'SHL', 0x0A: 'SHRA', 0x0C: 'SHRL', 0x0D: 'SHLL', 0x0E: 'SHRAL', 0x0F: 'NORML',
         0x18: 'SHRB', 0x19: 'SHLB', 0x1A: 'SHRAB'}
NOARG = {0xF0: 'RET', 0xF2: 'PUSHF', 0xF3: 'POPF', 0xF4: 'PUSHA', 0xF5: 'POPA', 0xF8: 'CLRC', 0xF9: 'SETC',
         0xFA: 'DI', 0xFB: 'EI', 0xFC: 'CLRVT', 0xFD: 'NOP', 0xFF: 'RST'}


def mode_operand(d, p, mode, size):
    """returns (text, nbytes consumed from p). mode 0 direct,1 imm,2 indirect,3 indexed"""
    if mode == 0:
        return reg(d[p]), 1
    if mode == 1:
        if size == 'b':
            return '#%02X' % d[p], 1
        return '#%04X' % (d[p] | d[p + 1] << 8), 2
    if mode == 2:
        r = d[p]
        return '[%s]%s' % (reg(r & 0xFE), '+' if r & 1 else ''), 1
    r = d[p]
    if r & 1:
        off = d[p + 1] | d[p + 2] << 8
        return '%04X[%s]' % (off, reg(r & 0xFE)), 3
    return '%02X[%s]' % (d[p + 1], reg(r & 0xFE)), 2


def decode(d, p, pc=None):
    """d: bytes, p: index, pc: address used for relative targets (default p). Returns (text, length, kind, target)
    kind in {None,'jmp','cjmp','call','ret','stop'}; target absolute 16-bit or None."""
    if pc is None:
        pc = p
    op = d[p]
    signed = False
    q = p
    if op == 0xFE:
        signed = True
        q = p + 1
        op = d[q]
    L = lambda n: n + (1 if signed else 0)
    if op in ALUMAP:
        name, mode, k, sz = ALUMAP[op]
        if signed:
            if name not in ('MULU', 'MULUB', 'DIVU', 'DIVUB'):
                return ('.db %02X' % d[p], 1, None, None)
            name = name.replace('U', '', 1)
        sz1 = 'b' if sz in ('b', 'wb') else 'w'
        t, n = mode_operand(d, q + 1, mode, sz1)
        nxt = q + 1 + n
        if k == 3:
            src1, dst = reg(d[nxt]), reg(d[nxt + 1])
            return ('%-6s %s, %s, %s' % (name, dst, src1, t), (nxt + 2) - p, None, None)
        dst = reg(d[nxt])
        return ('%-6s %s, %s' % (name, dst, t), (nxt + 1) - p, None, None)
    if op in (0xC0, 0xC2, 0xC3, 0xC4, 0xC6, 0xC7):  # ST / STB
        name = 'ST' if op < 0xC4 else 'STB'
        mode = {0: 0, 2: 2, 3: 3}[op & 3]
        t, n = mode_operand(d, q + 1, mode, 'w')
        s = reg(d[q + 1 + n])
        return ('%-6s %s, %s' % (name, t, s), q + 2 + n - p, None, None)
    if op in (0xC8, 0xC9, 0xCA, 0xCB):
        t, n = mode_operand(d, q + 1, op & 3, 'w')
        return ('%-6s %s' % ('PUSH', t), q + 1 + n - p, None, None)
    if op in (0xCC, 0xCE, 0xCF):
        t, n = mode_operand(d, q + 1, op & 3, 'w')
        return ('%-6s %s' % ('POP', t), q + 1 + n - p, None, None)
    if op in ONEREG:
        return ('%-6s %s' % (ONEREG[op], reg(d[q + 1])), q + 2 - p, None, None)
    if op in SHIFT:
        c = d[q + 1]
        cnt = '#%d' % c if c < 16 else reg(c)
        return ('%-6s %s, %s' % (SHIFT[op], reg(d[q + 2]), cnt), q + 3 - p, None, None)
    if op == 0x00:
        return ('SKIP   %02X' % d[q + 1], q + 2 - p, None, None)
    if 0x20 <= op <= 0x2F:
        disp = ((op & 7) << 8) | d[q + 1]
        if disp & 0x400:
            disp -= 0x800
        tgt = (pc + (q - p) + 2 + disp) & 0xFFFF
        return ('%-6s %04X' % ('SJMP' if op < 0x28 else 'SCALL', tgt), q + 2 - p, 'jmp' if op < 0x28 else 'call', tgt)
    if 0x30 <= op <= 0x3F:
        tgt = (pc + (q - p) + 3 + s8(d[q + 2])) & 0xFFFF
        return ('%-6s %s.%d, %04X' % ('JBC' if op < 0x38 else 'JBS', reg(d[q + 1]), op & 7, tgt), q + 3 - p, 'cjmp', tgt)
    if op in COND:
        tgt = (pc + (q - p) + 2 + s8(d[q + 1])) & 0xFFFF
        return ('%-6s %04X' % (COND[op], tgt), q + 2 - p, 'cjmp', tgt)
    if op == 0xE0:
        tgt = (pc + (q - p) + 3 + s8(d[q + 2])) & 0xFFFF
        return ('DJNZ   %s, %04X' % (reg(d[q + 1]), tgt), q + 3 - p, 'cjmp', tgt)
    if op == 0xE7 or op == 0xEF:
        disp = s16(d[q + 1] | d[q + 2] << 8)
        tgt = (pc + (q - p) + 3 + disp) & 0xFFFF
        return ('%-6s %04X' % ('LJMP' if op == 0xE7 else 'LCALL', tgt), q + 3 - p, 'jmp' if op == 0xE7 else 'call', tgt)
    if op == 0xE3:
        return ('BR     [%s]' % reg(d[q + 1] & 0xFE), q + 2 - p, 'stop', None)
    if op in NOARG:
        k = 'ret' if op == 0xF0 else ('stop' if op == 0xFF else None)
        return (NOARG[op], q + 1 - p, k, None)
    if op == 0xF7:
        return ('IDLPD  #%02X' % d[q + 1], q + 2 - p, None, None)
    if op == 0x0B:
        return ('XCH    %s, %s' % (reg(d[q + 2]), reg(d[q + 1])), q + 3 - p, None, None)
    if op == 0x1B:
        return ('XCHB   %s, %s' % (reg(d[q + 2]), reg(d[q + 1])), q + 3 - p, None, None)
    return ('.db %02X' % d[p], 1, 'bad', None)


if __name__ == '__main__':
    import sys
    sys.path.insert(0, '.')
    import romload
    c = romload.load_v111()
    p = 0x1000
    for _ in range(40):
        t, n, k, tg = decode(c, p)
        print('%04X: %-12s %s' % (p, c[p:p + n].hex(' '), t))
        p += n
