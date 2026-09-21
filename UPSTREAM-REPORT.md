# SIGSEGV in `google_find_phdr` during static initialization on non-prelinked PIE loaders (root cause for #9, relates to #64)

## Summary

`agy` crashes with `SIGSEGV` (`si_addr=0x5be0`) before reaching `main()` under any
dynamic loader that does not hand it pre-relocated dynamic-section pointers — musl, and
by the same mechanism the 39-bit-VA ARM64 kernel in #64.

The root cause is a **load-bias heuristic in `google_find_phdr`** that systematically
selects the unrelocated value of dynamic pointer tags. It is not OS-specific and not a
libc compatibility gap: it is an internal assumption that `DT_*` pointer tags arrive
already absolute, which is only true in prelinked/glibc-style environments.

Worth emphasising **#64** over the Android reports: that reproducer is an ASUSWRT-Merlin
router running a stock ARM64 Linux kernel, with no Android or proot involved. This is a
general correctness bug in PIE handling; Android is simply where it is most commonly hit.

Crash logs point at `process_state.cc` (Abseil's failure signal handler), which is
misleading — that is the handler catching the fault and then faulting a second time while
collecting a backtrace. The observed double `dl_iterate_phdr` frame is that secondary
fault, not the original one.

**All five affected instructions are present in Google's own unmodified v1.2.7 release
binary, at the addresses given below.** I verified this against a clean download of
`agy_cli_linux_arm64.tar.gz` from the 1.2.7 release, not against any third-party build.

## Environment

- Architecture: `aarch64`
- Loader: musl 1.2.6 (`ld-musl-aarch64.so.1`); applies to any non-prelinking PIE loader
- Binary: `agy` v1.2.7, `agy_cli_linux_arm64.tar.gz`, unmodified Google release
- Kernel: `6.12.30-android16` aarch64, 39-bit user VA
- No proot, no container — the binary's interpreter was repointed at musl and the
  glibc-only symbols supplied by a small shim (see "Reproduction")

## 1. Root cause: the load-bias heuristic

`google_find_phdr` scans the dynamic table and, for each pointer tag, guesses whether the
value is a link-time offset needing `dlpi_addr` added, or an already-absolute address:

```asm
; tag-scan loop in google_find_phdr (this instance handles DT_HASH)
94e286c: ldr   x9,  [x12], #0x10        ; raw tag value
94e2870: sub   x15, x9, #0x1, lsl #12   ; raw - 0x1000
94e2874: add   x16, x9, x19             ; raw + dlpi_addr  (x19 = load bias)
94e2878: cmp   x15, x11
94e287c: cset  w15, hs
94e2880: csel  x9,  x9, x16, lo         ; keep raw if "looks absolute", else biased
```

`x11` is assembled from `mov x11, #-0xfff` + `movk x11, #0xfffe, lsl #32`, giving
`0xfffffffefffff001`.

The `lo` condition keeps the **raw** value when `(raw - 0x1000) <u 0xfffffffefffff001`.
Because that bound is near the top of the unsigned 64-bit range, the comparison is true
for essentially every realistic link-time offset:

| raw | `raw - 0x1000` | keeps raw? |
|---|---|---|
| `0x5bd8` (this binary's `DT_GNU_HASH`) | `0x4bd8` | yes |
| `0x100000` | `0xff000` | yes |
| `0xf540000` | `0xf53f000` | yes |

So the biased value is chosen only when the tag is *already* a high absolute address —
the prelinked layout. For a standards-conforming PIE, every `d_ptr` is a relative virtual
offset that **must** have `dlpi_addr` added, and the heuristic is wrong every time.

## 2. Fault mechanism

The v1.2.7 binary has `DT_GNU_HASH` at relative offset `0x5bd8` and **no legacy
`DT_HASH`** (`objdump -p`):

```
GNU_HASH  0x0000000000005bd8
```

The consuming code branches on which table exists:

```asm
94e28ac: orr   x11, x9, x10      ; x9 = DT_HASH, x10 = DT_GNU_HASH
94e28b4: cbz   x11, <bail>       ; bail only if BOTH are absent
94e28c4: cbz   x9,  0x94e28d8    ; no DT_HASH -> take the DT_GNU_HASH path
```

With no `DT_HASH`, the `DT_GNU_HASH` path is always taken. That path hashes a symbol name
(DJB2) and then reads the GNU hash header from the pointer:

```asm
94e32b8: ldp   w9, w10, [x1, #0x8]   ; x1 = 0x5bd8 -> dereferences 0x5be0
```

`0x5be0 = 0x5bd8 + 8`, exactly the `#0x8` displacement — the reported `si_addr`. The raw
offset is dereferenced as an absolute address and faults on an unmapped low page.

Sequence, from `strace -k`:

1. `.init_array` runs Abseil's initialization, which sets up failure-signal handling
   (the trace shows a `sigaltstack` query, an 8KB `mmap`, and repeated `gettid` in the
   five syscalls immediately preceding the fault).
2. Abseil performs a symbol lookup, entering `google_find_phdr`.
3. The heuristic returns the unrelocated `DT_GNU_HASH` (`0x5bd8`).
4. The `DT_GNU_HASH` lookup path dereferences `0x5be0` → **SIGSEGV**.
5. Abseil's now-installed handler traps it and walks `dl_iterate_phdr` to build a
   backtrace, faulting again inside the same walk — hence `process_state.cc` in the logs
   and the doubled frame.

The fault site is inside `google_find_phdr` itself (the function starts at `0x94e1c70`),
not in the unwinder. The unwinder only appears after the fact.

## 3. Verification

Patching the five `csel` sites to unconditionally take the biased address resolves the
crash completely. Addresses and opcodes are for the unmodified v1.2.7 `linux_arm64`
binary; vaddr == file offset in this build.

| vaddr | tag | original | patched |
|---|---|---|---|
| `0x94e27e0` | `DT_GNU_HASH` (`0x6ffffef5`) | `csel x10,x10,x16,lo` `9a90314a` | `mov x10,x16` `aa1003ea` |
| `0x94e281c` | `DT_VERSYM` (`0x6ffffff0`) | `csel x3,x16,x17,lo` `9a913203` | `mov x3,x17` `aa1103e3` |
| `0x94e2848` | `DT_STRTAB` (`5`) | `csel x4,x16,x17,lo` `9a913204` | `mov x4,x17` `aa1103e4` |
| `0x94e2864` | `DT_SYMTAB` (`6`) | `csel x8,x8,x16,lo` `9a903108` | `mov x8,x16` `aa1003e8` |
| `0x94e2880` | `DT_HASH` (`4`) | `csel x9,x9,x16,lo` `9a903129` | `mov x9,x16` `aa1003e9` |

(`mov Xd, Xm` assembles as `ORR Xd, XZR, Xm`. The `cmp`/`cset` pair is left intact —
`w15` is read later at `0x94e2898`.)

With this 20-byte change, v1.2.7 gets past this crash and runs under musl with no
glibc present:

- `--version` and `--help` (full flag table)
- subcommand dispatch — `models`, `mcp`, `update`
- SQLite state initialization under `~/.gemini/antigravity-cli/`
- OAuth flow start, including browser handoff
- a real HTTPS round trip: `agy update` fetches the manifest and correctly reports
  "You are already on the latest version"

### Caveat: this is not the only problem

Two further issues remain, so the patch above should be read as isolating one specific
bug rather than as a complete fix:

1. **TCMalloc's 48-bit VA assumption** (#9, #64) is independent and still applies; the
   runs above used a separate patch for it.
2. **A path-length sensitivity.** After the `csel` fix the binary still faults before
   `main()` when the supporting library's resolved path is shorter than ~108 characters
   (sharp, deterministic threshold between 108 and 110). The fault is at `0x94e3538`:
   `mrs x9, TPIDR_EL0; sub x8, x9, #0x260; ldr x8, [x8]` — a fixed negative offset from
   the thread pointer, faulting at a high address rather than the near-null of the
   load-bias bug. I have measured the dependence but not established the mechanism, so
   I am reporting it as an observation rather than a diagnosis. It may well be a further
   consequence of the same glibc-layout assumptions.

## 4. Suggested fix

The heuristic looks intended to tolerate prelinked objects where `d_ptr` is already
absolute. Comparing against a fixed constant cannot distinguish the two cases: a
link-time offset and an absolute address are not separable by magnitude alone.

Two more robust options:

1. **Range-check against the object's own mapping.** Test whether the value falls within
   `[dlpi_addr, dlpi_addr + max PT_LOAD vaddr+memsz)` and add the bias only when it does
   not. This handles prelinked and PIE objects correctly.
2. **Always add `dlpi_addr`.** This is correct for every standards-conforming ELF object,
   including PIE, and is what the platform dynamic linkers do. If prelinking support is
   still required, gate it on an explicit check for a prelinked object rather than on the
   pointer's value.

Option 2 is what the patch above implements, and it is sufficient for every case tested.

## Reproduction

The binary was run under musl by repointing `PT_INTERP` at `ld-musl-aarch64.so.1`,
mapping `DT_NEEDED` onto musl's libc, and supplying ten glibc-only symbols via a ~40-line
shim: `__open`, `__close`, `__read`, `__lseek` (glibc's `__`-prefixed syscall aliases),
`__pthread_register_cancel`, `__pthread_unregister_cancel`, `gnu_dev_major`,
`gnu_dev_minor`, `gnu_dev_makedev`, and `pvalloc`. None of these are implicated in the
crash — a control run without the shim fails at symbol resolution, before any code runs.

Diagnosis used `strace -k` for the backtrace and `objdump -d --start-address=` for the
disassembly (no gdb or lldb needed):

```sh
strace -k -f -o k.log ./agy --version
grep -A12 SEGV_MAPERR k.log
objdump -d --start-address=0x94e2740 --stop-address=0x94e28d0 ./agy
```

Ruled out by test: DNS/network (crashes identically with `--help`, no args, and under
`env -i`; argv is never parsed), `patchelf` restructuring the ELF (reproduced with an
in-place `PT_INTERP` edit preserving `phnum=16`), the shim itself, main-thread stack size,
static TLS pressure (`PT_TLS memsz` is 1112 bytes), and lazy vs. eager binding.
