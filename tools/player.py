"""Rosetta boot player for the Roland D-70 (80C196KB). Port of the HR-GP5 / SP-808 boot player.

Called from the stock boot at 0x130D (LCALL replaces 'LDB INT_PEND,#00', which the player runs at its end).
At that point the LCD is initialised (page 6 0xB468) and nothing has been drawn yet.
- stream: file 0x0000-0x0FFF (free FF space), seen at CPU 0x8000 with bank register 0x0100 = 0. Starts 'JSW1'.
- frame buffer: RAM1 0xE000-0xE9FF. Its old contents are parked in LCD RAM 0x0A00-0x13FF (unused gap between
  the graphics area 0x0000-0x09FF and the text area 0x1400) and put back at the end; the gap is cleared again.
- each frame is pushed to LCD graphics 0x0000 with auto-write, then held N x 50 ms on TIMER1 (1.333 us/tick).
- at the end: graphics cleared, display mode back to 0x90|RA2 (stock's own flags), bank back to R54.
"""
from asm import Asm

ORG = 0x2100
STREAM = 0x8000            # CPU address of the stream (bank 0)
BUF, GB = 0xE000, 2560
GAP = 0x0A00
TICK = 37500               # 50 ms of TIMER1 at 12 MHz
LCD_D, LCD_C = 0x0800, 0x0802
BANK = 0x0100
PS, P, L, Q, T, V, HD, O, A, SV, TM, TD, X, ST, AD = (0x30, 0x32, 0x34, 0x36, 0x38, 0x39, 0x3A, 0x3C, 0x3E, 0x40,
                                                     0x42, 0x44, 0x46, 0x47, 0x4A)
SAVED = list(range(0x1C, 0x4C, 2))
MENU = 0x8920              # menu.py menu_entry (ROM page 0, bank 0)
KEY_EDIT = 0xD37D          # stock power-on key bank 5, bit 7 = [EDIT]
TIMER1, INT_PEND, R54, RA2 = 0x0A, 0x09, 0x54, 0xA2


