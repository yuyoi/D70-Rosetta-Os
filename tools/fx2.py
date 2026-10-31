"""FX2: the D-70 master-insert effects as recipes over the RCC's free steps, for the SINGLE / DOUBLE menu (v0.10).

Same insert as fxbox.py v3 (capture L at L252 / R at L417, outputs 0 / 6 read our cells), but every effect is a list
of ops placed on named steps, with gains as products of factors (curves over the menu values + runtime values), so
the firmware can compute them instead of storing every value. Plan + step map: fw/rosetta/FX2_PLAN.md.

Box 1 (pool A: RAMP1 PH1 CIR + CAPR, T53 / tap1) -> box 2 (pool B: CIM CRM0 P4 P5 P6); SINGLE modes are a box on
its own or a full-pool recipe (S_DRIVE stereo, S_CRUSH full range). Everything but S_DRIVE runs mono (L + R) / 2.
MIX: the captures take gain gc = 2(1-m) and the output biases read them (dry); the wet path is put back to unity
with igc = 1/gc; out gain = 2m x the chain's export scale.
Gains are worked out exactly like the firmware does (fw/rosetta/fxfw2.py): log2 registers, a 32-step 2^x table.
  py -3.10 tools/fx2.py            chains, steps used, program sizes
"""
import itertools
import math
import os

FS = 32000
Z, JUNK = 1, 0
GMAX = 15.875
HERE = os.path.dirname(os.path.abspath(__file__))
DUMP = os.environ.get('D70_RCC_DUMP') or os.path.join(HERE, '..', 'dsp_lab', 'hw_dump_v08_after_scan.txt')   # effects chip dump
DL, DR, AO, PARK_CELL = 3, 5, 6, 13                   # dry L / R (biases), box 1 out, tap0 parking constant
A_TEMPS = (22, 23, 10, 14, 12)
B_TEMPS = (16, 17, 4, 7, 8, 9, 11)
SCRATCH = (3, 5, 6, 12, 13, 22, 23)                   # zero at OFF
KEEP = (4, 7, 8, 9, 10, 11, 14, 16, 17)               # stock values saved / put back at OFF
T44_OFF = 0x080                                       # T44 (writes 0) moved here; tap0 parks one above it
MIXN = 16                                             # MIX 0..15, 15 = all wet


# ---- gain expressions: c * product(factors) + off ----
class E:
    def __init__(s, c=1.0, f=(), off=0.0):
        s.c, s.f, s.off = float(c), tuple(f), float(off)

    def __mul__(s, o):
        o = o if isinstance(o, E) else E(o)
        assert not s.off and not o.off, 'offset only last'
        return E(s.c * o.c, s.f + o.f)
    __rmul__ = __mul__

    def __neg__(s): return E(-s.c, s.f, -s.off)
    def __sub__(s, k): return E(s.c, s.f, s.off - k)

    def val(s, env):
        v = s.c
        for f in s.f: v *= env[f]
        return v + s.off

    def __repr__(s): return '%g%s%s' % (s.c, ''.join('*' + f for f in s.f), '%+g' % s.off if s.off else '')


def K(x): return E(x)
def F(name): return E(1.0, (name,))


# ---- steps: op -> fields {(word, 'g' | 'c' | 'n'): value} ----
def _set(**kw):
    out = {}
    for k, v in kw.items():
        out[(int(k[1:], 16), k[0])] = v if k[0] != 'g' or isinstance(v, E) else K(v)
    return out


def st_ramp1(op):          # r1c = c82 - c81*g82 (saturates); dst c88 = r1c * (1 + g86)
    k, d = op[0], op[1]
    base = _set(c83=15, g84=-GMAX, c86=Z, c88=d)                 # wrap off (limit c15 = E00000)
    if k == 'mul': base.update(_set(c82=Z, c81=op[2], g82=-op[3], g86=0))
    elif k == 'mac': base.update(_set(c82=op[2], c81=op[3], g82=-op[4], g86=0))
    elif k == 'mul2': base.update(_set(c82=Z, c81=op[2], g82=-op[3], g86=op[4] - 1))
    else: return None
    return base


def st_cim(op):            # dst c94 = c92 + c90*g92
    k, d = op[0], op[1]
    if k == 'mul': return _set(c92=Z, c90=op[2], g92=op[3], c94=d)
    if k == 'mac': return _set(c92=op[2], c90=op[3], g92=op[4], c94=d)


