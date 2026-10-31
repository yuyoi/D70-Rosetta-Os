"""Offline check of the FX2 engine (fxfw2.py) on the 80C196 sim: the chip starts with the real hw program
(dsp_lab/hw_dump_v08_after_scan.txt); the settings RAM is set like the menu would, fx_poll runs, and every word and
cell must equal what tools/fx2.py evaluates (the same log maths, checked on the RCC model) from the words the chip
had before that poll. Also: CHORUS types and bad DOUBLE pairs write nothing, unchanged settings write nothing, a stock
reload (effect edit) is seen and the chain goes back on, a new chain zeroes the temp cells, fx_tick says "no byte".
  py -3.10 test_fxfw2.py [build/D70_rosetta_v010_combined.bin]"""
import sys
import fxfw2 as FX
from fxfw2 import fx2
from test_dsplab import LabSim


def main():
    segs, labels, rep, tb = FX.build()
    if len(sys.argv) < 2:                                          # v0.8 image: drop the engine in
        img = bytearray(open('build/D70_rosetta_v08_combined.bin', 'rb').read())
        for org, blob in segs:
            f = org if org < 0x8000 else 4 * 0x4000 + org - 0x8000
            img[f:f + len(blob)] = blob
    else:
        img = bytearray(open(sys.argv[1], 'rb').read())
        for org, blob in segs:
            f = org if org < 0x8000 else 4 * 0x4000 + org - 0x8000
            assert img[f:f + len(blob)] == blob, 'engine in the image differs at %04X' % org
    st, _ = fx2.stock()
    s = LabSim(bytes(img)); s.word = dict(st); ok = True
    for c in range(32): s.cell[c] = 0x5A5A5A
    def setr(a, v): s.ram[a - 0xC000] = v
    def ram(a): return s.ram[a - 0xC000]
    for a in range(FX.FX_APP, 0xC700): setr(a, 0)
    for a in range(FX.N_SET): setr(FX.RAM_BASE + FX.S_SEL + a, 0)

    def check(name, cond, extra=''):
        nonlocal ok; ok &= bool(cond); print('%-62s %s%s' % (name, 'OK' if cond else 'FAIL', extra))

    def poll():
        s.rcc = []; s.bank = 4; s.run(labels['fx_poll'])
        return sum(1 for o, v in s.rcc if o in (4, 6))               # chip writes

    def single(name, vals, mix):
        setr(FX.RAM_BASE + FX.S_SEL, 0); setr(FX.RAM_BASE + FX.S_TYPE, FX.N_CHO + FX.SINGLE_NAMES.index(name))
        flat = [v for x in vals for v in x]
        for i, v in enumerate(flat): setr(FX.RAM_BASE + FX.S_P + i, v)
        setr(FX.RAM_BASE + FX.S_MIX, mix)

    def double(an, bn, va, vb, mix):
        setr(FX.RAM_BASE + FX.S_SEL, 1); setr(FX.RAM_BASE + FX.D_A, FX.DOUBLE_A.index(an))
        setr(FX.RAM_BASE + FX.D_B, FX.DOUBLE_B.index(bn))
        for i, v in enumerate(va): setr(FX.RAM_BASE + FX.D_AP + i, v)
        for i, v in enumerate(vb): setr(FX.RAM_BASE + FX.D_BP + i, v)
        setr(FX.RAM_BASE + FX.D_MIX, mix)

    def same(ch, vals, mix, before, new_chain):
        words, cells = fx2.evaluate(ch, vals, min(mix, fx2.MIXN - 1), before)
        bad = ['W%02X %06X want %06X' % (w, s.word[w], v) for w, v in words.items() if s.word[w] != v]
        bad += ['W%02X changed' % w for w in st if w not in words and s.word[w] != before[w]]
        bad += ['c%d %06X want %06X' % (c, s.cell.get(c), v) for c, v in cells.items() if s.cell.get(c) != v]
        if new_chain: bad += ['c%d not 0' % c for c in FX.TEMP_CELLS if c not in cells and s.cell.get(c) != 0]
        return bad

    def go(label, ch, vals, mix, new_chain):
        before = dict(s.word)
        poll()
        bad = same(ch, vals, mix, before, new_chain)
        check(label, not bad, '' if not bad else '  ' + ' | '.join(bad[:4]))

    setr(FX.RAM_BASE + FX.S_TYPE, 2); n = poll()
    check('SINGLE CHORUS 3: no chip writes, nothing applied', n == 0 and ram(FX.FX_APP) == 0)
    chains = fx2.all_chains()
    for name in FX.SINGLE_NAMES:
        ch = chains['S:' + name]
        recs = ch['recs'][1:]
        lo = [[0] * len(c.params) for c in recs]
        hi = [[p[1] - 1 for p in c.params] for c in recs]
        mid = fx2.defaults(ch)
        first = True
        for vals, mix in ((mid, 15), (lo, 0), (hi, 9), (mid, 1)):
            single(name, vals, mix)
            go('SINGLE %-8s %-16s MIX %2d' % (name, vals, mix), ch, vals, mix, first); first = False
        n = poll(); check('SINGLE %-8s same settings again: no writes' % name, n == 0)
    for an in FX.DOUBLE_A:
        for bn in FX.DOUBLE_B:
            if not fx2.pair_ok(an, bn):
                double(an, bn, [3], [3, 3], 8); n = poll()
                check('DOUBLE %s > %s (not allowed): nothing applied' % (an, bn), n == 0 and ram(FX.FX_APP) == 0)
                continue
            ch = chains['D:%s>%s' % (an, bn)]
            recs = ch['recs'][1:]
            for k, mix in ((0, 15), (1, 4)):
                vals = [[(p[1] - 1) if k else p[2] for p in c.params] for c in recs]
                double(an, bn, vals[0], vals[1], mix)
                go('DOUBLE %s > %-8s %-14s MIX %2d' % (an, bn, vals, mix), ch, vals, mix, k == 0)
    ch = chains['S:LPF']; single('LPF', [[], [10, 5]], 12); poll()
    before = dict(st); s.word = dict(st)                              # stock reloads the chip (effect page edit)
    poll(); bad = same(ch, [[], [10, 5]], 12, before, True)
    check('stock reload seen, LPF back on', not bad, ' '.join(bad[:3]))
    single('LPF', [[], [200, 5]], 12); before = dict(s.word); poll()
    check('CUTOFF out of range clamps to the top', not same(ch, [[], [fx2.CUT_N - 1, 5]], 12, before, False))
    single('LPF', [[], [10, 5]], 99); before = dict(s.word); poll()
    check('MIX out of range = full wet', not same(ch, [[], [10, 5]], 15, before, False))
    setr(FX.RAM_BASE + FX.S_TYPE, 0); n = poll()
    check('back to CHORUS 1: no writes, not applied', n == 0 and ram(FX.FX_APP) == 0)
    setr(FX.RAM_BASE + FX.S_SEL, 7); n = poll(); check('SEL out of range = off', n == 0)
    n = 0
    for _ in range(600):                                            # fx_tick: one poll every 256 empty reads
        s.bank = 2; s.c = True; s.run(labels['fx_tick'])
        n += not s.c
    check('fx_tick returns "no byte" every time', n == 600)
    print(rep)
    print('ALL OK' if ok else 'FAILED'); sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
