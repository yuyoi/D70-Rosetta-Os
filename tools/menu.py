"""ROSETTA menu for the Roland D-70 (BIOS style, HR-GP5 Rosetta v0.2 look).

Entered by holding [EDIT] at power-on: after the boot animation the player calls menu_entry. [EXIT] at the top
page (or the EXIT row) returns, and the stock boot carries on.

Code + data live in ROM page 0 after the boot stream (file 0x0920-0x0FFF, run at CPU 0x8920 with bank 0, which
the player has already selected). RAM: the player's parked buffer 0xE000-0xE9FF is free while the menu runs.

Screen (240 x 64, T6963C, text layer XOR graphics layer):
  text   = 40 x 8 chars from the LCD's own font at LCD 0x1400 (codes = ASCII: stock loads its own ASCII font into the LCD's CG RAM; 0 = blank)
  row 0  = black tab with the page title (text shows white), a line under the rest, status right-aligned
  rows 1-7 = items: label left, value / '>' right; the selected row is a black bar; scroll bar in col 39
Keys (stock panel scan ported from 0xA971, bitmap = MAME's KEY0-7 order, set bit = pressed):
  UP / DOWN move (repeat), INC / DEC change a value (repeat), ENTER or RIGHT open / run / step a list,
  EXIT or LEFT back (top page: leave), NUMBER 1-7 jump to item 1-7.
Pages are tables: title, status, n, then 8-byte items: label.w, kind.b, max.b, ptr.w, aux.w
  SUB ptr = page | NUM ptr = RAM byte, 0..max | LIST ptr = RAM byte, aux = name table | ACT ptr = routine | INFO aux = text
"""
from asm import Asm

ORG = 0x8920                      # file 0x0920, bank 0
FILE_OFF = 0x0920
TB = 0xE100                       # text buffer 40 x 8
V_SEL, V_TOP, V_DEP, V_LEAVE = 0xE002, 0xE003, 0xE004, 0xE005
STACK = 0xE006                    # 4 x (page.w, sel.b, top.b)
KCUR, KPREV = 0xE018, 0xE020
V_REP, V_HB, V_HM, V_HID = 0xE028, 0xE029, 0xE02A, 0xE02B
SET = 0xE030                      # settings (RAM only until an IC6 dump shows free battery-RAM bytes)
S_BOOT, S_SLMODE, S_SL = SET + 0, SET + 1, SET + 2      # S_SL..S_SL+3 = slider 1-4 parameter
NB = 0xE040                       # number text
IC6BUF = 0xE080                   # 128-byte block for the IC6 dump
LCD_D, LCD_C, LCD_TEXT = 0x0800, 0x0802, 0x1400
SBUF, PORT1, TIMER1 = 0x07, 0x0F, 0x0A
K_SUB, K_NUM, K_LIST, K_ACT, K_INFO = 0, 1, 2, 3, 4
ROWS = 7

# registers (all saved by the player)
PG, IT, W1, W2, T, V, SEL, TOP, N, KEY, S, DST, TM, TD, X, ST, CNT, AD = (
    0x30, 0x32, 0x34, 0x36, 0x38, 0x39, 0x3A, 0x3B, 0x3C, 0x3D, 0x3E, 0x40, 0x42, 0x44, 0x46, 0x47, 0x48, 0x4A)
Y, C, VAL, TS, TE, TL, SB, R28, KB = 0x20, 0x22, 0x24, 0x26, 0x27, 0x28, 0x29, 0x2A, 0x1C

# key ids
KU, KD, KI, KDEC, KENT, KEXIT, KNUM = 1, 2, 3, 4, 5, 6, 0x11
KEYTAB = [  # bank (D378+n), mask, id, repeat
    (3, 0x04, KU, 1), (3, 0x20, KD, 1), (3, 0x40, KI, 1), (3, 0x80, KDEC, 1),
    (0, 0x01, KENT, 0), (3, 0x08, KENT, 0), (4, 0x80, KEXIT, 0), (3, 0x10, KEXIT, 0)] + \
    [(2, 0x80 >> k, KNUM + k, 0) for k in range(7)]


def txt(s):
    return bytes(ord(c) for c in s) + b'\xff'