def st_ph1(op):            # dst cBA = cB1*gB2 - |cB7*gB8| (also tap1 phase with cB9 / gBA; residual -> cC8 = c0)
    k, d = op[0], op[1]
    if k == 'mul': return _set(cB1=op[2], gB2=op[3], gB8=0, cB9=Z, gBA=0, cBA=d)
    if k == 'abs': return _set(cB1=Z, gB2=0, cB7=op[2], gB8=op[3], cB9=Z, gBA=0, cBA=d)


def st_cir(op):            # dst cD3 = cC7*gC9 (cC7 shared with CRM0, so CRM0 multiplies by its C9 input)
    if op[0] == 'mul': return _set(cC7=op[2], gC9=op[3], cD3=op[1])


def st_crm0(op):           # dst cCC = tap0*gC1 (parked: 0) + cC7*gC8 - cC9*gCA
    if op[0] == 'mul': return _set(gC8=0, cC9=op[2], gCA=-op[3], cCC=op[1])


def st_p4(op):             # dst cE7 = cE5 + cE4*gE5 (cE4 shared with P5)
    k, d = op[0], op[1]
    if k == 'mul': return _set(cE5=Z, cE4=op[2], gE5=op[3], cE7=d)
    if k == 'mac': return _set(cE5=op[2], cE4=op[3], gE5=op[4], cE7=d)


def st_p5(op):             # dst cED = cEB - cE4*gEB (gE4 = 0; its abs input cE3 also feeds voice 28: never used)
    if op[0] == 'mac': return _set(cEB=op[2], cE4=op[3], gE4=0, gEB=-op[4], cED=op[1])


def st_p6(op):             # dst cEE = cEC - (cE9 - |cE8*gE9|)*gEC
    k, d = op[0], op[1]
    if k == 'mul': return _set(cE9=op[2], gE9=0, cEC=Z, gEC=-op[3], cEE=d)
    if k == 'mac': return _set(cE9=op[3], gE9=0, cEC=op[2], gEC=-op[4], cEE=d)
    if k == 'abs': return _set(cE9=Z, cE8=op[2], gE9=op[3], cEC=Z, gEC=-1.0, cEE=d)     # -|x*g|


def st_capl(op):           # dst c0B = c24 * -g09 (L252)
    if op[0] == 'mul' and op[2] == 24: return _set(c08=24, g09=-op[3], c0B=op[1])


def st_capr(op):           # dst cD6 = tap1*gCB + cD1*gD2 + cD2*gD4 (L417)
    k, d = op[0], op[1]
    if k == 'mul': return _set(cD1=op[2], gD2=op[3], gD4=0, cD6=d)
    if k == 'mac2': return _set(cD1=op[2], gD2=op[3], cD2=op[4], gD4=op[5], cD6=d)


STEPS = {   # name: (program line, adapter, neutral fields)
    'CAPL': (252, st_capl, _set(g09=0, c0B=JUNK)),
    'RAMP1': (353, st_ramp1, _set(c82=Z, c81=Z, g82=0, c83=15, g84=-GMAX, c86=Z, g86=0, c88=JUNK)),
    'CIM': (364, st_cim, _set(c92=Z, g92=0, c94=JUNK)),
    'PH1': (392, st_ph1, _set(cB1=Z, gB2=0, cB7=Z, gB8=0, cB9=Z, gBA=0, cBA=JUNK)),
    'CRM0': (409, st_crm0, _set(gC8=0, gCA=0, cCC=JUNK)),
    'CIR': (414, st_cir, _set(gC9=0, cD3=JUNK)),
    'CAPR': (417, st_capr, _set(gD2=0, gD4=0, cD6=JUNK)),
    'P4': (427, st_p4, _set(cE5=Z, gE5=0, cE7=JUNK)),
    'P5': (438, st_p5, _set(cEB=Z, gE4=0, gEB=0, cED=JUNK)),
    'P6': (441, st_p6, _set(cE9=Z, gE9=0, cEC=Z, gEC=0, cEE=JUNK)),
}
POOL_A = ('RAMP1', 'PH1', 'CIR')
POOL_B = ('CIM', 'CRM0', 'P4', 'P5', 'P6')
FULL = POOL_A + POOL_B
# fixed for every recipe: kill the chorus wet + its writes into our cells, park tap0 on T44's zeros, tap1 off
assert T44_OFF & 15 == 0
FRAME = _set(c13=JUNK, c6C=JUNK, g03=0, c03=JUNK, c6D=Z, c4F=JUNK, c8D=JUNK, cC8=JUNK, gCB=0, gD3=0,
             cA1=Z, gA2=0, cA2=PARK_CELL, cB0=Z, nB0=T44_OFF >> 12 & 15, nB1=T44_OFF >> 8 & 15, nB2=T44_OFF >> 4 & 15)
