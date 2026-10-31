"""DSP LAB: live access to the effects chip (IC23, RCC) over MIDI SysEx, for experiments driven from a PC.

Every incoming MIDI byte goes through the fixed jump table entry 0x108C (LJMP 0xBE2C, page 2: C = byte in R1C from
the RX ring 0xC000[R42], R40 = write index). The page-4 pump (0xB491) and the bulk-load waits (page 5 0x97FA /
0x9CCF) call it through the page-2 far stub 0x1050, so page 2 is in the window. 0x108C is retargeted to lab_get:
  F0 7D ...        the stock parser gets F0 F7 (an empty SysEx, ignored); the rest is collected here
  F0 7D 4C cmd <nibbles> F7      (4C = 'L')
    cmd 1  write parameter word  : addr(2 nib) value(6 nib, hi first)       -> STB 0A00-0A02, STB 0A06 <- addr
    cmd 2  write RAM A cell      : cell(2 nib) value(6 nib)                 -> STB 0A00-0A02, STB 0A04 <- cell
    cmd 3  read parameter word   : addr(2 nib)    reply F0 7D 4C 13 addr(2) value(6) F7   (STB 0A0A, LDB 0A00-0A02)
    cmd 4  read RAM A cell       : cell(2 nib)    reply F0 7D 4C 14 cell(2) value(6) F7   (STB 0A08)
    cmd 5  read CPU word         : addr lo, hi (4 nib, even)  reply F0 7D 4C 15 lo hi byte byte F7  (registers, RAM,
           chip ports; careful: reading SP_STAT 0x11 / IOS1 0x16 clears flags, 0x0608/0x0609 takes a pending key)
Replies go out through the stock MIDI send byte (0x1089 -> page 2 0xBE4B). A status byte inside our message aborts it
and goes to the stock parser. Note: the stock firmware rewrites effect words when the effect type changes.
"""
from asm import Asm

GET_ORG, GET_END = 0x4A9F, 0x4B1B      # fixed ROM freed by the ROM PLAY keys cut (cuts.py)
DO_ORG, DO_END = 0xBEE0, 0xBFFF        # page 2: stock FF padding after the 16 bit-mask words ending 0xBED9 (page 2 is
                                       # in the window whenever 0x108C runs: every caller goes through stub 0x1050)
JT_GET, JT_GET_OLD = 0x108C, bytes([0xE7, 0x9D, 0xAD])   # LJMP BE2C
TX = 0x1089                            # LJMP BE4B: send R1C (page 2)
RX_BUF, R40, R42 = 0xC000, 0x40, 0x42
LS_STATE, LS_BUF = 0xC6A2, 0xC6B0      # 0 idle, 1 after F0, 2 + n = in our message with n bytes; 16 bytes
LS_MAX = 16
R1A, R1B, R1C, X, W, P, Q = 0x1A, 0x1B, 0x1C, 0x1E, 0x20, 0x22, 0x24


def emit_get(a):
    """R1A / R1B are free here: every caller loads R1A (the far-call target) again before each call"""
    J = a.jcc
    a.L('lab_get')
    a.b(0x88, R40, R42); J('JE', 'lg_none')             # CMP R42, R40: ring empty
    a.ldb_lidx(R1C, R42, RX_BUF); a.incb(R42)
    a.ldb_abs(R1B, LS_STATE)
    a.cmpbi(R1C, 0x80); J('JNC', 'lg_dat')
    a.cmpbi(R1C, 0xF7); J('JNE', 'lg_st'); a.cmpbi(R1B, 2); J('JNC', 'lg_st')
    a.lcall('lab_do'); a.clrb(R1B); a.stb_abs(LS_STATE, R1B); a.sjmp('lab_get')   # our F7: run it, swallow
    a.L('lg_st'); a.clrb(R1B); a.cmpbi(R1C, 0xF0); J('JNE', 'lg_set'); a.incb(R1B)  # status: F0 -> 1, else 0
    a.L('lg_set'); a.stb_abs(LS_STATE, R1B); a.sjmp('lg_give')
    a.L('lg_dat'); a.cmpbi(R1B, 1); J('JNC', 'lg_give'); J('JNE', 'lg_in')
    a.cmpbi(R1C, 0x7D); J('JE', 'lg_ok'); a.clrb(R1B); a.sjmp('lg_set')
    a.L('lg_ok'); a.ldbi(R1B, 2); a.ldbi(R1C, 0xF7); a.sjmp('lg_set')          # stock sees F0 F7
    a.L('lg_in'); a.cmpbi(R1B, 2 + LS_MAX); J('JC', 'lab_get')                  # full: drop
    a.ldbze(R1A, R1B); a.stb_lidx(R1A, LS_BUF - 2, R1C); a.incb(R1A); a.stb_abs(LS_STATE, R1A); a.sjmp('lab_get')
    a.L('lg_none'); a.ljmp('fx_tick')                   # no byte: fxfw.py polls the FX BOX, then CLRC
    a.L('lg_give'); a.b(0xF9); a.ret()                  # SETC: byte in R1C


