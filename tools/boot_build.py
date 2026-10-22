"""Build Rosetta OS for the Roland D-70 from your own v1.11 EPROM dump.

    python -I tools/boot_build.py dumps/IC4_even.bin dumps/IC9_odd.bin [--out build] [--frames frames/x.npz]

IC4 = the EVEN EPROM, IC9 = the ODD EPROM (27C512, 64 KB each). Writes to build/: the two 27C512 images to burn,
the combined 128 KB image, a boot preview GIF, menu tour PNGs (boot menu + SYNTH menu) and the player listing.
Every build is checked in sim.py: the real player bytes run against an LCD model and every frame is compared,
the menus are driven through key tours, the ARP and the stock-screen cut are exercised, the IC6 dump is decoded
back, and page 4's code is scanned for references into the cut screens.
"""
import os, re, sys, hashlib, argparse
import numpy as np
from PIL import Image
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import codec_d70 as C
import player as PL
import menu as MN
import sliders as SL
import synth as SY
import arp as AR
from asm import Asm
import cuts
import font57
from sim import Sim

ARGS = argparse.Namespace(even=None, odd=None)
NPZ = OUT = None


def configure(even, odd, out=None, frames=None):
    """set the dumps and folders (the command line at the bottom, or rosetta_patcher.py)"""
    global NPZ, OUT
    ARGS.even, ARGS.odd = even, odd
    OUT = out or os.path.join(HERE, '..', 'build')
    NPZ = frames or os.path.join(HERE, '..', 'frames', 'roland_chan_frames.npz')


MD5_A = '3f9e12e8d73380a5c26c312940f1ff6e'      # v1.11 IC4 (even)
MD5_B = '640e279056386577499544f826df9253'      # v1.11 IC9 (odd)
HOOK, HOOK_OLD = 0x130D, bytes([0xB1, 0x00, 0x09])   # LDB INT_PEND,#00
STREAM_FILE, STREAM_MAX = 0x0000, 0x1000       # free FF, seen at CPU 0x8000 with bank 0
CODE_END = 0x2400                              # free FF runs 0x2083-0x23FF
UI_HOOK, UI_OLD = 0x151D, bytes([0xEF, 0xDF, 0x1E])   # LCALL 33FF (stock UI step)


