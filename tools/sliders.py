"""Common sliders for the Roland D-70: each part slider drives ONE parameter on all four layers.

  LOWER 1 -> CUTOFF   LOWER 2 -> RESONANCE   UPPER 3 -> ATTACK   UPPER 4 -> RELEASE

Stock play-mode slider loop (v1.11, fixed ROM): 0x4CB6 walks parts R58 = 0..3 and, for the selected control
button (R65 = key - 0x29: LEVEL 0, PAN 1, TUNING 2, CUTOFF 3, RESONANCE 4, ATTACK 5, RELEASE 6, fine tune 7),
applies part slider D39C[part] to that part: pickup state D3AA, last value D3A6, scale 0x4DC9 (R1C in/out),
write 0x4E32 (R20 = value, into the layer buffer D66B + R5F*0x42 + R58*0x0B).
The hook replaces the first 3 bytes of 0x4CB6 with LJMP. Mode D3B5 = 1 (four layers): for each slider whose
value changed, R65 = its control and the value goes to layers 0-3 through the stock scale + write. Any other
mode runs the stock code (the two instructions the LJMP covers, then back to 0x4CBD).
No pickup: a slider takes over as soon as it moves.
"""
from asm import Asm

HOOK = 0x4CB6
HOOK_OLD = bytes([0x11, 0x58, 0xB3, 0x01, 0xB5, 0xD3, 0x1C])   # CLRB R58 ; LDB R1C,D3B5[ZERO]
RESUME = 0x4CBD
SCALE, WRITE = 0x4DC9, 0x4E32
D3B5, D39C, D3A6 = 0xD3B5, 0xD39C, 0xD3A6
CONTROLS = [3, 4, 5, 6]          # slider 1-4 -> CUTOFF, RESONANCE, ATTACK, RELEASE
R1C, R20, R2C, S, VAL, R58, R65 = 0x1C, 0x20, 0x2C, 0x2E, 0x2F, 0x58, 0x65


def build(org, sltab=0):
    """sltab = address of the control table (pass 0 first, then the label from that build)"""
    a = Asm(org)
    J = a.jcc
    a.L('sl_hook')
    a.ldb_abs(R1C, D3B5); a.cmpbi(R1C, 1); J('JE', 'ours')
    a.clrb(R58); a.ljmp(RESUME)                         # stock path, R1C already = D3B5
    a.L('ours')
    a.push(R65 - 1); a.push(S); a.push(R58)            # word pushes: R64/R65, R2E/R2F, R58/R59
    a.ldbi(S, 4)                                       # S = slider + 1 (4..1), so the bases are one lower
    a.L('sl')
    a.ldbze(R2C, S); a.ldb_lidx(VAL, R2C, D39C - 1)
    a.ldb_lidx(R1C, R2C, D3A6 - 1); a.cmpb(R1C, VAL); J('JE', 'next')
    a.stb_lidx(R2C, D3A6 - 1, VAL)
    a.ldb_lidx(R65, R2C, (sltab - 1) & 0xFFFF)
    a.clrb(R58)
    a.L('pl'); a.ldb(R1C, VAL); a.lcall(SCALE); a.ldb(R20, R1C); a.lcall(WRITE)
    a.incb(R58); a.cmpbi(R58, 4); J('JNE', 'pl')
    a.L('next'); a.djnz(S, 'sl')
    a.pop(R58); a.pop(S); a.pop(R65 - 1); a.ret()
    a.L('sltab'); a.data(CONTROLS)
    return a
