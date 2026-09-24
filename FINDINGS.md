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

**The VA39 fix is only 82 bytes — and one of them is not TCMalloc.**
`cmp -l agy.va39 <google stock 1.2.7>` shows 82 changed bytes across 41
instructions, same file size: an in-place patch, not a rebuild. Disassembled,
40 of the 41 are TCMalloc address arithmetic — shifts `#42`->`#35` and
`#10`->`#3`, constants `2^42`->`2^35` and `2^44`->`2^37`, and the page-map top
bit `movk x9,#1,lsl #48` -> `lsl x9,x9,#39`.

The 41st, at file offset `0x68260c4`, is `mov x0,#439` -> `mov x0,#48`: the
faccessat2 fix below, nothing to do with the address space. Because it rode
inside a patch everyone called "the VA39 fix", this repo consumed it for months
without knowing it existed — and could not rebuild a working binary from a
stock release. `patch.py` now implements it directly.

**The TCMalloc 40 is dead code on 1.2.x.** Traced 2026-09-24 on a confirmed VA39
device (Android 16, aarch64; `mmap` hints honoured at 2^38, refused at 2^39):
stock Google 1.2.7 and 1.2.8 start, operate, and complete heavy multi-turn model
sessions with TCMalloc completely untouched (only the patches in `patch.py` applied).

