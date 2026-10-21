# Roland D-70 Rosetta OS  (work in progress)

<p align="center"><img src="docs/rosetta_boot_d70.gif" width="720" alt="Hex girl boot animation on the D-70 LCD"></p>

<p align="center"><a href="https://discord.gg/xcuYZtmeyZ"><b>Join the Discord</b></a></p>

A new OS for the **Roland D-70** (1990 Super LA synthesizer), built one piece at a time. Today it patches Roland's
own v1.11 firmware: **Hex girl**, the Rosetta mascot, boots on the 240 x 64 LCD, the D-70 starts straight into the
**ROSETTA OS** menu, and its **SYNTH** pages edit the four layers live (wave, zone, pitch, filter, amp, both
envelopes with a drawn graph) while you play. The four part sliders become **common sliders** (each one moves one
parameter on all four layers) and the ROM PLAY demo is gone to make room. **The goal is to replace the stock OS
completely**: Roland's patch and layer structure out, our own engine, menus and live controls in.

It's the same Rosetta boot as the [SP-808 Rosetta OS](https://github.com/yuyoi/Sp-808-rosetta-looper-OS-) and the
[Sony HR-GP5 Rosetta OS](https://github.com/yuyoi/Sony-HR-GP5-Rosetta-OS), now on a Roland synth.

> **Work in progress.** v0.5 runs on a real D-70 (boot, ROSETTA OS menu, SYNTH pages, back to the stock OS and
> in again). Not every page has had a long session on hardware yet. Keep your original EPROMs.

> **Made by JunkSmithWizard (JSW) together with Claude (Anthropic's AI).** Claude did the reverse engineering
> (80C196 disassembly, bank and LCD mapping, panel scan, the parameter system, slider and ROM PLAY code) and wrote
> the tools, the player, both menus and the simulator. JSW repaired the unit (new PCM56P DAC, 4052, a wave-ROM
> address line), dumped the EPROMs, drew Hex girl, designed the menus, decided what goes and what stays, and burns
> and tests on the real D-70.

**Legal:** this repo contains no Roland code or ROM data. You dump **your own** unit's EPROMs and the tools patch
them on your PC. Don't share dumps or builds: share this repo instead.

## Previews

Rendered from the build checker, which runs the real ROM code against a model of the D-70's LCD (the real unit
uses the LCD's own font; the previews use a stand-in, and the tone names are made up).

<p align="center"><img src="docs/synth_v05.png" width="720" alt="ROSETTA OS and SYNTH pages"></p>
<p align="center"><img src="docs/boot_frames.png" width="720" alt="Boot animation frames"></p>
<p align="center"><img src="docs/menu_v02.png" width="720" alt="ROSETTA boot menu screens"></p>

## Status

| What | Status |
|---|---|
| Hex girl boot animation (45 frames, 5.6 s) | **works on hardware** |
| Boots into **ROSETTA OS**: SYNTH, TOOLS, ABOUT, EXIT TO D-70. [USER] opens it again from the stock OS. Hold **[EXIT]** at power-on for the plain D-70 | **works on hardware** (v0.5) |
| **SYNTH** pages: LAYERS (tone, zone bar, level per layer, key mode), WAVE / ZONE, PITCH, FILTER, FILTER ENV, AMP, AMP ENV, LFO. Two-column pages, envelope pages draw the curve. F5 / L1-U4 pick the layer, F5 also gives ALL LAYERS (one edit writes all four) | **works on hardware** (v0.5), more pages to come |
| Edits go through Roland's own SysEx parameter code: every value is checked, clamped and applied like an incoming SysEx message | works on hardware |
| **Boot menu** (hold [EDIT] at power-on): BOOT, SLIDERS, TOOLS, ABOUT | works on hardware |
| **Common sliders**: LOWER 1 = CUTOFF, LOWER 2 = RESONANCE, UPPER 3 = ATTACK, UPPER 4 = RELEASE, each on all four layers | built, sim-verified |
| **TOOLS > DUMP IC6 OVER MIDI**: battery RAM out as SysEx, `tools/ic6_decode.py` turns it back into a file | built, decode is byte-exact in the sim |
| ROM PLAY demo removed: **6.3 KB** of page 7 free (now holds the sliders and the SYNTH menu) | built |
| Settings saved in battery RAM | waits for an IC6 dump from a real unit |
| Firmware map: banks, LCD, keys, sliders, parameters, free space ([docs/FINDINGS.md](docs/FINDINGS.md)) | first pass |

## Roadmap

1. **Patch the stock OS** (now): boot, menus, live synth pages, common sliders, cuts.
2. **Gut it**: the RAM card UI, the patch and layer structure, the menus nobody needs. Keep the drivers:
   LCD, panel, MIDI, PCM card slots, sound chip, filter, effects. (Space is the limit now: 57 B left on page 7.)
3. **Replace it**: our own voice engine on Roland's sound chips, every button a live synth parameter, Rosetta
   menus everywhere, animations on the big screen. Ideas on the list: a real wave view, on-board PCM editing.

## How it works

- **CPU:** Intel 80C196KB at 12 MHz, firmware in two 27C512 EPROMs (IC4 even bytes, IC9 odd bytes, 128 KB).
  The CPU sees 64 KB: fixed ROM, a 16 KB window that shows one of eight ROM pages or the battery RAM, and work RAM.
  The stock firmware keeps **page 2** in the window and calls into it directly, so all our page-7 code calls stock
  routines through Roland's own far-call stubs (bank in, call, bank back).
- **LCD:** T6963C, 240 x 64, 6 pixels per byte, text and graphics layers XORed together. Stock uses two text pages
  (0x1400, 0x1540) and two graphics pages; the menu draws its text at 0x1680 and flips the LCD's home addresses,
  so the stock screen keeps updating underneath and comes straight back on EXIT.
- **The boot:** 3 bytes at the start of the normal boot path call a 680-byte player in free space. It parks a
  bit of work RAM in unused LCD memory, plays the frames straight to the LCD (timed on the CPU's own timer, 50 ms
  steps), then puts everything back exactly as it found it. Then the stock boot carries on.
- **The codec** (`tools/codec_d70.py`) codes each frame as changes against the previous one (skips, fills, copies
  from ahead, XOR bytes, replays). Hex girl's 45 frames go from 115 KB raw to 2.3 KB.
- **ROSETTA OS menu:** 3 bytes in the main loop swap Roland's UI step for ours (via a 7-byte trampoline into
  page 7). Menu closed: Roland's UI runs as before. Menu open: our key handling and drawing run instead, while MIDI,
  sound and the sliders keep going. Parameters are read and written through the firmware's SysEx address table.
- **Common sliders:** 3 bytes jump over Roland's play-mode slider loop. In four-layer mode each moved slider goes
  to its own parameter on all four layers; every other mode runs Roland's code untouched.
- **Every build is checked** before it's written: `tools/sim.py` runs the real ROM bytes (decoded by our own
  80C196 disassembler) against an LCD model and compares every frame, drives both menus through key tours, checks
  every parameter write, decodes the IC6 dump back, exercises the slider hook, and fails on anything the real CPU
  would get wrong (word reads at odd addresses, stock code called with the wrong ROM page in the window).

