# D-70 firmware map (v1.11)

What we found in the stock firmware and what Rosetta OS hooks. Addresses are CPU addresses unless they say
"file" (offset in the 128 KB image: IC4 even bytes, IC9 odd bytes, interleaved).

## Hardware

| What | Where |
|---|---|
| CPU | Intel N80C196KB (IC15), 12 MHz, 16-bit bus. `LJMP` / `LCALL` are PC-relative |
| Program | IC4 (even) + IC9 (odd), 27C512 each = 128 KB |
| Work RAM | IC5 (low byte) + IC10 (high byte), 6264-type, `0xC000-0xFFFF` |
| Battery RAM | IC6, 32 KB, byte-wide, seen through the window |
| Gate array | IC14: address decode, bank register, chip selects |
| LCD | T6963C, 240 x 64, 6-dot font mode (40 bytes per row) |
| Sound | IC24 + IC25 (MB87419 / MB87420 PCM), IC22 MB87424 (TVF), IC23 (effects) |

## Address map

| CPU | What |
|---|---|
| `0x0000-0x00FF` | CPU registers |
| `0x0100` | bank register (write only). Bits 0-2 = ROM page in the window, bit 5 = IC6 instead of ROM, 0x30/0x31 = card |
| `0x0400-0x07FF` | key scan gate array |
| `0x0800` / `0x0802` | LCD data / command + status |
| `0x0900-0x09FF` | PCM sound chip |
| `0x0A00-0x0AFF` | effects DSP |
| `0x0C00-0x0CFF` | TVF chip |
| `0x1000-0x7FFF` | ROM, always visible (file `0x1000-0x7FFF`) |
| `0x8000-0xBFFF` | window: ROM page 0-7 (file `page * 0x4000`), or IC6 half 0 / 1 (bank 0x20 / 0x21) |
| `0xC000-0xFFFF` | work RAM |

Far calls: stubs at `0x1050-0x105F` select ROM page 2-7 and jump through that page's table at `0x8000`.

The stock firmware never selects page 0. The board does decode it (MAME agrees), and Rosetta uses it: file
`0x0000-0x0FFF` was empty (the CPU sees registers and I/O there, not ROM).

## LCD

- Graphics at LCD RAM `0x0000` (40 x 64 bytes, bit 5 = leftmost pixel), text at `0x1400` (40 x 8), CG RAM `0x1800`.
- Mode: graphics XOR text. Display flags in register `RA2`, mode command = `0x90 | RA2`.
- LCD RAM `0x0A00-0x13FF` is unused: Rosetta parks work RAM there while the boot animation runs.

## Buttons and sliders

- Panel scan (page 4 `0xA971`) bit-bangs PORT1 into `D378-D37F`: 8 banks of 8 keys, set bit = pressed.
  Bank order = MAME's KEY0-KEY7 (bank 2 bit 0 = [8], which is the power-on "Clear all memory" key).
- Power-on keys are checked at `0x11E0`; the normal boot path continues at `0x130D` (Rosetta's hook).
- Sliders: A/D interrupt (page 5 `0xBB95`) reads 13 channels; the four part sliders land in `D39C-D39F`.
- Play-mode control keys (LEVEL, PAN, TUNING, CUTOFF, RESONANCE, ATTACK, RELEASE) all go to `0x4EE1`, which
  sets `R65` = key code - 0x29 (CUTOFF 3, RESONANCE 4, ATTACK 5, RELEASE 6).
- Play-mode slider loop `0x4CB6`: for each layer `R58` it applies that layer's slider to control `R65`
  (scale `0x4DC9`, write `0x4E32` into the layer buffer). `D3B5` = 1 is the four-layer mode.

## What Rosetta changes

| Change | Where |
|---|---|
| Boot hook: `LCALL` the player | `0x130D` (3 bytes) |
| Boot player + IC6 copy helper | `0x2100-0x23A7` (free space) |
| Common sliders: `LJMP` over the stock loop | `0x4CB6` (3 bytes) -> `0x23A8-0x23FE` |
| ROM PLAY entry -> `RET` | `0x4A9E` (1 byte) |
| Boot frames (Hex girl, 2.3 KB) | file `0x0004-0x0916`, magic `JSW1` at `0x0000` |
| ROSETTA menu code | file `0x0920-0x0FAB` (runs at `0x8920`, page 0) |
| Menu pages and text | page 3 `0xBDA4` (free space), copied to RAM `0xE300` when the menu opens |

## Cut: ROM PLAY demo

The demo song ("Schizoid" by Eric Persing) and everything ROM PLAY loads lived on page 7, `0x83A9-0x9C47`
(6303 bytes): setup, 5 parts, 20 tones, the demo performance (copied into IC6 after backing up yours) and the
song. With the entry returning at once, nothing reads it. The power-on test mode right before it is kept.

## Free space

| Where | Bytes |
|---|---|
| Fixed ROM `0x2083-0x23FF` | full (1 byte left) |
| Page 7 `0x83A9-0x9C47` (the demo) | 6303 |
| Page 0 after the menu | about 80 |
| Page 2 `0xBEDA`, page 4 `0xBE54`, page 5 `0xBDFC` | 294, 428, 516 |
| Page 3 after the menu text | about 76 |

## IC6 dump (TOOLS > DUMP IC6 OVER MIDI)

256 SysEx messages `F0 7D 49 half block <256 nibbles, high first> F7`, byte address = half * 0x4000 +
block * 128, 0.4 ms per byte (about 27 s). `tools/ic6_decode.py capture.syx ic6.bin` turns a capture back
into the 32 KB image. It's how we will find bytes that are safe for Rosetta's settings.

## MAME

MAME has a D-70 driver. 0.270 runs it (the newest build crashed at start on our Norwegian Windows). The panel
scan emulation drops key presses; a Lua read tap on `D378-D37F` gets them in. With a blank IC6 the emulated
unit sits on the "No RAM Card!" popup, so it is not a full test bench yet.
