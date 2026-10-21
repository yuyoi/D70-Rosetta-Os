"""ROSETTA SYNTH menu for the Roland D-70: live tone / patch editing while the stock OS runs.

Opened with [USER] during play, closed with [EXIT] on its top page. Runs from ROM page 7 (the space the ROM PLAY
demo freed), called once per main-loop pass instead of the stock UI step:

  0x151D  LCALL 33FF (stock UI step)  ->  LCALL ui_tramp      fixed ROM: LD R1A,#rt_ui ; LJMP 105F (page 7 stub)
  rt_ui: menu closed -> watch [USER] (key bitmap D37C bit 1), else run the stock UI step 0x33FF as before
         menu open   -> one menu step: stock key reader (page 4 0xA5D8), handle the key, redraw
MIDI, sound and the sliders keep running because the main loop keeps running.

Parameters go through the firmware's own SysEx routines (page 5), so every edit is checked, clamped and applied
the same way an incoming SysEx message is:
  read  0x9DEF: RD6:RD4 = flat address -> RCA          write 0x9E7E: RCA -> RD6:RD4 (sets dirty bits in RDE)
  apply 0x10CB (fixed, via the 0x1050 stub) after a batch of writes
Flat address = a2<<14 | a1<<7 | a0 of the SysEx address (manual p.208). Current part = stock register R5F.
  TONE  part p section s : 0x0351 + (p*4 + s)*0x3E + offset            (Table 6, 62 bytes)
  PSEC  part p section s : 0x019E + p*0x57 + 0x23 + s*0x0D + offset    (Table 52, 13 bytes)
  PCOM  part p           : 0x019E + p*0x57 + offset                     (Table 5)
Section = L1, L2, U3, U4 or ALL (writes go to all four, the display shows L1).

State lives in RAM 0xC400-0xC47F (ROM PLAY's old backup buffer). Registers R1A-R2F are scratch and are
reloaded after every call into the stock firmware; every call into stock code goes through a far stub,
because stock code reads ROM page 2 (its home page) at 0x8000-0xBFFF directly; R30-R4F belong to the interrupt routines and are never used.
Text: ASCII into LCD text RAM 0x1680 (the stock CG RAM font is ASCII). Graphics: LCD 0x0A00, 6 px per byte,
XOR with text. Envelope lines are drawn with the T6963C bit-set command.
"""
from asm import Asm

ORG = 0x83A9                   # page 7, after the kept test-mode data
END = 0x9C47
# stock entry points
GETKEY, P4 = 0x8006, 0x1056    # LD R1A,#8006 ; LCALL 1056 -> C = key, R1C = code
RD, WR, P5 = 0x9DEF, 0x9E7E, 0x1059
APPLY, P2 = 0x10CB, 0x1050
IC6_RD = None                  # set by the build: fixed-ROM helper (bank R1C, address R20 -> R26)
STOCK_UI = 0x33FF
SCR_INIT, SCR_DRAW, SCR_LEDS = 0x37F1, 0x38A5, 0x4B1C
KEYMAP_USER = (0xD37C, 0x02)   # bank 4 bit 1
LCD_D, LCD_C = 0x0800, 0x0802
LCD_TEXT = 0x1680              # our text page: stock draws text at 0x1400 AND 0x1540 (R9A), CG RAM is 0x1800
assert 0x1680 <= LCD_TEXT and LCD_TEXT + 320 <= 0x1800
LCD_GFX = 0x0A00               # stock's 2nd graphics page; stock draws graphics at R9E (0x0000 on the play screen)
R9C, RA0, RA2 = 0x9C, 0xA0, 0xA2   # stock: text home, graphics home, display flags
# key codes (stock table page 4 0xA703)
K_UP, K_DN, K_LT, K_RT, K_DEC, K_INC, K_ENT, K_EXIT = 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x07, 0x22
K_F5, K_USER, K_L1 = 0x21, 0x1C, 0x35       # L1, L2, U3, U4 = 0x35-0x38
# RAM
M_ON, UPREV, PG, SEL, COL, SEC, RSEL, RTOP = 0xC400, 0xC401, 0xC402, 0xC403, 0xC404, 0xC405, 0xC406, 0xC407
VAL, ITYPE, IAUX, IMIN, IMAX, IAREA, IOFF = 0xC408, 0xC409, 0xC40A, 0xC40B, 0xC40C, 0xC40D, 0xC40E
ADDR, PAGEP, ITEMP = 0xC410, 0xC412, 0xC414
ROW, I, KIND, TL, SELROW, SC0, SC1, NITEM = 0xC416, 0xC417, 0xC418, 0xC419, 0xC41A, 0xC41B, 0xC41C, 0xC41D
SECW, KEYC, TMPB = 0xC41E, 0xC41F, 0xC4AC
TXT = 0xC420                   # 40-char row buffer
EV = 0xC480                    # envelope page: the 7 values, for the lines
NB = 0xC490                    # number / name text (16)
ZL, ZH = 0xC4A0, 0xC4A4        # layers page: zone bar start/end in pixels (4 each)
LE2 = 0xC4A8                   # line: 2 * err
NI = 0xC4AA                    # name reader: character index
BOOTHI, BOOTN = 0xC4AD, 0xC4AE # boot delay: last TIMER1 high byte, wrap count
GBUF = 0xC4D0                  # one graphics row (40) before it goes to the LCD
LX0, LY0, LX1, LY1, LDX, LDY, LSX, LSY, LERR = (0xC470, 0xC472, 0xC474, 0xC476, 0xC478, 0xC47A, 0xC47C,
                                               0xC47D, 0xC47E)
# scratch registers
T, U, W, P, Q, V, C, X, ST = 0x1C, 0x1D, 0x20, 0x22, 0x24, 0x26, 0x28, 0x1E, 0x1F
IDX, Z = 0x2C, 0x2E
RD4, RD6, RCA, RDE, R1A, R5F = 0xD4, 0xD6, 0xCA, 0xDE, 0x1A, 0x5F

A_TONE, A_PSEC, A_PCOM = 0, 1, 2
T_NUM, T_PLUS1, T_LIST, T_NOTE, T_MEDIA, T_NAME = 0, 1, 2, 3, 4, 5
K_ROOT, K_SPLIT, K_ENV, K_LAYERS = 0, 1, 2, 3

LISTS = {'offon': ['OFF', 'ON'], 'tvf': ['LPF', 'BPF', 'HPF', 'BYPASS'], 'lfo': ['TRI', 'SINE', 'SQUARE', 'SAW', 'RANDOM'],
         'ofs': ['-100%', '-50%', '0', '+50%', '+100%'], 'out': ['DRY', 'REVERB', 'CHORUS', 'DIRECT'],
         'kmode': ['LAYER', 'SPLIT', 'ZONE']}
LIST_IDS = {k: i for i, k in enumerate(LISTS)}


def it(label, area, off, typ, mn, mx, aux=0):
    return (label, area, off, typ, mn, mx, aux)


def L(name):
    return LIST_IDS[name]


