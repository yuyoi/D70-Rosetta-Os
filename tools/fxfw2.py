"""FX2 in the firmware (v0.10): the SINGLE / DOUBLE effects of tools/fx2.py, run by the D-70 itself.

Build time: tools/fx2.py's recipes are compiled to a small program each (ops on 16-bit log2 registers: a product is a
sum) plus a field list (word fields to set: constants, gains from a register, the two output biases; cells: constants
or a table over a param). Run time (fx_poll, page 4, from fx_tick like v0.9):
  - settings (RAM C630-C63A) say which chain: SINGLE type (box 1 recipe, box 2 recipe or a full one) or DOUBLE A / B
  - same chain, same settings, word 19 still ours: nothing to do
  - else: registers from MIX, programs (box 2 pre, box 1, box 2), then the common list, box 1's list, box 2's list;
    a new chain zeroes the temp cells first
  - SINGLE type CHORUS 1-5 = box off: nothing is put back here, the menu's TYPE write reloads the effect program
    (stock FX_RELOAD rewrites every word; the chorus cells come back from stock's own steps and live writes)
Gain fields come from a log by a 32-step 2^x table (fx_enc); tools/fx2.py does the same sums, so test_fxfw2.py
compares the sim's chip words with fx2.evaluate bit for bit.
"""
import os
import sys
from collections import defaultdict
from asm import Asm

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(HERE, '..', '..', 'tools'))      # last: fw dsplab.py must win over tools/dsplab.py
import fx2

RAM_BASE = 0xC600
S_SEL, S_TYPE, S_P, S_MIX = 0x30, 0x31, 0x32, 0x34            # SEL 0 SINGLE / 1 DOUBLE; SINGLE type, 2 params, MIX
D_A, D_AP, D_B, D_BP, D_MIX = 0x35, 0x36, 0x37, 0x38, 0x3A    # DOUBLE box 1 type + param, box 2 type + 2 params, MIX
N_SET = 11                                                     # C630-C63A
N_CHO = 5                                                      # SINGLE types 0-4 = stock CHORUS 1-5 (box off)
FX_APP, FX_APB, FX_CNT, FX_FULL, FX_SNAP = 0xC6C0, 0xC6C1, 0xC6C2, 0xC6C3, 0xC6C4   # snapshot C6C4-C6CE
REGS = 0xC6D0                                                  # 24 log registers, C6D0-C6FF
N_REGS = 24
TICK_ORG, TICK_END = 0x23E0, 0x23FF               # fixed ROM after the player (it ends ~0x23D2)
P4_BLOCKS = [(0x8B0D, 0x8F2F), (0x9869, 0x9AE8), (0x82A5, 0x8502), (0x9B49, 0x9C33), (0xBE54, 0xBFFF),
             (0x9700, 0x97AE),
             (0x9640, 0x96FF)]                    # the top of the arp / menu page-4 block (arp.IN_END = 0x963F)
STUB4 = 0x1056
RCC = 0x0A00
OPS = {'CUR': 1, 'ADD': 2, 'SUB': 3, 'MINK': 4, 'MAXK': 5, 'ADDK': 6}
TEMP_CELLS = sorted(set(fx2.SCRATCH + fx2.KEEP) - {fx2.PARK_CELL})

SINGLE_NAMES = list(fx2.SINGLE)                                # menu order after CHORUS 1-5
DOUBLE_A, DOUBLE_B = list(fx2.DOUBLE_A), list(fx2.DOUBLE_B)


# ---------------- build-time data ----------------
def recipes():
    """-> ordered {name: Compiled}: box 1, box 2, full recipes"""
    out = {}
    for n, f in fx2.A_MODES.items(): out['A_' + n] = fx2.compile_recipe(f(), {'IN': fx2.AO})
    for n, f in fx2.B_MODES.items():
        r = f()
        sig = r.out if not isinstance(r.out, str) else fx2.AO
        out['B_' + n] = fx2.compile_recipe(r, {'IN': fx2.AO}, fx2.out_fields(r, sig))
    for n, f in fx2.FULL_MODES.items():
        r = f(); out[n] = fx2.compile_recipe(r, {}, fx2.out_fields(r, r.out))
    return out


