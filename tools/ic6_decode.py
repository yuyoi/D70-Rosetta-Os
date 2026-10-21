"""ic6_decode.py <capture.syx> <out.bin> -- turn the ROSETTA 'DUMP IC6 OVER MIDI' SysEx into the 32 KB IC6 image.
Message: F0 7D 49 half blk <256 nibbles, high first> F7 ; byte address = half*0x4000 + blk*128."""
import sys


def decode(data):
    img = bytearray(0x8000); seen = set(); i = 0
    while True:
        s = data.find(b'\xf0\x7d\x49', i)
        if s < 0: break
        e = data.find(b'\xf7', s)
        m = data[s + 3:e]; i = e + 1
        if len(m) != 2 + 256: continue
        half, blk = m[0], m[1]; base = half * 0x4000 + blk * 128
        for k in range(128): img[base + k] = m[2 + 2 * k] << 4 | m[3 + 2 * k]
        seen.add(base)
    return bytes(img), len(seen)


if __name__ == '__main__':
    img, n = decode(open(sys.argv[1], 'rb').read())
    open(sys.argv[2], 'wb').write(img)
    print('%d of 256 blocks' % n)
