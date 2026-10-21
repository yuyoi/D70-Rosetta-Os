"""Build Rosetta OS for the Roland D-70 from your own v1.11 EPROM dump.

    python -I tools/boot_build.py dumps/IC4_even.bin dumps/IC9_odd.bin [--out build] [--frames frames/x.npz]

IC4 = the EVEN EPROM, IC9 = the ODD EPROM (27C512, 64 KB each). Writes to build/: the two 27C512 images to burn,
the combined 128 KB image, a boot preview GIF, a menu tour PNG and the player listing.
Every build is checked in sim.py: the real player bytes run against an LCD model and every frame is compared,
the menu is driven through a key tour, the IC6 dump is decoded back, and the slider hook is exercised.
"""
import os, sys, hashlib, argparse
import numpy as np
from PIL import Image
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import codec_d70 as C
import player as PL
import menu as MN
import sliders as SL
import cuts
import font57
from sim import Sim

ap = argparse.ArgumentParser(description='Build Rosetta OS for the Roland D-70 from your own v1.11 dump.')
ap.add_argument('even', help='IC4 (even) dump, 64 KB')
ap.add_argument('odd', help='IC9 (odd) dump, 64 KB')
ap.add_argument('--out', default=os.path.join(HERE, '..', 'build'))
ap.add_argument('--frames', default=os.path.join(HERE, '..', 'frames', 'roland_chan_frames.npz'))
ARGS = ap.parse_args()
NPZ, OUT = ARGS.frames, ARGS.out
MD5_A = '3f9e12e8d73380a5c26c312940f1ff6e'      # v1.11 IC4 (even)
MD5_B = '640e279056386577499544f826df9253'      # v1.11 IC9 (odd)
HOOK, HOOK_OLD = 0x130D, bytes([0xB1, 0x00, 0x09])   # LDB INT_PEND,#00
STREAM_FILE, STREAM_MAX = 0x0000, 0x1000       # free FF, seen at CPU 0x8000 with bank 0
CODE_END = 0x2400                              # free FF runs 0x2083-0x23FF


def pack(px):
    """bool[64][240] -> 2560 bytes, 6 px per byte, bit 5 = leftmost (T6963C 6-dot mode)"""
    out = bytearray()
    for row in px:
        for c in range(0, 240, 6):
            v = 0
            for k in range(6):
                v = v << 1 | int(row[c + k])
            out.append(v)
    return bytes(out)


def unpack(b):
    px = np.zeros((64, 240), bool)
    for i, v in enumerate(b):
        r, c = divmod(i, 40)
        for k in range(6):
            px[r, c * 6 + k] = v >> (5 - k) & 1
    return px


