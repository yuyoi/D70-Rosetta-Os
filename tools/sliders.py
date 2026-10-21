"""Common sliders for the Roland D-70: each part slider drives ONE parameter on all four layers.

  LOWER 1 -> CUTOFF   LOWER 2 -> RESONANCE   UPPER 3 -> ATTACK   UPPER 4 -> RELEASE

Stock play-mode slider loop (v1.11, fixed ROM): 0x4CB6 walks parts R58 = 0..3 and, for the selected control
button (R65 = key - 0x29: LEVEL 0, PAN 1, TUNING 2, CUTOFF 3, RESONANCE 4, ATTACK 5, RELEASE 6, fine tune 7),
applies part slider D39C[part] to that part: pickup state D3AA, last value D3A6, scale 0x4DC9 (R1C in/out),
write 0x4E32 (R20 = value, into the layer buffer D66B + R5F*0x42 + R58*0x0B).

0x4CB6 gets LJMP to a 7-byte trampoline in fixed ROM (LD R1A,#hook ; LJMP 105F), which runs the hook on ROM
page 7 (the stub saves the bank and restores it on RET). Mode D3B5 = 1 (four layers): for each slider whose value
changed, R65 = its control and the value goes to layers 0-3 through the stock scale + write. Any other mode runs
the stock code: the two instructions the LJMP covers, then the rest of the stock loop as a call.
No pickup: a slider takes over as soon as it moves.
"""

HOOK = 0x4CB6
HOOK_OLD = bytes([0x11, 0x58, 0xB3, 0x01, 0xB5, 0xD3, 0x1C])   # CLRB R58 ; LDB R1C,D3B5[ZERO]
RESUME = 0x4CBD
SCALE, WRITE = 0x4DC9, 0x4E32
P2 = 0x1050                      # far stub: ROM page 2 (stock's home page) in the window, BR [R1A]
D3B5, D39C, D3A6 = 0xD3B5, 0xD39C, 0xD3A6
CONTROLS = [3, 4, 5, 6]          # slider 1-4 -> CUTOFF, RESONANCE, ATTACK, RELEASE
R1A, R1C, R20, R2C, S, VAL, R58, R65 = 0x1A, 0x1C, 0x20, 0x2C, 0x2E, 0x2F, 0x58, 0x65


def emit(a):
    """slider hook into Asm a (ROM page 7); entry label 'sl_hook'"""
    J = a.jcc
    a.L('sl_hook')
    a.ldb_abs(R1C, D3B5); a.cmpbi(R1C, 1); J('JE', 'sl_ours')
    a.clrb(R58); a.ldi(R1A, RESUME); a.lcall(P2); a.ret()   # stock path, R1C already = D3B5
    a.L('sl_ours')
    a.push(R65 - 1); a.push(S); a.push(R58)            # word pushes: R64/R65, R2E/R2F, R58/R59
    a.ldbi(S, 4)                                       # S = slider + 1 (4..1), so the bases are one lower
    a.L('sl')
    a.ldbze(R2C, S); a.ldb_lidx(VAL, R2C, D39C - 1)
    a.ldb_lidx(R1C, R2C, D3A6 - 1); a.cmpb(R1C, VAL); J('JE', 'sl_next')
    a.stb_lidx(R2C, D3A6 - 1, VAL)
    a.ldb_lidx_l(R65, R2C, 'sltab', -1)
    a.clrb(R58)
    a.L('sl_pl'); a.ldb(R1C, VAL); a.ldi(R1A, SCALE); a.lcall(P2)
    a.ldb(R20, R1C); a.ldi(R1A, WRITE); a.lcall(P2)
    a.incb(R58); a.cmpbi(R58, 4); J('JNE', 'sl_pl')
    a.L('sl_next'); a.djnz(S, 'sl')
    a.pop(R58); a.pop(S); a.pop(R65 - 1); a.ret()
    a.L('sltab'); a.data(CONTROLS)
