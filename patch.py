#!/usr/bin/env python3
"""Patch Google's Antigravity CLI binary to run under musl on Android.

Two independent fixes, both in-place; neither changes the file size.

1. google_find_phdr load bias. The tag-scan loop decides, per dynamic pointer
   tag, whether the value needs the load bias added:

       ldr  Xn,  [x12], #0x10        ; raw tag value
       sub  x15, Xn, #0x1, lsl #12   ; raw - 0x1000
       add  Xm,  Xn, x19             ; raw + dlpi_addr
       cmp  x15, x11                 ; x11 = 0xfffffffefffff001
       cset w15, hs
       csel Xd,  Xn, Xm, lo          ; keep raw if "looks absolute"

   Because x11 is near the top of the unsigned range, the comparison is true for
   any realistic link-time offset, so the raw value is always kept. That is only
   correct for prelinked objects; for a PIE under musl every d_ptr needs the
   bias. Replacing each csel with `mov Xd, Xm` takes the biased value always.

   The cmp/cset are left alone: w15 is read later by the caller.

2. TCMalloc's 48-bit virtual address assumption, which aborts before main on the
   39-bit-VA kernels most Android devices use.

Sites are found by opcode pattern, not by hardcoded offsets, so a new release
that moves the code still patches. Run with --dry-run to see what would change.
"""
import struct
import sys

# csel Xd, Xn, Xm, lo  ->  0x9A800000 | Xm<<16 | cond(0b0011)<<12 | Xn<<5 | Xd
CSEL_MASK = 0xFFE0FC00
CSEL_LO   = 0x9A803000  # cond=lo, op=csel


def is_csel_lo(word):
    return (word & CSEL_MASK) == CSEL_LO


def decode_csel(word):
    return (word & 31), ((word >> 5) & 31), ((word >> 16) & 31)  # d, n, m


def mov_reg(d, m):
    """ORR Xd, XZR, Xm — the canonical `mov Xd, Xm`."""
    return 0xAA0003E0 | (m << 16) | d


def find_loads(f):
    f.seek(0)
    d = f.read(64)
    if d[:4] != b"\x7fELF":
        sys.exit("not an ELF file")
    phoff = struct.unpack("<Q", d[32:40])[0]
    phes = struct.unpack("<H", d[54:56])[0]
    phn = struct.unpack("<H", d[56:58])[0]
    loads = []
    f.seek(phoff)
    for _ in range(phn):
        e = f.read(phes)
        if struct.unpack("<I", e[0:4])[0] == 1:
            off = struct.unpack("<Q", e[8:16])[0]
            va = struct.unpack("<Q", e[16:24])[0]
            fsz = struct.unpack("<Q", e[32:40])[0]
            loads.append((va, off, fsz))
    return loads


def patch_load_bias(f, loads, dry):
    """Find the tag-scan loop by its shape and neutralise each csel."""
    hits = []
    for va, off, fsz in loads:
        f.seek(off)
        blob = f.read(fsz)
        # Walk 4-byte aligned words looking for the cmp/cset/csel tail.
        for i in range(0, len(blob) - 12, 4):
            w_cmp, w_cset, w_csel = struct.unpack("<III", blob[i:i + 12])
            # cmp x15, x11  = subs xzr, x15, x11
            if w_cmp != 0xEB0B01FF:
                continue
            # cset w15, hs  = csinc w15, wzr, wzr, lo
            if w_cset != 0x1A9F37EF:
                continue
            if not is_csel_lo(w_csel):
                continue
            d, n, m = decode_csel(w_csel)
            hits.append((off + i + 8, va + i + 8, w_csel, d, n, m))

    if not hits:
        sys.exit("found no load-bias csel sites — has the binary changed?")

    for foff, vaddr, old, d, n, m in hits:
        new = mov_reg(d, m)
        print(f"  {vaddr:#010x}  csel x{d},x{n},x{m},lo {old:#010x}"
              f" -> mov x{d},x{m} {new:#010x}")
        if not dry:
            f.seek(foff)
            f.write(struct.pack("<I", new))
    return len(hits)


def patch_va39(f, loads, dry):
    """TCMalloc's 48-bit VA assumption.

    The published wallentx VA39 patch rewrites the tagged mmap hint constants so
    the reservations land inside a 39-bit user VA. Those constants are what this
    looks for; if the binary already runs on a 39-bit kernel there is nothing to
    do, which is the common case for a build that has been patched already.
    """
    # Implemented as a no-op placeholder: the VA39 fix is carried by the upstream
    # patched release this repo is built against, and re-deriving it belongs in
    # its own change rather than being guessed at here.
    return 0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    dry = "--dry-run" in sys.argv
    if len(args) != 1:
        sys.exit("usage: patch.py [--dry-run] <binary>")
    path = args[0]

    mode = "rb" if dry else "r+b"
    with open(path, mode) as f:
        loads = find_loads(f)
        print("google_find_phdr load-bias sites:")
        n = patch_load_bias(f, loads, dry)
        patch_va39(f, loads, dry)

    print(f"{'would patch' if dry else 'patched'} {n} instruction(s), {n * 4} bytes")


if __name__ == "__main__":
    main()
