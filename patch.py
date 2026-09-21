#!/usr/bin/env python3
"""Patch Google's Antigravity CLI binary to run under musl on Android.

Two in-place fixes; neither changes the file size.

1. google_find_phdr load bias. The tag-scan loop decides, per dynamic pointer tag,
whether the value needs the load bias added:

    ldr  Xn,  [x12], #0x10        ; raw tag value
    sub  x15, Xn, #0x1, lsl #12   ; raw - 0x1000
    add  Xm,  Xn, x19             ; raw + dlpi_addr
    cmp  x15, x11                 ; x11 = 0xfffffffefffff001
    cset w15, hs
    csel Xd,  Xn, Xm, lo          ; keep raw if "looks absolute"

Because x11 is near the top of the unsigned range, the comparison is true for
any realistic link-time offset, so the raw value is always kept. That is only
correct for prelinked objects; for a PIE under musl every d_ptr needs the bias.
Replacing each csel with `mov Xd, Xm` takes the biased value always.

The cmp/cset are left alone: w15 is read later by the caller.

2. A read into glibc's reserved space below the thread pointer, at
   [tp - 0x260]. musl's TCB is smaller, so that address is often unmapped and
   the binary faults before main() -- and whether it does depends on where the
   thread pointer lands relative to its mapping, which is why it tracked the
   length of unrelated paths. The code already branches on that value being
   zero and falls back to reading globals, so the load is forced to zero.

This does NOT patch TCMalloc's 48-bit virtual-address assumption, which aborts
before main() on the 39-bit-VA kernels most Android devices use. That is a
separate fix (~82 bytes, retargeting TCMalloc's address-bit shifts from 48 to
39) and this repo does not reimplement it -- start from wallentx's already
VA39-patched engine instead, via install.sh --from-binary. See README.

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


# The TLS read: `mrs x9, TPIDR_EL0; sub x8, x9, #0x260; ldr x8, [x8]`.
MRS_TPIDR = 0xD53BD049  # mrs x9, tpidr_el0
SUB_260   = 0xD1098128  # sub x8, x9, #0x260
LDR_X8_X8 = 0xF9400108  # ldr x8, [x8]
MOV_X8_XZR = 0xAA1F03E8  # mov x8, xzr


def read_segments(f, loads):
    """Read each PT_LOAD once; both patchers scan the same buffers."""
    segs = []
    for va, off, fsz in loads:
        f.seek(off)
        segs.append((va, off, f.read(fsz)))
    return segs


def patch_tcb_read(f, segs, dry):
    """Neutralise a read into glibc's reserved space below the thread pointer.

    The binary reads [tp - 0x260] and branches on whether it is zero:

        mrs  x9, TPIDR_EL0
        sub  x8, x9, #0x260
        ldr  x8, [x8]          <- faults under musl
        cbz  x8, <fallback>    <- fallback reads globals instead

    glibc reserves several hundred bytes below the thread pointer; musl's TCB
    is much smaller, so that address is often unmapped. Whether it faults
    depends on where the thread pointer happens to land relative to its
    mapping, which is why the crash tracks the length of unrelated paths.

    The code already handles the value being zero, taking a fallback path that
    reads the same data from globals. Forcing the load to zero takes that path
    unconditionally, which is the correct behaviour when the slot does not
    exist.
    """
    MRS = struct.pack("<I", MRS_TPIDR)
    hits = []
    for va, off, blob in segs:
        i = blob.find(MRS)
        while i != -1:
            if i % 4 == 0 and i + 12 <= len(blob):
                b2, c2 = struct.unpack("<II", blob[i + 4:i + 12])
                if b2 == SUB_260 and c2 == LDR_X8_X8:
                    hits.append((off + i + 8, va + i + 8))
            i = blob.find(MRS, i + 1)

    if not hits:
        # Distinguish "already patched" from "moved in a new release" by
        # looking for the same prologue with the load already neutralised.
        for va, off, blob in segs:
            i = blob.find(MRS)
            while i != -1:
                if i % 4 == 0 and i + 12 <= len(blob):
                    b2, c2 = struct.unpack("<II", blob[i + 4:i + 12])
                    if b2 == SUB_260 and c2 == MOV_X8_XZR:
                        print("  already patched")
                        return 0
                i = blob.find(MRS, i + 1)
        print("  no TCB-offset read found (new release?)")
        return 0

    for foff, vaddr in hits:
        print(f"  {vaddr:#010x}  ldr x8,[x8] {LDR_X8_X8:#010x}"
              f" -> mov x8,xzr {MOV_X8_XZR:#010x}")
        if not dry:
            f.seek(foff)
            f.write(struct.pack("<I", MOV_X8_XZR))
    return len(hits)


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


def patch_load_bias(f, segs, dry):
    """Find the tag-scan loop by its shape and neutralise each csel."""
    # `cmp x15, x11` is rare; find it with bytes.find (C speed) and check the
    # two following words, rather than unpacking every word in a 210MB file.
    CMP = struct.pack("<I", 0xEB0B01FF)   # cmp x15, x11
    CSET = 0x1A9F37EF                     # cset w15, hs
    hits = []
    for va, off, blob in segs:
        i = blob.find(CMP)
        while i != -1:
            if i % 4 == 0 and i + 12 <= len(blob):
                w_cset, w_csel = struct.unpack("<II", blob[i + 4:i + 12])
                if w_cset == CSET and is_csel_lo(w_csel):
                    d, n, m = decode_csel(w_csel)
                    hits.append((off + i + 8, va + i + 8, w_csel, d, n, m))
            i = blob.find(CMP, i + 1)

    if not hits:
        # Either already patched, or the code moved in a new release. Tell the
        # two apart by looking for the movs this would have written.
        done = 0
        for va, off, blob in segs:
            i = blob.find(CMP)
            while i != -1:
                if i % 4 == 0 and i + 12 <= len(blob):
                    b2, c2 = struct.unpack("<II", blob[i + 4:i + 12])
                    if b2 == CSET and (c2 & 0xFFE0FFE0) == 0xAA0003E0:
                        done += 1
                i = blob.find(CMP, i + 1)
        if done:
            print(f"  already patched ({done} site(s))")
            return 0
        sys.exit("found no load-bias csel sites — has the binary changed?")

    for foff, vaddr, old, d, n, m in hits:
        new = mov_reg(d, m)
        print(f"  {vaddr:#010x}  csel x{d},x{n},x{m},lo {old:#010x}"
              f" -> mov x{d},x{m} {new:#010x}")
        if not dry:
            f.seek(foff)
            f.write(struct.pack("<I", new))
    return len(hits)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    dry = "--dry-run" in sys.argv
    if len(args) != 1:
        sys.exit("usage: patch.py [--dry-run] <binary>")
    path = args[0]

    mode = "rb" if dry else "r+b"
    with open(path, mode) as f:
        loads = find_loads(f)
        segs = read_segments(f, loads)
        print("google_find_phdr load-bias sites:")
        n = patch_load_bias(f, segs, dry)
        print("glibc TCB-offset reads:")
        n += patch_tcb_read(f, segs, dry)

    print(f"{'would patch' if dry else 'patched'} {n} instruction(s), {n * 4} bytes")


if __name__ == "__main__":
    main()