def pack(px, width=240):
    """bool[rows][width] -> bytes, 6 px per byte, bit 5 = leftmost (T6963C 6-dot mode)"""
    out = bytearray()
    for row in px:
        for c in range(0, width, 6):
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
        raise ValueError('even and odd are swapped: IC4 = even, IC9 = odd')
    if (ma, mb) != (MD5_A, MD5_B):
        raise ValueError('not D-70 v1.11 (MD5 %s / %s). Only v1.11 is supported so far.' % (ma, mb))
    img = bytearray(128 * 1024); img[0::2] = a; img[1::2] = b
    SCAN_DATA = {0x9C50, 0xB11C, 0xB15C}               # jump table 9C4A, byte table B0F8-B1F7 (checked by hand)
    blanked = cuts.apply(img)                          # ROM PLAY demo out: page 7 0x83A9-0x9C47 free
    from mcs96 import decode                           # nothing in page 4's code may point into a blanked screen
    pc = 0x9C34; hits = []
    while pc < 0xBE54:
        t, ln, kind, tg = decode(img, 4 * 0x4000 + pc - 0x8000, pc)
        for v in re.findall(r'#([0-9A-F]{4})\b|\b([0-9A-F]{4})\[', t):   # data refs (relative jumps: table noise)
            v = v[0] or v[1]
            if any(a <= int(v, 16) <= b for a, b in blanked) and pc not in SCAN_DATA:
                hits.append('%04X %s' % (pc, t))
        pc += max(ln, 1)
    assert not hits, 'page 4 code points into a cut screen: ' + ' | '.join(hits)
    print('screens cut: %d B of page 4 descriptors (%s)' % (sum(b - a + 1 for a, b in blanked),
          ' '.join('%04X-%04X' % r for r in blanked)))

    z = np.load(NPZ)
    scr, ticks = z['screens'].astype(bool), [int(t) for t in z['ticks']]
    xs = np.where(scr.any(axis=0).any(axis=0))[0]          # the art's columns, 108 px wide (= 18 bytes)
    sx = min(max(int(xs.min()), 0), scr.shape[2] - PL.SW * 6)
    small = scr[:, :PL.SH, sx:sx + PL.SW * 6]
    assert not scr[:, :, :sx].any() and not scr[:, :, sx + PL.SW * 6:].any(), 'art wider than 108 px'
    frames = []                                            # what the LCD must show: the art doubled, centred
    for s in small:
        f = np.zeros((64, 240), bool); f[:, PL.LX * 6:PL.LX * 6 + PL.SW * 12] = s.repeat(2, 0).repeat(2, 1)
        frames.append(pack(f))
    C.set_size(PL.SW, PL.SH)
    sframes = [pack(sf, PL.SW * 6) for sf in small]      # 18 bytes x 32 rows each
    stream = b'JSW1' + C.build_stream(sframes, ticks, PL.STREAM + 4)
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

    # page 7 (the freed demo space): common sliders + the SYNTH menu; fixed ROM keeps two 7-byte trampolines
    TRAMP = 0x18CB                                       # freed Extend Product Mode code (cuts.py)
    SY.IC6_RD = TRAMP + 21                               # fixed helper after the three trampolines
    dsegs, dlabels = SY.build_data()                    # SYNTH tables + strings -> the freed page-7 blocks
    for org, blob in dsegs:
        f = 7 * 0x4000 + org - 0x8000
        assert set(img[f:f + len(blob)]) == {0xFF}, 'data block %04X not free' % org
        img[f:f + len(blob)] = blob
    fx = Asm(AR.TICK_ORG); fx.ext = {}; AR.emit_tick(fx); fxcode = fx.assemble()   # ARP clock: freed fixed ROM
    assert AR.TICK_ORG + len(fxcode) <= AR.TICK_END + 1, 'ARP clock does not fit (%d B)' % len(fxcode)
    assert set(img[AR.TICK_ORG:AR.TICK_ORG + len(fxcode)]) == {0xFF}
    img[AR.TICK_ORG:AR.TICK_ORG + len(fxcode)] = fxcode
    p4 = Asm(AR.IN_ORG); p4.ext = {}; AR.emit_in(p4); p4code = p4.assemble()     # ARP key in: page 4 free tail
    f4 = 4 * 0x4000 + AR.IN_ORG - 0x8000
    assert AR.IN_ORG + len(p4code) <= AR.IN_END + 1, 'ARP key in does not fit (%d B)' % len(p4code)
    assert set(img[f4:f4 + len(p4code)]) == {0xFF}
    img[f4:f4 + len(p4code)] = p4code
    p7 = Asm(SY.ORG); p7.ext = dict(dlabels, **fx.labels); SL.emit(p7); SY.emit(p7)
    p7code = p7.assemble(); p7.labels.update(fx.labels); p7.labels.update(p4.labels)
    f7 = 7 * 0x4000 + SY.ORG - 0x8000
    assert SY.ORG + len(p7code) <= SY.END + 1, 'page 7 code does not fit (%d B)' % len(p7code)
    assert set(img[f7:f7 + len(p7code)]) == {0xFF}
    img[f7:f7 + len(p7code)] = p7code
    tramps = {}
    t = TRAMP
    for name, stub in (('sl_hook', 0x105F), ('rt_ui', 0x105F), ('arp_in', 0x1056)):   # LD R1A,#target ; LJMP stub
        tramps[name] = t
        assert set(img[t:t + 7]) == {0xFF}
        v = p7.labels[name]; d = (stub - (t + 7)) & 0xFFFF
        img[t:t + 7] = bytes([0xA1, v & 255, v >> 8, 0x1A, 0xE7, d & 255, d >> 8]); t += 7
    assert t == SY.IC6_RD and set(img[t:t + 16]) == {0xFF}
    # IC6 byte read for page-7 code: DI ; STB 0100,R1C ; LDB R26,[R20] ; STB 0100,R54 ; EI ; RET
    img[t:t + 16] = bytes([0xFA, 0xC7, 0x01, 0x00, 0x01, 0x1C, 0xB2, 0x20, 0x26, 0xC7, 0x01, 0x00, 0x01, 0x54,
                           0xFB, 0xF0]); t += 16
    send = t
    assert send <= 0x1991 + 1, 'trampolines do not fit'
    assert bytes(img[SL.HOOK:SL.HOOK + len(SL.HOOK_OLD)]) == SL.HOOK_OLD
    d = (tramps['sl_hook'] - (SL.HOOK + 3)) & 0xFFFF
    img[SL.HOOK:SL.HOOK + 3] = bytes([0xE7, d & 255, d >> 8])
    assert bytes(img[UI_HOOK:UI_HOOK + 3]) == UI_OLD
    d = (tramps['rt_ui'] - (UI_HOOK + 3)) & 0xFFFF
    img[UI_HOOK:UI_HOOK + 3] = bytes([0xEF, d & 255, d >> 8])
    assert bytes(img[AR.PUMP_HOOK:AR.PUMP_HOOK + 3]) == AR.PUMP_OLD   # keyboard events -> arp_in
    d = (tramps['arp_in'] - (AR.PUMP_HOOK + 3)) & 0xFFFF
    img[AR.PUMP_HOOK:AR.PUMP_HOOK + 3] = bytes([0xE7, d & 255, d >> 8])
    slider_check(bytes(img), p7)
    arp_check(bytes(img), p7)
    synth_shots = synth_check(bytes(img), p7, tramps['rt_ui'])

    assert bytes(img[HOOK:HOOK + 3]) == HOOK_OLD
    d = (PL.ORG - (HOOK + 3)) & 0xFFFF
    img[HOOK:HOOK + 3] = bytes([0xEF, d & 255, d >> 8])

    # ---- check: run the player bytes from the image
    sim = Sim(bytes(img), hold_pc=asm.labels['hold'])
    sim.reg[0x54] = 0x02; sim.reg[0xA2] = 0x0C            # stock bank + display flags at the hook
    sim.ram[PL.KEY_EDIT - 0xC000] = 0                     # no [EDIT] at power-on: no menu
    sim.ram[PL.KEY_EXIT - 0xC000] = 0x80                  # [EXIT] held: plain D-70, RAM must come back untouched
    for r in range(0x30, 0x4C): sim.reg[r] = r            # registers must come back unchanged
    ram0 = bytes(sim.ram); reg0 = bytes(sim.reg)
    sim.lcd.ram[0x1400:0x1600] = bytes(range(256)) * 2     # text area must be untouched
    lcd0 = bytes(sim.lcd.ram)
    steps = sim.run(PL.ORG)
    for r in range(0x1C, 0x30): pass
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
    save_tour(tour, os.path.join(OUT, 'D70_rosetta_v06_menu_tour.png'))
    save_tour(synth_shots, os.path.join(OUT, 'D70_rosetta_v06_synth_tour.png'))
    name = 'D70_rosetta_v06'
    ev, od = bytes(img[0::2]), bytes(img[1::2])
    for suf, data in (('IC4_even', ev), ('IC9_odd', od), ('combined', bytes(img))):
        open(os.path.join(OUT, '%s_%s.bin' % (name, suf)), 'wb').write(data)
    open(os.path.join(OUT, name + '_player.lst'), 'w').write(asm.listing(code))
    ims = [Image.fromarray(np.where(unpack(f), 40, 200).astype(np.uint8), 'L').resize((720, 192), Image.NEAREST)
           for f in sim.frames]
    ims[0].save(os.path.join(OUT, name + '_preview.gif'), save_all=True, append_images=ims[1:],
                duration=[t * 50 for t in ticks], loop=0)
    print('cuts: page 7 %d B freed, fixed ROM %d B freed (free blocks: page 7 0xAD4E-0xB289, 0xBA0A-0xBCA6)' % (cuts.free_bytes(7), cuts.free_bytes(None)))
    print('player %04X-%04X, trampolines %04X-%04X | page 7 %04X-%04X (%d B, left %d B)' % (PL.ORG, end - 1, TRAMP, send - 1, SY.ORG, SY.ORG + len(p7code) - 1, len(p7code), SY.END + 1 - SY.ORG - len(p7code)))
    print('SYNTH data: ' + ', '.join('%04X+%d' % (org, len(b)) for org, b in dsegs) + ' (blocks: ' +
          ', '.join('%04X-%04X' % sg for sg in SY.DATA_SEGS) + ')')
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
        sim.reg[0x54] = 0x02
        writes = []
        def scale(sm):
            assert sm.bank == 2, 'stock scale called without page 2'
            sm.reg[0x1C] = sm.reg[0x1C] ^ 0x80; return True       # fake scale: mark the value
        def write(sm):
            assert sm.bank == 2, 'stock write called without page 2'
            writes.append((sm.reg[0x65], sm.reg[0x58], sm.reg[0x20])); return True
        resumed = []
        def resume(sm):
            assert sm.bank == 2, 'stock slider loop called without page 2'
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
        assert sim.bank == 0x02, 'bank not restored'