def regmap(recs):
    """log registers: 0-5 runtime, box 1 recipes from 6, box 2 recipes after the biggest box 1, full from 6"""
    def names(c):
        s = []
        for op in c.pre + c.prog:
            for x in (op[1],) + tuple(y for y in op[2:] if isinstance(y, str)):
                if x not in fx2.RT and x not in s: s.append(x)
        for v in c.fields.values():
            if isinstance(v, tuple) and v[0] == 'G' and v[1] not in fx2.RT and v[1] not in s: s.append(v[1])
        return s
    m = {n: i for i, n in enumerate(fx2.RT)}
    top_a = 6
    for k, c in recs.items():
        if k.startswith('A_'):
            for i, n in enumerate(names(c)): m[n] = 6 + i
            top_a = max(top_a, 6 + len(names(c)))
    for k, c in recs.items():
        base = top_a if k.startswith('B_') else 6 if not k.startswith('A_') else None
        if base is None: continue
        for i, n in enumerate(names(c)): m[n] = base + i
    assert max(m.values()) < N_REGS, 'too many log registers: %d' % (max(m.values()) + 1)
    return m


def s16(v):
    assert -0x8000 <= v < 0x8000, v
    return [v & 255, v >> 8 & 255]


class Data:
    """byte blobs (name -> bytes, word tables kept even) + the references between them"""
    def __init__(s):
        s.blobs, s.refs = {}, {}            # refs: name -> [(offset, target name)]

    def add(s, prefix, bs, refs=()):
        for k, v in s.blobs.items():        # identical blobs are shared
            if v == bytes(bs) and not refs and not s.refs.get(k): return k
        n = '%s%d' % (prefix, len(s.blobs))
        s.blobs[n] = bytes(bs); s.refs[n] = list(refs)
        return n


def enc_prog(d, ops, rm):
    bs, refs = [], []
    for op in ops:
        o = OPS[op[0]]
        if op[0] == 'CUR':
            t = d.add('fx_cv', s16(len(op[3])) + sum((s16(v) for v in op[3]), []))
            bs += [o, rm[op[1]] * 2, op[2]]; refs.append((len(bs), t)); bs += [0, 0]
        elif op[0] in ('ADD', 'SUB'):
            bs += [o, rm[op[1]] * 2, rm[op[2]] * 2, rm[op[3]] * 2]
        else:
            bs += [o, rm[op[1]] * 2, rm[op[2]] * 2] + s16(op[3])
    bs.append(0)
    return d.add('fx_pg', bs, refs)


def enc_list(d, c, rm):
    """n words: addr, K, mid, lo [, G] [, bias]; n cells: cell, hi, mid, lo | cell + 80, table, param"""
    by = defaultdict(dict)
    for (w, kind), v in c.fields.items(): by[w][kind] = v
    bs, refs = [len(by)], []
    for w in sorted(by):
        f = by[w]; k, val, extra = 0, 0, []
        g = f.get('g')
        if g is not None:
            if g[0] == 'K': k |= 4; val |= g[1]
            else:
                k |= 32; extra.append(rm[g[1]] * 2 | g[2] << 6 | g[3] << 7)
        cl = f.get('c')
        if cl is not None:
            if isinstance(cl, str):
                k |= 64; extra.append({'BIASL': 0, 'BIASR': 1}[cl])
            else: k |= 8; val |= cl << 9
        if f.get('n') is not None: k |= 16; val |= f['n'] << 14
        assert not (k & 4 and k & 32) and not (k & 8 and k & 64)
        bs += [w, k | val >> 16, val >> 8 & 255, val & 255] + extra          # gain byte first, then the bias
    bs.append(len(c.cells))
    for cell, v in sorted(c.cells.items()):
        if isinstance(v, tuple):
            t = d.add('fx_ct', [len(v[2])] + list(v[2]))
            bs.append(cell | 0x80); refs.append((len(bs), t)); bs += [0, 0, v[1]]
        else:
            bs += [cell, v >> 16 & 255, v >> 8 & 255, v & 255]
    return d.add('fx_ls', bs, refs)