FRAME_CELLS = {PARK_CELL: (T44_OFF + 1) << 12}
DEFAULTS = {}
for _n, (_l, _a, _neutral) in STEPS.items(): DEFAULTS.update(_neutral)
DEFAULTS.update(FRAME)




def combine(*dicts):
    out = {}
    for d in dicts:
        for k, v in d.items():
            if k in out and repr(out[k]) != repr(v):
                raise ValueError('field clash W%02X.%s: %r vs %r' % (k[0], k[1], out[k], v))
            out[k] = v
    return out


# ---- gain maths, exactly as the firmware does it: factors are log2 values in 1/256 octave (int16), a product is
# a sum, a gain field is made from a log by a 32-step 2^x table ----
NEG = -0x4000                                         # log of 0
EXP2 = [round(2 ** (i / 32) * 16384) for i in range(32)]
RT = ('wet', 'gc', 'igc', 'isa', 'apre', 'one')       # registers 0..5, set before the programs run


def lg(x):
    return NEG if x == 0 else round(math.log2(x) * 256)


def lin(l):
    """the gain field's 128ths for log l (before sign / offset)"""
    if l < -16 * 256: return 0
    e, f = l >> 8, l & 255
    i = (f + 4) >> 3
    if i == 32: i, e = 0, e + 1
    if e > 4: return 4095
    s = 7 - e
    return 0 if s >= 16 else (EXP2[i] + (1 << s - 1)) >> s


def enc(l, neg=False, off=False):
    """9-bit gain field for sign * 2^(l/256) - off: 128ths below 1 (bit 8), else 8ths, clamped"""
    v = lin(l)
    if neg: v = -v
    if off: v -= 128
    if -128 < v < 128: return 0x100 | (v & 255)
    c = min((abs(v) + 8) >> 4, 127)
    return (-c if v < 0 else c) & 255


def gval(code):
    c = code & 255
    c = c - 256 if c & 128 else c
    return c / (128 if code & 0x100 else 8)


def mixlogs(mix):
    """runtime registers: wet = 2m, gc = 2(1-m) (1 at full wet), igc = 1/gc"""
    wet = lg(2 * mix / (MIXN - 1))
    gc = 0 if mix == MIXN - 1 else lg(2 * (MIXN - 1 - mix) / (MIXN - 1))
    return {'wet': wet, 'gc': gc, 'igc': -gc, 'isa': 0, 'apre': 0, 'one': 0}