def arp_check(img, p7):
    """keys in through the 0x1092 hook, notes out through page 4 0xB49F: pass-through when off, UP over 2 octaves,
    gate, UP-DOWN, release"""
    sim = Sim(img, ram_fill=0)
    keys, out = [], []
    def key_event(sm):
        if keys: n, v = keys.pop(0); sm.reg[0x1C] = n; sm.reg[0x1D] = v; sm.c = True
        else: sm.c = False
        return True
    def queue(sm):
        assert sm.bank == 4, 'queue called without page 4'
        out.append((sm.reg[0x1E], sm.reg[0x1C], sm.reg[0x1D])); return True
    sim.hooks[AR.KEY_EVENT] = key_event; sim.hooks[0xB49F] = queue
    def press(n, v):                                   # one pump call, as page 4 0xB484 makes it
        keys.append((n, v)); sim.reg[0x1E] = 0x10; sim.reg[0x54] = 4; sim.bank = 4; sim.reg[0x1A] = 0x92; sim.reg[0x1B] = 0x10
        for k in range(0x20, 0x30): sim.reg[k] = k ^ 0x5A
        sim.run(0x1050); assert sim.bank == 4 and sim.reg[0x1E] == 0x10, 'pump context not restored'
        assert all(sim.reg[k] == k ^ 0x5A for k in range(0x20, 0x30)), 'arp_in changed R20-R2F (the pump caller keeps values there)'
        return sim.c, sim.reg[0x1C], sim.reg[0x1D]
    def tick(n=1, dt=20 * 256):
        for _ in range(n):
            sim.t1 = (sim.t1 + dt) & 0xFFFF; sim.reg[0x54] = 7; sim.bank = 7; sim.run(p7.labels['arp_tick'])
    r = lambda a: sim.ram[a - 0xC000]
    DN = 0x80                                          # velocity bit 7 set = key down (page 6 0x82B0)
    assert press(60, DN | 100) == (True, 60, DN | 100), 'ARP off must pass keys through'
    assert press(60, 0x30) == (True, 60, 0x30)
    tick(); assert r(AR.A_BPM) == 120
    sim.ram[AR.A_ON - 0xC000] = 1; sim.ram[AR.A_OCT - 0xC000] = 1
    for n, v in ((67, 90), (60, 100), (64, 80)): assert press(n, DN | v)[0] is False, 'ARP on must swallow keys'
    assert [r(AR.A_LIST + k) for k in range(r(AR.A_N))] == [60, 64, 67]
    tick(130)
    ons = [n for t_, n, v in out if v & DN]
    assert ons[:7] == [60, 64, 67, 72, 76, 79, 60], ons
    assert all(t_ == 0x10 for t_, n, v in out)
    st = []                                            # every note goes off before the next one starts
    for t_, n, v in out:
        if v & DN: assert not st, out; st.append(n)
        else: assert st == [n], out; st.pop()
    assert ons and out[0][2] == DN | 80                # velocity of the last key
    out.clear(); sim.ram[AR.A_MODE - 0xC000] = 2; sim.ram[AR.A_IDX - 0xC000] = 0xFF
    tick(240); ons = [n for t_, n, v in out if v & DN]
    assert ons[:12] == [60, 64, 67, 72, 76, 79, 76, 72, 67, 64, 60, 64], ons
    for n in (60, 64, 67): assert press(n, 0x30)[0] is False, 'ARP on must swallow key ups'
    tick(40); assert r(AR.A_N) == 0 and r(AR.A_PLAY) == 0 and not out[-1][2] & DN, (r(AR.A_N), r(AR.A_PLAY), r(AR.A_KEYS), out[-3:], [r(AR.A_LIST + k) for k in range(4)])
    out.clear(); sim.ram[AR.A_MODE - 0xC000] = 0; sim.ram[AR.A_OCT - 0xC000] = 3   # 4 octaves up from the top
    press(98, DN | 64); tick(200); press(98, 0x30); tick(40)
    ons = [n for t_, n, v in out if v & DN]
    assert ons and max(ons) <= AR.NOTE_TOP and min(ons) >= 28, ons


