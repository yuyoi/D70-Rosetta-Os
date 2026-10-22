"""Rosetta Patcher: turn your own Roland D-70 v1.11 EPROM dumps into D-70 Rosetta OS (two 27C512 images to burn).
Roland's code is never shipped: you bring the two dumps (IC4 = even, IC9 = odd), or one combined 128 KB file.

window:        python tools/rosetta_patcher.py
command line:  python -I tools/rosetta_patcher.py --even IC4.bin --odd IC9.bin --out outdir
               python -I tools/rosetta_patcher.py --both combined_128k.bin --out outdir
Every build runs the full check set (a few minutes); the images are only written when everything passes.
"""
import argparse, glob, hashlib, io, os, queue, sys, tempfile, threading
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
BASE = getattr(sys, '_MEIPASS', os.path.join(HERE, '..'))     # bundled data in the .exe, the repo otherwise
FRAMES = os.path.join(BASE, 'frames', 'roland_chan_frames.npz')
MD5_EVEN = '3f9e12e8d73380a5c26c312940f1ff6e'                   # v1.11 IC4
MD5_ODD = '640e279056386577499544f826df9253'                    # v1.11 IC9


def load_dumps(even, odd, log=print):
    """-> (even bytes, odd bytes): two 64 KB dumps in any order, or one 128 KB combined image (odd may be None)"""
    a = open(even, 'rb').read()
    if not odd:
        if len(a) != 128 * 1024: raise ValueError('one file given: it must be the combined 128 KB image')
        a, b = a[0::2], a[1::2]; log('combined image split into IC4 (even) and IC9 (odd)')
    else:
        b = open(odd, 'rb').read()
    if len(a) != 64 * 1024 or len(b) != 64 * 1024:
        raise ValueError('each dump must be 64 KB (27C512), got %d and %d bytes' % (len(a), len(b)))
    ma, mb = hashlib.md5(a).hexdigest(), hashlib.md5(b).hexdigest()
    if (ma, mb) == (MD5_ODD, MD5_EVEN):
        a, b = b, a; log('the two dumps were the other way round: swapped (IC4 = even, IC9 = odd)')
    elif (ma, mb) != (MD5_EVEN, MD5_ODD):
        raise ValueError('not Roland D-70 v1.11 (MD5 %s / %s). Only v1.11 is supported so far. A bad read? '
                         'Read each EPROM twice and compare.' % (ma[:8], mb[:8]))
    log('Roland D-70 v1.11 found (IC4 %s, IC9 %s)' % (MD5_EVEN[:8], MD5_ODD[:8]))
    return a, b


class _Lines(io.TextIOBase):
    """stdout -> log(), one call per line (the build prints its progress)"""
    def __init__(self, log): self.log, self.buf = log, ''
    def write(self, s):
        self.buf += s
        while '\n' in self.buf:
            line, self.buf = self.buf.split('\n', 1)
            if line.strip(): self.log(line)
        return len(s)


