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

ORG = 0x8143                   # page 7, after the kept sample-table readers 0x8133 / 0x813B
END = 0x9BBF                   # code limit; 0x9BA0-0x9C47 + fixed ROM blocks hold SYNTH data (DATA_SEGS)
# stock entry points
GETKEY, P4 = 0x8006, 0x1056    # LD R1A,#8006 ; LCALL 1056 -> C = key, R1C = code
DIAL = 0x8018                  # page 4 0xAA9C: panel counter byte, NEGB -> R1C = signed clicks (stock 0x1A35)
SL_LOOP = 0x4CB6               # stock play-screen slider loop (-> sliders.py hook); only stock screens call it
SLD, SLD_LAST = 0xD39C, 0xD3A6 # slider 1 now / last seen by that loop
RD, WR, P5 = 0x9DEF, 0x9E7E, 0x1059
APPLY, P2 = 0x10CB, 0x1050
IC6_RD = None                  # set by the build: fixed-ROM helper (bank R1C, address R20 -> R26)
STOCK_UI = 0x33FF
SCR_INIT, SCR_DRAW, SCR_LEDS = 0x37F1, 0x38A5, 0x4B1C
KEYMAP_USER = (0xD37C, 0x02)   # bank 4 bit 1
KEYMAP_FX = (0xD37F, 0x01)     # EFFECT/CTRL: bank 7 bit 0 (key code 0x1A, table page 4 0xA703)
KEYMAP_MIDI = (0xD37B, 0x02)   # MIDI OUT: bank 3 bit 1 (key code 0x19)
KEYMAP_CARD = (0xD37D, 0x01)   # PCM CARD: bank 5 bit 0 (key code 0x34); stock would start its tone list
K_FXB, K_MIDB, K_CARDB = 0x1A, 0x19, 0x34
GK_STOCK = 0xA5D8              # page 4: stock key read (scans the panel itself); jump table 0x8006 -> gk_hook
CARDQ = 0xC4AF                 # gk_hook took PCM CARD from stock (menu closed): rt_ui opens the wave page
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
STEPS = 0xC448                 # INC / DEC steps for this key (the dial gives up to 8)
RD_PG = 0xC449                 # redirect: page to open
SET_OFF = 0x20                 # A_RAM offsets from here are SETTINGS (never snapped)
KLOCK = 0xC600 + SET_OFF       # KNOB LOCK: 0 = ON (the default after power-up), 1 = OFF
L_VALID, L_REC, L_SEC = 0xC628, 0xC629, 0xC62F   # locked item: ITYPE IAUX IMIN IMAX IAREA IOFF, section
R56_, R5A_ = 0x56, 0x5A        # stock: play screen, UI mode (0 = play)
SCR_OFF, SCR_ENTER = 0x52ED, 0x34F4   # stock screen change: LEDs off, then BR to the enter routine of R56
REDIRECT = [(0x13, 0x13, 'layers'), (0x46, 0x46, 'wave'), (0x14, 0x1E, 'midi'), (0x1F, 0x20, 'fx'), (0x2C, 0x2E, 'midi'), (0x2F, 0x2F, 'fx'),
            (0x36, 0x3F, 'synth'), (0x41, 0x44, 'tools'), (0x4A, 0x4B, 'midi')]   # R56 ranges cut in cuts.py
EV = 0xC480                    # envelope page: the 7 values, for the lines
NB = 0xC490                    # number / name text (16)
ZL, ZH = 0xC4A0, 0xC4A4        # layers page: zone bar start/end in pixels (4 each)
STOP = 0xC44A                  # LIST pages: first item on screen
LZ_PG = 0xC44B                 # LIST pages: zone strip read for this page (0xFF = read again)
RB_T, RB_V = 0xC44C, 0xC452    # LIST pages: per visible row, bar type (0 none, 1 level, 2 pan) + value
BX0, BX1 = 138, 202            # bars: x range (values end at x 216+)
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

A_TONE, A_PSEC, A_PCOM, A_PERF, A_RAM, A_MOUT = 0, 1, 2, 3, 4, 5   # PERF = performance common (SysEx 0x00AD), RAM = our own
MOUT_BASE = 0x00AD + 0xD5      # A_MOUT: performance MIDI OUT section s at MOUT_BASE + s * 7 (manual table 22)
FX_LAST = 0xB7                 # PERF offsets 0xAD..0xB7 are the effects (reload the chip after a write)
RAM_BASE = 0xC600              # A_RAM items: settings in RAM (arp.py), offset = item offset
T_NUM, T_PLUS1, T_LIST, T_NOTE, T_MEDIA, T_NAME, T_WAVE, T_WNAME, T_LINK = 0, 1, 2, 3, 4, 5, 6, 7, 8   # LINK: aux = page
K_ROOT, K_SPLIT, K_ENV, K_LAYERS, K_LIST, K_PEEK = 0, 1, 2, 3, 4, 5   # LIST: one scrolling column; PEEK: DSP cells
# DSP PEEK (TOOLS): the effects chip's 32 RAM A cells, read back live (STB 0A08 <- cell, LDB 0A00-0A02; the stock
# firmware never reads them, the readback is from the RCC model rccBus.cpp). PEAK = highest bit used since ENTER.
RCC_RD, RCC_D0 = 0x0A08, 0x0A00
PK_PEAK, PK_MODE, PK_INIT = 0xC680, 0xC6A0, 0xC6A1  # 32 peaks (bits 0-23), 0 PEAK / 1 LIVE, A5 = peaks cleared
PK_ROUNDS = 8                  # reads of all 32 cells per menu pass, for the peaks

LISTS = {'offon': ['OFF', 'ON'], 'tvf': ['LPF', 'BPF', 'HPF', 'BYPASS'], 'lfo': ['TRI', 'SINE', 'SQUARE', 'SAW', 'RANDOM'],
         'ofs': ['-100%', '-50%', '0', '+50%', '+100%'], 'out': ['DRY', 'REVERB', 'CHORUS', 'DIRECT'],
         'kmode': ['LAYER', 'SPLIT', 'ZONE'], 'ab': ['A', 'B'],
         'rev': ['ROOM 1', 'ROOM 2', 'ROOM 3', 'HALL 1', 'HALL 2', 'GATE', 'DELAY', 'X DELAY'],
         'cho': ['CHORUS 1', 'CHORUS 2', 'FB-CHORUS', 'FLANGER', 'SHORT DLY'], 'prepost': ['PRE REV', 'POST REV'],
         'arpm': ['UP', 'DOWN', 'UP-DOWN', 'RANDOM'], 'lock': ['ON', 'OFF'], 'dlmv': ['OFF  >', 'ON  >']}
LIST_IDS = {k: i for i, k in enumerate(LISTS)}


def it(label, area, off, typ, mn, mx, aux=0):
    return (label, area, off, typ, mn, mx, aux)


def L(name):
    return LIST_IDS[name]


