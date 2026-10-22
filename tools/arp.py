"""ROSETTA ARP for the Roland D-70: an arpeggiator on the D-70's own keyboard, in ROM page 7.

Keyboard notes reach the firmware as (note, velocity) pairs: the keyboard interrupt (0x15BC) queues them at RAM
0xC200 (note = key + 0x1C: 28-103 on the 76 keys), and the page-4 event pump (0xB481) fetches each one through
0x1092 (LJMP 0x1F42: C set, R1C = note, R1D = velocity) and hands it to 0xB49F, which tags it 0x10 and queues it
for the voice code. Velocity bit 7 SET = key down (page 6 0x82B0 sends MIDI 9n and marks the key in 0xEBC7),
clear = key up with the release velocity. 0xEBC7 has one byte per key, so notes stay inside 28-103.

  arp_in   0x1092 is pointed at a fixed-ROM trampoline into here. ARP off: the event passes untouched.
           ARP on: the key goes into the held-note list (sorted, 8 max) and is swallowed (C clear).
  arp_tick called once per main-loop pass (from rt_ui). TIMER1 (1.333 us) drives a phase accumulator in
           341 us units: acc += units * BPM, a 16th note is due every 43945 (= 703125 / 16) -> exact tempo.
           Notes go out through 0xB49F (tag 0x10), the same way a real key does: every layer, the effects and
           MIDI OUT follow. Gate 50 %: the note is released half way to the next step.
Modes UP, DOWN, UP-DOWN, RANDOM over 1-4 octaves; HOLD keeps the chord after the keys are let go (a new key
after all keys are up starts a new chord). Registers: R1C-R1D (event) and R20-R2F; R1E is the caller's queue
tag in arp_in and must survive there, and arp_in saves every other register it uses (the pump's caller keeps
values in R20-R2F).
"""

PUMP_HOOK = 0x1092             # LJMP 1F42 in the far-call table, called from page 4 0xB484 via the 0x1050 stub
PUMP_OLD = bytes([0xE7, 0xAD, 0x0E])   # LJMP 1F42
KEY_EVENT = 0x1F42             # fixed: next keyboard event -> C, R1C, R1D
QUEUE = 0xB49F                 # page 4: queue event R1E (tag), R1C, R1D
P4 = 0x1056
TIMER1 = 0x0A
K = 43945                      # 16th note at 1 BPM in 341.3 us units, / BPM (703125 / 16)
KEY_TAG = 0x10
KEY_UP = 0x40                  # key-up event: bit 7 clear, release velocity 64
NOTE_TOP = 103                 # top key (G7): 0x1C + 75

A_ON, A_MODE, A_OCT, A_BPM, A_HOLD = 0xC600, 0xC601, 0xC602, 0xC603, 0xC604   # settings (ARP page)
A_N, A_VEL, A_IDX, A_DIR, A_NOTE, A_PLAY, A_KEYS = 0xC605, 0xC606, 0xC607, 0xC608, 0xC609, 0xC60A, 0xC60B
A_LAST, A_ACC = 0xC60C, 0xC60E   # words
A_LIST = 0xC610                  # 8 held notes, ascending
MODES = ['UP', 'DOWN', 'UP-DOWN', 'RANDOM']

T, U, X = 0x1C, 0x1D, 0x1E
W, P, Q, V, C, IDX, Z = 0x20, 0x22, 0x24, 0x26, 0x28, 0x2C, 0x2E
R1A = 0x1A


IN_ORG, IN_END = 0xBE54, 0xBFFF       # page 4: stock's unused tail
TICK_ORG, TICK_END = 0x73D8, 0x7613   # fixed ROM freed by the bulk load cut (cuts.py): the clock lives here,
                                      # called from page 7 directly (fixed ROM is in every bank)


def emit(a):
    """the whole arpeggiator into one Asm (size checks)"""
    emit_in(a); emit_tick(a)