def build(a, b, outdir, log=print):
    """run the Rosetta build (all checks) on the dumps -> (IC4 image path, IC9 image path)"""
    import boot_build as bb
    os.makedirs(outdir, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        ea, ob = os.path.join(td, 'IC4_even.bin'), os.path.join(td, 'IC9_odd.bin')
        open(ea, 'wb').write(a); open(ob, 'wb').write(b)
        bb.configure(ea, ob, outdir, FRAMES)
        old = sys.stdout
        sys.stdout = _Lines(log if log is not print else lambda s: old and old.write(s + '\n'))   # .exe: no stdout
        try:
            log('building and checking (a few minutes) ...')
            bb.main()
        finally:
            sys.stdout = old
    ic4 = max(glob.glob(os.path.join(outdir, 'D70_rosetta_v*_IC4_even.bin')), key=os.path.getmtime)
    ic9 = ic4.replace('_IC4_even.bin', '_IC9_odd.bin')
    combined = ic4.replace('_IC4_even.bin', '_combined.bin')
    if os.path.exists(combined): os.remove(combined)     # only what you burn (Roland-derived either way)
    return ic4, ic9


def run(even, odd, outdir, log=print):
    a, b = load_dumps(even, odd, log)
    ic4, ic9 = build(a, b, outdir, log)
    for p, chip in ((ic4, 'IC4'), (ic9, 'IC9')):
        log('%s: %s (MD5 %s)' % (chip, p, hashlib.md5(open(p, 'rb').read()).hexdigest()[:8]))
    log('done. Burn the two images into 27C512 EPROMs (we use W27C512): %s in IC4, %s in IC9. Keep your original '
        'chips. Hold EXIT at power-on for the plain D-70. Do not share the .bin files: they contain Roland code; '
        'share the patcher instead.' % (os.path.basename(ic4), os.path.basename(ic9)))
    return ic4, ic9


def gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext
    BG, PANEL, BLUE, GREEN, TXT = '#05080c', '#0d141c', '#2ec4ff', '#39ff88', '#cfeee0'
    root = tk.Tk(); root.title('Rosetta Patcher - D-70'); root.geometry('700x480'); root.configure(bg=BG)
    for ico in (os.path.join(BASE, 'tools', 'rosetta_patcher.ico'), os.path.join(HERE, 'rosetta_patcher.ico')):
        if os.path.exists(ico):
            try: root.iconbitmap(ico)
            except tk.TclError: pass
            break
    tk.Label(root, text='ROSETTA OS  //  D-70 PATCHER', bg=BG, fg=GREEN, font=('Consolas', 15, 'bold')).grid(
        row=0, column=0, columnspan=3, sticky='w', padx=10, pady=(10, 2))
    lab = dict(bg=BG, fg=BLUE, font=('Consolas', 10))
    ent = dict(bg=PANEL, fg=TXT, insertbackground=GREEN, relief='flat', font=('Consolas', 10), highlightthickness=1,
               highlightbackground='#1b2a36', highlightcolor=BLUE)
    btn_s = dict(bg=PANEL, fg=GREEN, activebackground=BLUE, activeforeground=BG, relief='flat', font=('Consolas', 10, 'bold'))
    v_even, v_odd = tk.StringVar(), tk.StringVar()
    v_out = tk.StringVar(value=os.path.join(os.path.expanduser('~'), 'Rosetta_D70'))
    dumps = [('EPROM dump', '*.bin *.BIN *.rom *.ROM'), ('all', '*.*')]
    def row(r, label, var, pick):
        tk.Label(root, text=label, anchor='w', **lab).grid(row=r, column=0, sticky='w', padx=10, pady=5)
        tk.Entry(root, textvariable=var, width=60, **ent).grid(row=r, column=1, sticky='we', padx=4, ipady=3)
        tk.Button(root, text='...', command=pick, width=3, **btn_s).grid(row=r, column=2, padx=10)
    row(1, 'IC4 dump (even)', v_even, lambda: v_even.set(filedialog.askopenfilename(
        title='IC4 (even) EPROM dump, or one combined 128 KB image', filetypes=dumps) or v_even.get()))
    row(2, 'IC9 dump (odd)', v_odd, lambda: v_odd.set(filedialog.askopenfilename(
        title='IC9 (odd) EPROM dump', filetypes=dumps) or v_odd.get()))
    row(3, 'Output folder', v_out, lambda: v_out.set(filedialog.askdirectory() or v_out.get()))
    root.columnconfigure(1, weight=1)
    tk.Label(root, text="You need dumps of your own D-70's v1.11 EPROMs (IC4 + IC9, 27C512). Leave IC9 empty for one "
             "combined 128 KB file.", bg=BG, fg='#4f6b7a', font=('Consolas', 9), wraplength=670, justify='left').grid(
        row=4, column=0, columnspan=3, sticky='w', padx=10)
    logw = scrolledtext.ScrolledText(root, height=14, bg=PANEL, fg=GREEN, insertbackground=GREEN, relief='flat',
                                     font=('Consolas', 9), highlightthickness=1, highlightbackground='#1b2a36')
    logw.grid(row=6, column=0, columnspan=3, sticky='nsew', padx=10, pady=10)
    root.rowconfigure(6, weight=1)
    q = queue.Queue()
    def log(s): q.put(s)
    def pump():
        while not q.empty():
            s = q.get()
            if s == '__done__': btn.config(state='normal'); continue
            logw.insert('end', s + '\n'); logw.see('end')
        root.after(100, pump)
    def go():
        if not v_even.get(): messagebox.showerror('Rosetta Patcher', 'Pick your IC4 dump first.'); return
        btn.config(state='disabled')
        def work():
            try: run(v_even.get(), v_odd.get() or None, v_out.get(), log)
            except Exception as ex: log('ERROR: %s' % ex)
            log('__done__')
        threading.Thread(target=work, daemon=True).start()
    btn = tk.Button(root, text='BUILD ROSETTA OS', command=go, height=2, width=24, **btn_s)
    btn.config(fg=BG, bg=GREEN, activebackground=BLUE); btn.grid(row=5, column=0, columnspan=3, pady=6)
    pump(); root.mainloop()


def main():
    if len(sys.argv) == 1: return gui()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--even', help='IC4 (even) dump, 64 KB')
    ap.add_argument('--odd', help='IC9 (odd) dump, 64 KB')
    ap.add_argument('--both', help='one combined 128 KB image instead of --even / --odd')
    ap.add_argument('--out', required=True, help='output folder for the two images')
    a = ap.parse_args()
    if not (a.both or (a.even and a.odd)): ap.error('give --even and --odd, or --both')
    try:
        run(a.both or a.even, None if a.both else a.odd, a.out)
    except ValueError as ex:
        sys.exit('ERROR: %s' % ex)


if __name__ == '__main__':
    main()