PAGES = [  # (key, title, kind, head0, head1, items0, items1); list pages take their items from LIST_PAGES
    ('rosetta', 'ROSETTA OS', K_ROOT, '', '', [], []),
    ('settings', 'SETTINGS', K_SPLIT, 'SLIDER 1', '',
     [it('KNOB LOCK', A_RAM, 0x20, T_LIST, 0, 1, L('lock'))], []),
    ('synth', 'SYNTH', K_ROOT, '', '', [], []),
    ('tools', 'TOOLS', K_ROOT, '', '', [], []),
    ('about', 'ABOUT', K_ROOT, '', '', [], []),
    ('layers', 'SOUND', K_LAYERS, '', '', [], []),        # the four layers at a glance; ENTER = that layer
    ('wave', 'LAYER', K_LIST, '', '',                      # one layer, everything about it, one scrolling column
     [it('WAVE', A_TONE, 0x0A, T_WAVE, 0, 0),           # INC/DEC sweep all waves; shows number + name
      it('ZONE LOW', A_PSEC, 0x0B, T_NOTE, 0, 127), it('ZONE HIGH', A_PSEC, 0x0C, T_NOTE, 0, 127),
      it('LEVEL', A_PSEC, 0x01, T_NUM, 0, 127), it('PAN', A_PSEC, 0x09, T_NUM, 0, 14, 7),   # drawn as bars too
      it('SHIFT', A_PSEC, 0x02, T_NUM, 0, 48, 24), it('FINE', A_PSEC, 0x03, T_NUM, 0, 127, 64),
      it('OUTPUT', A_PSEC, 0x08, T_LIST, 0, 3, L('out')),
      it('DLM', A_TONE, 0x0C, T_LIST, 0, 1, L('dlmv'))], []),   # ENTER: the DLM page + loop view
    ('dlm', 'DLM', K_SPLIT, 'DLM', 'LOOP SLICE',          # Differential Loop Modulation: loops a slice of the wave
     [it('DLM', A_TONE, 0x0C, T_LIST, 0, 1, L('offon')), it('MODE', A_TONE, 0x0D, T_LIST, 0, 1, L('ab')),
      it('START', A_TONE, 0x0E, T_NUM, 0, 127), it('LENGTH', A_TONE, 0x0F, T_PLUS1, 0, 127)], []),
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
    ('fx', 'EFFECTS', K_SPLIT, 'REVERB', 'CHORUS',      # performance common, SysEx 0x00AD + 0xAD..0xB7
     [it('TYPE', A_PERF, 0xAD, T_LIST, 0, 7, L('rev')), it('TIME', A_PERF, 0xAE, T_NUM, 0, 31),
      it('LEVEL', A_PERF, 0xAF, T_NUM, 0, 31), it('FEEDBACK', A_PERF, 0xB0, T_NUM, 0, 31),
      it('CHO OUT', A_PERF, 0xB2, T_LIST, 0, 1, L('prepost'))],
     [it('TYPE', A_PERF, 0xB1, T_LIST, 0, 4, L('cho')), it('LEVEL', A_PERF, 0xB3, T_NUM, 0, 31),
      it('DELAY', A_PERF, 0xB4, T_NUM, 0, 31), it('RATE', A_PERF, 0xB5, T_NUM, 0, 31),
      it('DEPTH', A_PERF, 0xB6, T_NUM, 0, 31), it('FEEDBACK', A_PERF, 0xB7, T_NUM, 0, 62, 31)]),
    ('arp', 'ARP', K_SPLIT, 'ARPEGGIO', '',             # arp.py settings, RAM 0xC600
     [it('ARP', A_RAM, 0, T_LIST, 0, 1, L('offon')), it('MODE', A_RAM, 1, T_LIST, 0, 3, L('arpm')),
      it('OCTAVES', A_RAM, 2, T_PLUS1, 0, 3), it('TEMPO', A_RAM, 3, T_NUM, 40, 240),
      it('HOLD', A_RAM, 4, T_LIST, 0, 1, L('offon'))], []),
    ('midi', 'MIDI OUT', K_SPLIT, 'KEYS / ZONE', 'SECTION',  # performance MIDI OUT (manual tables 4 + 22)
     [it('KEY MODE', A_PERF, 0xBA, T_LIST, 0, 2, L('kmode')), it('SPLIT', A_PERF, 0xBB, T_NOTE, 0, 127),
      it('VELO CRV', A_PERF, 0xB8, T_PLUS1, 0, 3), it('VELO SENS', A_PERF, 0xB9, T_NUM, 0, 127),
      it('ZONE LO', A_MOUT, 4, T_NOTE, 0, 127), it('ZONE HI', A_MOUT, 5, T_NOTE, 0, 127)],
     [it('MIDI', A_MOUT, 6, T_LIST, 0, 1, L('offon')), it('CHANNEL', A_MOUT, 0, T_PLUS1, 0, 15),
      it('PROGRAM', A_MOUT, 2, T_PLUS1, 0, 127), it('VOLUME', A_MOUT, 1, T_NUM, 0, 127),
      it('XPOSE', A_MOUT, 3, T_NUM, 0, 48, 24)]),
    ('peek', 'DSP PEEK', K_PEEK, '', '', [], []),       # TOOLS: effects chip cells, live
]
SYNTH_ITEMS = ['layers', 'pitch', 'filter', 'fenv', 'amp', 'aenv', 'lfo', 'fx', 'arp', 'midi']
ROOT_LABELS = {'layers': 'SOUND', 'pitch': 'PITCH', 'filter': 'FILTER',
               'fenv': 'FILTER ENV', 'amp': 'AMP', 'aenv': 'AMP ENV', 'lfo': 'LFO', 'dlm': 'DLM',
               'fx': 'EFFECTS', 'arp': 'ARP', 'midi': 'MIDI OUT'}