## Quick start

1. **Dump your EPROMs.** IC4 is the even one, IC9 the odd one. With a T48:
   `minipro -p "AM27C512@DIP28" -r dumps/IC4_even.bin` (and IC9). Read each twice and compare.
   The tools expect firmware **v1.11** (IC4 MD5 `3f9e12e8d73380a5c26c312940f1ff6e`,
   IC9 MD5 `640e279056386577499544f826df9253`) and refuse anything else.
2. `pip install pillow numpy`
3. `python -I tools/boot_build.py dumps/IC4_even.bin dumps/IC9_odd.bin` (a few minutes, most of it the checks)
4. **Burn two new chips and keep your originals.** 27C512 (we use W27C512):
   `build/D70_rosetta_v05_IC4_even.bin` goes in **IC4**, `build/D70_rosetta_v05_IC9_odd.bin` in **IC9**.
5. Power off, swap the chips (notch the same way round), power on. It boots into ROSETTA OS. Hold **[EXIT]** while
   switching on for the plain D-70, **[EDIT]** for the boot menu.

## Tools

| File | What |
|---|---|
| `tools/boot_build.py` | builds Rosetta OS from your dump, runs every check, writes the images and previews |
| `tools/player.py` | the boot player (80C196) |
| `tools/menu.py` | the boot menu: pages, keys, drawing, the IC6 dump |
| `tools/synth.py` | ROSETTA OS / SYNTH menu: pages, parameter access, drawing, envelope graphs |
| `tools/sliders.py` | the common-slider hook |
| `tools/cuts.py` | stock features removed and the space they free |
| `tools/codec_d70.py` | the frame codec |
| `tools/asm.py` | small 80C196 assembler; every instruction is decoded back to check it |
| `tools/mcs96.py` | 80C196 (MCS-96) disassembler |
| `tools/sim.py` | check-only 80C196 simulator with the D-70's LCD, banks and battery RAM |
| `tools/ic6_decode.py` | turns a captured IC6 dump (SysEx) into a 32 KB file |
| `tools/font57.py` | 5 x 7 preview font |
| `frames/roland_chan_frames.npz` | Hex girl boot frames (same as the SP-808 Rosetta boot) |
| `docs/FINDINGS.md` | the firmware map |

## Legal

Not affiliated with or endorsed by Roland Corporation. "Roland" and "D-70" are trademarks of Roland Corporation.
This repo contains **no Roland code or data**: you build from your own EPROM dumps. Hex girl art by JSW. Burn at
your own risk, and always keep your stock EPROMs.

License: MIT (see `LICENSE`).

---

**Thanks for enjoying, and God bless you!** — JSW