class Recipe:
    """one box (or a full SINGLE mode): a factor program (ops on log registers), fields, cells, output, export"""
    def __init__(s, name, pool, temps, params=()):
        s.name, s.pool, s.temps, s.params = name, pool, list(temps), list(params)
        s.place, s.prog, s.pre, s.cells, s.raw = [], [], [], {}, {}
        s.out, s.dry_r, s.want_pre = None, None, False
        s.export = K(1.0)        # multiply this box's output by it to get unity level

    def key(s, n): return n if n in RT else '%s.%s' % (s.name, n)
    def reg(s, x): return x.f[0] if isinstance(x, E) else s.key(x)

    def curve(s, name, pidx, fn):
        """register <name> = log fn(value of param pidx)"""
        s.prog.append(('CUR', s.key(name), pidx, [lg(fn(v)) for v in range(s.params[pidx][1])]))
        return F(s.key(name))

    def _op(s, op, name, a, b, pre=False):
        (s.pre if pre else s.prog).append((op, s.key(name), s.reg(a), b if isinstance(b, int) else s.reg(b)))
        return F(s.key(name))

    def add(s, n, a, b, pre=False): return s._op('ADD', n, a, b, pre)
    def sub(s, n, a, b, pre=False): return s._op('SUB', n, a, b, pre)
    def mink(s, n, a, x, pre=False): return s._op('MINK', n, a, lg(x), pre)        # min(a, log x)
    def maxk(s, n, a, x, pre=False): return s._op('MAXK', n, a, lg(x), pre)
    def addk(s, n, a, l, pre=False): return s._op('ADDK', n, a, l, pre)           # a + l (a log)

    def split2(s, n, t, m2=GMAX):
        """two cascaded stages for total gain t: the first up to GMAX, the second the rest (up to m2)"""
        a = s.mink(n + '1', t, GMAX)
        s.sub(n + 'r', t, a)
        return a, s.mink(n + '2', n + 'r', m2)

    def ups(s, n, total, k):
        """k up stages for gain `total`, each up to GMAX; register <n>rest = what is left for the out stage"""
        out, left = [], total
        for i in range(k):
            out.append(s.mink('%s%d' % (n, i), left, GMAX))
            left = s.sub('%sl%d' % (n, i), left, out[-1])
        s.addk(n + 'rest', left, 0)
        return out

    def op(s, step, *op):
        assert step in s.pool or step in ('CAPL', 'CAPR'), (s.name, step)
        s.place.append((step, op))
        return op[1]

    def fields(s, cellmap):
        used = [st for st, _ in s.place]
        assert len(used) == len(set(used)), (s.name, 'step used twice')
        parts = [s.raw]
        for st, op in s.place:
            if op[0] == 'raw': continue
            f = STEPS[st][1](tuple(cellmap.get(x, x) if isinstance(x, str) else x for x in op))
            if f is None: raise ValueError('%s: %s cannot do %r' % (s.name, st, op[0]))
            parts.append(f)
        return combine(*parts)

    def temp(s):
        return s.temps.pop(0)


# ---- box 1 (pool A), output AO; its program ends with register isa = its export ----
def a_thru():
    r = Recipe('A_THRU', POOL_A, A_TEMPS)
    r.op('CAPR', 'mac2', AO, 30, F('gc') * 0.5, DL, K(0.5))
    r.op('CIR', 'mul', DR, 30, F('gc'))
    r.out, r.dry_r, r.export = AO, DR, F('igc')
    return r


def drive_pre(v): return 8 * 2 ** (v / 3)                  # DRIVE 0..15 = x8 .. x256 (the bus sits at 3-10 % FS)
def drive_out(v): return 1 / min(drive_pre(v), 40)         # exact while unclipped; a clipped note ~ dry level
POST = 8.0                                                 # post-clip gain sits in a step at x POST, out stage 1/POST


def a_drive():
    """mono: CAPR t = (L + R) / 2 at unity, RAMP1 two clip stages, PH1 post gain"""
    r = Recipe('A_DRIVE', POOL_A, A_TEMPS, [('DRIVE', 16, 6)])
    pre = r.curve('pre', 0, drive_pre)
    post = r.curve('post', 0, lambda v: drive_out(v) * POST)
    s1, s2 = r.split2('s', pre, GMAX + 1)
    t, u = r.temp(), r.temp()
    r.op('CAPR', 'mac2', t, 30, K(0.5), DL, F('igc') * 0.5)
    r.op('CIR', 'mul', DR, 30, F('gc'))
    r.op('RAMP1', 'mul2', u, t, s1, s2)
    r.op('PH1', 'mul', AO, u, post)
    r.out, r.dry_r, r.export = AO, DR, K(1 / POST)
    return r


DEC_N = [1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 20, 24, 32, 40, 48, 64]


