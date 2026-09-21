# SIGSEGV before main(): hardcoded negative thread-pointer offset (TPIDR_EL0 - 0x260) faults on non-glibc TCB layouts

## Summary

`agy` reads thread-local state at a fixed negative offset from the AArch64 thread pointer:

```asm
94e3530: mrs  x9, TPIDR_EL0
94e3534: sub  x8, x9, #0x260
94e3538: ldr  x8, [x8]          ; SIGSEGV here
94e353c: cbz  x8, 0x94e356c     ; fallback: reads the same values from globals
```

On AArch64 glibc, `struct pthread` extends several hundred bytes below the thread pointer,
so `tp - 0x260` is always inside the TCB. On musl — and other minimal runtimes — the TCB
is much smaller. When the dynamic linker places the thread pointer near the start of a
mapped segment, `tp - 0x260` falls off the front into unmapped memory and the process dies
before `main()`.

A few instructions later the same pattern repeats with `[tp - 0x250]` and `[tp - 0x258]`.

This is a sibling of #1075 (the `google_find_phdr` dynamic-tag load bias), not a
continuation of it: different mechanism, different code path, different fix, and either
can be fixed and shipped without the other. Both are hardcoded assumptions about a
glibc-shaped runtime in what appears to be the same component, which may be worth an audit
for others of the same shape.

Reproduced on 1.2.7 `linux_arm64` (unmodified release binary) under musl 1.2.6 on
aarch64. Addresses below are from that build.

## The fallback already exists

This is the part I would draw attention to. The `cbz` immediately after the faulting load
branches to `0x94e356c`, which computes the same two output values from globals rather
than TLS, behind a guarded lazy initializer (`ldarb` / `tbz` → `0x94e35a0`), and returns
`w0 = 1` exactly as the TLS path does:

```asm
94e356c: adrp  x8, 0xb374000
94e3570: add   x8, x8, #0x120
94e3574: ldarb w8, [x8]            ; initialized flag, acquire
94e3578: tbz   w8, #0x0, 0x94e35a0 ; -> lazy initializer
94e357c: adrp  x8, 0xb374000
94e3580: ldr   x8, [x8, #0x128]
94e3584: str   x8, [x0]
...
94e3594: mov   w0, #0x1
```

So the "slot not populated" case is anticipated and handled. The assumption is narrower
than it first looks: not that the slot is always populated, but that it is always
*readable* — that an empty slot manifests as a zero inside a glibc-allocated block, rather
than as an address that is not mapped at all. On a runtime whose TCB does not extend that
far below the thread pointer, the load faults before the `cbz` can act on it.

## Why this looked like a filesystem-path bug

Worth recording, because the symptom is badly misleading. Whether the fault occurs depends
on where the dynamic linker happens to place the thread pointer, which depends on the
allocations it makes — including ones sized by the *path lengths* of the libraries it
loads. Reading `TPIDR_EL0` from inside the process:

| preloaded lib path length | TP | `tp - 0x260` | result |
|---|---|---|---|
| 104–109 chars | `…77100` | `…76ea0` | **not mapped** → SIGSEGV |
| 110+ chars | `…29180` | `…28f20` | mapped → runs |

At the crashing lengths the thread pointer landed at `…77100` — 256 bytes into its page —
and its containing mapping was the binary's own `rw-p` segment *starting* at `…77000`, so
`tp - 0x260` fell off the front. At longer lengths the thread pointer landed inside a large
anonymous `rw-p` region with space below it.

Sharp, deterministic, and entirely dependent on an unrelated string length. Anyone hitting
this without disassembling would have little chance of identifying the cause.

## Workaround

Forcing the load to zero takes the existing fallback unconditionally:

```
94e3538:  ldr x8, [x8]  ->  mov x8, xzr     (0xf9400108 -> 0xaa1f03e8)
```

Four bytes. Verified across preloaded-library path lengths from 56 to 162 characters,
including a completed OAuth login, authenticated API calls, MCP servers and subagents.

## Suggested fix

1. **Do not reach into the TCB by fixed offset.** If these slots are a cache whose
   canonical values already live in the globals the fallback reads, ordinary `__thread`
   variables (or just the globals) would remove the layout dependency entirely.
2. **Guard the access.** If the fixed offset is a deliberate optimization, gate it on
   something that establishes the runtime actually provides that region, rather than
   assuming it. The fallback to use when the check fails is already written.

## Reproduction

The binary was run under musl by repointing `PT_INTERP` at `ld-musl-aarch64.so.1`, mapping
`DT_NEEDED` onto musl's libc, supplying ten glibc-only symbols via a small shim, and
applying the #1075 load-bias patch (without which the process dies earlier). None of the
shim symbols are implicated here — the fault is a direct thread-pointer-relative load.

Diagnosis used `strace -k` for the backtrace and `objdump -d --start-address=` for the
disassembly, plus a small preloaded library that reads `TPIDR_EL0` in a constructor and
checks the target address against `/proc/self/maps`.

Full notes and the patcher: https://github.com/Aarstad/agy-termux-musl
