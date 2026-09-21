## Second instance: a hard-coded glibc TCB offset at `[tp - 0x260]`

Following up on the load-bias bug above, with a second occurrence of the same pattern in
what looks like the same component. Same 1.2.7 `linux_arm64` binary, same musl
environment.

After patching the five `csel` sites, the binary still faulted before `main()` — but only
sometimes, and apparently depending on the length of unrelated filesystem paths. A short
path for a preloaded library crashed; a longer one worked, with a sharp deterministic
threshold and no semantic explanation.

That turned out to be a symptom. The real cause:

```asm
94e3530: mrs  x9, TPIDR_EL0
94e3534: sub  x8, x9, #0x260
94e3538: ldr  x8, [x8]          ; faults: unmapped under musl
94e353c: cbz  x8, 0x94e356c     ; fallback, reads the same values from globals
```

`[tp - 0x260]`, along with `[tp - 0x258]` and `[tp - 0x250]` a few instructions later,
reads into the several hundred bytes glibc reserves below the thread pointer. musl's TCB
is much smaller, so that address is frequently unmapped.

Reading `TPIDR_EL0` from inside the process shows why the path length appeared to matter:

| preloaded lib path length | TP | `tp - 0x260` | result |
|---|---|---|---|
| 104–109 | `…77100` | `…76ea0` | **not mapped** → SIGSEGV |
| 110+ | `…29180` | `…28f20` | mapped → runs |

At the crashing lengths the thread pointer landed at `…77100` — 256 bytes into its page —
and its containing mapping was the binary's own `rw-p` segment *starting* at `…77000`, so
`tp - 0x260` fell off the front of the mapping. At longer lengths the thread pointer
landed inside a large anonymous `rw-p` region with space below it. Path length only
influenced where the loader placed things, and therefore where the thread pointer landed.

### The interesting part

**This code already handles the value not being available.** The `cbz` takes a fallback
at `0x94e356c` that produces the same two outputs from globals rather than TLS, behind a
guarded lazy initializer (`ldarb` / `tbz` → `0x94e35a0`), and returns `w0 = 1` exactly as
the TLS path does. It is a maintained path, not dead code.

So the uninitialized case is anticipated — the assumption is only that it manifests as a
**zero read from a glibc-allocated block**, rather than as an address that is not mapped
at all. On a runtime where the TCB is smaller than glibc's, the load faults before the
`cbz` can act on it.

### Workaround

Forcing the load to zero takes the existing fallback unconditionally:

```
94e3538:  ldr x8, [x8]  ->  mov x8, xzr     (0xf9400108 -> 0xaa1f03e8)
```

Verified across preloaded-library path lengths from 56 to 162 characters, including a
completed OAuth login and authenticated API calls. Four bytes.

### Suggested fix

The general case is the same as for the load-bias bug: an assumption about a glibc-shaped
runtime baked into direct memory access. Options, roughly in order of robustness:

1. **Do not reach into the TCB by fixed offset.** If these slots are a cache whose
   canonical values already live in the globals the fallback reads, `__thread` variables
   (or the existing globals) would avoid the layout dependency entirely.
2. **Guard the access.** If the fixed offset is a deliberate optimization, gate it on a
   check that the runtime actually provides that region rather than assuming it.

Taken together with the load-bias bug, both issues are hard-coded assumptions about
glibc's memory layout — one about prelinked dynamic-table pointers, one about TCB size —
in the same component. It may be worth auditing it for others of the same shape.

For what it is worth: with those two fixes, six instructions and 24 bytes in total, the
stock 1.2.7 `linux_arm64` binary runs under musl on Android with no functional gaps I
have found — interactive sessions, tool use, authenticated API calls, MCP servers and
subagents all work. Nothing else needed patching.