def emit_in(a):
    """key in, 'arp_in' (from the 0x1092 trampoline, via the page-4 stub 0x1056): ROM page 4's free tail"""
    J = a.jcc
    def lda(r, addr): a.ldb_abs(r, addr)
    def sta(addr, r): a.stb_abs(addr, r)
    def ldw(r, addr): a.ld_lidx(r, 0, addr)
    def stw(addr, r): a.st_lidx(0, addr, r)
    SETC, CLRC = 0xF9, 0xF8

    # ---------------- key in ----------------
    a.L('arp_in')
    a.lcall(KEY_EVENT); J('JC', 'ai1'); a.ret()                     # no key: C clear back to the pump
    a.L('ai1'); a.b(0x9B, 0x01, A_ON & 255, A_ON >> 8, 0x00); J('JNE', 'ai2')     # CMPB ZERO,A_ON[ZERO]
    a.b(SETC); a.ret()                                              # ARP off: pass it on, no register touched
    a.L('ai2'); a.push(Z); a.push(IDX); a.push(V); a.push(C); a.push(W)   # the pump's caller keeps values here
    a.jbs(U, 7, 'ai_on'); a.ljmp('ai_off')                         # bit 7 clear: key up
    a.L('ai_on')
    lda(Z, A_KEYS); a.cmpbi(Z, 0); J('JNE', 'ai3')                  # first key down: with HOLD, a new chord
    lda(Z, A_HOLD); a.cmpbi(Z, 0); J('JE', 'ai3'); a.clrb(Z); sta(A_N, Z)
    a.L('ai3'); lda(Z, A_KEYS); a.incb(Z); sta(A_KEYS, Z); sta(A_VEL, U)
    lda(C, A_N); a.cmpbi(C, 8); J('JE', 'ai_x')                     # list full
    a.clr(IDX)                                                      # find the place: first entry >= note
    a.L('ai4'); a.ldb(Z, IDX); a.cmpb(Z, C); J('JE', 'ai6')
    a.ldb_lidx(Z, IDX, A_LIST); a.cmpb(Z, T); J('JE', 'ai_x'); J('JH', 'ai6')   # already held / insert here
    a.incb(IDX); a.sjmp('ai4')
    a.L('ai6'); a.ldbze(V, C)                                       # shift the tail up by one
    a.L('ai7'); a.cmpb(V, IDX); J('JE', 'ai8')
    a.ldb_lidx(Z, V, A_LIST - 1); a.stb_lidx(V, A_LIST, Z); a.decb(V); a.sjmp('ai7')
    a.L('ai8'); a.stb_lidx(IDX, A_LIST, T)
    a.cmpbi(C, 0); J('JNE', 'ai9')                                  # was empty: start on the next pass
    a.ldbi(Z, 0xFF); sta(A_IDX, Z); a.clrb(Z); sta(A_DIR, Z)
    a.ldi(W, K); stw(A_ACC, W)
    a.L('ai9'); a.incb(C); sta(A_N, C)
    a.L('ai_x'); a.pop(W); a.pop(C); a.pop(V); a.pop(IDX); a.pop(Z); a.b(CLRC); a.ret()
    a.L('ai_off')
    lda(Z, A_KEYS); a.cmpbi(Z, 0); J('JE', 'ao1'); a.decb(Z); sta(A_KEYS, Z)
    a.L('ao1'); lda(Z, A_HOLD); a.cmpbi(Z, 0); J('JNE', 'ai_x')     # HOLD: the chord stays
    lda(C, A_N); a.clr(IDX)
    a.L('ao2'); a.ldb(Z, IDX); a.cmpb(Z, C); J('JE', 'ai_x')        # not in the list
    a.ldb_lidx(Z, IDX, A_LIST); a.cmpb(Z, T); J('JE', 'ao3'); a.incb(IDX); a.sjmp('ao2')
    a.L('ao3'); a.decb(C); sta(A_N, C)
    a.L('ao4'); a.ldb(Z, IDX); a.cmpb(Z, C); J('JE', 'ai_x')        # shift the tail down by one
    a.ldb_lidx(Z, IDX, A_LIST + 1); a.stb_lidx(IDX, A_LIST, Z); a.incb(IDX); a.sjmp('ao4')


