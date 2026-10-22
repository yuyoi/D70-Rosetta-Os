"""Stock features removed from v1.11 to free ROM.

ROM PLAY (demo song "Schizoid", Eric Persing):
  entry 0x4A9E (fixed ROM, reached from the mode tables at 0x3590 / 0x44EC): LDB R56,#47 (ROM Play screen) then the
  page-7 start routine. Patched to RET: the ROM Play key does nothing, the screen never becomes 0x47.
  Data freed, page 7 CPU 0x83A9-0x9C47 (file 0x1C3A9-0x1DC47, 6303 bytes):
    0x83A9 setup (108 B -> D5B4), 0x8415 5 parts x 56 B, 0x852D-0x8832 20 tone records x 86 B (pointer table 0xBAE0),
    0x8833-0x8B21 the demo performance (copied into IC6 0x8160 after backing the user's one up to RAM 0xC400),
    0x8B22-0x9C47 the song events.
  Code freed: fixed 0x4A9F-0x4B1B (ROM Play keys), page 7 0xBA0A-0xBCA6 (the player, jump entries 0x8000-0x800C).
  The main-loop tick at 0x1470 (only on screen 0x47) stays as it is: the loop paces itself on the timer interrupt
  (R94 vs D3B4) and changing its length there stalled it in MAME. It never fires (no screen 0x47) and its jump
  entry 0x800C now leads to a RET.

Hidden power-on modes. Keys held at power-on are read from D37A (bit 7 = [1] ... bit 0 = [8]), tested at 0x11E5:
  [1] version screen (page 7 0xB13B), [2] credits (page 7 0xAD4E), [5] Extend Product Mode (fixed 0x18CB-0x1946,
  bulk dump / load all memory at fixed 0x72EF-0x73B1 and 0x73D8-0x7613, screen page 7 0xAE9D), [6] Service Mode
  (fixed 0x1947-0x1991, screen page 7 0xAFEC: trim pot and A/D check). Their tests are taken out, so those keys at
  power-on just boot normally. Page 7 0xAD4E-0xB289 (the four screens) is one free block.
  Kept: [7] boot from a RAM card (0x1992), the sample-table readers page 7 0x8133 / 0x813B (0x3FEF / 0x400A);
  the factory test mode after them (0x8143-0x83A8) went with Service Mode,
  the SysEx header sender 0x73B2-0x73D7 (normal bulk dumps use it).
Page-7 jump entries that led into freed code now lead to a kept RET (0xA587), so a stray call does nothing.
"""

RET7 = 0xA587              # page 7: RET at the end of the kept routine 0xA575 (jump entry 0x8054)
NOP = 0xFD


def _lj(at, target):       # LJMP target, at CPU address at (pc-relative)
    d = (target - (at + 3)) & 0xFFFF
    return bytes([0xE7, d & 255, d >> 8])


def _p7(cpu):              # page 7 CPU address -> file offset
    return 7 * 0x4000 + cpu - 0x8000


PATCHES = [  # (file offset, old bytes, new bytes, what)
    (0x4A9E, bytes([0xB1, 0x47, 0x56]), bytes([0xF0, 0x47, 0x56]), 'ROM PLAY entry -> RET'),
    (0x11E5, bytes.fromhex('3f1c2b3e1c31'), bytes([NOP] * 6), '[1] version / [2] credits at power-on: out'),
    (0x11F1, bytes.fromhex('331c03e7d406'), bytes([0x20, 0x04]) + bytes([NOP] * 4), '[5] Extend Product Mode: out'),
    (0x11F7, bytes.fromhex('321c03e74a07'), bytes([0x20, 0x04]) + bytes([NOP] * 4), '[6] Service Mode: out'),
] + [
    (_p7(e), _lj(e, t), _lj(e, RET7), 'page 7 jump entry %04X -> RET' % e)
    for e, t in ((0x8000, 0xBA0A), (0x8003, 0xBB32), (0x8006, 0xBBAD), (0x8009, 0xBBCC), (0x800C, 0xBBD9),
                 (0x805A, 0xAD4E), (0x805D, 0xB13B), (0x8060, 0xAE9D), (0x8063, 0xAFEC), (0x806C, 0x8143))
]
FREED = [  # (page or None for fixed ROM, CPU start, CPU end inclusive)
    (7, 0x8143, 0x83A8),       # factory test mode (only Service Mode entered it, via 0x806C)
    (7, 0x83A9, 0x9C47),       # ROM PLAY data
    (7, 0xAD4E, 0xB289),       # credits, Extend Product Mode, Service Mode, version screens
    (7, 0xBA0A, 0xBCA6),       # ROM PLAY player
    (None, 0x18CB, 0x1991),    # Extend Product Mode + Service Mode loops
    (None, 0x4A9F, 0x4B1B),    # ROM PLAY keys
    (None, 0x72EF, 0x73B1),    # bulk dump all memory
    (None, 0x73D8, 0x7613),    # bulk load all memory + helpers
]


