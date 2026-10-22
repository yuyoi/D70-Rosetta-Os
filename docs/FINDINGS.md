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

MAME has a D-70 driver. 0.270 runs it (the newest build crashed at start on our Norwegian Windows). It is a
working test bench for Rosetta OS with the debugger on (`-debug -debugger none`) and a Lua script:
- keys: a breakpoint at page 4 0xA5DB (right after the panel scan) ORs the key bit into `D378-D37F`
  (MAME's scan drops panel keys, and Lua read taps don't reach the CPU's own reads);
- CMPL (C5) is missing from MAME's CPU core: two conditional breakpoints at page 5 0xA006 stand in for it;
- with a blank IC6 every stored item points at the RAM card and the popup loops: an emulator-only copy of the
  build with fixed 0x3E23 = CLRC ; RET gets past it (never burn that copy);
- debugger breakpoint numbers are hex.

## v0.3-v0.5: runtime menu findings

- **Home bank:** boot sets R54 = 2 (0x117F). Stock code expects ROM page 2 in 0x8000-0xBFFF and calls it directly
  (e.g. 0x385B LCALL 8496). Code on another page must call stock routines through the far stubs
  (0x1050/53/56/59/5C/5F = pages 2/3/4/5/6/7: push R54, push #16E9, bank in, BR [R1A]; 0x16E9 banks back).
- **Main loop:** 0x1470; the UI step is LCALL 33FF at 0x151D. Panel scan page 4 0xA971 fills D378-D37F, key
  reader page 4 0xA5D8 (via LD R1A,#8006 ; LCALL 1056) returns C set and R1C = key code.
- **Parameters:** SysEx address table page 5 0xA040. Read 0x9DEF (RD6:RD4 flat address -> RCA), write 0x9E7E
  (RCA -> address, clamps, dirty bits in RDE), apply 0x10CB via the page-2 stub. Flat address = a2<<14 | a1<<7 | a0.
  Temporary tone: 0x0351 + (part*4 + layer)*0x3E + offset. Patch layer: 0x019E + part*0x57 + 0x23 + layer*0x0D + offset.
- **LCD pages:** stock text at 0x1400 and 0x1540 (draw base R9A, shown R9C), graphics 0x0000 and 0x0A00 (R9E, RA0),
  CG RAM 0x1800 (stock loads an ASCII font, so text codes are ASCII). Free text RAM: 0x1680-0x17FF.
- **Words at odd addresses** read the even word (A0 ignored): tables of pointers must be word aligned.
- **CMPL (opcode C5)** is used by the stock SysEx code (page 5 0xA006); MAME 0.270's CPU core lacks it.

## v0.6 findings

- **Keyboard:** key ISR 0x15BC reads 0x0609 (key, note = (k & 0x7F) + 0x1C = 28-103 on the 76 keys) and 0x0608
  (velocity: **bit 7 set = key down**, clear = key up with release velocity) into the ring at RAM 0xC200. The page-4
  pump 0xB481 fetches events through the far-call entry 0x1092 (-> 0x1F42) and queues them with tag 0x10 at 0xC300
  (0xB49F); 0x7910 hands them to page 6 0x8295, which sends MIDI 9n and keeps one byte per key at 0xEBC7.
- **Alpha Dial:** counted on the panel board. Page 4 entry 0x8018 (-> 0xAA9C) clocks one byte out over PORT1 and
  negates it: R1C = signed clicks since the last read. Stock edit screens call it through the page-4 stub 0x1056.
- **Panel key codes:** table at page 4 0xA703 (64 entries, bank n of the scan bytes D378-D37F, bit b).
  EFFECT/CTRL = 0x1A, MIDI OUT = 0x19, Up 0x23, Down 0x24, Left 0x25, Right 0x26, Dec 0x27, Inc 0x28.
- **Screens:** play screen R56 (UI mode R5A = 0). Step routines 0x345A[R56], enter routines 0x3502[R56] (a screen
  change is STB R56 ; LCALL 0x52ED ; LJMP 0x34F4), display records page 5 0xAC46[R56] -> (R59, R57). R59 indexes the
  display lists on page 4 (pointer table 0x8064, 0x38 lists with the text inline). Edit UI modes: handlers 0x340C,
  display records page 5 0xB0D6. COMMAND's list: page 4 0xAD90, 29 x 17 bytes (16 characters + target R56).
- **MIDI OUT parameters** (performance common, flat 0x00AD + offset): 0xB8 velocity curve, 0xB9 velocity sens,
  0xBA key mode, 0xBB split, 0xBC-0xD0 controller maps, 0xD1-0xD4 velocity mode / threshold, 0xD5 + s * 7 section
  s: Tx channel, volume, program, transpose (24 = 0), zone low / high, MIDI OUT switch.
- **Effects:** after writing performance common 0xAD-0xB7, stock's effects reload (page 3 entry 0x8003) has to run.
- **LCD:** the D-70's T6963C ignores the bit set / reset commands; draw with read (C5) / OR / write (C4).
- **80C196:** a 32-bit MULU / DIVU result needs a 4-aligned register (R20, R24, ...).