def a_decimate():
    """mono S&H through the chorus DRAM: (L + R) gc in by T53 (offset 1: CIR + cell D2), tap1 reads 2 + ST, RAMP1
    counts ST up one sample per frame and resets at n, so the read address stands still, then jumps"""
    r = Recipe('A_DECIMATE', POOL_A, A_TEMPS, [('RATE', 16, 4)])
    inc, lim, st, br = r.temp(), r.temp(), r.temp(), r.temp()
    r.cells.update({inc: 0x1000, lim: ('TAB', 0, DEC_N), st: 0, br: 0x2000})        # lim = n << 12
    r.place += [('RAMP1', ('raw',)), ('PH1', ('raw',)), ('CAPR', ('raw',))]
    r.op('CIR', 'mul', DR, 30, F('gc'))                                                # R gc: T53 input + dry R
    r.raw = _set(c82=inc, c81=st, g82=-1.0, c83=lim, g84=1.0, c86=Z, g86=0, c88=st,      # ST += INC, wraps at LIM
                 cB1=st, gB2=1.0, cB7=Z, gB8=0, cB9=br, gBA=-1.0, cBA=JUNK,             # tap1 phase = ST + BR
                 cD2=DL, gD3=1.0,                                                      # T53 = R gc + L gc
                 gCB=F('apre') * 0.5, gD2=0, gD4=0, cD6=AO)                            # AO = tap1 / 2 (x crush pre-down)
    r.out, r.dry_r, r.export = AO, DR, F('igc')
    return r


# ---- box 2 (pool B), input IN at scale 1 / isa ----
def b_thru():
    r = Recipe('B_THRU', POOL_B, B_TEMPS)
    r.out, r.export = 'IN', F('isa')
    return r


def b_drive():
    """mono: CIM + P4 clip stages, P6 post gain"""
    r = Recipe('B_DRIVE', POOL_B, B_TEMPS, [('DRIVE', 16, 6)])
    pre = r.curve('pre', 0, drive_pre)
    post = r.curve('post', 0, lambda v: drive_out(v) * POST)
    s1, s2 = r.split2('s', r.add('t', pre, 'isa'))
    a, b, c = r.temp(), r.temp(), r.temp()
    r.op('CIM', 'mul', a, 'IN', s1)
    r.op('P4', 'mul', b, a, s2)
    r.op('P6', 'mul', c, b, post)
    r.out, r.export = c, K(1 / POST)
    return r


CUT_N, RES_N = 32, 16
def cutoff_hz(c): return 50 * 100 ** (c / (CUT_N - 1))       # 50 Hz .. 5 kHz
def reso_q(r): return 0.7 * (16 / 0.7) ** (r / (RES_N - 1))  # Q 0.7 .. 16


def b_filter(kind):
    """Chamberlin SVF in program order: CIM bp += f*hp (hp from last frame), P4 lp += f*bp, P5 t = x - q*bp
    (P5's cE4 = bp, the same cell P4 reads), P6 hp = t - lp"""
    r = Recipe('B_' + kind.upper(), POOL_B, B_TEMPS, [('CUTOFF', CUT_N, 20), ('RESO', RES_N, 4)])
    f = r.curve('f', 0, lambda c: 2 * math.sin(math.pi * cutoff_hz(c) / FS))
    q = r.curve('q', 1, lambda v: 1 / reso_q(v))
    bp, lp, t, hp = r.temp(), r.temp(), r.temp(), r.temp()
    r.op('CIM', 'mac', bp, bp, hp, f)
    r.op('P4', 'mac', lp, lp, bp, f)
    r.op('P5', 'mac', t, 'IN', bp, -q)
    r.op('P6', 'mac', hp, t, lp, K(-1.0))
    r.out = {'lpf': lp, 'bpf': bp, 'hpf': hp}[kind]
    r.export = F('isa') * (r.curve('lvl', 1, lambda v: min(1.0, reso_q(v) ** -0.5)) if kind == 'bpf' else K(1.0))
    return r


RECT_G = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10, 12, 15]


def b_rectify():
    """full-wave rectifier (octave-up fuzz) + DC blocker: P6 y = -|x g|, CIM hp = y - lp, P4 lp += hp / 128"""
    r = Recipe('B_RECTIFY', POOL_B, B_TEMPS, [('AMOUNT', 16, 3)])
    g = r.curve('g', 0, lambda v: RECT_G[v])
    y, hp, lp = r.temp(), r.temp(), r.temp()
    r.op('CIM', 'mac', hp, y, lp, K(-1.0))
    r.op('P4', 'mac', lp, lp, hp, K(1 / 128))
    r.op('P6', 'abs', y, 'IN', g)
    r.out, r.export = hp, -F('isa')
    return r


BITS_N = 17                                                 # BITS = the bit count itself (menu shows it), 2..16
B_BITS_MIN = 7                                              # CRUSH after DECIMATE: 6 + 7 bits of down


