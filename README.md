# agy-termux-musl

Run **Google's Antigravity CLI natively on Android**, in Termux, with no glibc, no proot
and no VM.

Google publishes `agy` only as a glibc-linked binary, and it does not run on Android. The
usual answer is a glibc runtime in Termux (~450MB) or a proot container. This repo takes
the other route: run it against musl, which needs a 723KB loader instead, and fix the
four things that break along the way.

```
$ agy models
Fetching available models...
gemini-3.8-flash-high	Gemini 3.8 Flash (High)
gemini-3.1-pro-high	Gemini 3.1 Pro (High)
...
```

Logged in, authenticated, talking to Google's API from a phone.

Nothing here redistributes Google's binary. `install.sh` takes their published
release, applies **24 bytes** of patches, and builds a small shim beside it.

## What actually breaks, and why

Four independent Android-specific problems, each with a small fix:

**1. The interpreter and libc.** `agy` is a dynamic PIE wanting
`/lib/ld-linux-aarch64.so.1` and `GLIBC_2.26`. Android has no `/lib` and no glibc. The
interpreter is repointed at musl, `DT_NEEDED` is rewritten, and ten glibc-only symbols
are supplied by a ~40-line shim (`shim.c`): the `__`-prefixed syscall aliases
(`__open`, `__close`, `__read`, `__lseek`), two pthread cancellation stubs, the three
`gnu_dev_*` macros, and `pvalloc`.

Build the shim against the musl loader, not bionic — a normal build links to bionic and
dies on `__register_atfork`.

**2. A load-bias bug in Google's own code.** This is the interesting one.
`google_find_phdr` guesses whether each dynamic-table pointer tag needs the load bias
added, by comparing it against a constant:

```asm
sub  x15, x9, #0x1, lsl #12   ; raw - 0x1000
add  x16, x9, x19             ; raw + dlpi_addr
cmp  x15, x11                 ; x11 = 0xfffffffefffff001
csel x9,  x9, x16, lo         ; keep raw if "looks absolute"
```

Because `x11` sits near the top of the unsigned range, the comparison is true for any
realistic link-time offset, so the **raw** value is always kept. That is only right for
prelinked objects; for a PIE under musl every `d_ptr` needs the bias. The binary has
`DT_GNU_HASH` but no `DT_HASH`, so it dereferences `0x5bd8` as an absolute address and
faults at `0x5be0` before `main()`.

Five `csel` instructions, replaced with unconditional `mov`. 20 bytes.

**2b. glibc TCB offsets.** The binary also reads `[tp - 0x260]`, inside the several
hundred bytes glibc reserves below the thread pointer. musl's TCB is much smaller, so
that address is frequently unmapped and the process faults before `main()`:

```asm
mrs  x9, TPIDR_EL0
sub  x8, x9, #0x260
ldr  x8, [x8]          ; faults
cbz  x8, <fallback>    ; fallback reads the same data from globals
```

Whether it faults depends on where the thread pointer lands relative to its mapping,
which made it look like it depended on the *length of unrelated filesystem paths* — a
short shim path crashed, a long one worked. It does not; that was a symptom.

Since the code already branches on the value being zero and has a fallback that reads
globals, forcing the load to zero takes that path unconditionally. One instruction,
4 bytes.

This is not Android-specific — it breaks any PIE under any non-prelinking loader. Reported
upstream as
[antigravity-cli#1075](https://github.com/google-antigravity/antigravity-cli/issues/1075),
with the full analysis there. Related: [#9](https://github.com/google-antigravity/antigravity-cli/issues/9)
and [#64](https://github.com/google-antigravity/antigravity-cli/issues/64) are a separate
TCMalloc problem in the same family, and #64 is a plain ARM64 router with no Android
involved.

**3. DNS.** musl resolves through `/etc/resolv.conf`, which Android does not have, so DNS
inside the process hangs rather than failing. A small proxy runs on the bionic side, where
DNS works, and `agy` tunnels through it via `HTTPS_PROXY`. This is
[claude-code-termux-musl](https://github.com/Aarstad/claude-code-termux-musl)'s
`dns-proxy.c` unchanged — a single-threaded `epoll` + `splice(2)` tunnel, ~2.8MB resident.

**4. TLS certificates.** Go's `crypto/x509` looks for a CA bundle at
`/etc/ssl/certs/ca-certificates.crt` and four other standard paths. **None exist on
Android**, so every TLS verification fails with `certificate signed by unknown authority`
— including the OAuth token-exchange POST, which makes login impossible. Termux ships the
bundle at `$PREFIX/etc/tls/cert.pem`; the wrapper points `SSL_CERT_FILE` at it.

## Install

```bash
git clone https://github.com/Aarstad/agy-termux-musl
cd agy-termux-musl

# Start from wallentx's VA39-patched engine (see "TCMalloc" below):
curl -fsSL -o agy.tar.gz \
  https://github.com/wallentx/antigravity-cli-termux/releases/download/v1.2.7/antigravity-termux-standalone.tar.gz
tar xzf agy.tar.gz agy.va39
./install.sh --from-binary agy.va39

ln -sf "$PWD/agy" ~/.local/bin/agy   # optional, for PATH
```

**TCMalloc.** `agy` bundles TCMalloc, which assumes a 48-bit virtual address space and
aborts before `main()` on the 39-bit-VA kernels most Android devices use. That fix is
independent of everything in this repo and is **not** reimplemented here —
[wallentx's build](https://github.com/wallentx/antigravity-cli-termux/releases) already
carries it (82 bytes, in place), so the install above starts from that engine and applies
the load-bias patch on top. Running `./install.sh` with no arguments downloads Google's
stock binary and will abort in TCMalloc on such a device.

Then run `agy` and follow the browser prompts to log in.

## Requirements

- Termux on aarch64
- A musl loader at `$PREFIX/lib/ld-musl-aarch64.so.1` —
  [claude-code-termux-musl](https://github.com/Aarstad/claude-code-termux-musl)'s
  `install.sh` fetches it from Alpine
- `curl`, `tar`, `clang`, `python3`, `patchelf`
- ~250MB of storage, ~58MB of download
- A Google account with Antigravity access

## Known issues

None outstanding. The install path constraint that earlier versions carried is fixed —
see "glibc TCB offsets" below.


**Verified:** everything the CLI does. Model turns, tool use (`Read`, `Bash`), multi-turn
reasoning, streaming output, SQLite state, a completed OAuth login, authenticated API
calls, **MCP servers** (an external server spawning `bun`, called via `CallMcpTool`) and
**subagents** (full approval lifecycle across multiple steps). No errors.

**Untested:** long-running sessions only.

## Credits

- [wallentx/antigravity-cli-termux](https://github.com/wallentx/antigravity-cli-termux)
  for the TCMalloc VA39 patch and for showing this was possible at all. That build takes
  the glibc route (a bionic bootstrapper re-execing the engine against Termux's glibc
  package); this one takes the musl route instead.
- Google, for publishing the binary, and for `google_find_phdr`.

## License

MIT, for the code in this repo. Google's binary is theirs and is not redistributed here.