DATA_RAM = 0xE300                 # pages, name tables and strings run from RAM
DATA_ROM = [(3, 0xBDA4, 0x25C), (5, 0xBDFC, 0x204)]    # (ROM page, CPU address, room): free FF at the page ends


def build(bank_copy, ext=None, split=None):
    """bank_copy = CPU address of the fixed-area helper (bank in R4A, src R36, dst R34, CNT bytes).
    ext = data labels; split = bytes of the data blob that go to the first ROM gap"""
    a = Asm(ORG)
    a.ext = ext or {}
    J = a.jcc

    # ================= entry =================
    a.L('menu_entry')
    dst = DATA_RAM
    for (bank, src, room), n in zip(DATA_ROM, split or [1, 1]):
        if not n: continue
        a.ldbi(AD, bank); a.ldi(W2, src); a.ldi(W1, dst); a.ldi(CNT, n); a.lcall(bank_copy); dst += n
    a.ldi_l(PG, 'pg_root'); a.clrb(T)
    for v in (V_SEL, V_TOP, V_DEP, V_LEAVE, V_HID): a.stb_abs(v, T)
    a.ldi(W2, SET); a.ldi_l(S, 'defaults'); a.ldbi(T, 6)
    a.L('dflt'); a.ldb_pi(V, S); a.stb_pi(W2, V); a.djnz(T, 'dflt')
    a.L('rel'); a.lcall('wait10'); a.lcall('scan'); a.lcall('anykey'); J('JNE', 'rel')   # let go of EDIT first
    a.lcall('kcopy')
    a.ldbi(X, 0x9C); a.lcall('cmd')                    # text + graphics on
    a.L('redraw'); a.lcall('draw')
    a.L('loop'); a.lcall('wait10'); a.lcall('scan'); a.lcall('getkey'); a.lcall('kcopy')
    a.cmpbi(KEY, 0); J('JE', 'loop')
    a.lcall('dispatch')
    a.ldb_abs(T, V_LEAVE); a.cmpbi(T, 0); J('JNE', 'leave'); a.ljmp('redraw')
    a.L('leave')
    a.ldi(AD, LCD_TEXT); a.lcall('setaddr'); a.ldi(CNT, 320); a.lcall('aw_zero')
    a.ret()

    # ================= keys =================
    a.L('scan')                                         # port of stock 0xA971 into KCUR
    a.ldi(W2, KCUR)
    for v in (0xFF, 0xDF, 0xCF, 0xDF, 0xCF, 0xFF): a.ldbi(PORT1, v)
    a.ldbi(T, 8)
    a.L('sb'); a.ldbi(PORT1, 0x7F); a.ldbi(PORT1, 0xFF); a.ldbi(V, 8)
    a.L('sk'); a.ldbi(PORT1, 0xEF); a.ldbi(PORT1, 0xFF); a.shrb(KB, 1)
    a.jbs(PORT1, 2, 'sn'); a.orbi(KB, 0x80); a.L('sn'); a.djnz(V, 'sk')
    a.stb_pi(W2, KB); a.djnz(T, 'sb')
    a.ldbi(PORT1, 0xBF); a.ret()

    a.L('anykey'); a.ldi(W2, KCUR); a.clrb(T); a.ldbi(V, 8)      # -> Z clear if any key is down
    a.L('ak'); a.ldb_pi(X, W2); a.orb(T, X); a.djnz(V, 'ak'); a.cmpbi(T, 0); a.ret()

    a.L('kcopy'); a.ldi(W2, KCUR); a.ldbi(V, 8)
    a.L('kc'); a.ldb_ind(X, W2)
    a.stb_lidx(W2, 8, X); a.inc(W2); a.djnz(V, 'kc'); a.ret()       # KPREV = KCUR + 8

    a.L('getkey'); a.clrb(KEY); a.ldi_l(S, 'keytab')                  # -> KEY = new press or repeat
    a.L('gk'); a.ldb_pi(T, S); a.cmpbi(T, 0xFF); J('JE', 'grep')
    a.ldbze(W1, T); a.ldb_pi(V, S)                                    # bank, mask
    a.ldb_lidx(X, W1, KCUR); a.andb(X, V); J('JE', 'gskip')
    a.ldb_lidx(ST, W1, KPREV); a.andb(ST, V); J('JNE', 'gskip')
    a.ldb_pi(KEY, S); a.ldb_pi(X, S); a.cmpbi(X, 0); J('JE', 'gnr')   # new press
    a.stb_abs(V_HB, T); a.stb_abs(V_HM, V); a.stb_abs(V_HID, KEY); a.clrb(X); a.stb_abs(V_REP, X); a.ret()
    a.L('gnr'); a.clrb(X); a.stb_abs(V_HID, X); a.ret()
    a.L('gskip'); a.inc(S); a.inc(S); a.sjmp('gk')
    a.L('grep'); a.ldb_abs(X, V_HID); a.cmpbi(X, 0); J('JNE', 'gr1'); a.ret()
    a.L('gr1'); a.ldb_abs(T, V_HB); a.ldbze(W1, T)
    a.ldb_lidx(T, W1, KCUR); a.ldb_abs(V, V_HM); a.andb(T, V); J('JNE', 'gr2')
    a.clrb(X); a.stb_abs(V_HID, X); a.ret()
    a.L('gr2'); a.ldb_abs(T, V_REP); a.incb(T); a.cmpbi(T, 30); J('JNC', 'gr3')   # 300 ms, then every 50 ms
    a.ldbi(T, 25); a.ldb_abs(KEY, V_HID)
    a.L('gr3'); a.stb_abs(V_REP, T); a.ret()

    # ================= actions =================
    a.L('dispatch')
    a.ldb_idx(N, PG, 4); a.ldb_abs(SEL, V_SEL); a.ldb_abs(TOP, V_TOP); a.lcall('item')
    a.cmpbi(KEY, KU); J('JNE', 'd1'); a.cmpbi(SEL, 0); J('JE', 'dfix'); a.decb(SEL); a.sjmp('dfix')
    a.L('d1'); a.cmpbi(KEY, KD); J('JNE', 'd2')
    a.ldb(T, SEL); a.incb(T); a.cmpb(T, N); J('JC', 'dfix'); a.ldb(SEL, T); a.sjmp('dfix')
    a.L('d2'); a.cmpbi(KEY, KNUM); J('JNC', 'd3')
    a.ldb(T, KEY); a.subbi(T, KNUM); a.cmpb(T, N); J('JC', 'd3x'); a.ldb(SEL, T); a.sjmp('dfix')
    a.L('d3x'); a.ret()
    a.L('d3'); a.cmpbi(KEY, KI); J('JE', 'dinc'); a.cmpbi(KEY, KDEC); J('JE', 'ddec')
    a.cmpbi(KEY, KENT); J('JE', 'dent'); a.cmpbi(KEY, KEXIT); J('JE', 'dexit'); a.ret()
    a.L('dfix'); a.cmpb(SEL, TOP); J('JC', 'df1'); a.ldb(TOP, SEL)
    a.L('df1'); a.ldb(T, TOP); a.addbi(T, ROWS); a.cmpb(SEL, T); J('JNC', 'df2')
    a.ldb(TOP, SEL); a.subbi(TOP, ROWS - 1)
    a.L('df2'); a.stb_abs(V_SEL, SEL); a.stb_abs(V_TOP, TOP); a.ret()
    # value change: NUM / LIST only
    a.L('dinc'); a.lcall('valrd'); J('JNE', 'dx'); a.cmpb(V, X); J('JC', 'dx'); a.incb(V); a.sjmp('valwr')
    a.L('ddec'); a.lcall('valrd'); J('JNE', 'dx'); a.cmpbi(V, 0); J('JE', 'dx'); a.decb(V); a.sjmp('valwr')
    a.L('valwr'); a.ld_idx(W2, IT, 4); a.stb_ind(W2, V)
    a.L('dx'); a.ret()
    a.L('valrd')                                        # -> Z if NUM/LIST, V = value, X = max
    a.ldb_idx(T, IT, 2); a.cmpbi(T, K_NUM); J('JE', 'vr1'); a.cmpbi(T, K_LIST); J('JE', 'vr1'); a.ret()
    a.L('vr1'); a.ld_idx(W2, IT, 4); a.ldb_ind(V, W2); a.ldb_idx(X, IT, 3); a.cmpb(T, T); a.ret()
    a.L('dent'); a.ldb_idx(T, IT, 2)
    a.cmpbi(T, K_LIST); J('JNE', 'de1')                 # LIST: step, wrap
    a.lcall('valrd'); a.cmpb(V, X); J('JC', 'de0'); a.incb(V); a.sjmp('valwr')
    a.L('de0'); a.clrb(V); a.sjmp('valwr')
    a.L('de1'); a.cmpbi(T, K_ACT); J('JNE', 'de2')
    a.ld_idx(W2, IT, 4); a.push_imm('dx'); a.br(W2)    # call the routine, it returns to dx
    a.L('de2'); a.cmpbi(T, K_SUB); J('JNE', 'dx')
    a.ldb_abs(T, V_DEP); a.cmpbi(T, 4); J('JE', 'dx')
    a.ldbze(W1, T); a.shl(W1, 2); a.ld(W2, PG); a.st_lidx(W1, STACK, W2)
    a.stb_lidx(W1, STACK + 2, SEL); a.stb_lidx(W1, STACK + 3, TOP)
    a.incb(T); a.stb_abs(V_DEP, T)
    a.ld_idx(PG, IT, 4); a.clrb(T); a.stb_abs(V_SEL, T); a.stb_abs(V_TOP, T); a.ret()
    a.L('dexit'); a.ldb_abs(T, V_DEP); a.cmpbi(T, 0); J('JNE', 'dx1')
    a.L('act_exit'); a.ldbi(T, 1); a.stb_abs(V_LEAVE, T); a.ret()
    a.L('dx1'); a.decb(T); a.stb_abs(V_DEP, T); a.ldbze(W1, T); a.shl(W1, 2)
    a.ld_lidx(PG, W1, STACK); a.ldb_lidx(T, W1, STACK + 2); a.stb_abs(V_SEL, T)
    a.ldb_lidx(T, W1, STACK + 3); a.stb_abs(V_TOP, T); a.ret()

    a.L('item')                                         # IT = PG + 6 + SEL*8
    a.ldbze(W1, SEL); a.shl(W1, 3); a.add3(IT, PG, W1); a.addi(IT, 6); a.ret()

    # TOOLS > DUMP IC6: 256 SysEx messages F0 7D 49 half blk <128 bytes as 256 nibbles> F7
    a.L('act_dump')
    a.ldi_l(S, 's_sending'); a.ldi(DST, TB + 7 * 40 + 1); a.lcall('puts'); a.lcall('push_text')
    a.clr(Y)                                            # Y = block 0..255 (128 bytes each)
    a.L('db'); a.ldbi(AD, 0x20); a.ld(W2, Y); a.shl(W2, 7)
    a.cmpi(Y, 128); J('JNC', 'db1'); a.ldbi(AD, 0x21); a.subi(W2, 0x4000)
    a.L('db1'); a.addi(W2, 0x8000); a.ldi(W1, IC6BUF); a.ldi(CNT, 128)
    a.lcall(bank_copy)
    for v in (0xF0, 0x7D, 0x49): a.ldbi(X, v); a.lcall('tx')
    a.ldb(X, AD); a.andbi(X, 1); a.lcall('tx'); a.ldb(X, Y); a.andbi(X, 0x7F); a.lcall('tx')
    a.ldi(W2, IC6BUF); a.ldbi(V, 128)
    a.L('dn'); a.ldb_pi(T, W2); a.ldb(X, T); a.shrb(X, 4); a.lcall('tx')
    a.ldb(X, T); a.andbi(X, 0x0F); a.lcall('tx'); a.djnz(V, 'dn')
    a.ldbi(X, 0xF7); a.lcall('tx')
    a.inc(Y); a.cmpi(Y, 256); J('JE', 'dd'); a.ljmp('db')
    a.L('dd'); a.ret()
    a.L('tx')
    a.b(0xC4, SBUF, X)                                  # STB SBUF, X
    a.ld(TM, TIMER1)
    a.L('txw'); a.ld(TD, TIMER1); a.sub(TD, TM); a.cmpi(TD, 300); J('JNC', 'txw'); a.ret()   # 400 us / byte

    # ================= drawing =================
    a.L('draw')
    a.ldb_idx(N, PG, 4); a.ldb_abs(SEL, V_SEL); a.ldb_abs(TOP, V_TOP)
    a.ldi(W2, TB); a.ldi(CNT, 320); a.clrb(X)
    a.L('dc'); a.stb_pi(W2, X); a.dec(CNT); J('JNE', 'dc')
    a.ld_idx(S, PG, 0); a.ldi(DST, TB + 1); a.lcall('puts')
    a.ld_idx(S, PG, 0); a.lcall('strlen'); a.ldb(TL, T); a.addbi(TL, 2)
    a.ld_idx(S, PG, 2); a.cmpi(S, 0); J('JE', 'nost'); a.ldi(DST, TB + 39); a.lcall('rputs')
    a.L('nost'); a.clrb(VAL)                            # VAL = visible row 0..6
    a.L('dr'); a.ldb(T, TOP); a.addb(T, VAL); a.cmpb(T, N); J('JNC', 'dr0'); a.ljmp('drdone')
    a.L('dr0'); a.ldb(SEL, T); a.lcall('item')
    a.ldbze(DST, VAL); a.incb(DST); a.ldbze(W1, DST); a.shl(W1, 3); a.ld(DST, W1)
    a.shl(W1, 2); a.add(DST, W1); a.addi(DST, TB)      # DST = TB + (row+1)*40
    a.push(DST); a.inc(DST); a.ld_idx(S, IT, 0); a.lcall('puts'); a.pop(DST)
    a.addi(DST, 38)                                     # values end before col 38
    a.ldb_idx(T, IT, 2)
    a.cmpbi(T, K_SUB); J('JNE', 'dk1'); a.ldi_l(S, 's_gt'); a.sjmp('dkp')
    a.L('dk1'); a.cmpbi(T, K_INFO); J('JNE', 'dk2'); a.ld_idx(S, IT, 6); a.sjmp('dkp')
    a.L('dk2'); a.cmpbi(T, K_ACT); J('JE', 'dknext')
    a.ld_idx(W2, IT, 4); a.ldb_ind(V, W2)
    a.cmpbi(T, K_LIST); J('JNE', 'dk3')
    a.ldbze(W1, V); a.add(W1, W1); a.ld_idx(W2, IT, 6); a.add(W2, W1)
    a.b(0xA2, W2, S)                                    # LD S,[W2]
    a.sjmp('dkp')
    a.L('dk3'); a.lcall('numstr'); a.ldi(S, NB)
    a.L('dkp'); a.lcall('rputs')
    a.L('dknext'); a.incb(VAL); a.cmpbi(VAL, ROWS); J('JE', 'drdone'); a.ljmp('dr')
    a.L('drdone')
    a.lcall('push_text')
    # scroll thumb rows TS..TE-1 (pixel rows 8..63) when there are more than 7 items
    a.clrb(TS); a.clrb(TE); a.cmpbi(N, ROWS + 1); J('JC', 'sc0'); a.sjmp('sc1')
    a.L('sc0')
    a.ldbze(W1, TOP); a.ldbi(V, 56); a.lcall('mul_div'); a.ldb(TS, W1); a.addbi(TS, 8)
    a.ldi(W1, ROWS); a.ldbi(V, 56); a.lcall('mul_div'); a.ldb(TE, TS); a.addb(TE, W1)   # length 56*7/N
    a.L('sc1')
    a.ldb_abs(SB, V_SEL); a.subb(SB, TOP); a.incb(SB)
    a.ldi(AD, 0); a.lcall('setaddr'); a.ldbi(X, 0xB0); a.lcall('cmd')
    a.clr(Y)
    a.L('gy'); a.clr(C); a.ldb(R28, Y); a.shrb(R28, 3)          # R28 = band
    a.L('gx'); a.clrb(X)
    a.cmpbi(R28, 0); J('JNE', 'g1')
    a.cmpb(C, TL); J('JC', 'g0'); a.ldbi(X, 0x3F); a.sjmp('gout')
    a.L('g0'); a.ldb(T, Y); a.andbi(T, 7); a.cmpbi(T, 7); J('JNE', 'gout'); a.ldbi(X, 0x3F); a.sjmp('gout')
    a.L('g1'); a.cmpbi(C, 39); J('JNE', 'g2')
    a.cmpbi(TE, 0); J('JE', 'gout'); a.ldbi(X, 0x0C)
    a.cmpb(Y, TS); J('JNC', 'gout'); a.cmpb(Y, TE); J('JC', 'gout'); a.ldbi(X, 0x1E); a.sjmp('gout')
    a.L('g2'); a.cmpb(R28, SB); J('JNE', 'gout'); a.ldbi(X, 0x3F)
    a.L('gout'); a.ldb_abs(ST, LCD_C); a.jbc(ST, 3, 'gout'); a.stb_abs(LCD_D, X)
    a.incb(C); a.cmpbi(C, 40); J('JNE', 'gx')
    a.incb(Y); a.cmpbi(Y, 64); J('JNE', 'gy')
    a.ldbi(X, 0xB2); a.lcall('cmd'); a.ret()

    a.L('mul_div')                                      # W1 = W1 * V / N   (small numbers, by loops)
    a.ldbze(W2, V); a.clr(TD)
    a.L('md1'); a.cmpi(W2, 0); J('JE', 'md2'); a.add(TD, W1); a.dec(W2); a.sjmp('md1')
    a.L('md2'); a.clr(W1); a.ldbze(W2, N)
    a.L('md3'); a.cmp(TD, W2); J('JNC', 'md4'); a.sub(TD, W2); a.inc(W1); a.sjmp('md3')
    a.L('md4'); a.ret()

    a.L('push_text'); a.ldi(AD, LCD_TEXT); a.lcall('setaddr'); a.ldi(W2, TB); a.ldi(CNT, 320)
    a.ldbi(X, 0xB0); a.lcall('cmd')
    a.L('pt'); a.ldb_abs(ST, LCD_C); a.jbc(ST, 3, 'pt'); a.ldb_pi(X, W2); a.stb_abs(LCD_D, X)
    a.dec(CNT); J('JNE', 'pt'); a.ldbi(X, 0xB2); a.sjmp('cmd')

    a.L('puts'); a.ldb_pi(X, S); a.cmpbi(X, 0xFF); J('JE', 'pu1'); a.stb_pi(DST, X); a.sjmp('puts')
    a.L('pu1'); a.ret()
    a.L('strlen'); a.ld(W2, S); a.clrb(T)                # -> T = length of S
    a.L('sl1'); a.ldb_pi(X, W2); a.cmpbi(X, 0xFF); J('JE', 'pu1'); a.incb(T); a.sjmp('sl1')
    a.L('rputs'); a.lcall('strlen'); a.ldbze(W1, T); a.sub(DST, W1); a.sjmp('puts')   # S ends right before DST
    a.L('numstr')                                       # V = 0..255 -> NB, no leading zeros
    a.ldi(W2, NB); a.clrb(KB); a.ldbi(T, 100); a.lcall('nd'); a.ldbi(T, 10); a.lcall('nd')
    a.ldb(X, V); a.addbi(X, 0x30); a.stb_pi(W2, X); a.ldbi(X, 0xFF); a.stb_pi(W2, X); a.ret()
    a.L('nd'); a.clrb(X)
    a.L('nd1'); a.cmpb(V, T); J('JNC', 'nd2'); a.subb(V, T); a.incb(X); a.sjmp('nd1')
    a.L('nd2'); a.orb(KB, X); a.cmpbi(KB, 0); J('JE', 'pu1'); a.addbi(X, 0x30); a.stb_pi(W2, X); a.ret()

    # ---- T6963C (same handshakes as the stock driver)
    a.L('wst'); a.ldb_abs(ST, LCD_C); a.andbi(ST, 3); a.cmpbi(ST, 3); J('JNE', 'wst'); a.ret()
    a.L('cmd'); a.lcall('wst'); a.stb_abs(LCD_C, X); a.ret()
    a.L('dat'); a.lcall('wst'); a.stb_abs(LCD_D, X); a.ret()
    a.L('setaddr'); a.ldb(X, AD); a.lcall('dat'); a.ldb(X, AD + 1); a.lcall('dat'); a.ldbi(X, 0x24); a.sjmp('cmd')
    a.L('aw_zero'); a.ldbi(X, 0xB0); a.lcall('cmd')
    a.L('z1'); a.ldb_abs(ST, LCD_C); a.jbc(ST, 3, 'z1'); a.stb_abs(LCD_D, 0)
    a.dec(CNT); J('JNE', 'z1'); a.ldbi(X, 0xB2); a.sjmp('cmd')
    a.L('wait10'); a.ld(TM, TIMER1)                     # 10 ms
    a.L('w10'); a.ld(TD, TIMER1); a.sub(TD, TM); a.cmpi(TD, 7500); J('JNC', 'w10'); a.ret()

    a.L('keytab')
    for bnk, m, kid, rep in KEYTAB: a.data([bnk, m, kid, rep])
    a.data([0xFF])
    a.L('defaults'); a.data([1, 0, 0, 1, 2, 3])        # BOOT on, sliders STOCK, CUTOFF RESO ATTACK RELEASE
    return a