PIDX = {p[0]: i for i, p in enumerate(PAGES)}
LI_LINK, LI_ACT, LI_INFO = 0, 1, 2
ACT_CLOSE, ACT_DUMP = 0, 1
LIST_PAGES = {   # list page -> items (label, kind, arg); arg = page key, action number or info text
    'rosetta': [('SYNTH', LI_LINK, 'synth'), ('QUICK PLAY', LI_LINK, 'wave'), ('TOOLS', LI_LINK, 'tools'), ('SETTINGS', LI_LINK, 'settings'),
                ('ABOUT', LI_LINK, 'about'),
                ('EXIT TO D-70', LI_ACT, ACT_CLOSE)],
    'synth': [(ROOT_LABELS[k], LI_LINK, k) for k in SYNTH_ITEMS],
    'tools': [('DUMP IC6 OVER MIDI', LI_ACT, ACT_DUMP), ('DSP PEEK', LI_LINK, 'peek')],
    'about': [('OS', LI_INFO, 'ROSETTA 0.8'), ('BY', LI_INFO, 'JSW + CLAUDE'), ('MASCOT', LI_INFO, 'HEX GIRL'),
              ('BASE', LI_INFO, 'ROLAND D-70 1.11'), ('OLD D-70', LI_INFO, 'HOLD EXIT AT POWER ON')],
}
PARENT = {'rosetta': None, 'synth': 'rosetta', 'tools': 'rosetta', 'settings': 'rosetta', 'about': 'rosetta'}
PARENT.update({k: 'synth' for k in SYNTH_ITEMS})
PARENT.update({'wave': 'layers', 'dlm': 'wave', 'peek': 'tools'})   # SOUND > LAYER > LOOP VIEW
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
    a.lcall('arp_tick')                                  # arpeggiator clock, every main-loop pass
    lda(T, M_ON); a.cmpbi(T, 0); J('JNE', 'rt_on')
    lda(T, KLOCK); a.cmpbi(T, 0); J('JNE', 'lk_x')      # KNOB LOCK (0 = ON): slider 1 keeps the last item it took
    lda(T, L_VALID); a.cmpbi(T, 0); J('JE', 'lk_x')
    lda(T, SLD); lda(U, SLD_LAST); a.cmpb(T, U); J('JE', 'lk_x')
    sta(SLD_LAST, T); a.ldbi(C, 6); a.clr(IDX)          # seen: the stock slider loop leaves it alone
    a.L('lk_c'); a.ldb_lidx(T, IDX, L_REC); a.stb_lidx(IDX, ITYPE, T); a.inc(IDX); a.djnz(C, 'lk_c')
    lda(T, L_SEC); sta(SEC, T); a.lcall('snap_go')
    a.L('lk_x')
    a.ldb(T, R5A_); a.cmpbi(T, 0x0E); J('JNE', 'rd_m')  # the old tone picker (UI mode 0x0E): back to play, LAYERS
    a.clrb(T); a.ldb(R5A_, T); a.ldbi(V, PIDX['layers']); a.sjmp('rd_go')
    a.L('rd_m'); a.cmpbi(T, 0); J('JNE', 'rd_x')        # play mode on a screen Rosetta replaced (cuts.SCREENS):
    a.ldb(T, R56_)                                       # back to the Tone Display, the menu opens instead
    for n, (lo, hi, pg) in enumerate(REDIRECT):
        a.cmpbi(T, lo); J('JNC', 'rd%d' % n); a.cmpbi(T, hi + 1); J('JC', 'rd%d' % n)
        a.ldbi(V, PIDX[pg]); a.sjmp('rd_go')
        a.L('rd%d' % n)
    a.sjmp('rd_x')
    a.L('rd_go'); sta(RD_PG, V); a.ldbi(T, 1); a.ldb(R56_, T)
    stock(SCR_OFF, P2); stock(SCR_ENTER, P2)            # stock's own screen change: LEDs off, enter screen 01
    lda(V, RD_PG); a.ljmp('bk4')
    a.L('rd_x')
    lda(T, CARDQ); a.cmpbi(T, 0); J('JE', 'bkc')       # PCM CARD (taken from stock's key read): the wave page
    a.clrb(T); sta(CARDQ, T); a.ldbi(V, PIDX['wave']); a.ljmp('bk4')
    a.L('bkc'); a.clrb(T)                                # T = USER 1 | EFFECT/CTRL 2 | MIDI OUT 4, held now
    for n, (adr, bit) in enumerate((KEYMAP_USER, KEYMAP_FX, KEYMAP_MIDI)):
        lda(U, adr); a.andbi(U, bit); J('JE', 'bk%d' % n); a.orbi(T, 1 << n); a.L('bk%d' % n)
    lda(U, UPREV); sta(UPREV, T); a.b(0x12, U); a.andb(T, U); J('JE', 'rt_stock')   # NOTB U: went down now
    a.ldbi(V, PIDX['midi']); a.jbs(T, 2, 'bk4'); a.ldbi(V, PIDX['fx']); a.jbs(T, 1, 'bk4'); a.clrb(V)
    a.L('bk4'); a.ldbi(T, 0xFF); sta(LZ_PG, T)
    a.ldbi(T, 1); sta(M_ON, T); a.clrb(T)              # open: USER at the top page, the others on their own
    for v in (SEL, COL, RSEL, RTOP): sta(v, T)
    sta(SEC, T); sta(PG, V)
    a.lcall('draw'); a.ret()
    a.L('rt_stock'); stock(STOCK_UI, P2); a.ret()       # stock code expects ROM page 2 in the window
    a.L('rt_on')
    a.cmpbi(T, 2); J('JNE', 'rt_on1')                   # opened at boot: let the stock startup finish first
    stock(STOCK_UI, P2)                                  # (about 2 s = 24 TIMER1 wraps of 87 ms)
    a.ldb_abs(T, TIMER1_ + 1); lda(U, BOOTHI); sta(BOOTHI, T); a.cmpb(T, U); J('JC', 'rt_x')
    lda(T, BOOTN); a.incb(T); sta(BOOTN, T); a.cmpbi(T, 24); J('JNE', 'rt_x')
    a.ldbi(T, 1); sta(M_ON, T); a.lcall('draw'); a.ret()
    a.L('rt_on1')                                        # sliders: slider 1 snaps the selected item on value pages
    a.lcall('page_ptr'); lda(V, KIND); a.cmpbi(V, K_SPLIT); J('JE', 'sld1'); a.cmpbi(V, K_LIST); J('JE', 'sld1')
    a.cmpbi(V, K_ENV); J('JNE', 'sld2')
    a.L('sld1'); lda(T, SLD); lda(U, SLD_LAST); a.cmpb(T, U); J('JE', 'sld2')
    a.lcall('snap')                                      # taken: marked as seen + redrawn; else still CUTOFF
    a.L('sld2'); stock(SL_LOOP, P2)                      # then the play-screen slider loop, as outside the menu
    # Alpha Dial: stock's panel counter read, signed
    stock(DIAL, P4); a.cmpbi(T, 0); J('JE', 'rt_k')     # R1C = clicks since the last read (+ = clockwise)
    a.ldbi(U, K_INC); a.cmpbi(T, 0x80); J('JNC', 'dia1'); a.ldbi(U, K_DEC); a.b(0x13, T)   # NEGB T
    a.L('dia1'); a.cmpbi(T, 9); J('JNC', 'dia2'); a.ldbi(T, 8)                    # at most 8 steps a read
    a.L('dia2'); sta(STEPS, T); lda(V, KIND); a.cmpbi(V, K_ROOT); J('JNE', 'dia3')
    a.subbi(U, K_INC - K_DN)                             # lists: INC / DEC -> DOWN / UP
    a.L('dia3'); sta(KEYC, U)
    a.L('dia4'); a.lcall('key'); lda(V, KIND); a.cmpbi(V, K_ROOT); J('JNE', 'dia5')   # value pages: STEPS in kval
    lda(T, STEPS); a.decb(T); sta(STEPS, T); J('JNE', 'dia4')                        # lists: one move per click
    a.L('dia5'); a.lcall('draw')
    a.L('rt_k'); a.ldbi(T, 1); sta(STEPS, T)
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
    a.cmpbi(T, K_FXB); J('JNE', 'kb1'); a.ldbi(T, PIDX['fx']); a.sjmp('kb2')        # EFFECT/CTRL, MIDI OUT:
    a.L('kb1'); a.cmpbi(T, K_MIDB); J('JNE', 'kb1c'); a.ldbi(T, PIDX['midi']); a.sjmp('kb2')   # straight to our pages
    a.L('kb1c'); a.cmpbi(T, K_CARDB); J('JNE', 'kb3'); a.ldbi(T, 0xFF); sta(LZ_PG, T); a.ldbi(T, PIDX['wave'])
    a.L('kb2'); sta(PG, T); a.clrb(T); sta(SEL, T); sta(COL, T); a.ret()
    a.L('kb3'); lda(T, KEYC)
    a.cmpbi(T, K_F5); J('JNE', 'k1')                   # F5: next section (L1 L2 U3 U4 ALL)
    lda(T, SEC); a.incb(T); a.cmpbi(T, 5); J('JNE', 'k0'); a.clrb(T)
    a.L('k0'); sta(SEC, T); a.ret()
    a.L('k1'); a.cmpbi(T, K_L1); J('JNC', 'k2'); a.cmpbi(T, K_L1 + 4); J('JC', 'k2')
    a.subbi(T, K_L1); sta(SEC, T); a.ret()             # L1-U4 buttons pick the section
    a.L('k2'); lda(U, KIND)
    a.cmpbi(U, K_ROOT); J('JE', 'key_root')
    a.cmpbi(U, K_LAYERS); J('JE', 'key_layers')
    a.cmpbi(U, K_PEEK); J('JNE', 'k3'); a.ljmp('key_peek')
    a.L('k3')
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
    a.cmpbi(T, K_ENT); J('JNE', 'kpr')                   # ENTER on a link: open its page
    lda(T, COL); lda(U, SEL); a.lcall('item_load'); lda(T, ITYPE); a.cmpbi(T, T_LIST); J('JNE', 'kpl')
    lda(T, IAUX); a.cmpbi(T, L('dlmv')); J('JNE', 'kpl')
    a.ldbi(T, PIDX['dlm']); a.sjmp('kpg')                # the DLM row: its page
    a.L('kpl'); lda(T, ITYPE); a.cmpbi(T, T_LINK); J('JNE', 'kpr'); lda(T, IAUX)
    a.L('kpg'); sta(PG, T); a.clrb(T); sta(SEL, T); sta(COL, T)
    a.L('kpr'); a.ret()
    a.L('kval')                                          # INC / DEC the selected item
    lda(T, COL); lda(U, SEL); a.lcall('item_load')
    lda(T, ITYPE); a.cmpbi(T, T_NAME); J('JE', 'kpr'); a.cmpbi(T, T_WNAME); J('JE', 'kpr')
    a.cmpbi(T, T_LINK); J('JE', 'kpr')
    a.cmpbi(T, T_WAVE); J('JNE', 'kv_n'); a.ljmp('wave_step')
    a.L('kv_n'); lda(T, SEC); a.cmpbi(T, 4); J('JNE', 'kv0'); a.clrb(T)
    a.L('kv0'); sta(SECW, T); a.lcall('item_read')      # value from the section (L1 when ALL)
    lda(T, VAL); lda(C, STEPS)
    a.L('kv_l'); lda(U, KEYC); a.cmpbi(U, K_INC); J('JNE', 'kv_dn')
    lda(U, IMAX); a.cmpb(T, U); J('JC', 'kv_e'); a.incb(T); a.sjmp('kv_s')
    a.L('kv_dn'); lda(U, IMIN); a.cmpb(U, T); J('JC', 'kv_e'); a.decb(T)
    a.L('kv_s'); a.decb(C); J('JNE', 'kv_l')
    a.L('kv_e'); lda(U, VAL); a.cmpb(T, U); J('JE', 'kpr')                  # at the end already
    sta(VAL, T); a.lcall('item_write'); a.ret()
    a.L('snap')                                          # selected item = IMIN + slider * (IMAX - IMIN + 1) / 128
    lda(T, COL); lda(U, SEL); a.lcall('item_load')
    lda(T, ITYPE); a.cmpbi(T, T_NAME); J('JC', 'kpr')   # names and waves: buttons / dial only
    lda(T, IAREA); a.cmpbi(T, A_RAM); J('JNE', 'sn_l'); lda(T, IOFF); a.cmpbi(T, SET_OFF); J('JC', 'kpr')   # settings
    a.L('sn_l'); lda(T, SLD); sta(SLD_LAST, T)          # taken: the stock slider loop skips slider 1
    a.ldbi(C, 6); a.clr(IDX)                             # KNOB LOCK: remember the item (ITYPE..IOFF) + section
    a.L('sn_c'); a.ldb_lidx(T, IDX, ITYPE); a.stb_lidx(IDX, L_REC, T); a.inc(IDX); a.djnz(C, 'sn_c')
    lda(T, SEC); sta(L_SEC, T); a.ldbi(T, 1); sta(L_VALID, T)
    a.lcall('snap_go'); a.ljmp('draw')
    a.L('snap_go')
    lda(T, SEC); a.cmpbi(T, 4); J('JNE', 'sn0'); a.clrb(T)
    a.L('sn0'); sta(SECW, T); a.lcall('item_read')
    lda(T, IMAX); lda(U, IMIN); a.subb(T, U); a.incb(T); lda(U, SLD)
    a.b(0x5C, U, T, W); a.shr(W, 7); lda(U, IMIN); a.addb(W, U)            # MULUB W, T, U
    lda(T, IMAX); a.cmpb(W, T); J('JNH', 'sn1'); a.ldb(W, T)
    a.L('sn1'); lda(T, VAL); a.cmpb(W, T); J('JE', 'kpr')
    sta(VAL, W); a.lcall('item_write'); a.ret()

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
    a.L('kl3'); a.cmpbi(T, K_ENT); J('JNE', 'kl4'); a.ldbi(T, 0xFF); sta(LZ_PG, T)
    a.ldbi(T, PIDX['wave']); sta(PG, T); a.clrb(T)
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
    a.L('ad1'); a.cmpbi(T, A_PERF); J('JNE', 'ad1m'); a.ldi(W, 0x00AD); a.sjmp('ad_off')
    a.L('ad1m'); a.cmpbi(T, A_MOUT); J('JNE', 'ad2')
    a.ldbze(W, U); a.b(0x6D, 7, 0x00, W); a.addi(W, MOUT_BASE); a.sjmp('ad_off')   # MULU W, #0007
    a.L('ad2'); a.ldbze(W, R5F); a.b(0x6D, 0x57, 0x00, W); a.addi(W, 0x019E)   # W = patch of part
    a.cmpbi(T, A_PCOM); J('JE', 'ad_off')
    a.ldbze(IDX, U); a.b(0x6D, 0x0D, 0x00, IDX); a.add(W, IDX); a.addi(W, 0x23)
    a.L('ad_off'); lda(T, IOFF); a.ldbze(IDX, T); a.add(W, IDX); stw(ADDR, W); a.ret()

    a.L('item_read')                                     # VAL = value at the item for section SECW
    lda(T, IAREA); a.cmpbi(T, A_RAM); J('JNE', 'ir1')
    lda(T, IOFF); a.ldbze(IDX, T); a.ldb_lidx(T, IDX, RAM_BASE); sta(VAL, T); a.ret()
    a.L('ir1'); a.lcall('addr'); ldw(RD4, ADDR); a.clr(RD6); stock(RD, P5); sta(VAL, RCA); a.ret()
    a.L('write1')                                        # VAL -> item for section SECW (no apply)
    lda(T, IAREA); a.cmpbi(T, A_RAM); J('JNE', 'wr1')
    lda(T, IOFF); a.ldbze(IDX, T); lda(T, VAL); a.stb_lidx(IDX, RAM_BASE, T); a.ret()
    a.L('wr1'); a.lcall('addr'); ldw(RD4, ADDR); a.clr(RD6); lda(RCA, VAL); stock(WR, P5); a.ret()
    a.L('item_write')                                    # VAL -> current section, or all four, then apply
    a.ldbi(T, 0xFF); sta(LZ_PG, T); a.clrb(RDE)
    lda(T, SEC); a.cmpbi(T, 4); J('JE', 'iw_all'); sta(SECW, T); a.lcall('write1'); a.sjmp('iw_ap')
    a.L('iw_all'); a.clrb(T); sta(SECW, T)
    a.L('iw_l'); a.lcall('write1'); lda(T, SECW); a.incb(T); sta(SECW, T); a.cmpbi(T, 4); J('JNE', 'iw_l')
    a.L('iw_ap'); stock(APPLY, P2)
    lda(T, IAREA); a.cmpbi(T, A_PERF); J('JNE', 'iw_r'); lda(T, IOFF); a.cmpbi(T, FX_LAST + 1); J('JC', 'iw_r')
    stock(FX_RELOAD, 0x1053)                             # effects chip: reload
    a.L('iw_r'); a.ret()

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
    a.cmpbi(T, K_PEEK); J('JNE', 'd0s'); a.ldi_l(Q, 's_pkpeak'); lda(T, PK_MODE); a.cmpbi(T, 0); J('JE', 'd0p')
    a.ldi_l(Q, 's_pklive')
    a.L('d0p'); a.ldbi(T, 39); a.lcall('rput'); a.sjmp('d0e')
    a.L('d0s'); lda(T, SEC); a.cmpbi(T, 4); J('JNE', 'd0c')
    a.ldi_l(Q, 's_all'); a.ldbi(T, 39); a.lcall('rput'); a.sjmp('d0e')
    a.L('d0c'); a.ldi_l(Q, 's_chips'); a.ldbi(T, 28); a.lcall('put'); a.sjmp('d0e')
    a.L('d0r'); a.ldi_l(Q, 's_ver'); a.ldbi(T, 39); a.lcall('rput')
    a.L('d0e'); a.lcall('tpush')
    lda(T, KIND)
    a.cmpbi(T, K_ROOT); J('JNE', 'dk1'); a.lcall('draw_root'); a.sjmp('dgfx')
    a.L('dk1'); a.cmpbi(T, K_SPLIT); J('JNE', 'dk2'); a.lcall('draw_split'); a.sjmp('dgfx')
    a.L('dk2'); a.cmpbi(T, K_ENV); J('JNE', 'dk3'); a.lcall('draw_env_text'); a.sjmp('dgfx')
    a.L('dk3'); a.cmpbi(T, K_LIST); J('JNE', 'dk4'); a.lcall('draw_list'); a.sjmp('dgfx')
    a.L('dk4'); a.cmpbi(T, K_PEEK); J('JNE', 'dk5'); a.lcall('draw_peek'); a.sjmp('dgfx')
    a.L('dk5'); a.lcall('draw_layers')
    a.L('dgfx'); a.lcall('gfx')
    lda(T, PG); a.cmpbi(T, PIDX['dlm']); J('JNE', 'dg1'); a.ldi_l(R1A, 'wave_view'); a.lcall(P4); a.ret()
    a.L('dg1'); lda(T, KIND); a.cmpbi(T, K_ENV); J('JNE', 'dret'); a.lcall('env_lines')
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
    a.cmpbi(T, T_LINK); J('JNE', 'ivk'); a.ldi_l(Q, 's_gt'); a.ret()
    a.L('ivk'); a.cmpbi(T, T_WAVE); J('JNE', 'iv1'); a.ljmp('iv_wave')
    a.L('iv1'); a.cmpbi(T, T_WNAME); J('JNE', 'iv2'); a.ljmp('iv_wname')
    a.L('iv2'); a.lcall('item_read'); a.lcall('fmt'); a.ret()
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

    a.L('draw_list')                                     # row 1: the four zones (gfx), rows 2-7: one column
    lda(T, PG); lda(U, LZ_PG); a.cmpb(T, U); J('JE', 'dli0'); sta(LZ_PG, T)   # zones: once per visit
    a.clrb(T); sta(I, T)                                 # ZL / ZH = zone LOW / HIGH in pixels 0..238, per section
    a.L('dli1'); lda(T, I); sta(SECW, T); a.ldbi(T, A_PSEC); sta(IAREA, T)
    for off_, arr in ((0x0B, ZL), (0x0C, ZH)):
        a.ldbi(T, off_); sta(IOFF, T); a.lcall('item_read'); lda(T, VAL); a.ldbi(U, 15)
        a.b(0x5C, U, T, W); a.shr(W, 3); lda(U, I); a.ldbze(IDX, U); a.stb_lidx(IDX, arr, W)   # MULUB W, T, U
    lda(T, I); a.incb(T); sta(I, T); a.cmpbi(T, 4); J('JNE', 'dli1')
    a.L('dli0'); a.ldbi(T, 1); sta(ROW, T); a.lcall('tclr'); a.lcall('tpush')
    lda(T, SEL); lda(U, STOP); a.cmpb(T, U); J('JC', 'dli2'); sta(STOP, T)   # scroll: SEL stays in rows 2-7
    a.L('dli2'); lda(U, STOP); a.addbi(U, 5); a.cmpb(U, T); J('JC', 'dli3'); a.subbi(T, 5); sta(STOP, T)
    a.L('dli3'); a.ldbi(T, 2); sta(ROW, T)
    a.L('dli4'); a.lcall('tclr'); lda(T, ROW); a.subbi(T, 2); a.ldbze(IDX, T); a.stb_lidx(IDX, RB_T, 0)
    lda(U, STOP); a.addb(T, U); lda(U, NITEM); a.cmpb(T, U); J('JC', 'dli5')
    a.ldb(U, T); a.clrb(T); a.lcall('item_load')
    a.ld_lidx(Q, 0, ITEMP); a.ld_idx(Q, Q, 0); a.ldbi(T, 1); a.lcall('put')
    a.lcall('item_value'); a.ldbi(T, 38); a.lcall('rput')
    a.clrb(V); lda(T, IAREA); a.cmpbi(T, A_PSEC); J('JNE', 'dlb')   # LEVEL / PAN: also drawn as a bar
    lda(T, IOFF); a.cmpbi(T, 0x01); J('JNE', 'dla'); a.ldbi(V, 1); a.sjmp('dlb')
    a.L('dla'); a.cmpbi(T, 0x09); J('JNE', 'dlb'); a.ldbi(V, 2)
    a.L('dlb'); lda(T, ROW); a.subbi(T, 2); a.ldbze(IDX, T); a.stb_lidx(IDX, RB_T, V)
    lda(U, VAL); a.stb_lidx(IDX, RB_V, U)
    a.L('dli5'); a.lcall('tpush'); lda(T, ROW); a.incb(T); sta(ROW, T); a.cmpbi(T, 8); J('JNE', 'dli4')
    lda(T, SEL); lda(U, STOP); a.subb(T, U); a.addbi(T, 2); sta(SELROW, T)
    a.clrb(T); sta(SC0, T); a.ldbi(T, 39); sta(SC1, T); a.ret()

    a.L('draw_layers')
    a.ldbi(T, 1); sta(ROW, T); a.lcall('tclr')
    a.ldi_l(Q, 's_lhead'); a.ldbi(T, 1); a.lcall('put'); a.ldi_l(Q, 's_lvl'); a.ldbi(T, 39); a.lcall('rput')
    a.lcall('tpush')
    a.clrb(T); sta(I, T)
    a.L('dl1'); a.lcall('tclr')
    lda(T, I); a.ldbze(IDX, T); a.add(IDX, IDX); a.ld_lidx_l(Q, IDX, 'sec_names'); a.ldbi(T, 1); a.lcall('put')
    lda(T, I); sta(SECW, T); a.ldbi(T, A_TONE); sta(IAREA, T); a.ldbi(T, T_NAME); sta(ITYPE, T)
    a.lcall('iv_wname'); a.ldbi(T, 4); a.lcall('put')     # the layer's wave name
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
    a.L('g_chip'); lda(C, KIND); a.cmpbi(C, K_ROOT); J('JE', 'g_out'); a.cmpbi(C, K_PEEK); J('JE', 'g_out')
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
    a.L('g_b3'); a.cmpbi(C, K_LIST); J('JNE', 'g_b4'); a.cmpbi(U, 1); J('JNE', 'g_b3b')
    a.lcall('g_strip'); a.sjmp('g_sel')
    a.L('g_b3b'); J('JNC', 'g_sel'); a.lcall('g_bar'); a.sjmp('g_sel')
    a.L('g_b4'); a.cmpbi(C, K_LAYERS); J('JNE', 'g_sel')
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
    a.L('g_strip')                                       # text row 1: zone of section y / 2 as a 2 px line,
    a.ldb(Q, T); a.shrb(Q, 1); a.ldbze(IDX, Q)           # solid for the section shown, dotted for the others
    a.ldb_lidx(Z, IDX, ZL); a.ldb_lidx(Z + 1, IDX, ZH)
    lda(P, SEC); a.cmpbi(P, 4); J('JE', 'gs0'); a.cmpb(P, Q); J('JE', 'gs0')
    a.ldbi(Q, 1); a.sjmp('gs1')
    a.L('gs0'); a.clrb(Q)
    a.L('gs1'); a.ldb(C, V); a.ldbi(C + 1, 6); a.b(0x5C, C + 1, C, C)   # C = column * 6
    a.ldbi(P, 0x20); a.ldbi(P + 1, 6)
    a.L('gs2'); a.cmpb(C, Z); J('JNC', 'gs3'); a.cmpb(Z + 1, C); J('JNC', 'gs3')
    a.cmpbi(Q, 0); J('JE', 'gs4'); a.jbs(C, 0, 'gs3')   # dotted: even x only
    a.L('gs4'); a.orb(X, P)
    a.L('gs3'); a.incb(C); a.shrb(P, 1); a.djnz(P + 1, 'gs2')
    a.ret()
    a.L('g_bar')                                         # text rows 2-7: LEVEL bar / PAN marker, x BX0..BX1
    a.ldbze(IDX, U); a.subi(IDX, 2); a.ldb_lidx(Q, IDX, RB_T); a.cmpbi(Q, 0); J('JE', 'gb_r')
    a.ldb_lidx(Z, IDX, RB_V); a.cmpbi(Q, 1); J('JNE', 'gb0')
    a.ldb(Z + 1, Z); a.shrb(Z + 1, 1); a.addbi(Z + 1, BX0); a.sjmp('gb00')     # level: fill up to BX0 + v / 2
    a.L('gb0'); a.ldb(Z + 1, Z); a.addb(Z + 1, Z + 1); a.addb(Z + 1, Z + 1); a.addbi(Z + 1, BX0 + 4)   # pan
    a.L('gb00'); a.ldb(C, V); a.ldbi(C + 1, 6); a.b(0x5C, C + 1, C, C)   # C = x of the byte's first pixel
    a.ldbi(P, 0x20); a.ldbi(P + 1, 6)
    a.L('gb1'); a.cmpbi(T, 7); J('JE', 'gb_n'); a.cmpbi(C, BX0); J('JNC', 'gb_n'); a.cmpbi(C, BX1 + 1); J('JC', 'gb_n')
    a.cmpbi(Q, 1); J('JNE', 'gb_p')
    a.cmpbi(T, 0); J('JE', 'gb_on'); a.cmpbi(T, 6); J('JE', 'gb_on')          # level: outline + fill
    a.cmpbi(C, BX0); J('JE', 'gb_on'); a.cmpbi(C, BX1); J('JE', 'gb_on')
    a.cmpb(Z + 1, C); J('JC', 'gb_on'); a.sjmp('gb_n')
    a.L('gb_p'); a.cmpbi(T, 3); J('JE', 'gb_on')       # pan: the track, the centre tick, the marker
    a.cmpbi(C, (BX0 + BX1) // 2); J('JNE', 'gb_m'); a.cmpbi(T, 1); J('JNC', 'gb_m'); a.cmpbi(T, 6); J('JNC', 'gb_on')
    a.L('gb_m'); a.ldb(IDX, Z + 1); a.decb(IDX); a.cmpb(C, IDX); J('JNC', 'gb_n')
    a.addbi(IDX, 2); a.cmpb(IDX, C); J('JC', 'gb_on'); a.sjmp('gb_n')
    a.L('gb_on'); a.orb(X, P)
    a.L('gb_n'); a.incb(C); a.shrb(P, 1); a.djnz(P + 1, 'gb1')
    a.L('gb_r'); a.ret()
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
    ldw(Q, LY0); a.b(0x6D, 40, 0, Q); a.ldbze(P, W); a.add(P, Q); a.addi(P, LCD_GFX)   # long result: R24 (4-aligned)
    a.ldbi(T, 5); a.subb(T, W + 1); a.ldbi(U, 1)        # U = pixel mask (bit 5 = leftmost)
    a.L('pl1'); a.cmpbi(T, 0); J('JE', 'pl2'); a.addb(U, U); a.decb(T); a.sjmp('pl1')
    a.L('pl2'); a.ld(W, P); a.b(0xFA); a.lcall('setaddr')   # read, OR, write back: the panel's T6963C
    a.ldbi(X, 0xC5); a.lcall('cmd'); a.lcall('wst'); a.ldb_abs(X, LCD_D)   # ignores bit set / reset
    a.orb(X, U); a.lcall('dat'); a.ldbi(X, 0xC4); a.lcall('cmd'); a.b(0xFB)
    a.ret()

    # ================= TOOLS > DUMP IC6: 256 SysEx messages F0 7D 49 half blk <256 nibbles> F7 =================
    a.L('dump')
    a.ldbi(T, 7); sta(ROW, T); a.lcall('tclr'); a.ldi_l(Q, 's_sending'); a.ldbi(T, 1); a.lcall('put')
    a.lcall('tpush')
    a.ldi_l(R1A, 'dump_send'); a.lcall(P4); a.ret()     # the sending runs from ROM page 4 (emit_p4)

    # ================= LCD =================
    a.L('wst'); a.ldb_abs(ST, LCD_C); a.andbi(ST, 3); a.cmpbi(ST, 3); J('JNE', 'wst'); a.ret()
    a.L('wauto'); a.ldb_abs(ST, LCD_C); a.jbc(ST, 3, 'wauto'); a.ret()
    a.L('cmd'); a.lcall('wst'); a.stb_abs(LCD_C, X); a.ret()
    a.L('dat'); a.lcall('wst'); a.stb_abs(LCD_D, X); a.ret()
    a.L('setaddr'); a.ldb(X, W); a.lcall('dat'); a.ldb(X, W + 1); a.lcall('dat'); a.ldbi(X, 0x24); a.sjmp('cmd')
    emit_wave(a, J, lda, sta, ldw, stw)
    emit_wavesel(a, J, lda, sta, ldw, stw, stock)
    emit_peek(a, J, lda, sta, ldw, stw)


def emit_p4(a):
    """TOOLS > DUMP IC6, the sending part: ROM page 4 (reached through the page-4 stub; IC6_RD puts R54 = 4
    back in the window). 256 SysEx blocks F0 7D 49 hi lo <128 bytes as nibbles> F7, 0.4 ms per byte."""
    J = a.jcc
    def lda(r, addr): a.ldb_abs(r, addr)
    def sta(addr, r): a.stb_abs(addr, r)
    def ldw(r, addr): a.ld_lidx(r, 0, addr)
    def stw(addr, r): a.st_lidx(0, addr, r)
    def far(label): a.ldi_l(R1A, label); a.lcall(0x105F)
    emit_wave_view(a, J, lda, sta, ldw, stw, far)
    # stock key read, filtered: stock acts on a key in the same pass it scans it, so PCM CARD (menu closed) is
    # taken here, before any stock screen sees it; rt_ui opens the wave page on its next pass
    a.L('gk_hook'); a.lcall(GK_STOCK); J('JNC', 'gk_r')
    a.cmpbi(T, K_CARDB); J('JNE', 'gk_s')
    lda(IDX, M_ON); a.cmpbi(IDX, 0); J('JNE', 'gk_s')
    a.ldbi(IDX, 1); sta(CARDQ, IDX); a.b(0xF8); a.ret()   # CLRC: no key for stock
    a.L('gk_s'); a.b(0xF9)                               # SETC: the key, as stock returned it
    a.L('gk_r'); a.ret()
    a.L('dump_send')
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


# ================= data: page tables, items and strings, packed into the freed page-7 blocks =================
DATA_SEGS = [(0xAD4E, 0xB289), (0xBA0A, 0xBCA6), (0xBF23, 0xBFFF),   # page 7, inclusive (cuts.py)
             (END + 1, 0x9C47),                                     # + the top of the code block
             (0x18F0, 0x1991), (0x72EF, 0x73B1), (0x75B0, 0x7613)]  # freed fixed ROM: readable from any page


def s_(d, name, text): d.L(name); d.data(bytes(text, 'ascii') + bytes([0xFF]))


class _Probe:
    """stands in for an Asm while the string pool is collected"""
    def __getattr__(self, name): return lambda *a, **k: None


def chunks():
    """data in pieces that can each go to any segment (every piece starts word aligned). Every text (titles,
    heads, item labels, list words) is stored once in a shared pool: pages point at the pooled copy."""
    out, pool = [], {}

    def S(text):                                        # label of the pooled copy of text
        if text not in pool: pool[text] = 'sx_%d' % len(pool)
        return pool[text]

    def pagetab(d):
        d.L('pages')
        for key, title, kind, h0, h1, i0, i1 in PAGES:
            n0 = len(LIST_PAGES[key]) if kind == K_ROOT else len(i0)
            par = 0xFF if PARENT[key] is None else PIDX[PARENT[key]]
            d.dw(S(title)); d.data([kind, n0, len(i1), par]); d.dw(S(h0)); d.dw(S(h1))
            d.dw('i0_' + key); d.dw('i1_' + key); d.data([0, 0])
    out.append(pagetab)
    for key, title, kind, h0, h1, i0, i1 in PAGES:
        def page(d, key=key, kind=kind, i0=i0, i1=i1):
            if kind == K_ROOT:
                d.L('i0_' + key); d.L('i1_' + key)
                for n, (lab, k_, arg) in enumerate(LIST_PAGES[key]):
                    d.dw(S(lab))
                    d.data([k_, PIDX[arg] if k_ == LI_LINK else (arg if k_ == LI_ACT else 0)])
                    d.dw(S(arg) if k_ == LI_INFO else 0); d.dw(0)
            else:
                for col, items in ((0, i0), (1, i1)):
                    d.align(); d.L('i%d_%s' % (col, key))
                    for n, (lab, area, off, typ, mn, mx, aux) in enumerate(items):
                        if isinstance(aux, str): aux = PIDX[aux]          # T_LINK: target page
                        d.dw(S(lab)); d.data([area, off, typ, mn, mx, aux])
        out.append(page)

    def listtab(d):
        d.L('listtab')
        for k in LISTS: d.dw('lt_' + k)
    out.append(listtab)
    for k, names in LISTS.items():
        def lst(d, names=names, k=k):
            d.L('lt_' + k)
            for nm in names: d.dw(S(nm))
        out.append(lst)

    def misc(d):
        d.L('sec_names')
        for n in ('L1', 'L2', 'U3', 'U4'): d.dw(S(n))
        d.L('notes'); d.data(b'C C#D D#E F F#G G#A A#B ')
    out.append(misc)
    for fn in list(out): fn(_Probe())                    # fill the pool (labels only, no output)
    texts = list(pool.items())
    for g in range(0, len(texts), 8):
        out.append(lambda d, grp=texts[g:g + 8]: [s_(d, lb, t) for t, lb in grp])
    for name, text in STRINGS:
        out.append(lambda d, name=name, text=text: s_(d, name, text))
    return out


STRINGS = [('s_all', 'ALL LAYERS'), ('s_chips', 'L1 L2 U3 U4'), ('s_ver', 'V0.8'),
           ('s_sending', 'SENDING IC6 ... ABOUT 30 S'), ('s_gt', '>'), ('s_lhead', 'SEC WAVE      ZONE'),
           ('s_lvl', 'LVL'), ('s_off', 'OFF'), ('s_mode', 'MODE'), ('s_split', 'SPLIT'),
           ('s_pkpeak', 'PEAK BITS'), ('s_pklive', 'LIVE HEX')]


def build_data():
    """-> ([(cpu, bytes)], labels): chunks placed first-fit, in order, into DATA_SEGS"""
    from collections import defaultdict
    from asm import Asm
    fns = chunks()
    cur = [a for a, b in DATA_SEGS]                     # first fit: each piece into the first block it fits
    place = []
    for fn in fns:
        for k, (lo, hi) in enumerate(DATA_SEGS):
            d = Asm(cur[k]); d.ext = defaultdict(int); d.align(); fn(d); n = len(d.assemble())
            if cur[k] + n - 1 <= hi: break
        else:
            raise AssertionError('SYNTH data does not fit')
        place.append((cur[k], fn)); cur[k] += n
    labels = {}
    for org, fn in place:
        d = Asm(org); d.ext = defaultdict(int); d.align(); fn(d); d.assemble(); labels.update(d.labels)
    out = []
    for org, fn in place:
        d = Asm(org); d.ext = labels; d.align(); fn(d); out.append((org, d.assemble()))
    return out, labels


# ================= DLM page: the bytes DLM loops, read from wave ROM and drawn =================
# Stock voice setup (page 3 0xBA81-0xBB82): the chip addresses wave ROM in 4-byte steps.
#   MODE B: loop = sample start (& ~3) + START * 4, (LENGTH + 1) * 4 bytes, inside the tone's own sample
#   MODE A: loop = (0x1000 + wave * 0x50 + START) * 4, same length: a fixed grid in wave memory
# Samples are 8-bit DPCM (one signed step per byte, see u110_card/FORMAT.md); the view adds the steps up
# and keeps going round the loop, so the DLM drift shows.
WSEL = 0x3F83                  # fixed: R1C media, R1D wave -> R1C ROM select, R1D entry (C set = no such wave)
WDIR = 0x3FB1                  # fixed: R1C select, R1D entry -> 0x50-byte wave entry at RAM 0xEB77
WSMP = 0x3FDA                  # fixed: R1C select, R1D sample -> 10-byte sample entry at [R2A]
WBYTE, P3 = 0xB8FA, 0x1053     # page 3: byte at R22:R20 (R23 select) -> R1C, address + 1
WENT = 0xEB77
V_ENT = 0xC640                 # sample entry (10)
V_ADR, V_HI, V_BASE, V_BHI = 0xC64A, 0xC64C, 0xC64E, 0xC650     # read address / base (word + hi word)
V_ACC, V_N, V_LEFT, V_K, V_COL, V_P = 0xC652, 0xC654, 0xC656, 0xC658, 0xC659, 0xC65A
WX0, WY0, WH = 120, 18, 44     # view: x 120..238, y 18..61 (row 1 = 8..15 holds the LOOP SLICE head + rule)


def emit_wave(a, J, lda, sta, ldw, stw):
    a.L('tread'); sta(IOFF, T); a.ldbi(T, A_TONE); sta(IAREA, T); a.lcall('item_read'); lda(T, VAL); a.ret()


def emit_wave_view(a, J, lda, sta, ldw, stw, far):
    """the DLM loop view, ROM page 4 (far = call into page 7 through the 0x105F stub)"""
    a.L('wave_view')
    lda(T, SEC); a.cmpbi(T, 4); J('JNE', 'wv0'); a.clrb(T)
    a.L('wv0'); sta(SECW, T)
    for off, var in ((0x0D, V_P), (0x0E, V_P + 1), (0x0F, V_P + 2), (0x0A, V_P + 3), (0x0B, V_P + 4)):
        a.ldbi(T, off); far('tread'); sta(var, T)
    lda(T, V_P + 3); lda(U, V_P + 4); a.lcall(WSEL); J('JNC', 'wv1'); a.ret()       # no such wave
    a.L('wv1'); sta(V_HI + 1, T)                          # select -> high byte of the address word
    lda(V, V_P); a.cmpbi(V, 0); J('JNE', 'wv_b')
    a.ldbze(W, U); a.b(0x6D, 0x50, 0, W); a.addi(W, 0x1000)   # MODE A: (0x1000 + wave * 0x50 + START) * 4
    lda(V, V_P + 1); a.ldbze(Q, V); a.add(W, Q); a.ld(P, W); a.shr(P, 14); a.shl(W, 2); a.sjmp('wv_set')
    a.L('wv_b'); a.lcall(WDIR)                            # MODE B: the sample under middle C
    a.clr(IDX); a.clrb(V)
    a.L('wv2'); a.ldb_lidx(Z, IDX, WENT + 0x10); a.cmpbi(Z, 61); J('JC', 'wv3')    # split <= 60: next zone
    a.incb(V); a.incb(IDX); a.cmpbi(IDX, 11); J('JNE', 'wv2')
    a.L('wv3'); a.ldbze(IDX, V); a.ldb_lidx(U, IDX, WENT + 0x1B); a.cmpbi(U, 0xFF); J('JNE', 'wv4')
    lda(U, WENT + 0x1B)
    a.L('wv4'); lda(T, V_HI + 1); a.ldi(0x2A, V_ENT); a.lcall(WSMP)
    ldw(W, V_ENT); a.andbi(W, 0xFC); lda(V, V_ENT + 2); a.andbi(V, 7); a.ldbze(P, V)
    lda(V, V_P + 1); a.ldbze(Q, V); a.shl(Q, 2); a.add(W, Q); J('JNC', 'wv_set'); a.inc(P)
    a.L('wv_set'); stw(V_BASE, W); stw(V_ADR, W); sta(V_BHI, P); sta(V_HI, P)
    lda(T, V_HI + 1); sta(V_BHI + 1, T)
    lda(V, V_P + 2); a.ldbze(W, V); a.inc(W); a.shl(W, 2); stw(V_N, W); stw(V_LEFT, W)   # (LENGTH + 1) * 4
    a.addi(W, 118); a.b(0x9D, 119, W); a.cmpbi(W, 0); J('JNE', 'wv5'); a.incb(W)         # k = bytes per column
    a.L('wv5'); sta(V_K, W)
    a.clr(W); stw(V_ACC, W); a.clrb(T); sta(V_COL, T)
    a.ldi(W, WX0 - 3); stw(LX1, W); a.ldi(W, WY0 + WH // 2); stw(LY1, W)
    a.L('wv_col'); lda(C, V_K)
    a.L('wv_byte')
    ldw(W, V_ADR); ldw(P, V_HI); a.ldi(R1A, WBYTE); a.lcall(P3); stw(V_ADR, W); sta(V_HI, P)
    ldw(W, V_LEFT); a.dec(W); stw(V_LEFT, W); J('JNE', 'wv6')                # end of the loop: back to start
    ldw(W, V_BASE); stw(V_ADR, W); lda(P, V_BHI); sta(V_HI, P); ldw(W, V_N); stw(V_LEFT, W)
    a.L('wv6'); a.ldbze(W, T); a.clrb(U); a.cmpbi(T, 0x80); J('JNC', 'wv7')   # DPCM step: |b| -> shift, mantissa
    a.ldi(P, 0x100); a.sub(P, W); a.ld(W, P); a.ldbi(U, 1)
    a.L('wv7'); a.ld(P, W); a.shr(P, 4); a.andbi(W, 15); a.cmpi(P, 0); J('JE', 'wv9')
    a.addi(W, 16)
    a.L('wv8'); a.dec(P); J('JE', 'wv9'); a.add(W, W); a.sjmp('wv8')
    a.L('wv9'); ldw(Q, V_ACC); a.cmpbi(U, 0); J('JE', 'wv10'); a.sub(Q, W); a.sjmp('wv11')
    a.L('wv10'); a.add(Q, W)
    a.L('wv11'); a.cmpi(Q, 2047); J('JLE', 'wv12'); a.ldi(Q, 2047)          # 12-bit, like the chip
    a.L('wv12'); a.cmpi(Q, -2047 & 0xFFFF); J('JGE', 'wv13'); a.ldi(Q, -2047 & 0xFFFF)
    a.L('wv13'); stw(V_ACC, Q); a.decb(C); J('JNE', 'wv_byte')
    a.ldi(W, 2047); a.sub(W, Q); a.shr(W, 6); a.ldbi(U, WH); a.b(0x7C, U, W); a.shr(W, 6)   # y = WY0 + (2047-acc)/64*WH/64
    a.addi(W, WY0); ldw(P, LX1); stw(LX0, P); a.inc(P); stw(LX1, P)
    ldw(P, LY1); stw(LY0, P); stw(LY1, W); far('seg')
    lda(T, V_COL); a.incb(T); sta(V_COL, T); a.cmpbi(T, 119); J('JNE', 'wv_col')
    a.ret()


# ================= WAVE page: one item sweeps every wave that exists =================
# MEDIA >= 0x40 = internal (0x40 / 0x41 / 0x42 = I1 / I2 / I3, 47 / 44 / 28 waves: page 6 tables 0xBCE3 / 0xBD41 /
# 0xBD99), below 0x40 = a card's ID, valid only while that card is in (RE1 / RE2, set by the card check 0x41BE).
# After MEDIA + NUMBER are written, 0x3E9C rebuilds the tone record (wave name at +0x37, zones, samples) the way
# stock does, then the usual apply. A changed effect is sent by stock's effects reload (page 3 0x8003 = 0xB165).
TONE_FIX, FX_RELOAD = 0x3E9C, 0x8003   # R1D = part * 4 + section ; page 3 entry, via 0x1053
TONE_RAM = 0xD79F              # temporary tone records, 0x60 each (SysEx table entry 5)
W_PTR, W_M, W_N, W_S = 0xC660, 0xC662, 0xC663, 0xC664
MEDIA_N = {0x40: 47, 0x41: 44, 0x42: 28}
RE1, RE2 = 0xE1, 0xE2


def emit_wavesel(a, J, lda, sta, ldw, stw, stock):
    a.L('wave_step')
    lda(T, SEC); a.cmpbi(T, 4); J('JNE', 'ws0'); a.clrb(T)
    a.L('ws0'); sta(SECW, T)
    a.ldbi(T, 0x0A); a.lcall('tread'); sta(W_M, T); a.ldbi(T, 0x0B); a.lcall('tread'); sta(W_N, T)
    lda(U, KEYC); a.cmpbi(U, K_INC); J('JNE', 'ws_dn')
    a.incb(T); sta(W_N, T); a.lcall('wcnt'); lda(U, W_N); a.cmpb(U, T); J('JNC', 'ws_w')
    a.lcall('wnext'); a.clrb(T); sta(W_N, T); a.sjmp('ws_w')
    a.L('ws_dn'); a.cmpbi(T, 0); J('JE', 'ws2'); a.decb(T); sta(W_N, T); a.sjmp('ws_w')
    a.L('ws2'); a.lcall('wprev'); a.lcall('wcnt'); a.decb(T); sta(W_N, T)
    a.L('ws_w'); a.clrb(RDE); lda(T, SEC); a.cmpbi(T, 4); J('JNE', 'ws_1')
    a.clrb(T)
    a.L('ws_l'); sta(W_S, T); a.lcall('ws_one'); lda(T, W_S); a.incb(T); a.cmpbi(T, 4); J('JNE', 'ws_l')
    a.sjmp('ws_ap')
    a.L('ws_1'); sta(W_S, T); a.lcall('ws_one')
    a.L('ws_ap'); stock(APPLY, P2); a.ret()
    a.L('ws_one'); lda(T, W_S); sta(SECW, T)
    a.ldbi(T, 0x0A); sta(IOFF, T); lda(T, W_M); sta(VAL, T); a.lcall('write1')
    a.ldbi(T, 0x0B); sta(IOFF, T); lda(T, W_N); sta(VAL, T); a.lcall('write1')
    a.ldb(U, R5F); a.addb(U, U); a.addb(U, U); lda(T, W_S); a.addb(U, T)     # R1D = part * 4 + section
    stock(TONE_FIX, P2); a.ret()
    a.L('wcnt'); lda(U, W_M); a.ldbi(T, 128)            # T = waves in media W_M (cards: 128)
    for m, n in MEDIA_N.items():
        a.cmpbi(U, m); J('JNE', 'wc%x' % m); a.ldbi(T, n); a.L('wc%x' % m)
    a.ret()
    a.L('wnext'); lda(U, W_M); a.ldbi(T, 0x41); a.cmpbi(U, 0x40); J('JE', 'wn_x')
    a.ldbi(T, 0x42); a.cmpbi(U, 0x41); J('JE', 'wn_x')
    a.cmpbi(U, 0x42); J('JNE', 'wn1'); a.ldb(T, RE1); a.cmpbi(T, 0xFF); J('JNE', 'wn_x'); a.sjmp('wn2')
    a.L('wn1'); a.cmpb(U, RE1); J('JNE', 'wn3')
    a.L('wn2'); a.ldb(T, RE2); a.cmpbi(T, 0xFF); J('JNE', 'wn_x')
    a.L('wn3'); a.ldbi(T, 0x40)
    a.L('wn_x'); sta(W_M, T); a.ret()
    a.L('wprev'); lda(U, W_M); a.ldbi(T, 0x40); a.cmpbi(U, 0x41); J('JE', 'wn_x')
    a.ldbi(T, 0x41); a.cmpbi(U, 0x42); J('JE', 'wn_x')
    a.cmpbi(U, 0x40); J('JNE', 'wp1'); a.ldb(T, RE2); a.cmpbi(T, 0xFF); J('JNE', 'wn_x'); a.sjmp('wp2')
    a.L('wp1'); a.cmpb(U, RE2); J('JNE', 'wp3')
    a.L('wp2'); a.ldb(T, RE1); a.cmpbi(T, 0xFF); J('JNE', 'wn_x')
    a.L('wp3'); a.ldbi(T, 0x42); a.sjmp('wn_x')
    a.L('iv_wave')                                       # "I1-12" / "C22-3"
    a.ldbi(T, 0x0A); a.lcall('tread'); a.ldi(P, NB); a.ldbi(U, ord('I'))
    a.cmpbi(T, 0x40); J('JNC', 'iw1'); a.subbi(T, 0x3F); a.sjmp('iw2')
    a.L('iw1'); a.ldbi(U, ord('C'))
    a.L('iw2'); a.stb_pi(P, U); a.lcall('udec'); a.ldbi(U, ord('-')); a.stb_pi(P, U); stw(W_PTR, P)
    a.ldbi(T, 0x0B); a.lcall('tread'); a.incb(T); ldw(P, W_PTR); a.lcall('udec')
    a.ldbi(U, 0x20); a.stb_pi(P, U); stw(W_PTR, P); a.sjmp('iwn0')    # + a space and the wave's name
    a.L('iv_wname'); a.ldi(P, NB); stw(W_PTR, P)        # the wave's name, from the tone record (+0x37)
    a.L('iwn0'); a.ldbze(W, R5F); a.shl(W, 2); lda(T, SECW); a.ldbze(IDX, T); a.add(W, IDX)
    a.b(0x6D, 0x60, 0x00, W); a.addi(W, TONE_RAM + 0x37); ldw(P, W_PTR); a.ldbi(C, 10)   # MULU hits P: reload
    a.L('iwn'); a.ldb_pi(T, W); a.stb_pi(P, T); a.decb(C); J('JNE', 'iwn'); a.ljmp('fend')


# ================= DSP PEEK page (TOOLS): the effects chip's RAM A cells, live =================
# Rows 1-7: "NN " + 5 cells of 7 chars. PEAK: highest bit used since ENTER (0-23, |x| of the 24-bit cell) + a bar,
# LIVE: the cell now, 24-bit hex. INC / DEC = PEAK / LIVE, ENTER = clear the peaks, EXIT = TOOLS.
# Hold a note and watch which cells move: that is where the D-70's sound enters the effects chip.
PEEK_ORG, PEEK_END = 0x72EF, 0x73B1   # fixed ROM freed by the bulk dump cut: the PEEK helpers (page 7 is full)


def emit_peek_fixed(a, J, lda, sta):
    """runs from fixed ROM, called from page-7 code (page 7 stays in the window)"""
    a.L('rcc_rd')                                        # Z = cell -> V hi, W+1 mid, W lo (interrupts off)
    a.pushf(); a.stb_abs(RCC_RD, Z)
    a.ldb_abs(V, RCC_D0); a.ldb_abs(W + 1, RCC_D0 + 1); a.ldb_abs(W, RCC_D0 + 2); a.popf(); a.ret()
    a.L('pk_bits')                                       # V:W+1:W -> T = bits of |x| (0..23)
    a.jbc(V, 7, 'pb_p'); a.notb(V); a.notb(W + 1); a.notb(W)
    a.L('pb_p'); a.ldbi(T, 16); a.ldb(U, V); a.cmpbi(U, 0); J('JNE', 'pb_l')
    a.ldbi(T, 8); a.ldb(U, W + 1); a.cmpbi(U, 0); J('JNE', 'pb_l')
    a.clrb(T); a.ldb(U, W); a.cmpbi(U, 0); J('JE', 'pb_x')
    a.L('pb_l'); a.incb(T); a.shrb(U, 1); a.cmpbi(U, 0); J('JNE', 'pb_l')
    a.L('pb_x'); a.ret()
    a.L('pk_clear'); a.clr(IDX); a.clrb(U)
    a.L('pc_l'); a.stb_lidx(IDX, PK_PEAK, U); a.inc(IDX); a.cmpi(IDX, 32); J('JNE', 'pc_l')
    a.ldbi(U, 0xA5); sta(PK_INIT, U); a.ret()
    a.L('dec2')                                          # T = 0..99 -> 2 digits at [P]+
    a.clrb(X)
    a.L('d2a'); a.cmpbi(T, 10); J('JNC', 'd2b'); a.subbi(T, 10); a.incb(X); a.sjmp('d2a')
    a.L('d2b'); a.addbi(X, 0x30); a.stb_pi(P, X); a.addbi(T, 0x30); a.stb_pi(P, T); a.ret()
    a.L('hexb')                                          # T -> 2 hex digits at [P]+
    a.ldb(U, T); a.shrb(U, 4); a.lcall('hx1'); a.ldb(U, T); a.andbi(U, 0x0F)
    a.L('hx1'); a.cmpbi(U, 10); J('JNC', 'hx2'); a.addbi(U, 7)
    a.L('hx2'); a.addbi(U, 0x30); a.stb_pi(P, U); a.ret()



def emit_peek(a, J, lda, sta, ldw, stw):
    a.L('key_peek')                                      # T = key
    a.cmpbi(T, K_EXIT); J('JNE', 'kk1'); a.ljmp('to_parent')
    a.L('kk1'); a.cmpbi(T, K_ENT); J('JNE', 'kk2'); a.ljmp('pk_clear')
    a.L('kk2'); a.cmpbi(T, K_INC); J('JE', 'kk3'); a.cmpbi(T, K_DEC); J('JNE', 'kk4')
    a.L('kk3'); lda(T, PK_MODE); a.b(0x95, 0x01, T); sta(PK_MODE, T)   # XORB T, #1
    a.L('kk4'); a.ret()

    a.L('pk_sample')                                     # update the peaks: every cell PK_ROUNDS times
    lda(T, PK_INIT); a.cmpbi(T, 0xA5); J('JE', 'dp0'); a.clrb(T); sta(PK_MODE, T); a.lcall('pk_clear')
    a.L('dp0'); a.ldbi(C + 1, PK_ROUNDS)                # peaks: every cell PK_ROUNDS times a pass
    a.L('dp_r'); a.clrb(Z)
    a.L('dp_c'); a.lcall('rcc_rd'); a.lcall('pk_bits'); a.ldbze(IDX, Z); a.ldb_lidx(U, IDX, PK_PEAK)
    a.cmpb(U, T); J('JC', 'dp_n'); a.stb_lidx(IDX, PK_PEAK, T)
    a.L('dp_n'); a.incb(Z); a.cmpbi(Z, 32); J('JNE', 'dp_c'); a.djnz(C + 1, 'dp_r')
    a.ret()

    a.L('draw_peek')
    a.ldbi(T, 0xFF); sta(SELROW, T); a.lcall('pk_sample')
    a.ldbi(T, 1); sta(ROW, T); a.clrb(Z)
    a.L('dp_row'); a.lcall('tclr'); a.ldi(P, TXT); a.ldb(T, Z); a.lcall('dec2'); a.ldbi(U, 0x20); a.stb_pi(P, U)
    a.ldbi(C, 5)
    a.L('dp_s'); a.cmpbi(Z, 32); J('JE', 'dp_p')
    lda(T, PK_MODE); a.cmpbi(T, 0); J('JNE', 'dp_lv')
    a.ldbze(IDX, Z); a.ldb_lidx(T, IDX, PK_PEAK); a.ldb(V, T); a.lcall('dec2')
    a.ldb(T, V); a.shrb(T, 2); a.cmpbi(T, 5); J('JNC', 'dp_b'); a.ldbi(T, 4)
    a.L('dp_b'); a.ldbi(X, 5)
    a.L('dp_bl'); a.ldbi(U, 0x20); a.cmpbi(T, 0); J('JE', 'dp_bs'); a.ldbi(U, ord('#')); a.decb(T)
    a.L('dp_bs'); a.stb_pi(P, U); a.djnz(X, 'dp_bl'); a.sjmp('dp_nx')
    a.L('dp_lv'); a.lcall('rcc_rd'); a.ldb(T, V); a.lcall('hexb'); a.ldb(T, W + 1); a.lcall('hexb')
    a.ldb(T, W); a.lcall('hexb'); a.ldbi(U, 0x20); a.stb_pi(P, U)
    a.L('dp_nx'); a.incb(Z); a.djnz(C, 'dp_s')
    a.L('dp_p'); a.lcall('tpush'); lda(T, ROW); a.incb(T); sta(ROW, T); a.cmpbi(T, 8); J('JNE', 'dp_row')
    a.ret()
