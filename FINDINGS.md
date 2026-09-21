# agy on musl — how far it gets

Experiment: can the `claude-code-termux-musl` approach (repoint the interpreter
at musl, no glibc runtime) be applied to Google's Antigravity CLI?

Tested on aarch64 Termux, Android, 39-bit VA kernel, against wallentx's
patched `agy.va39` v1.2.7 engine.

## Result: it works, logged in

    $ agy models
    Fetching available models...
    gemini-3.8-flash-high	Gemini 3.8 Flash (High)
    gemini-3.1-pro-high	Gemini 3.1 Pro (High)
    ...

    $ agy --version
    1.2.7
    $ ./patched --help
    Usage of patched:
      --add-dir    Add a directory to the workspace (repeatable)
      --agent      Agent for the current CLI session
      ...

Five patched instructions (20 bytes) take Google's Antigravity CLI from
segfaulting before `main` to a working CLI on musl: version, help,
subcommand dispatch (`auth`, `mcp`), and far enough into the auth flow to
open a browser for Google login.

**No glibc, no proot.** The ~450MB Termux glibc package that wallentx's
build requires is not needed.

### The fix

`google_find_phdr` decides, for each dynamic-table pointer tag, whether the
value needs the load bias added:

    ldr  x9,  [x12], #0x10        ; raw tag value
    sub  x15, x9, #0x1, lsl #12   ; raw - 0x1000
    add  x16, x9, x19             ; raw + dlpi_addr (x19 = load bias)
    cmp  x15, x11                 ; x11 = 0xfffffffefffff001
    csel x9,  x9, x16, lo         ; keep raw if (raw-0x1000) <u x11

Because x11 is enormous, `(raw - 0x1000) <u x11` is true for any plausible
link-time offset, so **raw is always kept**. It only selects the biased value
when the tag is already a high absolute address — the prelinked/glibc shape.
For a PIE under musl, every dynamic pointer is a link-time offset needing the
bias, so the heuristic is wrong every time.

The binary has `DT_GNU_HASH` but no `DT_HASH`, so it always reaches the
`DT_GNU_HASH` path and dereferences `0x5bd8` as an absolute address —
`si_addr=0x5be0` is that plus the `ldp [x1,#0x8]` displacement.

Replace each `csel Xd, Xraw, Xbiased, lo` with `mov Xd, Xbiased`
(`ORR Xd, XZR, Xm`). The `cmp`/`cset` are left alone: `w15` is read later at
`0x94e2898`.

| vaddr | tag | was | becomes |
|---|---|---|---|
| `0x94e27e0` | `DT_GNU_HASH` | `csel x10,x10,x16,lo` `9a90314a` | `mov x10,x16` `aa1003ea` |
| `0x94e281c` | `DT_VERSYM`   | `csel x3,x16,x17,lo`  `9a913203` | `mov x3,x17`  `aa1103e3` |
| `0x94e2848` | `DT_STRTAB`   | `csel x4,x16,x17,lo`  `9a913204` | `mov x4,x17`  `aa1103e4` |
| `0x94e2864` | `DT_SYMTAB`   | `csel x8,x8,x16,lo`   `9a903108` | `mov x8,x16`  `aa1003e8` |
| `0x94e2880` | `DT_HASH`     | `csel x9,x9,x16,lo`   `9a903129` | `mov x9,x16`  `aa1003e9` |

Offsets are for wallentx's v1.2.7 `agy.va39`; vaddr == file offset here, but
verify the original opcode before writing rather than trusting the address.

### Network: works through the existing C DNS proxy

The `dns-proxy.c` from `claude-code-termux-musl` carries agy unmodified —
build it, export the usual proxy vars, and agy reaches the internet:

    $ ./patched update
    ⟳ Checking for updates... (current version 1.2.7)
    ✓ You are already on the latest version.

    strace: port-53 queries: 0   connects to proxy: 2

Control, same command with the proxy vars unset:

    ERROR: Failed to check for updates: ... dial tcp: lookup
    antigravity-cli-auto-updater-....run.app on [::1]:53:
    read udp [::1]:32771->[::1]:53: read: connection refused

So the full chain is proven: musl-linked agy -> bionic-side proxy ->
network. No glibc anywhere.

### TLS: Go finds no CA bundle on Android

Go's `crypto/x509` searches a fixed list of Linux CA-bundle paths
(`/etc/ssl/certs/ca-certificates.crt`, `/etc/pki/tls/certs/ca-bundle.crt`,
`/etc/ssl/ca-bundle.pem`, `/etc/ssl/cert.pem`, `/etc/ssl/certs`). **None exist
on Android.** Every TLS verification then fails:

    tls: failed to verify certificate: x509: certificate signed by unknown authority
    browser.go:133] consumerOAuth: token exchange failed