def synth_check(img, p7, ui):
    """drive the SYNTH menu through main-loop passes with the stock key reader / parameter read-write stubbed:
    open with [USER], visit pages, edit values, close; check every write and the stock hand-back"""
    import synth as SY
    mem = {}
    # fake temporary part 1: four made-up tones (the repo carries no Roland data), patch sections
    for sec, nm in enumerate(('ROSETTA 1 ', 'HEX GIRL  ', 'JSW PAD   ', 'CLAUDE BEL')):
        t = [0] * 0x3E; t[0:10] = nm.encode('ascii'); t[0x0B] = 10 + sec * 7; t[0x10] = 24; t[0x11] = 32
        t[0x19] = 80 - sec * 10; t[0x1A] = 10 + sec * 5; t[0x1D] = 40; t[0x29] = 100
        t[0x2F:0x36] = [10 + sec, 100, 30, 80, 60, 70, 40 + sec * 5]; t[0x22:0x29] = [5, 90, 30, 70, 50, 60, 30]
        t[0x0A] = 0x40
        for k in range(0x3E): mem[0x0351 + sec * 0x3E + k] = t[k]
    pb = 0x019E
    mem[pb + 0x0D] = 1; mem[pb + 0x0E] = 60
    for sec, (lo, hi, lvl, on) in enumerate(((0, 59, 100, 1), (0, 59, 72, 1), (60, 127, 90, 1), (60, 127, 64, 0))):
        q = pb + 0x23 + sec * 0x0D
        mem[q + 0x01] = lvl; mem[q + 0x02] = 24; mem[q + 0x03] = 64; mem[q + 0x08] = 1; mem[q + 0x09] = 7
        mem[q + 0x0A] = on; mem[q + 0x0B] = lo; mem[q + 0x0C] = hi
    sim = Sim(img, ram_fill=0, tick=8)              # the stock boot clears work RAM
    sim.reg[0x54] = 0x02; sim.reg[0x5F] = 0
    writes, applies, calls = [], [], []
    keyq, dialq = [], []
    def p4(sm):
        if sm.rw(0x1A) == SY.GETKEY:
            if keyq: sm.reg[0x1C] = keyq.pop(0); sm.c = True
            else: sm.c = False
        elif sm.rw(0x1A) == SY.DIAL:
            sm.reg[0x1C] = dialq.pop(0) & 255 if dialq else 0
        else:
            raise AssertionError('page 4 entry %04X' % sm.rw(0x1A))
        return True
    def p5(sm):
        ad = sm.rw(0xD4) | sm.rw(0xD6) << 16
        if sm.rw(0x1A) == SY.RD: sm.reg[0xCA] = mem.get(ad, 0); sm.c = False
        elif sm.rw(0x1A) == SY.WR: mem[ad] = sm.reg[0xCA]; writes.append((ad, sm.reg[0xCA]))
        return True
    def apply(sm):
        assert sm.bank == 2, 'apply called without page 2'
        applies.append(len(writes)); return True
    sim.hooks[SY.P4] = p4; sim.hooks[SY.P5] = p5; sim.hooks[SY.APPLY] = apply   # the real page-2 stub runs
    wreads = []                                        # DLM view: wave ROM stand-ins
    def wsel(sm): sm.reg[0x1C] = 3; sm.c = False; return True
    def wdir(sm):
        for k, v in enumerate([40, 52, 64, 76, 88] + [0xFF] * 6): sm.ram[SY.WENT + 0x10 + k - 0xC000] = v
        for k in range(12): sm.ram[SY.WENT + 0x1B + k - 0xC000] = k
        return True
    def wsmp(sm):
        e = bytes([0x45, 0x23, 0x01 | 0x08, 0xFF, 0x0F, 0, 4, 0x40, 60, 0x40])   # start 0x12345 (+card bit)
        for k, v in enumerate(e): sm.wb(sm.rw(0x2A) + k, v)
        wreads.append(('smp', sm.reg[0x1C], sm.reg[0x1D])); return True
    def wbyte(sm):
        assert sm.bank == 3 and sm.reg[0x23] == 3, 'wave read: page 3 / select'
        ad = sm.rw(0x20) | (sm.reg[0x22] & 7) << 16
        wreads.append(ad); sm.reg[0x1C] = (0x08 if (ad >> 4) & 1 else 0xF8)
        ad += 1; sm.ww(0x20, ad & 0xFFFF); sm.reg[0x22] = (sm.reg[0x22] & 0xF8) | (ad >> 16 & 7); return True
    sim.hooks[SY.WSEL] = wsel; sim.hooks[SY.WDIR] = wdir; sim.hooks[SY.WSMP] = wsmp; sim.hooks[SY.WBYTE] = wbyte
    fixes, fx = [], []
    def tone_fix(sm):
        assert sm.bank == 2, 'tone refresh called without page 2'
        fixes.append(sm.reg[0x1D]); return True
    def fx_reload(sm):
        assert sm.bank == 3, 'effects reload called without page 3'
        fx.append(len(writes)); return True
    sim.hooks[SY.TONE_FIX] = tone_fix; sim.hooks[0xB165] = fx_reload
    def stockfn(n):
        def f(sm):
            assert sm.bank == 2, n + ' called without ROM page 2 in the window'
            calls.append(n); return True
        return f
    for fn, nm in ((SY.STOCK_UI, 'ui'), (SY.SCR_INIT, 'init'), (SY.SCR_DRAW, 'scr'), (SY.SCR_LEDS, 'leds'),
                   (SY.SCR_OFF, 'off'), (SY.SCR_ENTER, 'enter')):
        sim.hooks[fn] = stockfn(nm)
    sls = []
    def sl_loop(sm):
        assert sm.bank == 2, 'slider loop called without page 2'
        sls.append(sm.ram[SY.SLD_LAST - 0xC000]); return True
    sim.hooks[SY.SL_LOOP] = sl_loop
    shots = []
    def step(key=None, user=False, shot=True):
        sim.ram[SY.KEYMAP_USER[0] - 0xC000] = SY.KEYMAP_USER[1] if user else 0
        if key is not None: keyq.append(key)
        try:
            import time; t0 = time.time()
            n_ = sim.run(ui, limit=80_000_000)
            if n_ > 1_500_000: print('slow step', len(shots), n_, '%.0fs' % (time.time() - t0))
        except Exception as e:
            stk = [sim.rw((sim.rw(0x18) + 2 * k) & 0xFFFF) for k in range(8)]
            lab = lambda v: max(((a_, k_) for k_, a_ in p7.labels.items() if a_ <= v), default=(0, '?'))[1]
            print('stack', ['%04X %s' % (v, lab(v)) for v in stk], 'Q=%04X VAL=%d ITYPE=%d IAUX=%d' % (sim.rw(0x24), sim.ram[SY.VAL - 0xC000], sim.ram[SY.ITYPE - 0xC000], sim.ram[SY.IAUX - 0xC000]))
            raise RuntimeError('synth step %d key %s: %s at %04X %s (page 7 label near: %s) PG=%d SEL=%d COL=%d' % (len(shots), key, e, sim.last[0], sim.last[1], max(((v, k) for k, v in p7.labels.items() if v <= sim.last[0]), default=(0, '?'))[1], sim.ram[SY.PG - 0xC000], sim.ram[SY.SEL - 0xC000], sim.ram[SY.COL - 0xC000]))
        if shot: shots.append((bytes(sim.lcd.ram[SY.LCD_GFX:SY.LCD_GFX + 2560]),
                               bytes(sim.lcd.ram[SY.LCD_TEXT:SY.LCD_TEXT + 320])))
    step(shot=False); assert calls == ['ui'], 'closed menu must run the stock UI'
    step(user=True); step(SY.K_USER)                                   # open; the USER key event is ignored
    assert sim.lcd.homes.get(0x40) == SY.LCD_TEXT and sim.lcd.homes.get(0x42) == SY.LCD_GFX
    step(SY.K_ENT)                                                     # ROSETTA OS > SYNTH
    step(SY.K_DN); step(SY.K_ENT)                                      # WAVE / ZONE
    m0, n0_ = mem[0x0351 + 0x0A], mem[0x0351 + 0x0B]
    step(SY.K_DN); step(SY.K_INC)                                      # WAVE: next wave, tone record rebuilt
    assert writes[-2:] == [(0x0351 + 0x0A, m0), (0x0351 + 0x0B, n0_ + 1)] and fixes[-1] == 0, (writes[-2:], fixes)
    assert applies[-1] == len(writes)
    mem[0x0351 + 0x0A] = 0x40; mem[0x0351 + 0x0B] = 46; step(SY.K_INC)  # last wave of I1 -> I2 1
    assert writes[-2:] == [(0x0351 + 0x0A, 0x41), (0x0351 + 0x0B, 0)], writes[-2:]
    step(SY.K_DEC)                                                     # and back to I1 47
    assert writes[-2:] == [(0x0351 + 0x0A, 0x40), (0x0351 + 0x0B, 46)], writes[-2:]
    step(SY.K_RT); step(SY.K_DN); step(SY.K_DEC)                       # ZONE column: SHIFT - 1
    assert writes[-1] == (0x019E + 0x23 + 0x02, 23), (writes[-3:], [hex(sim.ram[k - 0xC000]) for k in (SY.PG, SY.SEL, SY.COL, SY.SEC)])
    step(SY.K_F5); step(SY.K_F5); step(SY.K_F5); step(SY.K_F5)         # sections L2 U3 U4 ALL
    n0 = len(writes); step(SY.K_INC)                                   # ALL: one INC writes four sections
    assert [w[0] for w in writes[n0:]] == [0x019E + 0x23 + k * 0x0D + 0x02 for k in range(4)]
    step(SY.K_EXIT)
    for _ in range(5): step(SY.K_DN, shot=False)                       # WAVE -> down to AMP ENV
    step(SY.K_DN); step(SY.K_ENT); step(SY.K_DN); step(SY.K_INC)       # AMP ENV, A LEVEL + 1
    step(SY.K_EXIT); step(SY.K_UP); step(SY.K_ENT)                     # AMP
    step(SY.K_EXIT); step(SY.K_UP); step(SY.K_UP); step(SY.K_ENT)      # FILTER
    step(SY.K_EXIT); step(SY.K_UP); step(SY.K_ENT)                     # PITCH
    def view_check():                                  # the DLM view read exactly the loop slice
        sec = 0; t0 = 0x0351 + sec * 0x3E
        mode, start, ln, num = mem.get(t0 + 0x0D, 0), mem.get(t0 + 0x0E, 0), mem.get(t0 + 0x0F, 0), mem.get(t0 + 0x0B, 0)
        n = (ln + 1) * 4; base = (0x1000 + num * 0x50 + start) * 4 if mode == 0 else (0x12345 & ~3) + start * 4
        rd = [x for x in wreads if not isinstance(x, tuple)]
        k = (n + 118) // 119
        assert len(rd) == 119 * k, (len(rd), k)
        assert rd[:min(n, len(rd))] == [base + i for i in range(min(n, len(rd)))], (hex(base), [hex(x) for x in rd[:4]])
        if mode: assert ('smp', 3, 2) in wreads           # the sample under middle C (zone 2)
        wreads.clear()
    step(SY.K_EXIT); step(SY.K_UP); wreads.clear(); step(SY.K_ENT); view_check()   # DLM, its view
    mem[0x0351 + 0x0D] = 1; mem[0x0351 + 0x0E] = 5; mem[0x0351 + 0x0F] = 9; step(SY.K_UP); view_check()   # MODE B
    step(SY.K_DN); step(SY.K_DN); n0 = len(writes); step(SY.K_INC)     # START + 1 (section ALL: four layers)
    assert [w[0] for w in writes[n0:]] == [0x0351 + k * 0x3E + 0x0E for k in range(4)], writes[n0:]
    s0 = writes[-1][1]
    n0 = len(writes); dialq.append(3); step(shot=False)               # Alpha Dial: 3 clicks = START + 3, once
    assert writes[n0:] == [(0x0351 + k * 0x3E + 0x0E, s0 + 3) for k in range(4)], writes[n0:]
    n0 = len(writes); dialq.append(-2); step(shot=False)              # 2 clicks back
    assert [w[1] for w in writes[n0:]] == [s0 + 1] * 4, writes[n0:]
    n0 = len(writes); sim.ram[SY.SLD - 0xC000] = 127; step(shot=False)  # slider 1 to the top: START = 127
    assert [w[1] for w in writes[n0:]] == [127] * 4 and sls[-1] == 127, (writes[n0:], sls[-3:])
    n0 = len(writes); sim.ram[SY.SLD - 0xC000] = 64; step(shot=False)   # half way: 64
    assert [w[1] for w in writes[n0:]] == [64] * 4, writes[n0:]
    n0 = len(writes); step(shot=False); assert writes[n0:] == []       # not moved: nothing
    step(SY.K_UP); n0 = len(writes); sim.ram[SY.SLD - 0xC000] = 0; step(shot=False)     # MODE (B -> A)
    assert [w[1] for w in writes[n0:]] == [0] * 4, writes[n0:]
    n0 = len(writes); sim.ram[SY.SLD - 0xC000] = 127; step(shot=False)  # and back to B
    assert [w[1] for w in writes[n0:]] == [1] * 4, writes[n0:]
    step(SY.K_DN)
    step(SY.K_EXIT)
    for _ in range(4): step(SY.K_DN, shot=False)                       # DLM -> EFFECTS: 4 keys ...
    dialq.append(3); step(shot=False)                                  # ... + 3 dial clicks
    step(SY.K_ENT); step(SY.K_RT); step(SY.K_DN); step(SY.K_INC)       # chorus LEVEL + 1
    assert writes[-1] == (0x00AD + 0xB3, 1) and fx and fx[-1] == len(writes), (writes[-4:], fx)
    step(SY.K_EXIT)
    for _ in range(9): step(SY.K_UP, shot=False)
    step(SY.K_UP); step(SY.K_ENT); step(SY.K_DN); step(SY.K_INC)       # LAYERS, L2 level + 1
    assert writes[-1] == (0x019E + 0x23 + 1 * 0x0D + 0x01, 73)
    step(SY.K_EXIT); step(SY.K_EXIT)                                   # back to SYNTH, ROSETTA OS
    step(SY.K_DN); step(SY.K_DN); step(SY.K_ENT); step(SY.K_EXIT)      # SETTINGS
    step(SY.K_DN); step(SY.K_ENT)                                      # ABOUT
    step(SY.K_EXIT); step(SY.K_UP); step(SY.K_UP); step(SY.K_ENT)      # TOOLS
    sim.reg[0x9C] = 0x00; sim.reg[0x9D] = 0x14; sim.reg[0xA0] = 0; sim.reg[0xA1] = 0; sim.reg[0xA2] = 0x0C
    sim.tx.clear(); step(SY.K_ENT, shot=False)                           # DUMP IC6
    from ic6_decode import decode
    dimg, nblk = decode(bytes(sim.tx)); assert nblk == 256 and dimg == sim.ic6, 'runtime IC6 dump mismatch'
    step(SY.K_EXIT); step(SY.K_DN); step(SY.K_DN); step(SY.K_DN); n = len(calls); step(SY.K_ENT)   # EXIT TO D-70
    assert sim.ram[SY.M_ON - 0xC000] == 0 and calls[n:] == []
    assert sim.lcd.homes[0x40] == 0x1400 and sim.lcd.homes[0x42] == 0 and sim.lcd.mode == 0x9C
    step(shot=False); assert calls[-1] == 'ui'
    sim.ram[SY.M_ON - 0xC000] = 2; n = len(calls); step(shot=False)     # boot open: waits, stock UI runs
    assert calls[n:] == ['ui'] and sim.ram[SY.M_ON - 0xC000] == 2
    sim.ram[SY.BOOTN - 0xC000] = 23; sim.ram[SY.BOOTHI - 0xC000] = 0xFF; sim.t1 = 0; step()     # last wrap
    assert sim.ram[SY.M_ON - 0xC000] == 1 and sim.lcd.homes[0x40] == SY.LCD_TEXT
    assert sim.bank == 0x02
    sim.ram[SY.SEC - 0xC000] = 0
    step(SY.K_MIDB); assert sim.ram[SY.PG - 0xC000] == SY.PIDX['midi']        # MIDI OUT key: our page
    nfx = len(fx); step(SY.K_RT); step(SY.K_INC)                       # SECTION column: MIDI on (L1)
    assert writes[-1] == (SY.MOUT_BASE + 6, 1), writes[-2:]
    step(SY.K_DN); step(SY.K_INC); assert writes[-1] == (SY.MOUT_BASE + 0, 1), writes[-2:]   # CHANNEL 2
    step(SY.K_F5); step(SY.K_INC); assert writes[-1] == (SY.MOUT_BASE + 7, 1), writes[-2:]   # L2
    step(SY.K_LT); step(SY.K_INC); assert writes[-1] == (0x00AD + 0xBB, 1), writes[-2:]     # SPLIT
    assert len(fx) == nfx, 'MIDI OUT edits must not reload the effects chip'
    step(SY.K_FXB); assert sim.ram[SY.PG - 0xC000] == SY.PIDX['fx']           # EFFECT/CTRL key
    sim.ram[SY.M_ON - 0xC000] = 0; sim.ram[SY.UPREV - 0xC000] = 0; step(shot=False)   # closed, stock runs
    sim.ram[SY.KEYMAP_FX[0] - 0xC000] = SY.KEYMAP_FX[1]; step(shot=False)    # EFFECT/CTRL with the menu closed
    assert sim.ram[SY.M_ON - 0xC000] == 1 and sim.ram[SY.PG - 0xC000] == SY.PIDX['fx']
    sim.ram[SY.KEYMAP_FX[0] - 0xC000] = 0
    for r56, pg in ((0x37, 'synth'), (0x2F, 'fx'), (0x42, 'tools'), (0x1D, 'midi')):   # stock enters a cut screen
        sim.ram[SY.M_ON - 0xC000] = 0; sim.reg[SY.R5A_] = 0; sim.reg[SY.R56_] = r56; n = len(calls); step(shot=False)
        assert sim.reg[SY.R56_] == 1 and calls[n:] == ['off', 'enter'], (hex(r56), calls[n:])
        assert sim.ram[SY.M_ON - 0xC000] == 1 and sim.ram[SY.PG - 0xC000] == SY.PIDX[pg], hex(r56)
    sim.ram[SY.M_ON - 0xC000] = 0; sim.reg[SY.R56_] = 0x36; sim.reg[SY.R5A_] = 3; n = len(calls); step(shot=False)
    assert calls[n:] == ['ui'] and sim.ram[SY.M_ON - 0xC000] == 0, 'edit modes are left alone'
    sim.reg[SY.R56_] = 1; sim.reg[SY.R5A_] = 0                          # KNOB LOCK: menu closed, slider 1 still
    assert sim.ram[SY.L_VALID - 0xC000] == 1                           # drives the last item it took (DLM MODE)
    n0 = len(writes); sim.ram[SY.SLD - 0xC000] = 0; step(shot=False)
    assert [w for w in writes[n0:]] == [(0x0351 + k * 0x3E + 0x0D, 0) for k in range(4)], writes[n0:]
    assert sim.ram[SY.SLD_LAST - 0xC000] == 0 and calls[-1] == 'ui'
    sim.ram[SY.KLOCK - 0xC000] = 1; n0 = len(writes); sim.ram[SY.SLD - 0xC000] = 127; step(shot=False)
    assert writes[n0:] == [], 'KNOB LOCK OFF: slider 1 is the stock slider again'
    sim.ram[SY.KLOCK - 0xC000] = 0
    out, last = [], None
    for sh in shots:
        if sh != last: out.append(sh); last = sh
    return out


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
        for y, row in enumerate(font57.glyph(chr(code) if code >= 0x20 else ' ')):
            for k, on in enumerate(row):
                if on: tx[r * 8 + y, c * 6 + k] = True
    return unpack(g) ^ tx


def save_tour(tour, path):
    S, cols = 3, 2
    rows = (len(tour) + cols - 1) // cols
    sheet = Image.new('RGB', (cols * (240 * S + 12) + 12, rows * (64 * S + 12) + 12), (30, 30, 30))
    for i, (g, t) in enumerate(tour):
        px = render(g, t)
        im = Image.fromarray(np.where(px[..., None], np.array([20, 30, 60], np.uint8), np.array([150, 200, 235], np.uint8)))
        im = im.resize((240 * S, 64 * S), Image.NEAREST)
        r, c = divmod(i, cols); sheet.paste(im, (12 + c * (240 * S + 12), 12 + r * (64 * S + 12)))
    sheet.save(path)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Build Rosetta OS for the Roland D-70 from your own v1.11 dump.')
    ap.add_argument('even', help='IC4 (even) dump, 64 KB')
    ap.add_argument('odd', help='IC9 (odd) dump, 64 KB')
    ap.add_argument('--out', help='output folder (default: build/)')
    ap.add_argument('--frames', help='boot frames .npz (default: frames/roland_chan_frames.npz)')
    a = ap.parse_args()
    configure(a.even, a.odd, a.out, a.frames)
    try:
        main()
    except ValueError as ex:
        sys.exit(str(ex))