def b_crush():
    """mono crush after DECIMATE: its tap1 return gain (0.5) takes up to 6 bits of the down (apre, exact powers of
    two), CIM the rest, CRM0 P4 P6 up by isa * 2^k, the out stage the rest"""
    r = Recipe('B_CRUSH', POOL_B, B_TEMPS, [('BITS', BITS_N, 8)])
    r.want_pre = True
    nk = r.curve('nk', 0, lambda v: 2.0 ** -(20 - max(v, B_BITS_MIN)))
    r.pre.append(r.prog.pop())                                    # before box 1 (its last gain takes apre)
    r.maxk('apre', nk, 2.0 ** -6, pre=True)
    d = r.sub('d', nk, 'apre')
    us = r.ups('u', r.sub('tot', 'isa', nk), 3)
    a, b, c, o = r.temp(), r.temp(), r.temp(), r.temp()
    r.op('CIM', 'mul', a, 'IN', d)
    r.op('CRM0', 'mul', b, a, us[0])
    r.op('P4', 'mul', c, b, us[1])
    r.op('P6', 'mul', o, c, us[2])
    r.out, r.export = o, F(r.key('urest'))
    return r


# ---- full-pool SINGLE recipes ----
def s_drive():
    """stereo: L = RAMP1 two clip stages from DL (+ igc), CIM post; R = CAPR stage 1 from c30, PH1 stage 2, CRM0 post"""
    r = Recipe('S_DRIVE', FULL, A_TEMPS + B_TEMPS, [('DRIVE', 16, 6)])
    pre = r.curve('pre', 0, drive_pre)
    post = r.curve('post', 0, lambda v: drive_out(v) * POST)
    l1, l2 = r.split2('l', r.add('tl', pre, 'igc'), GMAX + 1)
    r1, r2 = r.split2('r', pre)
    ul, tr, ur, ol, orr = (r.temp() for _ in range(5))
    r.op('RAMP1', 'mul2', ul, DL, l1, l2)
    r.op('CIM', 'mul', ol, ul, post)
    r.op('CAPR', 'mul', tr, 30, r1)
    r.op('PH1', 'mul', ur, tr, r2)
    r.op('CRM0', 'mul', orr, ur, post)
    r.op('CIR', 'mul', DR, 30, F('gc'))
    r.out, r.dry_r, r.export = (ol, orr), DR, K(1 / POST)
    return r


def s_crush():
    """mono, full range: CAPR (L + R) / 2, RAMP1 two downs, CIM third, PH1 CRM0 P4 P6 up, out the rest"""
    r = Recipe('S_CRUSH', FULL, A_TEMPS + B_TEMPS, [('BITS', BITS_N, 8)])
    nk = r.curve('nk', 0, lambda v: 2.0 ** -(20 - max(v, 2)))
    d1 = r.maxk('d1', nk, 2.0 ** -7)
    r1 = r.sub('r1', nk, d1)
    d2 = r.maxk('d2', r1, 2.0 ** -7)
    d3 = r.sub('d3', r1, d2)
    us = r.ups('u', r.sub('tot', 'one', nk), 4)
    t, a, b, c, d, f, o = (r.temp() for _ in range(7))
    r.op('CAPR', 'mac2', t, 30, K(0.5), DL, F('igc') * 0.5)
    r.op('CIR', 'mul', DR, 30, F('gc'))
    r.op('RAMP1', 'mul2', a, t, d1, d2)
    r.op('CIM', 'mul', b, a, d3)
    r.op('PH1', 'mul', c, b, us[0])
    r.op('CRM0', 'mul', d, c, us[1])
    r.op('P4', 'mul', f, d, us[2])
    r.op('P6', 'mul', o, f, us[3])
    r.out, r.dry_r, r.export = o, DR, F(r.key('urest'))
    return r


A_MODES = {'THRU': a_thru, 'DRIVE': a_drive, 'DECIMATE': a_decimate}
B_MODES = {'THRU': b_thru, 'DRIVE': b_drive, 'CRUSH': b_crush, 'RECTIFY': b_rectify,
           'LPF': lambda: b_filter('lpf'), 'BPF': lambda: b_filter('bpf'), 'HPF': lambda: b_filter('hpf')}