Live `strace` measurements across startup and multi-turn interactive turns show:
- 3,227 `mmap` calls during `--version` (0 `MAP_FIXED_NOREPLACE`, 0 1GB reservations).
- 3,340 `mmap` calls during full interactive turns (0 calls to TCMalloc's system allocator).
- All active heap allocation is handled cleanly by Go's runtime allocator (using 64MB arena hints that the 39-bit kernel silently relocates).

Furthermore, the session was run under real memory pressure: the device was actively
swapping with 1.5–2.4 GB of system swap in use and under 150 MB of free physical RAM.
Stock `agy` operated flawlessly, consuming a lean **~80 MB resident RAM** (compared to
heavier tools like Claude Code which consumed 1.1–1.5 GB across its host and subagents).

`install.sh` keeps an empirical run-before-install safety check to ensure future Google
releases cannot introduce regressions, but stock builds are now the primary install path.

## Android's seccomp filter kills `faccessat2`

Syscall 439 is not in the filter's allowlist, and Android answers it with
**SIGSYS (signal 31)** rather than `ENOSYS`. Confirmed independently of `agy`:

    long r = syscall(439, -100, "/system/bin/sh", 1, 0x200);   /* dies, signal 31 */
    long r = syscall(48,  -100, "/system/bin/sh", 1);          /* fine */

Go's `os/exec.findExecutable` calls `unix.Eaccess` on a candidate that exists,
and only falls back to permission bits on `ENOSYS` — which never arrives. The
CLI dies before `main()`:

    SIGSYS: bad system call
    syscall.Syscall6(0x1b7, ...)                 <- 0x1b7 = 439
    os/exec.lookPath({..., 0x14})                <- LookPath("termux-clipboard-get")
    clipboard.init.0()

It only bites once `termux-clipboard-get` exists on `$PATH`, which is why it
looks intermittent across devices. Rewriting the number in the
`syscall.faccessat2` wrapper to 48 takes plain `faccessat`, which the filter
allows; `faccessat` has no flags argument, so `AT_EACCESS` is dropped — for a
single-uid app, the same answer.

Only the wrapper reached by `mov x5,xzr; mov x6,xzr; movz x0,#439; bl` is
rewritten. Four other `movz x0,#439` sites open functions nothing on this path
calls; `agy.va39` leaves them alone too.

**`proot` hides it, which is worth knowing before trusting a reproduction.**
Natively the raw syscall is fatal; under `proot` it returns `ENOSYS`:

    $ ./t439b                 # native
    Unknown signal 31         # SIGSYS, killed at the faccessat2 call

    $ proot ./t439b
    faccessat2 (439): ret=-1 errno=38  Function not implemented
    faccessat  (48):  ret=0

`ENOSYS` is exactly what Go's `findExecutable` waits for, so under `proot` the
fallback runs and nothing crashes. `proot` traces at the ptrace syscall-entry
stop, which the kernel takes before evaluating the seccomp filter, so a syscall
it does not implement never reaches the filter — not implementing 439
accidentally produces the behaviour Go expects and the platform filter refuses.

The practical consequence is inverted from the usual: this bug appears on a
native install and disappears inside the sandbox. Anyone reproducing it inside
`proot-distro` will conclude, wrongly, that it is not there.

**It is not a libc property.** Go issues this syscall itself. The number is a
literal in Go code (`movz x0, #439`); `syscall.Syscall6` spills the arguments,
puts it in `x8` and executes `svc #0`. No libc wrapper is consulted, so glibc,
musl and bionic are interchangeable here. A freestanding test settles it —
`-nostdlib -static`, zero `DT_NEEDED` entries, nothing but the raw `svc`:

    $ ./t439raw            # native, no libc linked at all
    Unknown signal 31      # SIGSYS
    $ proot ./t439raw
    (exit 0)

The split is traced versus untraced, not one libc versus another.

**So the glibc route is affected identically.** wallentx's twin-binary design
runs the engine against `$PREFIX/glibc/lib/ld-linux-aarch64.so.1`, which
changes nothing about syscall 439. That build escapes the crash only because
`agy.va39` already carries the faccessat2 patch — a glibc install of a *stock*
Google binary dies exactly as a musl one does.

(Termux's glibc package is not installed here, so that last case rests on the
freestanding test and the disassembly rather than on running it. Glibc's own
`faccessat()` does try 439 first and fall back on `ENOSYS`, so it would trip
the same trap if anything called it — but nothing does, because Go never gets
that far.)

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

## The auto-updater, and the one switch that turns it off

`third_party/jetski/cli/updater` spawns a background update process about a
second into every session. When an update lands it renames `agy.bin` to
`agy.bin.<unixnano>.old` and writes a stock Google build in its place, undoing
every patch here. The session that triggered it keeps running on the renamed
inode (`/proc/<pid>/exe -> ...old`), so the breakage only appears at the *next*
launch. It deletes its own `.old` backups afterwards, so the fallback copy does
not stay around either.

Observed 2026-09-22: it fired at 18:37:42 and again at 19:58:19 — 80 minutes
apart, the second time replacing a binary that had just been repaired by hand.

**`AGY_CLI_DISABLE_AUTO_UPDATE=true` turns it off, and nothing else does.** The
name is in the binary but in no help output, and the check is an exact
four-byte comparison:

    tbz  w0, #0, +12
    adrp/add x0, "AGY_CLI_DISABLE_AUTO_UPDATE"
    mov  x1, #27
    bl   os.Getenv
    cmp  x1, #4                       ; length must be exactly 4
    ldr  w3, [x0]
    mov  x4, #0x7274 ; movk #0x6575   ; 't','r','u','e'
    cmp  w3, w4
    cset x3, eq
    tbnz w3, #0, +4184                ; taken -> skip the update trigger

So `1`, `TRUE` and `yes` are accepted by the shell and ignored by the binary.
Set correctly, the log says:

    auto_updater.go:247] Auto-update disabled via environment variable
                         AGY_CLI_DISABLE_AUTO_UPDATE

There is no settings.json equivalent. `store.(*Manager)` carries accessors for
the other settings (`GetAllowNonWorkspaceAccess`, `GetAutoExecutionPolicy`,
`GetArtifactReviewMode`...) and none for updates.

**Testing this needs care.** `updater.ttlStillFresh` short-circuits the spawn if
the last check was under 15 minutes ago, logging `skipping update (fast path)`.
Two runs back to back therefore both look like the variable worked. Remove
`~/.gemini/antigravity-cli/last_check.timestamp` between runs, or the test
measures nothing.


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

## Patch sites across releases

All three bugs survive every release so far with byte-identical encodings;
only the addresses move. `patch.py --dry-run` on Google's stock binaries
(addresses are virtual; in the main text segment they equal file offsets):