def data(code_labels):
    """pages, name tables, strings (assembled for RAM at DATA_RAM)"""
    a = Asm(DATA_RAM)
    a.ext = code_labels

    def page(name, title, status, items):
        a.L(name); a.dw(title); a.dw(status or 0); a.data([len(items), 0])
        for lab, kind, mx, ptr, aux in items:
            a.dw(lab); a.data([kind, mx]); a.dw(ptr or 0); a.dw(aux or 0)

    page('pg_root', 't_root', 's_ver', [
        ('l_boot', K_SUB, 0, 'pg_boot', 0), ('l_sliders', K_SUB, 0, 'pg_sl', 0),
        ('l_tools', K_SUB, 0, 'pg_tools', 0), ('l_about', K_SUB, 0, 'pg_about', 0),
        ('l_exit', K_ACT, 0, 'act_exit', 0)])
    page('pg_boot', 't_boot', 's_ram', [('l_hexgirl', K_LIST, 1, S_BOOT, 'n_offon')])
    page('pg_sl', 't_sliders', 's_soon', [('l_mode', K_LIST, 1, S_SLMODE, 'n_slmode')] +
         [('l_sl%d' % k, K_LIST, 7, S_SL + k, 'n_params') for k in range(4)])
    page('pg_tools', 't_tools', 0, [('l_dump', K_ACT, 0, 'act_dump', 0)])
    page('pg_about', 't_about', 's_ver', [
        ('l_os', K_INFO, 0, 0, 'v_os'), ('l_by', K_INFO, 0, 0, 'v_by'),
        ('l_mascot', K_INFO, 0, 0, 'v_mascot'), ('l_base', K_INFO, 0, 0, 'v_base')])

    def table(name, labels):
        a.L(name)
        for l in labels: a.dw(l)
    table('n_offon', ['o_off', 'o_on'])
    table('n_slmode', ['m_stock', 'm_rosetta'])
    params = ['CUTOFF', 'RESONANCE', 'ATTACK', 'RELEASE', 'PAN', 'TUNING', 'LEVEL', 'PORTAMENTO']
    table('n_params', ['p_%d' % i for i in range(8)])

    strings = dict(t_root='ROSETTA OS', s_ver='V0.2', t_boot='BOOT', s_ram='NOT SAVED YET', t_sliders='SLIDERS',
                   s_soon='SOON', t_tools='TOOLS', t_about='ABOUT', l_boot='BOOT', l_sliders='SLIDERS',
                   l_tools='TOOLS', l_about='ABOUT', l_exit='EXIT TO D-70', l_hexgirl='HEX GIRL', l_mode='MODE',
                   l_dump='DUMP IC6 OVER MIDI', l_os='OS', l_by='BY', l_mascot='MASCOT', l_base='BASE',
                   v_os='ROSETTA 0.2', v_by='JSW + CLAUDE', v_mascot='HEX GIRL', v_base='ROLAND D-70 1.11',
                   o_off='OFF', o_on='ON', m_stock='STOCK', m_rosetta='ROSETTA', s_gt='>',
                   s_sending='SENDING IC6 ... ABOUT 30 S')
    strings.update({'l_sl%d' % k: 'SLIDER %d' % (k + 1) for k in range(4)})
    strings.update({'p_%d' % i: p for i, p in enumerate(params)})
    for k, v in strings.items():
        a.L(k); a.data(txt(v))
    return a
