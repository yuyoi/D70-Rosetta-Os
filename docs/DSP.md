# D-70 effects chip (IC23, RCC): findings and the FX box

Everything here was measured on a real D-70 running Rosetta 0.8 with its outputs looped into a PC, and checked against
a bit-exact model of the chip.

## The chip

- IC23 = RCC (TC23SC140AF-007). It is **fixed-function**, not a DSP you can program: it runs one fixed list of steps
  every sample frame. You can still change every step's **gain**, **input/output cell** and **delay tap**. That is
  enough to rebuild the chorus section into other effects.
- Bit-exact reference: the RCC model in [gearmulator](https://github.com/dsp56300/gearmulator) (`ronaldo/custom_chips/rcc`,
  GPL-3). The research port to Python lives outside this repo for licence reasons. The tools here talk only to the real chip.
- **256 parameter words**, 24 bits each:

  | Bits | Meaning |
  |---|---|
  | 0-7 | gain, signed |
  | 8 | scale: set = /128 (fine), clear = /8 (up to ±15.875) |
  | 9-13 | RAM A cell (0-31) |
  | 14-17 | delay tap nibble (tap t = words 4t..4t+3) |

- **32 RAM A cells**, 24-bit. Some hold constants on hardware: c1 = 0, c2 = 0000C0 (output bias), c7 = 200000,
  c10 = 350000, c15 = E00000.
- **Port** 0x0A00-0x0A0F: data 0-2, write cell 4, write word 6, read cell 8, read word 0x0A. Stock firmware never
  reads the chip; Rosetta's DSP PEEK (v0.7) and DSP LAB (v0.8) do.

## How the D-70 uses it

- On **every note-on**, stock rewrites 123 "voice" words: 32 voice slots × (2 gains, read cell, write cell). Never
  patch these, or the next note overwrites them. 23 more words are **mixed**: they hold a voice role and an effect
  role at once, so leave them alone too. The full split is in [dsp/data/hw_word_classes.txt](../dsp/data/hw_word_classes.txt);
  the other 110 words are the static effect program.
- **Buses** (cell pairs, L/R). This is how a section's Output Assign lands:

  | Assign | Cells | Path |
  |---|---|---|
  | Dry | c24 / c30 | main mix |
  | Rev | c18 / c19 | reverb in + dry → main |
  | Cho | c20 / c21 | chorus → (CHO OUT POST REV) reverb bus → main |
  | Dir | c26 / c28 | direct outs |

- **Main mix:** c24 (L) / c30 (R). Output 0 reads c24 (×2, word 1C) and a seed step then clears it; output 6 reads
  c30 the same way (word DC).
- On real patches, much of the sound **does not use the Cho bus** even when the sections say Cho. An FX box in the
  chorus slot only caught 0-70% per MIDI channel; drums and some parts skipped it completely.

## FX box v3: a master insert (`dsp/fxbox.py`)

It captures the main mix just before the outputs read it, runs the effect in the chorus's steps, and the outputs
read the result. **Every part goes through it, whatever its Output Assign.**

- **Captures:**
  - L: outputCapture1 (word 08 → c24, gain 09, dest 0B), just before output 0 reads c24.
  - R: chorusReturnMix1 (word D1 → c30, gain D2, a second input D2/D4, dest D6). This step also makes the L+R sum
    for the mono modes.
- **Outputs:** output 0 / 6 read the result cells (words 19 / DB) at gains 1C / DC.
- **Chorus:**
  - The chorus wet goes silent (delay input C9 = 0, returns 03 / 6D / CB).
  - Its dry path stays, so Cho parts still reach the reverb.
- **Free cells:** 3 5 6 12 13 22 23.
- **Cost:**
  - No chorus.
  - Voice slots 1-2 (L) and 26 (R) are lost on that side whenever they land on the main bus between capture and
    read. That is one note now and then.

| Mode | What | Steps |
|---|---|---|
| `thru` | no effect (reference) | captures → outputs |
| `drive N` | ×N into the 24-bit rail, level matched (stereo) | capture gains, output gain 2/N |
| `crush K` | drop K of 24 bits, 7-17 (17 ≈ 5-bit), mono | /128 captures, mixBaseUpdate /2^(K-7), up again through phase3, ramp1 (two gains), chorusInputMix, chorusReturnMix0, outputs |
| `filter lp/bp/hp Hz Q` | resonant Chamberlin state-variable filter, mono | ramp1 bp += f·hp, mixBaseUpdate lp += f·bp, phase2 t = x − q·bp, phase3 hp = t − lp |
| `decimate N` | sample and hold for N frames (stereo) | R into the chorus delay (tap 53), L into tap 44 at +0x80; ramp1 counts and resets at N; chorus taps 0/1 read the held address |
| `rectify G` | full-wave rectifier (octave up), mono, 40 Hz DC blocker | phase1 fold = −\|x·G\|, ramp1 / mixBaseUpdate high-pass |

Tricks that make it work:
- ramp1's wrap with limit c15 (E00000) and gain −15.875 always returns the candidate, so ramp1 becomes a plain
  two-gain step.
- Fold gain 0 turns phase2 / phase3 into plain subtract steps.
- The chorus delay taps hold the same DRAM address while a ramp counts, which gives sample and hold.

### Hardware results (2026-10-29)

- **Bypass test:** box output muted while each MIDI channel 1-5 and 10 plays a chord. The result is −95 dB on
  every channel (noise floor), so **nothing skips the box**. The old chorus-slot box let 30-100% through.
- **Filters**, measured on the D-70:
  - LP 500: highs −18 dB.
  - HP 2500: lows −28 dB.
  - BP 1200: lows / highs −32 / −23 dB.
- **Drive, crush, decimate, rectify:** the model shows them working on every bus (crush 17 = 65 levels, decimate
  holds exactly N, rectify peaks at the octave with no DC). On the D-70 they are judged by ear: **"it's all great"**.
- Patches live in chip RAM. Changing the effect type or switching off brings stock back. `fxbox.py off` restores
  the saved stock words and checks them by readback.

## Tools (`dsp/`, need Rosetta 0.8+ and a MIDI loop to the D-70)

| File | What |
|---|---|
| `dsp/dsplab.py` | read / write words and cells over SysEx (`F0 7D 4C cmd nibbles F7`), full dump in 3 s |
| `dsp/fxbox.py` | the FX box: `thru`, `drive`, `crush`, `filter`, `decimate`, `rectify`, `off` |
| `dsp/hwab.py` | play a note, record the audio loop, measure body / tail |
| `dsp/hwscan.py` | knockout scan: zero each static word, measure what changes |
| `dsp/fxhw_master.py` | the bypass test + every mode recorded into one preview WAV |

Next: an **FX page** in Rosetta OS (mode + live parameters on the D-70 itself, no PC).