PAGES = [  # (key, title, kind, head0, head1, items0, items1); list pages take their items from LIST_PAGES
    ('rosetta', 'ROSETTA OS', K_ROOT, '', '', [], []),
    ('synth', 'SYNTH', K_ROOT, '', '', [], []),
    ('tools', 'TOOLS', K_ROOT, '', '', [], []),
    ('about', 'ABOUT', K_ROOT, '', '', [], []),
    ('layers', 'LAYERS', K_LAYERS, '', '', [], []),
    ('wave', 'WAVE', K_SPLIT, 'WAVE', 'ZONE',
     [it('NAME', A_TONE, 0x00, T_NAME, 0, 0), it('MEDIA', A_TONE, 0x0A, T_MEDIA, 0, 66),
      it('NUMBER', A_TONE, 0x0B, T_PLUS1, 0, 127), it('DLM', A_TONE, 0x0C, T_LIST, 0, 1, L('offon')),
      it('MODE', A_TONE, 0x0D, T_LIST, 0, 1, L('offon')), it('START', A_TONE, 0x0E, T_NUM, 0, 127)],
     [it('LOW', A_PSEC, 0x0B, T_NOTE, 0, 127), it('HIGH', A_PSEC, 0x0C, T_NOTE, 0, 127),
      it('SHIFT', A_PSEC, 0x02, T_NUM, 0, 48, 24), it('FINE', A_PSEC, 0x03, T_NUM, 0, 127, 64),
      it('PAN', A_PSEC, 0x09, T_NUM, 0, 14, 7), it('OUTPUT', A_PSEC, 0x08, T_LIST, 0, 3, L('out'))]),
    ('pitch', 'PITCH', K_SPLIT, 'PITCH', 'PITCH ENV',
     [it('COARSE', A_TONE, 0x10, T_NUM, 0, 96, 48), it('FINE', A_TONE, 0x11, T_NUM, 0, 127, 64),
      it('KEYF', A_TONE, 0x12, T_NUM, 0, 15), it('TIME KF', A_TONE, 0x13, T_NUM, 0, 15)],
     [it('A TIME', A_TONE, 0x14, T_NUM, 0, 127), it('A DEPTH', A_TONE, 0x15, T_NUM, 0, 24, 12),
      it('R TIME', A_TONE, 0x16, T_NUM, 0, 127), it('R DEPTH', A_TONE, 0x17, T_NUM, 0, 24, 12)]),
    ('filter', 'FILTER', K_SPLIT, 'TVF', 'TVF ENV',
     [it('MODE', A_TONE, 0x18, T_LIST, 0, 3, L('tvf')), it('CUTOFF', A_TONE, 0x19, T_NUM, 0, 127),
      it('RESO', A_TONE, 0x1A, T_NUM, 0, 127), it('KEYF', A_TONE, 0x1C, T_NUM, 0, 15),
      it('VELO CRV', A_TONE, 0x1B, T_PLUS1, 0, 3)],
     [it('DEPTH', A_TONE, 0x1D, T_NUM, 0, 127, 64), it('D.VELO', A_TONE, 0x1E, T_NUM, 0, 127),
      it('TIME KF', A_TONE, 0x1F, T_NUM, 0, 15), it('T.VELO', A_TONE, 0x20, T_NUM, 0, 127),
      it('T.REL V', A_TONE, 0x21, T_NUM, 0, 127)]),
    ('fenv', 'FILTER ENV', K_ENV, '', '',
     [it('A TIME', A_TONE, 0x22, T_NUM, 0, 127), it('A LEVEL', A_TONE, 0x23, T_NUM, 0, 127),
      it('D1 TIME', A_TONE, 0x24, T_NUM, 0, 127), it('BREAK', A_TONE, 0x25, T_NUM, 0, 127),
      it('D2 TIME', A_TONE, 0x26, T_NUM, 0, 127), it('SUSTAIN', A_TONE, 0x27, T_NUM, 0, 127),
      it('RELEASE', A_TONE, 0x28, T_NUM, 0, 127)], []),
    ('amp', 'AMP', K_SPLIT, 'TVA', 'ENV TIME',
     [it('LEVEL', A_TONE, 0x29, T_NUM, 0, 127), it('VELO CRV', A_TONE, 0x2A, T_PLUS1, 0, 3),
      it('VELO', A_TONE, 0x2B, T_NUM, 0, 127), it('SEC LVL', A_PSEC, 0x01, T_NUM, 0, 127)],
     [it('KEYF', A_TONE, 0x2C, T_NUM, 0, 15), it('ON VELO', A_TONE, 0x2D, T_NUM, 0, 127),
      it('REL VELO', A_TONE, 0x2E, T_NUM, 0, 127)]),
    ('aenv', 'AMP ENV', K_ENV, '', '',
     [it('A TIME', A_TONE, 0x2F, T_NUM, 0, 127), it('A LEVEL', A_TONE, 0x30, T_NUM, 0, 127),
      it('D1 TIME', A_TONE, 0x31, T_NUM, 0, 127), it('BREAK', A_TONE, 0x32, T_NUM, 0, 127),
      it('D2 TIME', A_TONE, 0x33, T_NUM, 0, 127), it('SUSTAIN', A_TONE, 0x34, T_NUM, 0, 127),
      it('RELEASE', A_TONE, 0x35, T_NUM, 0, 127)], []),
    ('lfo', 'LFO', K_SPLIT, 'LFO', 'DEPTH',
     [it('WAVE', A_TONE, 0x36, T_LIST, 0, 4, L('lfo')), it('RATE', A_TONE, 0x37, T_NUM, 0, 127),
      it('DELAY', A_TONE, 0x38, T_NUM, 0, 15), it('RISE', A_TONE, 0x39, T_NUM, 0, 15),
      it('OFFSET', A_TONE, 0x3A, T_LIST, 0, 4, L('ofs'))],
     [it('PITCH', A_TONE, 0x3B, T_NUM, 0, 127, 64), it('CUTOFF', A_TONE, 0x3C, T_NUM, 0, 127, 64),
      it('AMP', A_TONE, 0x3D, T_NUM, 0, 127, 64)]),
]
SYNTH_ITEMS = ['layers', 'wave', 'pitch', 'filter', 'fenv', 'amp', 'aenv', 'lfo']
ROOT_LABELS = {'layers': 'LAYERS', 'wave': 'WAVE / ZONE', 'pitch': 'PITCH', 'filter': 'FILTER',
               'fenv': 'FILTER ENV', 'amp': 'AMP', 'aenv': 'AMP ENV', 'lfo': 'LFO'}
PIDX = {p[0]: i for i, p in enumerate(PAGES)}
LI_LINK, LI_ACT, LI_INFO = 0, 1, 2
ACT_CLOSE, ACT_DUMP = 0, 1
LIST_PAGES = {   # list page -> items (label, kind, arg); arg = page key, action number or info text
    'rosetta': [('SYNTH', LI_LINK, 'synth'), ('TOOLS', LI_LINK, 'tools'), ('ABOUT', LI_LINK, 'about'),
                ('EXIT TO D-70', LI_ACT, ACT_CLOSE)],
    'synth': [(ROOT_LABELS[k], LI_LINK, k) for k in SYNTH_ITEMS],
    'tools': [('DUMP IC6 OVER MIDI', LI_ACT, ACT_DUMP)],
    'about': [('OS', LI_INFO, 'ROSETTA 0.5'), ('BY', LI_INFO, 'JSW + CLAUDE'), ('MASCOT', LI_INFO, 'HEX GIRL'),
              ('BASE', LI_INFO, 'ROLAND D-70 1.11'), ('OLD D-70', LI_INFO, 'HOLD EXIT AT POWER ON')],
}
PARENT = {'rosetta': None, 'synth': 'rosetta', 'tools': 'rosetta', 'about': 'rosetta'}
PARENT.update({k: 'synth' for k in SYNTH_ITEMS})
RSAVE = 0xC4B0                 # list selection per page (16)
SBUF_, TIMER1_ = 0x07, 0x0A