This is what blocked login: the browser handshake completes, but the
token-exchange POST cannot verify Google's certificate. Termux ships the bundle
at `$PREFIX/etc/tls/cert.pem`, so the wrapper exports `SSL_CERT_FILE` and
`SSL_CERT_DIR` (both honouring anything already set).

Note the telemetry POSTs to `play.googleapis.com/log` fail with the same error
every 5 seconds and are unrelated to whether login works — the line that matters
is `browser.go consumerOAuth`.

### Path-length sensitivity — solved: a glibc TCB assumption

After the load-bias patch the binary still faulted before `main()` when the shim's
resolved path was shorter than ~108 characters — a sharp, deterministic threshold
that appeared to depend on nothing semantic.

It was not about paths. Reading `TPIDR_EL0` directly inside the process shows the
real mechanism:

| shim path length | TP | `tp-0x260` | result |
|---|---|---|---|
| 104-109 | `…77100` | `…76ea0` | **not mapped** -> fault |
| 110+ | `…29180` | `…28f20` | mapped -> works |

At the crashing lengths the thread pointer landed at `…77100` — 256 bytes into its
page — and its mapping was `agy.bin`'s own `rw-p` segment *starting* at `…77000`, so
`tp - 0x260` fell off the front into unmapped space. At longer lengths the thread
pointer landed inside a large anonymous `rw-p` region with room below it.

The binary reads `[tp - 0x260]`, `[tp - 0x258]` and `[tp - 0x250]`: glibc reserves
several hundred bytes below the thread pointer, musl's TCB does not. Path length only
decided where the thread pointer happened to land.

**Fix:** the code already branches on `[tp - 0x260]` being zero and falls back to
reading the same values from globals, so the load is forced to zero and the fallback
is taken unconditionally:

    94e3530: mrs  x9, TPIDR_EL0
    94e3534: sub  x8, x9, #0x260
    94e3538: ldr  x8, [x8]        ->  mov x8, xzr   (0xf9400108 -> 0xaa1f03e8)
    94e353c: cbz  x8, <fallback>

Verified at path lengths from 56 to 162 characters, including a full authenticated
API call. The padded lib directory is gone.


### Verified

- `--version`, `--help`, subcommand dispatch (`models`, `update`, `mcp`)
- `update`: real HTTPS round-trip to Google's manifest endpoint, correct answer
- **A completed OAuth login.** The browser handshake, the token-exchange POST
  and the token write to `~/.gemini/antigravity-cli/antigravity-oauth-token` all
  succeed once `SSL_CERT_FILE` is set.
- **Authenticated API calls.** `agy models` returns the live model list from
  Google's API — token, HTTPS, response parsing, all working.

### Not yet verified

- Long-running sessions.

Everything else works as it would on a supported platform: model turns, tool
use (`Read`, `Bash`), multi-turn reasoning, streaming output, MCP servers (an
external server spawning `bun`, invoked through `CallMcpTool`) and subagents
(a full approval lifecycle over multiple steps, via `subagent_manager`).

## What was established

**wallentx's build is not a musl port.** It is a twin-binary design: a 20KB
bionic C bootstrapper (`agy`) that clears LD_PRELOAD and execs the engine
(`agy.va39`) against `$PREFIX/glibc/lib/ld-linux-aarch64.so.1`. It needs
Termux's full glibc package (~450MB).

**The VA39 fix is only 82 bytes.** `cmp -l agy.va39 <google stock>` shows 82
changed bytes, same file size — an in-place patch of TCMalloc's 48-bit address
assumption, not a rebuild. Reusable independently.

