"""Stock features removed from v1.11 to free ROM.

ROM PLAY (demo song "Schizoid", Eric Persing):
  entry 0x4A9E (fixed ROM, reached from the mode tables at 0x3590 / 0x44EC): LDB R56,#47 (ROM Play screen) then the
  page-7 start routine. Patched to RET: the ROM Play key does nothing, the screen never becomes 0x47, so the
  sequencer tick (0x1475, only when R56 = 0x47) and the ROM Play key handler (0x4AB3) never run.
  Data freed, page 7 CPU 0x83A9-0x9C47 (file 0x1C3A9-0x1DC47, 6303 bytes):
    0x83A9 setup (108 B -> D5B4), 0x8415 5 parts x 56 B, 0x852D-0x8832 20 tone records x 86 B (pointer table 0xBAE0),
    0x8833-0x8B21 the demo performance (copied into IC6 0x8160 after backing the user's one up to RAM 0xC400),
    0x8B22-0x9C47 the song events.
  Kept: test mode ([6] at power-on) code 0x8133-0x828E and its data 0x828F-0x839B.
"""

PATCHES = [  # (file offset, old bytes, new bytes, what)
    (0x4A9E, bytes([0xB1, 0x47, 0x56]), bytes([0xF0, 0x47, 0x56]), 'ROM PLAY entry -> RET'),
]
FREED = [  # (page, CPU start, CPU end inclusive)
    (7, 0x83A9, 0x9C47),
]


def apply(img):
    for off, old, new, what in PATCHES:
        assert bytes(img[off:off + len(old)]) == old, what
        img[off:off + len(new)] = new
    for page, a, b in FREED:
        f = page * 0x4000 + a - 0x8000
        img[f:f + b - a + 1] = b'\xff' * (b - a + 1)


def free_bytes():
    return sum(b - a + 1 for _, a, b in FREED)