def main():
    a, b = open(ARGS.even, 'rb').read(), open(ARGS.odd, 'rb').read()
    ma, mb = hashlib.md5(a).hexdigest(), hashlib.md5(b).hexdigest()
    if (ma, mb) == (MD5_B, MD5_A):
        sys.exit('even and odd are swapped: IC4 = even, IC9 = odd')
    if (ma, mb) != (MD5_A, MD5_B):
        sys.exit('not D-70 v1.11 (MD5 %s / %s). Only v1.11 is supported so far.' % (ma, mb))
    img = bytearray(128 * 1024); img[0::2] = a; img[1::2] = b
    cuts.apply(img)                                    # ROM PLAY demo out: page 7 0x83A9-0x9C47 free

    z = np.load(NPZ)
    scr, ticks = z['screens'].astype(bool), [int(t) for t in z['ticks']]
    h, w = scr.shape[1:]
    x0, y0 = ((240 - w) // 2) // 6 * 6, (64 - h) // 2
    frames = []
    for s in scr:
        f = np.zeros((64, 240), bool); f[y0:y0 + h, x0:x0 + w] = s
        frames.append(pack(f))

    stream = b'JSW1' + C.build_stream(frames, ticks, PL.STREAM + 4)
    assert len(stream) <= STREAM_MAX, 'stream %d bytes, room %d' % (len(stream), STREAM_MAX)
    assert set(img[STREAM_FILE:STREAM_FILE + len(stream)]) == {0xFF}
    img[STREAM_FILE:STREAM_FILE + len(stream)] = stream

    asm = PL.build(); code = asm.assemble()
    end = PL.ORG + len(code)
    assert end <= CODE_END and set(img[PL.ORG:end]) == {0xFF}, 'player does not fit (%04X)' % end
    img[PL.ORG:end] = code

    # menu: code in page 0 after the stream, data blob copied from the page 3 / page 5 gaps into RAM
    bc = asm.labels['ic6_copy']
    dummy = MN.data({}).lidx
    m0 = MN.build(bc, ext={k: 0 for k in dummy}, split=[1, 1]); m0.assemble()
    dasm = MN.data(m0.labels); dblob = dasm.assemble()
    r0 = MN.DATA_ROM[0][2]; split = [min(len(dblob), r0), len(dblob) - min(len(dblob), r0)]
    assert split[1] <= MN.DATA_ROM[1][2], 'menu data does not fit (%d B)' % len(dblob)
    masm = MN.build(bc, ext=dasm.labels, split=split); mcode = masm.assemble()
    if masm.labels != m0.labels:                       # layout moved (second copy dropped): data again
        dasm = MN.data(masm.labels); dblob = dasm.assemble()
        masm = MN.build(bc, ext=dasm.labels, split=split); mcode = masm.assemble()
        assert len(dblob) == sum(split)
    off = 0
    for (bank, cpu, room), n in zip(MN.DATA_ROM, split):
        f = bank * 0x4000 + cpu - 0x8000
        assert set(img[f:f + room]) == {0xFF} and f + room in (0x10000, 0x18000)
        img[f:f + n] = dblob[off:off + n]; off += n
    assert len(stream) <= MN.FILE_OFF, 'stream runs into the menu'
    assert MN.FILE_OFF + len(mcode) <= STREAM_MAX, 'menu does not fit (%d B)' % len(mcode)
    assert set(img[MN.FILE_OFF:MN.FILE_OFF + len(mcode)]) == {0xFF}
    img[MN.FILE_OFF:MN.FILE_OFF + len(mcode)] = mcode
    assert masm.labels['menu_entry'] == PL.MENU

    # common sliders: hook code right after the player, LJMP over the start of the stock loop at 0x4CB6
    s0 = SL.build(end); s0.assemble()
    sasm = SL.build(end, s0.labels['sltab']); scode = sasm.assemble()
    send = end + len(scode)
    assert send <= CODE_END and set(img[end:send]) == {0xFF}, 'slider hook does not fit (%04X)' % send
    img[end:send] = scode
    assert bytes(img[SL.HOOK:SL.HOOK + len(SL.HOOK_OLD)]) == SL.HOOK_OLD
    d = (end - (SL.HOOK + 3)) & 0xFFFF
    img[SL.HOOK:SL.HOOK + 3] = bytes([0xE7, d & 255, d >> 8])
    slider_check(bytes(img), sasm)

    assert bytes(img[HOOK:HOOK + 3]) == HOOK_OLD
    d = (PL.ORG - (HOOK + 3)) & 0xFFFF
    img[HOOK:HOOK + 3] = bytes([0xEF, d & 255, d >> 8])

    # ---- check: run the player bytes from the image
    sim = Sim(bytes(img), hold_pc=asm.labels['hold'])
    sim.reg[0x54] = 0x02; sim.reg[0xA2] = 0x0C            # stock bank + display flags at the hook
    sim.ram[PL.KEY_EDIT - 0xC000] = 0                     # no [EDIT] at power-on: no menu
    for r in range(0x30, 0x4C): sim.reg[r] = r            # registers must come back unchanged
    ram0 = bytes(sim.ram); reg0 = bytes(sim.reg)
    sim.lcd.ram[0x1400:0x1600] = bytes(range(256)) * 2     # text area must be untouched
    lcd0 = bytes(sim.lcd.ram)
    steps = sim.run(PL.ORG)
    assert sim.frames == frames, 'frame mismatch: %d frames shown, first bad %s' % (
        len(sim.frames), next((i for i, (x, y) in enumerate(zip(sim.frames, frames)) if x != y), None))
    assert sim.ram[:0x3E00] == ram0[:0x3E00], 'RAM1 not restored'       # 0xFE00-0xFFFF = sim stack
    assert bytes(sim.reg[0x30:0x4C]) == reg0[0x30:0x4C] and sim.reg[0x54] == 0x02, 'registers changed'
    assert sim.reg[0x09] == 0 and sim.bank == 0x02, 'hook instruction / bank not restored'
    assert bytes(sim.lcd.ram) == lcd0, 'LCD RAM not left as found'
    assert sim.lcd.mode == 0x9C, 'display mode not restored'

    tour = menu_tour(bytes(img), asm, masm)
    assert dump_check(bytes(img), masm), 'IC6 dump does not match'

    os.makedirs(OUT, exist_ok=True)
    save_tour(tour, os.path.join(OUT, 'D70_rosetta_v02_menu_tour.png'))
    name = 'D70_rosetta_v02'
    ev, od = bytes(img[0::2]), bytes(img[1::2])
    for suf, data in (('IC4_even', ev), ('IC9_odd', od), ('combined', bytes(img))):
        open(os.path.join(OUT, '%s_%s.bin' % (name, suf)), 'wb').write(data)
    open(os.path.join(OUT, name + '_player.lst'), 'w').write(asm.listing(code))
    ims = [Image.fromarray(np.where(unpack(f)[..., None], LCD_ON, LCD_OFF)).resize((720, 192), Image.NEAREST)
           for f in sim.frames]
    ims[0].save(os.path.join(OUT, name + '_preview.gif'), save_all=True, append_images=ims[1:],
                duration=[t * 50 for t in ticks], loop=0)
    print('cuts: ROM PLAY demo removed, %d B free on page 7 (CPU 0x83A9-0x9C47)' % cuts.free_bytes())
    print('slider hook %04X-%04X (%d B, fixed ROM left %d B)' % (end, send - 1, len(scode), CODE_END - send))
    print('menu code %04X-%04X (%d B, room to %04X) | data %d B (%d + %d) | tour %d screens' % (
          MN.ORG, MN.ORG + len(mcode) - 1, len(mcode), 0x8000 + STREAM_MAX - 1, len(dblob), split[0], split[1], len(tour)))
    print('frames %d, %.1f s | stream %d B (room %d) | player %04X-%04X (%d B) | sim %d steps' % (
        len(frames), sum(ticks) * 0.05, len(stream), STREAM_MAX, PL.ORG, end - 1, len(code), steps))
    for suf, data in (('IC4_even', ev), ('IC9_odd', od)):
        print('%s  MD5 %s' % (suf, hashlib.md5(data).hexdigest()))


# ---- menu tour: hold [EDIT] at power-on, then press keys; a screen is grabbed after every redraw
KEYS = {'UP': (3, 0x04), 'DOWN': (3, 0x20), 'INC': (3, 0x40), 'DEC': (3, 0x80), 'ENTER': (0, 0x01),
        'EXIT': (4, 0x80), 'RIGHT': (3, 0x08), 'LEFT': (3, 0x10), 'N4': (2, 0x10), 'EDIT': (5, 0x80)}
TOUR = ['ENTER', 'ENTER', 'EXIT', 'DOWN', 'ENTER', 'DOWN', 'INC', 'INC', 'DOWN', 'DEC', 'EXIT', 'DOWN',
        'ENTER', 'EXIT', 'N4', 'ENTER', 'EXIT', 'DOWN', 'ENTER']


def menu_tour(img, asm, masm):
    sim = Sim(img, tick=4)
    sim.reg[0x54] = 0x02; sim.reg[0xA2] = 0x0C
    sim.ram[PL.KEY_EDIT - 0xC000] = 0x80
    script = [('EDIT', 3), (None, 2)]
    for k in TOUR: script += [(k, 2), (None, 2)]
    state = {'i': 0, 'left': None}
    def scan(sm):
        bits = [0] * 8
        if state['i'] < len(script):
            k, n = script[state['i']]
            if state['left'] is None: state['left'] = n
            if k: b, m = KEYS[k]; bits[b] = m
            state['left'] -= 1
            if state['left'] == 0: state['i'] += 1; state['left'] = None
        for j in range(8): sm.wb(MN.KCUR + j, bits[j])
        return True
    sim.hooks[masm.labels['scan']] = scan
    shots = []
    def grab(sm):
        shots.append((bytes(sm.lcd.ram[0:2560]), bytes(sm.lcd.ram[0x1400:0x1540])))
        return False
    sim.hooks[masm.labels['loop']] = grab
    sim.frames = []; sim.hold_pc = None
    try:
        sim.run(PL.ORG, limit=60_000_000)
    except Exception as e:
        raise RuntimeError('menu tour: %s at %04X %s (script step %d)' % (e, sim.last[0], sim.last[1], state['i']))
    assert state['i'] >= len(script) - 2, 'tour stopped early at step %d' % state['i']
    assert bytes(sim.lcd.ram[0x1400:0x1540]) == bytes(0x140), 'menu text not cleared'
    out, last = [], None
    for g, t in shots:
        if (g, t) != last: out.append((g, t)); last = (g, t)
    return out


def slider_check(img, sasm):
    """run the hook from 0x4CB6 with the stock scale/write stubbed out and check every write"""
    for mode in (1, 0):
        sim = Sim(img)
        sim.ram[SL.D3B5 - 0xC000] = mode
        vals = [10, 50, 90, 127]
        for k, v in enumerate(vals): sim.ram[SL.D39C - 0xC000 + k] = v
        sim.ram[SL.D3A6 - 0xC000 + 2] = 90            # slider 3 unchanged: must be skipped
        sim.reg[0x65] = 0x07; sim.reg[0x58] = 0x33; sim.reg[0x2E] = 0x44; sim.reg[0x2F] = 0x55
        writes = []
        def scale(sm):
            sm.reg[0x1C] = sm.reg[0x1C] ^ 0x80; return True       # fake scale: mark the value
        def write(sm):
            writes.append((sm.reg[0x65], sm.reg[0x58], sm.reg[0x20])); return True
        resumed = []
        def resume(sm):
            resumed.append(sm.reg[0x1C]); return True               # stand-in for the stock rest of the loop
        sim.hooks[SL.SCALE] = scale; sim.hooks[SL.WRITE] = write; sim.hooks[SL.RESUME] = resume
        sim.run(SL.HOOK)
        if mode == 1:
            want = [(SL.CONTROLS[s], p, vals[s] ^ 0x80) for s in (3, 1, 0) for p in range(4)]
            assert writes == want, writes
            assert [sim.ram[SL.D3A6 - 0xC000 + k] for k in range(4)] == vals
            assert sim.reg[0x65] == 7 and sim.reg[0x58] == 0x33 and sim.reg[0x2E] == 0x44 and not resumed
        else:
            assert not writes and resumed == [0] and sim.reg[0x58] == 0


def dump_check(img, masm):
    """TOOLS > DUMP IC6: run it and decode the MIDI bytes back (ic6_decode.py) against the simulated IC6"""
    from ic6_decode import decode
    sim = Sim(img, tick=40); sim.reg[0x54] = 0x02; sim.reg[0xA2] = 0x0C
    sim.ram[PL.KEY_EDIT - 0xC000] = 0x80
    keys = dict(KEYS, N3=(2, 0x20))
    script = [('EDIT', 3), (None, 2), ('N3', 2), (None, 2), ('ENTER', 2), (None, 2), ('ENTER', 2), (None, 2),
              ('EXIT', 2), (None, 2), ('EXIT', 2), (None, 2)]
    st = {'i': 0, 'n': None}
    def scan(sm):
        bits = [0] * 8
        if st['i'] < len(script):
            k, n = script[st['i']]
            if st['n'] is None: st['n'] = n
            if k: b, m = keys[k]; bits[b] = m
            st['n'] -= 1
            if st['n'] == 0: st['i'] += 1; st['n'] = None
        for j in range(8): sm.wb(MN.KCUR + j, bits[j])
        return True
    sim.hooks[masm.labels['scan']] = scan
    sim.run(PL.ORG, limit=80_000_000)
    out, n = decode(bytes(sim.tx))
    return n == 256 and out == sim.ic6


def render(g, t):
    """graphics layer XOR text layer (font57, 5x7 in a 6x8 cell)"""
    tx = np.zeros((64, 240), bool)
    for i, code in enumerate(t):
        r, c = divmod(i, 40)
        for y, row in enumerate(font57.glyph(chr(code + 0x20))):
            for k, on in enumerate(row):
                if on: tx[r * 8 + y, c * 6 + k] = True
    return unpack(g) ^ tx


LCD_ON, LCD_OFF = np.array([40, 44, 48], np.uint8), np.array([158, 170, 160], np.uint8)


def save_tour(tour, path):
    S, cols = 3, 2
    rows = (len(tour) + cols - 1) // cols
    sheet = Image.new('RGB', (cols * (240 * S + 12) + 12, rows * (64 * S + 12) + 12), (30, 30, 30))
    for i, (g, t) in enumerate(tour):
        px = render(g, t)
        im = Image.fromarray(np.where(px[..., None], LCD_ON, LCD_OFF))
        im = im.resize((240 * S, 64 * S), Image.NEAREST)
        r, c = divmod(i, cols); sheet.paste(im, (12 + c * (240 * S + 12), 12 + r * (64 * S + 12)))
    sheet.save(path)


if __name__ == '__main__':
    main()