FULL_MODES = {'S_DRIVE': s_drive, 'S_CRUSH': s_crush}
SINGLE = {'DRIVE': 'S_DRIVE', 'DECIMATE': ('DECIMATE', 'THRU'), 'CRUSH': 'S_CRUSH', 'RECTIFY': ('THRU', 'RECTIFY'),
          'LPF': ('THRU', 'LPF'), 'BPF': ('THRU', 'BPF'), 'HPF': ('THRU', 'HPF'), 'LO-FI': ('DECIMATE', 'CRUSH')}
DOUBLE_A = ('DRIVE', 'DECIMATE')
DOUBLE_B = ('DRIVE', 'CRUSH', 'RECTIFY', 'LPF', 'BPF', 'HPF')


def pair_ok(an, bn):
    """box 2 modes allowed after box 1 mode an (the DOUBLE page skips the rest)"""
    if bn == 'CRUSH': return an == 'DECIMATE'                  # crushing a clipped square does nothing
    return an != bn


# ---- compile: a recipe -> its program (gains folded in as registers) + field entries ----
class Compiled:
    """fields: {(word, kind): const value | ('G', reg, neg, off) | 'BIASL' | 'BIASR' | 'IN'}, cells, prog, pre"""
    def __init__(s, r, gains):
        s.r, s.name, s.params, s.prog, s.pre = r, r.name, r.params, list(r.prog), list(r.pre)
        s.fields, s.cells = {}, dict(r.cells)
        s.gains = gains

    def gain(s, v, tag):
        """E -> const field value or ('G', reg, neg, off); multi-factor / scaled gains get a register"""
        if not isinstance(v, E): v = K(v)
        neg, c = v.c < 0, abs(v.c)
        off = bool(v.off)
        assert v.off in (0, -1), v
        if not v.f: return ('K', enc(lg(c), neg, off))
        if len(v.f) == 1 and c == 1: return ('G', v.f[0], neg, off)
        reg = '%s.g%d' % (s.name, s.gains[0]); s.gains[0] += 1
        regs = list(v.f)
        if len(regs) == 1: s.prog.append(('ADDK', reg, regs[0], lg(c)))
        else:
            s.prog.append(('ADD', reg, regs[0], regs[1]))
            for x in regs[2:]: s.prog.append(('ADD', reg, reg, x))
            if c != 1: s.prog.append(('ADDK', reg, reg, lg(c)))
        return ('G', reg, neg, off)


def compile_recipe(r, cellmap, outspec=None):
    c = Compiled(r, [0])
    f = r.fields(cellmap)
    if outspec: f.update(outspec)
    for k, v in f.items():
        if k[1] == 'g': v = c.gain(v, k)
        c.fields[k] = v
    if r.name.startswith('A_'):                                   # box 1: register isa = its export
        g = c.gain(r.export, 'isa')
        c.prog.append(('ADDK', 'isa', g[1], 0) if g[0] == 'G' else ('ADDK', 'isa', 'one', lg(abs(r.export.c))))
    return c


def out_fields(r, sig):
    sig = sig if isinstance(sig, tuple) else (sig, sig)
    gx = F('wet') * r.export
    return _set(c19=sig[0], g1C=gx, c1C='BIASL', cDB=sig[1], gDC=gx, cDC='BIASR')


COMMON = None


def common():
    """every chain: the idle-step fields (recipes override the steps they use), frame, CAPL (dry L = c24 * gc)"""
    global COMMON
    if COMMON is None:
        r = Recipe('COMMON', (), ())
        c = Compiled(r, [0])
        f = {}
        for st in STEPS:                                          # every chain sets CAPL CAPR CIR itself
            if st not in ('CAPL', 'CAPR', 'CIR'): f.update(STEPS[st][2])
        f.update(FRAME); f.update(st_capl(('mul', DL, 24, F('gc'))))
        for k, v in f.items(): c.fields[k] = c.gain(v, k) if k[1] == 'g' else v
        c.cells = dict(FRAME_CELLS)
        COMMON = c
    return COMMON