| Site | 1.2.7 | 1.2.8 | 1.2.9 | 1.2.10 |
|---|---|---|---|---|
| load-bias `csel` ×5 (#1075) | `0x94e27e0`…`0x94e2880` | `0x9072e00`…`0x9072ea0` | `0x90bfca0`…`0x90bfd40` | `0x9147d80`…`0x9147e20` |
| TCB read (#1079) | `0x94e3538` | `0x9073b58` | `0x90c09f8` | `0x9148ad8` |
| `faccessat2` | — | `0x653b004` | `0x6569064` | `0x65b5024` |

### Two embedded helper executables, since at least 1.2.7

The binary carries two more aarch64 ELFs as data in its RW segment, and both
inherit bugs from the list above. Their offsets in the outer file:

| Embedded ELF | 1.2.8 | 1.2.9 | 1.2.10 |
|---|---|---|---|
| ripgrep | `0xaf19479` | `0xaf8b4b8` | `0xb089ae0` |
| webm_encoder | `0xb5043d9` | `0xb576418` | `0xb674a40` |

In 1.2.8 both start at odd offsets. `patch.py` used to scan the outer segments
and require 4-byte alignment relative to them, so it missed both images there
and patched 7 sites. In 1.2.9 and 1.2.10 the offsets happen to be aligned, so it
found 14 by accident. It now reads each embedded ELF's own program headers and
scans only their `PF_X` segments (see below). On every release so far that gives
14 sites: 7 in agy, 6 in ripgrep, 1 in webm_encoder. **An install of 1.2.8 made
with the old `patch.py` has unpatched copies of both helpers.** agy does not
start them on that release, and they could not run anyway (see the loader below).

**ripgrep** (~6.2MB): a Google build of `third_party/rust/ripgrep/v14`, linked
against the same TCMalloc/Abseil code. It has its own copies of the five
load-bias `csel`s and the TCB read, at image vaddrs `0x3bad40`…`0x3bade0` and
`0x3bba98` in 1.2.8/1.2.9, and `0x3bad60`…`0x3bae00` and `0x3bbab8` in 1.2.10.
In the tests so far agy never extracts or runs it. A search task in print mode
shelled out to `grep` instead.

**webm_encoder** (~15.8MB; its section headers come first in the file, so
sizing it from `e_shoff` gives far too small a number): a Go program,
`//third_party/jetski/cortex/utils/mcp/encoder:webm_encoder`, built with
Google's `go1.28-20260721-RC03`. It drives Chrome over CDP to record WebM. It
contains no TCMalloc or Abseil code, only the Go standard library's
`faccessat2` wrapper at image vaddr `0x1bdde4`, byte-identical in every
release:

    mov x2,x0; ldr x4,[sp,#0x88]; mov x5,xzr; mov x6,xzr
    movz x0,#439; bl Syscall6; cbz x2,...; cmp x2,#2

1.2.10 extracts it at startup ("Installing/updating embedded webm_encoder
binary to %s") to `~/.gemini/antigravity-cli/bin/webm_encoder`, together with
an `agentapi` launcher script there that re-executes the agy binary. The
extracted file is byte-identical to the patched embedded copy, so patching it
inside agy is enough.

Both helpers request `/usr/grte/v5/lib/ld-linux-aarch64.so.1`, the loader path
of Google's internal runtime. Ordinary systems do not have it. On Termux the
extracted webm_encoder fails with `cannot execute: required file not found`,
and the same would happen on any standard glibc distribution.

### Why the scan is per-ELF `PF_X`

`patch.py` searches only `PT_LOAD` segments with `PF_X`, both the binary's own
and those of each embedded ELF, found by `\x7fELF` plus a header sanity check
(ELF64, little-endian, `EM_AARCH64`, `ET_EXEC`/`ET_DYN`, program headers in
bounds). The embedded images live in the outer binary's RW segment, so a
`PF_X` filter on the outer headers alone would skip 7 of the 14 sites.
Checking alignment per image also fixes the 1.2.8 miss.

### 1.2.10 runs

1.2.10, patched at all 14 sites and installed into a scratch copy of the
installer, passes `--version`, `--help`, `agy models` (login and network), and
a print-mode (`-p`) task that uses tools.

## Caveat

Google does not publish the source (no go.mod, no .go files — the repo is
releases and issues only), and the binary is cgo-enabled Go with a large
statically-linked C++ component. So this is binary patching, not a rebuild,
and it is contingent on internals that any release can change.