def apply(img):
    for off, old, new, what in PATCHES:
        assert bytes(img[off:off + len(old)]) == old, what
        img[off:off + len(new)] = new
    for page, a, b in FREED:
        f = a if page is None else page * 0x4000 + a - 0x8000
        img[f:f + b - a + 1] = b'\xff' * (b - a + 1)
    return apply_screens(img)


# ================= stock screens Rosetta replaces =================
# Play screens are R56 (UI mode R5A = 0). Per screen: step routine (table 0x345A), enter routine (0x3502, reached
# by STB R56 ; LCALL 52ED ; LJMP 34F4), display record (page 5 0xAC46 -> R59 screen descriptor + R57). Descriptors:
# page 4 pointer table 0x8064 (R59 0x00-0x37), display lists with the text inline, 0x80D4-0x9C33.
# Cut: tone edit 0x36-0x3A, rhythm edit 0x3B-0x3F, D.Xfer 0x41-0x44, MIDI OUT / palette / effector 0x2D-0x2F,
# MIDI CTRL map 0x1C-0x1E 0x2C 0x4A 0x4B, MIDI OUT play displays 0x14-0x1B, reverb / chorus 0x1F-0x20.
# Each gets screen 01's routines + display (so any stock path into one is harmless), and the Rosetta menu's main
# pass (synth.REDIRECT) moves R56 back to 01 and opens the matching Rosetta page. UI modes 0x0F-0x1B (stock's
# EFFECT/CTRL and MIDI OUT buttons, which Rosetta takes first) get mode 00's display record.
CUT_R56 = (list(range(0x14, 0x21)) + [0x2C, 0x2D, 0x2E, 0x2F] + list(range(0x36, 0x40)) + list(range(0x41, 0x45))
           + [0x47, 0x4A, 0x4B])
CUT_R5A = list(range(0x0F, 0x1C))
STEP_T, ENTER_T, DISP_T, MODE_T, DESC_T = 0x345A, 0x3502, 0xAC46, 0xB0D6, 0x8064   # DISP / MODE page 5, DESC page 4
N_R56, N_R5A, N_DESC = 0x4D, 0x20, 0x38


def _w(img, f): return img[f] | img[f + 1] << 8
def _sw(img, f, v): img[f] = v & 255; img[f + 1] = v >> 8


def apply_screens(img):
    """-> [(page 4 cpu start, end)] blanked descriptor ranges"""
    p4 = lambda a: 4 * 0x4000 + a - 0x8000
    p5 = lambda a: 5 * 0x4000 + a - 0x8000
    for r in CUT_R56:
        for t in (STEP_T, ENTER_T):
            _sw(img, t + 2 * r, _w(img, t + 2 * 1))
        _sw(img, p5(DISP_T) + 2 * r, _w(img, p5(DISP_T) + 2 * 1))
    for m in CUT_R5A:
        _sw(img, p5(MODE_T) + 2 * m, _w(img, p5(MODE_T)))
    used = set()                                        # descriptors any record still names
    for t, n in ((DISP_T, N_R56), (MODE_T, N_R5A)):
        for k in range(n):
            rec = _w(img, p5(t) + 2 * k); r59 = img[p5(rec)]
            if r59 < N_DESC: used.add(_w(img, p4(DESC_T) + 2 * r59))
    starts = sorted(set(_w(img, p4(DESC_T) + 2 * k) for k in range(N_DESC)) | {0x9C34})
    blanked = []
    for a, b in zip(starts, starts[1:]):
        if a not in used:
            img[p4(a):p4(b)] = b'\xff' * (b - a); blanked.append((a, b - 1))
    return blanked


def free_bytes(page=7):
    return sum(b - a + 1 for p, a, b in FREED if p == page)