def build():
    a = Asm(ORG)
    a.L('player')
    a.pushf()                                   # PSW + INT_MASK saved and cleared: no interrupts
    for r in SAVED: a.push(r)
    a.clrb(T); a.stb_abs(BANK, T)               # ROM page 0 into 0x8000
    a.ldi(PS, STREAM)
    for ch in b'JSW1':
        a.ldb_pi(T, PS); a.cmpbi(T, ch); a.jcc('JE', 'm%d' % ch); a.ljmp('out'); a.L('m%d' % ch)
    # park RAM1 buffer in the LCD gap, clear the buffer
    a.ldi(AD, GAP); a.lcall('setaddr'); a.ldi(P, BUF); a.ldi(L, GB); a.lcall('aw_ram')
    a.ldi(P, BUF); a.ldi(L, GB); a.clrb(V)
    a.L('clr'); a.stb_pi(P, V); a.dec(L); a.jcc('JNE', 'clr')
    a.ldbi(X, 0x98); a.lcall('cmd')             # graphics on, text off
    # ---- frame loop
    a.L('fl'); a.ldb_pi(HD, PS); a.cmpbi(HD, 0); a.jcc('JNE', 'fl1'); a.ljmp('done'); a.L('fl1')
    a.jbc(HD, 7, 'norm')
    a.ldb_pi(A + 1, PS); a.ldb_pi(A, PS)         # replay address, hi then lo
    a.ld(SV, PS); a.ld(PS, A); a.lcall('decode'); a.ld(PS, SV); a.andbi(HD, 0x7F); a.sjmp('show')
    a.L('norm'); a.lcall('decode')
    a.L('show'); a.ldi(AD, 0); a.lcall('setaddr'); a.ldi(P, BUF); a.ldi(L, GB); a.lcall('aw_ram')
    a.L('hold'); a.ld(TM, TIMER1)
    a.L('wl'); a.ld(TD, TIMER1); a.sub(TD, TM); a.cmpi(TD, TICK); a.jcc('JNC', 'wl')
    a.addi(TM, TICK); a.decb(HD); a.jcc('JNE', 'wl')
    a.ljmp('fl')
    # ---- end: clear graphics, put RAM1 back, clear the gap, restore display mode
    a.L('done')
    a.ldb_abs(T, KEY_EDIT); a.jbc(T, 7, 'nomenu'); a.lcall(MENU)   # [EDIT] held at power-on: ROSETTA menu
    a.L('nomenu')
    a.ldi(AD, 0); a.lcall('setaddr'); a.ldi(L, GB); a.lcall('aw_zero')
    a.ldi(AD, GAP); a.lcall('setaddr'); a.ldi(P, BUF); a.ldi(L, GB); a.lcall('rd_ram')
    a.ldi(AD, GAP); a.lcall('setaddr'); a.ldi(L, GB); a.lcall('aw_zero')
    a.ldb(X, RA2); a.orbi(X, 0x90); a.lcall('cmd')
    a.L('out')
    a.stb_abs(BANK, R54)                        # stock's bank back
    for r in reversed(SAVED): a.pop(r)
    a.popf()
    a.ldbi(INT_PEND, 0x00)                      # the instruction the hook replaced
    a.ret()

    # ---- decode one frame from [PS]+ into BUF (codec_d70 tokens)
    a.L('decode'); a.ldi(P, BUF)
    a.L('dl'); a.cmpi(P, BUF + GB); a.jcc('JNC', 'dl1'); a.ret(); a.L('dl1')
    a.ldb_pi(T, PS)
    a.cmpbi(T, 0xC0); a.jcc('JC', 't_c0')
    a.cmpbi(T, 0x80); a.jcc('JC', 't_80')
    a.cmpbi(T, 0x60); a.jcc('JC', 't_60')
    a.cmpbi(T, 0x40); a.jcc('JC', 't_40')
    a.cmpbi(T, 0x3F); a.jcc('JNE', 'tk'); a.ret(); a.L('tk')
    a.cmpbi(T, 0x20); a.jcc('JC', 't_20')
    a.ldbze(L, T); a.inc(L); a.sjmp('adv')
    a.L('t_20'); a.andbi(T, 0x1F); a.ldbze(L, T); a.inc(L); a.shl(L, 5)
    a.L('adv'); a.add(P, L); a.sjmp('dl')
    a.L('t_40'); a.andbi(T, 0x1F); a.ldbze(L, T); a.inc(L); a.scall('chk'); a.ldb_pi(V, PS)
    a.L('f4'); a.stb_pi(P, V); a.dec(L); a.jcc('JNE', 'f4'); a.sjmp('dl')
    a.L('t_60'); a.andbi(T, 0x1F); a.ldb(L + 1, T); a.ldb_pi(L, PS); a.inc(L); a.scall('chk')
    a.ldbze_pi(O, PS); a.add3(Q, P, O); a.sjmp('cp')
    a.L('t_c0'); a.andbi(T, 0x3F); a.ldbze(L, T); a.inc(L); a.scall('chk'); a.ld(Q, P); a.inc(Q)
    a.L('cp'); a.ldb_pi(V, Q); a.stb_pi(P, V); a.dec(L); a.jcc('JNE', 'cp'); a.sjmp('dl')
    a.L('t_80'); a.andbi(T, 0x3F); a.ldbze(L, T); a.inc(L); a.scall('chk')
    a.L('x8'); a.ldb_pi(V, PS); a.xorb_ind(V, P); a.stb_pi(P, V); a.dec(L); a.jcc('JNE', 'x8'); a.sjmp('dl')
    # bounds check: P + L must stay inside the buffer, else stop the whole animation
    a.L('chk'); a.add3(TD, P, L); a.cmpi(TD, BUF + GB); a.jcc('JH', 'bad'); a.ret()
    a.L('bad'); a.pop(TD); a.pop(TD); a.ljmp('done')       # drop chk + decode return addresses

    # ---- for the menu (which runs in ROM page 0): copy CNT bytes of IC6 bank R4A from [R36] to RAM [R34]
    a.L('ic6_copy'); a.stb_abs(BANK, AD)
    a.L('ic1'); a.ldb_pi(X, 0x36); a.stb_pi(0x34, X); a.dec(0x48); a.jcc('JNE', 'ic1')
    a.clrb(X); a.stb_abs(BANK, X); a.ret()

    # ---- T6963C, same handshakes as the stock driver (page 2 0x6719-0x6782)
    a.L('wst'); a.ldb_abs(ST, LCD_C); a.andbi(ST, 3); a.cmpbi(ST, 3); a.jcc('JNE', 'wst'); a.ret()
    a.L('cmd'); a.scall('wst'); a.stb_abs(LCD_C, X); a.ret()
    a.L('dat'); a.scall('wst'); a.stb_abs(LCD_D, X); a.ret()
    a.L('setaddr'); a.ldb(X, AD); a.scall('dat'); a.ldb(X, AD + 1); a.scall('dat'); a.ldbi(X, 0x24); a.sjmp('cmd')
    a.L('aw_ram'); a.ldbi(X, 0xB0); a.scall('cmd')
    a.L('a1'); a.ldb_abs(ST, LCD_C); a.jbc(ST, 3, 'a1'); a.ldb_pi(X, P); a.stb_abs(LCD_D, X)
    a.dec(L); a.jcc('JNE', 'a1'); a.ldbi(X, 0xB2); a.sjmp('cmd')
    a.L('aw_zero'); a.ldbi(X, 0xB0); a.scall('cmd')
    a.L('z1'); a.ldb_abs(ST, LCD_C); a.jbc(ST, 3, 'z1'); a.stb_abs(LCD_D, 0)
    a.dec(L); a.jcc('JNE', 'z1'); a.ldbi(X, 0xB2); a.sjmp('cmd')
    a.L('rd_ram'); a.ldbi(X, 0xC5); a.scall('cmd')         # read + increment, one byte (stock 0x677D)
    a.scall('wst'); a.ldb_abs(X, LCD_D); a.stb_pi(P, X); a.dec(L); a.jcc('JNE', 'rd_ram'); a.ret()
    return a