def emit_tick(a):
    """clock, 'arp_tick' (called from rt_ui): fixed ROM"""
    J = a.jcc
    def lda(r, addr): a.ldb_abs(r, addr)
    def sta(addr, r): a.stb_abs(addr, r)
    def ldw(r, addr): a.ld_lidx(r, 0, addr)
    def stw(addr, r): a.st_lidx(0, addr, r)
    a.L('arp_tick')
    lda(Z, A_BPM); a.cmpbi(Z, 40); J('JC', 'at0'); a.ldbi(Z, 120); sta(A_BPM, Z)   # unset: 120 BPM
    a.L('at0'); lda(Z, A_ON); a.cmpbi(Z, 0); J('JNE', 'at0b')
    a.clrb(Z); sta(A_N, Z); sta(A_KEYS, Z); a.sjmp('at_stop')        # off: forget the chord
    a.L('at0b')
    a.ld(W, TIMER1); ldw(P, A_LAST); a.sub(W, P); a.shr(W, 8)       # whole 341 us units since last time
    a.cmpi(W, 64); J('JNH', 'at1')
    a.ld(P, TIMER1); stw(A_LAST, P); a.ldi(W, 64); a.sjmp('at2')    # long gap: resync, count 64
    a.L('at1'); a.ld(Q, W); a.shl(Q, 8); a.add(P, Q); stw(A_LAST, P)
    a.L('at2'); lda(Z, A_N); a.cmpbi(Z, 0); J('JE', 'at_stop')
    a.ldbze(Q, 0); lda(Q, A_BPM); a.b(0x6C, Q, W)                    # MULU W, Q: units * BPM
    ldw(P, A_ACC); a.add(P, W); stw(A_ACC, P)
    lda(Z, A_PLAY); a.cmpbi(Z, 0); J('JE', 'at3')                   # gate: off half way
    a.cmpi(P, K // 2); J('JNC', 'at3'); a.lcall('arp_off')
    a.L('at3'); ldw(P, A_ACC); a.cmpi(P, K); J('JC', 'at4'); a.ret()
    a.L('at4'); a.subi(P, K); a.cmpi(P, K); J('JNC', 'at5'); a.clr(P)
    a.L('at5'); stw(A_ACC, P); a.lcall('arp_off')
    # next position in the pattern of L = N * octaves notes
    lda(T, A_N); lda(U, A_OCT); a.incb(U); a.ldbze(C, T); a.b(0x7C, U, C)   # MULUB C, U: C = L
    lda(T, A_IDX); lda(U, A_MODE)
    a.cmpbi(U, 3); J('JNE', 'an1')
    a.ld(W, TIMER1); a.ldbze(W, W); a.b(0x9C, C, W); a.ldb(T, W + 1); a.sjmp('an_set')   # RANDOM: TIMER1 % L
    a.L('an1'); a.cmpbi(U, 2); J('JE', 'an_ud')
    a.incb(T); a.cmpb(T, C); J('JNC', 'an_set'); a.clrb(T); a.sjmp('an_set')   # UP / DOWN: count, wrap
    a.L('an_ud'); a.cmpbi(T, 0xFF); J('JNE', 'an2'); a.clrb(T); a.clrb(Z); sta(A_DIR, Z); a.sjmp('an_set')
    a.L('an2'); a.ldb(Z, C); a.decb(Z); a.cmpb(T, Z); J('JNC', 'an3'); a.ldb(T, Z)   # list shrank: clamp
    a.L('an3'); a.cmpbi(Z, 0); J('JE', 'an_set')                    # one note: stay
    lda(U, A_DIR); a.cmpbi(U, 0); J('JNE', 'an_dn')
    a.incb(T); a.cmpb(T, Z); J('JNC', 'an_set'); a.ldbi(U, 1); sta(A_DIR, U); a.sjmp('an_set')
    a.L('an_dn'); a.cmpbi(T, 0); J('JNE', 'an4'); a.ldbi(T, 1); a.clrb(U); sta(A_DIR, U); a.sjmp('an_set')
    a.L('an4'); a.decb(T); a.cmpbi(T, 0); J('JNE', 'an_set'); a.clrb(U); sta(A_DIR, U)
    a.L('an_set'); sta(A_IDX, T)
    lda(U, A_MODE); a.cmpbi(U, 1); J('JNE', 'an5'); a.ldb(Z, C); a.decb(Z); a.subb(Z, T); a.ldb(T, Z)   # DOWN
    a.L('an5'); a.ldbze(W, T); lda(U, A_N); a.b(0x9C, U, W)          # DIVUB W, U: W lo = octave, hi = index
    a.ldbze(IDX, W + 1); a.ldb_lidx(T, IDX, A_LIST)
    a.ldb(Z, W)
    a.L('an6'); a.cmpbi(Z, 0); J('JE', 'an7'); a.addbi(T, 12); a.decb(Z); a.sjmp('an6')
    a.L('an7'); a.cmpbi(T, NOTE_TOP + 1); J('JNC', 'an8'); a.subbi(T, 12); a.sjmp('an7')   # top key at most
    a.L('an8'); sta(A_NOTE, T); lda(U, A_VEL); a.lcall('arp_send')
    a.ldbi(Z, 1); sta(A_PLAY, Z); a.ret()
    a.L('at_stop'); a.clr(W); stw(A_ACC, W)                          # off, or no notes: let go
    a.L('arp_off'); lda(Z, A_PLAY); a.cmpbi(Z, 0); J('JNE', 'ao_1'); a.ret()
    a.L('ao_1'); a.clrb(Z); sta(A_PLAY, Z); lda(T, A_NOTE); a.ldbi(U, KEY_UP)
    a.L('arp_send')                                                  # R1C note, R1D velocity (bit 7 = down)
    a.ldbi(X, KEY_TAG); a.ldi(R1A, QUEUE); a.lcall(P4); a.ret()