def chain(aname, bname=None):
    """box 1 mode then box 2 mode, or a full recipe name -> list of Compiled (common first)"""
    if bname is None:
        r = FULL_MODES[aname]()
        recs = [compile_recipe(r, {}, out_fields(r, r.out))]
        name = aname
    else:
        a, b = A_MODES[aname](), B_MODES[bname]()
        cm = {'IN': a.out}
        out = cm.get(b.out, b.out) if isinstance(b.out, str) else b.out
        recs = [compile_recipe(a, cm), compile_recipe(b, cm, out_fields(b, out))]
        name = '%s>%s' % (aname, bname)
        combine(*[{k: v for k, v in c.fields.items()} for c in recs])          # box 1 and box 2 never share a field
        used = [s for x in (a, b) for s, _ in x.place]
        assert len(used) == len(set(used)), ('step clash', used)
    return dict(name=name, recs=[common()] + recs)


# ---- evaluate exactly like the firmware ----
def run_prog(prog, R, vals):
    for op in prog:
        if op[0] == 'CUR': R[op[1]] = op[3][min(vals[op[2]], len(op[3]) - 1)]
        elif op[0] == 'ADD': R[op[1]] = R[op[2]] + R[op[3]]
        elif op[0] == 'SUB': R[op[1]] = R[op[2]] - R[op[3]]
        elif op[0] == 'MINK': R[op[1]] = min(R[op[2]], op[3])
        elif op[0] == 'MAXK': R[op[1]] = max(R[op[2]], op[3])
        elif op[0] == 'ADDK': R[op[1]] = R[op[2]] + op[3]
        else: raise ValueError(op)


def registers(ch, vals, mix):
    """vals = [box 1 values, box 2 values] (or [full values])"""
    R = mixlogs(mix)
    recs = ch['recs'][1:]
    for c, v in zip(recs[1:], vals[1:]): run_prog(c.pre, R, v)
    for c, v in zip(recs, vals): run_prog(c.prog, R, v)
    return R


def evaluate(ch, vals, mix, st):
    """-> words {addr: value}, cells {cell: value}: common, then box 1, then box 2 (later lists win)"""
    R = registers(ch, vals, mix)
    full = mix == MIXN - 1
    words, cells = {}, {}
    pv = [[]] + list(vals)
    for c, v in zip(ch['recs'], pv):
        for (w, kind), x in sorted(c.fields.items()):
            cur = words.get(w, st[w])
            if kind == 'g':
                code = x[1] if x[0] == 'K' else enc(R[x[1]], x[2], x[3])
                cur = (cur & ~GAIN) | code
            elif kind == 'c':
                if x == 'BIASL': x = Z if full else DL
                elif x == 'BIASR': x = Z if full else DR
                cur = (cur & ~CELL) | x << 9
            else:
                cur = (cur & ~NIB) | (x & 15) << 14
            words[w] = cur
        for cell, x in c.cells.items():
            cells[cell] = (x[2][min(v[x[1]], len(x[2]) - 1)] << 12) if isinstance(x, tuple) else x
    return words, cells


GAIN, CELL, NIB = 0x1FF, 31 << 9, 15 << 14


def stock():
    st, cells = {}, {}
    for l in open(DUMP):
        if l.startswith('W'): st[int(l[1:3], 16)] = int(l[4:10], 16)
        if l.startswith('C'): cells[int(l[1:3])] = int(l[4:10], 16)
    return st, cells


def defaults(ch):
    return [[p[2] for p in c.params] for c in ch['recs'][1:]]


def all_chains():
    out = {}
    for name, sp in SINGLE.items():
        out['S:' + name] = chain(sp) if isinstance(sp, str) else chain(*sp)
    for an, bn in itertools.product(DOUBLE_A, DOUBLE_B):
        if pair_ok(an, bn): out['D:%s>%s' % (an, bn)] = chain(an, bn)
    return out


def touched():
    """every word any chain writes (saved at the first apply, put back at OFF)"""
    w = set()
    for ch in all_chains().values():
        for c in ch['recs']: w |= {k[0] for k in c.fields}
    return sorted(w)


def main():
    st, _ = stock()
    for name, ch in all_chains().items():
        steps = ' '.join(s for c in ch['recs'][1:] for s, _ in c.r.place)
        nprog = sum(len(c.prog) + len(c.pre) for c in ch['recs'][1:])
        print('%-20s %-42s %2d ops' % (name, steps, nprog))
    print('touched words: %d' % len(touched()))


if __name__ == '__main__':
    main()