def tables():
    recs = recipes()
    rm = regmap(recs)
    d = Data()
    ids = {n: i for i, n in enumerate(recs)}
    desc = []
    for n, c in recs.items():
        desc.append((enc_prog(d, c.prog, rm), enc_prog(d, c.pre, rm) if c.pre else None, enc_list(d, c, rm)))
    common = enc_list(d, fx2.common(), rm)
    stab = []
    for name in SINGLE_NAMES:
        sp = fx2.SINGLE[name]
        if isinstance(sp, str): stab += [ids[sp], 0xFF, S_P, S_P]
        else:
            na = len(recs['A_' + sp[0]].params)
            stab += [ids['A_' + sp[0]], ids['B_' + sp[1]], S_P, S_P + na]
    da = [ids['A_' + n] for n in DOUBLE_A]
    db = [ids['B_' + n] for n in DOUBLE_B]
    dbok = [int(fx2.pair_ok(a, b)) for b in DOUBLE_B for a in DOUBLE_A]
    wet = [fx2.mixlogs(m)['wet'] for m in range(fx2.MIXN)]
    return dict(recs=recs, rm=rm, d=d, desc=desc, common=common, stab=stab, da=da, db=db, dbok=dbok, wet=wet)


# ---------------- code ----------------
T, U, A, N = 0x1C, 0x1D, 0x1E, 0x1F                # bytes
P, Q = 0x20, 0x22                                  # list / table pointers
WH, WM, WL, G = 0x24, 0x25, 0x26, 0x27             # word read, gain flags
X, Y, IDX, Z = 0x28, 0x2A, 0x2C, 0x2E              # value, param base, index, scratch (words)
MH, MM, K2, MXO = 0x30, 0x31, 0x32, 0x33
RA, RB, PA, PB = 0x34, 0x35, 0x36, 0x37
SAVED = (0x1C, 0x1E, 0x20, 0x22, 0x24, 0x26, 0x28, 0x2A, 0x2C, 0x2E, 0x30, 0x32, 0x34, 0x36)


def emit_tick(a):
    """fixed ROM: lab_get's 'no byte' exit. R1A / R1B are free there."""
    a.L('fx_tick')
    a.ldb_abs(0x1B, FX_CNT); a.incb(0x1B); a.stb_abs(FX_CNT, 0x1B); a.jcc('JNE', 'ft_x')
    a.ldi_l(0x1A, 'fx_poll'); a.lcall(STUB4)
    a.L('ft_x'); a.b(0xF8); a.ret()                    # CLRC: no byte