**musl needs only 10 symbols.** After rewriting DT_NEEDED (drop libresolv,
libpthread, libm, libdl, librt; map libc.so.6 -> libc.musl-aarch64.so.1),
the only unresolved symbols are:

    __open __close __read __lseek          (glibc's __-prefixed syscall aliases)
    __pthread_register_cancel              (cancellation bookkeeping)
    __pthread_unregister_cancel
    gnu_dev_major gnu_dev_minor gnu_dev_makedev   (out-of-line major/minor macros)
    pvalloc                                (obsolete page-aligned malloc)

All shallow. `shim.c` here implements them in ~40 lines; built as a 7KB .so it
resolves every one.

**Build the shim against musl, not bionic.** Compiling it normally links it to
bionic and it fails on `__register_atfork`. Link against the musl loader
directly: `cc -shared -fPIC -O2 -nostdlib -o libagyshim.so shim.c \
$PREFIX/lib/ld-musl-aarch64.so.1`

## Reproduce

    patchelf --set-interpreter $PREFIX/lib/ld-musl-aarch64.so.1 agy.va39
    patchelf --remove-needed libresolv.so.2 --remove-needed libpthread.so.0 \
             --remove-needed libm.so.6 --remove-needed libdl.so.2 \
             --remove-needed librt.so.1 agy.va39
    patchelf --replace-needed libc.so.6 libc.musl-aarch64.so.1 agy.va39
    patchelf --add-needed ./libagyshim.so agy.va39
    patchelf --set-rpath <dir containing shim> agy.va39
    env -u LD_PRELOAD ./agy.va39 --version

## Root cause: a load-bias heuristic in `google_find_phdr`

Disassembly names the function: **`google_find_phdr`** (symbol survives even
though most are stripped). The faulting instruction at `0x94e32b8` is:

    94e32b8: ldp w9, w10, [x1, #0x8]      <- x1 = 0x5bd8, faults at 0x5be0

And the binary's own dynamic tag:

    GNU_HASH  0x0000000000005bd8

`0x5be0 - 0x5bd8 = 8`, exactly the `#0x8` displacement. **x1 is the
`DT_GNU_HASH` pointer, used unrelocated.** The code is reading the GNU hash
header (`nbuckets`, `symoffset`) from a link-time offset treated as an
absolute address, so it dereferences into unmapped low memory.

### The bug is a heuristic, not a missing symbol

The tag-scan loop applies this to each pointer tag (here for `DT_HASH`, tag 4;
the same pattern repeats for `DT_STRTAB` tag 5 and others):

    ldr  x9,  [x12], #0x10        ; raw tag value
    sub  x15, x9, #0x1, lsl #12   ; raw - 0x1000
    add  x16, x9, x19             ; raw + dlpi_addr  (x19 = load bias)
    cmp  x15, x11
    cset w15, hs
    csel x9,  x9, x16, lo         ; pick raw if "looks absolute", else biased

That is a guess at whether `d_ptr` is already absolute (as a prelinked glibc
object reports) or needs the load bias added. **Under glibc the guess happens
to come out right; under musl it comes out wrong**, and the raw offset is used.

Then:

    orr  x11, x9, x10             ; x9 = DT_HASH, x10 = DT_GNU_HASH
    cbz  x11, <bail>              ; bail only if BOTH are absent
    cbz  x9,  <use x10 via 0x94e3260>   ; no DT_HASH -> use DT_GNU_HASH

This binary has **`GNU_HASH` but no `DT_HASH`** (confirmed with `objdump -p`),
so it always takes the `DT_GNU_HASH` path into the faulting code. The stock
Google binary carries the identical `GNU_HASH 0x5bd8`, so this is not
something patchelf or the shim introduced.

### Why the backtrace looked like an unwinder bug

The `dl_iterate_phdr` frames and the doubled entry are abseil's failure
handler collecting a backtrace **after** the first fault, then faulting again
inside the same walk — a nested crash, as suspected. `dlpi_name` was a red
herring: nothing here reads it.

### Ruled out by test

- **DNS / network.** Identical crash with `--help`, no args, and `env -i`;
  crashes before `main` (an invalid flag segfaults rather than printing usage).
  agy will still need a DNS proxy once it starts — just not this bug.
- **patchelf restructuring.** It grew phnum 16 -> 19, which looked suspicious
  given phdr walking. Tested with an in-place `PT_INTERP` edit (26-byte path
  via the short symlink `/data/data/com.termux/f/l`) keeping phnum=16, stub
  `.so` files for `DT_NEEDED`, and `LD_PRELOAD` for the shim: **same crash.**
- **The shim** (control run without it fails with the original ten symbols),
  **stack size** (8MB, 32MB identical), **TCMalloc address bits**, **static
  TLS** (1112 bytes), **lazy binding**.

### Tools

Termux has no gdb or lldb, but `pkg install strace` works and `strace -k`
unwinds. `pkg install binutils` gives `objdump`. Symbols are stripped, so
`llvm-symbolizer` returns `??`, but `objdump -d --start-address=` on the file
offsets from `strace -k` disassembles fine and some symbol names survive.

    strace -k -f -o k.log ./musltest --version
    grep -A12 SEGV_MAPERR k.log
    objdump -d --start-address=0x94e3278 --stop-address=0x94e32e0 musltest

## Caveat

Google does not publish the source (no go.mod, no .go files — the repo is
releases and issues only), and the binary is cgo-enabled Go with a large
statically-linked C++ component. So this is binary patching, not a rebuild,
and it is contingent on internals that any release can change.