def emit(a):
    J = a.jcc

    def lda(r, addr): a.ldb_abs(r, addr)
    def sta(addr, r): a.stb_abs(addr, r)
    def ldw(r, addr): a.ld_lidx(r, 0, addr)              # LD r, addr[ZERO]
    def stw(addr, r): a.st_lidx(0, addr, r)
    def stock(fn, stub): a.ldi(R1A, fn); a.lcall(stub)

    # ================= main-loop entry =================
    a.L('rt_ui')
    lda(T, M_ON); a.cmpbi(T, 0); J('JNE', 'rt_on')
    lda(T, KEYMAP_USER[0]); a.andbi(T, KEYMAP_USER[1]); lda(U, UPREV); sta(UPREV, T)
    a.cmpbi(T, 0); J('JE', 'rt_stock'); a.cmpbi(U, 0); J('JNE', 'rt_stock')
    a.ldbi(T, 1); sta(M_ON, T); a.clrb(T)              # [USER] went down: open the menu at its top page
    for v in (PG, SEL, COL, RSEL, RTOP): sta(v, T)
    sta(SEC, T)
    a.lcall('draw'); a.ret()
    a.L('rt_stock'); stock(STOCK_UI, P2); a.ret()       # stock code expects ROM page 2 in the window
    a.L('rt_on')
    a.cmpbi(T, 2); J('JNE', 'rt_on1')                   # opened at boot: let the stock startup finish first
    stock(STOCK_UI, P2)                                  # (about 2 s = 24 TIMER1 wraps of 87 ms)
    a.ldb_abs(T, TIMER1_ + 1); lda(U, BOOTHI); sta(BOOTHI, T); a.cmpb(T, U); J('JC', 'rt_x')
    lda(T, BOOTN); a.incb(T); sta(BOOTN, T); a.cmpbi(T, 24); J('JNE', 'rt_x')
    a.ldbi(T, 1); sta(M_ON, T); a.lcall('draw'); a.ret()
    a.L('rt_on1')
    stock(GETKEY, P4); J('JNC', 'rt_x')
    sta(KEYC, T); a.lcall('key')
    lda(T, M_ON); a.cmpbi(T, 0); J('JE', 'rt_close')
    a.lcall('draw')
    a.L('rt_x'); a.ret()
    a.L('rt_close')                                     # leave: show the stock pages again (drawn meanwhile)
    a.ld(W, R9C); a.lcall('home40'); a.ld(W, RA0); a.ldbi(X, 0x42); a.lcall('home')
    a.ldb(X, RA2); a.orbi(X, 0x90); a.b(0xFA); a.lcall('cmd'); a.b(0xFB); a.ret()
    a.L('lcd_mine')                                      # show our text + graphics pages, text and graphics on
    a.ldi(W, LCD_TEXT); a.lcall('home40'); a.ldi(W, LCD_GFX); a.ldbi(X, 0x42); a.lcall('home')
    a.ldbi(X, 0x9C); a.b(0xFA); a.lcall('cmd'); a.b(0xFB); a.ret()
    a.L('home40'); a.ldbi(X, 0x40)
    a.L('home'); a.b(0xFA); a.push(X); a.ldb(X, W); a.lcall('dat'); a.ldb(X, W + 1); a.lcall('dat'); a.pop(X)
    a.lcall('cmd'); a.b(0xFB); a.ret()

    # ================= keys =================
    a.L('key')
    a.lcall('page_ptr')
    lda(T, KEYC)
    a.cmpbi(T, K_F5); J('JNE', 'k1')                   # F5: next section (L1 L2 U3 U4 ALL)
    lda(T, SEC); a.incb(T); a.cmpbi(T, 5); J('JNE', 'k0'); a.clrb(T)
    a.L('k0'); sta(SEC, T); a.ret()
    a.L('k1'); a.cmpbi(T, K_L1); J('JNC', 'k2'); a.cmpbi(T, K_L1 + 4); J('JC', 'k2')
    a.subbi(T, K_L1); sta(SEC, T); a.ret()             # L1-U4 buttons pick the section
    a.L('k2'); lda(U, KIND)
    a.cmpbi(U, K_ROOT); J('JE', 'key_root')
    a.cmpbi(U, K_LAYERS); J('JE', 'key_layers')
    # SPLIT / ENV pages
    a.cmpbi(T, K_EXIT); J('JNE', 'kp1'); a.ljmp('to_parent')
    a.L('kp1'); a.cmpbi(T, K_UP); J('JNE', 'kp2'); lda(T, SEL); a.cmpbi(T, 0); J('JE', 'kpr'); a.decb(T)
    sta(SEL, T); a.ret()
    a.L('kp2'); a.cmpbi(T, K_DN); J('JNE', 'kp3'); lda(T, SEL); a.incb(T); lda(U, NITEM); a.cmpb(T, U)
    J('JC', 'kpr'); sta(SEL, T); a.ret()
    a.L('kp3'); a.cmpbi(T, K_LT); J('JNE', 'kp4'); a.clrb(T); sta(COL, T); a.sjmp('kclamp')
    a.L('kp4'); a.cmpbi(T, K_RT); J('JNE', 'kp5')
    lda(U, KIND); a.cmpbi(U, K_SPLIT); J('JNE', 'kpr'); a.ldbi(T, 1); sta(COL, T)
    a.L('kclamp'); a.lcall('page_ptr'); lda(T, SEL); lda(U, NITEM); a.cmpb(T, U); J('JNC', 'kpr')
    a.decb(U); sta(SEL, U); a.ret()
    a.L('kp5'); a.cmpbi(T, K_INC); J('JE', 'kval'); a.cmpbi(T, K_DEC); J('JE', 'kval')
    a.L('kpr'); a.ret()
    a.L('kval')                                          # INC / DEC the selected item
    lda(T, COL); lda(U, SEL); a.lcall('item_load')
    lda(T, ITYPE); a.cmpbi(T, T_NAME); J('JE', 'kpr')
    lda(T, SEC); a.cmpbi(T, 4); J('JNE', 'kv0'); a.clrb(T)
    a.L('kv0'); sta(SECW, T); a.lcall('item_read')      # value from the section (L1 when ALL)
    lda(T, VAL); lda(U, KEYC); a.cmpbi(U, K_INC); J('JNE', 'kv_dn')
    lda(U, IMAX); a.cmpb(T, U); J('JC', 'kpr'); a.incb(T); a.sjmp('kv_w')
    a.L('kv_dn'); lda(U, IMIN); a.cmpb(U, T); J('JC', 'kpr'); a.decb(T)
    a.L('kv_w'); sta(VAL, T); a.lcall('item_write'); a.ret()

    a.L('key_root')
    a.cmpbi(T, K_EXIT); J('JE', 'to_parent'); a.cmpbi(T, K_LT); J('JE', 'to_parent')
    a.cmpbi(T, K_UP); J('JNE', 'kr2'); lda(T, RSEL); a.cmpbi(T, 0); J('JE', 'kpr'); a.decb(T)
    sta(RSEL, T); a.sjmp('fix_top')
    a.L('kr2'); a.cmpbi(T, K_DN); J('JNE', 'kr3'); lda(T, RSEL); a.incb(T); lda(U, NITEM); a.cmpb(T, U)
    J('JC', 'kpr'); sta(RSEL, T); a.sjmp('fix_top')
    a.L('kr3'); a.cmpbi(T, K_ENT); J('JE', 'kr4'); a.cmpbi(T, K_RT); J('JNE', 'kpr')
    a.L('kr4'); lda(U, RSEL); a.lcall('list_item')                  # Q = item: label, kind, arg, info
    a.ldb_idx(T, Q, 2); a.cmpbi(T, LI_LINK); J('JNE', 'kr5')
    lda(T, PG); a.ldbze(IDX, T); lda(U, RSEL); a.stb_lidx(IDX, RSAVE, U)      # remember where we were
    a.ldb_idx(T, Q, 3); sta(PG, T); a.clrb(T); sta(SEL, T); sta(COL, T); sta(RSEL, T); sta(RTOP, T); a.ret()
    a.L('kr5'); a.cmpbi(T, LI_ACT); J('JNE', 'kpr')
    a.ldb_idx(T, Q, 3); a.cmpbi(T, ACT_CLOSE); J('JNE', 'kr6')
    a.L('close'); a.clrb(T); sta(M_ON, T); a.ret()
    a.L('kr6'); a.ljmp('dump')
    a.L('to_parent')                                     # EXIT: up one level (top level: close the menu)
    a.lcall('page_ptr'); ldw(P, PAGEP); a.ldb_idx(T, P, 5); a.cmpbi(T, 0xFF); J('JE', 'close')
    sta(PG, T); a.ldbze(IDX, T); a.ldb_lidx(U, IDX, RSAVE); sta(RSEL, U); a.clrb(U); sta(RTOP, U)
    sta(SEL, U); sta(COL, U)
    a.L('fix_top')                                       # keep the selection inside the 7 visible rows
    lda(T, RSEL); lda(U, RTOP); a.cmpb(T, U); J('JC', 'krt1'); sta(RTOP, T); a.ret()
    a.L('krt1'); a.ldb(V, U); a.addbi(V, 7); a.cmpb(T, V); J('JNC', 'kpr'); a.subbi(T, 6); sta(RTOP, T); a.ret()
    a.L('list_item')                                     # U = index -> Q = list item of the current page
    ldw(P, PAGEP); a.ld_idx(Q, P, 10); a.ldbze(IDX, U); a.shl(IDX, 3); a.add(Q, IDX); a.ret()

    a.L('key_layers')
    a.cmpbi(T, K_EXIT); J('JNE', 'kl1'); a.ljmp('to_parent')
    a.L('kl1'); a.cmpbi(T, K_UP); J('JNE', 'kl2'); lda(T, SEC); a.cmpbi(T, 0); J('JE', 'kpr')
    a.cmpbi(T, 4); J('JNE', 'kl1b'); a.ldbi(T, 1)
    a.L('kl1b'); a.decb(T); sta(SEC, T); a.ret()
    a.L('kl2'); a.cmpbi(T, K_DN); J('JNE', 'kl3'); lda(T, SEC); a.cmpbi(T, 4); J('JNE', 'kl2b'); a.clrb(T)   # ALL sits on the L1 row
    a.L('kl2b'); a.cmpbi(T, 3); J('JC', 'kpr'); a.incb(T)
    sta(SEC, T); a.ret()
    a.L('kl3'); a.cmpbi(T, K_ENT); J('JNE', 'kl4'); a.ldbi(T, PIDX['wave']); sta(PG, T); a.clrb(T)
    sta(SEL, T); sta(COL, T); a.ret()
    a.L('kl4'); a.cmpbi(T, K_INC); J('JE', 'kl5'); a.cmpbi(T, K_DEC); J('JNE', 'kpr')
    a.L('kl5')                                           # INC / DEC = section level
    lda(T, SEC); a.cmpbi(T, 4); J('JNE', 'kl6'); a.clrb(T); sta(SEC, T)
    a.L('kl6'); a.ldbi(T, A_PSEC); sta(IAREA, T); a.ldbi(T, 0x01); sta(IOFF, T); a.ldbi(T, 127); sta(IMAX, T)
    a.clrb(T); sta(IMIN, T); sta(ITYPE, T); lda(T, SEC); sta(SECW, T); a.lcall('item_read')
    lda(T, VAL); lda(U, KEYC); a.cmpbi(U, K_INC); J('JNE', 'kl7'); a.cmpbi(T, 127); J('JE', 'kpr'); a.incb(T)
    a.sjmp('kl8')
    a.L('kl7'); a.cmpbi(T, 0); J('JE', 'kpr'); a.decb(T)
    a.L('kl8'); sta(VAL, T); a.clrb(RDE); a.lcall('write1'); stock(APPLY, P2); a.ret()

    # ================= items, addresses, stock read/write =================
    a.L('page_ptr')                                      # PAGEP, KIND, NITEM (current column), TL for PG
    lda(T, PG); a.ldbze(IDX, T); a.shl(IDX, 4); a.ldi_l(P, 'pages'); a.add(P, IDX); stw(PAGEP, P)
    a.ldb_idx(T, P, 2); sta(KIND, T)
    lda(U, COL); a.ldb_idx(T, P, 3); a.cmpbi(U, 0); J('JE', 'pp1'); a.ldb_idx(T, P, 4)
    a.L('pp1'); sta(NITEM, T)
    a.ld_idx(Q, P, 0); a.lcall('strlen'); a.addbi(T, 2); sta(TL, T); a.ret()

    a.L('item_load')                                     # T = column, U = row -> item fields
    ldw(P, PAGEP); a.ld_idx(Q, P, 10); a.cmpbi(T, 0); J('JE', 'il1'); a.ld_idx(Q, P, 12)
    a.L('il1'); a.ldbze(IDX, U); a.shl(IDX, 3); a.add(Q, IDX); stw(ITEMP, Q)
    for off, var in ((2, IAREA), (3, IOFF), (4, ITYPE), (5, IMIN), (6, IMAX), (7, IAUX)):
        a.ldb_idx(T, Q, off); sta(var, T)
    a.ret()

    a.L('addr')                                          # ADDR for IAREA / IOFF / section SECW, part R5F
    lda(T, IAREA); lda(U, SECW)
    a.cmpbi(T, A_TONE); J('JNE', 'ad1')
    a.ldbze(W, R5F); a.shl(W, 2); a.ldbze(IDX, U); a.add(W, IDX)
    a.b(0x6D, 0x3E, 0x00, W)                             # MULU W, #003E
    a.addi(W, 0x0351); a.sjmp('ad_off')
    a.L('ad1'); a.ldbze(W, R5F); a.b(0x6D, 0x57, 0x00, W); a.addi(W, 0x019E)   # W = patch of part
    a.cmpbi(T, A_PCOM); J('JE', 'ad_off')
    a.ldbze(IDX, U); a.b(0x6D, 0x0D, 0x00, IDX); a.add(W, IDX); a.addi(W, 0x23)
    a.L('ad_off'); lda(T, IOFF); a.ldbze(IDX, T); a.add(W, IDX); stw(ADDR, W); a.ret()

    a.L('item_read')                                     # VAL = value at the item for section SECW
    a.lcall('addr'); ldw(RD4, ADDR); a.clr(RD6); stock(RD, P5); sta(VAL, RCA); a.ret()
    a.L('write1')                                        # VAL -> item for section SECW (no apply)
    a.lcall('addr'); ldw(RD4, ADDR); a.clr(RD6); lda(RCA, VAL); stock(WR, P5); a.ret()
    a.L('item_write')                                    # VAL -> current section, or all four, then apply
    a.clrb(RDE)
    lda(T, SEC); a.cmpbi(T, 4); J('JE', 'iw_all'); sta(SECW, T); a.lcall('write1'); a.sjmp('iw_ap')
    a.L('iw_all'); a.clrb(T); sta(SECW, T)
    a.L('iw_l'); a.lcall('write1'); lda(T, SECW); a.incb(T); sta(SECW, T); a.cmpbi(T, 4); J('JNE', 'iw_l')
    a.L('iw_ap'); stock(APPLY, P2); a.ret()

    # ================= value text =================
    a.L('fmt')                                           # VAL, ITYPE, IAUX -> NB (ASCII, FF end); Q -> text
    a.ldi(Q, NB); lda(T, ITYPE)
    a.cmpbi(T, T_LIST); J('JNE', 'f1')
    lda(T, IAUX); a.ldbze(IDX, T); a.add(IDX, IDX); a.ld_lidx_l(Q, IDX, 'listtab')
    lda(T, VAL); a.ldbze(IDX, T); a.add(IDX, IDX); a.add(Q, IDX); a.b(0xA2, Q, Q); a.ret()   # LD Q,[Q]
    a.L('f1'); a.cmpbi(T, T_NOTE); J('JNE', 'f2')
    lda(T, VAL); a.ldbze(W, T); a.b(0x9D, 12, W)        # DIVUB W, #12 -> W lo = octave+1, hi = note
    a.ldbze(IDX, W + 1); a.add(IDX, IDX); a.ldi(P, NB)
    a.ldb_lidx_l(U, IDX, 'notes'); a.stb_pi(P, U); a.ldb_lidx_l(U, IDX, 'notes', 1)
    a.cmpbi(U, 0x20); J('JE', 'fn1'); a.stb_pi(P, U)
    a.L('fn1'); a.ldb(T, W); a.cmpbi(T, 0); J('JNE', 'fn2'); a.ldbi(U, ord('-')); a.stb_pi(P, U)
    a.ldbi(U, ord('1')); a.stb_pi(P, U); a.sjmp('fend')
    a.L('fn2'); a.decb(T); a.addbi(T, 0x30); a.stb_pi(P, T); a.sjmp('fend')
    a.L('f2'); a.cmpbi(T, T_MEDIA); J('JNE', 'f3')
    a.ldi(P, NB); lda(T, VAL); a.ldbi(U, ord('U')); a.cmpbi(T, 32); J('JNC', 'fm1')
    a.ldbi(U, ord('D')); a.subbi(T, 32); a.cmpbi(T, 32); J('JNC', 'fm1')
    a.ldbi(U, ord('I')); a.subbi(T, 32)
    a.L('fm1'); a.stb_pi(P, U); a.incb(T); a.lcall('udec'); a.sjmp('fend')
    a.L('f3'); a.ldi(P, NB); lda(T, VAL)
    lda(U, ITYPE); a.cmpbi(U, T_PLUS1); J('JNE', 'f4'); a.incb(T); a.sjmp('f5')
    a.L('f4'); lda(U, IAUX); a.cmpbi(U, 0); J('JE', 'f5')    # signed around IAUX
    a.cmpb(T, U); J('JE', 'f5z'); J('JC', 'fpos')
    a.subb(U, T); a.ldb(T, U); a.ldbi(U, ord('-')); a.stb_pi(P, U); a.sjmp('f5')
    a.L('fpos'); a.subb(T, U); a.ldbi(U, ord('+')); a.stb_pi(P, U); a.sjmp('f5')
    a.L('f5z'); a.clrb(T)
    a.L('f5'); a.lcall('udec')
    a.L('fend'); a.ldbi(U, 0xFF); a.stb_pi(P, U); a.ldi(Q, NB); a.ret()
    a.L('udec')                                          # T = 0..255 -> [P]+ decimal, no leading zeros
    a.clrb(V); a.ldbi(U, 100); a.lcall('ud1'); a.ldbi(U, 10); a.lcall('ud1')
    a.addbi(T, 0x30); a.stb_pi(P, T); a.ret()
    a.L('ud1'); a.clrb(X)
    a.L('ud2'); a.cmpb(T, U); J('JNC', 'ud3'); a.subb(T, U); a.incb(X); a.sjmp('ud2')
    a.L('ud3'); a.orb(V, X); a.cmpbi(V, 0); J('JE', 'ud4'); a.addbi(X, 0x30); a.stb_pi(P, X)
    a.L('ud4'); a.ret()

    # ================= text helpers (TXT row buffer) =================
    a.L('strlen'); a.ld(P, Q); a.clrb(T)                 # Q = string -> T = length
    a.L('sl1'); a.ldb_pi(U, P); a.cmpbi(U, 0xFF); J('JE', 'sl2'); a.incb(T); a.sjmp('sl1')
    a.L('sl2'); a.ret()
    a.L('tclr'); a.ldi(P, TXT); a.ldbi(T, 40); a.ldbi(U, 0x20)
    a.L('tc1'); a.stb_pi(P, U); a.djnz(T, 'tc1'); a.ret()
    a.L('put')                                           # Q = string, T = column
    a.ldbze(P, T); a.addi(P, TXT)
    a.L('pu1'); a.ldb_pi(U, Q); a.cmpbi(U, 0xFF); J('JE', 'sl2'); a.stb_pi(P, U); a.sjmp('pu1')
    a.L('rput')                                          # Q = string, T = column after its last char
    sta(TMPB, T); a.lcall('strlen'); lda(U, TMPB); a.subb(U, T); a.ldb(T, U); a.sjmp('put')
    a.L('tpush')                                         # TXT -> LCD text row ROW
    lda(T, ROW); a.ldbze(W, T); a.b(0x6D, 40, 0, W); a.addi(W, LCD_TEXT); a.ldi(P, TXT)
    a.L('row40')                                         # 40 bytes from [P] to LCD address W, interrupts off
    a.b(0xFA); a.lcall('setaddr'); a.ldi(Q, 40); a.ldbi(X, 0xB0); a.lcall('cmd')
    a.L('tp1'); a.lcall('wauto'); a.ldb_pi(X, P); a.stb_abs(LCD_D, X); a.dec(Q); J('JNE', 'tp1')
    a.ldbi(X, 0xB2); a.lcall('cmd'); a.b(0xFB); a.ret()

    # ================= drawing =================
    a.L('draw')
    a.lcall('lcd_mine'); a.lcall('page_ptr')
    a.clrb(T); sta(ROW, T)
    # row 0: title + section chips
    a.lcall('tclr'); ldw(P, PAGEP); a.ld_idx(Q, P, 0); a.ldbi(T, 1); a.lcall('put')
    lda(T, KIND); a.cmpbi(T, K_ROOT); J('JE', 'd0r')
    lda(T, SEC); a.cmpbi(T, 4); J('JNE', 'd0c')
    a.ldi_l(Q, 's_all'); a.ldbi(T, 39); a.lcall('rput'); a.sjmp('d0e')
    a.L('d0c'); a.ldi_l(Q, 's_chips'); a.ldbi(T, 28); a.lcall('put'); a.sjmp('d0e')
    a.L('d0r'); a.ldi_l(Q, 's_ver'); a.ldbi(T, 39); a.lcall('rput')
    a.L('d0e'); a.lcall('tpush')
    lda(T, KIND)
    a.cmpbi(T, K_ROOT); J('JNE', 'dk1'); a.lcall('draw_root'); a.sjmp('dgfx')
    a.L('dk1'); a.cmpbi(T, K_SPLIT); J('JNE', 'dk2'); a.lcall('draw_split'); a.sjmp('dgfx')
    a.L('dk2'); a.cmpbi(T, K_ENV); J('JNE', 'dk3'); a.lcall('draw_env_text'); a.sjmp('dgfx')
    a.L('dk3'); a.lcall('draw_layers')
    a.L('dgfx'); a.lcall('gfx')
    lda(T, KIND); a.cmpbi(T, K_ENV); J('JNE', 'dret'); a.lcall('env_lines')
    a.L('dret'); a.ret()

    a.L('draw_root')                                     # rows 1-7: list items (links >, info text)
    a.ldbi(T, 1); sta(ROW, T)
    a.L('drr'); a.lcall('tclr'); lda(T, ROW); a.decb(T); lda(U, RTOP); a.addb(T, U)
    lda(V, NITEM); a.cmpb(T, V); J('JC', 'drr2')
    a.ldb(U, T); a.lcall('list_item'); a.push(Q); a.ld_idx(Q, Q, 0); a.ldbi(T, 1); a.lcall('put'); a.pop(Q)
    a.ldb_idx(T, Q, 2); a.cmpbi(T, LI_LINK); J('JNE', 'drr1')
    a.ldi_l(Q, 's_gt'); a.ldbi(T, 38); a.lcall('put'); a.sjmp('drr2')
    a.L('drr1'); a.cmpbi(T, LI_INFO); J('JNE', 'drr2'); a.ld_idx(Q, Q, 4); a.ldbi(T, 39); a.lcall('rput')
    a.L('drr2'); a.lcall('tpush'); lda(T, ROW); a.incb(T); sta(ROW, T); a.cmpbi(T, 8); J('JNE', 'drr')
    lda(T, RSEL); lda(U, RTOP); a.subb(T, U); a.incb(T); sta(SELROW, T)
    a.clrb(T); sta(SC0, T); a.ldbi(T, 38); sta(SC1, T); a.ret()

    a.L('draw_split')                                    # row 1 heads, rows 2-7 two item columns
    a.ldbi(T, 1); sta(ROW, T); a.lcall('tclr')
    ldw(P, PAGEP); a.ld_idx(Q, P, 6); a.ldbi(T, 1); a.lcall('put')
    ldw(P, PAGEP); a.ld_idx(Q, P, 8); a.ldbi(T, 21); a.lcall('put'); a.lcall('tpush')
    a.ldbi(T, 2); sta(ROW, T)
    a.L('ds1'); a.lcall('tclr')
    for col in (0, 1):
        ldw(P, PAGEP); a.ldb_idx(U, P, 3 + col); lda(T, ROW); a.subbi(T, 2); a.cmpb(T, U)
        J('JC', 'ds_n%d' % col)                         # row >= item count: empty
        a.ldb(U, T); a.ldbi(T, col); a.lcall('item_load')
        a.ld_lidx(Q, 0, ITEMP); a.ld_idx(Q, Q, 0); a.ldbi(T, 1 + 20 * col); a.lcall('put')
        a.lcall('item_value'); a.ldbi(T, 18 + 21 * col); a.lcall('rput')
        a.L('ds_n%d' % col)
    a.lcall('tpush'); lda(T, ROW); a.incb(T); sta(ROW, T); a.cmpbi(T, 8); J('JNE', 'ds1')
    lda(T, SEL); a.addbi(T, 2); sta(SELROW, T)
    lda(T, COL); a.cmpbi(T, 0); J('JNE', 'ds2'); a.clrb(T); sta(SC0, T); a.ldbi(T, 18); sta(SC1, T); a.ret()
    a.L('ds2'); a.ldbi(T, 20); sta(SC0, T); a.ldbi(T, 39); sta(SC1, T); a.ret()

    a.L('item_value')                                    # Q -> value text of the loaded item (L1 / section)
    lda(T, SEC); a.cmpbi(T, 4); J('JNE', 'iv0'); a.clrb(T)
    a.L('iv0'); sta(SECW, T)
    lda(T, ITYPE); a.cmpbi(T, T_NAME); J('JE', 'iv_name')
    a.lcall('item_read'); a.lcall('fmt'); a.ret()
    a.L('iv_name')                                       # 10 name bytes -> NB.. (NB + 10 spill into ZL: free here)
    a.ldbi(T, 0); sta(NI, T)
    a.L('ivn1'); lda(T, NI); sta(IOFF, T); a.lcall('item_read')
    lda(T, NI); a.ldbze(IDX, T); lda(U, VAL); a.stb_lidx(IDX, NB, U)
    lda(T, NI); a.incb(T); sta(NI, T); a.cmpbi(T, 10); J('JNE', 'ivn1')
    a.ldbi(U, 0xFF); a.stb_abs(NB + 10, U); a.clrb(T); sta(IOFF, T); a.ldi(Q, NB); a.ret()

    a.L('draw_env_text')                                 # rows 1-7: env items in the right part
    a.ldbi(T, 1); sta(ROW, T)
    a.L('de1'); a.lcall('tclr')
    a.ldbi(T, 0); lda(U, ROW); a.decb(U); a.lcall('item_load')
    a.ld_lidx(Q, 0, ITEMP); a.ld_idx(Q, Q, 0); a.ldbi(T, 23); a.lcall('put')
    a.lcall('item_value'); a.ldbi(T, 39); a.lcall('rput')
    lda(T, ROW); a.decb(T); a.ldbze(IDX, T); lda(U, VAL); a.stb_lidx(IDX, EV, U)   # keep values for lines
    a.lcall('tpush'); lda(T, ROW); a.incb(T); sta(ROW, T); a.cmpbi(T, 8); J('JNE', 'de1')
    lda(T, SEL); a.incb(T); sta(SELROW, T); a.ldbi(T, 23); sta(SC0, T); a.ldbi(T, 39); sta(SC1, T); a.ret()

    a.L('draw_layers')
    a.ldbi(T, 1); sta(ROW, T); a.lcall('tclr')
    a.ldi_l(Q, 's_lhead'); a.ldbi(T, 1); a.lcall('put'); a.ldi_l(Q, 's_lvl'); a.ldbi(T, 39); a.lcall('rput')
    a.lcall('tpush')
    a.clrb(T); sta(I, T)
    a.L('dl1'); a.lcall('tclr')
    lda(T, I); a.ldbze(IDX, T); a.add(IDX, IDX); a.ld_lidx_l(Q, IDX, 'sec_names'); a.ldbi(T, 1); a.lcall('put')
    lda(T, I); sta(SECW, T); a.ldbi(T, A_TONE); sta(IAREA, T); a.ldbi(T, T_NAME); sta(ITYPE, T)
    a.lcall('iv_name_sec'); a.ldbi(T, 4); a.lcall('put')
    a.ldbi(T, A_PSEC); sta(IAREA, T); a.ldbi(T, T_NOTE); sta(ITYPE, T)
    a.ldbi(T, 0x0B); sta(IOFF, T); a.lcall('item_read')
    lda(T, VAL); a.shrb(T, 1); a.addbi(T, 144); lda(U, I); a.ldbze(IDX, U); a.stb_lidx(IDX, ZL, T)
    a.lcall('fmt'); a.ldbi(T, 15); a.lcall('put')
    a.ldbi(T, 0x0C); sta(IOFF, T); a.lcall('item_read')
    lda(T, VAL); a.shrb(T, 1); a.addbi(T, 144); lda(U, I); a.ldbze(IDX, U); a.stb_lidx(IDX, ZH, T)
    a.lcall('fmt'); a.ldbi(T, 19); a.lcall('put')
    a.ldbi(T, T_NUM); sta(ITYPE, T); a.clrb(T); sta(IAUX, T)
    a.ldbi(T, 0x0A); sta(IOFF, T); a.lcall('item_read'); lda(T, VAL); a.cmpbi(T, 0); J('JNE', 'dl2')
    a.ldi_l(Q, 's_off'); a.sjmp('dl3')
    a.L('dl2'); a.ldbi(T, 0x01); sta(IOFF, T); a.lcall('item_read'); a.lcall('fmt')
    a.L('dl3'); a.ldbi(T, 39); a.lcall('rput')
    lda(T, I); a.addbi(T, 2); sta(ROW, T); a.lcall('tpush')
    lda(T, I); a.incb(T); sta(I, T); a.cmpbi(T, 4); J('JNE', 'dl1')
    for r in (6, 7):
        a.ldbi(T, r); sta(ROW, T); a.lcall('tclr')
        if r == 7:
            a.ldi_l(Q, 's_mode'); a.ldbi(T, 1); a.lcall('put')
            a.ldbi(T, A_PCOM); sta(IAREA, T); a.ldbi(T, T_LIST); sta(ITYPE, T); a.ldbi(T, L('kmode'))
            sta(IAUX, T); a.ldbi(T, 0x0D); sta(IOFF, T); a.lcall('item_read'); a.lcall('fmt')
            a.ldbi(T, 6); a.lcall('put')
            a.ldi_l(Q, 's_split'); a.ldbi(T, 14); a.lcall('put')
            a.ldbi(T, T_NOTE); sta(ITYPE, T); a.ldbi(T, 0x0E); sta(IOFF, T); a.lcall('item_read')
            a.lcall('fmt'); a.ldbi(T, 20); a.lcall('put')
        a.lcall('tpush')
    lda(T, SEC); a.cmpbi(T, 4); J('JNE', 'dl4'); a.clrb(T)
    a.L('dl4'); a.addbi(T, 2); sta(SELROW, T); a.clrb(T); sta(SC0, T); a.ldbi(T, 39); sta(SC1, T); a.ret()
    a.L('iv_name_sec'); a.ldbi(T, T_NAME); sta(ITYPE, T); a.sjmp('iv_name')

    # ================= graphics layer: one pass, byte by byte =================
    a.L('gfx')
    a.clrb(T); sta(ROW, T)                               # ROW = pixel row y (0..63)
    a.L('gy'); a.clrb(T); sta(I, T)                      # I = byte column (0..39)
    a.L('gx'); a.clrb(X)
    lda(T, ROW); a.ldb(U, T); a.shrb(U, 3); a.andbi(T, 7)    # U = text row, T = y in row
    lda(V, I)
    a.cmpbi(U, 0); J('JNE', 'g_body')
    lda(C, TL); a.cmpb(V, C); J('JC', 'g_t1'); a.ldbi(X, 0x3F); a.sjmp('g_chip')    # inside the title tab
    a.L('g_t1'); a.cmpbi(T, 7); J('JNE', 'g_chip'); a.ldbi(X, 0x3F)
    a.L('g_chip'); lda(C, KIND); a.cmpbi(C, K_ROOT); J('JE', 'g_out')
    lda(C, SEC); a.cmpbi(C, 4); J('JNE', 'g_c1')
    a.cmpbi(V, 29); J('JNC', 'g_out'); a.lcall('g_inv'); a.sjmp('g_out')
    a.L('g_c1'); a.ldb(Z, C); a.addb(Z, C); a.addb(Z, C); a.addbi(Z, 28)        # chip column = 28 + 3*sec
    a.cmpb(V, Z); J('JNC', 'g_out'); a.incb(Z); a.incb(Z); a.cmpb(V, Z); J('JC', 'g_out'); a.lcall('g_inv')
    a.sjmp('g_out')
    a.L('g_body'); lda(C, KIND)
    a.cmpbi(C, K_SPLIT); J('JNE', 'g_b2')
    a.cmpbi(V, 19); J('JNE', 'g_s1'); a.ldbi(X, 0x01); a.sjmp('g_sel')     # divider x = 119
    a.L('g_s1'); a.cmpbi(U, 1); J('JNE', 'g_sel'); a.cmpbi(T, 7); J('JNE', 'g_sel'); a.ldbi(X, 0x3F)
    a.sjmp('g_sel')
    a.L('g_b2'); a.cmpbi(C, K_ENV); J('JNE', 'g_b3')
    a.cmpbi(V, 22); J('JNE', 'g_sel'); a.ldbi(X, 0x20); a.sjmp('g_sel')      # divider x = 132
    a.L('g_b3'); a.cmpbi(C, K_LAYERS); J('JNE', 'g_sel')
    a.cmpbi(U, 1); J('JNE', 'g_l1'); a.cmpbi(T, 7); J('JNE', 'g_sel'); a.ldbi(X, 0x3F); a.sjmp('g_sel')
    a.L('g_l1'); a.cmpbi(U, 2); J('JNC', 'g_sel'); a.cmpbi(U, 6); J('JC', 'g_sel')
    a.cmpbi(T, 2); J('JNC', 'g_sel'); a.cmpbi(T, 6); J('JC', 'g_sel')
    a.lcall('g_zone')
    a.L('g_sel'); lda(C, SELROW); a.cmpb(U, C); J('JNE', 'g_out')
    lda(C, SC0); a.cmpb(V, C); J('JNC', 'g_out'); lda(C, SC1); a.cmpb(C, V); J('JNC', 'g_out')
    a.lcall('g_inv')
    a.L('g_out'); lda(T, I); a.ldbze(IDX, T); a.stb_lidx(IDX, GBUF, X)
    lda(T, I); a.incb(T); sta(I, T); a.cmpbi(T, 40); J('JNE', 'gx')
    lda(T, ROW); a.ldbze(W, T); a.b(0x6D, 40, 0, W); a.addi(W, LCD_GFX); a.ldi(P, GBUF); a.lcall('row40')
    lda(T, ROW); a.incb(T); sta(ROW, T); a.cmpbi(T, 64); J('JNE', 'gy')
    a.ret()
    a.L('g_inv'); a.b(0x95, 0x3F, X); a.ret()           # XORB X, #3F
    a.L('g_zone')                                        # zone bar bits for byte column V in text row U
    a.ldbze(IDX, U); a.subi(IDX, 2); a.ldb_lidx(Z, IDX, ZL); a.ldb_lidx(Z + 1, IDX, ZH)
    a.ldb(C, V); a.ldbi(C + 1, 6); a.b(0x5C, C + 1, C, C)   # MULUB C, C, C+1 -> C = column * 6 (word)
    a.ldbi(T, 0x20); a.ldbi(U, 6)                        # T = pixel bit, U = pixels left
    a.L('gz1'); a.cmpb(C, Z); J('JNC', 'gz2'); a.cmpb(Z + 1, C); J('JNC', 'gz2'); a.orb(X, T)
    a.L('gz2'); a.incb(C); a.shrb(T, 1); a.djnz(U, 'gz1')
    lda(T, ROW); a.ldb(U, T); a.shrb(U, 3); a.andbi(T, 7); a.ret()            # restore T, U

    # ================= envelope lines (T6963C bit set) =================
    a.L('env_lines')                                     # EV.. = A T, A L, D1 T, BP, D2 T, SUS, REL
    a.clr(W); stw(LX1, W); a.ldi(W, 62); stw(LY1, W)    # start at the bottom left
    for t_, l_ in ((EV, EV + 1), (EV + 2, EV + 3), (EV + 4, EV + 5), (None, EV + 5), (EV + 6, None)):
        if t_ is None: a.ldbi(V, 40)
        else: a.ldb_abs(V, t_)
        a.lcall('ex')
        if l_ is None: a.clrb(V)
        else: a.ldb_abs(V, l_)
        a.lcall('ey'); a.lcall('seg')
    a.ret()
    a.L('ex'); ldw(W, LX1); stw(LX0, W); a.ldbze(Q, V); a.b(0x9D, 5, Q); a.ldbze(Q, Q)   # x += time / 5
    a.add(W, Q); stw(LX1, W); a.ret()
    a.L('ey'); ldw(P, LY1); stw(LY0, P)                  # y = 62 - level * 13 / 32
    a.ldbze(Q, V); a.ld(P, Q); a.shl(P, 3); a.add(P, Q); a.add(P, Q); a.add(P, Q); a.add(P, Q); a.add(P, Q)
    a.shr(P, 5); a.ldi(Q, 62); a.sub(Q, P); stw(LY1, Q); a.ret()
    a.L('seg')                                           # Bresenham LX0,LY0 -> LX1,LY1 (x never goes back)
    ldw(W, LX0); ldw(P, LX1); a.sub(P, W); stw(LDX, P)
    ldw(W, LY0); ldw(Q, LY1); a.ldbi(T, 1); a.cmp(Q, W); J('JC', 'sg1'); a.ldbi(T, 0xFF)
    a.ld(V, W); a.sub(V, Q); a.ld(Q, V); a.sjmp('sg2')
    a.L('sg1'); a.sub(Q, W)
    a.L('sg2'); stw(LDY, Q); sta(LSY, T)                 # LDX = dx, LDY = |dy|, LSY = y step
    ldw(W, LDX); a.sub(W, Q); stw(LERR, W)              # err = dx - dy
    a.L('sg3'); a.lcall('plot')
    ldw(W, LX0); ldw(P, LX1); a.cmp(W, P); J('JNE', 'sg4')
    ldw(W, LY0); ldw(P, LY1); a.cmp(W, P); J('JE', 'sg9')
    a.L('sg4'); ldw(W, LERR); a.add(W, W); stw(LE2, W)   # e2 = 2 * err
    ldw(P, LDY); a.add(W, P); a.cmpi(W, 0); J('JLE', 'sg5')                  # e2 > -dy ?
    ldw(W, LERR); a.sub(W, P); stw(LERR, W); ldw(W, LX0); a.inc(W); stw(LX0, W)
    a.L('sg5'); ldw(W, LE2); ldw(P, LDX); a.cmp(W, P); J('JGE', 'sg3')         # e2 < dx ?
    ldw(W, LERR); a.add(W, P); stw(LERR, W)
    ldw(W, LY0); lda(T, LSY); a.cmpbi(T, 1); J('JE', 'sgy1'); a.dec(W); a.sjmp('sgy2')
    a.L('sgy1'); a.inc(W)
    a.L('sgy2'); stw(LY0, W); a.sjmp('sg3')
    a.L('sg9'); a.ret()
    a.L('plot')                                          # bit set at LX0+2, LY0 (graph area x 2..130)
    ldw(W, LX0); a.addi(W, 2); a.b(0x9D, 6, W)           # DIVUB W, #6 -> lo = column, hi = bit from left
    ldw(P, LY0); a.b(0x6D, 40, 0, P); a.ldbze(Q, W); a.add(P, Q); a.addi(P, LCD_GFX)
    a.ldbi(T, 5); a.subb(T, W + 1); sta(TMPB, T)
    a.ld(W, P); a.b(0xFA); a.lcall('setaddr'); lda(X, TMPB); a.orbi(X, 0xF8); a.lcall('cmd'); a.b(0xFB)
    a.ret()

    # ================= TOOLS > DUMP IC6: 256 SysEx messages F0 7D 49 half blk <256 nibbles> F7 =================
    a.L('dump')
    a.ldbi(T, 7); sta(ROW, T); a.lcall('tclr'); a.ldi_l(Q, 's_sending'); a.ldbi(T, 1); a.lcall('put')
    a.lcall('tpush')
    a.clrb(T); sta(I, T)                                 # I = block 0..255 (128 bytes each)
    a.L('db')
    for v in (0xF0, 0x7D, 0x49): a.ldbi(X, v); a.lcall('tx')
    lda(X, I); a.shrb(X, 7); a.lcall('tx'); lda(X, I); a.andbi(X, 0x7F); a.lcall('tx')
    a.clrb(T); sta(NI, T)
    a.L('dn')
    lda(T, I); a.ldbze(W, T); a.andbi(W, 0x7F); a.shl(W, 7); lda(T, NI); a.ldbze(IDX, T); a.add(W, IDX)
    a.addi(W, 0x8000); lda(T, I); a.shrb(T, 7); a.orbi(T, 0x20)
    a.lcall(IC6_RD)                                      # fixed-ROM helper: bank T, address W -> V
    sta(VAL, V); a.ldb(X, V); a.shrb(X, 4); a.lcall('tx'); lda(X, VAL); a.andbi(X, 0x0F); a.lcall('tx')
    lda(T, NI); a.incb(T); sta(NI, T); a.cmpbi(T, 128); J('JNE', 'dn')
    a.ldbi(X, 0xF7); a.lcall('tx')
    lda(T, I); a.incb(T); sta(I, T); a.cmpbi(T, 0); J('JNE', 'db'); a.ret()
    a.L('tx'); a.b(0xC4, SBUF_, X); a.ld(C, TIMER1_)    # 0.4 ms per byte (31250 baud = 0.32 ms)
    a.L('txw'); a.ld(P, TIMER1_); a.sub(P, C); a.cmpi(P, 300); J('JNC', 'txw'); a.ret()

    # ================= LCD =================
    a.L('wst'); a.ldb_abs(ST, LCD_C); a.andbi(ST, 3); a.cmpbi(ST, 3); J('JNE', 'wst'); a.ret()
    a.L('wauto'); a.ldb_abs(ST, LCD_C); a.jbc(ST, 3, 'wauto'); a.ret()
    a.L('cmd'); a.lcall('wst'); a.stb_abs(LCD_C, X); a.ret()
    a.L('dat'); a.lcall('wst'); a.stb_abs(LCD_D, X); a.ret()
    a.L('setaddr'); a.ldb(X, W); a.lcall('dat'); a.ldb(X, W + 1); a.lcall('dat'); a.ldbi(X, 0x24); a.sjmp('cmd')

    # ================= data =================
    def s(name, text): a.L(name); a.data(bytes(text, 'ascii') + b'\xff')
    a.align(); a.L('pages')
    for key, title, kind, h0, h1, i0, i1 in PAGES:
        n0 = len(LIST_PAGES[key]) if kind == K_ROOT else len(i0)
        par = 0xFF if PARENT[key] is None else PIDX[PARENT[key]]
        a.dw('t_' + key); a.data([kind, n0, len(i1), par]); a.dw('h0_' + key); a.dw('h1_' + key)
        a.dw('i0_' + key); a.dw('i1_' + key); a.data([0, 0])
    for key, title, kind, h0, h1, i0, i1 in PAGES:
        s('t_' + key, title); s('h0_' + key, h0); s('h1_' + key, h1)
        if kind == K_ROOT:
            a.align(); a.L('i0_' + key); a.L('i1_' + key)
            for n, (lab, k_, arg) in enumerate(LIST_PAGES[key]):
                a.dw('ll_%s_%d' % (key, n))
                a.data([k_, PIDX[arg] if k_ == LI_LINK else (arg if k_ == LI_ACT else 0)])
                a.dw('li_%s_%d' % (key, n) if k_ == LI_INFO else 0); a.dw(0)
            for n, (lab, k_, arg) in enumerate(LIST_PAGES[key]):
                s('ll_%s_%d' % (key, n), lab)
                if k_ == LI_INFO: s('li_%s_%d' % (key, n), arg)
            continue
        for col, items in ((0, i0), (1, i1)):
            a.align(); a.L('i%d_%s' % (col, key))
            for n, (lab, area, off, typ, mn, mx, aux) in enumerate(items):
                a.dw('lb_%s_%d_%d' % (key, col, n)); a.data([area, off, typ, mn, mx, aux])
            for n, it_ in enumerate(items):
                s('lb_%s_%d_%d' % (key, col, n), it_[0])
    a.align(); a.L('listtab')
    for k in LISTS: a.dw('lt_' + k)
    for k, names in LISTS.items():
        a.align(); a.L('lt_' + k)
        for n in range(len(names)): a.dw('ls_%s_%d' % (k, n))
        for n, nm in enumerate(names): s('ls_%s_%d' % (k, n), nm)
    a.L('notes'); a.data(b'C C#D D#E F F#G G#A A#B ')
    a.align(); a.L('sec_names')
    for n in ('L1', 'L2', 'U3', 'U4'): a.dw('sn_' + n)
    for n in ('L1', 'L2', 'U3', 'U4'): s('sn_' + n, n)
    s('s_all', 'ALL LAYERS'); s('s_chips', 'L1 L2 U3 U4'); s('s_ver', 'V0.5'); s('s_sending', 'SENDING IC6 ... ABOUT 30 S'); s('s_gt', '>')
    s('s_lhead', 'SEC TONE      ZONE'); s('s_lvl', 'LVL'); s('s_off', 'OFF'); s('s_mode', 'MODE'); s('s_split', 'SPLIT')