def emit_engine(a, nsingle):
    J = a.jcc
    lda, sta = a.ldb_abs, a.stb_abs
    a.L('fx_poll')
    for r in SAVED: a.push(r)
    lda(T, RAM_BASE + S_SEL); a.cmpbi(T, 0); J('JNE', 'fp_d')
    lda(U, RAM_BASE + S_TYPE); a.subbi(U, N_CHO); a.cmpbi(U, nsingle); J('JC', 'fp_off')   # CHORUS 1-5: off
    a.ldbze(IDX, U); a.shl(IDX, 2)
    for i, r in enumerate((RA, RB, PA, PB)): a.ldb_lidx_l(r, IDX, 'fx_stab', i)
    a.ldbi(MXO, S_MIX); a.sjmp('fp_on')
    a.L('fp_d'); a.cmpbi(T, 1); J('JNE', 'fp_off')
    lda(U, RAM_BASE + D_A); a.cmpbi(U, len(DOUBLE_A)); J('JC', 'fp_off')
    lda(A, RAM_BASE + D_B); a.cmpbi(A, len(DOUBLE_B)); J('JC', 'fp_off')
    a.ldbze(IDX, A); a.shl(IDX, 1); a.ldbze(Z, U); a.add(IDX, Z)
    a.ldb_lidx_l(T, IDX, 'fx_dbok'); a.cmpbi(T, 0); J('JE', 'fp_off')
    a.ldbze(IDX, U); a.ldb_lidx_l(RA, IDX, 'fx_da'); a.ldbze(IDX, A); a.ldb_lidx_l(RB, IDX, 'fx_db')
    a.ldbi(PA, D_AP); a.ldbi(PB, D_BP); a.ldbi(MXO, D_MIX)
    a.L('fp_on')
    lda(T, FX_APP); a.cmpbi(T, 0); J('JE', 'fp_new')
    a.ldbi(A, 0x19); a.lcall('fx_rd'); a.shrb(WM, 1); a.andbi(WM, 31); a.cmpbi(WM, 24); J('JE', 'fp_new')   # reloaded
    lda(T, FX_APP); a.ldb(U, RA); a.incb(U); a.cmpb(T, U); J('JNE', 'fp_new')
    lda(T, FX_APB); a.cmpb(T, RB); J('JNE', 'fp_new')
    a.clr(IDX)
    a.L('fp_cmp'); a.ldb_lidx(T, IDX, RAM_BASE + S_SEL); a.ldb_lidx(U, IDX, FX_SNAP); a.cmpb(T, U); J('JNE', 'fp_set')
    a.inc(IDX); a.cmpi(IDX, N_SET); J('JNE', 'fp_cmp')
    a.sjmp('fp_x')
    a.L('fp_new')                                      # new chain (or stock reloaded): temp cells to 0
    a.ldi_l(P, 'fx_temps'); a.ldb_pi(N, P); a.clrb(WH); a.clrb(WM); a.clrb(WL)
    a.L('fp_z'); a.ldb_pi(A, P); a.lcall('fx_wc'); a.decb(N); J('JNE', 'fp_z')
    a.L('fp_set'); a.clr(IDX)
    a.L('fp_cp'); a.ldb_lidx(T, IDX, RAM_BASE + S_SEL); a.stb_lidx(IDX, FX_SNAP, T)
    a.inc(IDX); a.cmpi(IDX, N_SET); J('JNE', 'fp_cp')
    a.ldb(T, RA); a.incb(T); sta(FX_APP, T); sta(FX_APB, RB)
    a.lcall('fx_apply'); a.sjmp('fp_x')
    a.L('fp_off'); a.clrb(T); sta(FX_APP, T)
    a.L('fp_x')
    for r in reversed(SAVED): a.pop(r)
    a.ret()

    a.L('fx_apply')
    a.ldbze(Z, MXO); a.ldb_lidx(T, Z, RAM_BASE); a.cmpbi(T, fx2.MIXN - 1); J('JC', 'ap_full')
    a.clrb(U); a.sjmp('ap_m')
    a.L('ap_full'); a.ldbi(T, fx2.MIXN - 1); a.ldbi(U, 1)
    a.L('ap_m'); sta(FX_FULL, U)
    a.ldbze(IDX, T); a.add(IDX, IDX); a.ld_lidx_l(X, IDX, 'fx_wet'); a.clr(Z); a.st_lidx(Z, REGS, X)   # wet
    a.clr(X); a.cmpbi(U, 0); J('JNE', 'ap_gc')
    a.ldbi(A, fx2.MIXN - 1); a.subb(A, T); a.ldbze(IDX, A); a.add(IDX, IDX); a.ld_lidx_l(X, IDX, 'fx_wet')
    a.L('ap_gc'); a.st_lidx(Z, REGS + 2, X)                                                       # gc
    a.clr(Q); a.sub(Q, X); a.st_lidx(Z, REGS + 4, Q)                                               # igc
    for o in (6, 8, 10): a.st_lidx(Z, REGS + o, Z)                                                # isa apre one
    a.cmpbi(RB, 0xFF); J('JE', 'ap_a')
    a.ldb(T, RB); a.ldb(U, PB); a.ldbi(A, 2); a.lcall('ap_run')                                   # box 2 pre
    a.L('ap_a'); a.ldb(T, RA); a.ldb(U, PA); a.ldbi(A, 0); a.lcall('ap_run')
    a.cmpbi(RB, 0xFF); J('JE', 'ap_l')
    a.ldb(T, RB); a.ldb(U, PB); a.ldbi(A, 0); a.lcall('ap_run')
    a.L('ap_l'); a.ldi_l(P, 'fx_common'); a.lcall('fx_list')
    a.ldb(T, RA); a.ldb(U, PA); a.ldbi(A, 4); a.lcall('ap_run')
    a.cmpbi(RB, 0xFF); J('JE', 'ap_x')
    a.ldb(T, RB); a.ldb(U, PB); a.ldbi(A, 4); a.lcall('ap_run')
    a.L('ap_x'); a.ret()

    a.L('ap_run')                                      # recipe T, params at C600 + U, A = 0 prog / 2 pre / 4 list
    a.ldbze(IDX, T); a.ld(Z, IDX); a.add(IDX, IDX); a.add(IDX, Z); a.add(IDX, IDX)    # x 6
    a.ldbze(Z, A); a.add(IDX, Z); a.ld_lidx_l(P, IDX, 'fx_desc')
    a.ldbze(Y, U); a.addi(Y, RAM_BASE)
    a.cmpi(P, 0); J('JE', 'ar_x')
    a.cmpbi(A, 4); J('JNE', 'vm_l'); a.ljmp('fx_list')
    # ---- the program: ops on log registers ----
    a.L('vm_l'); a.ldb_pi(T, P); a.cmpbi(T, 0); J('JE', 'ar_x')
    a.ldb_pi(U, P)                                     # destination register x 2
    a.cmpbi(T, OPS['CUR']); J('JNE', 'vm_2')
    a.ldb_pi(A, P); a.ldb_pi(Q, P); a.ldb_pi(Q + 1, P)
    a.ldbze(Z, A); a.add(Z, Y); a.ldbze_ind(Z, Z)      # Z = param value
    a.ld_pi(X, Q); a.cmp(Z, X); J('JNC', 'vm_cin')      # table length: clamp to the last value
    a.ld(Z, X); a.dec(Z)
    a.L('vm_cin'); a.add(Z, Z); a.add(Q, Z); a.b(0xA2, Q, X); a.sjmp('vm_st')    # LD X,[Q]
    a.L('vm_2'); a.ldb_pi(A, P); a.ldbze(Z, A); a.ld_lidx(X, Z, REGS)
    a.cmpbi(T, OPS['MINK']); J('JNC', 'vm_rr')               # ADD / SUB: a second register
    a.ldb_pi(Z, P); a.ldb_pi(Z + 1, P); a.sjmp('vm_op')
    a.L('vm_rr'); a.ldb_pi(A, P); a.ldbze(Z, A); a.ld_lidx(Z, Z, REGS)
    a.L('vm_op'); a.cmpbi(T, OPS['SUB']); J('JNE', 'vm_o4'); a.sub(X, Z); a.sjmp('vm_st')
    a.L('vm_o4'); a.cmpbi(T, OPS['MINK']); J('JNE', 'vm_o5'); a.cmp(X, Z); J('JLE', 'vm_st'); a.ld(X, Z); a.sjmp('vm_st')
    a.L('vm_o5'); a.cmpbi(T, OPS['MAXK']); J('JNE', 'vm_add'); a.cmp(X, Z); J('JGE', 'vm_st'); a.ld(X, Z); a.sjmp('vm_st')
    a.L('vm_add'); a.add(X, Z)                         # ADD / ADDK
    a.L('vm_st'); a.ldbze(Z, U); a.st_lidx(Z, REGS, X); a.sjmp('vm_l')
    a.L('ar_x'); a.ret()



