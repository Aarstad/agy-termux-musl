# Antigravity CLI for Termux

Run Google's Antigravity CLI (`agy`) natively on Android inside Termux — with no glibc chroot, no proot, and no VM.

This installer sets up the ARM64 binary to run against a lightweight musl loader (~720KB instead of a ~450MB glibc environment), applies surgical binary patches for Android execution, and provides a small background proxy so networking and DNS work seamlessly.

Unlike heavier agent CLIs (which often hold 1.0–1.5 GB resident RAM across subagents and daemon hosts), `agy` sips a lean **~80 MB of RAM** while running live model turns natively in Termux.

---

## Quick Start

### 1. Requirements

- **Termux on ARM64 (`aarch64`)**: Run `uname -m` to verify it prints `aarch64`.
- **The musl loader**: `$PREFIX/lib/ld-musl-aarch64.so.1`  
  *(If you already installed [claude-code-termux-musl](https://github.com/Aarstad/claude-code-termux-musl), you already have this! Otherwise, see [Installing the musl loader](#installing-the-musl-loader) below).*
- **Build tools & dependencies**: `curl`, `tar`, `clang`, `python3`, `patchelf`.
- **A Google account** with Antigravity access.

### 2. Install

Open **Termux** and run:

```bash
# 1. Install prerequisites
pkg update
pkg install git curl tar clang python patchelf

# 2. Clone the repository
git clone https://github.com/Aarstad/agy-termux-musl.git
cd agy-termux-musl

# 3. Fetch, patch, and install
./install.sh

# 4. (Optional) Add agy to your PATH
mkdir -p ~/.local/bin
ln -sf "$PWD/agy" ~/.local/bin/agy
```

*(Note: Stock Google v1.2.7+ binaries run directly without TCMalloc issues. For legacy 1.0.x fallback or custom builds, `--from-binary <path>` is also supported).*

If `~/.local/bin` is in your `$PATH`, you can now run `agy` from anywhere.

### 3. Sign in and Verify

Run:

```bash
agy models
```

Follow the browser prompt to sign in with your Google account. Once authenticated, `agy` will list available models (e.g. Gemini 3.8 Flash, Gemini 3.1 Pro) and is ready for interactive coding sessions.

```bash
# Start an interactive session in your project
agy
```

### 4. Keeping it up to date

`agy`'s built-in auto-updater is off (see [Troubleshooting](#cannot-execute-required-file-not-found-on-launch)), so updates happen when you ask for them:

```bash
./agy-update --check      # compare installed against the latest release
./agy-update              # fetch it, patch it, verify it, install it
./agy-update --rollback   # go back to the previous binary
```

It resolves the newest release from GitHub, hands the binary to `install.sh` to patch and verify, and only replaces `agy.bin` once the patched build has been proven to run. The previous binary is kept as `agy.bin.prev` (a hard link, so it costs nothing until one of them changes); `--no-backup` skips it.

`./install.sh` on its own installs a **pinned** version, not the latest — use `agy-update` unless you want a specific build.

---

## Installing the musl Loader

`agy` runs against Alpine's musl dynamic linker rather than a heavy glibc package.

If `$PREFIX/lib/ld-musl-aarch64.so.1` is not already installed on your system, you can fetch and set it up quickly:

```bash
mkdir -p "$PREFIX/lib"
curl -fsSL -o "$PREFIX/lib/ld-musl-aarch64.so.1" \
  https://dl-cdn.alpinelinux.org/alpine/v3.20/main/aarch64/ld-musl-aarch64.so.1
chmod +x "$PREFIX/lib/ld-musl-aarch64.so.1"
```

---

## What Works

- **Interactive CLI & Full Turn Sessions**: Streaming output, multi-turn reasoning, and local SQLite state persistence.
- **Built-in Tool Execution**: File reading/writing, workspace search, terminal bash command execution.
- **Model Selection & Switching**: Seamlessly switch between Gemini 3.8 Flash, Gemini 3.1 Pro, etc.
- **Subagents**: Autonomous subagent spawning, task delegation, and lifecycle management.
- **MCP Servers**: Model Context Protocol servers (e.g. external servers running under `bun` or `node`) called via tool use.

---

## Troubleshooting & FAQ

### `cannot execute: required file not found` on launch
`agy` spawns a background auto-updater about a second into **every** session. When an update lands, it renames `agy.bin` to `agy.bin.<nanos>.old` and writes a stock Google build in its place, undoing every patch applied here. The stock build asks for glibc's loader, which Android does not have — so the *next* launch fails with this message, often hours later and with nothing on screen connecting the two.

The session that triggered the update keeps working, because the running process still holds the renamed inode (`/proc/<pid>/exe -> ...old`). Only the next start breaks.

Two things in the `agy` wrapper deal with this:
- **The updater is off by default.** The wrapper exports `AGY_CLI_DISABLE_AUTO_UPDATE=true`, which `agy` checks before spawning it (`auto_updater.go:247`). The value must be exactly `true` — the binary compares four bytes and ignores anything else, `1` included, without a word about it. The variable appears in no help output. Update deliberately with [`./agy-update`](#4-keeping-it-up-to-date), or allow the built-in updater for a single run with `AGY_ALLOW_UPDATE=1`.
- **If a stock build lands anyway**, the wrapper reads `PT_INTERP` before exec and re-runs `install.sh --from-binary` to put the patches back. Set `AGY_NO_REPAIR=1` to be told about it instead of repaired.

The updater also deletes its own `.old` backups, so don't count on one being there. Keep a copy of a known-good binary if you want a fast way back.

### TCMalloc abort / "Memory mapping failed" on startup
Historically, Google's bundled TCMalloc allocator was assumed to break on Android's 39-bit virtual address space (`VA39`) because of 48-bit address tags. Upstream issues (#9, #64) and early 1.0.x community builds relied on wallentx's `agy.va39` engine, which binary-patched 40 TCMalloc address-shift instructions.

- **Status in v1.2.7+ / v1.2.8**: **Stock Google binaries do not trigger the TCMalloc abort.** Tracing stock 1.2.8 with `strace` across startup and active multi-turn sessions shows **0 calls to TCMalloc's system allocator** (`MAP_FIXED_NOREPLACE` 1 GB reservations). Active heap allocations are handled by Go's runtime allocator (whose 64MB arena hints the Linux kernel safely relocates).
- **Stress-tested under real memory pressure**: Tested extensively on a confirmed VA39 device (Android 16 aarch64) under continuous low-memory conditions with 1.5–2.4 GB of active system swap. Stock unpatched TCMalloc completed all sessions without a single abort or mapping error.
- **Installation**: Stock binaries work directly via `patch.py`. wallentx's `agy.va39` remains supported as an optional fallback via `--from-binary`, but is no longer required on modern releases.

### Network hangs or TLS certificate errors
Android lacks standard Linux paths like `/etc/resolv.conf` and `/etc/ssl/certs/ca-certificates.crt`.
- The included `agy` wrapper automatically points to the shared `termux-dns-proxy` daemon on `127.0.0.1:18080` if running, or launches an ephemeral companion bionic DNS tunnel (`dns-proxy`) as fallback.
- Sets `SSL_CERT_FILE="$PREFIX/etc/tls/cert.pem"` for trusted root CA verification.
- If you run the raw binary directly (`./agy.bin`), DNS queries will hang. Always launch via `./agy` (or the symlink).

---

## How It Works (Under the Hood)

For the curious: Google distributes `agy` as a glibc-linked dynamic PIE executable. Making it run on Android under musl required solving five distinct hurdles:

1. **glibc to musl Loader & Shim (`shim.c`)**:
   The ELF interpreter is repointed to `ld-musl-aarch64.so.1`. A tiny 40-line C shim (`lib/libagyshim.so`) provides missing glibc symbols (`__open`, `__close`, `__read`, `pvalloc`, pthread cancellation stubs).
2. **Dynamic Phdr Load-Bias Heuristic (`patch.py`)**:
   The internal binary function `google_find_phdr` miscalculated load biases under non-prelinking loaders due to an unsigned 64-bit comparison against `0xfffffffefffff001`. Replacing 5 conditional selects (`csel`) with unconditional moves (`mov`) fixes the crash at startup (20 bytes patched).
3. **Thread-Control-Block (TCB) Fallback (`patch.py`)**:
   Glibc reserves several hundred bytes below the thread pointer (`[tp - 0x260]`). Musl has a smaller TCB, causing segfaults on unmapped memory. Forcing the check to zero routes execution into Google's built-in global fallback path (4 bytes patched).
4. **seccomp-blocked `faccessat2` (`patch.py`)**:
   Go's `os/exec.LookPath` calls `unix.Eaccess` on any candidate that exists, which issues syscall 439. Android's seccomp filter answers an unknown syscall number with `SIGSYS` rather than `ENOSYS`, so Go never reaches its permission-bit fallback — the CLI dies in `clipboard` package init, before `main()`, from the moment `termux-clipboard-get` is on `$PATH`. The number in the `syscall.faccessat2` wrapper is rewritten to 48, plain `faccessat`, which the filter allows (4 bytes patched).
5. **Android DNS & TLS Integration (`dns-proxy.c` / `termux-dns-proxy`)**:
   A lightweight, single-threaded `epoll` + `splice(2)` proxy bridges network lookups to Android's bionic resolver, while Termux's certificate bundle provides trusted root CAs. Can run as a shared background service (`termux-dns-proxy`) across all musl tools.

Detailed analysis, disassembly traces, and offset tables are documented in [FINDINGS.md](file:///data/data/com.termux/files/home/projects/agy-termux-musl/FINDINGS.md).

---

## Related Projects

- **[claude-code-termux-musl](https://github.com/Aarstad/claude-code-termux-musl)** — Claude Code on Termux via musl.
- **[codex-termux](https://github.com/Aarstad/codex-termux)** — OpenAI Codex CLI on Termux.
- **[wallentx/antigravity-cli-termux](https://github.com/wallentx/antigravity-cli-termux)** — The pioneer project that discovered the TCMalloc VA39 patch and proved running `agy` on Termux was possible. Its `agy.va39` also quietly carried the `faccessat2` fix — 1 of the 41 instructions it changes — which this repo depended on without knowing until it implemented the patch itself.

## License

MIT for the code and scripts in this repository. Google's `agy` binary is owned by Google and is not redistributed here.