def emit_do(a):
    J = a.jcc
    a.L('lab_do')                                      # LS_BUF: 4C cmd nibbles...
    a.push(X); a.push(W)
    a.ldb_abs(X, LS_STATE); a.cmpbi(X, 2 + 4); J('JNC', 'ld_x')    # n = LS_STATE - 2 bytes
    a.ldb_abs(X, LS_BUF); a.cmpbi(X, 0x4C); J('JNE', 'ld_x')
    a.push(P); a.push(Q)
    a.b(0xAD, 2, W); a.scall('lnb'); a.ldb(P, X)       # LDBZE W, #2 ; P = address / cell
    a.ldb_abs(X, LS_BUF + 1); a.cmpbi(X, 3); J('JC', 'ld_rd')
    a.ldb_abs(X, LS_STATE); a.cmpbi(X, 2 + 10); J('JNC', 'ld_pop')
    a.scall('lnb'); a.ldb(P + 1, X); a.scall('lnb'); a.ldb(Q, X); a.scall('lnb'); a.ldb(Q + 1, X)
    a.pushf()
    a.stb_abs(0x0A00, P + 1); a.stb_abs(0x0A01, Q); a.stb_abs(0x0A02, Q + 1)
    a.ldb_abs(X, LS_BUF + 1); a.cmpbi(X, 2); J('JE', 'ld_wc')
    a.stb_abs(0x0A06, P); a.sjmp('ld_wd')
    a.L('ld_wc'); a.andbi(P, 31); a.stb_abs(0x0A04, P)
    a.L('ld_wd'); a.popf(); a.sjmp('ld_pop')
    a.L('ld_rd'); a.cmpbi(X, 5); J('JNE', 'ld_rw')
    a.scall('lnb'); a.ldb(P + 1, X); a.b(0xA0, P, W)  # cmd 5: P = address (low byte first) -> LD W, P
    a.b(0xA2, W, Q); a.sjmp('ld_tx')                  # LD Q, [W]: the word at an even address, sent like a word reply
    a.L('ld_rw')
    a.pushf(); a.cmpbi(X, 4); J('JE', 'ld_rc')
    a.stb_abs(0x0A0A, P); a.sjmp('ld_rr')
    a.L('ld_rc'); a.andbi(P, 31); a.stb_abs(0x0A08, P)
    a.L('ld_rr'); a.ldb_abs(P + 1, 0x0A00); a.ldb_abs(Q, 0x0A01); a.ldb_abs(Q + 1, 0x0A02); a.popf()
    a.L('ld_tx')
    for v in (0xF0, 0x7D, 0x4C):
        a.ldbi(R1C, v); a.lcall(TX)
    a.ldb_abs(R1C, LS_BUF + 1); a.addbi(R1C, 0x10); a.lcall(TX)
    for r in (P, P + 1, Q, Q + 1):
        a.ldb(X, r); a.scall('txb')
    a.ldbi(R1C, 0xF7); a.lcall(TX)
    a.L('ld_pop'); a.pop(Q); a.pop(P)
    a.L('ld_x'); a.pop(W); a.pop(X); a.ret()
    a.L('lnb')                                         # W = index -> X = two nibbles from LS_BUF, W += 2
    a.ldb_lidx(X, W, LS_BUF); a.andbi(X, 15); a.b(0x19, 4, X); a.inc(W)    # SHLB X, #4
    a.ldb_lidx(R1C, W, LS_BUF); a.andbi(R1C, 15); a.orb(X, R1C); a.inc(W); a.ret()
    a.L('txb')                                         # X -> two nibble bytes out
    a.ldb(R1C, X); a.shrb(R1C, 4); a.lcall(TX); a.ldb(R1C, X); a.andbi(R1C, 15); a.ljmp(TX)


def build(ext):
    """-> [(org, code)], labels: lab_get (fixed ROM) calls lab_do (page 2)."""
    from collections import defaultdict
    g = Asm(GET_ORG); g.ext = defaultdict(int); emit_get(g); g.assemble()
    d = Asm(DO_ORG); d.ext = defaultdict(int); emit_do(d); d.assemble()
    labels = dict(g.labels, **d.labels)
    g = Asm(GET_ORG); g.ext = dict(ext, **labels); emit_get(g); gc = g.assemble()
    d = Asm(DO_ORG); d.ext = dict(ext, **labels); emit_do(d); dc = d.assemble()
    assert GET_ORG + len(gc) <= GET_END + 1, 'lab_get does not fit (%d B)' % len(gc)
    assert DO_ORG + len(dc) <= DO_END + 1, 'lab_do does not fit (%d B)' % len(dc)
    return [(GET_ORG, gc), (DO_ORG, dc)], labels