def emit_lists(a):
    J = a.jcc
    lda, sta = a.ldb_abs, a.stb_abs
    # ---- a field list: patch the words, then write the cells ----
    a.L('fx_list')
    a.ldb_pi(N, P)
    a.L('fl_w'); a.cmpbi(N, 0); J('JE', 'fl_c')
    a.ldb_pi(A, P); a.lcall('fx_rd')
    a.ldb_pi(T, P); a.ldb(K2, T); a.ldbi(MH, 0xFF); a.ldbi(MM, 0xFF); a.ldbi(U, 0xFF)
    a.jbc(T, 2, 'fl_k1'); a.clrb(U); a.andbi(MM, 0xFE)
    a.L('fl_k1'); a.jbc(T, 3, 'fl_k2'); a.andbi(MM, 0xC1)
    a.L('fl_k2'); a.jbc(T, 4, 'fl_k3'); a.andbi(MM, 0x3F); a.andbi(MH, 0xFC)
    a.L('fl_k3'); a.andbi(T, 3); a.andb(WH, MH); a.orb(WH, T)
    a.ldb_pi(T, P); a.andb(WM, MM); a.orb(WM, T)
    a.ldb_pi(T, P); a.andb(WL, U); a.orb(WL, T)
    a.jbc(K2, 5, 'fl_nb')
    a.ldb_pi(G, P); a.ldb(T, G); a.andbi(T, 63); a.ldbze(Z, T); a.ld_lidx(X, Z, REGS); a.lcall('fx_enc')
    a.L('fl_nb'); a.jbc(K2, 6, 'fl_nc')
    a.ldb_pi(T, P); a.ldbi(U, fx2.Z); lda(K2, FX_FULL); a.cmpbi(K2, 0); J('JNE', 'fl_bs')
    a.ldbi(U, fx2.DL); a.cmpbi(T, 0); J('JE', 'fl_bs'); a.ldbi(U, fx2.DR)
    a.L('fl_bs'); a.andbi(WM, 0xC1); a.addb(U, U); a.orb(WM, U)
    a.L('fl_nc'); a.lcall('fx_wr'); a.decb(N); a.sjmp('fl_w')
    a.L('fl_c'); a.ldb_pi(N, P)
    a.L('fl_c2'); a.cmpbi(N, 0); J('JE', 'fl_x')
    a.ldb_pi(A, P); a.jbs(A, 7, 'fl_tab')
    a.ldb_pi(WH, P); a.ldb_pi(WM, P); a.ldb_pi(WL, P); a.sjmp('fl_cw')
    a.L('fl_tab'); a.ldb_pi(Q, P); a.ldb_pi(Q + 1, P); a.ldb_pi(T, P)
    a.ldbze(Z, T); a.add(Z, Y); a.ldb_ind(T, Z)        # param value
    a.ldb_pi(U, Q); a.cmpb(T, U); J('JNC', 'fl_ti'); a.ldb(T, U); a.decb(T)
    a.L('fl_ti'); a.ldbze(Z, T); a.add(Z, Q); a.ldb_ind(T, Z)    # n -> cell = n << 12
    a.ldb(WH, T); a.shrb(WH, 4); a.ldb(WM, T); a.andbi(WM, 15); a.b(0x19, 4, WM); a.clrb(WL)   # SHLB WM, #4
    a.L('fl_cw'); a.lcall('fx_wc'); a.decb(N); a.sjmp('fl_c2')
    a.L('fl_x'); a.ret()

    # ---- log X, flags G (bit 6 negative, bit 7 minus one) -> gain field in WL + WM bit 0 ----
    a.L('fx_enc')
    a.cmpi(X, 0x10000 - 16 * 256); J('JLT', 'en_0')
    a.ldbze(IDX, X); a.addi(IDX, 4); a.shr(IDX, 3); a.ldb(MH, X + 1)       # step i, octave e
    a.cmpi(IDX, 32); J('JNE', 'en_1'); a.clr(IDX); a.incb(MH)
    a.L('en_1'); a.cmpbi(MH, 5); J('JLT', 'en_2'); a.ldi(Z, 4095); a.sjmp('en_s')
    a.L('en_2'); a.add(IDX, IDX); a.ld_lidx_l(Z, IDX, 'fx_exp2')
    a.ldbi(T, 6); a.subb(T, MH)                         # Z >> (6 - e), then round off one more bit
    a.L('en_sh'); a.shr(Z, 1); a.decb(T); J('JNE', 'en_sh')
    a.inc(Z); a.shr(Z, 1); a.sjmp('en_s')
    a.L('en_0'); a.clr(Z)
    a.L('en_s'); a.jbc(G, 6, 'en_n'); a.clr(IDX); a.sub(IDX, Z); a.ld(Z, IDX)
    a.L('en_n'); a.jbc(G, 7, 'en_o'); a.subi(Z, 128)
    a.L('en_o'); a.cmpi(Z, 128); J('JGE', 'en_c'); a.cmpi(Z, 0x10000 - 127); J('JLT', 'en_c')
    a.ldb(WL, Z); a.orbi(WM, 1); a.ret()               # fine: 128ths
    a.L('en_c'); a.andbi(WM, 0xFE); a.ld(IDX, Z); a.cmpi(IDX, 0); J('JGE', 'en_p'); a.clr(IDX); a.sub(IDX, Z)
    a.L('en_p'); a.addi(IDX, 8); a.shr(IDX, 4); a.cmpi(IDX, 127); J('JLE', 'en_m'); a.ldi(IDX, 127)
    a.L('en_m'); a.ldb(WL, IDX); a.cmpi(Z, 0); J('JGE', 'en_r'); a.b(0x13, WL)    # NEGB WL
    a.L('en_r'); a.ret()

    a.L('fx_rd'); a.pushf(); sta(RCC + 0x0A, A)
    lda(WH, RCC); lda(WM, RCC + 1); lda(WL, RCC + 2); a.popf(); a.ret()
    a.L('fx_wr'); a.pushf(); sta(RCC, WH); sta(RCC + 1, WM); sta(RCC + 2, WL); sta(RCC + 6, A); a.popf(); a.ret()
    a.L('fx_wc'); a.pushf(); sta(RCC, WH); sta(RCC + 1, WM); sta(RCC + 2, WL); a.andbi(A, 31); sta(RCC + 4, A)
    a.popf(); a.ret()


def build():
    """-> [(org, bytes)] (CPU addresses: < 0x8000 fixed ROM, else page 4), labels, report, tables"""
    tb = tables()
    d = tb['d']
    desc, refs = [], []
    for pg, pre, ls in tb['desc']:
        for t in (pg, pre, ls):
            if t: refs.append((len(desc), t))
            desc += [0, 0]
    small = {'fx_desc': (desc, refs), 'fx_wet': (sum((s16(v) for v in tb['wet']), []), []),
             'fx_exp2': (sum((s16(v) for v in fx2.EXP2), []), []), 'fx_stab': (tb['stab'], []),
             'fx_da': (tb['da'], []), 'fx_db': (tb['db'], []), 'fx_dbok': (tb['dbok'], []),
             'fx_temps': ([len(TEMP_CELLS)] + TEMP_CELLS, [])}
    for n, (bs, rf) in small.items(): d.blobs[n] = bytes(bs); d.refs[n] = rf
    units = [emit_engine, emit_lists]
    def asm_all(org, ext):
        out = []
        for i, fn in enumerate(units):
            a = Asm(org[i]); a.ext = ext
            fn(a, len(SINGLE_NAMES)) if fn is emit_engine else fn(a)
            out.append(a)
        return out
    probe = asm_all([0x8000, 0x8000], defaultdict(lambda: 0x8000))
    sizes = [len(a.assemble()) + 8 for a in probe]                   # + room for far branches after placement
    free = [list(b) for b in P4_BLOCKS]
    def place(n):
        blk = min((b for b in free if b[1] - (b[0] + (b[0] & 1)) + 1 >= n), key=lambda b: b[1] - b[0])
        blk[0] += blk[0] & 1; o = blk[0]; blk[0] += n
        return o
    orgs = [place(n) for n in sizes]
    labels, segs = {}, []
    for name, blob in sorted(d.blobs.items(), key=lambda kv: -len(kv[1])):
        o = place(len(blob)); labels[name] = o; segs.append([o, bytearray(blob), name])
    labels['fx_common'] = labels[tb['common']]
    for sg in segs:                                                   # patch the pointers inside the blobs
        for off, tgt in d.refs[sg[2]]:
            sg[1][off] = labels[tgt] & 255; sg[1][off + 1] = labels[tgt] >> 8
    for i in range(3):
        ext = defaultdict(lambda: orgs[0], labels) if i == 0 else dict(labels)
        units_a = asm_all(orgs, ext); codes = [a.assemble() for a in units_a]
        for a in units_a: labels.update(a.labels)
    for o, c, n in zip(orgs, codes, sizes): assert len(c) <= n, 'FX2 code grew'
    out = [(o, c) for o, c in zip(orgs, codes)] + [(o, bytes(b)) for o, b, _ in segs]
    t = Asm(TICK_ORG); t.ext = labels; emit_tick(t); tc = t.assemble()
    assert TICK_ORG + len(tc) - 1 <= TICK_END
    labels.update(t.labels); out.append((TICK_ORG, tc))
    for o1, b1 in out:
        for o2, b2 in out:
            assert o1 == o2 or o1 + len(b1) <= o2 or o2 + len(b2) <= o1, 'FX2 blocks overlap %04X %04X' % (o1, o2)
    left = sum(b[1] - b[0] + 1 for b in free)
    rep = 'FX2: code %s, data %d B in %d blobs, tick %04X (%d B), %d log registers, page 4 left %d B' % (
        ' + '.join('%04X (%d B)' % (o, len(c)) for o, c in zip(orgs, codes)), sum(len(b) for _, b, _ in segs),
        len(segs), TICK_ORG, len(tc), max(tb['rm'].values()) + 1, left)
    return out, labels, rep, tb


if __name__ == '__main__':
    segs, labels, rep, tb = build()
    print(rep)
    for o, b in segs: print('  %04X-%04X %4d' % (o, o + len(b) - 1, len(b)))
